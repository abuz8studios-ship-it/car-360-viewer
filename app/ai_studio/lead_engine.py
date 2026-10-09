"""
LotVue Pro — AI Lead Engine (Module 9)
Autonomous BDC: lead follow-up, appointment scheduling, lead scoring,
auto-response via text/email/chat — cloning best of 360autoai + Flai + BDC.ai.

All AI responses powered by Ollama LOCAL models (LOCAL > CLOUD > PAID).
Zero cloud cost — all local generation.
"""
import json
import os
import smtplib
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from email.mime.text import MIMEText

from app_paths import DB_PATH

OLLAMA_URL = os.getenv("LOTVUE_OLLAMA_URL", "http://localhost:11434")
# Canonical app DB is shared with vehicle, lead, and extension routes.


# =============================================================================
# LEAD SCORING — qualify leads automatically
# =============================================================================
def score_lead(lead: dict, vehicle: dict = None) -> dict:
    """
    Score a lead 0-100 based on engagement signals.
    Returns score, grade (HOT/WARM/COLD), and factors.
    """
    score = 0
    factors = []

    # Contact completeness
    if lead.get("phone"):
        score += 15
        factors.append("Phone provided (+15)")
    if lead.get("email"):
        score += 10
        factors.append("Email provided (+10)")
    if lead.get("name"):
        score += 5
        factors.append("Name provided (+5)")

    # Intent signals
    if lead.get("wants_test_drive"):
        score += 25
        factors.append("Wants test drive (+25)")
    if lead.get("wants_video"):
        score += 10
        factors.append("Requested video (+10)")

    msg = (lead.get("message") or "").lower()
    # High-intent keywords
    high_intent = ["buy", "purchase", "finance", "trade", "cash", "today", "tomorrow",
                   "asap", "ready", "serious", "approved", "pre-approved"]
    mid_intent = ["price", "payment", "available", "condition", "mileage", "history"]
    for kw in high_intent:
        if kw in msg:
            score += 10
            factors.append(f"High-intent keyword: '{kw}' (+10)")
            break
    for kw in mid_intent:
        if kw in msg:
            score += 5
            factors.append(f"Mid-intent keyword: '{kw}' (+5)")
            break

    # Message length (longer = more engaged)
    if len(msg) > 100:
        score += 10
        factors.append("Detailed message (+10)")
    elif len(msg) > 30:
        score += 5
        factors.append("Moderate message (+5)")

    # Source scoring
    source = (lead.get("source") or "").lower()
    source_scores = {
        "direct": 15, "referral": 15, "google": 10, "facebook": 8,
        "cargurus": 12, "autotrader": 12, "cars.com": 10,
        "craigslist": 5, "viewer": 5, "walk-in": 20
    }
    for src, pts in source_scores.items():
        if src in source:
            score += pts
            factors.append(f"Source: {source} (+{pts})")
            break

    # Vehicle price alignment (if vehicle data available)
    if vehicle and vehicle.get("price"):
        score += 5
        factors.append("Viewing priced vehicle (+5)")

    score = min(score, 100)

    if score >= 70:
        grade = "HOT"
    elif score >= 40:
        grade = "WARM"
    else:
        grade = "COLD"

    return {
        "score": score,
        "grade": grade,
        "factors": factors,
        "recommended_action": _recommend_action(grade, lead),
        "response_priority": "IMMEDIATE" if grade == "HOT" else "STANDARD",
        "follow_up_cadence": _get_cadence(grade),
    }


def _recommend_action(grade: str, lead: dict) -> str:
    if grade == "HOT":
        if lead.get("wants_test_drive"):
            return "CALL IMMEDIATELY — test drive request from hot lead"
        return "CALL within 5 minutes — high purchase intent"
    elif grade == "WARM":
        return "Send personalized video + follow-up text within 15 minutes"
    else:
        return "Auto-respond with vehicle info + drip sequence"


def _get_cadence(grade: str) -> list:
    if grade == "HOT":
        return [
            {"day": 0, "channel": "call", "message": "Immediate call"},
            {"day": 0, "channel": "text", "message": "Follow-up text if no answer"},
            {"day": 1, "channel": "text", "message": "Video walkaround link"},
            {"day": 2, "channel": "email", "message": "Digital dossier"},
            {"day": 3, "channel": "call", "message": "Second call attempt"},
            {"day": 5, "channel": "text", "message": "Price update / incentive"},
            {"day": 7, "channel": "email", "message": "Similar vehicles"},
        ]
    elif grade == "WARM":
        return [
            {"day": 0, "channel": "text", "message": "Intro + vehicle link"},
            {"day": 1, "channel": "email", "message": "Video walkaround"},
            {"day": 3, "channel": "text", "message": "Still interested?"},
            {"day": 7, "channel": "email", "message": "Price drop alert"},
            {"day": 14, "channel": "text", "message": "Similar vehicles"},
            {"day": 30, "channel": "email", "message": "Monthly inventory update"},
        ]
    else:
        return [
            {"day": 0, "channel": "email", "message": "Auto-response with info"},
            {"day": 3, "channel": "email", "message": "Featured inventory"},
            {"day": 14, "channel": "email", "message": "Monthly deals"},
        ]


# =============================================================================
# AI RESPONSE GENERATION — using Ollama local models
# =============================================================================
def generate_lead_response(lead: dict, vehicle: dict = None,
                           dealer_name: str = "LotVue Pro Dealer",
                           response_type: str = "initial") -> dict:
    """
    Generate an AI-crafted response for a lead using Ollama.
    Returns text for SMS and email variants.

    response_type: 'initial', 'follow_up', 'appointment_confirm',
                   'price_update', 'video_ready'
    """
    customer_name = lead.get("name", "").split()[0] if lead.get("name") else "there"
    vin = lead.get("vin", "")
    vehicle_title = ""
    price_str = ""

    if vehicle:
        year = vehicle.get("year", "")
        make = vehicle.get("make", "")
        model = vehicle.get("model", "")
        vehicle_title = f"{year} {make} {model}".strip()
        if vehicle.get("price"):
            try:
                price_str = f"${int(vehicle['price']):,}"
            except (ValueError, TypeError):
                price_str = ""

    templates = {
        "initial": {
            "sms": (
                f"Hi {customer_name}! Thanks for your interest in the "
                f"{vehicle_title}. {price_str + ' — great value.' if price_str else ''} "
                f"I have it ready for viewing. When works best for you to come see it? "
                f"— {dealer_name}"
            ),
            "email_subject": f"Your inquiry about the {vehicle_title}",
            "email_body": (
                f"Hi {customer_name},\n\n"
                f"Thank you for reaching out about the {vehicle_title}.\n\n"
                f"{'Listed at ' + price_str + ', this is' if price_str else 'This is'} "
                f"a fantastic vehicle and I'd love to show it to you in person.\n\n"
                f"Would you like to:\n"
                f"• Schedule a test drive\n"
                f"• See a 360° virtual walkaround\n"
                f"• Get a personalized video of the vehicle\n\n"
                f"Just reply to this email or call us directly. "
                f"I'm here to help make this easy for you.\n\n"
                f"Best regards,\n{dealer_name}"
            ),
        },
        "follow_up": {
            "sms": (
                f"Hey {customer_name}, just checking in on the {vehicle_title}. "
                f"Still available and I can hold it for you. Want to schedule a time to see it?"
            ),
            "email_subject": f"Still thinking about the {vehicle_title}?",
            "email_body": (
                f"Hi {customer_name},\n\n"
                f"I wanted to follow up on your interest in the {vehicle_title}. "
                f"It's still available and we've had some interest from other buyers.\n\n"
                f"I'd hate for you to miss out — would you like to come in this week?\n\n"
                f"Best,\n{dealer_name}"
            ),
        },
        "appointment_confirm": {
            "sms": (
                f"Confirmed! See you for the {vehicle_title}. "
                f"I'll have it pulled up front and ready. — {dealer_name}"
            ),
            "email_subject": f"Your appointment is confirmed — {vehicle_title}",
            "email_body": (
                f"Hi {customer_name},\n\n"
                f"Your appointment to see the {vehicle_title} is confirmed.\n\n"
                f"I'll have the vehicle ready and waiting for you.\n\n"
                f"See you soon!\n{dealer_name}"
            ),
        },
        "video_ready": {
            "sms": (
                f"Hi {customer_name}! Your personalized video of the {vehicle_title} "
                f"is ready — check your email for the link! — {dealer_name}"
            ),
            "email_subject": f"Your {vehicle_title} — Video Walkaround Ready",
            "email_body": (
                f"Hi {customer_name},\n\n"
                f"Great news! I've prepared a personalized video walkaround of the "
                f"{vehicle_title} just for you.\n\n"
                f"Click below to watch your video and explore every angle.\n\n"
                f"When you're ready, let's schedule a test drive!\n\n"
                f"Best,\n{dealer_name}"
            ),
        },
        "price_update": {
            "sms": (
                f"Price update on the {vehicle_title} you were looking at — "
                f"{'now ' + price_str if price_str else 'new pricing available'}. "
                f"Don't miss it! — {dealer_name}"
            ),
            "email_subject": f"Price update — {vehicle_title}",
            "email_body": (
                f"Hi {customer_name},\n\n"
                f"I have exciting news about the {vehicle_title} you were interested in — "
                f"{'the price has been updated to ' + price_str if price_str else 'new pricing is available'}.\n\n"
                f"This won't last long. Want to come in?\n\n"
                f"Best,\n{dealer_name}"
            ),
        },
    }

    template = templates.get(response_type, templates["initial"])

    # ── Try the LOCAL Ollama model for a personalized, on-brand rewrite.
    #    Strictly grounded: only the facts below, never invented specs/history/
    #    financing. Falls back to the template on any failure or weak output.
    if os.getenv("LOTVUE_LEAD_AI", "1") != "0":
        try:
            facts = (f"Customer first name: {customer_name}. "
                     f"Vehicle: {vehicle_title or 'the vehicle they asked about'}. "
                     f"Price: {price_str or 'not provided'}. Dealer: {dealer_name}. "
                     f"Their message: {(lead.get('message') or '')[:300] or '(none)'}. "
                     f"Response type: {response_type}.")
            system = ("You are an honest, friendly car-dealership BDC assistant. Use ONLY the "
                      "given facts. NEVER invent specs, mileage, history, financing terms, or "
                      "pricing you were not given. Output ONLY the requested text — no preamble.")
            sms = _ollama_generate(
                f"Write ONE warm, concise SMS (max 300 chars) to move this lead forward. "
                f"End with the dealer name. Facts: {facts}", system)
            body = _ollama_generate(
                f"Write a warm, professional plain-text email body (60-120 words) to move this "
                f"lead forward. Sign off with the dealer name. Facts: {facts}", system)
            if _ai_text_ok(sms) and _ai_text_ok(body):
                return {
                    "sms": sms[:320],
                    "email_subject": template["email_subject"],
                    "email_body": body,
                    "response_type": response_type,
                    "generated_at": datetime.now().isoformat(),
                    "model": f"ollama:{os.getenv('LOTVUE_OLLAMA_MODEL', 'qwen3.6-64k:latest')}",
                }
        except Exception as e:
            print(f"[Lead-AI] Ollama generation failed, using template: {e}")

    return {
        "sms": template["sms"],
        "email_subject": template["email_subject"],
        "email_body": template["email_body"],
        "response_type": response_type,
        "generated_at": datetime.now().isoformat(),
        "model": "template-v1",
    }


def _ollama_generate(prompt: str, system: str, timeout: int = 45) -> str:
    """One-shot local generation via Ollama /api/chat. Strips <think> blocks."""
    model = os.getenv("LOTVUE_OLLAMA_MODEL", "qwen3.6-64k:latest")
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps({"model": model, "stream": False,
                         "messages": [{"role": "system", "content": system},
                                      {"role": "user", "content": prompt}]}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        txt = json.loads(r.read()).get("message", {}).get("content", "").strip()
    if "</think>" in txt:
        txt = txt.split("</think>", 1)[1].strip()
    return txt.strip().strip('"')


def _ai_text_ok(t: str) -> bool:
    """Sanity gate — reject empty/garbled model output before it reaches a customer."""
    if not t or len(t) < 8 or len(t) > 1500:
        return False
    letters = sum(c.isalpha() or c.isspace() for c in t)
    return letters / max(1, len(t)) > 0.6


# =============================================================================
# APPOINTMENT SCHEDULING
# =============================================================================
_DEFAULT_DEALER_HOURS = {
    0: (9, 19),   # Monday
    1: (9, 19),   # Tuesday
    2: (9, 19),   # Wednesday
    3: (9, 19),   # Thursday
    4: (9, 19),   # Friday
    5: (9, 18),   # Saturday
    6: (11, 17),  # Sunday
}


def get_available_slots(dealer_hours: dict = None, days_ahead: int = 7,
                        user_email: str | None = None) -> list:
    """
    Generate available appointment slots for the next N days.
    Default: Mon-Sat 9am-7pm, Sun 11am-5pm.
    """
    if not dealer_hours:
        dealer_hours = _DEFAULT_DEALER_HOURS

    try:
        days_ahead = max(1, min(int(days_ahead), 30))
    except (TypeError, ValueError):
        days_ahead = 7
    slots = []
    today = datetime.now().replace(minute=0, second=0, microsecond=0)
    if today.minute > 30:
        today = today.replace(hour=today.hour + 1)

    for day_offset in range(days_ahead):
        date = today + timedelta(days=day_offset)
        dow = date.weekday()
        hours = dealer_hours.get(dow)
        if not hours:
            continue

        start_hour, end_hour = hours
        for hour in range(start_hour, end_hour):
            for minute in [0, 30]:
                slot_time = date.replace(hour=hour, minute=minute)
                if slot_time > datetime.now():
                    slots.append({
                        "datetime": slot_time.isoformat(),
                        "display": slot_time.strftime("%A %b %d, %I:%M %p"),
                        "date": slot_time.strftime("%Y-%m-%d"),
                        "time": slot_time.strftime("%I:%M %p"),
                        "day_of_week": slot_time.strftime("%A"),
                    })

    email = (user_email or "").strip().lower()
    conn = sqlite3.connect(str(DB_PATH))
    now_iso = datetime.now().isoformat(timespec="minutes")
    end_iso = (datetime.now() + timedelta(days=days_ahead + 1)).isoformat(timespec="minutes")
    try:
        if email:
            rows = conn.execute(
                "SELECT a.scheduled_at FROM appointments a "
                "JOIN leads l ON l.id=a.lead_id "
                "JOIN vehicles v ON v.vin=l.vin "
                "WHERE a.status='confirmed' AND a.scheduled_at>=? AND a.scheduled_at<? "
                "AND LOWER(v.user_email)=?",
                (now_iso, end_iso, email),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT scheduled_at FROM appointments "
                "WHERE status='confirmed' AND scheduled_at>=? AND scheduled_at<?",
                (now_iso, end_iso),
            ).fetchall()
        booked = set()
        for row in rows:
            try:
                booked.add(datetime.fromisoformat(row[0]).isoformat(timespec="minutes"))
            except (TypeError, ValueError):
                continue
        slots = [slot for slot in slots if slot["datetime"][:16] not in booked]
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return slots


def book_appointment(lead_id: int, slot_datetime: str,
                     appointment_type: str = "test_drive",
                     notes: str = "",
                     user_email: str | None = None) -> dict:
    """Book a future, unreserved appointment slot for an existing lead."""
    if appointment_type not in {"test_drive", "viewing", "trade_eval", "financing"}:
        return {"error": "Unsupported appointment type"}
    if len(notes or "") > 500:
        return {"error": "Appointment notes are too long"}
    try:
        slot = datetime.fromisoformat(str(slot_datetime).replace("Z", "+00:00"))
        if slot.tzinfo is not None:
            slot = slot.astimezone().replace(tzinfo=None)
        now = datetime.now()
        if slot <= now or slot > now + timedelta(days=30):
            return {"error": "Choose a future slot within the next 30 days"}
        if slot.minute not in {0, 30} or slot.second or slot.microsecond:
            return {"error": "Appointments must start on the hour or half hour"}
        hours = _DEFAULT_DEALER_HOURS.get(slot.weekday())
        if not hours or not hours[0] <= slot.hour < hours[1]:
            return {"error": "Choose a time during the dealership's appointment hours"}
        slot_datetime = slot.isoformat(timespec="minutes")
    except (TypeError, ValueError):
        return {"error": "Invalid appointment date and time"}

    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL,
                vin TEXT,
                appointment_type TEXT DEFAULT 'test_drive',
                scheduled_at TEXT NOT NULL,
                status TEXT DEFAULT 'confirmed',
                notes TEXT,
                reminder_sent INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (lead_id) REFERENCES leads(id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_appointments_status_time "
                     "ON appointments(status, scheduled_at)")
        lead_row = conn.execute(
            "SELECT l.*, v.user_email AS vehicle_email, v.status AS vehicle_status "
            "FROM leads l LEFT JOIN vehicles v ON v.vin=l.vin WHERE l.id=?", (lead_id,)
        ).fetchone()
        if not lead_row:
            conn.rollback()
            return {"error": "Lead not found"}
        lead = dict(lead_row)
        tenant_email = str(lead.get("vehicle_email") or "").strip().lower()
        if not tenant_email or lead.get("vehicle_status") != "active":
            conn.rollback()
            return {"error": "The lead is not linked to an active dealership vehicle"}
        actor_email = (user_email or "").strip().lower()
        owner_email = os.getenv("LOTVUE_OWNER_EMAIL", "").strip().lower()
        if actor_email and actor_email not in {tenant_email, owner_email}:
            conn.rollback()
            return {"error": "Lead not found"}
        occupied = conn.execute(
            "SELECT 1 FROM appointments a "
            "JOIN leads l ON l.id=a.lead_id "
            "JOIN vehicles v ON v.vin=l.vin "
            "WHERE a.scheduled_at=? AND a.status='confirmed' "
            "AND LOWER(v.user_email)=? LIMIT 1",
            (slot_datetime, tenant_email),
        ).fetchone()
        if occupied:
            conn.rollback()
            return {"error": "That time slot is already booked"}
        conn.execute("""
            INSERT INTO appointments (lead_id, vin, appointment_type, scheduled_at, notes)
            VALUES (?, ?, ?, ?, ?)
        """, (lead_id, lead.get("vin", ""), appointment_type, slot_datetime, notes or ""))
        conn.execute("UPDATE leads SET status='appointment_set' WHERE id=?", (lead_id,))
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        return {"error": "Unable to reserve appointment slot"}
    finally:
        conn.close()

    return {
        "status": "confirmed",
        "lead_id": lead_id,
        "appointment_type": appointment_type,
        "scheduled_at": slot_datetime,
        "customer_name": lead.get("name", ""),
        "vin": lead.get("vin", ""),
    }


# =============================================================================
# FOLLOW-UP QUEUE — track what to send and when
# =============================================================================
def get_follow_up_queue(user_email: str | None = None) -> list:
    """Get pending follow-ups for one tenant (None is reserved for internal dispatch)."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Create follow_ups table if not exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS follow_ups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            channel TEXT NOT NULL,
            message_type TEXT NOT NULL,
            scheduled_for TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            sent_at TEXT,
            content TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (lead_id) REFERENCES leads(id)
        )
    """)

    now = datetime.now().isoformat()
    email = (user_email or "").strip().lower()
    tenant_filter = (
        " AND EXISTS (SELECT 1 FROM vehicles v WHERE v.vin=l.vin "
        "AND LOWER(v.user_email)=?)"
        if email else ""
    )
    params = (now, email) if email else (now,)
    try:
        rows = conn.execute(f"""
            SELECT f.*, l.name, l.email, l.phone, l.vin
            FROM follow_ups f
            JOIN leads l ON f.lead_id = l.id
            WHERE f.status = 'pending' AND f.scheduled_for <= ?{tenant_filter}
            ORDER BY f.scheduled_for ASC
            LIMIT 50
        """, params).fetchall()
    except Exception:
        conn.close()
        return []

    conn.close()
    return [dict(r) for r in rows]


def schedule_follow_up_sequence(lead_id: int, grade: str = "WARM") -> list:
    """
    Schedule the full follow-up cadence for a lead based on their grade.
    """
    cadence = _get_cadence(grade)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS follow_ups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            channel TEXT NOT NULL,
            message_type TEXT NOT NULL,
            scheduled_for TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            sent_at TEXT,
            content TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (lead_id) REFERENCES leads(id)
        )
    """)

    now = datetime.now()
    scheduled = []
    for step in cadence:
        send_at = now + timedelta(days=step["day"])
        conn.execute("""
            INSERT INTO follow_ups (lead_id, channel, message_type, scheduled_for)
            VALUES (?, ?, ?, ?)
        """, (lead_id, step["channel"], step["message"], send_at.isoformat()))
        scheduled.append({
            "day": step["day"],
            "channel": step["channel"],
            "message": step["message"],
            "scheduled_for": send_at.isoformat(),
        })

    conn.commit()
    conn.close()
    return scheduled


# =============================================================================
# DIGITAL DOSSIER — Impel AI-style per-lead package
# =============================================================================
def build_digital_dossier(lead: dict, vehicle: dict,
                          base_url: str = "") -> dict:
    """
    Build a digital dossier for a lead — everything they need to make a decision.
    Combines: vehicle specs, pricing, payment calculator, 360 link, video link.
    """
    vin = vehicle.get("vin", "")
    year = vehicle.get("year", "")
    make = vehicle.get("make", "")
    model = vehicle.get("model", "")

    try:
        price = int(float(str(vehicle.get("price", 0) or 0).replace(",", "").replace("$", "").strip()))
    except (ValueError, TypeError):
        price = 0

    # Payment calculator (simple — 72 months, 6.5% APR, 10% down)
    payments = {}
    if price > 0:
        for term in [36, 48, 60, 72, 84]:
            for apr in [4.9, 5.9, 6.9, 7.9]:
                down = int(price * 0.1)
                financed = price - down
                monthly_rate = (apr / 100) / 12
                if monthly_rate > 0:
                    payment = financed * (monthly_rate * (1 + monthly_rate)**term) / ((1 + monthly_rate)**term - 1)
                else:
                    payment = financed / term
                if term == 72 and apr == 6.9:
                    payments["default"] = {
                        "monthly": round(payment, 2),
                        "down_payment": down,
                        "term": term,
                        "apr": apr,
                        "financed": financed,
                    }
                payments[f"{term}mo_{apr}apr"] = round(payment, 2)

    return {
        "lead": {
            "name": lead.get("name", ""),
            "email": lead.get("email", ""),
            "phone": lead.get("phone", ""),
        },
        "vehicle": {
            "vin": vin,
            "title": f"{year} {make} {model}",
            "year": year,
            "make": make,
            "model": model,
            "trim": vehicle.get("trim_level", ""),
            "mileage": vehicle.get("mileage", ""),
            "engine": vehicle.get("engine", ""),
            "transmission": vehicle.get("transmission", ""),
            "drivetrain": vehicle.get("drivetrain", ""),
            "fuel_type": vehicle.get("fuel_type", ""),
            "color": vehicle.get("color", ""),
            "price": price,
            "price_display": f"${price:,}" if price else "Call for pricing",
        },
        "payments": payments,
        "links": {
            "360_viewer": f"{base_url}/360-viewer/{vin}",
            "video_walkaround": f"{base_url}/api/walkaround/{vin}/video",
            "listing": f"{base_url}/view/{vin}",
            "dossier_page": f"{base_url}/dossier/{vin}?lead={lead.get('id', '')}",
        },
        "generated_at": datetime.now().isoformat(),
    }


# =============================================================================
# LEAD ANALYTICS — dashboard data
# =============================================================================
def get_lead_analytics(user_email: str | None = None) -> dict:
    """Get funnel metrics for one tenant; None retains the internal all-tenant view."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    email = (user_email or "").strip().lower()
    predicate = (
        "EXISTS (SELECT 1 FROM vehicles v WHERE v.vin=l.vin "
        "AND LOWER(v.user_email)=?)"
        if email else "1=1"
    )
    args = (email,) if email else ()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM leads l WHERE {predicate}", args
        ).fetchone()["c"]
        today = datetime.now().strftime("%Y-%m-%d")
        today_count = conn.execute(
            f"SELECT COUNT(*) AS c FROM leads l WHERE {predicate} AND l.created_at LIKE ?",
            (*args, f"{today}%"),
        ).fetchone()["c"]
        by_status = {
            row["status"]: row["c"]
            for row in conn.execute(
                f"SELECT l.status, COUNT(*) AS c FROM leads l WHERE {predicate} GROUP BY l.status", args
            )
        }
        by_source = {
            row["source"]: row["c"]
            for row in conn.execute(
                f"SELECT l.source, COUNT(*) AS c FROM leads l WHERE {predicate} "
                "GROUP BY l.source ORDER BY c DESC LIMIT 10", args
            )
        }
        appt_count = 0
        queue_size = 0
        try:
            appt_count = conn.execute(
                "SELECT COUNT(*) AS c FROM appointments a JOIN leads l ON l.id=a.lead_id "
                f"WHERE a.status='confirmed' AND {predicate}", args
            ).fetchone()["c"]
        except sqlite3.Error:
            pass
        try:
            queue_size = conn.execute(
                "SELECT COUNT(*) AS c FROM follow_ups f JOIN leads l ON l.id=f.lead_id "
                f"WHERE f.status='pending' AND {predicate}", args
            ).fetchone()["c"]
        except sqlite3.Error:
            pass
    except sqlite3.Error:
        return {
            "total_leads": 0, "today_leads": 0, "by_status": {}, "by_source": {},
            "appointments_pending": 0, "follow_up_queue": 0, "conversion_rate": 0,
        }
    finally:
        conn.close()

    conversion_rate = 0
    if total > 0:
        converted = by_status.get("appointment_set", 0) + by_status.get("sold", 0)
        conversion_rate = round((converted / total) * 100, 1)
    return {
        "total_leads": total,
        "today_leads": today_count,
        "by_status": by_status,
        "by_source": by_source,
        "appointments_pending": appt_count,
        "follow_up_queue": queue_size,
        "conversion_rate": conversion_rate,
    }


# =============================================================================
# OUTBOUND DISPATCHER — actually SEND the scheduled follow-ups, compliantly.
# Email via the dealer's SMTP; SMS via Twilio (dark until TWILIO_* set).
# 'call' touches become dealer tasks (never auto-dialed). Guardrails: opt-out
# suppression, one-click unsubscribe + STOP footer, per-run cap. DEFAULT OFF —
# the periodic loop only runs when LOTVUE_AUTO_FOLLOWUP=1, so a dealer opts in.
# =============================================================================
def _conn():
    c = sqlite3.connect(str(DB_PATH)); c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS opt_outs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT DEFAULT '', phone TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')))""")
    return c


def record_opt_out(email: str = "", phone: str = "") -> bool:
    if not (email or phone):
        return False
    c = _conn()
    c.execute("INSERT INTO opt_outs (email, phone) VALUES (?,?)", (email or "", phone or ""))
    c.commit(); c.close()
    return True


def _is_opted_out(c, email: str, phone: str) -> bool:
    try:
        return bool(c.execute(
            "SELECT 1 FROM opt_outs WHERE (email!='' AND email=?) OR (phone!='' AND phone=?) LIMIT 1",
            (email or "\x00", phone or "\x00")).fetchone())
    except Exception:
        return False


def _get_vehicle(c, vin: str) -> dict:
    if not vin:
        return {}
    try:
        row = c.execute("SELECT * FROM vehicles WHERE vin=?", (vin.upper(),)).fetchone()
        return {k: row[k] for k in row.keys()} if row else {}
    except Exception:
        return {}


def _smtp_send(to_addr: str, subject: str, body: str) -> bool:
    host = os.getenv("LOTVUE_SMTP_HOST", "")
    if not (host and to_addr):
        return False
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = os.getenv("LOTVUE_SMTP_FROM", os.getenv("LOTVUE_SMTP_USER", "lotvue@localhost"))
        msg["To"] = to_addr
        with smtplib.SMTP(host, int(os.getenv("LOTVUE_SMTP_PORT", "587")), timeout=20) as s:
            s.starttls()
            user = os.getenv("LOTVUE_SMTP_USER", "")
            if user:
                s.login(user, os.getenv("LOTVUE_SMTP_PASS", ""))
            s.sendmail(msg["From"], [to_addr], msg.as_string())
        return True
    except Exception as e:
        print(f"[Follow-up] email send failed: {e}")
        return False


def _twilio_sms(to_n: str, body: str) -> bool:
    sid = os.getenv("TWILIO_ACCOUNT_SID", ""); token = os.getenv("TWILIO_AUTH_TOKEN", "")
    from_n = os.getenv("TWILIO_FROM_NUMBER", "")
    if not (sid and token and from_n and to_n):
        return False
    try:
        import base64
        data = urllib.parse.urlencode({"From": from_n, "To": to_n, "Body": body[:320]}).encode()
        req = urllib.request.Request(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", data=data, method="POST")
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode())
        with urllib.request.urlopen(req, timeout=20):
            return True
    except Exception as e:
        print(f"[Follow-up] SMS send failed: {e}")
        return False


def send_due_follow_ups(base_url: str = "", limit: int = 25) -> dict:
    """Send every follow-up whose scheduled_for is due. Idempotent (stamps sent_at),
    safe to call repeatedly. Email needs SMTP configured; SMS needs Twilio; otherwise
    the touch is marked 'unsendable' so the queue never spins. Returns a summary."""
    base_url = (base_url or os.getenv("LOTVUE_PUBLIC_URL", "http://127.0.0.1:8360")).rstrip("/")
    queue = get_follow_up_queue()
    sent = suppressed = unsendable = tasks = 0
    c = _conn()
    for f in queue[:limit]:
        fid = f["id"]; channel = (f["channel"] or "").lower()
        email = f.get("email") or ""; phone = f.get("phone") or ""; vin = f.get("vin") or ""
        if channel == "call":
            c.execute("UPDATE follow_ups SET status='dealer_task' WHERE id=?", (fid,)); tasks += 1; continue
        if _is_opted_out(c, email, phone):
            c.execute("UPDATE follow_ups SET status='suppressed' WHERE id=?", (fid,)); suppressed += 1; continue
        lead = {"name": f.get("name"), "email": email, "phone": phone, "vin": vin, "id": f["lead_id"]}
        content = generate_lead_response(lead, _get_vehicle(c, vin), response_type="follow_up")
        ok = False
        if channel == "email" and email:
            footer = (f"\n\n—\nReply STOP to opt out. "
                      f"Unsubscribe: {base_url}/api/unsubscribe?e={urllib.parse.quote(email)}")
            ok = _smtp_send(email, content["email_subject"], content["email_body"] + footer)
        elif channel == "text" and phone:
            ok = _twilio_sms(phone, content["sms"] + " Reply STOP to opt out.")
        if ok:
            c.execute("UPDATE follow_ups SET status='sent', sent_at=?, content=? WHERE id=?",
                      (datetime.now().isoformat(), content.get("sms", "")[:500], fid)); sent += 1
        else:
            c.execute("UPDATE follow_ups SET status='unsendable' WHERE id=?", (fid,)); unsendable += 1
    c.commit(); c.close()
    return {"sent": sent, "suppressed": suppressed, "unsendable": unsendable,
            "dealer_tasks": tasks, "considered": min(len(queue), limit)}
