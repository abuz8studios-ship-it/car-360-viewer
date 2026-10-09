from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
sys.path.insert(0, str(APP_DIR))

import a2_extensions  # noqa: E402
import main  # noqa: E402
from a2_extensions import _make_tokens  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

VIN = "1HGCM82633A004352"
OWNER = "dealer@example.test"
OTHER = "other@example.test"


class _JobRecorder:
    calls = []


def _record_job(job_key, target, *args, **kwargs):
    _JobRecorder.calls.append((job_key, target, args, kwargs))
    return "started"


def _prepare(monkeypatch, tmp_path):
    storage = tmp_path / "storage"
    (storage / VIN / "exterior").mkdir(parents=True)
    (storage / VIN / "exterior" / "dealer-photo.jpg").write_bytes(b"image")
    db_path = tmp_path / "vehicles.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE users (email TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO users VALUES (?)", [(OWNER,), (OTHER,)])
        conn.execute(
            "CREATE TABLE vehicles (vin TEXT PRIMARY KEY, user_email TEXT, status TEXT)"
        )
        conn.execute("INSERT INTO vehicles VALUES (?, ?, 'active')", (VIN, OWNER))
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(a2_extensions, "DB_PATH", db_path)
    script = tmp_path / "fake-commercial-pipeline.py"
    script.write_text("# mocked; the test records thread startup", encoding="utf-8")
    monkeypatch.setattr(main, "STORAGE_DIR", storage)
    monkeypatch.setattr(main, "_COMMERCIAL_SCRIPT", str(script))
    monkeypatch.setattr(main, "_COMFYUI_PYTHON", sys.executable)
    monkeypatch.setattr(main, "_COMMERCIAL_JOB_LOCK", __import__("threading").Lock())
    monkeypatch.setattr(main, "_submit_background_job", _record_job)
    _JobRecorder.calls = []
    monkeypatch.setattr(main, "_comfyui_is_available", lambda: True)
    monkeypatch.setattr(main, "db_get_vehicle", lambda vin: {
        "vin": VIN,
        "user_email": OWNER,
        "status": "active",
        "year": "2025",
        "make": "Example",
        "model": "Model",
    })
    monkeypatch.setattr(main, "db_get_user", lambda email: {"email": email} if email in {OWNER, OTHER} else None)
    monkeypatch.delenv("LOTVUE_OWNER_EMAIL", raising=False)
    return TestClient(main.app)


def _auth(email: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_tokens(email)['access_token']}"}


def test_legacy_commercial_route_starts_owner_scoped_job_and_polls_status(monkeypatch, tmp_path) -> None:
    client = _prepare(monkeypatch, tmp_path)
    body = {"vin": VIN, "script": "A carefully worded vehicle commercial.", "style": "social"}

    started = client.post("/api/generate-commercial", json=body, headers=_auth(OWNER))
    assert started.status_code == 200
    assert started.json()["status"] == "started"
    assert len(_JobRecorder.calls) == 1
    job_key, target, args, kwargs = _JobRecorder.calls[0]
    assert job_key == f"commercial:{VIN}"
    assert target is main._run_commercial_pipeline_bg
    assert args[0] == VIN
    assert args[3:5] == (body["script"], body["style"])
    assert not kwargs

    duplicate = client.post("/api/generate-commercial", json=body, headers=_auth(OWNER))
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "already_running"
    assert len(_JobRecorder.calls) == 1

    status = client.get(f"/api/vehicle/{VIN}/commercial-status", headers=_auth(OWNER))
    assert status.status_code == 200
    assert status.json()["status"] == "running"

    other_tenant = client.get(f"/api/vehicle/{VIN}/commercial-status", headers=_auth(OTHER))
    assert other_tenant.status_code == 404


def test_commercial_route_rejects_bad_options_and_unavailable_comfyui(monkeypatch, tmp_path) -> None:
    client = _prepare(monkeypatch, tmp_path)
    invalid_style = client.post(
        "/api/generate-commercial",
        json={"vin": VIN, "style": "unsupported"},
        headers=_auth(OWNER),
    )
    assert invalid_style.status_code == 422

    too_long = client.post(
        "/api/generate-commercial",
        json={"vin": VIN, "script": "x" * 601},
        headers=_auth(OWNER),
    )
    assert too_long.status_code == 422

    monkeypatch.setattr(main, "_comfyui_is_available", lambda: False)
    unavailable = client.post(
        f"/api/vehicle/{VIN}/generate-commercial",
        headers=_auth(OWNER),
    )
    assert unavailable.status_code == 503
    assert _JobRecorder.calls == []


def test_commercial_route_rejects_another_tenant_and_anonymous_request(monkeypatch, tmp_path) -> None:
    client = _prepare(monkeypatch, tmp_path)
    anonymous = client.post("/api/generate-commercial", json={"vin": VIN})
    assert anonymous.status_code == 401

    denied = client.post(
        "/api/generate-commercial",
        json={"vin": VIN},
        headers=_auth(OTHER),
    )
    assert denied.status_code == 404
    assert _JobRecorder.calls == []
