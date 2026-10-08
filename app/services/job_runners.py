"""
app/services/job_runners.py
────────────────────────────
All background task implementations.

Each runner follows the same contract:
    async def run_*(job_id, file_paths, options) -> None

The `@job_runner` decorator handles the shared plumbing — concurrency limit,
"processing" → "complete"/"error" state transitions and error logging — so
each runner only contains its actual work and returns the list of outputs.
Blocking PyMuPDF / OCR calls run in worker threads, never on the event loop.
Output files are written to `results/{job_id}/`.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from pathlib import Path
from typing import Awaitable, Callable, List

import pymupdf as fitz   # PyMuPDF (>= 1.24.3)
import pandas as pd
from docx import Document

from app.core.config import (
    DEFAULT_OCR_DPI,
    MAX_CONCURRENT_JOBS,
    MAX_PDF_PAGES,
    RESULTS_DIR,
    UPLOAD_DIR,
)
from app.core.database import update_job
from app.services.ocr_engine import engine_input, extract_document, extract_text

log = logging.getLogger(__name__)

_slots = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

Output = dict
RunnerResult = tuple[list[Output], str | None]   # (outputs, preview_text)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _res_dir(job_id: str) -> Path:
    """Return (and create) the per-job results directory."""
    path = RESULTS_DIR / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _output(path: Path, output_id: str, fmt: str, label: str | None = None) -> Output:
    out = {"output_id": output_id, "format": fmt, "size_bytes": path.stat().st_size}
    if label:
        out["label"] = label
    return out


def _open_pdf(path: str) -> fitz.Document:
    """Open a PDF, rejecting encrypted files and oversized page counts."""
    doc = fitz.open(path)
    if doc.needs_pass:
        doc.close()
        raise ValueError("Encrypted PDFs are not supported.")
    if len(doc) > MAX_PDF_PAGES:
        n = len(doc)
        doc.close()
        raise ValueError(f"PDF has {n} pages; the limit is {MAX_PDF_PAGES}.")
    return doc


def job_runner(fn: Callable[..., Awaitable[RunnerResult]]):
    """Wrap a runner with concurrency limiting and job state management."""

    @functools.wraps(fn)
    async def wrapper(job_id: str, file_paths: List[str], options: dict) -> None:
        async with _slots:
            try:
                await update_job(job_id, status="processing", progress=10)
                outputs, preview = await fn(job_id, file_paths, options)
                await update_job(job_id, status="complete", progress=100,
                                 outputs=outputs, preview_text=preview)
                log.info("Job %s (%s) complete: %d output(s)", job_id, fn.__name__, len(outputs))
            except Exception:
                log.exception("Job %s (%s) failed", job_id, fn.__name__)
                await update_job(job_id, status="error")

    return wrapper


# ── OCR ────────────────────────────────────────────────────────────────────────

def _image_to_pdf(src: str, dest: str) -> None:
    with fitz.open(src) as img_doc:
        pdf_bytes = img_doc.convert_to_pdf()
    with fitz.open("pdf", pdf_bytes) as wrapped:
        wrapped.save(dest)


def _write_ocr_outputs(res: Path, full_text: str, md_text: str | None, tables: list) -> list[Output]:
    outputs: list[Output] = []

    p = res / "out_txt.txt"
    p.write_text(full_text, encoding="utf-8")
    outputs.append(_output(p, "out_txt", "txt"))

    # Engines like docling/marker/qwen provide real markdown.
    p = res / "out_md.md"
    p.write_text(f"# OCR Result\n\n{md_text or full_text}", encoding="utf-8")
    outputs.append(_output(p, "out_md", "md"))

    p = res / "out_docx.docx"
    doc_out = Document()
    doc_out.add_paragraph(_strip_xml_invalid(full_text))
    doc_out.save(str(p))
    outputs.append(_output(p, "out_docx", "docx"))

    # Tables may be DataFrames (docling/marker) or raw row-lists (PyMuPDF).
    if tables:
        p = res / "out_xlsx.xlsx"
        with pd.ExcelWriter(str(p)) as writer:
            for idx, tbl in enumerate(tables):
                df = tbl if isinstance(tbl, pd.DataFrame) else pd.DataFrame(tbl)
                df.to_excel(writer, sheet_name=f"Table_{idx + 1}", index=False, header=False)
        outputs.append(_output(p, "out_xlsx", "xlsx", "Extracted tables"))
    return outputs


def _strip_xml_invalid(text: str) -> str:
    """python-docx raises on control characters that OCR output can contain."""
    return "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)


@job_runner
async def run_ocr(job_id: str, file_paths: List[str], options: dict) -> RunnerResult:
    """
    Extract text (and optionally tables) from a PDF or image file.

    Outputs: out_txt.txt, out_md.md, out_docx.docx, and out_xlsx.xlsx when
    tables were found.
    """
    src   = file_paths[0]
    is_pdf = Path(src).suffix.lower() == ".pdf"
    model = options.get("ocr_model", "rapidocr")
    res   = _res_dir(job_id)

    md_text: str | None = None
    tables: list = []

    if engine_input(model) == "pdf":
        # Document-native engine (docling | marker): parses the PDF directly.
        pdf_src = src
        wrapped = None
        if not is_pdf:
            wrapped = UPLOAD_DIR / f"{job_id}_wrapped.pdf"
            await asyncio.to_thread(_image_to_pdf, src, str(wrapped))
            pdf_src = str(wrapped)
        try:
            result = await asyncio.to_thread(extract_document, pdf_src, model)
        finally:
            if wrapped is not None:
                wrapped.unlink(missing_ok=True)
        full_text = result["text"]
        md_text   = result.get("markdown")
        tables    = result.get("tables") or []
        await update_job(job_id, progress=90)

    elif is_pdf:
        pages: list[str] = []
        doc = await asyncio.to_thread(_open_pdf, src)
        try:
            total_pages = len(doc)
            for i, page in enumerate(doc):
                tab_result = await asyncio.to_thread(page.find_tables)
                if tab_result and tab_result.tables:
                    tables.extend(t.extract() for t in tab_result.tables)

                tmp_img = UPLOAD_DIR / f"{job_id}_page_{i}.png"
                try:
                    await asyncio.to_thread(lambda: page.get_pixmap(dpi=DEFAULT_OCR_DPI).save(str(tmp_img)))
                    # Heavy engines block for minutes on CPU — keep them off the event loop.
                    pages.append(await asyncio.to_thread(extract_text, str(tmp_img), model))
                finally:
                    tmp_img.unlink(missing_ok=True)

                await update_job(job_id, progress=10 + int((i + 1) / total_pages * 80))
        finally:
            doc.close()
        full_text = "\n\n".join(pages) + "\n\n" if pages else ""
    else:
        full_text = await asyncio.to_thread(extract_text, src, model)
        await update_job(job_id, progress=90)

    outputs = await asyncio.to_thread(_write_ocr_outputs, res, full_text, md_text, tables)
    return outputs, full_text[:2000]


# ── PDF → PNG ──────────────────────────────────────────────────────────────────

def _pdf_to_png(src: str, dpi: int, res: Path) -> list[Output]:
    outputs: list[Output] = []
    with _open_pdf(src) as doc:
        for i, page in enumerate(doc):
            out_id = f"out_page_{i + 1}"
            p = res / f"{out_id}.png"
            page.get_pixmap(dpi=dpi).save(str(p))
            outputs.append(_output(p, out_id, "png", f"Page {i + 1}"))
    return outputs


@job_runner
async def run_pdf_to_png(job_id: str, file_paths: List[str], options: dict) -> RunnerResult:
    """Rasterise each page of a PDF to an individual PNG file."""
    outputs = await asyncio.to_thread(_pdf_to_png, file_paths[0], options["dpi"], _res_dir(job_id))
    return outputs, None


# ── PNG → PDF ──────────────────────────────────────────────────────────────────

def _png_to_pdf(ordered: list[str], res: Path) -> list[Output]:
    out_id = "out_assembled"
    p = res / f"{out_id}.pdf"
    with fitz.open() as doc:
        for path in ordered:
            with fitz.open(path) as img_doc:
                pdf_data = img_doc.convert_to_pdf()
            with fitz.open("pdf", pdf_data) as img_pdf:
                doc.insert_pdf(img_pdf)
        doc.save(str(p))
    return [_output(p, out_id, "pdf", "assembled_document.pdf")]


@job_runner
async def run_png_to_pdf(job_id: str, file_paths: List[str], options: dict) -> RunnerResult:
    """Assemble one or more image files into a single PDF document."""
    ordered = [file_paths[i] for i in options["page_order"]]   # validated upstream
    return await asyncio.to_thread(_png_to_pdf, ordered, _res_dir(job_id)), None


# ── Split PDF ──────────────────────────────────────────────────────────────────

def _split_pdf(src: str, ranges: list[list[int]], res: Path) -> list[Output]:
    outputs: list[Output] = []
    with _open_pdf(src) as doc:
        for idx, (start, end) in enumerate(ranges):
            if end > len(doc):
                raise ValueError(f"Range {start}-{end} exceeds the document ({len(doc)} pages).")
            out_id = f"out_range_{idx}"
            p = res / f"{out_id}.pdf"
            with fitz.open() as segment:
                segment.insert_pdf(doc, from_page=start - 1, to_page=end - 1)
                segment.save(str(p))
            outputs.append(_output(p, out_id, "pdf", f"pages_{start}–{end}.pdf"))
    return outputs


@job_runner
async def run_split_pdf(job_id: str, file_paths: List[str], options: dict) -> RunnerResult:
    """
    Extract page ranges from a PDF into separate files.

    `options["ranges"]` is a list of [start, end] pairs (1-based), e.g. [[1, 3], [5, 8]].
    """
    outputs = await asyncio.to_thread(_split_pdf, file_paths[0], options["ranges"], _res_dir(job_id))
    return outputs, None


# ── Merge PDF ──────────────────────────────────────────────────────────────────

def _merge_pdf(ordered: list[str], res: Path) -> list[Output]:
    out_id = "out_merged"
    p = res / f"{out_id}.pdf"
    total = 0
    with fitz.open() as merged:
        for path in ordered:
            with _open_pdf(path) as src_doc:
                total += len(src_doc)
                if total > MAX_PDF_PAGES:
                    raise ValueError(f"Merged document would exceed {MAX_PDF_PAGES} pages.")
                merged.insert_pdf(src_doc)
        merged.save(str(p))
    return [_output(p, out_id, "pdf", "merged_document.pdf")]


@job_runner
async def run_merge_pdf(job_id: str, file_paths: List[str], options: dict) -> RunnerResult:
    """Merge multiple PDF files into a single document."""
    ordered = [file_paths[i] for i in options["file_order"]]   # validated upstream
    return await asyncio.to_thread(_merge_pdf, ordered, _res_dir(job_id)), None


# ── Dispatcher ─────────────────────────────────────────────────────────────────
# Maps action_type strings to async runner functions.

JOB_RUNNERS = {
    "ocr":        run_ocr,
    "pdf_to_png": run_pdf_to_png,
    "png_to_pdf": run_png_to_pdf,
    "split_pdf":  run_split_pdf,
    "merge_pdf":  run_merge_pdf,
}
