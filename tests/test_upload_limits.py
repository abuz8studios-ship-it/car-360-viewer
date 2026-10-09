from __future__ import annotations

import asyncio
import re
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
sys.path.insert(0, str(APP_DIR))

import main  # noqa: E402


def test_upload_body_middleware_rejects_streamed_overflow() -> None:
    async def endpoint(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"accepted"})

    middleware = main._UploadBodyLimitMiddleware(endpoint)
    middleware.rules = ((re.compile(r"^/upload$"), 4),)
    messages = [{"type": "http.request", "body": b"12345", "more_body": False}]
    sent = []

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/upload", "headers": []}
    asyncio.run(middleware(scope, receive, send))

    assert sent[0]["status"] == 413
    assert b"upload limit" in sent[1]["body"]


def test_upload_body_middleware_rejects_oversized_declared_length() -> None:
    async def endpoint(scope, receive, send):
        raise AssertionError("oversized request should be rejected before routing")

    middleware = main._UploadBodyLimitMiddleware(endpoint)
    middleware.rules = ((re.compile(r"^/upload$"), 4),)
    sent = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/upload",
        "headers": [(b"content-length", b"5")],
    }
    asyncio.run(middleware(scope, receive, send))
    assert sent[0]["status"] == 413


def test_multipart_part_limits_reject_before_endpoint_receives_body() -> None:
    async def endpoint(scope, receive, send):
        while True:
            message = await receive()
            if message["type"] == "http.disconnect" or not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"accepted"})

    middleware = main._UploadBodyLimitMiddleware(endpoint)
    middleware.rules = ((re.compile(r"^/upload$"), 4096, 1, 2),)
    body = (
        b'--arena-boundary\r\nContent-Disposition: form-data; name="first"; filename="a.jpg"\r\n'
        b"Content-Type: image/jpeg\r\n\r\nfirst-bytes\r\n"
        b'--arena-boundary\r\nContent-Disposition: form-data; name="second"; filename="b.jpg"\r\n'
        b"Content-Type: image/jpeg\r\n\r\nsecond-bytes\r\n--arena-boundary--\r\n"
    )
    chunks = [body[i:i + 17] for i in range(0, len(body), 17)]
    messages = [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ]
    sent = []

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/upload",
        "headers": [(b"content-type", b"multipart/form-data; boundary=arena-boundary")],
    }
    asyncio.run(middleware(scope, receive, send))

    assert sent[0]["status"] == 413
    assert b"multipart request" in sent[1]["body"].lower()


def test_multipart_part_limiter_accepts_valid_chunked_fields_and_file() -> None:
    guard = main._MultipartPartLimiter(b"boundary", max_files=1, max_fields=2)
    body = (
        b'--boundary\r\nContent-Disposition: form-data; name="vin"\r\n\r\n123\r\n'
        b'--boundary\r\nContent-Disposition: form-data; name="photo"; filename="x.jpg"\r\n'
        b"Content-Type: image/jpeg\r\n\r\nphoto-bytes\r\n--boundary--\r\n"
    )
    assert all(guard.feed(body[i:i + 5]) for i in range(0, len(body), 5))
    assert guard.fields == 1
    assert guard.files == 1


def test_upload_concurrency_limit_rejects_over_capacity_and_releases_slot() -> None:
    async def exercise():
        entered = asyncio.Event()
        release = asyncio.Event()
        endpoint_calls = 0

        async def endpoint(_scope, _receive, send):
            nonlocal endpoint_calls
            endpoint_calls += 1
            if endpoint_calls == 1:
                entered.set()
                await release.wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"accepted"})

        middleware = main._UploadBodyLimitMiddleware(endpoint)
        middleware.rules = ((re.compile(r"^/upload$"), 4096, 1, 1, True),)
        middleware.upload_slots = threading.BoundedSemaphore(1)
        scope = {"type": "http", "method": "POST", "path": "/upload", "headers": []}

        async def receive():
            return {"type": "http.disconnect"}

        async def invoke():
            sent = []

            async def send(message):
                sent.append(message)

            await middleware(scope, receive, send)
            return sent

        first_task = asyncio.create_task(invoke())
        await entered.wait()
        rejected = await invoke()
        release.set()
        first = await first_task
        accepted_after_release = await invoke()
        return first, rejected, accepted_after_release, endpoint_calls

    first, rejected, accepted_after_release, endpoint_calls = asyncio.run(exercise())

    assert first[0]["status"] == 200
    assert rejected[0]["status"] == 503
    assert (b"retry-after", b"30") in rejected[0]["headers"]
    assert b"capacity is full" in rejected[1]["body"]
    assert accepted_after_release[0]["status"] == 200
    assert endpoint_calls == 2


def test_multipart_part_limiter_bounds_field_payload_before_form_parsing() -> None:
    guard = main._MultipartPartLimiter(b"boundary", max_files=0, max_fields=1)
    headers = b'--boundary\r\nContent-Disposition: form-data; name="note"\r\n\r\n'
    assert guard.feed(headers)
    assert guard.feed(b"x" * (guard.MAX_FIELD_BYTES + 1))
    assert not guard.feed(b"x" * 16)
