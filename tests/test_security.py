from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from security import hash_password, is_password_hash, needs_rehash, verify_password


def test_password_hash_validation_verification_and_legacy_upgrade() -> None:
    digest = hash_password("a-long-test-password", iterations=100_000)
    assert is_password_hash(digest)
    assert verify_password("a-long-test-password", digest)
    assert not verify_password("wrong-password", digest)
    assert not is_password_hash("malformed")
    assert needs_rehash(digest)

    legacy = "a" * 64
    assert is_password_hash(legacy)
    assert verify_password("legacy", __import__("hashlib").sha256(b"legacy").hexdigest())
    assert needs_rehash(legacy)
