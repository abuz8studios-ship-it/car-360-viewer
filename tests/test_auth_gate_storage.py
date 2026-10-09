from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.auth_gate import (
    _is_public,
    _is_public_storage,
    _owns_private_storage,
    _owns_vehicle,
    _private_vehicle_scope,
    _storage_path_is_safe,
)


def test_published_vehicle_videos_are_publicly_readable() -> None:
    assert _is_public_storage("/storage/1HGCM82633A004352/video/clip.mp4")
    assert _is_public_storage("/storage/1HGCM82633A004352/videos/clip.m4v")
    assert _is_public("GET", "/storage/1HGCM82633A004352/walkaround/clip.webm")


def test_brand_image_assets_are_public_but_constrained() -> None:
    assert _is_public_storage("/storage/_brand/logo.png")
    assert _is_public_storage("/storage/_brand/background.jpg")
    assert not _is_public_storage("/storage/_brand/private.txt")
    assert not _is_public_storage("/storage/_brand/nested/logo.png")


def test_storage_allowlist_rejects_private_or_traversal_paths() -> None:
    assert not _is_public_storage("/storage/1HGCM82633A004352/raw/frame_0001.jpg")
    assert not _is_public_storage("/storage/1HGCM82633A004352/.video-secret.mp4")
    assert not _is_public_storage("/storage/1HGCM82633A004352/video/../private.mp4")
    assert not _is_public_storage("/storage/1HGCM82633A004352/video/%2e%2e/private.mp4")


def test_marketplace_and_social_vehicle_operations_are_tenant_scoped() -> None:
    vin = "1HGCM82633A004352"
    assert _private_vehicle_scope(f"/api/marketplace/craigslist/{vin}/pack") == vin
    assert _private_vehicle_scope(f"/api/marketplace/ebay/{vin}/publish") == vin
    assert _private_vehicle_scope(f"/api/social/postiz/{vin}/post") == vin
    assert _private_vehicle_scope("/api/marketplace/ebay/status") is None
    assert _private_vehicle_scope("/api/marketplace/facebook/catalog.csv") is None
    assert _private_vehicle_scope("/api/social/postiz/status") is None


class _StaticJWT:
    @staticmethod
    def decode(_token, _secret, algorithms):
        assert algorithms == ["test"]
        return {"sub": "owner@example.test", "type": "access"}


def test_platform_owner_still_needs_an_existing_vehicle_for_private_paths(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "vehicles.sqlite3"
    vin = "1HGCM82633A004352"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE vehicles (vin TEXT PRIMARY KEY, user_email TEXT, status TEXT)")
        conn.execute("INSERT INTO vehicles VALUES (?, ?, 'active')", (vin, "dealer@example.test"))
    monkeypatch.setenv("LOTVUE_OWNER_EMAIL", "owner@example.test")

    assert _owns_vehicle("token", vin, str(db_path), _StaticJWT, "secret", "test")
    assert not _owns_vehicle("token", "2HGCM82633A004353", str(db_path), _StaticJWT, "secret", "test")
    assert _owns_private_storage(
        f"/storage/{vin}/raw/capture.bin", "token", str(db_path), _StaticJWT, "secret", "test"
    )
    assert not _owns_private_storage(
        "/storage/2HGCM82633A004353/raw/capture.bin",
        "token", str(db_path), _StaticJWT, "secret", "test",
    )


def test_storage_file_serving_rejects_symlinks_and_path_traversal(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    video_dir = root / "1HGCM82633A004352" / "video"
    video_dir.mkdir(parents=True)
    regular = video_dir / "clip.mp4"
    regular.write_bytes(b"video")
    outside = tmp_path / "private.mp4"
    outside.write_bytes(b"private")
    (video_dir / "linked.mp4").symlink_to(outside)

    assert _storage_path_is_safe(
        "/storage/1HGCM82633A004352/video/clip.mp4", root
    )
    assert not _storage_path_is_safe(
        "/storage/1HGCM82633A004352/video/linked.mp4", root
    )
    assert not _storage_path_is_safe(
        "/storage/1HGCM82633A004352/video/../private.mp4", root
    )
