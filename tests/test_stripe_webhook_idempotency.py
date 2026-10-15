from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
sys.path.insert(0, str(APP_DIR))

import main  # noqa: E402


def _webhook_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                credits INTEGER DEFAULT 0,
                is_subscribed INTEGER DEFAULT 0,
                plan_type TEXT DEFAULT 'free'
            );
            CREATE TABLE payments (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                amount REAL DEFAULT 0,
                plan TEXT DEFAULT '',
                credits_purchased INTEGER DEFAULT 0,
                stripe_payment_id TEXT DEFAULT '',
                status TEXT DEFAULT '',
                created_at TEXT DEFAULT ''
            );
            CREATE TABLE credit_log (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                amount INTEGER,
                action TEXT,
                vin TEXT,
                created_at TEXT
            );
            CREATE TABLE vehicles (
                vin TEXT PRIMARY KEY,
                user_email TEXT,
                status TEXT DEFAULT 'active',
                is_paid INTEGER DEFAULT 0,
                watermark_mode TEXT DEFAULT 'preview',
                payment_id TEXT DEFAULT ''
            );
            CREATE TABLE stripe_webhook_events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'processing',
                received_at TEXT NOT NULL DEFAULT '',
                processed_at TEXT DEFAULT '',
                error TEXT DEFAULT ''
            );
            """
        )
        conn.execute("INSERT INTO users (id,email,credits) VALUES (1,'dealer@example.test',5)")
        conn.execute(
            "INSERT INTO vehicles (vin,user_email,status) VALUES "
            "('1HGCM82633A004352','dealer@example.test','active')"
        )


def test_credit_checkout_event_is_applied_once_even_across_event_ids(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "webhooks.sqlite3"
    _webhook_db(db_path)
    monkeypatch.setattr(main, "DB_PATH", db_path)
    event = {
        "id": "evt_credit_one",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_paid_once",
                "payment_status": "paid",
                "amount_total": 299,
                "customer_email": "dealer@example.test",
                "metadata": {"plan_id": "single", "user_email": "dealer@example.test"},
            }
        },
    }

    assert main._apply_stripe_event(event) == (True, [])
    assert main._apply_stripe_event(event) == (False, [])
    replay_with_new_event_id = {**event, "id": "evt_credit_retry_variant"}
    assert main._apply_stripe_event(replay_with_new_event_id) == (True, [])

    with sqlite3.connect(db_path) as conn:
        credits = conn.execute("SELECT credits FROM users WHERE id=1").fetchone()[0]
        payments = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        logs = conn.execute("SELECT COUNT(*) FROM credit_log").fetchone()[0]
    assert credits == 6
    assert payments == 1
    assert logs == 1


def test_vehicle_unlock_event_requires_matching_tenant_and_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "webhooks.sqlite3"
    _webhook_db(db_path)
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setenv("LOTVUE_OWNER_EMAIL", "platform@example.test")
    event = {
        "id": "evt_unlock_one",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_unlock_one",
                "payment_status": "paid",
                "metadata": {
                    "type": "unlock_vehicle",
                    "vin": "1HGCM82633A004352",
                    "user_email": "attacker@example.test",
                },
            }
        },
    }

    assert main._apply_stripe_event(event) == (True, [])
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT is_paid,payment_id FROM vehicles").fetchone() == (0, "")

    valid_event = {
        **event,
        "id": "evt_unlock_two",
        "data": {
            "object": {
                **event["data"]["object"],
                "id": "cs_unlock_two",
                "metadata": {
                    **event["data"]["object"]["metadata"],
                    "user_email": "dealer@example.test",
                },
            }
        },
    }
    assert main._apply_stripe_event(valid_event) == (True, ["1HGCM82633A004352"])
    assert main._apply_stripe_event(valid_event) == (False, [])
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT is_paid,payment_id FROM vehicles").fetchone() == (1, "cs_unlock_two")


def test_unlock_webhook_retries_if_clean_frame_queue_is_full(tmp_path: Path, monkeypatch) -> None:
    from fastapi.testclient import TestClient
    import stripe

    db_path = tmp_path / "webhooks.sqlite3"
    _webhook_db(db_path)
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(main, "STORAGE_DIR", tmp_path / "storage")
    monkeypatch.setattr(main, "STRIPE_KEY", "sk_test_placeholder")
    monkeypatch.setattr(main, "STRIPE_WEBHOOK_SECRET", "whsec_test_placeholder")
    monkeypatch.setattr(main, "FEATURES_ENABLED", {**main.FEATURES_ENABLED, "payments": True})
    monkeypatch.setattr(main, "PRICING_APPROVED", True)
    approved_subscription_plans = {
        key: dict(value) for key, value in main.PRICING_PLANS.items()
    }
    approved_subscription_plans["dealership"]["price"] = main.PLANS["dealership"]["price"]
    monkeypatch.setattr(main, "PRICING_PLANS", approved_subscription_plans)
    event = {
        "id": "evt_unlock_queue_retry",
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": "cs_unlock_queue_retry",
            "payment_status": "paid",
            "metadata": {
                "type": "unlock_vehicle",
                "vin": "1HGCM82633A004352",
                "user_email": "dealer@example.test",
            },
        }},
    }
    monkeypatch.setattr(
        stripe.Webhook,
        "construct_event",
        staticmethod(lambda payload, signature, secret: event),
    )
    queue_attempts = []

    def submit(job_key, target, *args, **kwargs):
        queue_attempts.append((job_key, target, args))
        return "busy" if len(queue_attempts) == 1 else "started"

    monkeypatch.setattr(main, "_submit_background_job", submit)
    client = TestClient(main.app)

    first = client.post("/api/stripe/webhook", content=b"{}", headers={"stripe-signature": "signed"})
    assert first.status_code == 503
    assert first.headers["retry-after"] == "30"

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT status FROM stripe_webhook_events").fetchone() == ("retry",)
        assert conn.execute("SELECT is_paid FROM vehicles").fetchone() == (1,)

    retried = client.post("/api/stripe/webhook", content=b"{}", headers={"stripe-signature": "signed"})
    assert retried.status_code == 200
    assert retried.json()["status"] == "received"
    assert len(queue_attempts) == 2
    assert queue_attempts[0][0] == queue_attempts[1][0] == "clean-frames:1HGCM82633A004352"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT status FROM stripe_webhook_events").fetchone() == ("processed",)
        assert conn.execute("SELECT is_paid,payment_id FROM vehicles").fetchone() == (
            1, "cs_unlock_queue_retry"
        )
