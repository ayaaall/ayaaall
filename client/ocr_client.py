"""
ocr_client.py — minimal Python client for the PDF-Tools OCR API.

Copy this single file into your project (needs `pip install httpx`).

    from ocr_client import OcrClient

    api = OcrClient("https://ocr.example.com", api_key="ocr_...")
    result = api.ocr("scan.pdf", wait=60)
    print(result["text"])
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

import httpx


class OcrApiError(Exception):
    def __init__(self, status_code: int, detail: Any):
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class OcrClient:
    def __init__(self, base_url: str, api_key: str, *, timeout: float = 150.0,
                 verify: bool | str = True) -> None:
        """`verify` may be False/a CA path for self-signed deployments (avoid False in production)."""
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout, verify=verify,
            headers={"X-API-Key": api_key},
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "OcrClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── helpers ────────────────────────────────────────────────────────────
    def _json(self, resp: httpx.Response) -> dict:
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise OcrApiError(resp.status_code, detail)
        return resp.json()

    # ── OCR (simple) ───────────────────────────────────────────────────────
    def ocr(self, path: str | Path, *, model: str = "rapidocr", wait: int = 30) -> dict:
        """Upload one PDF/image. With `wait` > 0 the text is returned if ready in time;
        otherwise call `wait_for(result['job_id'])`."""
        path = Path(path)
        with path.open("rb") as fh:
            resp = self._http.post("/api/v1/ocr", params={"wait": wait},
                                   data={"ocr_model": model}, files={"file": (path.name, fh)})
        return self._json(resp)

    def result(self, job_id: str) -> dict:
        return self._json(self._http.get(f"/api/v1/ocr/{job_id}"))

    def wait_for(self, job_id: str, *, timeout: float = 600, interval: float = 2.0) -> dict:
        """Poll until the job is complete (raises on error or timeout)."""
        deadline = time.monotonic() + timeout
        while True:
            res = self.result(job_id)
            if res["status"] == "complete":
                return res
            if res["status"] == "error":
                raise OcrApiError(500, f"Job {job_id} failed")
            if time.monotonic() > deadline:
                raise TimeoutError(f"Job {job_id} not finished after {timeout}s")
            time.sleep(interval)

    # ── Other tools (pdf_to_png, png_to_pdf, split_pdf, merge_pdf, ocr) ────
    def submit(self, action: str, paths: Iterable[str | Path], options: dict | None = None) -> dict:
        handles = [Path(p).open("rb") for p in paths]
        try:
            files = [("files", (Path(h.name).name, h)) for h in handles]
            resp = self._http.post("/api/jobs", data={"action_type": action,
                                                      "options": json.dumps(options or {})}, files=files)
        finally:
            for h in handles:
                h.close()
        return self._json(resp)

    def job(self, job_id: str) -> dict:
        return self._json(self._http.get(f"/api/jobs/{job_id}"))

    def download(self, job_id: str, output_id: str, dest: str | Path) -> Path:
        """Save one output file (see `outputs` of the job) to `dest`."""
        resp = self._http.get(f"/api/jobs/{job_id}/output", params={"output_id": output_id})
        if resp.status_code >= 400:
            raise OcrApiError(resp.status_code, resp.text)
        dest = Path(dest)
        dest.write_bytes(resp.content)
        return dest
