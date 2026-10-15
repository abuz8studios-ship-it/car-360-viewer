"""
Test harness: boots an ISOLATED copy of app/ (fresh SQLite DB, empty storage)
so tests never touch real dealer data, then exposes a FastAPI TestClient.

No network: Stripe is replaced with a fake in tests that need it, and webhook
events are signed locally with the same HMAC scheme Stripe uses.
"""
import hashlib
import hmac
import importlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
APP_SRC = REPO / "app"

WEBHOOK_SECRET = "whsec_test_lotvue_secret"
FEED_TOKEN = "feed-token-for-isolated-app-copy-0123456789"

_IGNORE = shutil.ignore_patterns(
    "storage", "*.db", ".env", ".jwt_secret", "__pycache__", "*.bak*",
    "main_v1.py", "main_original.py", "main_before_op6.py", "vehicles",
    "backgrounds",  # regenerated at boot
)


def _loaded_from(root: Path) -> dict:
    """Loaded modules whose source lives under root (main, billing, app_paths, ...)."""
    prefix = str(root)
    return {name: mod for name, mod in sys.modules.items()
            if str(getattr(mod, "__file__", "") or "").startswith(prefix)}


@pytest.fixture(scope="module")
def app_module(tmp_path_factory):
    work = tmp_path_factory.mktemp("lotvue_app") / "app"
    shutil.copytree(APP_SRC, work, ignore=_IGNORE)
    data = work.parent / "data"
    env = {
        "LOTVUE_DATA_DIR": str(data),
        "LOTVUE_STORAGE_DIR": str(data / "storage"),
        "LOTVUE_FEATURE_AUTH": "1",
        "LOTVUE_FEATURE_PAYMENTS": "1",
        "LOTVUE_PRICING_APPROVED": "1",
        "STRIPE_SECRET_KEY": "sk_test_fake_for_unit_tests",
        "STRIPE_PUBLISHABLE_KEY": "pk_test_fake",
        "STRIPE_WEBHOOK_SECRET": WEBHOOK_SECRET,
        "LOTVUE_PRICE_MONTHLY_CENTS": "9900",
        "LOTVUE_FREE_VEHICLES": "2",
        "LOTVUE_JWT_SECRET": "test-jwt-secret-for-isolated-app-copy-0123456789",
        "LOTVUE_PUBLIC_URL": "https://lotvue.test",
        "LOTVUE_ADMIN_EMAILS": "admin@lotvue.test",
        # Google feed is fail-closed until a dealer configures it.
        "LOTVUE_GOOGLE_FEED_TOKEN": FEED_TOKEN,
        "LOTVUE_GOOGLE_FEED_USER_EMAIL": "feed-dealer@lotvue.test",
        "LOTVUE_GOOGLE_STORE_CODE": "TEST-STORE",
        "LOTVUE_DEALER_NAME": "Test Motors",
        "LOTVUE_DEALER_ADDR1": "1 Main St",
        "LOTVUE_DEALER_CITY": "Dayton",
        "LOTVUE_DEALER_REGION": "OH",
        "LOTVUE_DEALER_POSTAL": "45402",
    }
    old = {k: os.environ.get(k) for k in env}
    # Modules other test files already imported from the real app/ would be
    # reused by the copy and point it back at the repo's data paths; set them
    # aside for this module's tests and restore them afterwards.
    saved = _loaded_from(APP_SRC)
    for name in saved:
        del sys.modules[name]
    os.environ.update(env)
    sys.path.insert(0, str(work))
    cwd = os.getcwd()
    os.chdir(work)
    try:
        main = importlib.import_module("main")
        yield main
    finally:
        os.chdir(cwd)
        sys.path.remove(str(work))
        for name in _loaded_from(work):
            del sys.modules[name]
        sys.modules.update(saved)
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture()
def client(app_module):
    from fastapi.testclient import TestClient
    import auth_rate
    auth_rate._BUCKETS.clear()  # every test is a fresh visitor for the sign-up rate limit
    with TestClient(app_module.app, base_url="https://lotvue.test") as c:
        yield c


def signed_webhook(client, event: dict, secret: str = WEBHOOK_SECRET):
    """POST an event to /api/stripe/webhook signed exactly like Stripe does."""
    payload = json.dumps(event)
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
    return client.post("/api/stripe/webhook", content=payload,
                       headers={"stripe-signature": f"t={ts},v1={sig}",
                                "content-type": "application/json"})


def register(client, email, password="CorrectHorse9", name="Test Dealer"):
    return client.post("/api/register", data={"email": email, "password": password, "name": name})
