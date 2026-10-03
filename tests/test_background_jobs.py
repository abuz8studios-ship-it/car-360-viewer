from __future__ import annotations

import sys
import threading
from concurrent.futures import Future
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import main  # noqa: E402


class _ControlledExecutor:
    def __init__(self, *, fail_submit: bool = False) -> None:
        self.futures: list[Future] = []
        self.calls = []
        self.fail_submit = fail_submit

    def submit(self, target, *args, **kwargs):
        if self.fail_submit:
            raise RuntimeError("executor unavailable")
        future = Future()
        self.calls.append((target, args, kwargs))
        self.futures.append(future)
        return future


def _patch_dispatcher(monkeypatch, capacity: int = 1, *, fail_submit: bool = False):
    executor = _ControlledExecutor(fail_submit=fail_submit)
    monkeypatch.setattr(main, "_BACKGROUND_EXECUTOR", executor)
    monkeypatch.setattr(main, "_BACKGROUND_JOB_SLOTS", threading.BoundedSemaphore(capacity))
    monkeypatch.setattr(main, "_BACKGROUND_JOBS_LOCK", threading.Lock())
    monkeypatch.setattr(main, "_BACKGROUND_JOB_KEYS", set())
    return executor


def test_background_jobs_deduplicate_and_release_capacity_on_completion(monkeypatch) -> None:
    executor = _patch_dispatcher(monkeypatch)
    target = lambda value: value

    assert main._submit_background_job("vehicle:1", target, "first") == "started"
    assert main._submit_background_job("vehicle:1", target, "duplicate") == "already_running"
    assert main._submit_background_job("vehicle:2", target, "queued") == "busy"
    assert len(executor.calls) == 1
    assert main._background_job_running("vehicle:1")

    executor.futures[0].set_result("done")

    assert not main._background_job_running("vehicle:1")
    assert main._submit_background_job("vehicle:2", target, "retry") == "started"
    assert len(executor.calls) == 2


def test_background_job_failures_release_capacity_and_submission_errors_roll_back(monkeypatch) -> None:
    executor = _patch_dispatcher(monkeypatch)
    assert main._submit_background_job("vehicle:1", lambda: None) == "started"

    executor.futures[0].set_exception(RuntimeError("worker failed"))

    assert not main._background_job_running("vehicle:1")
    assert main._submit_background_job("vehicle:2", lambda: None) == "started"

    failing = _patch_dispatcher(monkeypatch, fail_submit=True)
    try:
        main._submit_background_job("vehicle:3", lambda: None)
    except RuntimeError as error:
        assert str(error) == "executor unavailable"
    else:
        raise AssertionError("executor submission failure must propagate")
    assert not main._background_job_running("vehicle:3")
    assert main._BACKGROUND_JOB_SLOTS.acquire(blocking=False)
    main._BACKGROUND_JOB_SLOTS.release()
    assert failing.calls == []


def test_gaussian_worker_persists_a_failure_state(monkeypatch) -> None:
    written = []
    monkeypatch.setattr(main, "_write_gaussian_status", lambda vin, data: written.append((vin, data)))
    monkeypatch.setattr(main.subprocess, "run", lambda *args, **kwargs: type("Result", (), {"returncode": 3})())

    main._run_gaussian_3d_job("1HGCM82633A004352", ["--images", "/tmp/images"], 100, "natural", "light")

    assert written
    assert written[-1][0] == "1HGCM82633A004352"
    assert written[-1][1]["status"] == "error"
    assert "status 3" in written[-1][1]["error"]
