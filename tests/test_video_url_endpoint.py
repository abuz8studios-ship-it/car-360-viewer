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


def _prepare_endpoint(monkeypatch, tmp_path, owner_email=OWNER):
    db_path = tmp_path / "vehicles.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE users (email TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO users VALUES (?)", [(OWNER,), (OTHER,)])
        conn.execute(
            "CREATE TABLE vehicles (vin TEXT PRIMARY KEY, video_url TEXT, video_type TEXT, "
            "show_video INTEGER, user_email TEXT, status TEXT DEFAULT 'active')"
        )
        conn.execute(
            "INSERT INTO vehicles VALUES (?, ?, ?, 1, ?, 'active')",
            (VIN, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube", OWNER),
        )
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(a2_extensions, "DB_PATH", db_path)
    monkeypatch.setattr(main, "db_get_vehicle", lambda vin: {"vin": vin, "user_email": owner_email})
    monkeypatch.setattr(main, "db_get_user", lambda email: {"email": email})
    monkeypatch.delenv("LOTVUE_OWNER_EMAIL", raising=False)
    return TestClient(main.app)


def test_video_url_write_requires_a_session_and_vehicle_ownership(monkeypatch, tmp_path) -> None:
    client = _prepare_endpoint(monkeypatch, tmp_path)
    form = {"vin": VIN, "video_url": "https://youtu.be/dQw4w9WgXcQ"}

    anonymous = client.post("/api/upload/video-url", data=form)
    assert anonymous.status_code == 401

    wrong_session = client.post(
        "/api/upload/video-url",
        data=form,
        headers={"Authorization": f"Bearer {_make_tokens(OTHER)['access_token']}"},
    )
    assert wrong_session.status_code == 404


def test_vehicle_access_rejects_symlinked_storage_before_writing(monkeypatch, tmp_path) -> None:
    client = _prepare_endpoint(monkeypatch, tmp_path)
    storage = tmp_path / "storage"
    storage.mkdir()
    target = storage / "OTHER123"
    target.mkdir()
    (storage / VIN).symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(main, "STORAGE_DIR", storage)
    auth = {"Authorization": f"Bearer {_make_tokens(OWNER)['access_token']}"}

    response = client.post(
        "/api/upload/video-url",
        data={"vin": VIN, "video_url": "https://youtu.be/dQw4w9WgXcQ"},
        headers=auth,
    )

    assert response.status_code == 404


def test_video_url_write_normalizes_and_blank_value_clears(monkeypatch, tmp_path) -> None:
    client = _prepare_endpoint(monkeypatch, tmp_path)
    auth = {"Authorization": f"Bearer {_make_tokens(OWNER)['access_token']}"}

    saved = client.post(
        "/api/upload/video-url",
        data={"vin": VIN, "video_url": "https://youtu.be/dQw4w9WgXcQ"},
        headers=auth,
    )
    assert saved.status_code == 200
    assert saved.json()["video_url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert saved.json()["video_type"] == "youtube"

    cleared = client.post(
        "/api/upload/video-url",
        data={"vin": VIN, "video_url": ""},
        headers=auth,
    )
    assert cleared.status_code == 200
    with sqlite3.connect(main.DB_PATH) as conn:
        video_url, video_type, visible = conn.execute(
            "SELECT video_url, video_type, show_video FROM vehicles WHERE vin=?", (VIN,)
        ).fetchone()
    assert (video_url, video_type, visible) == ("", "", 0)
