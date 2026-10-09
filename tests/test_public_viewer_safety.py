from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import main  # noqa: E402

VIN = "1HGCM82633A004352"
JOB_ID = "public-viewer-test"


def _client_for_storage(monkeypatch, tmp_path: Path, *, status: str = "active") -> tuple[TestClient, Path]:
    storage = tmp_path / "storage"
    spin = storage / VIN / "spin"
    spin.mkdir(parents=True)
    (spin / "spin_0001.jpg").write_bytes(b"public-frame")
    monkeypatch.setattr(main, "STORAGE_DIR", storage)
    monkeypatch.setattr(
        main,
        "db_get_vehicle_by_job_id",
        lambda job_id: {"vin": VIN, "status": status} if job_id == JOB_ID else None,
    )
    return TestClient(main.app), storage


def test_public_viewer_serves_active_frames_and_limits_the_manifest(monkeypatch, tmp_path: Path) -> None:
    client, storage = _client_for_storage(monkeypatch, tmp_path)
    for number in range(2, 140):
        (storage / VIN / "spin" / f"spin_{number:04}.jpg").write_bytes(b"frame")

    page = client.get(f"/viewer/{JOB_ID}")
    assert page.status_code == 200
    assert page.text.count("/static/") == 120

    frame = client.get(f"/static/{JOB_ID}/frames/spin_0001.jpg")
    assert frame.status_code == 200
    assert frame.content == b"public-frame"


def test_public_viewer_rejects_inactive_inventory(monkeypatch, tmp_path: Path) -> None:
    client, _ = _client_for_storage(monkeypatch, tmp_path, status="deleted")

    assert client.get(f"/viewer/{JOB_ID}").status_code == 404
    assert client.get(f"/static/{JOB_ID}/frames/spin_0001.jpg").status_code == 404


def test_public_viewer_rejects_vehicle_and_spin_directory_symlinks(monkeypatch, tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    outside = tmp_path / "outside"
    spin = outside / "spin"
    spin.mkdir(parents=True)
    (spin / "spin_0001.jpg").write_bytes(b"private")
    storage.mkdir()
    (storage / VIN).symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(main, "STORAGE_DIR", storage)
    monkeypatch.setattr(
        main, "db_get_vehicle_by_job_id", lambda _job_id: {"vin": VIN, "status": "active"}
    )
    client = TestClient(main.app)

    assert client.get(f"/viewer/{JOB_ID}").status_code == 404
    assert client.get(f"/static/{JOB_ID}/frames/spin_0001.jpg").status_code == 404

    (storage / VIN).unlink()
    (storage / VIN).mkdir()
    (storage / VIN / "spin").symlink_to(spin, target_is_directory=True)
    assert client.get(f"/viewer/{JOB_ID}").status_code == 404
    assert client.get(f"/static/{JOB_ID}/frames/spin_0001.jpg").status_code == 404


def test_public_frame_route_rejects_file_symlinks(monkeypatch, tmp_path: Path) -> None:
    client, storage = _client_for_storage(monkeypatch, tmp_path)
    outside = tmp_path / "private.jpg"
    outside.write_bytes(b"private")
    frame = storage / VIN / "spin" / "spin_0002.jpg"
    frame.symlink_to(outside)

    response = client.get(f"/static/{JOB_ID}/frames/{frame.name}")
    assert response.status_code == 404


def test_public_vdp_embed_and_manifest_reject_vehicle_directory_symlinks(monkeypatch, tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    outside = tmp_path / "outside"
    (outside / "spin").mkdir(parents=True)
    (outside / "spin" / "spin_0001.jpg").write_bytes(b"private")
    (storage / VIN).symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(main, "STORAGE_DIR", storage)
    monkeypatch.setattr(main, "db_get_vehicle", lambda vin: {"vin": vin, "status": "active"})
    client = TestClient(main.app)

    assert client.get(f"/view/{VIN}").status_code == 404
    assert client.get(f"/embed/{VIN}").status_code == 404
    assert client.get(f"/api/vehicles/{VIN}/viewer").status_code == 404
