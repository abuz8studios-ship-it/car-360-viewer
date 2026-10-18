from __future__ import annotations

import os
import sys
from pathlib import Path

import cv2
import numpy as np
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import main  # noqa: E402
import validate_video  # noqa: E402

VIN = "1HGCM82633A004352"


def _panorama(seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    noise = rng.integers(0, 256, (60, 400), dtype=np.uint8)
    pano = cv2.resize(noise, (3200, 480), interpolation=cv2.INTER_CUBIC)
    return cv2.cvtColor(pano, cv2.COLOR_GRAY2BGR)


def _write_capture(directory: Path, *, moving: bool, count: int = 30, discrete: bool = False) -> list[Path]:
    """Synthetic capture: textured background behind a centred 'car' whose
    silhouette widens/narrows. moving=False is the fixed-camera turntable."""
    pano = _panorama()
    files = []
    for index in range(count):
        if discrete:
            frame = _panorama(seed=100 + index)[60:420, 0:640].copy()
        else:
            x = index * 40 if moving else 0
            frame = pano[60:420, x:x + 640].copy()
        half_width = int(120 + 80 * abs(np.cos(index / count * 2 * np.pi)))
        cv2.rectangle(frame, (320 - half_width, 150), (320 + half_width, 260), (40, 40, 200), -1)
        path = directory / f"frame_{index:04d}.jpg"
        cv2.imwrite(str(path), frame)
        files.append(path)
    return files


def test_tool_resolution_uses_platform_executable_name(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("FFPROBE_PATH", raising=False)
    exe = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    fake = tmp_path / exe
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(validate_video, "BASE", tmp_path / "app")

    assert validate_video._resolve_tool("FFPROBE_PATH", "ffprobe") == str(fake)

    override = tmp_path / "custom-ffprobe"
    override.write_text("#!/bin/sh\n")
    monkeypatch.setenv("FFPROBE_PATH", str(override))
    assert validate_video._resolve_tool("FFPROBE_PATH", "ffprobe") == str(override)


def test_default_install_gate_passes_a_moving_camera_capture(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("LOTVUE_REMBG_MODEL", raising=False)
    monkeypatch.delenv("LOTVUE_REMBG_LICENSE_ACCEPTED", raising=False)
    _write_capture(tmp_path, moving=True)

    result = validate_video.validate_dir(tmp_path)

    assert result["verdict"] == "PASS", result["reasons"]
    checks = result["checks"]
    assert checks["segmentation"] == "unavailable"
    assert checks["not_evaluated"] == ["car_present", "cropping", "angle_coverage"]
    assert checks["present_ratio"] is None and checks["aspect_range"] is None
    assert checks["bg_motion"] >= validate_video.BG_MOTION_MIN
    assert checks["weak_pair_ratio"] <= validate_video.WEAK_PAIR_RATIO_MAX


def test_default_install_gate_rejects_turntable_and_discrete_spins(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("LOTVUE_REMBG_MODEL", raising=False)
    turntable = tmp_path / "turntable"
    discrete = tmp_path / "discrete"
    turntable.mkdir()
    discrete.mkdir()
    _write_capture(turntable, moving=False)
    _write_capture(discrete, moving=True, discrete=True)

    static_result = validate_video.validate_dir(turntable)
    discrete_result = validate_video.validate_dir(discrete)

    assert static_result["verdict"] == "FAIL"
    assert any("turntable signature" in reason for reason in static_result["reasons"])
    assert discrete_result["verdict"] == "FAIL"
    assert any("continuous walkaround" in reason for reason in discrete_result["reasons"])


def test_gate_uses_the_approved_model_matte_when_configured(tmp_path, monkeypatch) -> None:
    _write_capture(tmp_path, moving=True)

    def centred_matte(image):
        mask = np.zeros((image.height, image.width), dtype=bool)
        mask[image.height // 3: image.height * 3 // 4, image.width // 4: image.width * 3 // 4] = True
        return mask

    monkeypatch.setattr(validate_video, "_model_masker", lambda: centred_matte)
    result = validate_video.validate_dir(tmp_path)

    assert result["checks"]["segmentation"] == "model"
    assert "not_evaluated" not in result["checks"]
    assert result["checks"]["present_ratio"] == 1.0
    assert result["checks"]["crop_ratio"] == 0.0


def test_gate_falls_back_when_the_configured_model_cannot_run(tmp_path, monkeypatch) -> None:
    _write_capture(tmp_path, moving=True)

    def broken_matte(_image):
        raise RuntimeError("weights could not be loaded")

    monkeypatch.setattr(validate_video, "_model_masker", lambda: broken_matte)
    result = validate_video.validate_dir(tmp_path)

    assert result["verdict"] == "PASS", result["reasons"]
    assert result["checks"]["segmentation"] == "model_failed"
    assert result["checks"]["not_evaluated"] == ["car_present", "cropping", "angle_coverage"]


def _upload_client(tmp_path, monkeypatch) -> tuple[TestClient, dict]:
    import a2_extensions

    storage = tmp_path / "storage"
    storage.mkdir()
    db_path = tmp_path / "gate.sqlite3"
    monkeypatch.setattr(main, "STORAGE_DIR", storage)
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(a2_extensions, "STORAGE_DIR", storage)
    monkeypatch.setattr(a2_extensions, "DB_PATH", db_path)
    monkeypatch.setenv("LOTVUE_JWT_SECRET", "capture-gate-test-secret-" + "x" * 40)
    monkeypatch.delenv("LOTVUE_OWNER_EMAIL", raising=False)
    main.init_db()
    main.db_create_user("dealer@example.test")
    token = a2_extensions._make_tokens("dealer@example.test")["access_token"]
    return TestClient(main.app), {"Authorization": f"Bearer {token}"}


def test_media_reupload_without_details_keeps_vehicle_details(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "upsert.sqlite3")
    main.init_db()
    main.db_upsert_vehicle(VIN, "2019", "Honda", "Accord", 0, 0, user_email="dealer@example.test")

    main.db_upsert_vehicle(VIN, "", "", "", 36, 0, user_email="dealer@example.test")

    vehicle = main.db_get_vehicle(VIN)
    assert (vehicle["year"], vehicle["make"], vehicle["model"]) == ("2019", "Honda", "Accord")
    assert vehicle["exterior_frames"] == 36


def test_env_file_override_appended_later_takes_effect(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# template\nLOTVUE_GOOGLE_STORE_CODE=\nLOTVUE_DEALER_NAME=\n"
        "LOTVUE_GOOGLE_STORE_CODE=STORE-1\nLOTVUE_DEALER_NAME=\"Stranger Motors\"\nnot a setting\n",
        encoding="utf-8",
    )

    values = main._read_env_values(env_file)

    assert values["LOTVUE_GOOGLE_STORE_CODE"] == "STORE-1"
    assert values["LOTVUE_DEALER_NAME"] == "Stranger Motors"
    assert "not a setting" not in values


def test_capture_page_collects_vehicle_details() -> None:
    html = main.capture_page().body.decode()
    for field in ("vinField", "yearField", "makeField", "modelField"):
        assert f'id="{field}"' in html
    assert "fd.append(k, details[k])" in html


def test_unreadable_video_is_rejected_with_a_clear_reason(tmp_path, monkeypatch) -> None:
    client, headers = _upload_client(tmp_path, monkeypatch)

    def failing_gate(_path):
        raise FileNotFoundError("[Errno 2] No such file or directory: '/secret/ffprobe'")

    monkeypatch.setattr(validate_video, "validate_video_file", failing_gate)
    response = client.post(
        "/api/upload", headers=headers, data={"vin": VIN},
        files={"video": ("walkaround.mp4", b"not really a video", "video/mp4")},
    )

    assert response.status_code == 422
    gate = response.json()["detail"]["gate"]
    assert gate["verdict"] == "FAIL"
    assert "could not be read" in gate["reasons"][0]
    assert "/secret" not in response.text and "Errno" not in response.text


def test_photo_gate_errors_return_a_structured_rejection(tmp_path, monkeypatch) -> None:
    client, headers = _upload_client(tmp_path, monkeypatch)
    photos_dir = tmp_path / "photos"
    photos_dir.mkdir()
    files = _write_capture(photos_dir, moving=True, count=4)
    for path in files:  # uploads below 800px are refused before the gate runs
        cv2.imwrite(str(path), cv2.resize(cv2.imread(str(path)), (1280, 720)))

    def failing_gate(_path):
        raise RuntimeError("decoder crashed")

    monkeypatch.setattr(validate_video, "validate_dir", failing_gate)
    response = client.post(
        "/api/upload", headers=headers, data={"vin": VIN},
        files=[("photos", (p.name, p.read_bytes(), "image/jpeg")) for p in files],
    )

    assert response.status_code == 422
    assert "could not be analyzed" in response.json()["detail"]["gate"]["reasons"][0]
    assert "decoder crashed" not in response.text


def test_gate_summary_never_returns_server_paths(tmp_path) -> None:
    _write_capture(tmp_path, moving=True)

    summary = validate_video.gate_summary(validate_video.validate_dir(tmp_path))

    assert "source" not in summary
    assert str(tmp_path) not in str(summary)
