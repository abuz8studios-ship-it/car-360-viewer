from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
sys.path.insert(0, str(APP_DIR))

import h_dealer_import as dealer_import  # noqa: E402


class _Response:
    def __init__(self, chunks: list[bytes], content_length: str = "", status_code: int = 200):
        self.chunks = chunks
        self.status_code = status_code
        self.headers = {"content-length": content_length}
        self.closed = False

    def iter_content(self, chunk_size: int):
        assert chunk_size == 64 * 1024
        yield from self.chunks

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, response: _Response):
        self.response = response

    def get(self, *args, **kwargs):
        assert kwargs.get("stream") is True
        return self.response


def test_scrape_response_limit_rejects_declared_and_streamed_overflow() -> None:
    declared = _Response([], content_length="11")
    with pytest.raises(ValueError, match="size limit"):
        dealer_import._read_response_bytes(declared, 10)

    streamed = _Response([b"123456", b"78901"])
    with pytest.raises(ValueError, match="size limit"):
        dealer_import._read_response_bytes(streamed, 10)


def test_remote_photo_download_streams_and_normalizes_jpeg(tmp_path: Path) -> None:
    from PIL import Image

    buffer = io.BytesIO()
    Image.effect_noise((300, 300), 100).convert("RGB").save(buffer, "JPEG", quality=95)
    jpeg = buffer.getvalue()
    response = _Response([jpeg[index:index + 65536] for index in range(0, len(jpeg), 65536)],
                         content_length=str(len(jpeg)))
    target = tmp_path / "p_0001.jpg"

    written = dealer_import._download_jpeg(_Session(response), "https://cdn.example/image.jpg",
                                           target, len(jpeg) + 1)

    assert written == len(jpeg)
    assert target.is_file()
    assert target.read_bytes().startswith(b"\xff\xd8\xff")
    assert response.closed
    assert not list(tmp_path.glob("*.download"))
    with Image.open(target) as normalized:
        assert normalized.format == "JPEG"


def test_failed_remote_import_preserves_previous_photo_set(tmp_path: Path, monkeypatch) -> None:
    vin = "1HGCM82633A004352"
    import_dir = tmp_path / vin / "_import"
    import_dir.mkdir(parents=True)
    old_photo = import_dir / "p_0001.jpg"
    old_photo.write_bytes(b"previous-good-photo")

    class _ClosableSession:
        def close(self):
            pass

    monkeypatch.setattr(dealer_import, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(dealer_import, "_session", lambda: _ClosableSession())
    monkeypatch.setattr(dealer_import, "_get_inventory", lambda *_args, **_kwargs: {
        "items": [], "by_vin": {vin: {"detail_url": "https://dealer.example/car", "_meta": {}}}
    })
    monkeypatch.setattr(dealer_import, "_fetch_text", lambda *_args, **_kwargs: (200, "detail"))
    monkeypatch.setitem(dealer_import._PHOTO_EXTRACTORS, "drivesmartwc", lambda _html: ["one", "two"])
    monkeypatch.setattr(dealer_import, "_download_jpeg", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad photo")))
    monkeypatch.setattr(dealer_import.time, "sleep", lambda *_args: None)

    response = dealer_import._import_vehicle_work("drivesmartwc", vin, "owner@example.com")

    assert response.status_code == 502
    assert old_photo.read_bytes() == b"previous-good-photo"
    assert not list(import_dir.parent.glob(".import-stage-*"))


def test_successful_remote_import_swaps_complete_photo_set(tmp_path: Path, monkeypatch) -> None:
    vin = "1HGCM82633A004352"
    import_dir = tmp_path / vin / "_import"
    import_dir.mkdir(parents=True)
    (import_dir / "p_0001.jpg").write_bytes(b"previous-good-photo")

    class _ClosableSession:
        def close(self):
            pass

    monkeypatch.setattr(dealer_import, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(dealer_import, "_session", lambda: _ClosableSession())
    monkeypatch.setattr(dealer_import, "_get_inventory", lambda *_args, **_kwargs: {
        "items": [], "by_vin": {vin: {"detail_url": "https://dealer.example/car", "_meta": {}}}
    })
    monkeypatch.setattr(dealer_import, "_fetch_text", lambda *_args, **_kwargs: (200, "detail"))
    monkeypatch.setitem(dealer_import._PHOTO_EXTRACTORS, "drivesmartwc", lambda _html: ["one", "two", "one"])
    monkeypatch.setattr(dealer_import.time, "sleep", lambda *_args: None)

    def download(_session, _url, destination, _limit):
        destination.write_bytes(b"normalized-jpeg")
        return 15

    monkeypatch.setattr(dealer_import, "_download_jpeg", download)
    registered = []
    monkeypatch.setattr(dealer_import, "_db_register_vehicle", lambda *args, **kwargs: registered.append((args, kwargs)))
    monkeypatch.setattr(dealer_import, "_classify_and_route", lambda _vin: {"exterior": 0})

    result = dealer_import._import_vehicle_work("drivesmartwc", vin, "owner@example.com")

    assert result["photos_found"] == 2
    assert result["photos_saved"] == 2
    assert sorted(path.name for path in import_dir.glob("p_*.jpg")) == ["p_0001.jpg", "p_0002.jpg"]
    assert (import_dir / "p_0001.jpg").read_bytes() == b"normalized-jpeg"
    assert registered and registered[0][1]["user_email"] == "owner@example.com"
