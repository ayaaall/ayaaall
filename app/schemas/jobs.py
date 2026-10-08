"""
app/schemas/jobs.py
────────────────────
Pydantic models that define the exact request / response contract
for every endpoint. Keeping them isolated makes it easy to version
the API without touching business logic.
"""

from __future__ import annotations

from typing import Any, List, Optional
from pydantic import BaseModel, Field


# ── Shared ─────────────────────────────────────────────────────────────────────

class OutputFile(BaseModel):
    """A single generated output file attached to a completed job."""
    output_id:  str
    format:     str
    label:      Optional[str] = None
    size_bytes: int


# ── POST /api/jobs ─────────────────────────────────────────────────────────────

class CreateJobResponse(BaseModel):
    job_id:       str
    status:       str
    action_type:  str
    file_count:   int
    submitted_at: str


# ── GET /api/jobs/{job_id} ─────────────────────────────────────────────────────

class JobStatusResponse(BaseModel):
    job_id:       str
    status:       str
    progress:     Optional[int]     = None
    action_type:  Optional[str]     = None
    outputs:      Optional[List[OutputFile]] = None
    preview_text: Optional[str]     = None
    sources:      Optional[List["SourceFile"]] = None


class SourceFile(BaseModel):
    """Original uploaded document kept with a job (history re-download)."""
    filename:   str
    size_bytes: int


# ── GET /api/history ───────────────────────────────────────────────────────────

class HistoryItem(BaseModel):
    job_id:      str
    filename:    Optional[str]
    action_type: str
    status:      str
    created_at:  str
    outputs:     List[str] = Field(default_factory=list)   # list of formats


class HistoryResponse(BaseModel):
    page:  int
    limit: int
    total: int
    items: List[HistoryItem]


# ── POST /api/files/metadata ───────────────────────────────────────────────────

class FileMetadataResponse(BaseModel):
    file_id: str
    stored:  bool


# ── DELETE /api/history/{job_id} ───────────────────────────────────────────────

class DeleteResponse(BaseModel):
    job_id:  str
    deleted: bool
