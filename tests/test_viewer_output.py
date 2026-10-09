from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
sys.path.insert(0, str(APP_DIR))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

VIN = "1HGCM82633A004352"


def test_public_viewer_rejects_invalid_vehicle_ids_with_400() -> None:
    response = TestClient(main.app).get("/view/invalid%21vin")
    assert response.status_code == 400


def test_public_viewer_escapes_database_text_and_hides_legacy_video(tmp_path, monkeypatch) -> None:
    storage = tmp_path / "storage"
    exterior = storage / VIN / "exterior"
    exterior.mkdir(parents=True)
    (exterior / "frame_0001.jpg").write_bytes(b"not decoded by the HTML route")
    monkeypatch.setattr(main, "STORAGE_DIR", storage)
    monkeypatch.setattr(
        main,
        "db_get_vehicle",
        lambda vin: {
            "vin": vin,
            "year": "2025",
            "make": '<img src=x onerror="alert(1)">',
            "model": "Coupe",
            "price": '<svg onload="alert(2)">',
            "mileage": "123",
            "color": "Blue",
            "trim_level": "<script>alert(3)</script>",
            "video_url": '</script><script>alert("video")</script>',
            "video_type": "upload",
        },
    )
    response = TestClient(main.app).get(f"/view/{VIN}")
    assert response.status_code == 200
    body = response.text
    assert "&lt;img" in body
    assert "&lt;svg" in body
    assert "&lt;script&gt;alert(3)&lt;/script&gt;" in body
    assert 'alert("video")' not in body
    assert 'const VIDEO_URL = "";' in body
    assert "container.innerHTML = `<video" not in body
    assert "container.innerHTML = `<iframe" not in body
