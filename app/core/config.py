"""
app/core/config.py
──────────────────
Central place for all application-wide settings.
Change values here rather than hunting through source files.
"""

import logging
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_log = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str) -> list[str]:
    return [v.strip() for v in os.getenv(name, "").split(",") if v.strip()]


# ── Environment ────────────────────────────────────────────────────────────────
# APP_ENV=production enforces strict startup checks (see below).
APP_ENV       = os.getenv("APP_ENV", "development").lower()
IS_PRODUCTION = APP_ENV == "production"

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).resolve().parent.parent.parent
UPLOAD_DIR  = BASE_DIR / "uploads"
RESULTS_DIR = BASE_DIR / "results"

# ── Database ───────────────────────────────────────────────────────────────────
# SQLAlchemy async URL. Defaults to local SQLite so the app runs without Docker;
# docker-compose overrides it with Postgres (postgresql+asyncpg://...).
DB_PATH      = BASE_DIR / "history.db"
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite+aiosqlite:///{DB_PATH}")

# ── Auth / JWT ─────────────────────────────────────────────────────────────────
JWT_ALGORITHM               = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30"))
REFRESH_TOKEN_EXPIRE_DAYS   = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "30"))
ALLOW_REGISTRATION          = _env_bool("ALLOW_REGISTRATION", True)
PASSWORD_MIN_LENGTH         = 10

# Login throttling (per client IP + identifiant)
LOGIN_MAX_ATTEMPTS   = int(os.getenv("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_WINDOW_SECONDS = int(os.getenv("LOGIN_WINDOW_SECONDS", "300"))

_KNOWN_WEAK_SECRETS = {"", "dev-secret-change-me-in-.env", "change-me-in-production", "change-me"}


def _load_jwt_secret() -> str:
    secret = os.getenv("JWT_SECRET", "")
    if secret in _KNOWN_WEAK_SECRETS or len(secret) < 32:
        if IS_PRODUCTION:
            raise RuntimeError(
                "JWT_SECRET must be set to a random value of at least 32 characters "
                "(generate one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\")."
            )
        _log.warning(
            "JWT_SECRET missing or weak — using a random per-process secret. "
            "Sessions will not survive a restart. Set JWT_SECRET in .env."
        )
        return secrets.token_urlsafe(48)
    return secret


JWT_SECRET = _load_jwt_secret()

# ── HTTP / CORS ────────────────────────────────────────────────────────────────
# The UI is served by this same app, so no cross-origin access is needed by
# default. List extra origins explicitly: CORS_ORIGINS=https://a.example,https://b.example
CORS_ORIGINS = _env_list("CORS_ORIGINS")
ENABLE_DOCS  = _env_bool("ENABLE_DOCS", not IS_PRODUCTION)

# ── Upload limits ──────────────────────────────────────────────────────────────
MAX_UPLOAD_MB      = int(os.getenv("MAX_UPLOAD_MB", "50"))      # per file
MAX_UPLOAD_BYTES   = MAX_UPLOAD_MB * 1024 * 1024
MAX_FILES_PER_JOB  = int(os.getenv("MAX_FILES_PER_JOB", "50"))
MAX_PDF_PAGES      = int(os.getenv("MAX_PDF_PAGES", "300"))
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "2"))  # heavy jobs at once

ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp"}

# ── Optional Ollama backend ────────────────────────────────────────────────────
# Leave OLLAMA_HOST empty to disable the ollama engine entirely (default).
# When set (e.g. http://localhost:11434), a "qwen_ollama" engine appears that
# calls a local Ollama server instead of running transformers in-process.
OLLAMA_HOST     = os.getenv("OLLAMA_HOST", "").rstrip("/")
OLLAMA_VL_MODEL = os.getenv("OLLAMA_VL_MODEL", "qwen3.5:2b")
# Thinking mode off (Qwen3.5 "thinks" by default and burns the output budget
# on reasoning instead of transcribing); generous limits for slow CPU boxes.
OLLAMA_THINK        = _env_bool("OLLAMA_THINK", False)
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
DEFAULT_OCR_DPI    = 150   # DPI used when rasterising PDF pages for OCR
DEFAULT_EXPORT_DPI = 200   # DPI used for PDF → PNG export
MIN_DPI, MAX_DPI   = 72, 400


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
