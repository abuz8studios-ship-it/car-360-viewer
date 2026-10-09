"""
LotVue360 billing — the ONE place pricing and payment logic live.

Stranger-dealer test step 7: "Pays — enters a card, is charged, gets a receipt."

Flow
  1. Signed-in dealer clicks Subscribe on /pricing or /billing
     -> POST /api/billing/checkout  -> Stripe Checkout (subscription mode)
  2. Dealer enters card on Stripe's hosted page, Stripe charges it
  3. Stripe redirects to /billing/success?session_id=...  (we re-fetch the
     session from Stripe server-side — never trust the redirect alone) and
     Stripe POSTs signed events to /api/stripe/webhook
  4. Either path activates the plan (idempotent). invoice.paid records the
     payment with Stripe's hosted receipt/invoice URL, shown on /billing.
     Stripe also emails the receipt (enable "Successful payments" under
     Stripe Dashboard -> Settings -> Customer emails).
  5. /api/billing/portal opens Stripe's Customer Portal (update card, cancel,
     download past invoices). Cancellation arrives as
     customer.subscription.deleted and drops the account back to Free.

Price — single source of truth
  LOTVUE_PRICE_MONTHLY_CENTS  (default 9900 = $99/mo, the target in BRAIN.md /
                               LOTVUE_GOAL_ARTIFACT.md)
  LOTVUE_FREE_VEHICLES        (default 5 — free vehicles before the paywall)
  Nothing else in the codebase may hard-code a price; it all reads plan().

Security
  - The webhook REFUSES unsigned/unverifiable events. (The previous handler
    accepted raw JSON when STRIPE_WEBHOOK_SECRET was unset, which let anyone
    POST a fake checkout.session.completed and grant themselves credits.)
  - Checkout/portal identify the dealer from their JWT session, never from a
    form field.
  - Plan changes only happen via verified Stripe data (webhook or a
    server-side session retrieve), or by an admin (LOTVUE_ADMIN_EMAILS).
"""
from __future__ import annotations

import html
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()

_DB_PATH: Path | None = None
_FEATURES: dict = {}

ACTIVE_STATUSES = {"active", "trialing", "past_due"}  # past_due = Stripe still retrying
ENDED_STATUSES = {"canceled", "unpaid", "incomplete_expired"}


# =============================================================================
# PRICING — single source of truth
# =============================================================================
def plan() -> dict:
    cents = int(os.getenv("LOTVUE_PRICE_MONTHLY_CENTS", "9900"))
    return {
        "id": "dealership",
        "label": "Dealership",
        "amount_cents": cents,
        "amount": cents / 100,
        "display": f"${cents / 100:,.0f}" if cents % 100 == 0 else f"${cents / 100:,.2f}",
        "currency": "usd",
        "interval": "month",
        "features": [
            "Unlimited vehicles",
            "360° studio spins with background replacement",
            "Public VDP + embed for every car",
            "Google Vehicle Listings feed",
            "Hotspots + printable damage report",
            "Cancel anytime from your billing page",
        ],
    }


def free_vehicle_limit() -> int:
    return int(os.getenv("LOTVUE_FREE_VEHICLES", "5"))


def catalog() -> dict:
    """Public plan list (used by /api/plans and the pricing page)."""
    return {
        "free": {
            "id": "free",
            "label": "Free",
            "amount_cents": 0,
            "amount": 0,
            "display": "$0",
            "interval": None,
            "vehicles": free_vehicle_limit(),
            "features": [f"{free_vehicle_limit()} vehicles free", "Full 360 spin + VDP",
                         "No credit card required"],
        },
        "dealership": plan(),
    }


# =============================================================================
# DB
# =============================================================================
def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def migrate():
    conn = _db()
    for col in ("plan_type TEXT DEFAULT 'free'",
                "is_subscribed INTEGER DEFAULT 0",
                "stripe_subscription_id TEXT DEFAULT ''",
                "subscription_status TEXT DEFAULT ''",
                "current_period_end TEXT DEFAULT ''"):
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    for col in ("receipt_url TEXT DEFAULT ''", "invoice_pdf TEXT DEFAULT ''"):
        try:
            conn.execute(f"ALTER TABLE payments ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    conn.execute("""CREATE TABLE IF NOT EXISTS stripe_events (
        id TEXT PRIMARY KEY, type TEXT, received_at TEXT)""")
    conn.commit()
    conn.close()


def _get_user(email: str) -> dict | None:
    conn = _db()
    row = conn.execute("SELECT * FROM users WHERE lower(email)=?", (email.lower(),)).fetchone()
    conn.close()
    return dict(row) if row else None


def _get_user_by_customer(customer_id: str) -> dict | None:
    if not customer_id:
        return None
    conn = _db()
    row = conn.execute("SELECT * FROM users WHERE stripe_customer_id=?", (customer_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _update_user(email: str, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    conn = _db()
    conn.execute(f"UPDATE users SET {cols} WHERE lower(email)=?", (*fields.values(), email.lower()))
    conn.commit()
    conn.close()


def is_active(user: dict | None) -> bool:
    if not user:
        return False
    if (user.get("plan_type") in ("dealership", "enterprise")
            and (user.get("subscription_status") or "active") in ACTIVE_STATUSES):
        return True
    # owner / admin-comped accounts (no Stripe subscription, plan set directly)
    if user.get("plan") == "dealership" and not user.get("stripe_subscription_id"):
        return True
    return bool(user.get("is_subscribed")) and user.get("subscription_status") in ACTIVE_STATUSES


def vehicles_owned(email: str) -> int:
    conn = _db()
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM vehicles WHERE lower(user_email)=? AND COALESCE(status,'active')!='deleted'",
            (email.lower(),)).fetchone()[0]
    except sqlite3.OperationalError:
        n = 0
    conn.close()
    return n


def _owns_vehicle(email: str, vin: str) -> bool:
    conn = _db()
    try:
        row = conn.execute("SELECT 1 FROM vehicles WHERE vin=? AND lower(user_email)=?",
                           (vin.upper(), email.lower())).fetchone()
    except sqlite3.OperationalError:
        row = None
    conn.close()
    return bool(row)


def entitlement(email: str) -> dict:
    user = _get_user(email) if email else None
    active = is_active(user)
    used = vehicles_owned(email) if email else 0
    limit = free_vehicle_limit()
    return {
        "email": email,
        "plan": "dealership" if active else "free",
        "active_subscription": active,
        "subscription_status": (user or {}).get("subscription_status", ""),
        "vehicles_used": used,
        "free_vehicle_limit": limit,
        "can_add_vehicle": active or used < limit or not payments_enabled(),
        "payments_enabled": payments_enabled(),
    }


def payments_enabled() -> bool:
    return bool(_FEATURES.get("payments", False))


# =============================================================================
# AUTH helpers (JWT cookie / bearer — same token the auth gate checks)
# =============================================================================
def session_email(request: Request) -> str:
    from a2_extensions import _jwt_secret, JWT_ALG
    from jose import jwt, JWTError
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else request.cookies.get("lotvue_token", "")
    if not token:
        return ""
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALG])
    except JWTError:
        return ""
    if payload.get("type") != "access":
        return ""
    return (payload.get("sub") or "").lower()


def _require_email(request: Request) -> str:
    email = session_email(request)
    if not email:
        raise HTTPException(401, "Sign in required")
    return email


def is_admin(email: str) -> bool:
    admins = {e.strip().lower() for e in os.getenv("LOTVUE_ADMIN_EMAILS", "").split(",") if e.strip()}
    owner = os.getenv("LOTVUE_OWNER_EMAIL", "").strip().lower()
    if owner:
        admins.add(owner)
    return bool(email) and email.lower() in admins


def require_vehicle_quota(request: Request, vin: str = ""):
    """Paywall. Call from any route that creates a NEW vehicle.
    Raises 402 with an upgrade URL when a free dealer has used their free vehicles.
    Re-uploading to a VIN the dealer already owns is always allowed."""
    if not payments_enabled():
        return
    email = session_email(request)
    if not email:
        return  # auth gate already decides who may reach the route
    if vin and _owns_vehicle(email, vin):
        return
    ent = entitlement(email)
    if not ent["can_add_vehicle"]:
        raise HTTPException(status_code=402, detail={
            "error": "payment_required",
            "message": (f"You've used your {ent['free_vehicle_limit']} free vehicles. "
                        f"Subscribe to the {plan()['label']} plan "
                        f"({plan()['display']}/{plan()['interval']}) for unlimited vehicles."),
            "upgrade_url": "/pricing",
            "entitlement": ent,
        })


# =============================================================================
# STRIPE
# =============================================================================
def _stripe():
    """Return the configured stripe module (separate function so tests can patch it)."""
    import stripe
    key = os.getenv("STRIPE_SECRET_KEY", "")
    if not key or key.startswith("sk_live_or_test"):
        raise HTTPException(503, "Payments are not configured yet (STRIPE_SECRET_KEY missing).")
    stripe.api_key = key
    return stripe


def _as_dict(obj) -> dict:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return json.loads(str(obj))


def _base_url(request: Request) -> str:
    env = os.getenv("LOTVUE_PUBLIC_URL", "").strip().rstrip("/")
    if env:
        return env
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def _stripe_error_message(e: Exception) -> str:
    """Log the real Stripe error server-side; give the dealer a clean message."""
    msg = getattr(e, "user_message", None) or str(e)
    print(f"[Billing] Stripe error: {type(e).__name__}: {msg}")
    if "Expired API Key" in msg or "Invalid API Key" in msg:
        return "Payments are temporarily unavailable (Stripe key needs to be renewed by the site owner)."
    return "Payments are temporarily unavailable — please try again in a few minutes."


def activate_subscription(email: str, customer_id: str = "", subscription_id: str = "",
                          status: str = "active", period_end: int | None = None):
    fields = {"plan": "dealership", "plan_type": "dealership", "is_subscribed": 1,
              "subscription_status": status or "active"}
    if customer_id:
        fields["stripe_customer_id"] = customer_id
    if subscription_id:
        fields["stripe_subscription_id"] = subscription_id
    if period_end:
        fields["current_period_end"] = datetime.fromtimestamp(int(period_end), timezone.utc).isoformat()
    _update_user(email, **fields)
    print(f"[Billing] subscription ACTIVE for {email} ({subscription_id or 'no-sub-id'})")


def deactivate_subscription(email: str, status: str):
    _update_user(email, plan="free", plan_type="free", is_subscribed=0, subscription_status=status)
    print(f"[Billing] subscription ENDED for {email} ({status})")


def record_invoice(invoice: dict) -> bool:
    """Record a paid invoice as a payment row (idempotent on invoice id)."""
    inv_id = invoice.get("id", "")
    user = (_get_user_by_customer(invoice.get("customer", "") or "")
            or (_get_user(invoice.get("customer_email") or "") if invoice.get("customer_email") else None))
    if not user or not inv_id:
        print(f"[Billing] invoice {inv_id} paid but no matching user (customer={invoice.get('customer')})")
        return False
    conn = _db()
    exists = conn.execute("SELECT 1 FROM payments WHERE stripe_payment_id=?", (inv_id,)).fetchone()
    if not exists:
        conn.execute("""INSERT INTO payments (user_id, amount, currency, plan, credits_purchased,
                        stripe_payment_id, status, created_at, receipt_url, invoice_pdf)
                        VALUES (?, ?, ?, 'dealership', 0, ?, 'paid', ?, ?, ?)""",
                     (user["id"], (invoice.get("amount_paid") or 0) / 100,
                      invoice.get("currency", "usd"), inv_id, datetime.now(timezone.utc).isoformat(),
                      invoice.get("hosted_invoice_url") or "", invoice.get("invoice_pdf") or ""))
        conn.commit()
    conn.close()
    return not exists


def _email_from_session(sess: dict) -> str:
    return ((sess.get("client_reference_id")
             or (sess.get("metadata") or {}).get("user_email")
             or (sess.get("customer_details") or {}).get("email")
             or sess.get("customer_email") or "")).lower()


def handle_event(event: dict) -> str:
    """Apply one verified Stripe event. Returns a short outcome string."""
    etype = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}

    if etype == "checkout.session.completed":
        if obj.get("mode") != "subscription":
            return "ignored:non-subscription-checkout"
        if obj.get("payment_status") not in ("paid", "no_payment_required"):
            return "ignored:unpaid"
        email = _email_from_session(obj)
        if not email or not _get_user(email):
            return "ignored:unknown-user"
        activate_subscription(email, obj.get("customer") or "", obj.get("subscription") or "")
        return "activated"

    if etype in ("invoice.paid", "invoice.payment_succeeded"):
        return "recorded" if record_invoice(obj) else "duplicate-or-unmatched"

    if etype == "invoice.payment_failed":
        user = _get_user_by_customer(obj.get("customer", ""))
        if user:
            _update_user(user["email"], subscription_status="past_due")
            return "past_due"
        return "ignored:unknown-customer"

    if etype in ("customer.subscription.updated", "customer.subscription.deleted",
                 "customer.subscription.created"):
        user = _get_user_by_customer(obj.get("customer", ""))
        if not user:
            return "ignored:unknown-customer"
        status = obj.get("status", "")
        if etype == "customer.subscription.deleted" or status in ENDED_STATUSES:
            deactivate_subscription(user["email"], status or "canceled")
            return "deactivated"
        period_end = obj.get("current_period_end") or (
            ((obj.get("items") or {}).get("data") or [{}])[0].get("current_period_end"))
        if status in ACTIVE_STATUSES:
            activate_subscription(user["email"], obj.get("customer", ""), obj.get("id", ""),
                                  status, period_end)
            return f"status:{status}"
        _update_user(user["email"], subscription_status=status)
        return f"status:{status}"

    return "ignored"


# =============================================================================
# ROUTES
# =============================================================================
@router.get("/api/plans")
async def api_plans():
    return {"plans": catalog(), "payments_enabled": payments_enabled()}


@router.get("/api/stripe/config")
async def stripe_config():
    return {"publishable_key": os.getenv("STRIPE_PUBLISHABLE_KEY", ""),
            "payments_enabled": payments_enabled()}


@router.get("/api/billing/status")
async def billing_status(request: Request):
    email = _require_email(request)
    ent = entitlement(email)
    conn = _db()
    user = _get_user(email) or {}
    rows = conn.execute("""SELECT amount, currency, status, created_at, receipt_url, invoice_pdf,
                           stripe_payment_id FROM payments WHERE user_id=? ORDER BY id DESC""",
                        (user.get("id", -1),)).fetchall()
    conn.close()
    ent["current_period_end"] = user.get("current_period_end", "")
    ent["receipts"] = [dict(r) for r in rows]
    ent["price"] = plan()
    return ent


@router.post("/api/billing/checkout")
async def billing_checkout(request: Request):
    if not payments_enabled():
        raise HTTPException(503, "Payments are turned off on this server (LOTVUE_FEATURE_PAYMENTS=0).")
    email = _require_email(request)
    user = _get_user(email)
    if not user:
        raise HTTPException(401, "Account not found — sign in again")
    if is_active(user):
        return {"status": "already_subscribed", "url": "/billing"}
    stripe = _stripe()
    p = plan()
    base = _base_url(request)
    try:
        params = dict(
            mode="subscription",
            line_items=[{
                "price_data": {
                    "currency": p["currency"],
                    "unit_amount": p["amount_cents"],
                    "recurring": {"interval": p["interval"]},
                    "product_data": {"name": f"LotVue360 {p['label']} plan"},
                },
                "quantity": 1,
            }],
            client_reference_id=email,
            metadata={"user_email": email, "plan_id": p["id"]},
            subscription_data={"metadata": {"user_email": email, "plan_id": p["id"]}},
            success_url=f"{base}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base}/pricing?canceled=1",
            allow_promotion_codes=True,
        )
        if user.get("stripe_customer_id"):
            params["customer"] = user["stripe_customer_id"]
        else:
            params["customer_email"] = email
        session = _as_dict(stripe.checkout.Session.create(**params))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, _stripe_error_message(e))
    return {"status": "ok", "url": session.get("url"), "session_id": session.get("id")}


@router.post("/api/billing/portal")
async def billing_portal(request: Request):
    email = _require_email(request)
    user = _get_user(email) or {}
    if not user.get("stripe_customer_id"):
        raise HTTPException(400, "No billing account yet — subscribe first.")
    stripe = _stripe()
    try:
        sess = _as_dict(stripe.billing_portal.Session.create(
            customer=user["stripe_customer_id"], return_url=f"{_base_url(request)}/billing"))
    except Exception as e:
        raise HTTPException(502, _stripe_error_message(e))
    return {"status": "ok", "url": sess.get("url")}


@router.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    if not secret or secret.startswith("whsec_here"):
        # Never process unsigned events — that would let anyone forge a payment.
        return JSONResponse({"error": "Webhook signing secret not configured"}, status_code=503)
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    import stripe
    try:
        stripe.WebhookSignature.verify_header(payload, sig, secret, 300)
    except Exception:
        return JSONResponse({"error": "Invalid signature"}, status_code=400)
    try:
        event = json.loads(payload)
    except ValueError:
        return JSONResponse({"error": "Invalid payload"}, status_code=400)

    conn = _db()
    try:
        conn.execute("INSERT INTO stripe_events (id, type, received_at) VALUES (?, ?, ?)",
                     (event.get("id", ""), event.get("type", ""), datetime.now(timezone.utc).isoformat()))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return {"status": "duplicate"}
    conn.close()

    try:
        outcome = handle_event(event)
    except Exception as e:
        # let Stripe retry: forget the event id so the retry is processed
        conn = _db()
        conn.execute("DELETE FROM stripe_events WHERE id=?", (event.get("id", ""),))
        conn.commit()
        conn.close()
        print(f"[Billing] webhook handler error on {event.get('type')}: {e}")
        return JSONResponse({"error": "handler error"}, status_code=500)
    print(f"[Billing] webhook {event.get('type')} -> {outcome}")
    return {"status": "ok", "outcome": outcome}


@router.post("/api/set-plan")
async def set_plan(request: Request):
    """Admin-only manual plan override (comp an account). Dealers pay via Checkout."""
    email = _require_email(request)
    if not is_admin(email):
        raise HTTPException(403, "Plan changes happen through checkout. Visit /pricing.")
    form = await request.form()
    target = (form.get("email") or "").strip().lower()
    new_plan = (form.get("plan") or "free").strip()
    if new_plan not in ("free", "dealership") or not _get_user(target):
        raise HTTPException(400, "Unknown user or plan")
    if new_plan == "dealership":
        _update_user(target, plan="dealership", plan_type="dealership", is_subscribed=1,
                     subscription_status="active")
    else:
        deactivate_subscription(target, "admin_downgrade")
    return {"status": "ok", "email": target, "plan": new_plan}


# ----------------------------------------------------------------------------- pages
_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'DM Sans','Inter','Segoe UI',system-ui,sans-serif;background:#FAF8F5;color:#2C2A26;min-height:100vh}
.hdr{display:flex;justify-content:space-between;align-items:center;padding:1rem 2rem;border-bottom:1px solid #ECE6DA;background:#fff}
.logo{font-weight:700;font-size:1.3rem;color:#2C2A26}.logo span{color:#C8A55C}
.hdr a{color:#8A6D2F;text-decoration:none;margin-left:1.2rem;font-weight:500}
.wrap{max-width:960px;margin:0 auto;padding:3rem 1.5rem}
h1{font-size:2.1rem;margin-bottom:.6rem}.sub{color:#6B665C;margin-bottom:2.4rem;font-size:1.05rem}
.plans{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1.5rem}
.card{background:#fff;border:1px solid #ECE6DA;border-radius:14px;padding:2rem}
.card.feat{border:2px solid #C8A55C;box-shadow:0 8px 30px rgba(200,165,92,.15)}
.badge{display:inline-block;background:#C8A55C;color:#fff;border-radius:20px;padding:.15rem .7rem;font-size:.72rem;font-weight:700;margin-bottom:.6rem}
.name{font-weight:600;font-size:1.15rem;margin-bottom:.4rem}.price{font-size:2.4rem;font-weight:700}
.price small{font-size:1rem;color:#6B665C;font-weight:400}
ul{list-style:none;margin:1.2rem 0 1.6rem}li{padding:.3rem 0 .3rem 1.4rem;position:relative;color:#4A463E}
li:before{content:'✓';position:absolute;left:0;color:#C8A55C;font-weight:700}
.btn{display:block;width:100%;text-align:center;padding:.85rem;border-radius:9px;border:none;background:#C8A55C;color:#fff;font-weight:600;font-size:1rem;cursor:pointer;text-decoration:none}
.btn:hover{background:#B39047}.btn.ghost{background:#fff;color:#8A6D2F;border:1px solid #C8A55C}
.btn[disabled]{opacity:.6;cursor:wait}
.note{margin-top:1rem;padding:.9rem 1rem;border-radius:9px;font-size:.92rem;display:none}
.note.err{display:block;background:#FDECEA;color:#9B2C1F;border:1px solid #F5C2BC}
.note.ok{display:block;background:#EAF6EF;color:#1F6B3A;border:1px solid #BFE3CC}
table{width:100%;border-collapse:collapse;margin-top:1rem;font-size:.92rem}
th,td{text-align:left;padding:.6rem;border-bottom:1px solid #ECE6DA}th{color:#6B665C;font-weight:500}
td a{color:#8A6D2F}
.muted{color:#6B665C;font-size:.9rem}
"""

_SUBSCRIBE_JS = """
async function subscribe(btn){
  const note=document.getElementById('note');note.className='note';
  btn.disabled=true;const t=btn.textContent;btn.textContent='Opening secure checkout…';
  try{
    const r=await fetch('/api/billing/checkout',{method:'POST',credentials:'same-origin'});
    if(r.status===401){location.href='/register?next=/pricing';return;}
    const d=await r.json();
    if(d.url){location.href=d.url;return;}
    throw new Error(d.detail||'Checkout unavailable');
  }catch(e){note.textContent=e.message;note.className='note err';btn.disabled=false;btn.textContent=t;}
}
async function portal(btn){
  const note=document.getElementById('note');note.className='note';btn.disabled=true;
  try{const r=await fetch('/api/billing/portal',{method:'POST',credentials:'same-origin'});
    const d=await r.json();if(d.url){location.href=d.url;return;}throw new Error(d.detail||'Unavailable');}
  catch(e){note.textContent=e.message;note.className='note err';btn.disabled=false;}
}
"""


def _page(title: str, body: str, signed_in: bool) -> HTMLResponse:
    nav = ('<a href="/dashboard">Dashboard</a><a href="/billing">Billing</a>' if signed_in
           else '<a href="/login">Sign in</a><a href="/register">Create account</a>')
    return HTMLResponse(f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)} — LotVue360</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{_CSS}</style></head><body>
<div class="hdr"><div class="logo">LotVue<span>360</span></div><div>{nav}</div></div>
<div class="wrap">{body}</div><script>{_SUBSCRIBE_JS}</script></body></html>""")


@router.get("/pricing")
async def pricing_page(request: Request, canceled: str = ""):
    email = session_email(request)
    active = is_active(_get_user(email)) if email else False
    p, f = plan(), catalog()["free"]
    feat = "".join(f"<li>{html.escape(x)}</li>" for x in p["features"])
    free_feat = "".join(f"<li>{html.escape(x)}</li>" for x in f["features"])
    if active:
        cta = '<a class="btn ghost" href="/billing">You\'re subscribed — manage billing</a>'
    elif email:
        cta = '<button class="btn" onclick="subscribe(this)">Subscribe — card checkout</button>'
    else:
        cta = '<a class="btn" href="/register?next=/pricing">Create account to subscribe</a>'
    free_cta = ('<a class="btn ghost" href="/dashboard">Go to dashboard</a>' if email
                else '<a class="btn ghost" href="/register">Start free</a>')
    banner = ('<div class="note err" style="display:block;margin-bottom:1.5rem">Checkout canceled — '
              'you were not charged.</div>' if canceled else "")
    off = ("" if payments_enabled() else
           '<p class="muted" style="margin-top:1.5rem">Online payments are not enabled on this server yet.</p>')
    body = f"""{banner}<h1>Simple pricing for independent dealers</h1>
<p class="sub">Start free. Upgrade when your lot needs more. No contracts.</p>
<div class="plans">
 <div class="card"><div class="name">Free</div><div class="price">$0</div>
  <ul>{free_feat}</ul>{free_cta}</div>
 <div class="card feat"><div class="badge">FOR DEALERS</div><div class="name">{html.escape(p['label'])}</div>
  <div class="price" id="plan-price">{p['display']}<small>/{p['interval']}</small></div>
  <ul>{feat}</ul>{cta}<div id="note" class="note"></div></div>
</div>{off}
<p class="muted" style="margin-top:2rem">Payments are processed securely by Stripe. You'll get an emailed
receipt for every charge, and every invoice is downloadable from your billing page.</p>"""
    return _page("Pricing", body, bool(email))


@router.get("/billing")
async def billing_page(request: Request):
    email = session_email(request)
    if not email:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/login?next=/billing", status_code=302)
    st = await billing_status(request)
    p = plan()
    if st["active_subscription"]:
        status = (f"<p><b>{html.escape(p['label'])}</b> plan — {p['display']}/{p['interval']} · "
                  f"status: {html.escape(st['subscription_status'] or 'active')}</p>")
        if st.get("current_period_end"):
            status += f'<p class="muted">Renews {html.escape(st["current_period_end"][:10])}</p>'
        actions = '<button class="btn ghost" style="max-width:320px;margin-top:1rem" onclick="portal(this)">Update card / cancel / invoices</button>'
    else:
        status = (f"<p><b>Free</b> plan — {st['vehicles_used']} of {st['free_vehicle_limit']} free vehicles used.</p>")
        actions = (f'<button class="btn" style="max-width:320px;margin-top:1rem" onclick="subscribe(this)">'
                   f'Subscribe — {p["display"]}/{p["interval"]}</button>')
    rows = "".join(
        f"<tr><td>{html.escape((r['created_at'] or '')[:10])}</td><td>${r['amount']:,.2f} {html.escape((r['currency'] or 'usd').upper())}</td>"
        f"<td>{html.escape(r['status'] or '')}</td><td>"
        + (f'<a href="{html.escape(r["receipt_url"])}" target="_blank" rel="noopener">Receipt</a>' if r.get("receipt_url") else "")
        + (f' · <a href="{html.escape(r["invoice_pdf"])}" target="_blank" rel="noopener">PDF</a>' if r.get("invoice_pdf") else "")
        + "</td></tr>" for r in st["receipts"])
    receipts = (f"<table><tr><th>Date</th><th>Amount</th><th>Status</th><th>Receipt</th></tr>{rows}</table>"
                if rows else '<p class="muted">No payments yet.</p>')
    body = f"""<h1>Billing</h1><p class="sub">{html.escape(email)}</p>
<div class="card">{status}{actions}<div id="note" class="note"></div></div>
<h2 style="margin:2rem 0 .5rem;font-size:1.3rem">Receipts</h2><div class="card">{receipts}</div>"""
    return _page("Billing", body, True)


@router.get("/billing/success")
async def billing_success(request: Request, session_id: str = ""):
    email = session_email(request)
    if not email:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(f"/login?next=/billing/success%3Fsession_id%3D{session_id}", status_code=302)
    receipt = ""
    msg = "Payment received — your plan is being activated. This page will update shortly."
    try:
        stripe = _stripe()
        sess = _as_dict(stripe.checkout.Session.retrieve(session_id))
        if (_email_from_session(sess) == email and sess.get("status") == "complete"
                and sess.get("payment_status") in ("paid", "no_payment_required")):
            activate_subscription(email, sess.get("customer") or "", sess.get("subscription") or "")
            msg = "You're subscribed. Unlimited vehicles are unlocked."
            if sess.get("invoice"):
                inv = _as_dict(stripe.Invoice.retrieve(sess["invoice"]))
                if inv.get("status") == "paid":
                    record_invoice(inv)
                if inv.get("hosted_invoice_url"):
                    receipt = (f'<p style="margin-top:1rem"><a class="btn ghost" style="max-width:320px" '
                               f'href="{html.escape(inv["hosted_invoice_url"])}" target="_blank" rel="noopener">View receipt</a></p>')
        elif _email_from_session(sess) and _email_from_session(sess) != email:
            msg = "This checkout belongs to a different account."
    except HTTPException:
        pass
    except Exception as e:
        print(f"[Billing] success-page session check failed: {e}")
    body = f"""<h1>Thank you!</h1><div class="card"><p>{html.escape(msg)}</p>{receipt}
<p class="muted" style="margin-top:1rem">A receipt has been emailed to {html.escape(email)} by Stripe.</p>
<p style="margin-top:1.5rem"><a class="btn" style="max-width:320px" href="/dashboard">Back to dashboard</a></p></div>"""
    return _page("Subscribed", body, True)


def register_billing(app, db_path: Path, features: dict):
    global _DB_PATH, _FEATURES
    _DB_PATH = Path(db_path)
    _FEATURES = features
    migrate()
    app.include_router(router)
    p = plan()
    print(f"[Billing] registered: {p['display']}/{p['interval']} Dealership plan, "
          f"{free_vehicle_limit()} free vehicles, payments={'ON' if payments_enabled() else 'OFF'}")
