"""POST /api/auth/refresh must not outlive the account it was issued for.

Before the fix the endpoint re-signed a fresh access token from any unexpired
refresh token without checking the ``users`` table, so a removed account could
keep minting tokens for the whole refresh lifetime.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import a2_extensions  # noqa: E402
from a2_extensions import _make_tokens  # noqa: E402

LIVE = "live@example.test"
REMOVED = "removed@example.test"


def _client(monkeypatch, tmp_path: Path, emails: list[str]) -> TestClient:
    db_path = tmp_path / "auth.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE users (email TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO users VALUES (?)", [(email,) for email in emails])
    monkeypatch.setattr(a2_extensions, "DB_PATH", db_path)
    monkeypatch.setenv("LOTVUE_JWT_SECRET", "refresh-revocation-test-secret-32-bytes")
    app = FastAPI()
    app.include_router(a2_extensions.router)
    return TestClient(app)


def test_refresh_issues_tokens_for_an_existing_account(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path, [LIVE, REMOVED])
    refresh_token = _make_tokens(LIVE)["refresh_token"]

    response = client.post("/api/auth/refresh", json={"refresh_token": refresh_token})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"] and body["refresh_token"]


def test_refresh_is_rejected_after_the_account_is_removed(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path, [LIVE, REMOVED])
    refresh_token = _make_tokens(REMOVED)["refresh_token"]

    # The dealership account is deleted while its refresh token is still unexpired.
    with sqlite3.connect(tmp_path / "auth.sqlite3") as conn:
        conn.execute("DELETE FROM users WHERE email=?", (REMOVED,))

    response = client.post("/api/auth/refresh", json={"refresh_token": refresh_token})

    assert response.status_code == 401
    assert response.json()["detail"] == "User no longer exists"


def test_refresh_rejects_non_refresh_and_malformed_tokens(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path, [LIVE])

    access_as_refresh = client.post(
        "/api/auth/refresh", json={"refresh_token": _make_tokens(LIVE)["access_token"]}
    )
    assert access_as_refresh.status_code == 401
    assert access_as_refresh.json()["detail"] == "Not a refresh token"

    garbage = client.post("/api/auth/refresh", json={"refresh_token": "not.a.jwt"})
    assert garbage.status_code == 401
    assert garbage.json()["detail"] == "Invalid or expired refresh token"

    missing = client.post("/api/auth/refresh", json={"refresh_token": ""})
    assert missing.status_code == 401
