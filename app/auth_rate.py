"""Small bounded per-process rate limiter for authentication endpoints."""
from __future__ import annotations

import hashlib
import threading
import time

from fastapi import HTTPException, Request

_BUCKETS: dict[str, tuple[float, int]] = {}
_LOCK = threading.Lock()
_WINDOW_SECONDS = 60.0
_MAX_BUCKETS = 4096


def check_auth_rate(request: Request, action: str, limit: int) -> None:
    """Limit password-sensitive actions by source IP without retaining raw IPs."""
    host = request.client.host if request.client else "unknown"
    key = hashlib.sha256(f"{action}:{host}".encode("utf-8", errors="ignore")).hexdigest()
    now = time.monotonic()
    with _LOCK:
        start, count = _BUCKETS.get(key, (now, 0))
        if now - start >= _WINDOW_SECONDS:
            start, count = now, 0
        if count >= limit:
            raise HTTPException(
                status_code=429,
                detail="Too many authentication attempts; try again shortly",
                headers={"Retry-After": str(int(_WINDOW_SECONDS))},
            )
        _BUCKETS[key] = (start, count + 1)
        size = len(_BUCKETS)
        if size > _MAX_BUCKETS and size % 256 == 0:
            expired = [client for client, (window, _) in _BUCKETS.items()
                       if now - window >= _WINDOW_SECONDS]
            for client in expired:
                _BUCKETS.pop(client, None)
            excess = len(_BUCKETS) - _MAX_BUCKETS
            if excess > 0:
                # Dict insertion order provides a cheap approximate oldest-first
                # eviction; the map remains strictly bounded under source floods.
                for client in list(_BUCKETS)[:excess]:
                    _BUCKETS.pop(client, None)
