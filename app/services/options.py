"""
app/services/options.py
───────────────────────
Strict validation of the user-supplied `options` JSON, per action type.

Everything the runners later index into (file orders, page ranges, DPI,
engine names) is checked here so a malformed request fails fast with a 400
instead of crashing — or exhausting memory — inside a background task.
"""

from __future__ import annotations

import json
from typing import Any, List

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.core.config import DEFAULT_EXPORT_DPI, MAX_DPI, MIN_DPI, ActionType

MAX_OPTIONS_BYTES = 10_000
MAX_RANGES = 100


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


class OcrOptions(_Base):
    ocr_model: str = "rapidocr"

    @field_validator("ocr_model")
    @classmethod
    def _known_engine(cls, v: str) -> str:
        from app.services.ocr_engine import available_engines  # lazy: heavy module

        if v not in available_engines():
            raise ValueError(f"Unknown OCR engine '{v}'.")
        return v


class PdfToPngOptions(_Base):
    dpi: int = Field(default=DEFAULT_EXPORT_DPI, ge=MIN_DPI, le=MAX_DPI)


class SplitPdfOptions(_Base):
    ranges: List[List[int]] = Field(min_length=1, max_length=MAX_RANGES)

    @field_validator("ranges")
    @classmethod
    def _valid_ranges(cls, v: List[List[int]]) -> List[List[int]]:
        for pair in v:
            if len(pair) != 2 or pair[0] < 1 or pair[1] < pair[0]:
                raise ValueError("Each range must be [start, end] with 1 <= start <= end.")
        return v


class PngToPdfOptions(_Base):
    page_order: List[int] | None = None


class MergePdfOptions(_Base):
    file_order: List[int] | None = None


_MODELS: dict[str, type[_Base]] = {
    ActionType.OCR: OcrOptions,
    ActionType.PDF_TO_PNG: PdfToPngOptions,
    ActionType.SPLIT_PDF: SplitPdfOptions,
    ActionType.PNG_TO_PDF: PngToPdfOptions,
    ActionType.MERGE_PDF: MergePdfOptions,
}


def _check_permutation(order: list[int], n: int, field: str) -> None:
    if sorted(order) != list(range(n)):
        raise HTTPException(status_code=400, detail={
            "error": "invalid_options",
            "message": f"'{field}' must list each file index 0..{n - 1} exactly once.",
        })


def validate_options(action_type: str, raw: str, file_count: int) -> dict[str, Any]:
    """Parse and validate the options JSON; return a clean dict for the runner."""
    if len(raw.encode("utf-8")) > MAX_OPTIONS_BYTES:
        raise HTTPException(status_code=400, detail={
            "error": "invalid_options", "message": "Options payload too large.",
        })
    try:
        data = json.loads(raw or "{}")
        if not isinstance(data, dict):
            raise ValueError("options must be a JSON object")
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail={
            "error": "invalid_options", "message": "Options must be a valid JSON object.",
        })

    try:
        opts = _MODELS[action_type](**data).model_dump()
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        raise HTTPException(status_code=400, detail={
            "error": "invalid_options", "message": f"{loc}: {first['msg']}",
        })

    if action_type == ActionType.PNG_TO_PDF:
        order = opts["page_order"] if opts["page_order"] is not None else list(range(file_count))
        _check_permutation(order, file_count, "page_order")
        opts["page_order"] = order
    elif action_type == ActionType.MERGE_PDF:
        order = opts["file_order"] if opts["file_order"] is not None else list(range(file_count))
        _check_permutation(order, file_count, "file_order")
        opts["file_order"] = order
    return opts
