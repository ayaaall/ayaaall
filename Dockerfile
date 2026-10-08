# ── SSP-ScanLine app image ───────────────────────────────────────────────────
FROM python:3.11-slim
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ENV HTTP_PROXY=${HTTP_PROXY}
ENV HTTPS_PROXY=${HTTPS_PROXY}
ENV NO_PROXY=${NO_PROXY}
ENV http_proxy=${HTTP_PROXY}
ENV https_proxy=${HTTPS_PROXY}
ENV no_proxy=${NO_PROXY}

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

EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host $HOST --port $PORT"]
