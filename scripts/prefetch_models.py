"""
scripts/prefetch_models.py
──────────────────────────
One-time download of OCR/VLM weights into the local HF cache so the
app can then run **fully offline** (HF_HUB_OFFLINE=1).

Usage
─────
    # host venv
    .venv/bin/python scripts/prefetch_models.py

    # inside docker (downloads into the hf_cache volume)
    docker compose run --rm app python scripts/prefetch_models.py

Env
───
    QWEN_VL_MODEL   model id to prefetch (default: Qwen/Qwen3.5-2B)
"""

from __future__ import annotations

import os


def main() -> None:
    from huggingface_hub import snapshot_download

    qwen_model = os.getenv("QWEN_VL_MODEL", "Qwen/Qwen3.5-2B")

    print(f"Prefetching {qwen_model} → {os.getenv('HF_HOME', '~/.cache/huggingface')}")
    snapshot_download(repo_id=qwen_model)
    print(f"✔ {qwen_model} cached.")

    # docling's layout + table-structure models also live on the Hub.
    try:
        from docling.utils.model_downloader import download_models
        print("Prefetching docling models…")
        download_models()
        print("✔ docling models cached.")
    except ImportError:
        print("docling not installed — skipping its model prefetch.")

    print("The app can now run with HF_HUB_OFFLINE=1.")


if __name__ == "__main__":
    main()
