from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
sys.path.insert(0, str(APP_DIR))

import ai_studio.lead_engine as lead_engine  # noqa: E402

TENANT = "dealer@example.test"
OTHER_TENANT = "other@example.test"
OWNER = "platform@example.test"
VIN = "1HGCM82633A004352"
OTHER_VIN = "2HGCM82633A004353"


def _future_slot(hour: int, minute: int = 0) -> str:
    day = datetime.now().date() + timedelta(days=1)
    while day.weekday() == 6:  # The default Sunday opening time is 11:00 a.m.
        day += timedelta(days=1)
    return datetime(day.year, day.month, day.day, hour, minute).isoformat(timespec="minutes")


def _prepare_db(path: Path, monkeypatch) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE vehicles (vin TEXT PRIMARY KEY, user_email TEXT, status TEXT)"
        )
        conn.executemany(
            "INSERT INTO vehicles VALUES (?, ?, 'active')",
            [(VIN, TENANT), (OTHER_VIN, OTHER_TENANT)],
        )
        conn.execute(
            "CREATE TABLE leads (id INTEGER PRIMARY KEY, vin TEXT, status TEXT, name TEXT)"
        )
        conn.executemany(
            "INSERT INTO leads VALUES (?, ?, 'new', ?)",
            [(1, VIN, "Buyer One"), (2, OTHER_VIN, "Buyer Two")],
        )
    monkeypatch.setattr(lead_engine, "DB_PATH", path)


def test_booking_is_tenant_scoped_and_owner_uses_lead_tenant(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "appointments.sqlite3"
    _prepare_db(db_path, monkeypatch)
    monkeypatch.setenv("LOTVUE_OWNER_EMAIL", OWNER)
    slot = _future_slot(10, 0)

    first = lead_engine.book_appointment(1, slot, user_email=TENANT)
    assert first["status"] == "confirmed"

    duplicate = lead_engine.book_appointment(1, slot, user_email=TENANT)
    assert duplicate["error"] == "That time slot is already booked"

    # A different dealership has a separate calendar, even at the same clock time.
    other_dealer = lead_engine.book_appointment(2, slot, user_email=OTHER_TENANT)
    assert other_dealer["status"] == "confirmed"

    # The platform owner can manage the lead, but the reservation still occupies
    # the vehicle dealer's calendar rather than the owner's separate account.
    owner_booking = lead_engine.book_appointment(1, _future_slot(10, 30), user_email=OWNER)
    assert owner_booking["status"] == "confirmed"

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 3


def test_booking_rejects_wrong_tenant_and_out_of_hours_slots(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "appointments.sqlite3"
    _prepare_db(db_path, monkeypatch)
    slot = _future_slot(10, 0)

    denied = lead_engine.book_appointment(1, slot, user_email=OTHER_TENANT)
    assert denied["error"] == "Lead not found"

    closed = lead_engine.book_appointment(1, _future_slot(8, 0), user_email=TENANT)
    assert "appointment hours" in closed["error"]

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='appointments'").fetchone()[0] == 0
