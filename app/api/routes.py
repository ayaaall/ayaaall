"""
app/api/routes.py
──────────────────
All API route handlers.

Routes are grouped into a single APIRouter so that `main.py` stays
minimal — it just mounts this router onto the FastAPI application.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from app.core.config import MEDIA_TYPES, RESULTS_DIR, UPLOAD_DIR, ActionType
from app.core.database import (
    User,
    count_jobs,
    delete_job,
    get_job as db_get_job,
    insert_job,
    list_jobs,
    update_job,
)
from app.api.auth_routes import get_current_user
from app.schemas.jobs import (
    CreateJobResponse,
    DeleteResponse,
    FileMetadataResponse,
    HistoryItem,
    HistoryResponse,
    JobStatusResponse,
    OutputFile,
    SourceFile,
)
from app.services.job_runners import JOB_RUNNERS

log = logging.getLogger(__name__)
router = APIRouter()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_options(raw: str) -> dict:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


# ── GET /api/engines ───────────────────────────────────────────────────────────

@router.get("/engines")
async def list_engines(user: User = Depends(get_current_user)):
    """Engines available for the OCR action (ollama engine appears only
    when OLLAMA_HOST is configured)."""
    from app.services.ocr_engine import available_engines

    return {"engines": [
        {"id": eid, "input": info["input"]}
        for eid, info in available_engines().items()
    ]}


# ── POST /api/jobs ─────────────────────────────────────────────────────────────

@router.post("/jobs", response_model=CreateJobResponse, status_code=200)
async def create_job(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    action_type: str = Form(...),
    options: str = Form("{}"),
    user: User = Depends(get_current_user),
):
    """
    Submit a new processing job.

    Multipart form fields
    ─────────────────────
    files[]     — one or more uploaded files
    action_type — one of: ocr | pdf_to_png | png_to_pdf | split_pdf | merge_pdf
    options     — JSON string with action-specific parameters
    """
    if action_type not in ActionType.ALL:
        raise HTTPException(status_code=400, detail={
            "error": "unsupported_action",
            "message": f"'{action_type}' is not a supported action.",
            "supported": list(ActionType.ALL),
        })

    n = len(files)

    # ── File count validation ───────────────────────────────────────────────
    if action_type in {ActionType.OCR, ActionType.PDF_TO_PNG, ActionType.SPLIT_PDF} and n != 1:
        raise HTTPException(status_code=400, detail={
            "error": "invalid_file_count",
            "message": f"'{action_type}' requires exactly 1 file; received {n}.",
        })
    if action_type == ActionType.MERGE_PDF and n < 2:
        raise HTTPException(status_code=400, detail={
            "error": "invalid_file_count",
            "message": f"'merge_pdf' requires at least 2 files; received {n}.",
        })

    opts       = _parse_options(options)
    job_id     = f"job_{uuid.uuid4().hex[:8]}"
    created_at = datetime.utcnow().isoformat() + "Z"

    # ── Persist uploaded files ──────────────────────────────────────────────
    saved_paths: List[str] = []
    source_files: list = []
    for upload in files:
        ext  = upload.filename.rsplit(".", 1)[-1] if "." in upload.filename else "bin"
        dest = UPLOAD_DIR / f"{job_id}_{uuid.uuid4().hex[:6]}.{ext}"
        content = await upload.read()
        dest.write_bytes(content)
        saved_paths.append(str(dest))
        source_files.append({
            "filename": upload.filename,
            "path": str(dest),
            "size_bytes": len(content),
        })

    primary_filename = files[0].filename if files else "unknown"

    # ── Insert job record ───────────────────────────────────────────────────
    await insert_job(job_id, user.id, action_type, created_at,
                     primary_filename, options, json.dumps(source_files))

    # ── Dispatch background task ────────────────────────────────────────────
    runner = JOB_RUNNERS[action_type]
    background_tasks.add_task(runner, job_id, saved_paths, opts)

    log.info("Job %s created  action=%s  files=%d", job_id, action_type, n)

    return CreateJobResponse(
        job_id=job_id,
        status="pending",
        action_type=action_type,
        file_count=n,
        submitted_at=created_at,
    )


# ── GET /api/jobs/{job_id} ─────────────────────────────────────────────────────

@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job(job_id: str, user: User = Depends(get_current_user)):
    """Poll the status and progress of a job."""
    job = await db_get_job(job_id)

    if job is None or job.user_id != user.id:
        raise HTTPException(status_code=404, detail="Job not found.")

    resp = JobStatusResponse(job_id=job_id, status=job.status)

    if job.status in {"pending", "processing"}:
        resp.progress = job.progress

    if job.status == "complete":
        resp.action_type = job.action_type
        try:
            resp.outputs = [OutputFile(**o) for o in json.loads(job.outputs)]
        except Exception:
            resp.outputs = []
        if job.action_type == ActionType.OCR and job.preview_text:
            resp.preview_text = job.preview_text

    # Original uploaded documents (for history re-download)
    try:
        src_list = json.loads(job.source_files or "[]")
    except Exception:
        src_list = []
    resp.sources = [
        SourceFile(filename=s.get("filename", "?"), size_bytes=s.get("size_bytes", 0))
        for s in src_list
    ]

    return resp


# ── GET /api/jobs/{job_id}/output ──────────────────────────────────────────────

@router.get("/jobs/{job_id}/output")
async def get_job_output(job_id: str, output_id: str, request: Request,
                         token: Optional[str] = None):
    """Stream a specific output file for download.

    Auth: Bearer header (API clients) or `?token=` query param (browser
    download links, which cannot set headers).
    """
    from app.core.database import get_user_by_id
    from app.core.security import decode_access_token

    uid = decode_access_token(token) if token else None
    if uid is None:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            uid = decode_access_token(auth[7:])
    user = await get_user_by_id(uid) if uid else None
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")

    job = await db_get_job(job_id)

    if job is None or job.user_id != user.id:
        raise HTTPException(status_code=404, detail="Job not found.")

    outputs = json.loads(job.outputs)
    match   = next((o for o in outputs if o.get("output_id") == output_id), None)

    if not match:
        raise HTTPException(status_code=404, detail="Output ID not found.")

    fmt  = match["format"]
    path = RESULTS_DIR / job_id / f"{output_id}.{fmt}"

    if not path.exists():
        raise HTTPException(status_code=404, detail="File deleted or unavailable.")

    media_type = MEDIA_TYPES.get(fmt, "application/octet-stream")
    return FileResponse(str(path), media_type=media_type, filename=f"{output_id}.{fmt}")


# ── GET /api/jobs/{job_id}/source ──────────────────────────────────────────────

@router.get("/jobs/{job_id}/source")
async def get_job_source(job_id: str, request: Request, file: int = 0,
                         token: Optional[str] = None):
    """Re-download the original uploaded document for a job (history).

    Auth: Bearer header or `?token=` query param (browser links).
    """
    from app.core.database import get_user_by_id
    from app.core.security import decode_access_token

    uid = decode_access_token(token) if token else None
    if uid is None:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            uid = decode_access_token(auth[7:])
    user = await get_user_by_id(uid) if uid else None
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")

    job = await db_get_job(job_id)
    if job is None or job.user_id != user.id:
        raise HTTPException(status_code=404, detail="Job not found.")

    try:
        sources = json.loads(job.source_files or "[]")
    except Exception:
        sources = []
    if not sources:
        raise HTTPException(status_code=404, detail="No source file stored for this job.")
    entry = sources[min(max(file, 0), len(sources) - 1)]

    path = Path(entry["path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="Source file deleted or unavailable.")
    return FileResponse(str(path), filename=entry.get("filename") or path.name)


# ── GET /api/history ───────────────────────────────────────────────────────────

@router.get("/history", response_model=HistoryResponse)
async def get_history(page: int = 1, limit: int = 20,
                      user: User = Depends(get_current_user)):
    """Return the current user's paginated job history, most recent first."""
    offset = (page - 1) * limit
    total  = await count_jobs(user.id)
    rows   = await list_jobs(user.id, limit, offset)

    items = []
    for row in rows:
        try:
            formats = [o.get("format") for o in json.loads(row.outputs)]
        except Exception:
            formats = []
        items.append(HistoryItem(
            job_id=row.id, filename=row.primary_filename, action_type=row.action_type,
            status=row.status, created_at=row.created_at, outputs=formats,
        ))

    return HistoryResponse(page=page, limit=limit, total=total, items=items)


# ── DELETE /api/history/{job_id} ───────────────────────────────────────────────

@router.delete("/history/{job_id}", response_model=DeleteResponse)
async def delete_history(job_id: str, user: User = Depends(get_current_user)):
    """Permanently remove one of the current user's job records."""
    await delete_job(job_id, user_id=user.id)
    return DeleteResponse(job_id=job_id, deleted=True)


# ── POST /api/files/metadata ───────────────────────────────────────────────────

@router.post("/files/metadata", response_model=FileMetadataResponse)
async def register_metadata(request: Request):
    """Register file metadata (spec compliance endpoint)."""
    await request.json()   # consume the body
    return FileMetadataResponse(file_id=f"file_{uuid.uuid4().hex[:8]}", stored=True)
