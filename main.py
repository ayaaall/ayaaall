"""
main.py
────────
Application entry point.

Run with:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from app.api.auth_routes import router as auth_router
from app.api.routes import router
from app.core.config import (
    API_DESCRIPTION,
    API_TITLE,
    API_VERSION,
    BASE_DIR,
    CORS_ORIGINS,
    ENABLE_DOCS,
    IS_PRODUCTION,
)
from app.core.database import init_db

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

INDEX_PATH = BASE_DIR / "index.html"

# The bundled UI uses inline <script>/<style> and Google Fonts, hence the
# 'unsafe-inline' allowances; tighten them if the JS/CSS are ever externalised.
_UI_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


# ── Lifecycle ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    yield


# ── Application ────────────────────────────────────────────────────────────────
app = FastAPI(
    title=API_TITLE,
    version=API_VERSION,
    description=API_DESCRIPTION,
    lifespan=lifespan,
    docs_url="/docs" if ENABLE_DOCS else None,
    redoc_url="/redoc" if ENABLE_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_DOCS else None,
)

# Same-origin by default; cross-origin access must be opted into via CORS_ORIGINS.
if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for header, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    if IS_PRODUCTION:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


# ── API routes ─────────────────────────────────────────────────────────────────
app.include_router(auth_router, prefix="/api")
app.include_router(router, prefix="/api")


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"status": "ok"}


# ── Frontend ───────────────────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
async def serve_frontend():
    # Serve index.html when a frontend is present; otherwise a basic JSON
    # status so the app does not crash without one.
    if INDEX_PATH.exists():
        return FileResponse(
            INDEX_PATH,
            headers={
                # always revalidate so UI updates land without stale-page bugs
                "Cache-Control": "no-cache, must-revalidate",
                "Content-Security-Policy": _UI_CSP,
            },
        )
    return JSONResponse({"status": "ok", "service": API_TITLE, "version": API_VERSION})
