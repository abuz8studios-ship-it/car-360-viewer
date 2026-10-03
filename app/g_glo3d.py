"""
G-Phase — Complete GLO3D upgrade for LotVue360.
Everything GLO3D bundles that LotVue was missing, built better + cheaper:

  Lead Magnets (buyer-facing, feed the lead pipeline):
    • Trade-in valuation tool      POST /api/leadmagnet/tradein
    • Price Advisor badge (public) GET  /api/leadmagnet/{vin}/price-advisor
    • Request vehicle history      POST /api/leadmagnet/request-history
    • Book a test drive            GET  /api/leadmagnet/test-drive-slots · POST /api/leadmagnet/book-test-drive
    • Request video walkaround     POST /api/leadmagnet/request-video
  AI BDC chatbot (their $944 flagship):
    • 24/7 vehicle-aware assistant POST /api/chat/{vin}
  Virtual Showroom microsite (Local SEO product):
    • Branded dealer storefront    GET  /showroom/{dealer}
    • Inventory JSON               GET  /api/showroom/{dealer}/inventory
  Merchandising paperwork dealers expect:
    • Window sticker PDF (gated)   GET  /api/vehicle/{vin}/window-sticker.pdf
    • FTC Buyers Guide PDF (gated) GET  /api/vehicle/{vin}/buyers-guide.pdf

Wired from main.py:  from g_glo3d import register_glo3d; register_glo3d(app)
"""

import html
import io
import json
import os
import re
import sqlite3
import urllib.request
from datetime import datetime
from typing import Literal, Optional
from urllib.parse import urlsplit

from app_paths import DB_PATH, STORAGE_DIR
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

router = APIRouter()

# ── brand palette (VISUAL LAW) ──────────────────────────────────────────────
BG = "#F8FAFC"; PRIMARY = "#2E75B6"; TEXT = "#2C3E50"; CARD = "#FFFFFF"; MUTED = "#7A8CA0"

# Depreciation model — kept consistent with main.py's VIN-decode pricing so
# numbers match everywhere in the product.
_BODY_BASE = {
    "sedan": 18000, "suv": 26000, "truck": 32000, "coupe": 24000, "hatchback": 16000,
    "convertible": 30000, "van": 22000, "wagon": 19000, "minivan": 24000,
}
_MAKE_PREMIUM = {
    "toyota": 1.05, "honda": 1.05, "lexus": 1.2, "bmw": 1.1, "mercedes-benz": 1.2,
    "audi": 1.1, "ford": 0.95, "chevrolet": 0.95, "tesla": 1.3, "porsche": 1.5,
    "jeep": 1.05, "subaru": 1.05, "mazda": 1.0, "volkswagen": 0.95, "hyundai": 0.9,
    "kia": 0.9, "nissan": 0.95, "gmc": 1.05, "ram": 1.05, "dodge": 0.95,
}


def _db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _vehicle(vin: str):
    vin = (vin or "").strip().upper()
    if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
        raise HTTPException(404, "Vehicle not found")
    conn = _db()
    v = conn.execute("SELECT * FROM vehicles WHERE vin=? AND status='active'", (vin,)).fetchone()
    conn.close()
    if not v:
        raise HTTPException(404, "Vehicle not found")
    return {k: v[k] for k in v.keys()}


def _public_base():
    return os.getenv("LOTVUE_PUBLIC_URL", "http://127.0.0.1:8360").rstrip("/")


def _photo_urls(vin, limit=24):
    base = _public_base()
    vdir = STORAGE_DIR / vin.upper()
    urls = []
    for sub in ("spin", "exterior", "gallery"):
        d = vdir / sub
        if d.exists():
            safe_files = [f for f in sorted(d.glob("*.jpg"))
                          if f.is_file() and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}\.jpg", f.name)]
            for f in safe_files[:max(0, limit - len(urls))]:
                urls.append(f"{base}/storage/{vin.upper()}/{sub}/{f.name}")
        if len(urls) >= limit:
            break
    return urls


def _estimate_value(year, body_style, make, mileage):
    """Shared valuation used by trade-in + price advisor. Returns (avg, low, high)."""
    try:
        years_old = max(0, datetime.now().year - int(year)) if year else 8
    except (ValueError, TypeError):
        years_old = 8
    body = (body_style or "sedan").lower()
    base = next((v for k, v in _BODY_BASE.items() if k in body), 18000)
    premium = _MAKE_PREMIUM.get((make or "").lower(), 1.0)
    dep = max(0.12, 0.85 ** years_old)  # ~15%/yr, floored
    try:
        miles = int(str(mileage).replace(",", "")) if mileage else years_old * 12000
    except (ValueError, TypeError):
        miles = years_old * 12000
    mile_factor = max(0.45, 1 - (miles / 200000) * 0.55)
    avg = int(base * premium * dep * mile_factor)
    return avg, int(avg * 0.9), int(avg * 1.1)


def _notify_lead(lead: dict) -> None:
    try:
        from a2_extensions import notify_lead
        notify_lead(lead)
    except Exception as exc:
        print(f"[Leadmagnet] notify failed: {type(exc).__name__}")


def _create_lead(vin, name, email, phone, message, source, extra=None, *, notify=True):
    vin = (vin or "").strip().upper()
    if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
        raise HTTPException(400, "A valid 17-character vehicle VIN is required")
    name = "".join(ch for ch in str(name or "") if ord(ch) >= 32 and ord(ch) != 127).strip()[:120]
    email = "".join(ch for ch in str(email or "") if ord(ch) >= 32 and ord(ch) != 127).strip().lower()[:254]
    phone = "".join(ch for ch in str(phone or "") if ord(ch) >= 32 and ord(ch) != 127).strip()[:40]
    message = "".join(ch for ch in str(message or "") if ord(ch) in (9, 10, 13) or ord(ch) >= 32 and ord(ch) != 127).strip()[:2000]
    if not name:
        raise HTTPException(400, "Name is required")
    if not email and not phone:
        raise HTTPException(400, "Email or phone is required")
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(400, "Enter a valid email address")
    source = source if source in {"tradein_tool", "history_request", "video_request", "test_drive"} else "website"
    conn = _db()
    vehicle = conn.execute(
        "SELECT user_email FROM vehicles WHERE vin=? AND status='active'", (vin,)
    ).fetchone()
    if not vehicle:
        conn.close()
        raise HTTPException(404, "Vehicle not found")
    tenant_email = str(vehicle["user_email"] or "").strip().lower()
    dealer = conn.execute(
        "SELECT id FROM dealers WHERE LOWER(COALESCE(NULLIF(user_email,''),email,''))=? ORDER BY id LIMIT 1",
        (tenant_email,),
    ).fetchone() if tenant_email else None
    dealer_id = int(dealer["id"]) if dealer else 0
    cur = conn.execute(
        """INSERT INTO leads (dealer_id, vin, name, email, phone, message, source, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (dealer_id, vin, name, email, phone, message, source, datetime.now().isoformat()))
    lead_id = cur.lastrowid
    conn.commit()
    conn.close()
    lead = {"id": lead_id, "vin": vin, "name": name, "email": email,
            "phone": phone, "message": message, **(extra or {})}
    if notify:
        _notify_lead(lead)
    return lead_id


# =============================================================================
# LEAD MAGNETS
# =============================================================================
class TradeIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(max_length=254)
    phone: Optional[str] = Field(default="", max_length=40)
    vehicle_of_interest: Optional[str] = Field(default="", max_length=17)
    year: Optional[str] = Field(default="", max_length=4)
    make: Optional[str] = Field(default="", max_length=60)
    model: Optional[str] = Field(default="", max_length=80)
    mileage: Optional[str] = Field(default="", max_length=12)
    condition: Optional[str] = Field(default="good", max_length=20)


@router.post("/api/leadmagnet/tradein")
def tradein_estimate(body: TradeIn, request: Request):
    from main import _check_public_lead_rate
    _check_public_lead_rate(request)
    avg, low, high = _estimate_value(body.year, "", body.make, body.mileage)
    cond_adj = {"excellent": 1.12, "good": 1.0, "fair": 0.85, "poor": 0.68}.get(
        (body.condition or "good").lower(), 1.0)
    avg, low, high = int(avg * cond_adj), int(low * cond_adj), int(high * cond_adj)
    msg = (f"Trade-in lead: {body.year} {body.make} {body.model}, {body.mileage} mi, "
           f"condition {body.condition}. Est ${low:,}-${high:,}. "
           f"Shopping: {body.vehicle_of_interest}")
    lead_id = None
    if body.vehicle_of_interest:
        lead_id = _create_lead(body.vehicle_of_interest, body.name, body.email, body.phone,
                               msg, "tradein_tool", {"wants_trade": 1})
    return {"status": "ok", "lead_id": lead_id,
            "estimate": {"low": low, "average": avg, "high": high},
            "disclaimer": "Instant estimate based on year, make, mileage and condition. "
                          "Final offer after a quick in-person appraisal."}


@router.get("/api/leadmagnet/{vin}/price-advisor")
def price_advisor(vin: str):
    """Public buyer-facing 'priced vs market' badge — honest estimate, labeled."""
    v = _vehicle(vin)
    avg, low, high = _estimate_value(v.get("year"), v.get("body_style"),
                                     v.get("make"), v.get("mileage"))
    try:
        asking = float(str(v.get("price") or 0).replace("$", "").replace(",", ""))
    except ValueError:
        asking = 0
    rating, color = "Fair Price", "#2E8B57"
    if asking and avg:
        ratio = asking / avg
        if ratio <= 0.94:
            rating, color = "Great Price", "#1E8449"
        elif ratio <= 1.06:
            rating, color = "Fair Price", "#2E75B6"
        elif ratio <= 1.18:
            rating, color = "Above Market", "#E08A1E"
        else:
            rating, color = "High Price", "#C0392B"
    return {"vin": vin.upper(), "asking_price": asking or None,
            "market_estimate": {"average": avg, "low": low, "high": high},
            "rating": rating, "rating_color": color,
            "disclaimer": "Estimate from year/make/mileage/body — not a guaranteed appraisal."}


class HistoryReq(BaseModel):
    vin: str = Field(min_length=17, max_length=17)
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(max_length=254)
    phone: Optional[str] = Field(default="", max_length=40)


@router.post("/api/leadmagnet/request-history")
def request_history(body: HistoryReq, request: Request):
    from main import _check_public_lead_rate
    _check_public_lead_rate(request)
    lead_id = _create_lead(body.vin, body.name, body.email, body.phone,
                           "Requested vehicle history report (CARFAX/AutoCheck).",
                           "history_request", {"wants_history": 1})
    return {"status": "ok", "lead_id": lead_id,
            "message": "Got it — we'll send the vehicle history report to your email shortly."}


class VideoReq(BaseModel):
    vin: str = Field(min_length=17, max_length=17)
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(max_length=254)
    phone: Optional[str] = Field(default="", max_length=40)


@router.post("/api/leadmagnet/request-video")
def request_video(body: VideoReq, request: Request):
    from main import _check_public_lead_rate
    _check_public_lead_rate(request)
    lead_id = _create_lead(body.vin, body.name, body.email, body.phone,
                           "Requested a personalized walk-around video.",
                           "video_request", {"wants_video": 1})
    return {"status": "ok", "lead_id": lead_id,
            "message": "We'll record a personal walk-around video and send it over."}


@router.get("/api/leadmagnet/test-drive-slots")
def test_drive_slots(vin: str, request: Request, days: int = 7):
    from main import _check_public_lead_rate
    _check_public_lead_rate(request)
    vehicle = _vehicle(vin)
    try:
        from ai_studio.lead_engine import get_available_slots
        slots = get_available_slots(days_ahead=max(1, min(days, 30)),
                                    user_email=str(vehicle.get("user_email") or ""))
    except Exception:
        slots = []
    return {"status": "ok", "slots": slots[:40], "count": len(slots)}


class TestDrive(BaseModel):
    vin: str = Field(min_length=17, max_length=17)
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(max_length=254)
    phone: Optional[str] = Field(default="", max_length=40)
    slot: str = Field(min_length=1, max_length=64)


@router.post("/api/leadmagnet/book-test-drive")
def book_test_drive(body: TestDrive, request: Request):
    from ai_studio.lead_engine import book_appointment
    from main import _check_public_lead_rate
    _check_public_lead_rate(request)
    vehicle = _vehicle(body.vin)
    lead_id = _create_lead(body.vin, body.name, body.email, body.phone,
                           f"Requested a test drive for {body.slot}.",
                           "test_drive", {"wants_test_drive": 1, "slot": body.slot}, notify=False)
    booked = book_appointment(
        lead_id, body.slot, "test_drive",
        user_email=str(vehicle.get("user_email") or "").strip().lower(),
    )
    if booked.get("error"):
        conn = _db()
        conn.execute("DELETE FROM leads WHERE id=? AND vin=?", (lead_id, body.vin.upper()))
        conn.commit()
        conn.close()
        raise HTTPException(409, booked["error"])
    _notify_lead({"id": lead_id, "vin": body.vin.upper(), "name": body.name,
                  "email": body.email, "phone": body.phone or "",
                  "message": f"Requested a test drive for {body.slot}.",
                  "wants_test_drive": 1})
    return {"status": "ok", "lead_id": lead_id,
            "message": f"Test drive requested for {body.slot}. We'll confirm by email/text."}


# =============================================================================
# AI BDC CHATBOT
# =============================================================================
class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=500)


class ChatTurn(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    history: list[ChatMessage] = Field(default_factory=list, max_length=12)


def _vehicle_context(v):
    title = f"{v.get('year','')} {v.get('make','')} {v.get('model','')} {v.get('trim_level','') or ''}".strip()
    facts = {
        "Vehicle": title, "Price": v.get("price") or "ask", "Mileage": v.get("mileage") or "n/a",
        "Engine": v.get("engine") or "n/a", "Transmission": v.get("transmission") or "n/a",
        "Drivetrain": v.get("drivetrain") or "n/a", "Fuel": v.get("fuel_type") or "n/a",
        "Exterior color": v.get("color") or "n/a", "Body": v.get("body_style") or "n/a",
        "VIN": v.get("vin"),
    }
    return title, facts


def _rule_based_reply(msg, title, facts):
    m = msg.lower()
    if any(w in m for w in ("price", "cost", "how much")):
        return f"The {title} is listed at {facts['Price']}. Want me to set up a quick call or test drive?"
    if any(w in m for w in ("mile", "odometer")):
        return f"It has {facts['Mileage']} miles. Anything else you'd like to know?"
    if any(w in m for w in ("engine", "horsepower", "drivetrain", "transmission", "mpg", "fuel")):
        return (f"Engine: {facts['Engine']}, transmission: {facts['Transmission']}, "
                f"drivetrain: {facts['Drivetrain']}, fuel: {facts['Fuel']}.")
    if any(w in m for w in ("available", "still", "sold")):
        return f"Yes, the {title} is available. Would you like to book a test drive?"
    if any(w in m for w in ("test drive", "see it", "visit", "appointment")):
        return "Great — tap “Book a Test Drive” below and pick a time that works. I'll confirm it."
    if any(w in m for w in ("trade", "my car")):
        return "We do trades — use “Value My Trade” below for an instant estimate."
    if any(w in m for w in ("history", "carfax", "autocheck", "accident")):
        return "I can send the full vehicle history — tap “Get Vehicle History” and I'll email it over."
    return (f"Happy to help with the {title}. You can ask about price, mileage, features, "
            f"history, or book a test drive. Want me to have a specialist reach out?")


def _ollama_chat_reply(body: ChatTurn, title: str, facts: dict) -> str | None:
    """Bounded local-model call; never block the ASGI event loop or buffer unlimited output."""
    facts_str = "; ".join(f"{key}: {value}" for key, value in facts.items())
    system_prompt = (
        "You are a friendly, concise sales assistant for a car dealership, chatting on a "
        f"vehicle's 360 listing page. ONLY use these facts about the car: {facts_str}. "
        "Never invent specs, history, or pricing you weren't given — if asked something you "
        "don't know, offer to connect them with the team. Keep replies under 60 words. "
        "Nudge toward booking a test drive or leaving contact info when natural.")
    messages = [{"role": "system", "content": system_prompt}]
    for item in body.history[-6:]:
        if item.content:
            messages.append({"role": item.role, "content": item.content})
    messages.append({"role": "user", "content": body.message})
    endpoint = os.getenv("LOTVUE_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    req = urllib.request.Request(
        f"{endpoint}/api/chat",
        data=json.dumps({"model": os.getenv("LOTVUE_OLLAMA_MODEL", "qwen3.6-64k:latest"),
                         "messages": messages, "stream": False}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as response:
        raw = response.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        return None
    payload = json.loads(raw)
    reply = payload.get("message", {}).get("content", "").strip()
    if "</think>" in reply:
        reply = reply.split("</think>", 1)[1].strip()
    letters = sum(char.isalpha() or char.isspace() for char in reply)
    if 5 <= len(reply) <= 800 and letters / max(1, len(reply)) > 0.7:
        return reply
    return None


@router.post("/api/chat/{vin}")
async def chat(vin: str, body: ChatTurn, request: Request):
    from main import _check_public_lead_rate
    _check_public_lead_rate(request)
    vehicle = await run_in_threadpool(_vehicle, vin)
    title, facts = _vehicle_context(vehicle)
    # Ollama is synchronous and may take several seconds; keep it off the event loop.
    try:
        reply = await run_in_threadpool(_ollama_chat_reply, body, title, facts)
        if reply:
            return {"vin": vin.upper(), "reply": reply, "source": "ai"}
    except Exception:
        pass
    return {"vin": vin.upper(), "reply": _rule_based_reply(body.message, title, facts),
            "source": "assistant"}


# =============================================================================
# VIRTUAL SHOWROOM MICROSITE
# =============================================================================
def _active_inventory(dealer: str, limit: int = 200):
    dealer_key = (dealer or "").strip()
    if not dealer_key or len(dealer_key) > 120:
        raise HTTPException(404, "Showroom not found")
    conn = _db()
    dealer_row = conn.execute(
        "SELECT user_email, email FROM dealers WHERE LOWER(name)=LOWER(?) LIMIT 1", (dealer_key,)
    ).fetchone()
    if not dealer_row:
        conn.close()
        raise HTTPException(404, "Showroom not found")
    tenant_email = str(dealer_row["user_email"] or dealer_row["email"] or "").strip().lower()
    if not tenant_email:
        conn.close()
        return []
    rows = conn.execute(
        "SELECT vin, year, make, model, trim_level, price, mileage, color "
        "FROM vehicles WHERE status='active' AND LOWER(COALESCE(user_email,''))=? "
        "ORDER BY created_at DESC LIMIT ?", (tenant_email, max(1, min(int(limit), 200)))
    ).fetchall()
    conn.close()
    out = []
    for v in rows:
        vin = str(v["vin"] or "").strip().upper()
        if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
            continue
        photos = _photo_urls(vin, limit=1)
        if not photos:
            continue  # only show vehicles with public media
        out.append({
            "vin": vin,
            "title": f"{v['year']} {v['make']} {v['model']}".strip() or vin,
            "trim": v["trim_level"] or "", "price": v["price"] or "",
            "mileage": v["mileage"] or "", "color": v["color"] or "",
            "thumb": photos[0], "url": f"/view/{vin}",
        })
    return out


@router.get("/api/showroom/{dealer}/inventory")
def showroom_inventory(dealer: str):
    return {"dealer": dealer, "inventory": _active_inventory(dealer)}


@router.get("/showroom/{dealer}", response_class=HTMLResponse)
def showroom(dealer: str, request: Request):
    dealer = (dealer or "").strip()
    if not dealer or len(dealer) > 120 or any(ord(ch) < 32 for ch in dealer):
        raise HTTPException(404, "Showroom not found")
    conn = _db()
    drow = conn.execute("SELECT * FROM dealers WHERE LOWER(name)=LOWER(?)", (dealer,)).fetchone()
    conn.close()
    if not drow:
        raise HTTPException(404, "Showroom not found")
    dname = str(drow["display_name"] or drow["name"] or "")[:120]
    logo = str(drow["logo_url"] or "")[:2048]
    brand = str(drow["brand_color"] or PRIMARY)
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", brand):
        brand = PRIMARY
    phone = str(drow["phone"] or "")[:40]
    if logo:
        try:
            parsed_logo = urlsplit(logo)
            port = parsed_logo.port
            if parsed_logo.scheme:
                valid_logo = (parsed_logo.scheme.lower() == "https" and bool(parsed_logo.hostname)
                              and not parsed_logo.username and not parsed_logo.password
                              and port in {None, 443})
            else:
                path = parsed_logo.path
                valid_logo = (not parsed_logo.netloc and not parsed_logo.query and not parsed_logo.fragment
                              and ".." not in path.split("/") and chr(92) not in path
                              and path.startswith("/storage/_brand/")
                              and re.fullmatch(r"/storage/_brand/[A-Za-z0-9_./-]+", path))
            if not valid_logo:
                logo = ""
        except ValueError:
            logo = ""
    inv = _active_inventory(dealer)
    base = _public_base()
    cards = "\n".join(f"""
      <a class="car" href="/view/{html.escape(c['vin'])}">
        <div class="thumb" style="background-image:url('{html.escape(c['thumb'])}')">
          <span class="badge360">360°</span>
        </div>
        <div class="info">
          <div class="ctitle">{html.escape(c['title'])}</div>
          <div class="cmeta">{html.escape(str(c['mileage']))+' mi' if c['mileage'] else ''}{' · '+html.escape(c['color']) if c['color'] else ''}</div>
          <div class="cprice">{('$'+html.escape(str(c['price']))) if c['price'] else 'Call for price'}</div>
        </div>
      </a>""" for c in inv) or '<p class="empty">No vehicles listed yet — check back soon.</p>'
    logo_html = (f'<img src="{html.escape(logo)}" alt="{html.escape(dname)}" class="logo">'
                 if logo else f'<div class="logo-text">{html.escape(dname)}</div>')
    page = f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{html.escape(dname)} — Inventory</title>
<meta property="og:title" content="{html.escape(dname)} — Vehicle Inventory">
<meta property="og:description" content="Browse our interactive 360° vehicle showroom.">
<style>
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:{BG};color:{TEXT};}}
.top{{background:{CARD};border-bottom:1px solid #E3EAF2;padding:1rem 1.5rem;display:flex;align-items:center;gap:1rem;position:sticky;top:0;z-index:5;}}
.logo{{height:42px;max-width:200px;object-fit:contain;}}
.logo-text{{font-size:1.4rem;font-weight:800;color:{brand};}}
.top .grow{{flex:1;}}
.top .ph{{color:{MUTED};font-weight:600;}}
.hero{{padding:2rem 1.5rem 1rem;max-width:1200px;margin:0 auto;}}
.hero h1{{font-size:1.6rem;color:{TEXT};}}
.hero p{{color:{MUTED};margin-top:.3rem;}}
.search{{margin:1rem 0 0;}}
.search input{{width:100%;max-width:420px;padding:.8rem 1rem;border:1.5px solid #DDE5EE;border-radius:10px;font-size:1rem;}}
.grid{{max-width:1200px;margin:1rem auto 3rem;padding:0 1.5rem;display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:1.2rem;}}
.car{{background:{CARD};border:1px solid #E3EAF2;border-radius:14px;overflow:hidden;text-decoration:none;color:inherit;transition:transform .15s,box-shadow .15s;display:block;}}
.car:hover{{transform:translateY(-3px);box-shadow:0 8px 24px rgba(44,62,80,.12);}}
.thumb{{aspect-ratio:4/3;background-size:cover;background-position:center;position:relative;background-color:#E8EEF5;}}
.badge360{{position:absolute;top:10px;left:10px;background:{brand};color:#fff;font-size:.72rem;font-weight:700;padding:3px 9px;border-radius:20px;}}
.info{{padding:.9rem 1rem 1.1rem;}}
.ctitle{{font-weight:700;font-size:1.02rem;}}
.cmeta{{color:{MUTED};font-size:.85rem;margin:.2rem 0 .5rem;}}
.cprice{{font-weight:800;color:{brand};font-size:1.15rem;}}
.empty{{grid-column:1/-1;text-align:center;color:{MUTED};padding:3rem;}}
.foot{{text-align:center;color:{MUTED};font-size:.8rem;padding:2rem;}}
</style></head><body>
<div class="top">{logo_html}<div class="grow"></div>{f'<span class="ph">📞 {html.escape(phone)}</span>' if phone else ''}</div>
<div class="hero"><h1>{html.escape(dname)} — Inventory</h1>
<p>{len(inv)} vehicle{'s' if len(inv)!=1 else ''} · every one with a full interactive 360° tour</p>
<div class="search"><input id="q" placeholder="Search make, model, year…" oninput="filt()"></div></div>
<div class="grid" id="grid">{cards}</div>
<div class="foot">Interactive showroom by LotVue360</div>
<script>
function filt(){{var q=document.getElementById('q').value.toLowerCase();
document.querySelectorAll('.car').forEach(function(c){{
c.style.display=c.querySelector('.ctitle').textContent.toLowerCase().includes(q)?'block':'none';}});}}
</script></body></html>"""
    return HTMLResponse(page)


# =============================================================================
# WINDOW STICKER + FTC BUYERS GUIDE (dealer paperwork — gated)
# =============================================================================
@router.get("/api/vehicle/{vin}/window-sticker.pdf")
def window_sticker(vin: str):
    import qrcode
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas as pdfcanvas

    v = _vehicle(vin)
    vin = vin.upper()
    title = f"{v.get('year','')} {v.get('make','')} {v.get('model','')}".strip()
    buf = io.BytesIO()
    c = pdfcanvas.Canvas(buf, pagesize=letter)
    W, H = letter
    c.setFillColor(colors.HexColor(PRIMARY))
    c.rect(0, H - 1.3 * inch, W, 1.3 * inch, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 30)
    c.drawString(0.6 * inch, H - 0.95 * inch, title or vin)
    c.setFont("Helvetica", 13)
    c.drawString(0.6 * inch, H - 1.2 * inch, f"{v.get('trim_level','') or ''}  ·  VIN {vin}")
    # price block
    c.setFillColor(colors.HexColor(TEXT))
    c.setFont("Helvetica-Bold", 46)
    price = str(v.get("price") or "").replace("$", "")
    c.drawString(0.6 * inch, H - 2.4 * inch, f"${price}" if price else "Call for Price")
    # specs
    rows = [("Mileage", f"{v.get('mileage','')} mi" if v.get('mileage') else "—"),
            ("Engine", v.get("engine") or "—"), ("Transmission", v.get("transmission") or "—"),
            ("Drivetrain", v.get("drivetrain") or "—"), ("Fuel", v.get("fuel_type") or "—"),
            ("Exterior", v.get("color") or "—"), ("Body", v.get("body_style") or "—")]
    y = H - 3.1 * inch
    c.setFont("Helvetica", 13)
    for label, val in rows:
        c.setFillColor(colors.HexColor(MUTED)); c.drawString(0.6 * inch, y, label)
        c.setFillColor(colors.HexColor(TEXT)); c.setFont("Helvetica-Bold", 13)
        c.drawString(2.6 * inch, y, str(val)); c.setFont("Helvetica", 13)
        y -= 0.34 * inch
    # QR to the 360 viewer
    qr = qrcode.make(f"{_public_base()}/view/{vin}")
    qbuf = io.BytesIO(); qr.save(qbuf, format="PNG"); qbuf.seek(0)
    from reportlab.lib.utils import ImageReader
    c.drawImage(ImageReader(qbuf), W - 2.4 * inch, 0.7 * inch, 1.7 * inch, 1.7 * inch)
    c.setFillColor(colors.HexColor(MUTED)); c.setFont("Helvetica", 10)
    c.drawString(W - 2.45 * inch, 0.5 * inch, "Scan for 360° tour")
    c.setFont("Helvetica", 9)
    c.drawString(0.6 * inch, 0.4 * inch, "Presented with LotVue360 interactive merchandising.")
    c.showPage(); c.save(); buf.seek(0)
    return Response(buf.getvalue(), media_type="application/pdf",
                    headers={"Content-Disposition": f"inline; filename={vin}_window_sticker.pdf"})


@router.get("/api/vehicle/{vin}/buyers-guide.pdf")
def buyers_guide(vin: str, warranty: str = "as_is"):
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas as pdfcanvas

    v = _vehicle(vin); vin = vin.upper()
    title = f"{v.get('year','')} {v.get('make','')} {v.get('model','')}".strip()
    buf = io.BytesIO(); c = pdfcanvas.Canvas(buf, pagesize=letter); W, H = letter
    c.setFont("Helvetica-Bold", 22)
    c.drawCentredString(W / 2, H - 0.8 * inch, "BUYERS GUIDE")
    c.setFont("Helvetica", 10)
    c.drawCentredString(W / 2, H - 1.05 * inch,
                        "IMPORTANT: Spoken promises are difficult to enforce. Ask the dealer "
                        "to put all promises in writing. Keep this form.")
    c.setFont("Helvetica-Bold", 14)
    c.drawString(0.7 * inch, H - 1.6 * inch, f"{title}    VIN: {vin}")
    # warranty box
    c.rect(0.7 * inch, H - 3.2 * inch, W - 1.4 * inch, 1.4 * inch)
    c.setFont("Helvetica-Bold", 13)
    is_asis = warranty == "as_is"
    c.drawString(0.9 * inch, H - 1.95 * inch, ("[X]" if is_asis else "[  ]") + "  AS IS - NO DEALER WARRANTY")
    c.setFont("Helvetica", 10)
    c.drawString(1.0 * inch, H - 2.2 * inch,
                 "THE DEALER DOES NOT PROVIDE A WARRANTY FOR ANY REPAIRS AFTER SALE.")
    c.setFont("Helvetica-Bold", 13)
    c.drawString(0.9 * inch, H - 2.6 * inch, ("[X]" if not is_asis else "[  ]") + "  DEALER WARRANTY")
    c.setFont("Helvetica", 10)
    c.drawString(1.0 * inch, H - 2.85 * inch,
                 "The dealer will pay the listed percentage of covered systems' repair cost.")
    # systems checklist (abbreviated FTC list)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(0.7 * inch, H - 3.6 * inch, "SYSTEMS COVERED / MAJOR DEFECTS THAT MAY OCCUR:")
    systems = ["Frame & Body", "Engine", "Transmission", "Differential", "Cooling System",
               "Electrical", "Fuel System", "Brakes", "Steering", "Suspension", "Air Conditioning"]
    c.setFont("Helvetica", 10); y = H - 3.9 * inch
    for i, s in enumerate(systems):
        x = 0.8 * inch + (i % 2) * 3.4 * inch
        if i % 2 == 0 and i:
            y -= 0.26 * inch
        c.drawString(x, y, f"•  {s}")
    c.setFont("Helvetica", 9)
    c.drawString(0.7 * inch, 0.7 * inch,
                 "See the back of this form for important additional information, including a list "
                 "of major defects that may occur in used motor vehicles.")
    c.drawString(0.7 * inch, 0.5 * inch, f"Generated by LotVue360 · {datetime.now():%Y-%m-%d}")
    c.showPage(); c.save(); buf.seek(0)
    return Response(buf.getvalue(), media_type="application/pdf",
                    headers={"Content-Disposition": f"inline; filename={vin}_buyers_guide.pdf"})


def register_glo3d(app):
    app.include_router(router)
    print("[G-GLO3D] registered: lead magnets (trade-in/price-advisor/history/test-drive/video), "
          "AI BDC chatbot, virtual showroom microsite, window sticker + FTC buyers guide")
