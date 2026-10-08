"""
main.py
────────
Application entry point.
 
Run with:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""
 
import logging
from pathlib import Path
 
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
 
from app.api.auth_routes import router as auth_router
from app.api.routes import router
from app.core.config import API_DESCRIPTION, API_TITLE, API_VERSION
from app.core.database import init_db
 
# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
 
# ── Application factory ────────────────────────────────────────────────────────
app = FastAPI(
    title=API_TITLE,
    version=API_VERSION,
    description=API_DESCRIPTION,
)
 
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
 
# ── API routes ─────────────────────────────────────────────────────────────────
app.include_router(auth_router, prefix="/api")
app.include_router(router, prefix="/api")
 
 
# ── Lifecycle ──────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def on_startup() -> None:
    await init_db()
 
 
# ── Frontend ───────────────────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
async def serve_frontend():
    # Sert index.html si un frontend est présent ; sinon renvoie un statut JSON
    # basique pour ne pas planter tant qu'aucun frontend n'est ajouté.
    index_path = Path("index.html")
    if index_path.exists():
        # no-cache: always revalidate so UI updates land without stale-page bugs
        return FileResponse(
            str(index_path),
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )
    return JSONResponse({"status": "ok", "service": API_TITLE, "version": API_VERSION})
 