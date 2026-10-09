from __future__ import annotations

import sqlite3
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
sys.path.insert(0, str(APP_DIR))

import b_distribution as distribution  # noqa: E402
from a2_extensions import _make_tokens  # noqa: E402

TENANT = "dealer@example.test"
OTHER = "other@example.test"
VIN_OWNED_BY_OTHER = "2HGCM82633A004352"
VIN_NEW = "1HGCM82633A004352"


def _make_db(path: Path) -> None:
    columns = ", ".join(f"{name} TEXT DEFAULT ''" for name in distribution._IMPORTABLE)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE users (email TEXT PRIMARY KEY)")
        conn.execute(
            f"CREATE TABLE vehicles (vin TEXT PRIMARY KEY, created_at TEXT, "
            f"user_email TEXT DEFAULT '', status TEXT DEFAULT 'active', {columns})"
        )
        conn.executemany("INSERT INTO users (email) VALUES (?)", [(TENANT,), (OTHER,)])
        conn.execute(
            "INSERT INTO vehicles (vin, user_email, make, status) VALUES (?, ?, ?, 'active')",
            (VIN_OWNED_BY_OTHER, OTHER, "Protected Make"),
        )


def _request_with_token(token: str) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"authorization", f"Bearer {token}".encode("ascii"))],
    })


def test_actor_email_validates_access_token_and_existing_user(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "distribution.sqlite3"
    _make_db(db_path)
    monkeypatch.setattr(distribution, "DB_PATH", db_path)
    monkeypatch.setenv("LOTVUE_JWT_SECRET", "test-secret-for-distribution-auth")

    token = _make_tokens(f" {TENANT.upper()} ")["access_token"]
    assert distribution._actor_email(_request_with_token(token)) == TENANT

    with pytest.raises(HTTPException) as exc:
        distribution._actor_email(_request_with_token("not-a-jwt"))
    assert exc.value.status_code == 401

    deleted_user_token = _make_tokens("deleted@example.test")["access_token"]
    with pytest.raises(HTTPException) as exc:
        distribution._actor_email(_request_with_token(deleted_user_token))
    assert exc.value.status_code == 401


def test_import_rows_cannot_update_another_tenant(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "distribution.sqlite3"
    _make_db(db_path)
    monkeypatch.setattr(distribution, "DB_PATH", db_path)
    monkeypatch.delenv("LOTVUE_OWNER_EMAIL", raising=False)

    result = distribution._import_rows(
        [
            {"vin": VIN_OWNED_BY_OTHER, "make": "Changed Make"},
            {"vin": VIN_NEW, "make": "Honda", "model": "Accord"},
        ],
        source="test",
        user_email=TENANT,
    )

    assert result["imported"] == 1
    assert result["updated"] == 0
    assert result["errors"] == [
        {"row": 1, "vin": VIN_OWNED_BY_OTHER, "error": "vehicle belongs to another tenant"}
    ]
    with sqlite3.connect(db_path) as conn:
        protected = conn.execute(
            "SELECT make, user_email FROM vehicles WHERE vin=?", (VIN_OWNED_BY_OTHER,)
        ).fetchone()
        imported = conn.execute(
            "SELECT make, user_email FROM vehicles WHERE vin=?", (VIN_NEW,)
        ).fetchone()
    assert protected == ("Protected Make", OTHER)
    assert imported == ("Honda", TENANT)


def test_csv_import_rejects_short_or_disallowed_vins(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "distribution.sqlite3"
    _make_db(db_path)
    monkeypatch.setattr(distribution, "DB_PATH", db_path)
    result = distribution._import_rows(
        [{"vin": "ABC123"}, {"vin": "1HGCM82633A004352", "make": "Honda"}],
        source="test",
        user_email=TENANT,
    )
    assert result["imported"] == 1
    assert result["errors"][0]["error"] == "invalid VIN"


def test_google_feed_requires_a_secret_and_single_tenant(monkeypatch) -> None:
    secret = "feed-secret-that-is-long-enough-to-be-random-123"
    monkeypatch.setenv("LOTVUE_GOOGLE_FEED_TOKEN", secret)
    monkeypatch.setenv("LOTVUE_GOOGLE_FEED_USER_EMAIL", TENANT)

    assert distribution._google_feed_email(secret) == TENANT
    with pytest.raises(HTTPException) as exc:
        distribution._google_feed_email("wrong")
    assert exc.value.status_code == 404

    monkeypatch.delenv("LOTVUE_GOOGLE_FEED_USER_EMAIL")
    with pytest.raises(HTTPException) as exc:
        distribution._google_feed_email(secret)
    assert exc.value.status_code == 503


def test_google_feed_is_https_and_isolated_to_configured_tenant(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "feed.sqlite3"
    storage = tmp_path / "storage"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE vehicles (vin TEXT PRIMARY KEY, user_email TEXT, status TEXT, "
            "exterior_frames INTEGER, year TEXT, make TEXT, model TEXT, trim_level TEXT, "
            "price TEXT, mileage TEXT, color TEXT)"
        )
        conn.executemany(
            "INSERT INTO vehicles VALUES (?, ?, 'active', 1, '2025', ?, 'Accord', 'EX', '$27995', '12000', 'Blue')",
            [(VIN_NEW, TENANT, "<Honda & Sons>"), (VIN_OWNED_BY_OTHER, OTHER, "Other Make")],
        )
    for vin in (VIN_NEW, VIN_OWNED_BY_OTHER):
        photo = storage / vin / "spin" / "spin_0001.jpg"
        photo.parent.mkdir(parents=True)
        photo.write_bytes(b"fixture")

    monkeypatch.setattr(distribution, "DB_PATH", db_path)
    monkeypatch.setattr(distribution, "STORAGE_DIR", storage)
    monkeypatch.setenv("LOTVUE_GOOGLE_FEED_TOKEN", "g" * 48)
    monkeypatch.setenv("LOTVUE_GOOGLE_FEED_USER_EMAIL", TENANT)
    monkeypatch.setenv("LOTVUE_PUBLIC_URL", "https://dealer.example.test")
    monkeypatch.setenv("LOTVUE_GOOGLE_STORE_CODE", "dayton-1")
    monkeypatch.setenv("LOTVUE_DEALER_NAME", "LotVue Dayton")
    monkeypatch.setenv("LOTVUE_DEALER_ADDR1", "123 Main Street")
    monkeypatch.setenv("LOTVUE_DEALER_CITY", "Dayton")
    monkeypatch.setenv("LOTVUE_DEALER_REGION", "OH")
    monkeypatch.setenv("LOTVUE_DEALER_POSTAL", "45402")
    monkeypatch.setenv("LOTVUE_DEALER_COUNTRY", "US")

    response = distribution.google_vehicle_feed("g" * 48)
    root = ET.fromstring(response.body)
    namespace = {"g": "http://base.google.com/ns/1.0"}
    items = root.findall("./channel/item")
    assert len(items) == 1
    assert items[0].findtext("g:vin", namespaces=namespace) == VIN_NEW
    assert items[0].findtext("g:make", namespaces=namespace) == "<Honda & Sons>"
    assert "https://dealer.example.test/view/" + VIN_NEW in response.body.decode()
    assert VIN_OWNED_BY_OTHER not in response.body.decode()


def test_external_feed_urls_reject_local_hosts_and_csv_cells_guard_formulas(monkeypatch) -> None:
    monkeypatch.setenv("LOTVUE_PUBLIC_URL", "http://127.0.0.1:8001")
    with pytest.raises(HTTPException) as exc:
        distribution._public_base(require_public=True)
    assert exc.value.status_code == 503
    assert distribution._csv_cell(" =HYPERLINK(\"https://bad.test\")").startswith("'")


def test_existing_post_log_table_gets_tenant_column(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "distribution.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE post_log (id INTEGER PRIMARY KEY, vin TEXT, channel TEXT, posted_at TEXT, meta TEXT)")
    monkeypatch.setattr(distribution, "DB_PATH", db_path)

    distribution._ensure_post_log()

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(post_log)")}
    assert "user_email" in columns
