"""
app/services/job_runners.py
────────────────────────────
All background task implementations.

Each runner follows the same contract:
    async def run_*(job_id, file_paths, options) -> None

They update job state via `database.update_job` and write output
files to `results/{job_id}/`. Errors are caught and stored so the
UI can surface a meaningful status to the user.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import List

import fitz         # PyMuPDF
import pandas as pd
from docx import Document

from app.core.config import UPLOAD_DIR, RESULTS_DIR, DEFAULT_OCR_DPI, DEFAULT_EXPORT_DPI
from app.core.database import update_job
from app.services.ocr_engine import engine_input, extract_document, extract_text

log = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _res_dir(job_id: str) -> Path:
    """Return (and create) the per-job results directory."""
    path = RESULTS_DIR / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _size(path: Path) -> int:
    return path.stat().st_size


# ── OCR ────────────────────────────────────────────────────────────────────────

async def run_ocr(job_id: str, file_paths: List[str], options: dict) -> None:
    """
    Extract text (and optionally tables) from a PDF or image file.

    Outputs
    -------
    - {job_id}/out_txt.txt   — plain text
    - {job_id}/out_md.md     — Markdown document
    - {job_id}/out_docx.docx — Word document
    - {job_id}/out_xlsx.xlsx — Excel workbook (only when tables found)
    """
    try:
        await update_job(job_id, status="processing", progress=10)

        src      = file_paths[0]
        ext      = Path(src).suffix.lstrip(".").lower()
        model    = options.get("ocr_model", "rapidocr")
        res      = _res_dir(job_id)

        full_text: str = ""
        md_text: str | None = None
        tables: list   = []

        if engine_input(model) == "pdf":
            # ── Document-native engine (docling | marker) ───────────
            # These parse the PDF directly (layout + real tables).
            pdf_src = src
            if ext != "pdf":
                # Wrap a bare image into a one-page PDF so the engine can eat it.
                pdf_src = str(UPLOAD_DIR / f"{job_id}_wrapped.pdf")
                with fitz.open(src) as img_doc:
                    pdf_bytes = img_doc.convert_to_pdf()
                with fitz.open("pdf", pdf_bytes) as wrapped, open(pdf_src, "wb") as fh:
                    fh.write(wrapped.tobytes())

            result     = await asyncio.to_thread(extract_document, pdf_src, model)
            full_text  = result["text"]
            md_text    = result.get("markdown")
            tables     = result.get("tables") or []
            if pdf_src != src:
                os.unlink(pdf_src)
            await update_job(job_id, progress=90)

        elif ext == "pdf":
            doc         = fitz.open(src)
            total_pages = len(doc)

            for i, page in enumerate(doc):
                # ── Table detection ─────────────────────────────
                tab_result = page.find_tables()
                if tab_result and tab_result.tables:
                    tables.extend(t.extract() for t in tab_result.tables)

                # ── Rasterise page → run OCR ────────────────────
                tmp_img = UPLOAD_DIR / f"{job_id}_page_{i}.png"
                page.get_pixmap(dpi=DEFAULT_OCR_DPI).save(str(tmp_img))
                # Heavy engines block for minutes on CPU — never run them
                # on the event loop or every other request starves.
                page_text = await asyncio.to_thread(extract_text, str(tmp_img), model)
                full_text += page_text + "\n\n"
                tmp_img.unlink(missing_ok=True)

                progress = 10 + int((i + 1) / total_pages * 80)
                await update_job(job_id, progress=progress)

            doc.close()
        else:
            # Direct image OCR
            full_text = await asyncio.to_thread(extract_text, src, model)
            await update_job(job_id, progress=90)

        # ── Write outputs ───────────────────────────────────────
        outputs: list = []

        # Plain text
        p = res / "out_txt.txt"
        p.write_text(full_text, encoding="utf-8")
        outputs.append({"output_id": "out_txt", "format": "txt", "size_bytes": _size(p)})

        # Markdown (engines like docling/marker/qwen provide real markdown)
        p = res / "out_md.md"
        p.write_text(f"# OCR Result\n\n{md_text or full_text}", encoding="utf-8")
        outputs.append({"output_id": "out_md", "format": "md", "size_bytes": _size(p)})

        # Word document
        p = res / "out_docx.docx"
        doc_out = Document()
        doc_out.add_paragraph(full_text)
        doc_out.save(str(p))
        outputs.append({"output_id": "out_docx", "format": "docx", "size_bytes": _size(p)})

        # Excel (tables only) — tables may be DataFrames (docling/marker)
        # or raw row-lists (PyMuPDF detection).
        if tables:
            p = res / "out_xlsx.xlsx"
            with pd.ExcelWriter(str(p)) as writer:
                for idx, tbl in enumerate(tables):
                    df = tbl if isinstance(tbl, pd.DataFrame) else pd.DataFrame(tbl)
                    df.to_excel(
                        writer, sheet_name=f"Table_{idx + 1}",
                        index=False, header=False,
                    )
            outputs.append({
                "output_id": "out_xlsx", "format": "xlsx",
                "label": "Extracted tables", "size_bytes": _size(p),
            })

        preview = full_text[:2000]
        await update_job(job_id, status="complete", progress=100,
                         outputs=outputs, preview_text=preview)
        log.info("OCR job %s complete (%d output(s))", job_id, len(outputs))

    except Exception as exc:
        log.exception("OCR job %s failed: %s", job_id, exc)
        await update_job(job_id, status="error")


# ── PDF → PNG ──────────────────────────────────────────────────────────────────

async def run_pdf_to_png(job_id: str, file_paths: List[str], options: dict) -> None:
    """Rasterise each page of a PDF to an individual PNG file."""
    try:
        await update_job(job_id, status="processing", progress=10)

        src = file_paths[0]
        dpi = int(options.get("dpi", DEFAULT_EXPORT_DPI))
        res = _res_dir(job_id)

        doc     = fitz.open(src)
        outputs = []

        for i, page in enumerate(doc):
            out_id = f"out_page_{i + 1}"
            p      = res / f"{out_id}.png"
            page.get_pixmap(dpi=dpi).save(str(p))
            outputs.append({
                "output_id": out_id, "format": "png",
                "label": f"Page {i + 1}", "size_bytes": _size(p),
            })

        doc.close()
        await update_job(job_id, status="complete", progress=100, outputs=outputs)
        log.info("PDF→PNG job %s complete (%d page(s))", job_id, len(outputs))

    except Exception as exc:
        log.exception("PDF→PNG job %s failed: %s", job_id, exc)
        await update_job(job_id, status="error")


# ── PNG → PDF ──────────────────────────────────────────────────────────────────

async def run_png_to_pdf(job_id: str, file_paths: List[str], options: dict) -> None:
    """Assemble one or more image files into a single PDF document."""
    try:
        await update_job(job_id, status="processing", progress=10)

        order   = options.get("page_order", list(range(len(file_paths))))
        ordered = [file_paths[i] for i in order]
        res     = _res_dir(job_id)

        doc = fitz.open()
        for path in ordered:
            img_doc  = fitz.open(path)
            pdf_data = img_doc.convert_to_pdf()
            img_pdf  = fitz.open("pdf", pdf_data)
            doc.insert_pdf(img_pdf)
            img_doc.close()
            img_pdf.close()

        out_id = "out_assembled"
        p      = res / f"{out_id}.pdf"
        doc.save(str(p))
        doc.close()

        outputs = [{"output_id": out_id, "format": "pdf",
                    "label": "assembled_document.pdf", "size_bytes": _size(p)}]
        await update_job(job_id, status="complete", progress=100, outputs=outputs)
        log.info("PNG→PDF job %s complete", job_id)

    except Exception as exc:
        log.exception("PNG→PDF job %s failed: %s", job_id, exc)
        await update_job(job_id, status="error")


# ── Split PDF ──────────────────────────────────────────────────────────────────

async def run_split_pdf(job_id: str, file_paths: List[str], options: dict) -> None:
    """
    Extract page ranges from a PDF into separate files.

    `options["ranges"]` should be a list of [start, end] pairs (1-based).
    Example: [[1, 3], [5, 8]]
    """
    try:
        await update_job(job_id, status="processing", progress=10)

        src    = file_paths[0]
        ranges = options.get("ranges", [])
        res    = _res_dir(job_id)

        doc     = fitz.open(src)
        outputs = []

        for idx, (start, end) in enumerate(ranges):
            out_id  = f"out_range_{idx}"
            p       = res / f"{out_id}.pdf"
            segment = fitz.open()
            segment.insert_pdf(doc, from_page=start - 1, to_page=end - 1)
            segment.save(str(p))
            segment.close()
            outputs.append({
                "output_id": out_id, "format": "pdf",
                "label": f"pages_{start}–{end}.pdf", "size_bytes": _size(p),
            })

        doc.close()
        await update_job(job_id, status="complete", progress=100, outputs=outputs)
        log.info("Split PDF job %s complete (%d segment(s))", job_id, len(outputs))

    except Exception as exc:
        log.exception("Split PDF job %s failed: %s", job_id, exc)
        await update_job(job_id, status="error")


# ── Merge PDF ──────────────────────────────────────────────────────────────────

async def run_merge_pdf(job_id: str, file_paths: List[str], options: dict) -> None:
    """Merge multiple PDF files into a single document."""
    try:
        await update_job(job_id, status="processing", progress=10)

        order   = options.get("file_order", list(range(len(file_paths))))
        ordered = [file_paths[i] for i in order]
        res     = _res_dir(job_id)

        merged = fitz.open()
        for path in ordered:
            src_doc = fitz.open(path)
            merged.insert_pdf(src_doc)
            src_doc.close()

        out_id = "out_merged"
        p      = res / f"{out_id}.pdf"
        merged.save(str(p))
        merged.close()

        outputs = [{"output_id": out_id, "format": "pdf",
                    "label": "merged_document.pdf", "size_bytes": _size(p)}]
        await update_job(job_id, status="complete", progress=100, outputs=outputs)
        log.info("Merge PDF job %s complete", job_id)

    except Exception as exc:
        log.exception("Merge PDF job %s failed: %s", job_id, exc)
        await update_job(job_id, status="error")


# ── Dispatcher ─────────────────────────────────────────────────────────────────
# Maps action_type strings to async runner functions.

JOB_RUNNERS = {
    "ocr":        run_ocr,
    "pdf_to_png": run_pdf_to_png,
    "png_to_pdf": run_png_to_pdf,
    "split_pdf":  run_split_pdf,
    "merge_pdf":  run_merge_pdf,
}
