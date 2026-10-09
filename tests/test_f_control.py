from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import f_control  # noqa: E402
import main  # noqa: E402

VIN = "1HGCM82633A004352"
OTHER_VIN = "2HGCM82633A004353"


def _database(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE vehicles (vin TEXT PRIMARY KEY, user_email TEXT, status TEXT DEFAULT 'active')"
        )
        conn.executemany(
            "INSERT INTO vehicles (vin,user_email) VALUES (?,?)",
            [(VIN, "dealer@example.test"), (OTHER_VIN, "other@example.test")],
        )


def test_clear_all_vehicles_uses_database_helper_and_preserves_other_tenants(
    monkeypatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "vehicles.sqlite3"
    storage = tmp_path / "storage"
    owned_dir = storage / VIN
    other_dir = storage / OTHER_VIN
    owned_dir.mkdir(parents=True)
    other_dir.mkdir()
    (owned_dir / "private.jpg").write_bytes(b"owned")
    (other_dir / "private.jpg").write_bytes(b"other")
    _database(db_path)

    monkeypatch.setattr(f_control, "DB_PATH", db_path)
    monkeypatch.setattr(f_control, "STORAGE_DIR", storage)
    monkeypatch.setattr(
        main, "_authenticated_user_email", lambda _request: "dealer@example.test"
    )
    monkeypatch.delenv("LOTVUE_OWNER_EMAIL", raising=False)

    result = f_control.clear_all_vehicles(object())

    assert result == {"status": "ok", "vehicles_removed": 1}
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT vin FROM vehicles").fetchall() == [(OTHER_VIN,)]
    assert not owned_dir.exists()
    assert (other_dir / "private.jpg").read_bytes() == b"other"


def test_clear_all_vehicles_unlinks_storage_symlink_without_following_it(
    monkeypatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "vehicles.sqlite3"
    storage = tmp_path / "storage"
    storage.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.jpg").write_bytes(b"keep")
    (storage / VIN).symlink_to(outside, target_is_directory=True)
    _database(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM vehicles WHERE vin=?", (OTHER_VIN,))
        conn.execute("UPDATE vehicles SET user_email='owner@example.test' WHERE vin=?", (VIN,))

    monkeypatch.setattr(f_control, "DB_PATH", db_path)
    monkeypatch.setattr(f_control, "STORAGE_DIR", storage)
    monkeypatch.setattr(
        main, "_authenticated_user_email", lambda _request: "owner@example.test"
    )
    monkeypatch.setenv("LOTVUE_OWNER_EMAIL", "owner@example.test")

    result = f_control.clear_all_vehicles(object())

    assert result == {"status": "ok", "vehicles_removed": 1}
    assert not (storage / VIN).exists()
    assert (outside / "private.jpg").read_bytes() == b"keep"
