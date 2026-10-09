"""
Password hashing for LotVue360 dealer accounts.

New hashes:   pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>   (stdlib only)
Legacy:       64-char unsalted sha256 hex — still VERIFIED so existing accounts
              (and the .env-asserted owner account) keep working, and are
              transparently upgraded to PBKDF2 on the next successful login.
"""
import hashlib
import hmac
import os
import re

_ITERATIONS = 390_000
_PREFIX = "pbkdf2_sha256"
_LEGACY_RE = re.compile(r"^[0-9a-f]{64}$")

MIN_PASSWORD_LEN = 8
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{_PREFIX}${_ITERATIONS}${salt.hex()}${dk.hex()}"


def is_legacy_hash(stored: str) -> bool:
    return bool(stored) and bool(_LEGACY_RE.match(stored.lower()))


def verify_password(password: str, stored: str) -> bool:
    if not stored or password is None:
        return False
    if stored.startswith(_PREFIX + "$"):
        try:
            _, iters, salt_hex, hash_hex = stored.split("$", 3)
            dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     bytes.fromhex(salt_hex), int(iters))
            return hmac.compare_digest(dk.hex(), hash_hex)
        except (ValueError, TypeError):
            return False
    if is_legacy_hash(stored):
        legacy = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(legacy, stored.lower())
    return False


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_signup(email: str, password: str) -> str:
    """Return an error message, or '' if the signup input is acceptable."""
    if not _EMAIL_RE.match(email or ""):
        return "Enter a valid email address."
    if len(password or "") < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    return ""
