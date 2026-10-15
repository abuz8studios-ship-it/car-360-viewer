from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import main  # noqa: E402

billing = main._billing


def test_unapproved_price_catalog_is_not_published(monkeypatch) -> None:
    monkeypatch.setattr(main, "PRICING_APPROVED", False)
    client = TestClient(main.app)

    page = client.get("/pricing")
    plans = client.get("/api/plans")

    assert main.get_plans() == {"status": "pending_approval", "plans": []}
    assert plans.status_code == 200
    assert plans.json() == {"status": "pending_approval", "plans": []}
    assert page.status_code == 200
    assert "Pricing is being finalized" in page.text
    for amount in ("$99", "$2.99", "$19.99", "$75"):
        assert amount not in page.text


def test_approved_offer_is_the_single_billing_price(monkeypatch) -> None:
    monkeypatch.setattr(main, "PRICING_APPROVED", True)
    client = TestClient(main.app)

    plans = main.get_plans()
    page = client.get("/pricing").text

    assert plans["status"] == "ok"
    assert plans["plans"]["dealership"]["amount_cents"] == billing.plan()["amount_cents"] == 9900
    assert plans["plans"]["free"]["vehicles"] == billing.free_vehicle_limit() == 5
    assert main.PLANS["dealership"]["price"] == main.PRICING_PLANS["dealership"]["price"] == 99.0
    assert "$99" in page
    assert "$2.99" not in page and "$19.99" not in page


def test_credit_packs_stay_off_when_the_subscription_is_live(monkeypatch) -> None:
    monkeypatch.setattr(main, "FEATURES_ENABLED", {**main.FEATURES_ENABLED, "payments": True})
    monkeypatch.setattr(main, "PRICING_APPROVED", True)
    monkeypatch.setattr(main, "CREDIT_PACKS_ENABLED", False)
    monkeypatch.setattr(main, "_authenticated_user_email", lambda _request: "dealer@example.test")
    monkeypatch.setattr(main, "_require_vehicle_owner", lambda *_args, **_kwargs: {"vin": "1HGCM82633A004352"})

    assert main._payments_available()
    assert not main._credit_packs_available()
    assert main.create_checkout_session(object(), "single", "dealer@example.test")["status"] == "disabled"
    assert main.create_subscription(object(), "dealer@example.test", "dealership")["status"] == "disabled"
    assert main.unlock_render(object(), "1HGCM82633A004352")["status"] == "disabled"


def test_legacy_subscription_amount_comes_from_the_billing_price(monkeypatch) -> None:
    monkeypatch.setattr(main, "FEATURES_ENABLED", {**main.FEATURES_ENABLED, "payments": True})
    monkeypatch.setattr(main, "PRICING_APPROVED", True)
    monkeypatch.setattr(main, "CREDIT_PACKS_ENABLED", True)
    monkeypatch.setattr(main, "STRIPE_KEY", "sk_test_placeholder")
    monkeypatch.setattr(main, "_authenticated_user_email", lambda _request: "dealer@example.test")
    monkeypatch.setattr(main, "db_get_user", lambda _email: None)
    captured = {}

    stripe_stub = ModuleType("stripe")
    stripe_stub.Customer = SimpleNamespace(
        create=lambda **_kwargs: SimpleNamespace(id="cus_test")
    )
    stripe_stub.Subscription = SimpleNamespace(
        create=lambda **kwargs: (captured.update(kwargs) or SimpleNamespace(
            id="sub_test",
            get=lambda key, default=None: {"latest_invoice": {}}.get(key, default),
        ))
    )
    monkeypatch.setitem(sys.modules, "stripe", stripe_stub)

    result = main.create_subscription(object(), "dealer@example.test", "dealership")

    assert result["status"] == "ok"
    price_data = captured["items"][0]["price_data"]
    assert price_data["unit_amount"] == billing.plan()["amount_cents"] == 9900
    assert price_data["recurring"] == {"interval": "month"}


def test_subscription_rejects_unpublished_legacy_plan(monkeypatch) -> None:
    monkeypatch.setattr(main, "FEATURES_ENABLED", {**main.FEATURES_ENABLED, "payments": True})
    monkeypatch.setattr(main, "PRICING_APPROVED", True)
    monkeypatch.setattr(main, "CREDIT_PACKS_ENABLED", True)
    monkeypatch.setattr(main, "STRIPE_KEY", "sk_test_placeholder")
    monkeypatch.setattr(main, "_authenticated_user_email", lambda _request: "dealer@example.test")

    result = main.create_subscription(object(), "dealer@example.test", "enterprise")

    assert result == {"error": "Invalid plan"}


def test_payment_routes_remain_disabled_even_if_feature_flag_is_accidentally_enabled(
    monkeypatch,
) -> None:
    monkeypatch.setattr(main, "FEATURES_ENABLED", {**main.FEATURES_ENABLED, "payments": True})
    monkeypatch.setattr(main, "PRICING_APPROVED", False)
    monkeypatch.setattr(main, "CREDIT_PACKS_ENABLED", True)
    monkeypatch.setattr(main, "_authenticated_user_email", lambda _request: "dealer@example.test")

    checkout = main.create_checkout_session(object(), "single", "dealer@example.test")
    subscription = main.create_subscription(object(), "dealer@example.test", "dealership")
    webhook = asyncio.run(main.stripe_webhook(object()))

    assert checkout["status"] == "disabled"
    assert subscription["status"] == "disabled"
    assert webhook.status_code == 503
    assert not main._payments_available()
    assert not billing.payments_enabled()
    assert billing.free_slots_left("dealer@example.test") is None  # no paywall
    assert main.check_user_can_render("dealer@example.test")["reason"] == (
        "Payments disabled or pricing unapproved"
    )
