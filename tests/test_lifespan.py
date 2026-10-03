from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import main  # noqa: E402


def test_lifespan_stops_the_optional_followup_dispatcher(monkeypatch) -> None:
    stop_event = threading.Event()
    monkeypatch.setattr(main, "_start_followup_dispatcher", lambda: stop_event)

    async def exercise_lifespan() -> None:
        async with main.app.router.lifespan_context(main.app):
            assert not stop_event.is_set()
        assert stop_event.is_set()

    asyncio.run(exercise_lifespan())
