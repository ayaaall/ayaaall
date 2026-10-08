"""
app/services/storage.py
───────────────────────
Safe handling of uploaded files and generated results on disk.

- Uploads are streamed with a hard size cap and never trust the client
  filename: the stored name/extension is derived from the file's magic bytes.
- Result/source paths are always resolved inside their storage root.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from fastapi import HTTPException, UploadFile

from app.core.config import MAX_UPLOAD_BYTES, MAX_UPLOAD_MB, RESULTS_DIR, UPLOAD_DIR

log = logging.getLogger(__name__)

_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class StoredUpload:
    filename: str      # original (display only, sanitised)
    path: Path         # where it lives on disk
    size_bytes: int
    kind: str          # "pdf" | "image"


def display_name(raw: str | None) -> str:
    """Client filename reduced to a harmless display string (no path, no control chars)."""
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    return name[:200] or "document"


def sniff(head: bytes) -> tuple[str, str] | None:
    """Return (extension, kind) from magic bytes, or None if unsupported."""
    if b"%PDF-" in head[:1024]:
        return "pdf", "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg", "image"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff", "image"
    if head.startswith(b"BM"):
        return "bmp", "image"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp", "image"
    return None


async def save_upload(upload: UploadFile, job_id: str) -> StoredUpload:
    """Stream an upload to disk, enforcing size and content-type limits."""
    shown = display_name(upload.filename)
    first = await upload.read(_CHUNK)
    detected = sniff(first)
    if detected is None:
        raise HTTPException(status_code=415, detail={
            "error": "unsupported_file_type",
            "message": f"'{shown}' is not a supported PDF or image file.",
        })
    ext, kind = detected

    dest = UPLOAD_DIR / f"{job_id}_{uuid.uuid4().hex[:8]}.{ext}"
    size = 0
    try:
        with dest.open("wb") as fh:
            chunk = first
            while chunk:
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail={
                        "error": "file_too_large",
                        "message": f"'{shown}' exceeds the {MAX_UPLOAD_MB} MB limit.",
                    })
                fh.write(chunk)
                chunk = await upload.read(_CHUNK)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return StoredUpload(filename=shown, path=dest, size_bytes=size, kind=kind)


def _inside(path: Path, root: Path) -> Path | None:
    """Return the resolved path if it stays within `root`, else None."""
    try:
        resolved = path.resolve()
        resolved.relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    return resolved


def result_file(job_id: str, output_id: str, fmt: str) -> Path | None:
    return _inside(RESULTS_DIR / job_id / f"{output_id}.{fmt}", RESULTS_DIR)


def source_file(path: str) -> Path | None:
    return _inside(Path(path), UPLOAD_DIR)


def delete_job_files(job_id: str, source_paths: Iterable[str]) -> None:
    """Best-effort removal of everything a job stored on disk."""
    for raw in source_paths:
        p = source_file(raw)
        if p is not None:
            p.unlink(missing_ok=True)
    res = _inside(RESULTS_DIR / job_id, RESULTS_DIR)
    if res is not None and res.is_dir():
        shutil.rmtree(res, ignore_errors=True)
    for leftover in UPLOAD_DIR.glob(f"{job_id}_*"):
        leftover.unlink(missing_ok=True)
