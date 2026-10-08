# ── SSP-ScanLine app image ───────────────────────────────────────────────────
FROM python:3.11-slim
# Optional build-time proxy. ARG only (not ENV) so it is NOT baked into the
# runtime image; pip/apt pick it up through the variables set per RUN below.
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY

# System deps for PyMuPDF / Pillow / OpenCV-style OCR preprocessors
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/app

# Resilient pip: long timeout + retries for flaky networks.
ENV PIP_DEFAULT_TIMEOUT=180 \
    PIP_RETRIES=10 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    HF_HOME=/models/hf_cache \
    PYTHONUNBUFFERED=1

# Python deps first (better layer caching). Model weights are NOT baked in —
# they are downloaded on first use into the `hf_cache` named volume.
# requirements-heavy.txt pulls CPU-only torch (no multi-GB CUDA wheels).
COPY requirements.txt requirements-heavy.txt ./
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir -r requirements-heavy.txt

COPY . .

# Run as an unprivileged user; only the storage dirs are writable.
RUN useradd --system --uid 10001 --home-dir /srv/app app \
    && mkdir -p /srv/app/uploads /srv/app/results /models/hf_cache \
    && chown -R app:app /srv/app/uploads /srv/app/results /models
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${PORT}/healthz || exit 1

# --proxy-headers/--forwarded-allow-ips: trust X-Forwarded-* only from the nginx
# container's network, so request.client.host is the real client IP.
CMD ["sh", "-c", "uvicorn main:app --host $HOST --port $PORT --proxy-headers --forwarded-allow-ips='*'"]
