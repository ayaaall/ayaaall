import io
import time

import pytest

from tests.conftest import make_pdf


def _wait_done(client, headers, job_id, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        r = client.get(f"/api/jobs/{job_id}", headers=headers)
        if r.json()["status"] in {"complete", "error"}:
            return r.json()
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def _post_job(client, headers, action, files, options="{}"):
    return client.post("/api/jobs", headers=headers, data={"action_type": action, "options": options},
                       files=[("files", f) for f in files])


# ── Auth ───────────────────────────────────────────────────────────────────────

def test_endpoints_require_auth(client):
    for method, url in [("get", "/api/history"), ("get", "/api/engines"),
                        ("get", "/api/jobs/job_x"), ("post", "/api/files/metadata")]:
        assert getattr(client, method)(url).status_code == 401


def test_query_string_token_is_not_accepted(client, auth):
    headers, tokens = auth
    r = client.get(f"/api/jobs/job_x/output?output_id=a&token={tokens['access_token']}")
    assert r.status_code == 401


def test_weak_password_rejected(client):
    r = client.post("/api/auth/register", json={"identifiant": "weakling", "password": "short"})
    assert r.status_code == 422


def test_refresh_token_is_single_use_and_stored_hashed(client, auth):
    _, tokens = auth
    refresh = tokens["refresh_token"]
    r1 = client.post("/api/auth/refresh", json={"refresh_token": refresh})
    assert r1.status_code == 200
    assert client.post("/api/auth/refresh", json={"refresh_token": refresh}).status_code == 401

    import asyncio
    from sqlalchemy import select
    from app.core.database import SessionLocal, Session

    async def raw_tokens():
        async with SessionLocal() as db:
            return [r for r in (await db.execute(select(Session.token))).scalars()]

    assert refresh not in asyncio.run(raw_tokens())


def test_login_is_throttled(client):
    client.post("/api/auth/register", json={"identifiant": "throttled", "password": "correct-horse-1"})
    codes = [client.post("/api/auth/login", json={"identifiant": "throttled", "password": "wrong-password"}).status_code
             for _ in range(5)]
    assert codes[:3] == [401, 401, 401]
    assert 429 in codes[3:]
    # even the right password is blocked while throttled
    assert client.post("/api/auth/login", json={"identifiant": "throttled", "password": "correct-horse-1"}).status_code == 429


def test_unknown_user_login_is_401(client):
    assert client.post("/api/auth/login", json={"identifiant": "nobody-here", "password": "whatever-pw"}).status_code == 401


# ── Uploads ────────────────────────────────────────────────────────────────────

def test_path_traversal_filename_is_harmless(client, auth):
    headers, _ = auth
    r = _post_job(client, headers, "ocr", [("../../../../tmp/evil.x/../../y", io.BytesIO(make_pdf(1)), "application/pdf")])
    assert r.status_code == 200, r.text
    st = _wait_done(client, headers, r.json()["job_id"])
    assert st["sources"][0]["filename"] == "y"


def test_non_pdf_content_rejected_regardless_of_extension(client, auth):
    headers, _ = auth
    r = _post_job(client, headers, "ocr", [("evil.pdf", io.BytesIO(b"MZ\x90\x00not a pdf"), "application/pdf")])
    assert r.status_code == 415


def test_oversized_upload_rejected(client, auth):
    headers, _ = auth
    big = make_pdf(1) + b"0" * (2 * 1024 * 1024)
    r = _post_job(client, headers, "ocr", [("big.pdf", io.BytesIO(big), "application/pdf")])
    assert r.status_code == 413


def test_wrong_kind_for_action(client, auth):
    headers, _ = auth
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    r = _post_job(client, headers, "split_pdf", [("a.png", io.BytesIO(png), "image/png")], '{"ranges": [[1, 1]]}')
    assert r.status_code == 400


@pytest.mark.parametrize("opts", [
    '{"ranges": [[0, 1]]}', '{"ranges": [[3, 1]]}', '{"ranges": []}', '{"ranges": "1-2"}', "not json", "[]",
])
def test_bad_split_options_rejected(client, auth, opts):
    headers, _ = auth
    r = _post_job(client, headers, "split_pdf", [("a.pdf", io.BytesIO(make_pdf(2)), "application/pdf")], opts)
    assert r.status_code == 400


def test_dpi_bounds(client, auth):
    headers, _ = auth
    r = _post_job(client, headers, "pdf_to_png", [("a.pdf", io.BytesIO(make_pdf(1)), "application/pdf")], '{"dpi": 100000}')
    assert r.status_code == 400


def test_merge_order_must_be_permutation(client, auth):
    headers, _ = auth
    files = [("a.pdf", io.BytesIO(make_pdf(1)), "application/pdf"), ("b.pdf", io.BytesIO(make_pdf(1)), "application/pdf")]
    r = _post_job(client, headers, "merge_pdf", files, '{"file_order": [0, 5]}')
    assert r.status_code == 400


def test_unknown_ocr_engine_rejected(client, auth):
    headers, _ = auth
    r = _post_job(client, headers, "ocr", [("a.pdf", io.BytesIO(make_pdf(1)), "application/pdf")], '{"ocr_model": "../../etc"}')
    assert r.status_code == 400


# ── Functional paths + ownership ───────────────────────────────────────────────

def test_split_merge_roundtrip_and_download(client, auth):
    headers, _ = auth
    r = _post_job(client, headers, "split_pdf", [("a.pdf", io.BytesIO(make_pdf(4)), "application/pdf")],
                  '{"ranges": [[1, 2], [3, 4]]}')
    st = _wait_done(client, headers, r.json()["job_id"])
    assert st["status"] == "complete" and len(st["outputs"]) == 2
    out = client.get(f"/api/jobs/{st['job_id']}/output", params={"output_id": "out_range_0"}, headers=headers)
    assert out.status_code == 200 and out.content.startswith(b"%PDF")

    files = [("a.pdf", io.BytesIO(make_pdf(1)), "application/pdf"), ("b.pdf", io.BytesIO(make_pdf(2)), "application/pdf")]
    r = _post_job(client, headers, "merge_pdf", files, '{"file_order": [1, 0]}')
    assert _wait_done(client, headers, r.json()["job_id"])["status"] == "complete"


def test_split_range_beyond_document_errors_cleanly(client, auth):
    headers, _ = auth
    r = _post_job(client, headers, "split_pdf", [("a.pdf", io.BytesIO(make_pdf(2)), "application/pdf")], '{"ranges": [[1, 9]]}')
    assert _wait_done(client, headers, r.json()["job_id"])["status"] == "error"


def test_pdf_to_png_job(client, auth):
    headers, _ = auth
    r = _post_job(client, headers, "pdf_to_png", [("a.pdf", io.BytesIO(make_pdf(2)), "application/pdf")], '{"dpi": 72}')
    st = _wait_done(client, headers, r.json()["job_id"])
    assert st["status"] == "complete" and len(st["outputs"]) == 2


def test_jobs_are_isolated_between_users(client, auth):
    h1, _ = auth
    r = _post_job(client, h1, "pdf_to_png", [("a.pdf", io.BytesIO(make_pdf(1)), "application/pdf")], '{"dpi": 72}')
    job_id = r.json()["job_id"]
    _wait_done(client, h1, job_id)

    other = client.post("/api/auth/register", json={"identifiant": "intruder-1", "password": "correct-horse-1"}).json()
    h2 = {"Authorization": f"Bearer {other['access_token']}"}
    assert client.get(f"/api/jobs/{job_id}", headers=h2).status_code == 404
    assert client.get(f"/api/jobs/{job_id}/output", params={"output_id": "out_page_1"}, headers=h2).status_code == 404
    assert client.get(f"/api/jobs/{job_id}/source", headers=h2).status_code == 404
    assert client.delete(f"/api/history/{job_id}", headers=h2).status_code == 404
    assert client.get("/api/history", headers=h2).json()["total"] == 0


def test_delete_history_removes_files(client, auth):
    from app.core.config import RESULTS_DIR

    headers, _ = auth
    r = _post_job(client, headers, "pdf_to_png", [("a.pdf", io.BytesIO(make_pdf(1)), "application/pdf")], '{"dpi": 72}')
    job_id = r.json()["job_id"]
    _wait_done(client, headers, job_id)
    assert (RESULTS_DIR / job_id).is_dir()
    assert client.delete(f"/api/history/{job_id}", headers=headers).status_code == 200
    assert not (RESULTS_DIR / job_id).exists()


def test_history_pagination_bounds(client, auth):
    headers, _ = auth
    assert client.get("/api/history?limit=100000", headers=headers).status_code == 422
    assert client.get("/api/history?page=0", headers=headers).status_code == 422


# ── Hardening ──────────────────────────────────────────────────────────────────

def test_security_headers_and_csp(client):
    r = client.get("/")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_no_wildcard_cors(client):
    r = client.options("/api/history", headers={"Origin": "https://evil.example",
                                                "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in r.headers


def test_weak_jwt_secret_refused_in_production(monkeypatch):
    import importlib
    import app.core.config as cfg

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("JWT_SECRET", "change-me-in-production")
    with pytest.raises(RuntimeError):
        importlib.reload(cfg)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("JWT_SECRET", "t" * 48)
    importlib.reload(cfg)


def test_ocr_pdf_job_with_stub_engine(client, auth, monkeypatch):
    import app.services.job_runners as jr

    monkeypatch.setattr(jr, "extract_text", lambda path, model: "hello\x00 world")
    headers, _ = auth
    r = _post_job(client, headers, "ocr", [("a.pdf", io.BytesIO(make_pdf(2)), "application/pdf")], '{"ocr_model": "rapidocr"}')
    st = _wait_done(client, headers, r.json()["job_id"])
    assert st["status"] == "complete"
    assert {o["format"] for o in st["outputs"]} >= {"txt", "md", "docx"}
    assert "hello" in st["preview_text"]
