"""
app/services/ocr_engine.py
───────────────────────────
Lazy-loading OCR engine registry.

Each model is loaded **once** on first use and then cached as a
function-level attribute.  This avoids loading heavy PyTorch /
TensorFlow weights at startup and keeps memory usage minimal when
only a subset of models are actually used.

Two engine families
────────────────────
- ``"image"`` engines receive a *page image path* (PDF pages are
  rasterised upstream) and return plain text.
- ``"pdf"`` engines (Docling, Marker) parse the whole document natively
  and return text **plus** markdown **plus** real table DataFrames —
  much better layout/table quality than the image pipeline.

Supported model keys
────────────────────
  rapidocr   — RapidOCR (ONNX, CPU-optimised, default)          [image]
  easyocr    — EasyOCR  (PyTorch, multi-language)               [image]
  surya      — Surya OCR (PyTorch, high-accuracy documents)     [image]
  doctr      — DocTR    (PyTorch, Mindee document AI)           [image]
  keras      — Keras-OCR (TF/Keras, robust to distortion)       [image]
  docling    — Docling (IBM, layout + TableFormer)              [pdf]
  marker     — Marker-PDF (PDF → high-quality Markdown)         [pdf]
  qwen35_vl  — Qwen3.5-VL 2B (VLM OCR, tables via <table> tags) [image]
"""

from __future__ import annotations

import logging
import os
import re

log = logging.getLogger(__name__)


def engine_input(model: str) -> str:
    """Return the input family of an engine: ``"pdf"`` or ``"image"``."""
    return available_engines().get(model, _ENGINES["rapidocr"])["input"]


def extract_text(img_path: str, model: str = "rapidocr") -> str:
    """
    Run the selected **image-based** OCR model on *img_path*.

    Parameters
    ----------
    img_path : str  — Absolute or relative path to the image file.
    model    : str  — One of the image-engine keys listed above.

    Returns
    -------
    str  — Extracted text, lines separated by newlines.
    """
    engine = available_engines().get(model, _ENGINES["rapidocr"])
    log.debug("OCR model=%s, file=%s", model, img_path)
    return engine["runner"](img_path)


def extract_document(pdf_path: str, model: str) -> dict:
    """
    Run a **document-native** engine (docling | marker) on a PDF.

    Returns
    -------
    dict with keys:
        text     : str                    — plain text (whole document)
        markdown : str | None             — markdown rendering, if available
        tables   : list[pandas.DataFrame] — detected tables
    """
    engine = available_engines().get(model)
    if engine is None or engine["input"] != "pdf":
        raise ValueError(f"'{model}' is not a PDF-native engine.")
    log.debug("Document OCR model=%s, file=%s", model, pdf_path)
    return engine["runner"](pdf_path)


# ── Table parsing helpers ──────────────────────────────────────────────────────

def _markdown_tables(md_text: str) -> list[str]:
    """Pull GitHub-flavoured markdown table blocks out of *md_text*."""
    lines = md_text.splitlines()
    tables: list[str] = []
    i = 0

    def is_row(s: str) -> bool:
        s = s.strip()
        return s.startswith("|") and s.endswith("|")

    def is_separator(s: str) -> bool:
        return bool(re.match(r"^\|?[\s:\-\|]+\|?$", s.strip())) and "-" in s

    while i < len(lines):
        if is_row(lines[i]) and i + 1 < len(lines) and is_separator(lines[i + 1]):
            block = [lines[i], lines[i + 1]]
            i += 2
            while i < len(lines) and is_row(lines[i]):
                block.append(lines[i])
                i += 1
            tables.append("\n".join(block))
        else:
            i += 1
    return tables


def _md_table_to_frame(block: str):
    """Convert one markdown table block into a pandas DataFrame (best effort)."""
    import pandas as pd

    rows = [
        [c.strip() for c in ln.strip().strip("|").split("|")]
        for ln in block.splitlines()
        if not re.match(r"^\|?[\s:\-\|]+\|?$", ln.strip())  # skip separator
    ]
    if not rows:
        return pd.DataFrame()
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    return pd.DataFrame(rows)


# ── Image-based model runners ─────────────────────────────────────────────────

def _run_rapidocr(img_path: str) -> str:
    from rapidocr_onnxruntime import RapidOCR
    if not hasattr(_run_rapidocr, "_engine"):
        log.info("Loading RapidOCR engine …")
        _run_rapidocr._engine = RapidOCR()
    result, _ = _run_rapidocr._engine(img_path)
    return "\n".join(line[1] for line in result) if result else ""


def _run_easyocr(img_path: str) -> str:
    import easyocr
    if not hasattr(_run_easyocr, "_reader"):
        log.info("Loading EasyOCR reader (en + fr) …")
        _run_easyocr._reader = easyocr.Reader(["en", "fr"])
    result = _run_easyocr._reader.readtext(img_path)
    return "\n".join(item[1] for item in result)


def _run_surya(img_path: str) -> str:
    from PIL import Image
    from surya.foundation import FoundationPredictor
    from surya.recognition import RecognitionPredictor
    from surya.detection import DetectionPredictor

    if not hasattr(_run_surya, "_models"):
        log.info("Loading Surya OCR models …")
        foundation = FoundationPredictor()
        _run_surya._models = (
            DetectionPredictor(),
            RecognitionPredictor(foundation_predictor=foundation),
        )
    det, rec = _run_surya._models
    image = Image.open(img_path).convert("RGB")
    preds = rec([image], [["fr", "en"]], det_predictor=det)
    lines = getattr(preds[0], "text_lines", [])
    return "\n".join(line.text for line in lines)


def _run_doctr(img_path: str) -> str:
    from doctr.io import DocumentFile
    from doctr.models import ocr_predictor

    if not hasattr(_run_doctr, "_model"):
        log.info("Loading DocTR predictor …")
        _run_doctr._model = ocr_predictor(pretrained=True)
    doc = DocumentFile.from_images(img_path)
    result = _run_doctr._model(doc)
    lines = []
    for page in result.pages:
        for block in page.blocks:
            for line in block.lines:
                lines.append(" ".join(word.value for word in line.words))
    return "\n".join(lines)


def _run_keras(img_path: str) -> str:
    import keras_ocr
    if not hasattr(_run_keras, "_pipeline"):
        log.info("Loading Keras-OCR pipeline …")
        _run_keras._pipeline = keras_ocr.pipeline.Pipeline()
    images = [keras_ocr.tools.read(img_path)]
    predictions = _run_keras._pipeline.recognize(images)
    return "\n".join(word[0] for word in predictions[0])


def _run_qwen35_vl(img_path: str) -> str:
    """Qwen3.5-VL-2B vision-language OCR (ported from the Qwen3-VL notebook).

    The model ID is configurable via the ``QWEN_VL_MODEL`` env var; the
    vision-token pixel budget via ``QWEN_VL_MAX_PIXELS`` keeps memory in
    check on CPU/small-GPU machines.
    """
    import torch
    from PIL import Image
    from transformers import AutoModelForImageTextToText, AutoProcessor

    if not hasattr(_run_qwen35_vl, "_model"):
        model_id = os.getenv("QWEN_VL_MODEL", "Qwen/Qwen3.5-2B")
        log.info("Loading Qwen VLM %s (first run downloads the weights)…", model_id)
        processor = AutoProcessor.from_pretrained(model_id)
        max_pixels = int(os.getenv("QWEN_VL_MAX_PIXELS", str(768 * 32 * 32)))
        min_pixels = 256 * 32 * 32
        try:
            processor.image_processor.size = {
                "longest_edge": max_pixels, "shortest_edge": min_pixels,
            }
        except Exception:
            pass  # not all processors expose size override
        model = AutoModelForImageTextToText.from_pretrained(
            model_id, dtype="auto", device_map="auto",
        )
        _run_qwen35_vl._model = (processor, model)
        log.info("Qwen VLM ready on %s", model.device)

    processor, model = _run_qwen35_vl._model

    prompt = (
        "Transcribe all the text on this page exactly as it appears, preserving "
        "reading order and paragraph breaks. If the page contains one or more "
        "tables, reproduce each one as a GitHub-flavored markdown table, wrapped "
        "between <table> and </table> tags. Do not add commentary, explanations, "
        "or anything that is not on the page."
    )
    image = Image.open(img_path).convert("RGB")
    messages = [{
        "role": "user",
        "content": [
            {"type": "image", "image": image},
            {"type": "text", "text": prompt},
        ],
    }]
    inputs = processor.apply_chat_template(
        messages, tokenize=True, add_generation_prompt=True,
        return_dict=True, return_tensors="pt",
    ).to(model.device)

    input_len = inputs["input_ids"].shape[1]
    with torch.inference_mode():
        generated = model.generate(
            **inputs, max_new_tokens=2048, temperature=0.0, do_sample=False,
        )
    # Decode only the newly generated tokens, not the echoed prompt.
    return processor.decode(generated[0][input_len:], skip_special_tokens=True)


_OCR_PROMPT = (
    "Transcribe all the text on this page exactly as it appears, preserving "
    "reading order and paragraph breaks. If the page contains one or more "
    "tables, reproduce each one as a GitHub-flavored markdown table, wrapped "
    "between <table> and </table> tags. Do not add commentary, explanations, "
    "or anything that is not on the page."
)


def _run_qwen_ollama(img_path: str) -> str:
    """Qwen VLM OCR via a local Ollama server (optional alternative).

    Enabled only when OLLAMA_HOST is configured. The image is sent to the
    local Ollama HTTP API (`POST {OLLAMA_HOST}/api/chat`); nothing leaves
    the machine. Model pulled beforehand with:
        ollama pull $OLLAMA_VL_MODEL
    """
    import base64

    import httpx

    from app.core.config import OLLAMA_HOST, OLLAMA_VL_MODEL

    b64 = base64.b64encode(open(img_path, "rb").read()).decode("ascii")

    from app.core.config import OLLAMA_HOST, OLLAMA_NUM_PREDICT, OLLAMA_THINK, OLLAMA_TIMEOUT_SEC

    payload = {
        "model": OLLAMA_VL_MODEL,
        "messages": [{
            "role": "user",
            "content": _OCR_PROMPT,
            "images": [b64],
        }],
        "stream": False,
        # Qwen3.5 emits <think> reasoning by default — disable it so the
        # whole output budget goes to the transcription itself.
        "think": OLLAMA_THINK,
        "options": {
            "temperature": 0.0,
            "num_predict": OLLAMA_NUM_PREDICT,
        },
    }
    # CPU VLM inference is slow: allow a long read timeout.
    timeout = httpx.Timeout(OLLAMA_TIMEOUT_SEC, connect=15.0)
    resp = httpx.post(f"{OLLAMA_HOST}/api/chat", json=payload, timeout=timeout)
    resp.raise_for_status()
    text = resp.json()["message"]["content"]
    # Belt & braces: strip any residual <think> block if present.
    text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()
    # Unwrap a single markdown code fence if the model wrapped the page in one.
    fenced = re.fullmatch(r"```[a-zA-Z]*\n(.*)\n```\s*", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    return text


# ── PDF-native model runners ───────────────────────────────────────────────────

def _run_docling(pdf_path: str) -> dict:
    """Docling (IBM): layout parsing + TableFormer table extraction."""
    from docling.document_converter import DocumentConverter

    if not hasattr(_run_docling, "_converter"):
        log.info("Loading Docling converter (first run downloads ~1 GB of models)…")
        _run_docling._converter = DocumentConverter()
    doc = _run_docling._converter.convert(pdf_path).document
    tables = [t.export_to_dataframe(doc=doc) for t in doc.tables]
    return {
        "text": doc.export_to_text(),
        "markdown": doc.export_to_markdown(),
        "tables": tables,
    }


def _run_marker(pdf_path: str) -> dict:
    """Marker-PDF: whole-PDF → markdown pipeline (tables come out as md)."""
    from marker.converters.pdf import PdfConverter
    from marker.models import create_model_dict
    from marker.output import text_from_rendered

    if not hasattr(_run_marker, "_converter"):
        log.info("Loading Marker-PDF models …")
        _run_marker._converter = PdfConverter(artifact_dict=create_model_dict())
    rendered = _run_marker._converter(pdf_path)
    markdown, _, _ = text_from_rendered(rendered)
    tables = [_md_table_to_frame(b) for b in _markdown_tables(markdown)]
    return {"text": markdown, "markdown": markdown, "tables": tables}


# ── Registry ───────────────────────────────────────────────────────────────────
# Map user-facing keys to runner functions + input family.

_ENGINES = {
    "rapidocr":  {"runner": _run_rapidocr,  "input": "image"},
    "easyocr":   {"runner": _run_easyocr,   "input": "image"},
    "surya":     {"runner": _run_surya,     "input": "image"},
    "doctr":     {"runner": _run_doctr,     "input": "image"},
    "keras":     {"runner": _run_keras,     "input": "image"},
    "qwen35_vl": {"runner": _run_qwen35_vl, "input": "image"},
    "docling":   {"runner": _run_docling,   "input": "pdf"},
    "marker":    {"runner": _run_marker,    "input": "pdf"},
}


def available_engines() -> dict:
    """Engines exposed to clients. The ollama engine only appears when an
    Ollama server is configured via OLLAMA_HOST."""
    engines = dict(_ENGINES)
    from app.core.config import OLLAMA_HOST
    if OLLAMA_HOST:
        engines["qwen_ollama"] = {"runner": _run_qwen_ollama, "input": "image"}
    return engines


# Backwards-compat alias kept for any code importing _REGISTRY.
_REGISTRY = {k: v["runner"] for k, v in available_engines().items()}
