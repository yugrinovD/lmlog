from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lmlog.serve.app import create_app  # noqa: E402
from lmlog.serve.jobs import sanitize_filename  # noqa: E402

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "console-sample.log"


@pytest.fixture()
def client(tmp_path):
    app = create_app(data_dir=str(tmp_path), api_token=TOKEN, use_llm=False)
    with TestClient(app) as test_client:
        yield test_client


def upload(client, name="console-sample.log", content=None):
    data = content if content is not None else SAMPLE.read_bytes()
    response = client.post(
        "/api/v1/jobs",
        headers=AUTH,
        files={"file": (name, data, "text/plain")},
    )
    return response


def wait_for(client, job_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}", headers=AUTH)
        assert response.status_code == 200
        job = response.json()
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_health_is_public(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_api_requires_bearer_token(client):
    assert client.get("/api/v1/jobs").status_code == 401
    assert client.get("/api/v1/jobs", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_filename_sanitization():
    assert sanitize_filename("../../etc/passwd.log")[0] == "passwd.log"
    assert sanitize_filename("..\\..\\evil.txt")[0] == "evil.txt"
    assert sanitize_filename("") == ("upload.log", ".log")
    assert sanitize_filename("bad\x00name.jsonl")[0] == "badname.jsonl"


def test_upload_lifecycle_and_snapshot_views(client):
    response = upload(client)
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    job = wait_for(client, job_id)
    assert job["status"] == "completed", job
    assert job["stats"]["entries"] > 0
    assert job["filename"] == "console-sample.log"

    summary = client.get(f"/api/v1/snapshots/{job_id}/summary", headers=AUTH)
    assert summary.status_code == 200
    data = summary.json()
    assert data["signatures"] > 0
    assert data["errors"] > 0
    assert data["entries"] == job["stats"]["entries"]

    signatures = client.get(
        f"/api/v1/snapshots/{job_id}/signatures?limit=2&sort=errors", headers=AUTH
    )
    assert signatures.status_code == 200
    page = signatures.json()
    assert page["total"] >= len(page["items"]) == 2
    assert page["items"][0]["errors"] >= page["items"][1]["errors"]

    errors = client.get(f"/api/v1/snapshots/{job_id}/errors?limit=5", headers=AUTH)
    assert errors.status_code == 200
    assert errors.json()["total"] > 0

    trends = client.get(f"/api/v1/snapshots/{job_id}/trends?days=7", headers=AUTH)
    assert trends.status_code == 200
    assert isinstance(trends.json(), list)


def test_sse_replays_terminal_event(client):
    response = upload(client)
    job_id = response.json()["job_id"]
    wait_for(client, job_id)
    with client.stream("GET", f"/api/v1/jobs/{job_id}/events", headers=AUTH) as stream:
        assert stream.status_code == 200
        body = "".join(stream.iter_text())
    assert "event: started" in body
    assert "event: completed" in body
    assert "id: " in body


def test_upload_rejects_bad_extension_and_empty_file(client):
    bad = client.post(
        "/api/v1/jobs",
        headers=AUTH,
        files={"file": ("archive.gz", b"data", "application/gzip")},
    )
    assert bad.status_code == 415

    empty = upload(client, name="empty.log", content=b"")
    assert empty.status_code == 400


def test_upload_enforces_size_limit(client):
    client.app.state.manager.max_upload_bytes = 128
    response = upload(client, name="big.log", content=b"x" * 1024)
    assert response.status_code == 413


def test_snapshot_and_job_ids_are_validated(client):
    assert client.get("/api/v1/jobs/../../etc/passwd", headers=AUTH).status_code == 404
    assert client.get("/api/v1/jobs/not-a-uuid", headers=AUTH).status_code == 404
    assert client.get("/api/v1/snapshots/not-a-uuid/summary", headers=AUTH).status_code == 404


def test_explain_and_chat_use_heuristic_fallback(client):
    response = upload(client)
    job_id = response.json()["job_id"]
    wait_for(client, job_id)
    errors = client.get(f"/api/v1/snapshots/{job_id}/errors?limit=1", headers=AUTH).json()
    signature = errors["items"][0]["signature"]

    explained = client.post(
        "/api/v1/explain",
        headers=AUTH,
        json={
            "snapshot_id": job_id,
            "signature": signature,
            "use_llm": False,
        },
    )
    assert explained.status_code == 200
    payload = explained.json()
    assert payload["source"] == "heuristic"
    assert "WHAT IS HAPPENING" in payload["text"]

    cached = client.post(
        "/api/v1/explain",
        headers=AUTH,
        json={
            "snapshot_id": job_id,
            "signature": signature,
            "use_llm": False,
        },
    )
    assert cached.status_code == 200
    assert cached.json()["source"] == "cache"

    chat = client.post(
        "/api/v1/chat",
        headers=AUTH,
        json={
            "snapshot_id": job_id,
            "question": "What should I check first?",
            "use_llm": False,
        },
    )
    assert chat.status_code == 200
    answer = chat.json()
    assert answer["source"] == "heuristic"
    assert answer["citations"]
    assert "No retrieval" in answer["note"]


def test_explain_rejects_unknown_signature(client):
    response = upload(client)
    job_id = response.json()["job_id"]
    wait_for(client, job_id)
    missing = client.post(
        "/api/v1/explain",
        headers=AUTH,
        json={"snapshot_id": job_id, "signature": "missing", "use_llm": False},
    )
    assert missing.status_code == 404


def test_job_not_ready_returns_conflict(client, tmp_path):
    app = create_app(data_dir=str(tmp_path / "second"), api_token=TOKEN, use_llm=False)
    with TestClient(app) as other:
        job, path = other.app.state.manager.create("slow.log", 10)
        with open(path, "wb") as fh:
            fh.write(b"not finished\n")
        response = other.get(f"/api/v1/snapshots/{job.id}/summary", headers=AUTH)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "job_not_ready"
