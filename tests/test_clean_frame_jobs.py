from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import main  # noqa: E402

VIN = "1HGCM82633A004352"


def test_clean_frame_rebuild_uses_retained_vehicle_sources(monkeypatch, tmp_path) -> None:
    storage = tmp_path / "storage"
    vehicle_dir = storage / VIN
    raw_dir = vehicle_dir / "raw"
    exterior_dir = vehicle_dir / "exterior"
    raw_dir.mkdir(parents=True)
    exterior_dir.mkdir()
    (exterior_dir / "frame_0001.jpg").write_bytes(b"old-watermarked")
    for index in range(4):
        (raw_dir / f"frame_{index + 1:04d}.jpg").write_bytes(f"clean-{index}".encode())
    monkeypatch.setattr(main, "STORAGE_DIR", storage)
    monkeypatch.setattr(main, "RAW_DIR", storage / "raw")
    monkeypatch.setattr(main, "db_get_vehicle", lambda vin: {
        "turntable_style": "none", "background_id": "",
    })
    monkeypatch.setattr(main, "process_exterior_photo", lambda content, path: path.write_bytes(content))

    def interpolate(input_dir, output_dir, **kwargs):
        assert input_dir == exterior_dir
        assert kwargs["target_frames"] == 36
        (output_dir / "spin_0001.jpg").write_bytes(b"clean spin")
        return 1

    processed = []
    monkeypatch.setattr(main, "interpolate_frames", interpolate)
    monkeypatch.setattr(
        main,
        "process_all_frames_background",
        lambda directory, mode, background, style: processed.append((directory, mode, background, style)),
    )
    monkeypatch.setattr(main, "generate_thumbnail", lambda *args: None)
    monkeypatch.setattr(main, "db_update_counts", lambda vin: None)

    assert main.generate_clean_frames(VIN)
    assert (exterior_dir / "frame_0001.jpg").read_bytes() == b"clean-0"
    assert (vehicle_dir / "spin" / "spin_0001.jpg").read_bytes() == b"clean spin"
    assert len(processed) == 1
    assert processed[0][0].name.startswith(".clean-spin-")
    assert processed[0][1:] == ("clean", None, "none")


def test_clean_frame_job_persists_error_status(monkeypatch, tmp_path) -> None:
    vehicle_dir = tmp_path / VIN
    vehicle_dir.mkdir()
    monkeypatch.setattr(main, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(main, "generate_clean_frames", lambda vin: False)

    main._run_clean_frames_job(VIN)

    status = json.loads((vehicle_dir / "clean-frame-status.json").read_text(encoding="utf-8"))
    assert status["status"] == "error"
    assert "source frames" in status["error"]
