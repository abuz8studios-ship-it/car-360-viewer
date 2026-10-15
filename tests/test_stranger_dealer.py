"""
The stranger-dealer test (PRD.md "Definition of done"), automated end to end
against an isolated app instance:

  1. Create account -> land on a working dashboard (not 401, not blank)
  2/3. Upload a capture -> bad capture rejected with a structured reason,
       good capture proceeds
  5/6. Public VDP / Google feed respond
  7. PAYS -> Stripe Checkout (subscription) -> signed webhook activates the
     plan -> receipt recorded and shown on /billing

Stripe itself is faked (no network); webhook events are signed with the real
Stripe HMAC scheme so the signature verification path is exercised for real.
"""
import hashlib
import io
import sqlite3
import xml.etree.ElementTree as ET

import pytest
from PIL import Image

from conftest import FEED_TOKEN, register, signed_webhook


# ----------------------------------------------------------------------------- fakes
class _Obj(dict):
    def to_dict(self):
        return dict(self)


class FakeStripe:
    """Records calls; mimics the subset of the stripe SDK billing.py uses."""

    def __init__(self):
        self.created = []
        self.sessions = {}
        self.invoices = {}
        outer = self

        class _Session:
            @staticmethod
            def create(**kw):
                sid = f"cs_test_{len(outer.created) + 1}"
                outer.created.append(kw)
                s = _Obj(id=sid, url=f"https://checkout.stripe.test/{sid}", **kw)
                outer.sessions[sid] = s
                return s

            @staticmethod
            def retrieve(sid):
                return outer.sessions[sid]

        class _Checkout:
            Session = _Session

        class _PortalSession:
            @staticmethod
            def create(**kw):
                return _Obj(url="https://billing.stripe.test/portal", **kw)

        class _Portal:
            Session = _PortalSession

        class _Invoice:
            @staticmethod
            def retrieve(iid):
                return outer.invoices[iid]

        self.checkout = _Checkout
        self.billing_portal = _Portal
        self.Invoice = _Invoice


@pytest.fixture()
def fake_stripe(app_module, monkeypatch):
    import billing
    fs = FakeStripe()
    monkeypatch.setattr(billing, "_stripe", lambda: fs)
    return fs


def _db(app_module):
    conn = sqlite3.connect(str(app_module.DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _user(app_module, email):
    conn = _db(app_module)
    row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _jpeg(color, size=(1200, 800)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


# ----------------------------------------------------------------------------- 1. account + dashboard
def test_anonymous_root_redirects_to_login(client):
    r = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login")


def test_signup_lands_on_working_dashboard(client):
    r = register(client, "Dealer1@Example.com")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"
    assert r.json()["email"] == "dealer1@example.com"          # normalized
    assert "lotvue_token" in client.cookies                      # signed straight in
    d = client.get("/dashboard", headers={"accept": "text/html"}, follow_redirects=False)
    assert d.status_code == 200
    assert "LotVue" in d.text and "/capture" in d.text and "/billing" in d.text


def test_signup_validation_and_duplicates(client):
    assert register(client, "not-an-email").status_code == 400
    assert register(client, "short@example.com", password="123").status_code == 400
    assert register(client, "dup@example.com").status_code == 200
    r = register(client, "DUP@example.com")
    assert r.status_code == 409


def test_passwords_are_salted_not_plain_sha256(client, app_module):
    register(client, "salted@example.com", password="CorrectHorse9")
    stored = _user(app_module, "salted@example.com")["password_hash"]
    assert stored.startswith("pbkdf2_sha256$")
    assert stored != hashlib.sha256(b"CorrectHorse9").hexdigest()


def test_login_and_legacy_hash_upgrade(client, app_module):
    conn = _db(app_module)
    conn.execute("INSERT INTO users (email, password_hash, plan, credits) VALUES (?, ?, 'free', 5)",
                 ("legacy@example.com", hashlib.sha256(b"OldPassword1").hexdigest()))
    conn.commit()
    conn.close()
    assert client.post("/api/login", data={"email": "legacy@example.com",
                                           "password": "wrong-pass"}).status_code == 401
    r = client.post("/api/login", data={"email": "Legacy@Example.com", "password": "OldPassword1"})
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert _user(app_module, "legacy@example.com")["password_hash"].startswith("pbkdf2_sha256$")
    # JSON token endpoint verifies the upgraded hash too
    t = client.post("/api/auth/token", json={"email": "legacy@example.com", "password": "OldPassword1"})
    assert t.status_code == 200 and t.json()["access_token"]


# ----------------------------------------------------------------------------- 2/3. capture gate
def test_upload_gate_rejects_bad_capture_and_accepts_good(client, monkeypatch):
    import validate_video
    register(client, "uploader@example.com")
    files = [("photos", (f"p{i}.jpg", _jpeg((40 * i, 90, 120)), "image/jpeg")) for i in range(4)]

    monkeypatch.setattr(validate_video, "validate_dir", lambda d, sample=10: {
        "verdict": "FAIL", "reasons": ["car not present in 7/10 frames"], "checks": {}, "frames": 4})
    r = client.post("/api/upload", data={"vin": "1HGCM82633A004352"}, files=files)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "capture_rejected"
    assert "car not present" in str(r.json()["detail"]["gate"])

    monkeypatch.setattr(validate_video, "validate_dir", lambda d, sample=10: {
        "verdict": "PASS", "reasons": [], "checks": {}, "frames": 4})
    r = client.post("/api/upload", data={"vin": "1HGCM82633A004352"}, files=files)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "uploaded" and r.json()["photo_count"] == 4


# ----------------------------------------------------------------------------- 5/6. public surfaces
def test_public_feed_and_pricing_are_reachable_anonymously(client):
    client.cookies.clear()
    feed = client.get("/api/feeds/google-vehicle-listings.xml", params={"token": FEED_TOKEN})
    assert feed.status_code == 200
    ET.fromstring(feed.content)  # well-formed XML
    p = client.get("/pricing")
    assert p.status_code == 200 and "$99" in p.text


# ----------------------------------------------------------------------------- 7. pricing single source
def test_price_has_one_source_of_truth(client, app_module):
    import billing
    plans = client.get("/api/plans").json()["plans"]
    assert plans["dealership"]["amount_cents"] == 9900
    assert app_module.PLANS["dealership"]["price"] == billing.plan()["amount"] == 99.0


# ----------------------------------------------------------------------------- 7. pays
def test_checkout_requires_sign_in(client, fake_stripe):
    client.cookies.clear()
    assert client.post("/api/billing/checkout").status_code == 401
    assert fake_stripe.created == []


def test_full_payment_flow_checkout_webhook_receipt(client, app_module, fake_stripe):
    email = "payer@example.com"
    register(client, email)

    # checkout session: subscription, the ONE configured price, tied to this account
    r = client.post("/api/billing/checkout")
    assert r.status_code == 200, r.text
    assert r.json()["url"].startswith("https://checkout.stripe.test/")
    kw = fake_stripe.created[-1]
    assert kw["mode"] == "subscription"
    assert kw["client_reference_id"] == email
    li = kw["line_items"][0]["price_data"]
    assert li["unit_amount"] == 9900 and li["recurring"] == {"interval": "month"}
    assert kw["success_url"].startswith("https://lotvue.test/billing/success?session_id=")

    # Stripe charges the card and notifies us (signed)
    ev = {"id": "evt_1", "type": "checkout.session.completed", "data": {"object": {
        "id": "cs_test_1", "mode": "subscription", "payment_status": "paid", "status": "complete",
        "client_reference_id": email, "customer": "cus_123", "subscription": "sub_123"}}}
    w = signed_webhook(client, ev)
    assert w.status_code == 200 and w.json()["outcome"] == "activated"
    u = _user(app_module, email)
    assert u["plan"] == "dealership" and u["is_subscribed"] == 1
    assert u["stripe_customer_id"] == "cus_123" and u["stripe_subscription_id"] == "sub_123"

    # replayed event is ignored (idempotent)
    assert signed_webhook(client, ev).json()["status"] == "duplicate"

    # invoice paid -> receipt recorded
    inv = {"id": "evt_2", "type": "invoice.paid", "data": {"object": {
        "id": "in_123", "customer": "cus_123", "amount_paid": 9900, "currency": "usd",
        "hosted_invoice_url": "https://invoice.stripe.test/in_123",
        "invoice_pdf": "https://invoice.stripe.test/in_123.pdf"}}}
    assert signed_webhook(client, inv).json()["outcome"] == "recorded"

    st = client.get("/api/billing/status").json()
    assert st["active_subscription"] is True
    assert st["receipts"][0]["amount"] == 99.0
    assert st["receipts"][0]["receipt_url"] == "https://invoice.stripe.test/in_123"
    page = client.get("/billing")
    assert page.status_code == 200 and "https://invoice.stripe.test/in_123" in page.text

    # portal for card updates / cancel
    assert client.post("/api/billing/portal").json()["url"] == "https://billing.stripe.test/portal"

    # cancellation drops back to free
    cancel = {"id": "evt_3", "type": "customer.subscription.deleted", "data": {"object": {
        "id": "sub_123", "customer": "cus_123", "status": "canceled"}}}
    assert signed_webhook(client, cancel).json()["outcome"] == "deactivated"
    u = _user(app_module, email)
    assert u["plan"] == "free" and u["is_subscribed"] == 0


def test_success_page_activates_from_server_side_session(client, app_module, fake_stripe):
    email = "redirect@example.com"
    register(client, email)
    sid = client.post("/api/billing/checkout").json()["session_id"]
    s = fake_stripe.sessions[sid]
    s.update(status="complete", payment_status="paid", customer="cus_r", subscription="sub_r",
             invoice="in_r")
    fake_stripe.invoices["in_r"] = {"id": "in_r", "status": "paid", "customer": "cus_r",
                                    "amount_paid": 9900, "currency": "usd",
                                    "hosted_invoice_url": "https://invoice.stripe.test/in_r"}
    page = client.get(f"/billing/success?session_id={sid}")
    assert page.status_code == 200
    assert "Unlimited vehicles are unlocked" in page.text and "https://invoice.stripe.test/in_r" in page.text
    assert _user(app_module, email)["plan"] == "dealership"


def test_webhook_rejects_forged_and_unsigned_events(client, app_module, monkeypatch):
    email = "victim@example.com"
    register(client, email)
    ev = {"id": "evt_forged", "type": "checkout.session.completed", "data": {"object": {
        "mode": "subscription", "payment_status": "paid", "client_reference_id": email}}}
    assert signed_webhook(client, ev, secret="whsec_attacker").status_code == 400
    assert client.post("/api/stripe/webhook", json=ev).status_code == 400
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET")
    monkeypatch.setattr(app_module, "STRIPE_WEBHOOK_SECRET", "")  # read once at import
    assert client.post("/api/stripe/webhook", json=ev).status_code == 503
    assert _user(app_module, email)["plan"] == "free"


def test_dealer_cannot_self_upgrade_via_set_plan(client, app_module):
    email = "sneaky@example.com"
    register(client, email)
    r = client.post("/api/set-plan", data={"email": email, "plan": "dealership"})
    assert r.status_code == 403
    assert _user(app_module, email)["plan"] == "free"


def test_paywall_after_free_vehicles_then_unlocked_by_payment(client, app_module, fake_stripe):
    email = "paywall@example.com"
    register(client, email)
    meta = {"year": "2019", "make": "Honda", "model": "Civic"}  # skip NHTSA lookup
    vins = ["2HGFC2F69KH500111", "2HGFC2F69KH500112", "2HGFC2F69KH500113"]
    assert client.post("/api/create-vehicle", data={"vin": vins[0], **meta}).status_code == 200
    assert client.post("/api/create-vehicle", data={"vin": vins[1], **meta}).status_code == 200
    # re-working an owned vehicle is always fine
    assert client.post("/api/create-vehicle", data={"vin": vins[0], **meta}).status_code == 200
    r = client.post("/api/create-vehicle", data={"vin": vins[2], **meta})
    assert r.status_code == 402
    assert r.json()["detail"]["upgrade_url"] == "/pricing"
    assert "$99" in r.json()["detail"]["message"]

    ev = {"id": "evt_pw", "type": "checkout.session.completed", "data": {"object": {
        "mode": "subscription", "payment_status": "paid", "client_reference_id": email,
        "customer": "cus_pw", "subscription": "sub_pw"}}}
    assert signed_webhook(client, ev).status_code == 200
    assert client.post("/api/create-vehicle", data={"vin": vins[2], **meta}).status_code == 200


def test_admin_can_comp_an_account(client, app_module):
    register(client, "comped@example.com")
    client.cookies.clear()
    register(client, "admin@lotvue.test")
    r = client.post("/api/set-plan", data={"email": "comped@example.com", "plan": "dealership"})
    assert r.status_code == 200
    assert _user(app_module, "comped@example.com")["plan"] == "dealership"
