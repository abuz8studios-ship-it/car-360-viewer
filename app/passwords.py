"""
Password helpers for LotVue360 dealer accounts — a thin layer over security.py.

New hashes come from security.hash_password (PBKDF2-SHA256, base64 salt/digest),
so the whole app stores one format. verify_password also accepts:
  - legacy 64-char unsalted sha256 hex (upgraded on the next successful login)
  - pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex> written by the 2026-10-09
    billing branch before it was merged onto security.py

Owner bootstrap hash for LOTVUE_OWNER_PASSWORD_HASH (run inside app/):
    python -c "from passwords import hash_password as h; print(h('your-password'))"
"""
import hashlib
import hmac
import re

import security

MIN_PASSWORD_LEN = 12
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_HEX_PBKDF2_RE = re.compile(r"^pbkdf2_sha256\$(\d+)\$([0-9a-f]{32,})\$([0-9a-f]{64})$")


def hash_password(password: str) -> str:
    return security.hash_password(password)


def is_legacy_hash(stored: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{64}", stored or ""))


def verify_password(password: str, stored: str) -> bool:
    if security.verify_password(password, stored):
        return True
    m = _HEX_PBKDF2_RE.match(stored or "")
    if not m or not isinstance(password, str) or len(password) > 1024:
        return False
    iterations = int(m.group(1))
    if not 100_000 <= iterations <= 2_000_000:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(m.group(2)), iterations)
    return hmac.compare_digest(digest.hex(), m.group(3))


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_signup(email: str, password: str) -> str:
    """Return an error message, or '' if the signup input is acceptable."""
    if not _EMAIL_RE.match(email or ""):
        return "Enter a valid email address."
    if not MIN_PASSWORD_LEN <= len(password or "") <= 1024:
        return f"Password must be {MIN_PASSWORD_LEN}–1,024 characters."
    return ""
