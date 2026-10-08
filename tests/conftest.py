import os
import sys
import tempfile
from pathlib import Path

# Isolated config BEFORE the app is imported.
_tmp = tempfile.mkdtemp(prefix="ocr-tests-")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp}/test.db"
os.environ["JWT_SECRET"] = "t" * 48
os.environ["APP_ENV"] = "development"
os.environ["LOGIN_MAX_ATTEMPTS"] = "3"
os.environ["MAX_UPLOAD_MB"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

import app.core.config as config


@pytest.fixture(scope="session")
def client():
    from main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth(client):
    """Register a fresh user; return (headers, tokens)."""
    import uuid

    ident = f"user-{uuid.uuid4().hex[:8]}"
    r = client.post("/api/auth/register", json={"identifiant": ident, "password": "correct-horse-1"})
    assert r.status_code == 201, r.text
    tokens = r.json()
    return {"Authorization": f"Bearer {tokens['access_token']}"}, tokens


def make_pdf(pages: int = 3) -> bytes:
    import fitz

    doc = fitz.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"page {i + 1}")
    data = doc.tobytes()
    doc.close()
    return data
