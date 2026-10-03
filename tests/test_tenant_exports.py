from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
sys.path.insert(0, str(APP_DIR))

import a2_extensions  # noqa: E402
import b_distribution  # noqa: E402
import main  # noqa: E402
from a2_extensions import _make_tokens  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

OWNER = "tenant@example.test"


def test_authenticated_syndication_exports_use_only_the_current_tenant(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "users.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE users (email TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO users VALUES (?)", (OWNER,))
    monkeypatch.setattr(a2_extensions, "DB_PATH", db_path)
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(main, "db_get_user", lambda email: {"email": email} if email == OWNER else None)
    monkeypatch.setenv("LOTVUE_JWT_SECRET", "tenant-export-test-secret-with-32-bytes")
    monkeypatch.delenv("LOTVUE_OWNER_EMAIL", raising=False)
    calls = []
    tenant_inventory = [{"vin": "1HGCM82633A004352", "user_email": OWNER}]
    monkeypatch.setattr(
        main,
        "db_get_vehicles_by_email",
        lambda email: calls.append(email) or tenant_inventory,
    )
    monkeypatch.setattr(main, "db_get_vehicles", lambda: (_ for _ in ()).throw(AssertionError("global inventory read")))

    from ai_studio import listing_syndication

    monkeypatch.setattr(listing_syndication, "export_facebook_csv", lambda rows: f"facebook:{rows[0]['vin']}")
    monkeypatch.setattr(listing_syndication, "export_xml_feed", lambda rows: f"xml:{rows[0]['vin']}")
    monkeypatch.setattr(listing_syndication, "export_json_api", lambda rows: {"vehicles": rows})

    client = TestClient(main.app)
    auth = {"Authorization": f"Bearer {_make_tokens(OWNER)['access_token']}"}
    for endpoint in ("/api/syndication/facebook-csv", "/api/syndication/xml-feed", "/api/syndication/json"):
        response = client.get(endpoint, headers=auth)
        assert response.status_code == 200
        assert OWNER in calls
    assert response.json() == {"vehicles": tenant_inventory}
    assert len(calls) == 3


def test_csv_guards_catch_formulae_after_tab_cr_and_lf() -> None:
    for whitespace in (chr(9), chr(13), chr(10), " " + chr(9), " " + chr(13), " " + chr(10)):
        formula = f"{whitespace}=1+2"
        assert a2_extensions._safe_csv_value(formula) == "'" + formula
        assert b_distribution._csv_cell(formula) == "'" + formula
    assert a2_extensions._safe_csv_value("@SUM(A1:A2)") == "'@SUM(A1:A2)"
    assert a2_extensions._safe_csv_value(123) == 123
    assert b_distribution._csv_cell("+cmd|' /C calc'!A0") == "'+cmd|' /C calc'!A0"


def test_showroom_paths_are_tenant_specific_and_names_cannot_escape() -> None:
    assert main._showroom_file("one@example.test") != main._showroom_file("two@example.test")
    try:
        main._showroom_file("one@example.test", "../outside")
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 400
    else:
        raise AssertionError("path traversal showroom name was accepted")
