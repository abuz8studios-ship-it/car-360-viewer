from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from ai_studio import video_walkaround  # noqa: E402


def _write_jpeg(path: Path, color: tuple[int, int, int] = (30, 80, 140)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (16, 12), color)
    try:
        image.save(path, format="JPEG", quality=80)
    finally:
        image.close()


def test_pick_best_angles_caps_frames_and_preserves_clockwise_positions(tmp_path, monkeypatch) -> None:
    source = tmp_path / "exterior"
    source.mkdir()
    for index in range(100):
        _write_jpeg(source / f"frame_{index:04d}.jpg", (index % 255, 80, 140))

    angles = video_walkaround.pick_best_angles(source)

    assert set(angles) == {"front", "side", "rear", "quarter"}
    expected = {
        "front": "frame_0000.jpg",
        "side": "frame_0018.jpg",
        "rear": "frame_0036.jpg",
        "quarter": "frame_0054.jpg",
    }
    assert {name: path.name for name, path in angles.items()} == expected
    expanded_request = video_walkaround.pick_best_angles(
        source, max_frames=5000, max_scan_entries=5000
    )
    assert {name: path.name for name, path in expanded_request.items()} == expected

    validated = []
    monkeypatch.setattr(
        video_walkaround,
        "_source_photo_is_valid",
        lambda path, _pixel_cap: (validated.append(path), True)[1],
    )
    video_walkaround.pick_best_angles(source)
    assert len(validated) == 4


def test_pick_best_angles_ignores_symlinks_corrupt_oversize_and_pixel_limit(tmp_path) -> None:
    source = tmp_path / "exterior"
    source.mkdir()
    _write_jpeg(source / "frame_0001.jpg")
    _write_jpeg(source / "frame_0003.jpg")
    _write_jpeg(source / "frame_0004.jpg")
    (source / "frame_0002.jpg").write_bytes(b"not a JPEG")
    outside = tmp_path / "outside.jpg"
    _write_jpeg(outside)
    try:
        (source / "frame_0000.jpg").symlink_to(outside)
    except OSError:
        pass

    angles = video_walkaround.pick_best_angles(source)

    assert "side" not in angles
    assert set(angles.values()) == {
        source / "frame_0001.jpg",
        source / "frame_0003.jpg",
        source / "frame_0004.jpg",
    }
    assert video_walkaround.pick_best_angles(source, max_photo_bytes=1) == {}
    assert video_walkaround.pick_best_angles(source, max_image_pixels=100) == {}
    assert video_walkaround.pick_best_angles(source, validation_bytes_limit=1) == {}

    hidden_only = tmp_path / "hidden-only"
    hidden_only.mkdir()
    _write_jpeg(hidden_only / ".hidden.jpg")
    assert video_walkaround.pick_best_angles(hidden_only) == {}


def test_walkaround_rejects_symlinked_media_directories(tmp_path, monkeypatch) -> None:
    storage = tmp_path / "storage"
    vehicle_dir = storage / "1HGCM82633A004352"
    vehicle_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_jpeg(outside / "frame_0001.jpg")
    (vehicle_dir / "studio").symlink_to(outside, target_is_directory=True)
    (vehicle_dir / "walkaround").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(video_walkaround, "STORAGE_DIR", storage)

    assert video_walkaround.pick_best_angles(vehicle_dir / "studio") == {}
    assert video_walkaround.generate_walkaround("1HGCM82633A004352", {}) is None
    assert video_walkaround.get_walkaround_status("1HGCM82633A004352")["status"] == "error"
    assert video_walkaround.get_walkaround_status("1HGCM82633A004352")["error"] == "Unsafe vehicle media path"
    assert sorted(path.name for path in outside.iterdir()) == ["frame_0001.jpg"]


def test_directory_enumeration_stops_exactly_at_scan_cap(tmp_path, monkeypatch) -> None:
    class Entry:
        def __init__(self, name: str) -> None:
            self.name = name

        def is_symlink(self) -> bool:
            return False

        def is_file(self, *, follow_symlinks: bool) -> bool:
            assert follow_symlinks is False
            return True

    class Entries:
        def __init__(self, names: list[str]) -> None:
            self._entries = iter(Entry(name) for name in names)
            self.read_count = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def __iter__(self):
            return self

        def __next__(self):
            self.read_count += 1
            return next(self._entries)

    names_for_test = [f"frame_{index:05d}.jpg" for index in range(5000)]
    entries = Entries(names_for_test)
    monkeypatch.setattr(video_walkaround.os, "scandir", lambda _directory: entries)

    names = video_walkaround._bounded_jpeg_names(
        tmp_path, max_frames=5, max_scan_entries=7
    )

    assert entries.read_count == 7
    assert names == [f"frame_{index:05d}.jpg" for index in range(5)]

    entries = Entries(names_for_test)
    monkeypatch.setattr(video_walkaround.os, "scandir", lambda _directory: entries)
    capped_names = video_walkaround._bounded_jpeg_names(
        tmp_path, max_frames=5000, max_scan_entries=5000
    )
    assert entries.read_count == video_walkaround.MAX_WALKAROUND_SCAN_ENTRIES == 4096
    assert len(capped_names) == video_walkaround.MAX_WALKAROUND_FRAMES == 72
