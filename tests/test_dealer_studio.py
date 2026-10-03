from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
sys.path.insert(0, str(APP_DIR))

import main  # noqa: E402
import studio_agent  # noqa: E402
from ai_studio import photo_processor  # noqa: E402
from ai_studio.background_studio import PREMIUM_SCENES, list_scenes  # noqa: E402
from app.auth_gate import _private_vehicle_scope  # noqa: E402
from background_catalog import STATIC_BACKGROUND_DIR, list_background_assets  # noqa: E402
from a2_extensions import H, W, _render_bg  # noqa: E402

VIN = "1HGCM82633A004352"


def _vehicle(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    storage = tmp_path / "storage"
    vehicle_dir = storage / VIN
    exterior = vehicle_dir / "exterior"
    exterior.mkdir(parents=True)
    db_path = tmp_path / "vehicles.sqlite3"
    monkeypatch.setattr(main, "STORAGE_DIR", storage)
    monkeypatch.setattr(main, "DB_PATH", db_path)
    main.init_db()
    main.db_upsert_vehicle(VIN, "2022", "Test", "Vehicle", user_email="dealer@example.test")
    return vehicle_dir, exterior


def _make_frames(exterior: Path, count: int = 4) -> None:
    for index in range(count):
        image = Image.new("RGB", (96, 64), (35 + index * 8, 50, 70))
        draw = ImageDraw.Draw(image)
        draw.rectangle((14 + index, 25, 78 + index, 50), fill=(190, 40 + index * 8, 35))
        image.save(exterior / f"frame_{index + 1:04d}.jpg", quality=85)
        image.close()


def test_procedural_background_gradient_preserves_endpoints() -> None:
    image = _render_bg((10, 20, 30), (210, 220, 230), "plain")
    try:
        assert image.size == (W, H)
        assert image.getpixel((0, 0)) == (10, 20, 30)
        assert image.getpixel((W - 1, H - 1)) == (210, 220, 230)
    finally:
        image.close()


def test_packaged_backgrounds_are_one_real_shared_catalog() -> None:
    assets = list_background_assets()
    disk_assets = sorted(STATIC_BACKGROUND_DIR.glob("*.jpg"))
    assert assets
    assert len(assets) == len(disk_assets)
    for asset in assets:
        assert (ROOT / "app" / "static" / asset["url"].removeprefix("/static/")).is_file()
        assert (ROOT / "app" / "static" / asset["thumbnail_url"].removeprefix("/static/")).is_file()

    scenes = list_scenes()
    assert set(scene["id"] for scene in scenes) == set(PREMIUM_SCENES)
    assert all(scene["renderer"] == "local_image_composite" for scene in scenes)
    assert all(scene["ai_generated"] is False for scene in scenes)
    assert all(scene["has_preview"] for scene in scenes)


def test_photo_processor_never_claims_background_swap_without_a_cutout(tmp_path, monkeypatch) -> None:
    source = tmp_path / "input.jpg"
    output = tmp_path / "studio.jpg"
    Image.new("RGB", (320, 180), (120, 140, 160)).save(source)

    def no_cutout(image):
        return image.convert("RGBA"), None

    monkeypatch.setattr(photo_processor, "remove_background", no_cutout)
    monkeypatch.setattr(photo_processor, "BACKGROUNDS_DIR", tmp_path / "missing-backgrounds")
    result = photo_processor.PhotoProcessor().process(str(source), str(output))

    assert result["status"] == "degraded"
    assert "bg_removal:unavailable_or_no_alpha" in result["steps"]
    assert "bg_swap:skipped_without_cutout" in result["steps"]
    assert output.is_file()
    with Image.open(output) as processed:
        assert processed.size == (320, 180)


def test_photo_processor_honors_keep_background_selection_without_cutout(tmp_path, monkeypatch) -> None:
    source = tmp_path / "input.jpg"
    output = tmp_path / "studio.jpg"
    Image.new("RGB", (320, 180), (120, 140, 160)).save(source)

    def should_not_remove(_image):
        raise AssertionError("keep-background selection must not call cut-out")

    monkeypatch.setattr(photo_processor, "remove_background", should_not_remove)
    result = photo_processor.PhotoProcessor(background_name="none").process(
        str(source), str(output)
    )

    assert output.is_file()
    assert result["steps"][:2] == [
        "bg_removal:skipped_keep_background", "bg_swap:skipped_keep_background",
    ]
    assert not any(step.startswith("bg_removal:error") for step in result["steps"])


def test_360_render_uses_truthful_natural_fallback_and_commits_36_frames(tmp_path, monkeypatch) -> None:
    vehicle_dir, exterior = _vehicle(tmp_path, monkeypatch)
    _make_frames(exterior)
    monkeypatch.setattr(main, "REMBG_AVAILABLE", False)
    monkeypatch.setenv("LOTVUE_RENDER_MODE", "studio")

    main._run_vehicle_spin_job(VIN)

    spin_dir = vehicle_dir / "spin"
    spin_frames = sorted(spin_dir.glob("spin_*.jpg"))
    assert len(spin_frames) == 36
    manifest = json.loads((spin_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["render_mode"] == "natural"
    assert manifest["render_status"] == "degraded"
    assert manifest["background_removed"] is False
    assert (vehicle_dir / "thumbnail.jpg").is_file()
    job = next(item for item in main._read_pipeline_jobs(VIN) if item["kind"] == "spin")
    assert job["status"] == "degraded"
    assert "photographed backgrounds were retained" in job["message"]
    assert not list(vehicle_dir.glob(".spin-stage-*"))


def test_failed_360_rebuild_preserves_previous_viewer_output(tmp_path, monkeypatch) -> None:
    vehicle_dir, exterior = _vehicle(tmp_path, monkeypatch)
    _make_frames(exterior)
    spin_dir = vehicle_dir / "spin"
    spin_dir.mkdir()
    old_frame = spin_dir / "spin_0001.jpg"
    old_frame.write_bytes(b"previous-good-frame")
    monkeypatch.setattr(main, "interpolate_frames", lambda *args, **kwargs: 0)

    main._run_vehicle_spin_job(VIN)

    assert old_frame.read_bytes() == b"previous-good-frame"
    job = next(item for item in main._read_pipeline_jobs(VIN) if item["kind"] == "spin")
    assert job["status"] == "error"
    assert "viewer output" in job["message"].lower()


def test_strict_background_replacement_fails_closed_when_rembg_is_missing(tmp_path, monkeypatch) -> None:
    _vehicle(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "REMBG_AVAILABLE", False)

    try:
        main._queue_photo_studio_job(
            VIN, background="studio_white", kind="background_studio",
            max_photos=20, require_rembg=True,
        )
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "no photos were changed" in exc.detail
    else:
        raise AssertionError("strict scene replacement must not report success without rembg")


def test_public_viewer_page_returns_at_most_120_frames(tmp_path, monkeypatch) -> None:
    vehicle_dir, _ = _vehicle(tmp_path, monkeypatch)
    spin_dir = vehicle_dir / "spin"
    spin_dir.mkdir()
    job_id = "viewer-frame-cap-test"
    main.db_upsert_vehicle(
        VIN, "2022", "Test", "Vehicle", user_email="dealer@example.test", job_id=job_id
    )
    for index in range(1, 131):
        (spin_dir / f"spin_{index:04d}.jpg").write_bytes(b"frame")

    response = TestClient(main.app).get(f"/viewer/{job_id}")

    assert response.status_code == 200
    encoded_urls = response.text.split("const FRAME_URLS = ", 1)[1].split(";", 1)[0]
    urls = json.loads(encoded_urls)
    assert len(urls) == 120
    assert urls[0].endswith("spin_0001.jpg")
    assert urls[-1].endswith("spin_0120.jpg")


def test_authenticated_studio_page_and_vehicle_plan_enforce_tenancy(tmp_path, monkeypatch) -> None:
    import a2_extensions

    storage = tmp_path / "storage"
    (storage / VIN / "exterior").mkdir(parents=True)
    db_path = tmp_path / "studio-auth.sqlite3"
    monkeypatch.setattr(main, "STORAGE_DIR", storage)
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(a2_extensions, "STORAGE_DIR", storage)
    monkeypatch.setattr(a2_extensions, "DB_PATH", db_path)
    monkeypatch.setenv("LOTVUE_JWT_SECRET", "dealer-studio-test-secret-" + "x" * 40)
    monkeypatch.delenv("LOTVUE_OWNER_EMAIL", raising=False)
    main.init_db()
    main.db_create_user("dealer@example.test")
    main.db_create_user("other@example.test")
    main.db_upsert_vehicle(
        VIN, "2022", "Test", "Vehicle", user_email="dealer@example.test"
    )
    client = TestClient(main.app)
    owner_token = a2_extensions._make_tokens("dealer@example.test")["access_token"]
    other_token = a2_extensions._make_tokens("other@example.test")["access_token"]

    assert client.get(f"/api/studio/vehicles/{VIN}/plan").status_code == 401
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    other_headers = {"Authorization": f"Bearer {other_token}"}
    assert client.get("/studio", headers=owner_headers).status_code == 200
    assert client.get(f"/api/studio/vehicles/{VIN}/plan", headers=owner_headers).status_code == 200
    assert client.get(f"/api/studio/vehicles/{VIN}/plan", headers=other_headers).status_code == 404


def test_google_feed_readiness_matches_static_route_requirements(monkeypatch) -> None:
    required = {
        "LOTVUE_GOOGLE_FEED_TOKEN": "feed-secret-" + "x" * 40,
        "LOTVUE_GOOGLE_FEED_USER_EMAIL": "dealer@example.test",
        "LOTVUE_GOOGLE_STORE_CODE": "store-1",
        "LOTVUE_DEALER_NAME": "Example Motors",
        "LOTVUE_DEALER_ADDR1": "1 Main Street",
        "LOTVUE_DEALER_CITY": "Dayton",
        "LOTVUE_DEALER_REGION": "OH",
        "LOTVUE_DEALER_POSTAL": "45402",
        "LOTVUE_PUBLIC_URL": "https://dealer.example",
    }
    for key in required:
        monkeypatch.delenv(key, raising=False)
    assert studio_agent._google_feed_configuration_ready() is False

    for key, value in required.items():
        monkeypatch.setenv(key, value)
    assert studio_agent._google_feed_configuration_ready() is True
    monkeypatch.setenv("LOTVUE_PUBLIC_URL", "https://localhost")
    assert studio_agent._google_feed_configuration_ready() is False
    monkeypatch.setenv("LOTVUE_PUBLIC_URL", "https://dealer.example")
    monkeypatch.setenv("LOTVUE_GOOGLE_FEED_TOKEN", "x" * 513)
    assert studio_agent._google_feed_configuration_ready() is False


def test_studio_vehicle_routes_are_tenant_scoped_and_health_is_non_probing(monkeypatch) -> None:
    import rembg_runtime

    monkeypatch.setattr(main, "REMBG_AVAILABLE", False)
    monkeypatch.setattr(rembg_runtime, "package_available", lambda: True)
    monkeypatch.delenv("LOTVUE_REMBG_MODEL", raising=False)
    monkeypatch.delenv("LOTVUE_REMBG_LICENSE_ACCEPTED", raising=False)
    for suffix in ("plan", "jobs", "run"):
        path = f"/api/studio/vehicles/{VIN}/{suffix}"
        assert _private_vehicle_scope(path) == VIN

    payload = studio_agent.build_system_health(main)
    assert payload["external_services_contacted"] is False
    assert payload["probe_mode"] == "local_configuration_only"
    assert payload["limits"]["spin_frames"] == 36
    assert payload["limits"]["viewer_frames"] == 120
    assert payload["limits"]["vehicle_media_entries"] == 300
    assert payload["subsystems"]["billing"]["status"] == "owner_action_required"
    assert payload["subsystems"]["background_removal"]["status"] == "setup_required"
    assert "no model is selected" in payload["subsystems"]["background_removal"]["detail"]
    assert "pricing_approved" in payload["runtime"]
