"""HTTP-level coverage for POST /api/appointments/book.

The engine function was already unit-tested, but the route itself was not: a
double-escaped control-character regex made every request raise ``re.error``
and return HTTP 500. These tests drive the real route through the auth gate.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
sys.path.insert(0, str(APP_DIR))

import a2_extensions  # noqa: E402
import ai_studio.lead_engine as lead_engine  # noqa: E402
import main  # noqa: E402
from a2_extensions import _make_tokens  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

TENANT = "dealer@example.test"
VIN = "1HGCM82633A004352"


def _future_slot(hour: int = 10) -> str:
    day = datetime.now().date() + timedelta(days=1)
    while day.weekday() == 6:  # The default Sunday opening time is 11:00 a.m.
        day += timedelta(days=1)
    return datetime(day.year, day.month, day.day, hour, 0).isoformat(timespec="minutes")


def _client(monkeypatch, tmp_path: Path) -> TestClient:
    db_path = tmp_path / "appointments.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE users (email TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO users VALUES (?)", (TENANT,))
        conn.execute("CREATE TABLE vehicles (vin TEXT PRIMARY KEY, user_email TEXT, status TEXT)")
        conn.execute("INSERT INTO vehicles VALUES (?, ?, 'active')", (VIN, TENANT))
        conn.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY, vin TEXT, status TEXT, name TEXT)")
        conn.execute("INSERT INTO leads VALUES (1, ?, 'new', 'Buyer One')", (VIN,))
    # The auth gate, the route helpers, and the engine each read their own module copy.
    monkeypatch.setattr(a2_extensions, "DB_PATH", db_path)
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(lead_engine, "DB_PATH", db_path)
    monkeypatch.setenv("LOTVUE_JWT_SECRET", "appointment-route-test-secret-32-bytes")
    monkeypatch.delenv("LOTVUE_OWNER_EMAIL", raising=False)
    return TestClient(main.app)


def _auth() -> dict:
    return {"Authorization": f"Bearer {_make_tokens(TENANT)['access_token']}"}


def test_booking_route_sanitizes_notes_and_confirms(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    raw_notes = "Customer\x00 wants\x07 a\ttest\ndrive\x1f soon\x7f!"

    response = client.post(
        "/api/appointments/book",
        json={"lead_id": 1, "datetime": _future_slot(), "type": "test_drive", "notes": raw_notes},
        headers=_auth(),
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok"

    stored = sqlite3.connect(tmp_path / "appointments.sqlite3").execute(
        "SELECT notes FROM appointments WHERE lead_id=1"
    ).fetchone()[0]
    # C0 controls and DEL are stripped; TAB and LF survive as ordinary whitespace.
    assert stored == "Customer wants a\ttest\ndrive soon!"
    assert not any(ord(ch) < 9 or ord(ch) == 127 for ch in stored)


def test_booking_route_rejects_missing_fields_with_400_not_500(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)

    empty = client.post("/api/appointments/book", json={}, headers=_auth())
    assert empty.status_code == 400
    assert empty.json()["detail"] == "lead_id and datetime required"

    no_body = client.post("/api/appointments/book", headers=_auth())
    assert no_body.status_code == 400


def test_booking_route_rejects_malformed_input_with_400(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    headers = {**_auth(), "Content-Type": "application/json"}

    bad_json = client.post("/api/appointments/book", content=b"{not json", headers=headers)
    assert bad_json.status_code == 400
    assert bad_json.json()["detail"] == "Invalid JSON body"

    bad_type = client.post(
        "/api/appointments/book",
        json={"lead_id": 1, "datetime": _future_slot(), "type": "launch_rocket"},
        headers=_auth(),
    )
    assert bad_type.status_code == 400
    assert bad_type.json()["detail"] == "Unsupported appointment type"

    bad_lead = client.post(
        "/api/appointments/book",
        json={"lead_id": "not-a-number", "datetime": _future_slot()},
        headers=_auth(),
    )
    assert bad_lead.status_code == 400
    assert bad_lead.json()["detail"] == "Valid lead_id required"


def test_booking_route_requires_authentication(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)

    anonymous = client.post(
        "/api/appointments/book",
        json={"lead_id": 1, "datetime": _future_slot()},
    )
    assert anonymous.status_code == 401
