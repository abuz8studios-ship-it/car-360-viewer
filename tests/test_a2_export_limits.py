from __future__ import annotations

import asyncio
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
sys.path.insert(0, str(APP_DIR))

import a2_extensions  # noqa: E402


def test_frame_directory_skips_symlinks_and_caps_file_count(tmp_path: Path, monkeypatch) -> None:
    vin = "1HGCM82633A004352"
    spin_dir = tmp_path / vin / "spin"
    spin_dir.mkdir(parents=True)
    (spin_dir / "spin_0001.jpg").write_bytes(b"frame one")
    (spin_dir / "spin_0002.jpg").write_bytes(b"frame two")
    (spin_dir / "spin_0003.jpg").write_bytes(b"frame three")
    outside = tmp_path / "private.jpg"
    outside.write_bytes(b"private")
    (spin_dir / "spin_0000.jpg").symlink_to(outside)

    monkeypatch.setattr(a2_extensions, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(a2_extensions, "MAX_EXPORT_FRAMES", 2)

    frame_dir, frames = a2_extensions._frames_dir(vin)

    assert frame_dir == spin_dir.resolve()
    assert [frame.name for frame in frames] == ["spin_0001.jpg", "spin_0002.jpg"]


def test_frame_export_zip_uses_disk_backed_archive(tmp_path: Path, monkeypatch) -> None:
    from PIL import Image

    vin = "1HGCM82633A004352"
    spin_dir = tmp_path / vin / "spin"
    spin_dir.mkdir(parents=True)
    for index, color in enumerate(("red", "blue"), 1):
        Image.new("RGB", (24, 24), color).save(spin_dir / f"spin_{index:04d}.jpg")
    monkeypatch.setattr(a2_extensions, "STORAGE_DIR", tmp_path)

    response = a2_extensions.export_zip(vin)

    assert response.media_type == "application/zip"
    archive_path = Path(response.path)
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == [f"{vin}_frame_01.jpg", f"{vin}_frame_02.jpg"]
        assert archive.read(archive.namelist()[0]).startswith(b"\xff\xd8\xff")
    asyncio.run(response.background())
    assert not archive_path.exists()


def test_frame_export_zip_rejects_oversized_source_set(tmp_path: Path, monkeypatch) -> None:
    vin = "1HGCM82633A004352"
    spin_dir = tmp_path / vin / "spin"
    spin_dir.mkdir(parents=True)
    (spin_dir / "spin_0001.jpg").write_bytes(b"0123456789")
    monkeypatch.setattr(a2_extensions, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(a2_extensions, "MAX_EXPORT_INPUT_BYTES", 5)

    with pytest.raises(a2_extensions.HTTPException) as exc_info:
        a2_extensions.export_zip(vin)

    assert exc_info.value.status_code == 413


def test_gif_export_clamps_width_and_rate(tmp_path: Path, monkeypatch) -> None:
    from PIL import Image

    vin = "1HGCM82633A004352"
    spin_dir = tmp_path / vin / "spin"
    spin_dir.mkdir(parents=True)
    for index, color in enumerate(("red", "blue"), 1):
        Image.new("RGB", (48, 32), color).save(spin_dir / f"spin_{index:04d}.jpg")
    monkeypatch.setattr(a2_extensions, "STORAGE_DIR", tmp_path)

    response = a2_extensions.export_gif(vin, width=5000, fps=5000)

    assert response.media_type == "image/gif"
    assert Path(response.path).name == "spin_640_30.gif"
    with Image.open(response.path) as gif:
        assert gif.width <= 640
        assert gif.n_frames == 2
