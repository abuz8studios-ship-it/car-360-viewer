from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import main  # noqa: E402

VIN = "1HGCM82633A004352"


def _public_page(monkeypatch, tmp_path: Path, video_url: str, video_type: str) -> str:
    storage = tmp_path / "storage"
    spin = storage / VIN / "spin"
    spin.mkdir(parents=True)
    (spin / "spin_0001.jpg").write_bytes(b"frame")
    monkeypatch.setattr(main, "STORAGE_DIR", storage)
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "viewer.sqlite3")
    monkeypatch.setattr(
        main,
        "db_get_vehicle",
        lambda vin: {
            "vin": vin,
            "status": "active",
            "year": "2025",
            "make": "Honda",
            "model": "Accord",
            "video_url": video_url,
            "video_type": video_type,
        },
    )
    return TestClient(main.app).get(f"/view/{VIN}").text


def test_legacy_unsafe_video_url_is_not_reflected_into_public_page(monkeypatch, tmp_path: Path) -> None:
    page = _public_page(
        monkeypatch,
        tmp_path,
        "javascript:alert(\"xss\")",
        "upload",
    )

    assert 'const VIDEO_URL = "";' in page
    assert "javascript:alert" not in page
    assert "xss" not in page


def test_markup_in_direct_video_url_is_json_escaped_and_assigned_via_dom(monkeypatch, tmp_path: Path) -> None:
    page = _public_page(
        monkeypatch,
        tmp_path,
        "https://video.example.test/</script><script>alert(1)</script>.mp4",
        "upload",
    )

    assert "</script><script>alert(1)" not in page
    assert "\\u003c/script\\u003e\\u003cscript\\u003e" in page
    assert "video.src = parsed.href" in page
    assert "container.innerHTML = VIDEO_URL" not in page
