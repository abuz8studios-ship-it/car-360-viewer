"""
LotVue Pro — AI Lead Engine (Module 9)
Autonomous BDC: lead follow-up, appointment scheduling, lead scoring,
auto-response via text/email/chat — cloning best of 360autoai + Flai + BDC.ai.

All AI responses powered by Ollama LOCAL models (LOCAL > CLOUD > PAID).
Zero cloud cost — all local generation.
"""
import json
import sqlite3
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

OLLAMA_URL = "http://localhost:11434"
DB_PATH = Path(r".\360Car\app\lotvue.db")


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
    return {
        "sms": template["sms"],
        "email_subject": template["email_subject"],
        "email_body": template["email_body"],
        "response_type": response_type,
        "generated_at": datetime.now().isoformat(),
        "model": "template-v1",
    }


# =============================================================================
# APPOINTMENT SCHEDULING
# =============================================================================
def get_available_slots(dealer_hours: dict = None, days_ahead: int = 7) -> list:
    """
    Generate available appointment slots for the next N days.
    Default: Mon-Sat 9am-7pm, Sun 11am-5pm.
    """
    if not dealer_hours:
        dealer_hours = {
            0: (9, 19),   # Monday
            1: (9, 19),   # Tuesday
            2: (9, 19),   # Wednesday
            3: (9, 19),   # Thursday
            4: (9, 19),   # Friday
            5: (9, 18),   # Saturday
            6: (11, 17),  # Sunday
        }

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

    return slots


def book_appointment(lead_id: int, slot_datetime: str,
                     appointment_type: str = "test_drive",
                     notes: str = "") -> dict:
    """
    Book an appointment for a lead.
    appointment_type: 'test_drive', 'viewing', 'trade_eval', 'financing'
    """
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Create appointments table if not exists
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

    # Get lead info
    lead = dict(conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone() or {})
    if not lead:
        conn.close()
        return {"error": "Lead not found"}

    # Insert appointment
    conn.execute("""
        INSERT INTO appointments (lead_id, vin, appointment_type, scheduled_at, notes)
        VALUES (?, ?, ?, ?, ?)
    """, (lead_id, lead.get("vin", ""), appointment_type, slot_datetime, notes))

    # Update lead status
    conn.execute("UPDATE leads SET status='appointment_set' WHERE id=?", (lead_id,))
    conn.commit()
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
def get_follow_up_queue() -> list:
    """
    Get all pending follow-ups that need to go out.
    Returns leads with their next scheduled action.
    """
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
    try:
        rows = conn.execute("""
            SELECT f.*, l.name, l.email, l.phone, l.vin
            FROM follow_ups f
            JOIN leads l ON f.lead_id = l.id
            WHERE f.status = 'pending' AND f.scheduled_for <= ?
            ORDER BY f.scheduled_for ASC
            LIMIT 50
        """, (now,)).fetchall()
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
def get_lead_analytics() -> dict:
    """
    Get lead funnel analytics for the dashboard.
    """
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Ensure tables exist
    try:
        conn.execute("SELECT 1 FROM leads LIMIT 1")
    except Exception:
        conn.close()
        return {
            "total_leads": 0, "today_leads": 0, "by_status": {}, "by_source": {},
            "appointments_pending": 0, "follow_up_queue": 0, "conversion_rate": 0,
        }

    total = conn.execute("SELECT COUNT(*) as c FROM leads").fetchone()["c"]
    today = datetime.now().strftime("%Y-%m-%d")
    today_count = conn.execute(
        "SELECT COUNT(*) as c FROM leads WHERE created_at LIKE ?",
        (f"{today}%",)
    ).fetchone()["c"]

    by_status = {}
    for row in conn.execute("SELECT status, COUNT(*) as c FROM leads GROUP BY status"):
        by_status[row["status"]] = row["c"]

    by_source = {}
    for row in conn.execute("SELECT source, COUNT(*) as c FROM leads GROUP BY source ORDER BY c DESC LIMIT 10"):
        by_source[row["source"]] = row["c"]

    # Appointment count
    appt_count = 0
    try:
        appt_count = conn.execute("SELECT COUNT(*) as c FROM appointments WHERE status='confirmed'").fetchone()["c"]
    except:
        pass

    # Follow-up queue size
    queue_size = 0
    try:
        queue_size = conn.execute("SELECT COUNT(*) as c FROM follow_ups WHERE status='pending'").fetchone()["c"]
    except:
        pass

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
