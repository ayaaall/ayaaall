"""
app/services/jobs.py
────────────────────
Job submission shared by every entry point (UI routes, public v1 API):
validate the request, store the uploads, create the DB record and hand back
what is needed to launch the runner.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List

from fastapi import HTTPException, UploadFile

from app.core.config import MAX_FILES_PER_JOB, ActionType
from app.core.database import User, insert_job
from app.schemas.jobs import CreateJobResponse
from app.services import storage
from app.services.job_runners import JOB_RUNNERS
from app.services.options import validate_options

log = logging.getLogger(__name__)

# Which uploaded content kinds each action accepts.
_ACCEPTS = {
    ActionType.OCR:        {"pdf", "image"},
    ActionType.PDF_TO_PNG: {"pdf"},
    ActionType.SPLIT_PDF:  {"pdf"},
    ActionType.MERGE_PDF:  {"pdf"},
    ActionType.PNG_TO_PDF: {"image"},
}
_SINGLE_FILE_ACTIONS = {ActionType.OCR, ActionType.PDF_TO_PNG, ActionType.SPLIT_PDF}

# Strong references so fire-and-forget tasks are not garbage-collected mid-run.
_running: set[asyncio.Task] = set()


@dataclass(frozen=True)
class PreparedJob:
    job_id: str
    runner: Callable
    paths: list[str]
    options: dict
    response: CreateJobResponse


def load_json_list(raw: str | None) -> list:
    try:
        data = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return data if isinstance(data, list) else []


def bad_request(error: str, message: str, **extra) -> HTTPException:
    return HTTPException(status_code=400, detail={"error": error, "message": message, **extra})


def _check_file_count(action_type: str, n: int) -> None:
    if n > MAX_FILES_PER_JOB:
        raise bad_request("too_many_files", f"At most {MAX_FILES_PER_JOB} files per job; received {n}.")
    if action_type in _SINGLE_FILE_ACTIONS and n != 1:
        raise bad_request("invalid_file_count",
                          f"'{action_type}' requires exactly 1 file; received {n}.")
    if action_type == ActionType.MERGE_PDF and n < 2:
        raise bad_request("invalid_file_count",
                          f"'merge_pdf' requires at least 2 files; received {n}.")
    if action_type == ActionType.PNG_TO_PDF and n < 1:
        raise bad_request("invalid_file_count", "'png_to_pdf' requires at least 1 file.")


async def prepare_job(files: List[UploadFile], action_type: str, options: str, user: User) -> PreparedJob:
    """Validate and persist a job request. Does NOT start the runner."""
    if action_type not in ActionType.ALL:
        raise bad_request("unsupported_action", f"'{action_type}' is not a supported action.",
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
                raise bad_request(
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
    log.info("Job %s created  user=%s  action=%s  files=%d", job_id, user.id, action_type, n)

    return PreparedJob(
        job_id=job_id,
        runner=JOB_RUNNERS[action_type],
        paths=[str(s.path) for s in stored],
        options=opts,
        response=CreateJobResponse(job_id=job_id, status="pending", action_type=action_type,
                                   file_count=n, submitted_at=created_at),
    )


def start_now(job: PreparedJob) -> None:
    """Launch the runner immediately (used when the caller will wait for it)."""
    task = asyncio.create_task(job.runner(job.job_id, job.paths, job.options))
    _running.add(task)
    task.add_done_callback(_running.discard)
