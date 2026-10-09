"""Small, dependency-free helpers for credential handling.

New password hashes use PBKDF2-HMAC-SHA256 with a random salt. The verifier
still accepts the legacy SHA-256 hashes used by earlier LotVue versions and
lets the login path transparently upgrade them after a successful check.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 600_000
_MIN_SALT_BYTES = 16
_MAX_ACCEPTED_ITERATIONS = 2_000_000
_LEGACY_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


def hash_password(password: str, *, iterations: int = _ITERATIONS) -> str:
    """Hash a password using a random 128-bit salt.

    Password length is bounded to avoid an attacker forcing excessive work or
    memory use through registration/login requests.
    """
    if not isinstance(password, str):
        raise TypeError("password must be text")
    if not password or len(password) > 1024:
        raise ValueError("password must be between 1 and 1024 characters")
    if not 100_000 <= iterations <= _MAX_ACCEPTED_ITERATIONS:
        raise ValueError("invalid PBKDF2 iteration count")

    salt = secrets.token_bytes(_MIN_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    salt_text = base64.urlsafe_b64encode(salt).decode("ascii").rstrip("=")
    digest_text = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"{_ALGORITHM}${iterations}${salt_text}${digest_text}"


def is_password_hash(stored_hash: str | None) -> bool:
    """Validate a supported persisted password-hash encoding without a password."""
    if not isinstance(stored_hash, str):
        return False
    if _LEGACY_SHA256.fullmatch(stored_hash):
        return True
    try:
        algorithm, raw_iterations, salt_text, digest_text = stored_hash.split("$", 3)
        iterations = int(raw_iterations)
        if algorithm != _ALGORITHM or not 100_000 <= iterations <= _MAX_ACCEPTED_ITERATIONS:
            return False
        salt = base64.urlsafe_b64decode(salt_text + "=" * (-len(salt_text) % 4))
        digest = base64.urlsafe_b64decode(digest_text + "=" * (-len(digest_text) % 4))
        return len(salt) >= _MIN_SALT_BYTES and len(digest) == hashlib.sha256().digest_size
    except (ValueError, TypeError, base64.binascii.Error):
        return False


def verify_password(password: str, stored_hash: str | None) -> bool:
    """Constant-time verification for current PBKDF2 and legacy SHA-256 hashes."""
    if not isinstance(password, str) or len(password) > 1024 or not stored_hash:
        return False

    if _LEGACY_SHA256.fullmatch(stored_hash):
        candidate = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(candidate.lower(), stored_hash.lower())

    try:
        algorithm, raw_iterations, salt_text, digest_text = stored_hash.split("$", 3)
        iterations = int(raw_iterations)
        if algorithm != _ALGORITHM or not 100_000 <= iterations <= _MAX_ACCEPTED_ITERATIONS:
            return False
        # Restore optional base64 padding. Reject malformed or undersized salts.
        salt = base64.urlsafe_b64decode(salt_text + "=" * (-len(salt_text) % 4))
        expected = base64.urlsafe_b64decode(digest_text + "=" * (-len(digest_text) % 4))
        if len(salt) < _MIN_SALT_BYTES or len(expected) != hashlib.sha256().digest_size:
            return False
    except (ValueError, TypeError, base64.binascii.Error):
        return False

    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def secure_cookie_for_request(request) -> bool:
    """Return whether session cookies should carry Secure.

    An explicit deployment setting wins; otherwise use the request scheme
    (which should reflect the external HTTPS scheme when proxy headers are
    configured on Uvicorn).
    """
    import os

    configured = os.getenv("LOTVUE_COOKIE_SECURE")
    if configured is not None:
        return configured.strip().lower() in {"1", "true", "yes", "on"}
    return str(getattr(getattr(request, "url", None), "scheme", "http")).lower() == "https"


def needs_rehash(stored_hash: str | None) -> bool:
    """Return true for legacy hashes or hashes weaker than the current policy."""
    if not stored_hash or _LEGACY_SHA256.fullmatch(stored_hash):
        return True
    try:
        algorithm, raw_iterations, _salt, _digest = stored_hash.split("$", 3)
        return algorithm != _ALGORITHM or int(raw_iterations) < _ITERATIONS
    except (ValueError, TypeError):
        return True
