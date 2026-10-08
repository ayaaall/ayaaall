"""
app/core/config.py
──────────────────
Central place for all application-wide settings.
Change values here rather than hunting through source files.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).resolve().parent.parent.parent
UPLOAD_DIR  = BASE_DIR / "uploads"
RESULTS_DIR = BASE_DIR / "results"

# ── Database ───────────────────────────────────────────────────────────────────
# SQLAlchemy async URL. Defaults to local SQLite so the app runs without Docker;
# docker-compose overrides it with Postgres (postgresql+asyncpg://...).
DB_PATH     = BASE_DIR / "history.db"          # kept for reference / migrations
DATABASE_URL = os.getenv(
    "DATABASE_URL", f"sqlite+aiosqlite:///{DB_PATH}"
)

# ── Auth / JWT ─────────────────────────────────────────────────────────────────
JWT_SECRET        = os.getenv("JWT_SECRET", "dev-secret-change-me-in-.env")
JWT_ALGORITHM     = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
REFRESH_TOKEN_EXPIRE_DAYS   = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "30"))

# ── Optional Ollama backend ────────────────────────────────────────────────────
# Leave OLLAMA_HOST empty to disable the ollama engine entirely (default).
# When set (e.g. http://localhost:11434), a "qwen_ollama" engine appears that
# calls a local Ollama server instead of running transformers in-process.
OLLAMA_HOST     = os.getenv("OLLAMA_HOST", "").rstrip("/")
OLLAMA_VL_MODEL = os.getenv("OLLAMA_VL_MODEL", "qwen3.5:2b")
# Thinking mode off (Qwen3.5 "thinks" by default and burns the output budget
# on reasoning instead of transcribing); generous limits for slow CPU boxes.
OLLAMA_THINK        = os.getenv("OLLAMA_THINK", "0") == "1"
OLLAMA_NUM_PREDICT  = int(os.getenv("OLLAMA_NUM_PREDICT", "4096"))
OLLAMA_TIMEOUT_SEC  = int(os.getenv("OLLAMA_TIMEOUT_SEC", "3600"))

# Ensure storage directories exist at import time
UPLOAD_DIR.mkdir(exist_ok=True)
RESULTS_DIR.mkdir(exist_ok=True)

# ── API settings ───────────────────────────────────────────────────────────────
API_TITLE       = "PDF-Tools OCR API"
API_VERSION     = "1.0.0"
API_DESCRIPTION = "REST API for PDF manipulation and OCR text extraction."

# ── Job settings ───────────────────────────────────────────────────────────────
DEFAULT_OCR_DPI   = 150   # DPI used when rasterising PDF pages for OCR
DEFAULT_EXPORT_DPI = 200  # DPI used for PDF → PNG export

# ── Supported action types ─────────────────────────────────────────────────────
class ActionType:
    OCR        = "ocr"
    PDF_TO_PNG = "pdf_to_png"
    PNG_TO_PDF = "png_to_pdf"
    SPLIT_PDF  = "split_pdf"
    MERGE_PDF  = "merge_pdf"

    ALL = {OCR, PDF_TO_PNG, PNG_TO_PDF, SPLIT_PDF, MERGE_PDF}

# ── MIME type registry ─────────────────────────────────────────────────────────
MEDIA_TYPES: dict[str, str] = {
    "txt":  "text/plain",
    "md":   "text/markdown",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf":  "application/pdf",
    "png":  "image/png",
}
