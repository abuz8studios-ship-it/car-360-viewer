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


def _client(monkeypatch, tmp_path: Path, submission: str):
    db_path = tmp_path / "vehicles.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE users (email TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO users VALUES (?)", (OWNER,))
        conn.execute(
            "CREATE TABLE vehicles (vin TEXT PRIMARY KEY, user_email TEXT, status TEXT, "
            "watermark_mode TEXT DEFAULT 'preview')"
        )
        conn.execute(
            "INSERT INTO vehicles (vin,user_email,status) VALUES (?,?,'active')", (VIN, OWNER)
        )
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(a2_extensions, "DB_PATH", db_path)
    monkeypatch.setattr(main, "STORAGE_DIR", storage)
    monkeypatch.setattr(main, "FEATURES_ENABLED", {**main.FEATURES_ENABLED, "watermark": True})
    writes = []
    jobs = []
    monkeypatch.setattr(main, "_write_clean_frame_status", lambda *args: writes.append(args))
    monkeypatch.setattr(
        main,
        "_submit_background_job",
        lambda *args: jobs.append(args) or submission,
    )
    return TestClient(main.app), db_path, writes, jobs


def test_clean_watermark_queues_rebuild_instead_of_rewriting_in_route(
    monkeypatch, tmp_path: Path
) -> None:
    client, db_path, statuses, jobs = _client(monkeypatch, tmp_path, "started")
    auth = {"Authorization": f"Bearer {_make_tokens(OWNER)['access_token']}"}

    response = client.post(f"/api/watermark/{VIN}", data={"mode": "clean"}, headers=auth)

    assert response.status_code == 200
    assert response.json() == {"status": "processing", "mode": "clean", "vin": VIN}
    assert statuses == [(VIN, "queued")]
    assert jobs == [(f"clean-frames:{VIN}", main._run_clean_frames_job, VIN)]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT watermark_mode FROM vehicles WHERE vin=?", (VIN,)).fetchone() == (
            "clean",
        )


def test_clean_watermark_queue_full_returns_retryable_error(monkeypatch, tmp_path: Path) -> None:
    client, db_path, statuses, jobs = _client(monkeypatch, tmp_path, "busy")
    auth = {"Authorization": f"Bearer {_make_tokens(OWNER)['access_token']}"}

    response = client.post(f"/api/watermark/{VIN}", data={"mode": "clean"}, headers=auth)

    assert response.status_code == 429
    assert response.headers["retry-after"] == "30"
    assert statuses[-1] == (VIN, "error", "Background processing capacity is full")
    assert len(jobs) == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT watermark_mode FROM vehicles WHERE vin=?", (VIN,)).fetchone() == (
            "preview",
        )
