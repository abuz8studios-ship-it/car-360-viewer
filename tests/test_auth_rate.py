from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import auth_rate  # noqa: E402


def _request(host: str) -> Request:
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/api/login",
        "headers": [],
        "client": (host, 12345),
    })


def test_auth_rate_limits_per_ip_and_keeps_buckets_bounded(monkeypatch) -> None:
    monkeypatch.setattr(auth_rate, "_BUCKETS", {})
    auth_rate.check_auth_rate(_request("192.0.2.1"), "login", 2)
    auth_rate.check_auth_rate(_request("192.0.2.1"), "login", 2)
    with pytest.raises(HTTPException) as exc:
        auth_rate.check_auth_rate(_request("192.0.2.1"), "login", 2)
    assert exc.value.status_code == 429

    auth_rate.check_auth_rate(_request("192.0.2.2"), "login", 2)
    assert all(len(key) == 64 for key in auth_rate._BUCKETS)
    assert "192.0.2.1" not in repr(auth_rate._BUCKETS)
