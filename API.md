# Using the OCR API from another project

Base URL: `https://<your-host>/api` — interactive docs at `/docs` (when `ENABLE_DOCS=1`).

## 1. Get an API key

Log in to the web UI (or call the API), then create a key. The key is shown **once**.

```bash
TOKEN=$(curl -s -X POST $HOST/api/auth/login -H 'Content-Type: application/json' \
  -d '{"identifiant":"me","password":"..."}' | jq -r .access_token)

curl -s -X POST $HOST/api/keys -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"name":"billing-service"}'
# → {"id":"key_…","api_key":"ocr_…", …}
```

`GET /api/keys` lists keys (prefix + last use), `DELETE /api/keys/{id}` revokes one.
Keys act on behalf of their owner (same jobs/history) and cannot manage other keys.
Store them in a secret manager / environment variable — never in source control or front-end code.

Send the key with either header:

```
X-API-Key: ocr_…
Authorization: Bearer ocr_…
```

## 2. OCR in one call

```bash
curl -H "X-API-Key: $KEY" -F file=@scan.pdf -F ocr_model=rapidocr \
  "$HOST/api/v1/ocr?wait=60"
```

```json
{"job_id":"job_…","status":"complete","progress":100,
 "text":"…","markdown":"# OCR Result …","outputs":["txt","md","docx","xlsx"]}
```

- `wait` (0–120 s): block until done. If it takes longer you get `"status":"processing"` —
  poll `GET /api/v1/ocr/{job_id}`.
- Accepted: PDF and images (png, jpg, tiff, bmp, webp), up to `MAX_UPLOAD_MB` (50 MB default) and
  `MAX_PDF_PAGES` (300). Engines: `GET /api/engines`.
- Other formats (docx, xlsx tables): `GET /api/jobs/{job_id}/output?output_id=out_docx`.

## 3. Other tools

`POST /api/jobs` (multipart: `files`, `action_type`, `options`) with `pdf_to_png`, `png_to_pdf`,
`split_pdf` (`{"ranges":[[1,3],[5,8]]}`), `merge_pdf` (`{"file_order":[1,0]}`); poll `GET /api/jobs/{id}`.

## 4. Python client

```python
from ocr_client import OcrClient          # client/ocr_client.py — copy it, needs httpx

with OcrClient("https://ocr.example.com", api_key=os.environ["OCR_API_KEY"]) as api:
    res = api.ocr("scan.pdf", wait=60)
    if res["status"] != "complete":
        res = api.wait_for(res["job_id"])
    print(res["text"])
```

## Errors

| Code | Meaning |
|------|---------|
| 400 | invalid options / file count / wrong file type for the action |
| 401 | missing, invalid or revoked key |
| 413 | file larger than the limit |
| 415 | not a PDF/image (checked on content, not extension) |
| 422 | malformed parameters (e.g. `wait` out of range) |
| 429 | too many requests (nginx / login throttle) |

Errors return `{"detail": …}`. A job that fails during processing has `"status":"error"`.
