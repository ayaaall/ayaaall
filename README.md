# SSP-ScanLine

## Overview

**SSP-ScanLine** is a fast, production‑ready web‑based OCR and PDF utility built with **FastAPI** (backend) and a sleek, modern UI (frontend).  It supports:

- **OCR** with eight interchangeable engines (RapidOCR, Docling, Marker, Qwen3.5‑VL, EasyOCR, Surya, DocTR, Keras‑OCR).
- **PDF → PNG** rasterisation.
- **PNG → PDF** assembly.
- **Split PDF** by page ranges.
- **Merge PDF** for combining multiple PDFs.
- **User accounts** — JWT auth (`identifiant` + `mot de passe`, optional name), refresh-token sessions, per-user job history.
- **PostgreSQL** persistence in production, zero-config SQLite for local dev.
- Clean, asynchronous background processing.

The UI follows a beautiful glass‑morphism design with a responsive sidebar, drag‑and‑drop uploads, progress tracking, and dynamic output download cards.

---

## Features

| Feature | Description |
|---------|-------------|
| **OCR** | Extract text and generate Markdown, DOCX, TXT outputs; tables exported to XLSX. Choose the OCR engine at runtime. |
| **Docling / Marker** | PDF-native engines: layout-aware parsing with real table extraction (DataFrames → XLSX) and full-document Markdown. |
| **Qwen3.5-VL 2B** | Vision-language-model OCR (default `Qwen/Qwen3.5-2B`, configurable via `QWEN_VL_MODEL`); tables come back as markdown blocks. |
| **PDF → PNG** | Convert each PDF page to a high‑resolution PNG (configurable DPI). |
| **PNG → PDF** | Assemble one or more PNG images into a single PDF. |
| **Split PDF** | Provide page ranges (e.g. `1‑3,5‑7`) to extract separate PDF files. |
| **Merge PDF** | Combine an arbitrary number of PDFs, preserving the original order or a custom order. |
| **Accounts & History** | JWT auth + refresh sessions; each user sees only their own job history. |
| **API** | Fully documented REST endpoints (`/api/auth/*`, `/api/jobs`, `/api/history`, …). |
| **Extensible** | New engines plug into the registry in `app/services/ocr_engine.py`. |

---

## Benchmark Models

| Model | Provider | Input | Strengths | Notes |
|-------|----------|-------|-----------|-------|
| **RapidOCR** | ONNX Runtime | page image | Very fast inference, low memory (default) | CPU-friendly |
| **Docling** | IBM | whole PDF | Layout parsing + TableFormer table extraction → XLSX | ~1 GB models on first run |
| **Marker** | Marker-PDF | whole PDF | Whole-document Markdown with rendered tables | GPU recommended |
| **Qwen3.5-VL 2B** | Qwen | page image | VLM OCR, best-in-class on hard scans; tables as markdown blocks | Slow on CPU; `QWEN_VL_MODEL` to switch |
| **EasyOCR** | EasyOCR (PyTorch) | page image | Broad language coverage | GPU recommended |
| **Surya** | Surya OCR (PyTorch) | page image | High accuracy on scanned documents | GPU recommended |
| **DocTR** | DocTR (PyTorch) | page image | Document-oriented detection + recognition | GPU recommended |
| **Keras‑OCR** | Keras-OCR (TensorFlow) | page image | Robust to distortion | Legacy architecture |

All engines are lazy‑loaded on first use and registered in `app/services/ocr_engine.py`. Image-based engines receive rasterised page images; PDF-native engines (`docling`, `marker`) receive the document directly for better layout/table fidelity. The API accepts an `ocr_model` option (`rapidocr`, `docling`, `marker`, `qwen35_vl`, `easyocr`, `surya`, `doctr`, `keras`).

---

## Confidence Metric

When OCR runs, each extracted line is accompanied by a **confidence score** (0‑100%). The score is calculated by the underlying OCR engine:

- **RapidOCR**: Average softmax probability of the top‑5 character predictions.
- **EasyOCR**: Mean of the confidence values returned by the CRNN for each character.
- **Surya**: Average probability of the detection and recognition heads.
- **DocTR**: Mean of the text line confidence returned by the transformer decoder.
- **Keras‑OCR**: Average of the bounding‑box confidence.

The service aggregates these per‑line scores into an overall job confidence by taking the mean of all line confidences. This metric gives a quick estimate of result reliability—higher values indicate clearer, well‑recognised text, while lower values suggest noisy input or difficult fonts. It is **not** a ground‑truth accuracy measure but helps users decide whether to trust the output or manually review it.

---

## Installation

### Local development (light stack)

```bash
python -m venv .venv
source .venv/bin/activate
pip install fastapi "uvicorn[standard]" python-multipart pydantic PyMuPDF pandas python-docx Pillow \
            "sqlalchemy[asyncio]" aiosqlite pyjwt bcrypt rapidocr_onnxruntime
uvicorn main:app --host 0.0.0.0 --port 8000
```

Defaults to SQLite at `./history.db` — no database server needed.

> **Note** – Heavy engines (torch, easyocr, surya, doctr, keras, docling, marker, transformers) install separately: `pip install -r requirements.txt`. Weights download automatically on first use of each engine.

### Docker (app + PostgreSQL)

```bash
cp .env.example .env        # edit JWT_SECRET etc.
docker compose up --build
```

The app listens on `${APP_PORT:-8000}` with Postgres, uploads, results and model-cache volumes.

### Qwen VLM backends (both 100% local)

- **`qwen35_vl`** — transformers in-process; weights cached in the `hf_cache` volume. Runtime is fully offline (`HF_HUB_OFFLINE=1`). One-time prefetch: `docker compose run --rm -e HF_HUB_OFFLINE=0 app python scripts/prefetch_models.py`
- **`qwen_ollama`** — delegates to a local [Ollama](https://ollama.com) server (`OLLAMA_HOST`, default `http://host.docker.internal:11434`). Pull the model first: `ollama pull qwen3.5:2b`. Requires an Ollama version that supports the `qwen35` architecture.

Each history session also keeps the **original uploaded document(s)** — re-download via the "Document original" chip in a reopened session or `GET /api/jobs/{job_id}/source`.

---

## Authentication

Register/login from the UI (`Identifiant` + `Mot de passe`, name optional). Endpoints return a short-lived **access token** (JWT) plus a revocable **refresh token** stored in the `sessions` table. All job/history endpoints require `Authorization: Bearer <token>`; output downloads accept `?token=` as well.

---

## Usage

Open `http://localhost:8000` in a browser.  Choose an action from the sidebar, upload the required file(s), optionally tweak the options (OCR model, DPI, page ranges, etc.), and click **Run**.  The UI will show a progress bar and, once finished, a list of downloadable output files.

---

## API Reference

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `POST` | `/api/auth/register` | Create account (`identifiant`, `password`, optional `name`). Returns tokens. |
| `POST` | `/api/auth/login` | Authenticate. Returns tokens. |
| `POST` | `/api/auth/refresh` | Rotate refresh token → new token pair. |
| `POST` | `/api/auth/logout` | Revoke a refresh token. |
| `GET`  | `/api/auth/me` | Current user profile. |
| `POST` | `/api/jobs` | Submit a job.  `multipart/form-data` with fields `files`, `action_type`, and optional `options` JSON. |
| `GET`  | `/api/jobs/{job_id}` | Poll job status / progress. |
| `GET`  | `/api/jobs/{job_id}/output?output_id=...` | Download a specific output file (header or `?token=`). |
| `GET`  | `/api/history` | Paginated history for the current user (`page`, `limit`). |
| `DELETE` | `/api/history/{job_id}` | Delete one of the user's jobs. |

---

## Development

The project follows a clean, modular layout:

```
app/
├─ api/          # FastAPI routers
├─ core/         # Config, DB helpers
├─ schemas/      # Pydantic models
└─ services/    # OCR engine wrapper & background job runners
```

Run the test suite (if added) with:

```bash
pytest
```

---

## License

MIT – feel free to use, modify, and distribute.

---

## Contributing

1. Fork the repository.
2. Create a feature branch (`git checkout -b feature/awesome‑feature`).
3. Commit your changes (`git commit -m "Add awesome feature"`).
4. Push to your fork and open a Pull Request.

---

## Contact

---

---

## Security notes

- **Secrets:** copy `.env.example` to `.env`. With `APP_ENV=production` the app refuses to start without a random `JWT_SECRET` (≥ 32 chars); `docker compose` also requires `POSTGRES_PASSWORD`.
- **Auth:** 30‑min access JWTs, single‑use refresh tokens stored as SHA‑256 digests, login throttling (app + nginx), bcrypt with constant‑time checks. Set `ALLOW_REGISTRATION=0` once your accounts exist.
- **Downloads** use the `Authorization` header only — tokens are never accepted in URLs.
- **Uploads:** content sniffed by magic bytes (PDF/images only), size/page/file‑count caps (`MAX_UPLOAD_MB`, `MAX_PDF_PAGES`, `MAX_FILES_PER_JOB`), stored under server‑generated names. Job options are strictly validated.
- **Deployment:** container runs as non‑root with a read‑only filesystem; the app port is bound to loopback so traffic goes through nginx (TLS 1.2+, HSTS, rate limits). CORS is same‑origin unless `CORS_ORIGINS` is set.
- Existing users must log in again after upgrading (refresh tokens are now hashed).

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

## Using the API from another project

API keys (`POST /api/keys`), a one-call OCR endpoint (`POST /api/v1/ocr?wait=60`) and a copy-paste Python client are available — see [API.md](API.md) and `client/ocr_client.py`.
