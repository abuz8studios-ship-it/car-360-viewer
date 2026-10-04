from __future__ import annotations

import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from ai_studio import video_walkaround  # noqa: E402

VIN = "1HGCM82633A004352"


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
    vehicle_dir = storage / VIN
    vehicle_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_jpeg(outside / "frame_0001.jpg")
    (vehicle_dir / "studio").symlink_to(outside, target_is_directory=True)
    (vehicle_dir / "walkaround").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(video_walkaround, "STORAGE_DIR", storage)

    assert video_walkaround.pick_best_angles(vehicle_dir / "studio") == {}
    assert video_walkaround.generate_walkaround(VIN, {}) is None
    assert video_walkaround.get_walkaround_status(VIN)["status"] == "error"
    assert video_walkaround.get_walkaround_status(VIN)["error"] == "Unsafe vehicle media path"
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


def test_comfyui_output_paths_are_confined_and_symlink_safe(tmp_path, monkeypatch) -> None:
    output_dir = tmp_path / "comfy-output"
    output_dir.mkdir()
    output_file = output_dir / "clips" / "render.mp4"
    output_file.parent.mkdir()
    output_file.write_bytes(b"clip")
    outside = tmp_path / "private.mp4"
    outside.write_bytes(b"private")
    monkeypatch.setattr(video_walkaround, "COMFY_OUTPUT", output_dir)

    assert video_walkaround._safe_comfy_output_file("clips/render.mp4") == output_file
    assert video_walkaround._safe_comfy_output_file("../private.mp4") is None
    assert video_walkaround._safe_comfy_output_file(str(outside)) is None
    try:
        (output_dir / "linked.mp4").symlink_to(outside)
    except OSError:
        return
    assert video_walkaround._safe_comfy_output_file("linked.mp4") is None


@pytest.mark.parametrize(
    ("returncode", "write_output", "expected"),
    [(0, True, True), (1, True, False), (0, False, False)],
)
def test_silent_voiceover_fallback_requires_successful_nonempty_output(
    tmp_path, monkeypatch, returncode: int, write_output: bool, expected: bool
) -> None:
    output = tmp_path / "voiceover.wav"

    def comfyui_unavailable(*_args, **_kwargs):
        raise OSError("ComfyUI unavailable")

    def fake_ffmpeg(command, **_kwargs):
        if write_output:
            Path(command[-1]).write_bytes(b"silence audio")
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(urllib.request, "urlopen", comfyui_unavailable)
    monkeypatch.setattr(video_walkaround.subprocess, "run", fake_ffmpeg)

    assert video_walkaround.generate_tts_voiceover("test vehicle", str(output)) is expected


def test_assembly_failure_preserves_the_previous_public_video(tmp_path, monkeypatch) -> None:
    working_dir = tmp_path / ".walkaround-work"
    working_dir.mkdir()
    clip = working_dir / "clip_front.mp4"
    clip.write_bytes(b"clip")
    voiceover = working_dir / "voiceover.wav"
    voiceover.write_bytes(b"generated audio")
    output = tmp_path / "walkaround.mp4"
    output.write_bytes(b"previous valid video")

    def failed_ffmpeg(command, **_kwargs):
        staged = Path(command[-1])
        assert staged.parent == working_dir
        assert staged.name.startswith(".walkaround-")
        staged.write_bytes(b"partial incomplete output")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(video_walkaround.subprocess, "run", failed_ffmpeg)

    assert video_walkaround._assemble_walkaround(
        [clip], voiceover, output, working_dir
    ) is False
    assert output.read_bytes() == b"previous valid video"
    assert sorted(path.name for path in working_dir.iterdir()) == ["clip_front.mp4", "concat.txt", "voiceover.wav"]


@pytest.mark.parametrize(
    ("returncode", "write_output", "expected_status"),
    [(0, True, "done"), (1, True, "error"), (0, False, "error")],
)
def test_walkaround_reports_done_only_for_nonempty_successful_assembly(
    tmp_path, monkeypatch, returncode: int, write_output: bool, expected_status: str
) -> None:
    storage = tmp_path / "storage"
    exterior = storage / VIN / "exterior"
    exterior.mkdir(parents=True)
    for index in range(4):
        _write_jpeg(exterior / f"frame_{index + 1:04d}.jpg")
    output_dir = storage / VIN / "walkaround"
    output_dir.mkdir()
    previous_video = output_dir / "walkaround.mp4"
    previous_video.write_bytes(b"old video")
    (output_dir / "clip_front.mp4").write_bytes(b"legacy public intermediate")
    (output_dir / "voiceover.wav").write_bytes(b"legacy public audio")
    (output_dir / "concat.txt").write_text("legacy public concat", encoding="utf-8")
    outside_clip = tmp_path / "outside.mp4"
    outside_clip.write_bytes(b"outside data")
    try:
        (output_dir / "clip_side.mp4").symlink_to(outside_clip)
    except OSError:
        outside_clip = None
    comfy_output = tmp_path / "comfy-output"
    comfy_output.mkdir()
    factory_dir = tmp_path / "factory"
    factory_dir.mkdir()
    monkeypatch.setattr(video_walkaround, "STORAGE_DIR", storage)
    monkeypatch.setattr(video_walkaround, "COMFY_OUTPUT", comfy_output)
    monkeypatch.setattr(video_walkaround, "FACTORY_DIR", factory_dir)
    monkeypatch.setattr(video_walkaround, "generate_tts_voiceover", lambda _text, _path: False)
    rendered = []

    def render_one(_photo_path, _prompt):
        filename = f"clip-{len(rendered)}.mp4"
        (comfy_output / filename).write_bytes(b"generated clip")
        rendered.append(filename)
        return filename

    monkeypatch.setitem(sys.modules, "factory", SimpleNamespace(render_one=render_one))
    path_entry = str(factory_dir.resolve())
    try:
        def fake_ffmpeg(command, **_kwargs):
            staged = Path(command[-1])
            assert staged.parent.name.startswith(".walkaround-work-")
            assert staged.name.startswith(".walkaround-")
            if write_output:
                staged.write_bytes(b"complete new walkaround" if returncode == 0 else b"partial output")
            return SimpleNamespace(returncode=returncode)

        monkeypatch.setattr(video_walkaround.subprocess, "run", fake_ffmpeg)
        result = video_walkaround.generate_walkaround(
            VIN, {"year": 2022, "make": "Test", "model": "Car"}
        )
    finally:
        if path_entry in sys.path:
            sys.path.remove(path_entry)

    assert len(rendered) == 4
    status = video_walkaround.get_walkaround_status(VIN)
    assert status["status"] == expected_status
    if expected_status == "done":
        assert result == str(previous_video)
        assert previous_video.read_bytes() == b"complete new walkaround"
    else:
        assert result is None
        assert status["error"] == "Video assembly failed"
        assert previous_video.read_bytes() == b"old video"
    assert [path.name for path in output_dir.iterdir()] == ["walkaround.mp4"]
    if outside_clip is not None:
        assert outside_clip.read_bytes() == b"outside data"
