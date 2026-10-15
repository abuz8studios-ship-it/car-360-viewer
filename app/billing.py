"""
LotVue360 billing — the ONE place pricing and payment logic live.

Stranger-dealer test step 7: "Pays — enters a card, is charged, gets a receipt."

Flow
  1. Signed-in dealer clicks Subscribe on /pricing or /billing
     -> POST /api/billing/checkout  -> Stripe Checkout (subscription mode)
  2. Dealer enters card on Stripe's hosted page, Stripe charges it
  3. Stripe redirects to /billing/success?session_id=...  (we re-fetch the
     session from Stripe server-side — never trust the redirect alone) and
     Stripe POSTs signed events to /api/stripe/webhook (main.py verifies the
     signature and applies handle_event() inside its idempotent transaction)
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
  LOTVUE_FREE_VEHICLES        (default 5 — free vehicles per account, for the life
                               of the account; deleting a vehicle never frees a
                               slot and re-adding one the dealer already had never
                               costs one. Counted from vehicle_claims, which a
                               SQLite trigger fills for every vehicle row attached
                               to an account, whatever code path created it.)
  Nothing else in the codebase may hard-code a price; it all reads plan().
  Nothing is sold until the owner sets LOTVUE_PRICING_APPROVED=1 as well as
  LOTVUE_FEATURE_PAYMENTS=1 (main.py passes that gate in register_billing).

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
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()

_DB_PATH: Path | None = None
_FEATURES: dict = {}
_PAYMENTS_AVAILABLE = None   # callable from main.py: payments flag AND owner-approved price
_PRICING_APPROVED = None     # callable from main.py: owner approved the price for publication

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


@contextmanager
def _conn(conn: sqlite3.Connection | None = None):
    """Use the caller's connection (and its transaction) or open a short one."""
    if conn is not None:
        yield conn
        return
    own = _db()
    try:
        yield own
        own.commit()
    finally:
        own.close()


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
    for col in ("receipt_url TEXT DEFAULT ''", "invoice_pdf TEXT DEFAULT ''",
                "currency TEXT DEFAULT 'usd'"):
        try:
            conn.execute(f"ALTER TABLE payments ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    # Lifetime free-tier ledger: one row per (account, VIN) ever attached.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS vehicle_claims (
            email TEXT NOT NULL, vin TEXT NOT NULL, claimed_at TEXT NOT NULL,
            PRIMARY KEY (email, vin));
        CREATE TRIGGER IF NOT EXISTS vehicle_claim_on_insert AFTER INSERT ON vehicles
        WHEN COALESCE(NEW.user_email, '') != ''
        BEGIN
            INSERT INTO vehicle_claims (email, vin, claimed_at)
            SELECT lower(NEW.user_email), upper(NEW.vin), datetime('now')
            WHERE NOT EXISTS (SELECT 1 FROM vehicle_claims
                              WHERE email = lower(NEW.user_email) AND vin = upper(NEW.vin));
        END;
        CREATE TRIGGER IF NOT EXISTS vehicle_claim_on_owner AFTER UPDATE OF user_email ON vehicles
        WHEN COALESCE(NEW.user_email, '') != ''
        BEGIN
            INSERT INTO vehicle_claims (email, vin, claimed_at)
            SELECT lower(NEW.user_email), upper(NEW.vin), datetime('now')
            WHERE NOT EXISTS (SELECT 1 FROM vehicle_claims
                              WHERE email = lower(NEW.user_email) AND vin = upper(NEW.vin));
        END;
        INSERT OR IGNORE INTO vehicle_claims (email, vin, claimed_at)
            SELECT lower(user_email), upper(vin), COALESCE(NULLIF(created_at, ''), datetime('now'))
            FROM vehicles WHERE COALESCE(user_email, '') != '';
    """)
    conn.commit()
    conn.close()


def _get_user(email: str, conn: sqlite3.Connection | None = None) -> dict | None:
    with _conn(conn) as c:
        row = c.execute("SELECT * FROM users WHERE lower(email)=?", (email.lower(),)).fetchone()
    return dict(row) if row else None


def _get_user_by_customer(customer_id: str, conn: sqlite3.Connection | None = None) -> dict | None:
    if not customer_id:
        return None
    with _conn(conn) as c:
        row = c.execute("SELECT * FROM users WHERE stripe_customer_id=?", (customer_id,)).fetchone()
    return dict(row) if row else None


def _user_for_stripe_object(obj: dict, conn: sqlite3.Connection | None = None) -> dict | None:
    """Match a Stripe customer, falling back to the email we stamped in metadata."""
    user = _get_user_by_customer(obj.get("customer", "") or "", conn)
    if user:
        return user
    email = str((obj.get("metadata") or {}).get("user_email") or obj.get("customer_email") or "")
    return _get_user(email, conn) if email.strip() else None


def _update_user(email: str, conn: sqlite3.Connection | None = None, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with _conn(conn) as c:
        c.execute(f"UPDATE users SET {cols} WHERE lower(email)=?", (*fields.values(), email.lower()))


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


def vehicles_claimed(email: str) -> int:
    """Vehicles this account has ever had (deleted ones included)."""
    conn = _db()
    try:
        n = conn.execute("SELECT COUNT(*) FROM vehicle_claims WHERE email=?",
                         (email.lower(),)).fetchone()[0]
    except sqlite3.OperationalError:
        n = 0
    conn.close()
    return n


def _owns_vehicle(email: str, vin: str) -> bool:
    """True if this account has or ever had this VIN (re-shoots never cost a slot)."""
    conn = _db()
    try:
        row = conn.execute(
            "SELECT 1 FROM vehicles WHERE vin=? AND lower(user_email)=? "
            "UNION SELECT 1 FROM vehicle_claims WHERE vin=? AND email=?",
            (vin.upper(), email.lower(), vin.upper(), email.lower())).fetchone()
    except sqlite3.OperationalError:
        row = None
    conn.close()
    return bool(row)


def free_slots_left(email: str) -> int | None:
    """Free vehicles this account may still add; None means no limit applies
    (payments off / price not approved, or an active Dealership plan)."""
    if not payments_enabled() or not email:
        return None
    if is_active(_get_user(email)):
        return None
    return max(0, free_vehicle_limit() - vehicles_claimed(email))


def payment_required_detail(email: str) -> dict:
    return {
        "error": "payment_required",
        "message": (f"You've used your {free_vehicle_limit()} free vehicles. "
                    f"Subscribe to the {plan()['label']} plan "
                    f"({plan()['display']}/{plan()['interval']}) for unlimited vehicles."),
        "upgrade_url": "/pricing",
        "entitlement": entitlement(email),
    }


def entitlement(email: str) -> dict:
    user = _get_user(email) if email else None
    active = is_active(user)
    used = vehicles_claimed(email) if email else 0
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
    if _PAYMENTS_AVAILABLE is not None:
        return bool(_PAYMENTS_AVAILABLE())
    return bool(_FEATURES.get("payments", False))


def pricing_approved() -> bool:
    return True if _PRICING_APPROVED is None else bool(_PRICING_APPROVED())


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
    require_quota_for(session_email(request), vin)


def require_quota_for(email: str, vin: str = ""):
    """Paywall for an already-authenticated dealer (see require_vehicle_quota)."""
    if not email:
        return  # auth gate already decides who may reach the route
    if vin and _owns_vehicle(email, vin):
        return
    left = free_slots_left(email)
    if left is None or left > 0:
        return
    raise HTTPException(status_code=402, detail=payment_required_detail(email))


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
                          status: str = "active", period_end: int | None = None,
                          conn: sqlite3.Connection | None = None):
    fields = {"plan": "dealership", "plan_type": "dealership", "is_subscribed": 1,
              "subscription_status": status or "active"}
    if customer_id:
        fields["stripe_customer_id"] = customer_id
    if subscription_id:
        fields["stripe_subscription_id"] = subscription_id
    if period_end:
        fields["current_period_end"] = datetime.fromtimestamp(int(period_end), timezone.utc).isoformat()
    _update_user(email, conn, **fields)
    print(f"[Billing] subscription ACTIVE for {email} ({subscription_id or 'no-sub-id'})")


def deactivate_subscription(email: str, status: str, conn: sqlite3.Connection | None = None):
    _update_user(email, conn, plan="free", plan_type="free", is_subscribed=0,
                 subscription_status=status)
    print(f"[Billing] subscription ENDED for {email} ({status})")


def record_invoice(invoice: dict, conn: sqlite3.Connection | None = None) -> bool:
    """Record a paid invoice as a payment row (idempotent on invoice id)."""
    inv_id = invoice.get("id", "")
    user = _user_for_stripe_object(invoice, conn)
    if not user or not inv_id:
        print(f"[Billing] invoice {inv_id} paid but no matching user (customer={invoice.get('customer')})")
        return False
    with _conn(conn) as c:
        exists = c.execute("SELECT 1 FROM payments WHERE stripe_payment_id=?", (inv_id,)).fetchone()
        if not exists:
            c.execute("""INSERT INTO payments (user_id, amount, currency, plan, credits_purchased,
                         stripe_payment_id, status, created_at, receipt_url, invoice_pdf)
                         VALUES (?, ?, ?, 'dealership', 0, ?, 'paid', ?, ?, ?)""",
                      (user["id"], (invoice.get("amount_paid") or 0) / 100,
                       invoice.get("currency", "usd"), inv_id, datetime.now(timezone.utc).isoformat(),
                       invoice.get("hosted_invoice_url") or "", invoice.get("invoice_pdf") or ""))
    return not exists


def _email_from_session(sess: dict) -> str:
    return ((sess.get("client_reference_id")
             or (sess.get("metadata") or {}).get("user_email")
             or (sess.get("customer_details") or {}).get("email")
             or sess.get("customer_email") or "")).lower()


def handle_event(event: dict, conn: sqlite3.Connection | None = None) -> str:
    """Apply one verified Stripe event. Returns a short outcome string.

    main.py calls this inside its webhook transaction (passing conn), after the
    signature check and the event-id idempotency row."""
    etype = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}

    if etype == "checkout.session.completed":
        if obj.get("mode") != "subscription":
            return "ignored:non-subscription-checkout"
        if obj.get("payment_status") not in ("paid", "no_payment_required"):
            return "ignored:unpaid"
        email = _email_from_session(obj)
        if not email or not _get_user(email, conn):
            return "ignored:unknown-user"
        activate_subscription(email, obj.get("customer") or "", obj.get("subscription") or "",
                              conn=conn)
        return "activated"

    if etype in ("invoice.paid", "invoice.payment_succeeded"):
        return "recorded" if record_invoice(obj, conn) else "duplicate-or-unmatched"

    if etype == "invoice.payment_failed":
        user = _user_for_stripe_object(obj, conn)
        if user:
            _update_user(user["email"], conn, subscription_status="past_due")
            return "past_due"
        return "ignored:unknown-customer"

    if etype in ("customer.subscription.updated", "customer.subscription.deleted",
                 "customer.subscription.created"):
        user = _user_for_stripe_object(obj, conn)
        if not user:
            return "ignored:unknown-customer"
        status = obj.get("status", "")
        if etype == "customer.subscription.deleted" or status in ENDED_STATUSES:
            deactivate_subscription(user["email"], status or "canceled", conn)
            return "deactivated"
        period_end = obj.get("current_period_end") or (
            ((obj.get("items") or {}).get("data") or [{}])[0].get("current_period_end"))
        if status in ACTIVE_STATUSES:
            activate_subscription(user["email"], obj.get("customer", ""), obj.get("id", ""),
                                  status, period_end, conn)
            return f"status:{status}"
        _update_user(user["email"], conn, subscription_status=status)
        return f"status:{status}"

    return "ignored"


# =============================================================================
# ROUTES
# =============================================================================
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
        raise HTTPException(503, "Payments are turned off on this server (payments not enabled or price not approved).")
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


def comp_plan(target: str, new_plan: str) -> dict:
    """Admin plan override (comp an account); main.py's /api/set-plan checks is_admin."""
    target = (target or "").strip().lower()
    new_plan = (new_plan or "free").strip()
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
    if not pricing_approved():
        body = """<h1>Pricing is being finalized</h1>
<p class="sub">LotVue360 is free while pricing is finalized. Create an account and start
building 360° spins today.</p>
<div class="card"><a class="btn" style="max-width:320px" href="/register">Start free</a></div>"""
        return _page("Pricing", body, bool(email))
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


def register_billing(app, db_path: Path, features: dict,
                     payments_available=None, pricing_approved=None):
    global _DB_PATH, _FEATURES, _PAYMENTS_AVAILABLE, _PRICING_APPROVED
    _DB_PATH = Path(db_path)
    _FEATURES = features
    _PAYMENTS_AVAILABLE = payments_available
    _PRICING_APPROVED = pricing_approved
    migrate()
    app.include_router(router)
    p = plan()
    print(f"[Billing] registered: {p['display']}/{p['interval']} Dealership plan, "
          f"{free_vehicle_limit()} free vehicles, payments={'ON' if payments_enabled() else 'OFF'}")
