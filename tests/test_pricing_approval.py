from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import main  # noqa: E402


def test_unapproved_price_catalog_is_not_published(monkeypatch) -> None:
    monkeypatch.setattr(main, "PRICING_APPROVED", False)
    page = main.pricing_page()

    assert main.get_plans() == {"status": "pending_approval", "plans": []}
    assert page.status_code == 200
    assert "Pricing is being finalized" in page.body.decode()
    assert "$2.99" not in page.body.decode()
    assert "$19.99" not in page.body.decode()
    assert "$75" not in page.body.decode()


def test_public_pricing_routes_hide_unapproved_prices(monkeypatch) -> None:
    monkeypatch.setattr(main, "PRICING_APPROVED", False)
    client = TestClient(main.app)

    page = client.get("/pricing")
    plans = client.get("/api/plans")

    assert page.status_code == 200
    assert "$2.99" not in page.text and "$75" not in page.text
    assert plans.status_code == 200
    assert plans.json() == {"status": "pending_approval", "plans": []}


def test_accidental_approval_cannot_publish_conflicting_plan_amounts(monkeypatch) -> None:
    monkeypatch.setattr(main, "FEATURES_ENABLED", {**main.FEATURES_ENABLED, "payments": True})
    monkeypatch.setattr(main, "PRICING_APPROVED", True)

    monkeypatch.setattr(main, "_authenticated_user_email", lambda _request: "dealer@example.test")
    monkeypatch.setattr(main, "_require_vehicle_owner", lambda *_args, **_kwargs: {"vin": "1HGCM82633A004352"})

    assert not main._pricing_catalog_consistent()
    assert not main._payments_available()
    assert main.get_plans() == {"status": "pending_approval", "plans": []}
    assert "Pricing is being finalized" in main.pricing_page().body.decode()
    assert main.create_checkout_session(object(), "single", "dealer@example.test")["status"] == "disabled"
    assert main.create_subscription(object(), "dealer@example.test", "dealership")["status"] == "disabled"
    assert main.unlock_render(object(), "1HGCM82633A004352")["status"] == "disabled"
    assert asyncio.run(main.stripe_webhook(object())).status_code == 503


def test_approved_consistent_catalog_drives_public_amounts(monkeypatch) -> None:
    checkout_plans = {key: dict(value) for key, value in main.PLANS.items()}
    subscription_plans = {key: dict(value) for key, value in main.PRICING_PLANS.items()}
    subscription_plans["dealership"]["price"] = checkout_plans["dealership"]["price"]
    monkeypatch.setattr(main, "PLANS", checkout_plans)
    monkeypatch.setattr(main, "PRICING_PLANS", subscription_plans)
    monkeypatch.setattr(main, "PRICING_APPROVED", True)

    page = main.pricing_page().body.decode()

    assert main.get_plans()["status"] == "ok"
    assert "$2.99" in page
    assert "$19.99" in page
    assert "$75.00<span>/mo</span>" in page


def test_subscription_amount_comes_from_the_aligned_catalog(monkeypatch) -> None:
    checkout_plans = {key: dict(value) for key, value in main.PLANS.items()}
    subscription_plans = {key: dict(value) for key, value in main.PRICING_PLANS.items()}
    subscription_plans["dealership"]["price"] = checkout_plans["dealership"]["price"]
    monkeypatch.setattr(main, "PLANS", checkout_plans)
    monkeypatch.setattr(main, "PRICING_PLANS", subscription_plans)
    monkeypatch.setattr(main, "FEATURES_ENABLED", {**main.FEATURES_ENABLED, "payments": True})
    monkeypatch.setattr(main, "PRICING_APPROVED", True)
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
    assert price_data["unit_amount"] == 7500
    assert price_data["recurring"] == {"interval": "month"}


def test_subscription_rejects_unpublished_legacy_plan(monkeypatch) -> None:
    subscription_plans = {key: dict(value) for key, value in main.PRICING_PLANS.items()}
    subscription_plans["dealership"]["price"] = main.PLANS["dealership"]["price"]
    monkeypatch.setattr(main, "PRICING_PLANS", subscription_plans)
    monkeypatch.setattr(main, "FEATURES_ENABLED", {**main.FEATURES_ENABLED, "payments": True})
    monkeypatch.setattr(main, "PRICING_APPROVED", True)
    monkeypatch.setattr(main, "STRIPE_KEY", "sk_test_placeholder")
    monkeypatch.setattr(main, "_authenticated_user_email", lambda _request: "dealer@example.test")

    result = main.create_subscription(object(), "dealer@example.test", "enterprise")

    assert result == {"error": "Invalid plan"}


def test_payment_routes_remain_disabled_even_if_feature_flag_is_accidentally_enabled(
    monkeypatch,
) -> None:
    monkeypatch.setattr(main, "FEATURES_ENABLED", {**main.FEATURES_ENABLED, "payments": True})
    monkeypatch.setattr(main, "PRICING_APPROVED", False)
    monkeypatch.setattr(main, "_authenticated_user_email", lambda _request: "dealer@example.test")

    checkout = main.create_checkout_session(object(), "single", "dealer@example.test")
    subscription = main.create_subscription(object(), "dealer@example.test", "dealership")
    webhook = asyncio.run(main.stripe_webhook(object()))

    assert checkout["status"] == "disabled"
    assert subscription["status"] == "disabled"
    assert webhook.status_code == 503
    assert not main._payments_available()
    assert main.check_user_can_render("dealer@example.test")["reason"] == (
        "Payments disabled or pricing unapproved"
    )
