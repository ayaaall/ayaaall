"""
app/api/v1_routes.py
────────────────────
Integration-friendly endpoint for other projects: upload one PDF/image, get
the text back in a single call.

    curl -H "X-API-Key: $KEY" -F file=@scan.pdf "https://host/api/v1/ocr?wait=30"

With `wait` (seconds, max 120) the response contains the text if the job
finishes in time; otherwise it returns `status: "processing"` and the caller
polls `GET /api/v1/ocr/{job_id}` (or the regular /api/jobs/{id} endpoint).
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from pydantic import BaseModel

from app.api.deps import get_current_user, get_owned_job
from app.core.database import Job, User, get_job
from app.core.config import ActionType
from app.services import storage
from app.services.jobs import load_json_list, prepare_job, start_now

router = APIRouter(prefix="/v1", tags=["v1"])

MAX_WAIT_SECONDS = 120
_POLL_INTERVAL = 0.5


class OcrResult(BaseModel):
    job_id: str
    status: str                       # pending | processing | complete | error
    progress: Optional[int] = None
    text: Optional[str] = None
    markdown: Optional[str] = None
    outputs: list[str] = []           # available formats; fetch via /api/jobs/{id}/output


def _read(job: Job, output_id: str, fmt: str) -> Optional[str]:
    path = storage.result_file(job.id, output_id, fmt)
    return path.read_text(encoding="utf-8") if path and path.is_file() else None


def _to_result(job: Job) -> OcrResult:
    res = OcrResult(job_id=job.id, status=job.status, progress=job.progress)
    if job.status == "complete":
        res.outputs = [o.get("format") for o in load_json_list(job.outputs)]
        res.text = _read(job, "out_txt", "txt")
        res.markdown = _read(job, "out_md", "md")
    return res


@router.post("/ocr", response_model=OcrResult)
async def ocr(
    file: UploadFile = File(..., description="PDF or image"),
    ocr_model: str = Form("rapidocr"),
    wait: int = Query(0, ge=0, le=MAX_WAIT_SECONDS, description="Seconds to wait for the result"),
    user: User = Depends(get_current_user),
):
    """Run OCR on one document and (optionally) wait for the text."""
    import json

    job = await prepare_job([file], ActionType.OCR, json.dumps({"ocr_model": ocr_model}), user)
    start_now(job)

    deadline = time.monotonic() + wait
    current = await get_job(job.job_id)
    while wait and current.status in {"pending", "processing"} and time.monotonic() < deadline:
        await asyncio.sleep(_POLL_INTERVAL)
        current = await get_job(job.job_id)
    return _to_result(current)


@router.get("/ocr/{job_id}", response_model=OcrResult)
async def ocr_result(job: Job = Depends(get_owned_job)):
    """Poll a job started with POST /api/v1/ocr."""
    return _to_result(job)
