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
from datetime import datetime, timezone
from typing import List

from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Request, UploadFile,
)
from fastapi.responses import FileResponse

from app.api.deps import get_current_user, get_owned_job
from app.core.config import MAX_FILES_PER_JOB, MEDIA_TYPES, ActionType
from app.core.database import (
    Job,
    User,
    count_jobs,
    delete_job,
    insert_job,
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
from app.services.job_runners import JOB_RUNNERS
from app.services.options import validate_options

log = logging.getLogger(__name__)
router = APIRouter()

# Which uploaded content kinds each action accepts.
_ACCEPTS = {
    ActionType.OCR:        {"pdf", "image"},
    ActionType.PDF_TO_PNG: {"pdf"},
    ActionType.SPLIT_PDF:  {"pdf"},
    ActionType.MERGE_PDF:  {"pdf"},
    ActionType.PNG_TO_PDF: {"image"},
}
_SINGLE_FILE_ACTIONS = {ActionType.OCR, ActionType.PDF_TO_PNG, ActionType.SPLIT_PDF}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_json_list(raw: str | None) -> list:
    try:
        data = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return data if isinstance(data, list) else []


def _bad_request(error: str, message: str, **extra) -> HTTPException:
    return HTTPException(status_code=400, detail={"error": error, "message": message, **extra})


def _check_file_count(action_type: str, n: int) -> None:
    if n > MAX_FILES_PER_JOB:
        raise _bad_request("too_many_files", f"At most {MAX_FILES_PER_JOB} files per job; received {n}.")
    if action_type in _SINGLE_FILE_ACTIONS and n != 1:
        raise _bad_request("invalid_file_count",
                           f"'{action_type}' requires exactly 1 file; received {n}.")
    if action_type == ActionType.MERGE_PDF and n < 2:
        raise _bad_request("invalid_file_count",
                           f"'merge_pdf' requires at least 2 files; received {n}.")
    if action_type == ActionType.PNG_TO_PDF and n < 1:
        raise _bad_request("invalid_file_count", "'png_to_pdf' requires at least 1 file.")


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
    if action_type not in ActionType.ALL:
        raise _bad_request("unsupported_action",
                           f"'{action_type}' is not a supported action.",
                           supported=sorted(ActionType.ALL))

    n = len(files)
    _check_file_count(action_type, n)
    opts = validate_options(action_type, options, n)

    job_id     = f"job_{uuid.uuid4().hex[:16]}"
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # ── Persist uploaded files (all-or-nothing) ─────────────────────────────
    stored: list[storage.StoredUpload] = []
    try:
        for upload in files:
            item = await storage.save_upload(upload, job_id)
            stored.append(item)
            if item.kind not in _ACCEPTS[action_type]:
                raise _bad_request(
                    "invalid_file_type",
                    f"'{item.filename}' is not accepted by '{action_type}' "
                    f"(expected: {', '.join(sorted(_ACCEPTS[action_type]))}).")
    except BaseException:
        for item in stored:
            item.path.unlink(missing_ok=True)
        raise

    source_files = [
        {"filename": s.filename, "path": str(s.path), "size_bytes": s.size_bytes}
        for s in stored
    ]

    await insert_job(job_id, user.id, action_type, created_at,
                     stored[0].filename, json.dumps(opts), json.dumps(source_files))

    background_tasks.add_task(JOB_RUNNERS[action_type], job_id, [str(s.path) for s in stored], opts)
    log.info("Job %s created  user=%s  action=%s  files=%d", job_id, user.id, action_type, n)

    return CreateJobResponse(
        job_id=job_id, status="pending", action_type=action_type,
        file_count=n, submitted_at=created_at,
    )


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
