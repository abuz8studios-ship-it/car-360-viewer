from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
sys.path.insert(0, str(APP_DIR))

import viewer_api  # noqa: E402

VIN = "1HGCM82633A004352"


def test_viewer_api_escapes_database_content_and_limits_asset_paths(tmp_path, monkeypatch) -> None:
    storage = tmp_path / "storage"
    spin = storage / VIN / "spin"
    spin.mkdir(parents=True)
    (spin / "frame_0001.jpg").write_bytes(b"fake image")
    (spin / '<script>.jpg').write_bytes(b"ignored filename")
    (storage / VIN / "meta.json").write_text(
        json.dumps({"vehicle": {"year": "2025", "make": '<script>alert("title")</script>', "model": "Coupe"}}),
        encoding="utf-8",
    )
    db_path = tmp_path / "viewer.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE vehicles (vin TEXT PRIMARY KEY, status TEXT, year TEXT, make TEXT, "
            "model TEXT, price TEXT, mileage TEXT)"
        )
        conn.execute(
            "INSERT INTO vehicles (vin, status, year, make, model, price, mileage) "
            "VALUES (?, 'active', '2025', '<script>alert(\"title\")</script>', 'Coupe', '0', '0')",
            (VIN,),
        )
        conn.execute(
            "CREATE TABLE hotspots (id INTEGER, vin TEXT, photo_filename TEXT, tag TEXT, "
            "hotspot_x REAL, hotspot_y REAL, condition_type TEXT, severity TEXT, note TEXT, close_up TEXT)"
        )
        conn.execute(
            "INSERT INTO hotspots VALUES (1, ?, ?, ?, .5, .4, ?, ?, ?, ?)",
            (VIN, "frame_0001.jpg", '<img src=x onerror="alert(2)">', "scratch", "major",
             '<script>alert("note")</script>', "bad/../../payload.jpg"),
        )
    monkeypatch.setattr(viewer_api, "STORAGE_ROOTS", (storage,))
    monkeypatch.setattr(viewer_api, "DB_PATH", db_path)
    app = FastAPI()
    viewer_api.register_viewer(app)
    client = TestClient(app)

    vdp = client.get(f"/vdp/{VIN}")
    assert vdp.status_code == 200
    assert "&lt;script&gt;alert(&quot;title&quot;)&lt;/script&gt;" in vdp.text
    assert '<script>alert("title")</script>' not in vdp.text
    assert '<script>.jpg' not in vdp.text

    report = client.get(f"/vdp/{VIN}/damage-report")
    assert report.status_code == 200
    assert '&lt;img src=x onerror=&quot;alert(2)&quot;&gt;' in report.text
    assert '&lt;script&gt;alert("note")&lt;/script&gt;' in report.text
    assert 'payload.jpg' not in report.text


def test_viewer_identifier_rejects_path_and_html_payloads() -> None:
    for value in ("../secret", "VIN%2f..", '<script>alert(1)</script>', ""):
        try:
            viewer_api.validate_vehicle_id(value)
        except Exception as error:
            assert getattr(error, "status_code", None) == 400
        else:
            raise AssertionError(f"unsafe vehicle identifier accepted: {value!r}")


def test_active_vehicle_symlink_cannot_expose_another_vehicle(tmp_path, monkeypatch) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    target = storage / "OTHER123"
    target.mkdir()
    (target / "meta.json").write_text('{"vehicle":{"make":"secret"}}', encoding="utf-8")
    (storage / VIN).symlink_to(target, target_is_directory=True)
    db_path = tmp_path / "viewer.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE vehicles (vin TEXT PRIMARY KEY, status TEXT)")
        conn.execute("INSERT INTO vehicles VALUES (?, 'active')", (VIN,))
    monkeypatch.setattr(viewer_api, "STORAGE_ROOTS", (storage,))
    monkeypatch.setattr(viewer_api, "DB_PATH", db_path)

    try:
        viewer_api.build_manifest(VIN)
    except Exception as error:
        assert getattr(error, "status_code", None) == 404
    else:
        raise AssertionError("a symlinked vehicle directory must never be public")


def test_spin_manifest_skips_symlinks_and_is_bounded(tmp_path) -> None:
    vehicle_dir = tmp_path / VIN
    spin = vehicle_dir / "spin"
    spin.mkdir(parents=True)
    for index in range(125):
        (spin / f"frame_{index:03}.jpg").write_bytes(b"frame")
    (spin / "frame_link.jpg").symlink_to(spin / "frame_001.jpg")

    frames = viewer_api._spin_frames(vehicle_dir, VIN)

    assert len(frames) == 120
    assert all("frame_link.jpg" not in frame for frame in frames)
    assert frames == sorted(frames)
