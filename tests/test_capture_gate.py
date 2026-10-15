from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
sys.path.insert(0, str(APP_DIR))

import studio  # noqa: E402
import validate_video  # noqa: E402


def _write_frames(directory: Path, count: int, luma: int) -> None:
    rng = np.random.default_rng(7)
    for i in range(count):
        noise = rng.integers(-20, 20, size=(120, 160, 3))
        pixels = np.clip(luma + noise, 0, 255).astype(np.uint8)
        Image.fromarray(pixels).save(directory / f"frame_{i + 1:04d}.jpg")


def _no_model(monkeypatch) -> None:
    monkeypatch.delenv("LOTVUE_REMBG_MODEL", raising=False)
    monkeypatch.delenv("LOTVUE_REMBG_LICENSE_ACCEPTED", raising=False)

    def fail(_image):
        raise AssertionError("matting must not run without a configured model")

    monkeypatch.setattr(studio, "cut_car", fail)


def test_gate_answers_without_background_removal_model(tmp_path, monkeypatch) -> None:
    _no_model(monkeypatch)
    _write_frames(tmp_path, 6, luma=128)

    result = validate_video.validate_dir(tmp_path)

    assert result["verdict"] == "PASS"
    assert result["checks"]["subject_checks"].startswith("skipped")
    assert "bg_motion" not in result["checks"]
    assert result["splat_eligible"] is False


def test_gate_still_rejects_bad_exposure_without_model(tmp_path, monkeypatch) -> None:
    _no_model(monkeypatch)
    _write_frames(tmp_path, 6, luma=5)

    result = validate_video.validate_dir(tmp_path)

    assert result["verdict"] == "FAIL"
    assert any("too dark" in reason for reason in result["reasons"])


def test_gate_runs_subject_checks_when_model_enabled(tmp_path, monkeypatch) -> None:
    _write_frames(tmp_path, 6, luma=128)

    def matte(image):
        alpha = Image.new("L", image.size, 0)
        w, h = image.size
        alpha.paste(255, (w // 4, h // 3, 3 * w // 4, 5 * h // 6))
        rgba = image.convert("RGBA")
        rgba.putalpha(alpha)
        return rgba

    monkeypatch.setattr(validate_video, "_segmentation_enabled", lambda: True)
    monkeypatch.setattr(studio, "cut_car", matte)

    result = validate_video.validate_dir(tmp_path)

    assert "subject_checks" not in result["checks"]
    assert result["checks"]["present_ratio"] == 1.0
    assert "bg_motion" in result["checks"]


def test_gate_summary_never_returns_server_paths(tmp_path, monkeypatch) -> None:
    _no_model(monkeypatch)
    _write_frames(tmp_path, 6, luma=128)

    summary = validate_video.gate_summary(validate_video.validate_dir(tmp_path))

    assert "source" not in summary
    assert str(tmp_path) not in str(summary)
