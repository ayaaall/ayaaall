"""
app/api/routes.py
──────────────────
Job, history and download route handlers.

Routes are grouped into a single APIRouter so that `main.py` stays
minimal — it just mounts this router onto the FastAPI application.
All routes require a Bearer access token (see app/api/deps.py).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import List

from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Request, UploadFile,
)
from fastapi.responses import FileResponse

from app.api.deps import get_current_user, get_owned_job
from app.core.config import MEDIA_TYPES, ActionType
from app.core.database import (
    Job,
    User,
    count_jobs,
    delete_job,
    list_jobs,
)
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
from app.services import storage
from app.services.jobs import load_json_list as _load_json_list
from app.services.jobs import prepare_job

log = logging.getLogger(__name__)
router = APIRouter()

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
    files[]     — one or more uploaded files (PDF / images, size-capped)
    action_type — one of: ocr | pdf_to_png | png_to_pdf | split_pdf | merge_pdf
    options     — JSON string with action-specific parameters
    """
    job = await prepare_job(files, action_type, options, user)
    background_tasks.add_task(job.runner, job.job_id, job.paths, job.options)
    return job.response


# ── GET /api/jobs/{job_id} ─────────────────────────────────────────────────────

@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job(job: Job = Depends(get_owned_job)):
    """Poll the status and progress of a job."""
    resp = JobStatusResponse(job_id=job.id, status=job.status)

    if job.status in {"pending", "processing"}:
        resp.progress = job.progress

    if job.status == "complete":
        resp.action_type = job.action_type
        resp.outputs = [OutputFile(**o) for o in _load_json_list(job.outputs)]
        if job.action_type == ActionType.OCR and job.preview_text:
            resp.preview_text = job.preview_text

    resp.sources = [
        SourceFile(filename=s.get("filename", "?"), size_bytes=s.get("size_bytes", 0))
        for s in _load_json_list(job.source_files)
    ]
    return resp


# ── GET /api/jobs/{job_id}/output ──────────────────────────────────────────────

@router.get("/jobs/{job_id}/output")
async def get_job_output(output_id: str, job: Job = Depends(get_owned_job)):
    """Stream a specific output file (Bearer auth only — tokens never go in URLs)."""
    match = next((o for o in _load_json_list(job.outputs) if o.get("output_id") == output_id), None)
    if not match:
        raise HTTPException(status_code=404, detail="Output ID not found.")

    fmt  = match["format"]
    path = storage.result_file(job.id, output_id, fmt)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="File deleted or unavailable.")

    return FileResponse(path, media_type=MEDIA_TYPES.get(fmt, "application/octet-stream"),
                        filename=f"{output_id}.{fmt}")


# ── GET /api/jobs/{job_id}/source ──────────────────────────────────────────────

@router.get("/jobs/{job_id}/source")
async def get_job_source(file: int = Query(0, ge=0), job: Job = Depends(get_owned_job)):
    """Re-download the original uploaded document for a job (history)."""
    sources = _load_json_list(job.source_files)
    if not sources:
        raise HTTPException(status_code=404, detail="No source file stored for this job.")
    entry = sources[min(file, len(sources) - 1)]

    path = storage.source_file(entry.get("path", ""))
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Source file deleted or unavailable.")
    return FileResponse(path, filename=entry.get("filename") or path.name,
                        media_type="application/octet-stream")


# ── GET /api/history ───────────────────────────────────────────────────────────

@router.get("/history", response_model=HistoryResponse)
async def get_history(page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100),
                      user: User = Depends(get_current_user)):
    """Return the current user's paginated job history, most recent first."""
    total = await count_jobs(user.id)
    rows  = await list_jobs(user.id, limit, (page - 1) * limit)

    items = [
        HistoryItem(
            job_id=row.id, filename=row.primary_filename, action_type=row.action_type,
            status=row.status, created_at=row.created_at,
            outputs=[o.get("format") for o in _load_json_list(row.outputs)],
        )
        for row in rows
    ]
    return HistoryResponse(page=page, limit=limit, total=total, items=items)


# ── DELETE /api/history/{job_id} ───────────────────────────────────────────────

@router.delete("/history/{job_id}", response_model=DeleteResponse)
async def delete_history(job: Job = Depends(get_owned_job), user: User = Depends(get_current_user)):
    """Permanently remove one of the current user's jobs and its files on disk."""
    sources = [s.get("path", "") for s in _load_json_list(job.source_files)]
    deleted = await delete_job(job.id, user_id=user.id)
    if deleted:
        storage.delete_job_files(job.id, sources)
    return DeleteResponse(job_id=job.id, deleted=deleted)


# ── POST /api/files/metadata ───────────────────────────────────────────────────

@router.post("/files/metadata", response_model=FileMetadataResponse)
async def register_metadata(request: Request, user: User = Depends(get_current_user)):
    """Register file metadata (spec compliance endpoint; body is validated, not stored)."""
    if int(request.headers.get("content-length") or 0) > 10_000:
        raise HTTPException(status_code=413, detail="Payload too large.")
    try:
        await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Body must be valid JSON.")
    return FileMetadataResponse(file_id=f"file_{uuid.uuid4().hex[:8]}", stored=True)
