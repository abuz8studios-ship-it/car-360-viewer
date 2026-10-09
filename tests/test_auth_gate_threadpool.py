from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

import a2_extensions  # noqa: E402
import starlette.concurrency  # noqa: E402
from app.auth_gate import install_auth_gate  # noqa: E402


def test_authentication_database_lookup_runs_off_the_event_loop(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "auth.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE users (email TEXT PRIMARY KEY)")
    monkeypatch.setattr(a2_extensions, "DB_PATH", db_path)
    monkeypatch.setenv("LOTVUE_JWT_SECRET", "threadpool-auth-test-secret-at-least-32-bytes")
    token = a2_extensions._make_tokens("not-present@example.test")["access_token"]

    event_loop_threads: list[int] = []
    database_threads: list[int] = []
    original_run_in_threadpool = starlette.concurrency.run_in_threadpool
    original_connect = sqlite3.connect

    async def tracked_run_in_threadpool(func, *args, **kwargs):
        if getattr(func, "__name__", "") == "_valid":
            event_loop_threads.append(threading.get_ident())
        return await original_run_in_threadpool(func, *args, **kwargs)

    def tracked_connect(*args, **kwargs):
        database_threads.append(threading.get_ident())
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(starlette.concurrency, "run_in_threadpool", tracked_run_in_threadpool)
    monkeypatch.setattr(sqlite3, "connect", tracked_connect)

    app = FastAPI()
    app.get("/api/private")(lambda: {"ok": True})
    install_auth_gate(app)

    response = TestClient(app).get(
        "/api/private", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401
    assert event_loop_threads
    assert database_threads
    assert all(db_thread != loop_thread for db_thread, loop_thread in zip(database_threads, event_loop_threads))
