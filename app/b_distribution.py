"""
B-Phase Distribution Extensions — LotVue360
B6a: CSV bulk vehicle importer (cars get in without hand entry)
B1:  Facebook Marketplace flow with ban guardrails (assisted posting — no ToS-violating automation)
B2:  Google Vehicle Listings feed
B4:  eBay Motors via official API (dark until credentialed)
B3:  Craigslist posting helper (already partially in main.py syndication — extended here)
B5:  Social posting via Postiz API
B6b: DealerCenter + Frazer DMS feed consumers

Wired in from main.py via:  from b_distribution import register_distribution; register_distribution(app)
"""

import csv
import io
import json
import os
import re
import sqlite3
from datetime import datetime
from ipaddress import ip_address
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from app_paths import DB_PATH, STORAGE_DIR
from auth_rate import check_auth_rate
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

router = APIRouter()


def _db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _actor_email(request: Request) -> str:
    token = request.headers.get("authorization", "")
    token = token[7:].strip() if token.lower().startswith("bearer ") else request.cookies.get("lotvue_token", "")
    from a2_extensions import JWT_ALG, JWTError, _jwt_secret, jwt
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALG])
    except JWTError as exc:
        raise HTTPException(401, "Sign in required") from exc
    email = str(payload.get("sub", "")).strip().lower()
    if payload.get("type") != "access" or not email:
        raise HTTPException(401, "Sign in required")
    conn = _db()
    exists = conn.execute("SELECT 1 FROM users WHERE LOWER(email)=?", (email,)).fetchone()
    conn.close()
    if not exists:
        raise HTTPException(401, "User no longer exists")
    return email


def _is_owner(email: str) -> bool:
    return bool(os.getenv("LOTVUE_OWNER_EMAIL", "").strip().lower() == email)


def _strict_vin(value: str) -> str:
    vin = str(value or "").strip().upper()
    if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
        raise HTTPException(404, "Vehicle not found")
    return vin


def _csv_cell(value) -> str:
    """Neutralize spreadsheet formulas in any user-controlled CSV field."""
    text = "".join(ch for ch in str(value if value is not None else "")
                   if ord(ch) in (9, 10, 13) or ord(ch) >= 32)
    if text.lstrip(" \t\r\n")[:1] in {"=", "+", "-", "@"}:
        return "'" + text
    return text


def _xml_escape(value) -> str:
    from xml.sax.saxutils import escape

    text = str(value if value is not None else "")
    cleaned = "".join(
        ch for ch in text
        if ord(ch) in (9, 10, 13)
        or 0x20 <= ord(ch) <= 0xD7FF
        or 0xE000 <= ord(ch) <= 0xFFFD
        or 0x10000 <= ord(ch) <= 0x10FFFF
    )
    return escape(cleaned)


def _usd_price(value) -> str:
    text = str(value if value is not None else "").replace("$", "").replace(",", "").strip()
    if len(text) > 16 or not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", text):
        return ""
    try:
        return text if 0 < float(text) <= 100_000_000 else ""
    except ValueError:
        return ""


# =============================================================================
# B6a — CSV BULK IMPORTER
# =============================================================================
# header alias → canonical vehicles column
_COL_ALIASES = {
    "vin": "vin", "vin#": "vin", "vin number": "vin",
    "year": "year", "yr": "year", "model year": "year",
    "make": "make", "manufacturer": "make",
    "model": "model",
    "trim": "trim_level", "trim_level": "trim_level", "series": "trim_level",
    "engine": "engine",
    "transmission": "transmission", "trans": "transmission",
    "drivetrain": "drivetrain", "drive": "drivetrain", "drive type": "drivetrain",
    "fuel": "fuel_type", "fuel_type": "fuel_type", "fuel type": "fuel_type",
    "body": "body_style", "body_style": "body_style", "body style": "body_style", "body type": "body_style",
    "color": "color", "exterior color": "color", "ext color": "color", "exterior_color": "color",
    "price": "price", "asking price": "price", "list price": "price", "internet price": "price",
    "selling price": "price", "listprice": "price",
    "mileage": "mileage", "miles": "mileage", "odometer": "mileage",
    "stock": "stock_number", "stock#": "stock_number", "stock #": "stock_number",
    "stock_number": "stock_number", "stocknumber": "stock_number", "stock number": "stock_number",
    "description": "description", "comments": "description", "notes": "description",
}
_IMPORTABLE = ["year", "make", "model", "trim_level", "engine", "transmission", "drivetrain",
               "fuel_type", "body_style", "color", "price", "mileage", "stock_number",
               "description"]


def _import_rows(rows: list, source: str = "csv", user_email: str = ""):
    """Import metadata only into the authenticated tenant's inventory."""
    user_email = (user_email or "").strip().lower()
    if not user_email:
        raise HTTPException(401, "Sign in required")
    owner = _is_owner(user_email)
    import billing
    free_left = billing.free_slots_left(user_email)  # None = no free-tier limit applies
    conn = _db()
    imported, updated, errors = 0, 0, []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for i, row in enumerate(rows, 1):
            vin = str(row.get("vin", "")).strip().upper()
            if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
                errors.append({"row": i, "vin": vin[:32], "error": "invalid VIN"})
                continue
            fields = {}
            too_long = False
            for key in _IMPORTABLE:
                value = str(row.get(key, "") or "").strip()
                if value:
                    limit = 2000 if key == "description" else 300
                    if len(value) > limit:
                        too_long = True
                        break
                    fields[key] = value
            if too_long:
                errors.append({"row": i, "vin": vin, "error": "metadata field is too long"})
                continue
            existing = conn.execute(
                "SELECT user_email, status FROM vehicles WHERE vin=?", (vin,)
            ).fetchone()
            if existing and existing["status"] != "active":
                errors.append({"row": i, "vin": vin, "error": "vehicle is inactive"})
                continue
            if existing and not owner and str(existing["user_email"] or "").strip().lower() != user_email:
                errors.append({"row": i, "vin": vin, "error": "vehicle belongs to another tenant"})
                continue
            try:
                if existing:
                    if fields:
                        sets = ", ".join(f"{key}=?" for key in fields)
                        conn.execute(f"UPDATE vehicles SET {sets} WHERE vin=? AND status='active'", (*fields.values(), vin))
                    updated += 1
                else:
                    if free_left is not None and not billing._owns_vehicle(user_email, vin):
                        if free_left <= 0:
                            errors.append({"row": i, "vin": vin, "error": "free vehicle limit reached",
                                           "upgrade_url": "/pricing"})
                            continue
                        free_left -= 1
                    cols = ["vin", "created_at", "user_email"] + list(fields.keys())
                    vals = [vin, datetime.now().isoformat(), user_email] + list(fields.values())
                    ph = ",".join("?" for _ in cols)
                    conn.execute(f"INSERT INTO vehicles ({','.join(cols)}) VALUES ({ph})", vals)
                    imported += 1
            except sqlite3.Error:
                errors.append({"row": i, "vin": vin, "error": "could not import row"})
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        conn.close()
        raise HTTPException(500, "Vehicle import failed")
    conn.close()
    print(f"[Import:{source}] imported={imported} updated={updated} errors={len(errors)}")
    return {"imported": imported, "updated": updated, "errors": errors,
            "total_rows": len(rows), "source": source}


@router.post("/api/import/csv")
async def import_csv(request: Request, file: UploadFile = File(...)):
    """Bulk import only into the current tenant's inventory."""
    from starlette.concurrency import run_in_threadpool

    email = await run_in_threadpool(_actor_email, request)
    raw = await file.read(10 * 1024 * 1024 + 1)
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, "CSV file exceeds 10 MB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(400, "CSV has no header row")
    rows = []
    for raw_row in reader:
        row = {}
        for k, v in raw_row.items():
            if k is None:
                continue
            canon = _COL_ALIASES.get(k.strip().lower())
            if canon:
                row[canon] = v
        if row:
            if len(rows) >= 5000:
                raise HTTPException(413, "CSV may contain at most 5,000 vehicle rows")
            rows.append(row)
    if not rows:
        raise HTTPException(400, f"No usable columns found. Headers seen: {reader.fieldnames}. "
                                 f"Need at least a 'vin' column.")
    safe_filename = Path(file.filename or "import.csv").name[:100]
    return await run_in_threadpool(_import_rows, rows, f"csv:{safe_filename}", email)


@router.get("/api/import/csv-template")
def csv_template():
    tpl = ("vin,year,make,model,trim,mileage,price,color,stock_number,description\n"
           "1HGCM82633A004352,2024,Honda,Accord,EX-L,12500,27995,Silver,STK1001,One owner clean carfax\n")
    return Response(tpl, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=lotvue_import_template.csv"})


# =============================================================================
# B1 — FACEBOOK MARKETPLACE (assisted, with ban guardrails)
# =============================================================================
# HARD GUARDRAILS (why this is assisted, not automated):
#  - Facebook ToS prohibits bot-driven Marketplace posting from personal accounts;
#    accounts get banned fast. The safe paths are (a) the official dealer
#    inventory partner feed (requires FB partner onboarding) or (b) a human
#    pasting a pre-built listing.
#  - This module therefore generates ready-to-paste listing packs and a CSV in
#    FB vehicle-catalog format, plus enforces pacing rules a human should follow.
FB_GUARDRAILS = {
    "max_posts_per_day": 5,           # new-account-safe pacing
    "min_minutes_between_posts": 45,  # burst posting is the #1 ban trigger
    "vary_description": True,         # identical text across listings trips spam detection
    "no_link_in_description": True,   # raw links in FBMP descriptions trigger review
    "photos_min": 4,                  # listings with <4 photos get low reach + spam suspicion
    "account_age_days_recommended": 30,
    "notes": [
        "Never use browser automation on a personal FB account — near-certain ban.",
        "Official path for volume: Facebook 'vehicles catalog' feed via Commerce Manager.",
        "Post manually using the pack below; respect the pacing limits.",
    ],
}

_fb_post_log_init = False


def _ensure_post_log():
    global _fb_post_log_init
    conn = _db()
    conn.execute("""CREATE TABLE IF NOT EXISTS post_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        vin TEXT, channel TEXT, posted_at TEXT, meta TEXT DEFAULT '',
        user_email TEXT DEFAULT ''
    )""")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(post_log)")}
    if "user_email" not in columns:
        conn.execute("ALTER TABLE post_log ADD COLUMN user_email TEXT DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_post_log_owner_channel_time "
                 "ON post_log(user_email, channel, posted_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_post_log_channel_time "
                 "ON post_log(channel, posted_at DESC)")
    conn.commit()
    conn.close()
    _fb_post_log_init = True


def _vehicle(vin: str):
    vin = _strict_vin(vin)
    conn = _db()
    v = conn.execute("SELECT * FROM vehicles WHERE vin=? AND status='active'", (vin,)).fetchone()
    conn.close()
    if not v:
        raise HTTPException(404, "Vehicle not found")
    return {k: v[k] for k in v.keys()}


def _public_base(*, require_public: bool = False):
    base = (os.getenv("LOTVUE_PUBLIC_URL", "").strip() or "http://127.0.0.1:8001").rstrip("/")
    parts = urlsplit(base)
    try:
        port = parts.port
    except ValueError as exc:
        raise HTTPException(503, "LOTVUE_PUBLIC_URL must be a valid absolute HTTP(S) URL") from exc
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment):
        raise HTTPException(503, "LOTVUE_PUBLIC_URL must be a valid absolute HTTP(S) URL")
    if require_public:
        host = parts.hostname.lower().rstrip(".")
        private_host = host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal"))
        try:
            addr = ip_address(host)
            private_host = private_host or not addr.is_global
        except ValueError:
            pass
        if parts.scheme != "https" or private_host or (port is not None and port != 443):
            raise HTTPException(503, "LOTVUE_PUBLIC_URL must use a public HTTPS host for feed exports")
    return base


def _photo_urls(vin: str, limit: int = 20):
    try:
        vin = _strict_vin(vin)
    except HTTPException:
        return []
    base = _public_base()
    root = STORAGE_DIR.resolve()
    vdir = (STORAGE_DIR / vin).resolve()
    if vdir.parent != root or not vdir.is_dir():
        return []
    urls = []
    for sub in ["spin", "exterior", "gallery"]:
        d = vdir / sub
        if d.is_symlink() or not d.is_dir():
            continue
        safe_files = [
            f for f in sorted(d.glob("*.jpg"))
            if (not f.is_symlink() and f.is_file()
                and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}\.jpg", f.name)
                and f.resolve().is_relative_to(vdir))
        ]
        for f in safe_files[:max(0, limit - len(urls))]:
            urls.append(f"{base}/storage/{vin.upper()}/{sub}/{f.name}")
        if len(urls) >= limit:
            break
    return urls


@router.get("/api/marketplace/facebook/{vin}/pack")
def fb_listing_pack(vin: str, request: Request):
    """Ready-to-paste Facebook Marketplace listing pack + guardrail check."""
    email = _actor_email(request)
    _ensure_post_log()
    v = _vehicle(vin)
    title = f"{v.get('year','')} {v.get('make','')} {v.get('model','')} {v.get('trim_level','') or ''}".strip()
    price = v.get("price") or ""
    mileage = v.get("mileage") or ""
    desc_lines = [
        f"{title} — {mileage} miles." if mileage else title,
        (v.get("description") or "").strip(),
        f"Engine: {v.get('engine')}" if v.get("engine") else "",
        f"Transmission: {v.get('transmission')}" if v.get("transmission") else "",
        f"Drivetrain: {v.get('drivetrain')}" if v.get("drivetrain") else "",
        "360 interactive tour available — ask and we'll send the link.",  # no raw link (guardrail)
        "Serious inquiries welcome. Test drives by appointment.",
    ]
    # Guardrail: pacing check from post_log
    conn = _db()
    today = datetime.now().strftime("%Y-%m-%d")
    scope_sql = "" if _is_owner(email) else " AND user_email=?"
    scope_params = () if _is_owner(email) else (email,)
    posts_today = conn.execute(
        "SELECT COUNT(*) FROM post_log WHERE channel='facebook' AND posted_at LIKE ?" + scope_sql,
        (today + "%", *scope_params)).fetchone()[0]
    last = conn.execute(
        "SELECT posted_at FROM post_log WHERE channel='facebook'" + scope_sql +
        " ORDER BY posted_at DESC LIMIT 1", scope_params
    ).fetchone()
    conn.close()
    minutes_since_last = None
    if last:
        try:
            minutes_since_last = (datetime.now() - datetime.fromisoformat(last[0])).total_seconds() / 60
        except Exception:
            pass
    photos = _photo_urls(vin)
    warnings = []
    if posts_today >= FB_GUARDRAILS["max_posts_per_day"]:
        warnings.append(f"PACING: already {posts_today} FB posts today (limit {FB_GUARDRAILS['max_posts_per_day']}). Wait until tomorrow.")
    if minutes_since_last is not None and minutes_since_last < FB_GUARDRAILS["min_minutes_between_posts"]:
        warnings.append(f"PACING: last FB post {minutes_since_last:.0f} min ago (min gap {FB_GUARDRAILS['min_minutes_between_posts']} min).")
    if len(photos) < FB_GUARDRAILS["photos_min"]:
        warnings.append(f"QUALITY: only {len(photos)} photos; FBMP listings perform poorly under {FB_GUARDRAILS['photos_min']}.")
    return {
        "vin": vin.upper(),
        "ok_to_post": not warnings,
        "warnings": warnings,
        "listing": {
            "title": title,
            "price": price,
            "mileage": mileage,
            "category": "Cars & Trucks",
            "description": "\n".join(x for x in desc_lines if x),
            "photo_urls": photos,
        },
        "guardrails": FB_GUARDRAILS,
    }


@router.post("/api/marketplace/facebook/{vin}/mark-posted")
def fb_mark_posted(vin: str, request: Request):
    """Log a manual FB post so pacing guardrails stay accurate."""
    email = _actor_email(request)
    _ensure_post_log()
    conn = _db()
    conn.execute("INSERT INTO post_log (vin, channel, posted_at, user_email) VALUES (?,?,?,?)",
                 (vin.upper(), "facebook", datetime.now().isoformat(), email))
    conn.commit()
    conn.close()
    return {"status": "logged", "vin": vin.upper(), "channel": "facebook"}


@router.get("/api/marketplace/facebook/catalog.csv")
def fb_catalog_csv(request: Request):
    """Download the authenticated tenant's Facebook vehicle catalog."""
    email = _actor_email(request)
    conn = _db()
    if _is_owner(email):
        rows = conn.execute(
            "SELECT * FROM vehicles WHERE status='active' AND (exterior_frames > 0 OR price != '')"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM vehicles WHERE status='active' AND LOWER(COALESCE(user_email,''))=? "
            "AND (exterior_frames > 0 OR price != '')", (email,)
        ).fetchall()
    conn.close()
    base = _public_base(require_public=True)
    buf = io.StringIO()
    w = csv.writer(buf)
    # address.* fields are required by Meta's vehicle feed spec; market id must be
    # a Comscore Market ID (Nielsen DMA codes stop working June 22, 2026)
    w.writerow(["fb_page_id", "vehicle_id", "title", "description", "url", "make", "model", "year",
                "mileage.value", "mileage.unit", "price", "state_of_vehicle", "vehicle_type",
                "exterior_color", "transmission", "fuel_type", "body_style", "image[0].url",
                "address.addr1", "address.city", "address.region", "address.postal_code",
                "address.country", "comscore_market_id"])
    page_id = os.getenv("LOTVUE_FB_PAGE_ID", "").strip()
    addr = [os.getenv("LOTVUE_DEALER_ADDR1", "").strip(), os.getenv("LOTVUE_DEALER_CITY", "").strip(),
            os.getenv("LOTVUE_DEALER_REGION", "").strip(), os.getenv("LOTVUE_DEALER_POSTAL", "").strip(),
            os.getenv("LOTVUE_DEALER_COUNTRY", "US").strip(),
            os.getenv("LOTVUE_COMSCORE_MARKET_ID", "").strip()]
    if not page_id or any(not value for value in addr):
        raise HTTPException(503, "Facebook catalog requires page id, dealer address, and Comscore market ID settings")
    for v in rows:
        price_val = _usd_price(v["price"])
        if not price_val:
            continue
        title = f"{v['year']} {v['make']} {v['model']}".strip()
        photos = _photo_urls(v["vin"], limit=1)
        row = [
            page_id, v["vin"], title, (v["description"] or title),
            f"{base}/view/{v['vin']}", v["make"], v["model"], v["year"],
            v["mileage"] or "0", "MI", f"{price_val} USD", "USED", "car_truck",
            v["color"] or "", v["transmission"] or "", v["fuel_type"] or "",
            v["body_style"] or "", photos[0] if photos else "", *addr,
        ]
        w.writerow([_csv_cell(value) for value in row])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=fb_vehicle_catalog.csv"})


# =============================================================================
# B2 — GOOGLE VEHICLE LISTINGS FEED
# =============================================================================
def _google_feed_email(token: str) -> str:
    """Authorize the crawler's secret URL and bind the feed to one tenant."""
    import hmac

    expected = os.getenv("LOTVUE_GOOGLE_FEED_TOKEN", "")
    tenant_email = os.getenv("LOTVUE_GOOGLE_FEED_USER_EMAIL", "").strip().lower()
    if (len(expected) < 32 or len(expected) > 512 or not re.fullmatch(
            r"[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,63}", tenant_email)):
        raise HTTPException(503, "Google vehicle feed is not configured")
    if (not token or len(token) != len(expected) or len(token) > 512
            or not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))):
        raise HTTPException(404, "Feed not found")
    return tenant_email


@router.get("/api/feeds/google-vehicle-listings.xml")
def google_vehicle_feed(token: str = ""):
    """Google Vehicle Listings feed (Merchant Center 'vehicle ads' XML).
    Register at Merchant Center → vehicle ads program, point it at this URL.

    Emits every REQUIRED attribute per the published spec
    (developers.google.com/vehicle-listings/reference/feed-specification,
    fetched 2026-08-12): vin, store_code, dealership_name, dealership_address,
    price, condition, make, model, trim (where available), year, mileage.
    Rows with no price or no photo are SKIPPED — Google disapproves them and
    partial items drag down feed quality score."""
    feed_email = _google_feed_email(token)
    base = _public_base(require_public=True)
    store_code = os.getenv("LOTVUE_GOOGLE_STORE_CODE", "").strip()
    dealer_name = os.getenv("LOTVUE_DEALER_NAME", "").strip()
    address_parts = (
        os.getenv("LOTVUE_DEALER_ADDR1", "").strip(), os.getenv("LOTVUE_DEALER_CITY", "").strip(),
        os.getenv("LOTVUE_DEALER_REGION", "").strip(), os.getenv("LOTVUE_DEALER_POSTAL", "").strip(),
        os.getenv("LOTVUE_DEALER_COUNTRY", "US").strip(),
    )
    if not store_code or not dealer_name or any(not part for part in address_parts):
        raise HTTPException(503, "Google vehicle feed requires store code and complete dealer address settings")
    dealer_addr = ", ".join(address_parts)
    conn = _db()
    rows = conn.execute(
        "SELECT vin, year, make, model, trim_level, price, mileage, color "
        "FROM vehicles WHERE status='active' AND LOWER(COALESCE(user_email,''))=? "
        "AND (exterior_frames > 0 OR price != '') ORDER BY vin LIMIT 5000", (feed_email,)
    ).fetchall()
    conn.close()
    items, skipped = [], 0
    for v in rows:
        try:
            vin = _strict_vin(v["vin"])
        except HTTPException:
            skipped += 1
            continue
        photos = _photo_urls(vin, limit=10)
        price_val = _usd_price(v["price"])
        if not photos or not price_val:
            skipped += 1
            continue
        e = _xml_escape
        title = e(f"{v['year']} {v['make']} {v['model']} {v['trim_level'] or ''}".strip())
        trim = f"\n      <g:trim>{e(v['trim_level'])}</g:trim>" if v["trim_level"] else ""
        color = (f"\n      <g:exterior_color>{e(v['color'])}</g:exterior_color>"
                 if v["color"] else "")
        imgs = "\n".join(f"      <g:additional_image_link>{e(u)}</g:additional_image_link>"
                         for u in photos[1:])
        items.append(f"""    <item>
      <g:id>{e(v['vin'])}</g:id>
      <g:vin>{e(v['vin'])}</g:vin>
      <title>{title}</title>
      <link>{e(base)}/view/{e(v['vin'])}</link>
      <g:store_code>{e(store_code)}</g:store_code>
      <g:dealership_name>{e(dealer_name)}</g:dealership_name>
      <g:dealership_address>{e(dealer_addr)}</g:dealership_address>
      <g:price>{e(price_val)} USD</g:price>
      <g:condition>used</g:condition>
      <g:make>{e(v['make'])}</g:make>
      <g:model>{e(v['model'])}</g:model>{trim}
      <g:year>{e(v['year'])}</g:year>
      <g:mileage>{e(v['mileage'] or '0')} miles</g:mileage>{color}
      <g:vehicle_fulfillment option="in_store" store_code="{e(store_code)}"/>
      <g:image_link>{e(photos[0])}</g:image_link>
{imgs}
    </item>""")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">
  <channel>
    <title>{_xml_escape(dealer_name)} Vehicle Inventory</title>
    <link>{_xml_escape(base)}</link>
    <description>Vehicle listings feed for Google vehicle ads</description>
    <!-- {len(items)} items emitted, {skipped} rows skipped (missing price or photos) -->
{chr(10).join(items)}
  </channel>
</rss>"""
    return Response(xml, media_type="application/xml", headers={"Cache-Control": "no-store"})


# =============================================================================
# B4 — EBAY MOTORS (official API; dark until credentialed)
# =============================================================================
def _ebay_creds():
    return {
        "app_id": os.getenv("EBAY_APP_ID", ""),
        "cert_id": os.getenv("EBAY_CERT_ID", ""),
        "dev_id": os.getenv("EBAY_DEV_ID", ""),
        "oauth_token": os.getenv("EBAY_OAUTH_TOKEN", ""),
        "sandbox": os.getenv("EBAY_SANDBOX", "1") == "1",
    }


@router.get("/api/marketplace/ebay/status")
def ebay_status():
    c = _ebay_creds()
    configured = bool(c["app_id"] and c["cert_id"] and c["oauth_token"])
    return {"configured": configured, "sandbox": c["sandbox"],
            "missing": [k for k in ("app_id", "cert_id", "oauth_token") if not c[k]],
            "how_to": "Set EBAY_APP_ID / EBAY_CERT_ID / EBAY_DEV_ID / EBAY_OAUTH_TOKEN env vars. "
                      "Get keys at developer.ebay.com (Motors category 6001)."}


@router.get("/api/marketplace/ebay/{vin}/draft")
def ebay_draft(vin: str, request: Request):
    """Build the eBay Motors listing payload for the authenticated tenant."""
    _actor_email(request)
    v = _vehicle(vin)
    photos = _photo_urls(vin, limit=12)
    title = f"{v.get('year','')} {v.get('make','')} {v.get('model','')} {v.get('trim_level','') or ''}".strip()[:80]
    price_val = str(v.get("price") or "0").replace("$", "").replace(",", "")
    payload = {
        "sku": f"LOTVUE-{vin.upper()}",
        "product": {
            "title": title,
            "description": (v.get("description") or title) +
                           f"\n\nInteractive 360 tour: {_public_base()}/view/{vin.upper()}",
            "aspects": {
                "Year": [str(v.get("year") or "")], "Make": [v.get("make") or ""],
                "Model": [v.get("model") or ""], "Trim": [v.get("trim_level") or ""],
                "Mileage": [str(v.get("mileage") or "")], "VIN": [vin.upper()],
                "Transmission": [v.get("transmission") or ""],
                "Fuel Type": [v.get("fuel_type") or ""],
                "Exterior Color": [v.get("color") or ""],
                "Body Type": [v.get("body_style") or ""],
            },
            "imageUrls": photos,
        },
        "condition": "USED_EXCELLENT",
        "categoryId": "6001",
        "pricingSummary": {"price": {"value": price_val, "currency": "USD"}},
    }
    return {"vin": vin.upper(), "configured": ebay_status()["configured"], "draft": payload,
            "note": "POST /api/marketplace/ebay/{vin}/publish to submit once credentialed."}


@router.post("/api/marketplace/ebay/{vin}/publish")
def ebay_publish(vin: str, request: Request):
    _actor_email(request)
    c = _ebay_creds()
    if not (c["app_id"] and c["oauth_token"]):
        raise HTTPException(503, "eBay API not credentialed. Set EBAY_* env vars (see /api/marketplace/ebay/status).")
    import urllib.request
    draft = ebay_draft(vin, request)["draft"]
    host = "api.sandbox.ebay.com" if c["sandbox"] else "api.ebay.com"
    req = urllib.request.Request(
        f"https://{host}/sell/inventory/v1/inventory_item/{draft['sku']}",
        data=json.dumps({"product": draft["product"], "condition": draft["condition"]}).encode(),
        headers={"Authorization": f"Bearer {c['oauth_token']}",
                 "Content-Type": "application/json", "Content-Language": "en-US"},
        method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            code = resp.status
    except Exception as e:
        raise HTTPException(502, f"eBay API call failed: {e}")
    _ensure_post_log()
    conn = _db()
    conn.execute("INSERT INTO post_log (vin, channel, posted_at, meta) VALUES (?,?,?,?)",
                 (vin.upper(), "ebay", datetime.now().isoformat(), f"http {code}"))
    conn.commit(); conn.close()
    return {"status": "submitted", "http": code, "sku": draft["sku"], "sandbox": c["sandbox"]}


# =============================================================================
# B3 — CRAIGSLIST PACK (manual-post helper; CL has no public posting API)
# =============================================================================
@router.get("/api/marketplace/craigslist/{vin}/pack")
def craigslist_pack(vin: str, request: Request):
    """Build a listing pack for the authenticated tenant's vehicle."""
    _actor_email(request)
    v = _vehicle(vin)
    title = f"{v.get('year','')} {v.get('make','')} {v.get('model','')} {v.get('trim_level','') or ''}".strip()
    price_val = str(v.get("price") or "").replace("$", "").replace(",", "")
    body = "\n".join(x for x in [
        title,
        f"Mileage: {v.get('mileage')}" if v.get("mileage") else "",
        f"Engine: {v.get('engine')}" if v.get("engine") else "",
        f"Transmission: {v.get('transmission')}" if v.get("transmission") else "",
        f"Color: {v.get('color')}" if v.get("color") else "",
        f"VIN: {vin.upper()}",
        "",
        (v.get("description") or "").strip(),
        "",
        f"Interactive 360 tour: {_public_base()}/view/{vin.upper()}",
    ] if x is not None)
    return {"vin": vin.upper(),
            "posting": {"title": f"{title} - ${price_val}" if price_val else title,
                        "price": price_val,
                        "category": "cars & trucks - by dealer",
                        "body": body,
                        "photo_urls": _photo_urls(vin, limit=24)}}


# =============================================================================
# B5 — SOCIAL VIA POSTIZ
# =============================================================================
def _postiz_cfg():
    return {"url": os.getenv("POSTIZ_URL", "").rstrip("/"), "key": os.getenv("POSTIZ_API_KEY", "")}


@router.get("/api/social/postiz/status")
def postiz_status():
    c = _postiz_cfg()
    if not (c["url"] and c["key"]):
        return {"configured": False, "how_to": "Set POSTIZ_URL and POSTIZ_API_KEY env vars."}
    import urllib.request
    try:
        req = urllib.request.Request(f"{c['url']}/api/public/v1/integrations",
                                     headers={"Authorization": c["key"]})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Postiz integrations response exceeds 1 MB")
        chans = json.loads(raw)
        return {"configured": True, "channels": chans}
    except Exception as e:
        return {"configured": True, "reachable": False, "error": str(e)}


class SocialPost(BaseModel):
    channels: list[str] = Field(default_factory=list, max_length=20)
    text: Optional[str] = Field(default="", max_length=3000)
    schedule_iso: Optional[str] = Field(default="", max_length=64)


@router.post("/api/social/postiz/{vin}/post")
def postiz_post(vin: str, body: SocialPost, request: Request):
    """Queue a bounded post for the authenticated tenant's vehicle."""
    email = _actor_email(request)
    for channel in body.channels:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", channel):
            raise HTTPException(400, "Invalid social integration id")
    if body.schedule_iso:
        try:
            datetime.fromisoformat(body.schedule_iso.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(400, "schedule_iso must be an ISO-8601 datetime") from exc
    c = _postiz_cfg()
    if not (c["url"] and c["key"]):
        raise HTTPException(503, "Postiz not configured (POSTIZ_URL / POSTIZ_API_KEY).")
    v = _vehicle(vin)
    title = f"{v.get('year','')} {v.get('make','')} {v.get('model','')}".strip()
    caption = body.text or (
        f"Just in: {title} — {v.get('mileage','')} miles. "
        f"Take the 360 tour: {_public_base()}/view/{vin.upper()}")
    import urllib.request
    when = body.schedule_iso or datetime.now().isoformat()
    payload = {
        "type": "draft" if not body.channels else "now",
        "date": when,
        "posts": [{"integration": {"id": ch}, "value": [{"content": caption}]}
                  for ch in (body.channels or ["__draft__"])],
    }
    try:
        req = urllib.request.Request(f"{c['url']}/api/public/v1/posts",
                                     data=json.dumps(payload).encode(),
                                     headers={"Authorization": c["key"], "Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Postiz response exceeds 1 MB")
        res = json.loads(raw)
    except Exception as e:
        raise HTTPException(502, f"Postiz call failed: {e}")
    _ensure_post_log()
    conn = _db()
    conn.execute("INSERT INTO post_log (vin, channel, posted_at, meta, user_email) VALUES (?,?,?,?,?)",
                 (vin.upper(), "postiz", datetime.now().isoformat(), json.dumps(body.channels), email))
    conn.commit(); conn.close()
    return {"status": "queued", "caption": caption, "postiz": res}


# =============================================================================
# B6b — DMS FEED CONSUMERS (DealerCenter / Frazer)
# =============================================================================
# Both DMSes export inventory as CSV (scheduled FTP drop or manual export).
# These endpoints consume those files: upload directly, or point at a watch
# folder that the DMS FTP-drops into.
_DMS_MAPS = {
    "dealercenter": {
        "vin": "vin", "year": "year", "make": "make", "model": "model",
        "trim": "trim_level", "miles": "mileage", "mileage": "mileage",
        "price": "price", "internet price": "price", "stock number": "stock_number",
        "stock #": "stock_number", "exterior color": "color", "color": "color",
        "engine": "engine", "transmission": "transmission", "drive type": "drivetrain",
        "fuel type": "fuel_type", "body style": "body_style", "description": "description",
        "comments": "description",
    },
    "frazer": {
        "vin": "vin", "yr": "year", "year": "year", "make": "make", "model": "model",
        "style": "body_style", "miles": "mileage", "mileage": "mileage",
        "retail": "price", "retail price": "price", "price": "price",
        "stk#": "stock_number", "stock#": "stock_number", "stock number": "stock_number",
        "color": "color", "engine": "engine", "trans": "transmission",
        "fuel": "fuel_type", "comments": "description",
    },
}


def _consume_dms_csv(text: str, dms: str, user_email: str):
    mapping = _DMS_MAPS.get(dms)
    if not mapping:
        raise HTTPException(400, f"Unknown DMS '{dms}'. Supported: {list(_DMS_MAPS)}")
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for raw_row in reader:
        row = {}
        for k, v in raw_row.items():
            if k is None:
                continue
            canon = mapping.get(k.strip().lower())
            if canon:
                row[canon] = v
        if row.get("vin"):
            if len(rows) >= 5000:
                raise HTTPException(413, "DMS CSV may contain at most 5,000 vehicle rows")
            rows.append(row)
    return _import_rows(rows, source=f"dms:{dms}", user_email=user_email)


@router.post("/api/dms/{dms}/import")
async def dms_import(dms: str, request: Request, file: UploadFile = File(...)):
    """Consume a DealerCenter or Frazer inventory CSV into this tenant."""
    from starlette.concurrency import run_in_threadpool

    email = await run_in_threadpool(_actor_email, request)
    raw = await file.read(10 * 1024 * 1024 + 1)
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, "DMS CSV exceeds 10 MB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return await run_in_threadpool(_consume_dms_csv, text, dms.lower(), email)


@router.post("/api/dms/{dms}/sync-folder")
def dms_sync_folder(dms: str, request: Request, folder: str = Form(...)):
    """Consume the newest CSV in this tenant's configured DMS watch folder."""
    email = _actor_email(request)
    base_root = Path(os.getenv("LOTVUE_DMS_WATCH_ROOT", str(STORAGE_DIR / "dms_inbox"))).resolve()
    if _is_owner(email):
        watch_root = base_root
    else:
        import hashlib
        watch_root = (base_root / hashlib.sha256(email.encode("utf-8")).hexdigest()[:20]).resolve()
    fdir = Path(folder).resolve()
    if watch_root != fdir and watch_root not in fdir.parents:
        raise HTTPException(403, "Folder must be inside this account's DMS watch folder")
    if not fdir.exists() or not fdir.is_dir():
        raise HTTPException(404, "DMS watch folder not found")
    csvs = []
    for candidate in fdir.glob("*.csv"):
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if (fdir == resolved.parent or fdir in resolved.parents) and resolved.is_file():
            csvs.append(resolved)
    csvs.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    if not csvs:
        raise HTTPException(404, "No CSV files in the watch folder")
    with csvs[0].open("rb") as csv_file:
        raw = csv_file.read(10 * 1024 * 1024 + 1)
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, "DMS CSV exceeds 10 MB")
    text = raw.decode("utf-8-sig", errors="replace")
    result = _consume_dms_csv(text, dms.lower(), email)
    result["file"] = csvs[0].name
    return result


# =============================================================================
# B7 — OUTBOUND MEDIA PUSH (FTP/SFTP to HomeNet / vAuto / website providers)
#       The retention lock-in GLO3D has: one capture → lands in the dealer's
#       existing inventory pipeline. Dark until FTP creds configured.
# =============================================================================
def _ftp_cfg():
    try:
        port = int(os.getenv("LOTVUE_FTP_PORT", "21"))
    except ValueError as exc:
        raise HTTPException(503, "LOTVUE_FTP_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise HTTPException(503, "LOTVUE_FTP_PORT must be between 1 and 65535")
    return {
        "host": os.getenv("LOTVUE_FTP_HOST", "").strip(),
        "port": port,
        "user": os.getenv("LOTVUE_FTP_USER", "").strip(),
        "password": os.getenv("LOTVUE_FTP_PASS", ""),
        "base_dir": os.getenv("LOTVUE_FTP_DIR", "/"),
        "tls": os.getenv("LOTVUE_FTP_TLS", "1").strip().lower() not in {"0", "false", "no"},
    }


@router.get("/api/syndication/push/status")
def push_status():
    c = _ftp_cfg()
    return {"configured": bool(c["host"] and c["user"]),
            "host": c["host"] or None,
            "how_to": "Set LOTVUE_FTP_HOST/PORT/USER/PASS/DIR to the photo-feed FTP your "
                      "inventory provider gives you (HomeNet, vAuto, Dealer Car Search, "
                      "website vendor). Then POST /api/syndication/push/{vin}."}


@router.post("/api/syndication/push/{vin}")
def push_vehicle_media(vin: str, request: Request):
    """Push this authenticated tenant's processed frames to its configured FTP drop."""
    email = _actor_email(request)
    c = _ftp_cfg()
    if not (c["host"] and c["user"]):
        raise HTTPException(503, "Media push not configured — set LOTVUE_FTP_* env vars "
                                 "(see /api/syndication/push/status).")
    vin = vin.upper()
    v = _vehicle(vin)
    fdir = STORAGE_DIR / vin / "spin"
    resolved_spin_dir = fdir.resolve()
    frames = sorted(
        path for path in fdir.glob("spin_*.jpg")
        if path.is_file() and re.fullmatch(r"spin_[A-Za-z0-9_.-]{1,120}\.jpg", path.name)
        and path.resolve().parent == resolved_spin_dir
    ) if fdir.is_dir() else []
    if not frames:
        raise HTTPException(404, "No processed frames to push — finalize the vehicle first.")
    public_base = _public_base(require_public=True)
    import ftplib
    pushed = []
    try:
        ftp_client = ftplib.FTP_TLS if c["tls"] else ftplib.FTP
        with ftp_client() as ftp:
            ftp.connect(c["host"], c["port"], timeout=30)
            ftp.login(c["user"], c["password"])
            if c["tls"]:
                ftp.prot_p()
            if c["base_dir"] and c["base_dir"] != "/":
                ftp.cwd(c["base_dir"])
            try:
                ftp.mkd(vin)
            except ftplib.error_perm:
                pass  # exists
            ftp.cwd(vin)
            for f in frames:
                with open(f, "rb") as fh:
                    ftp.storbinary(f"STOR {f.name}", fh)
                pushed.append(f.name)
            manifest_text = io.StringIO()
            manifest_writer = csv.writer(manifest_text)
            manifest_writer.writerow(["vin", "year", "make", "model", "price", "mileage", "frame_count", "viewer_url"])
            manifest_writer.writerow([_csv_cell(value) for value in (
                vin, v.get("year", ""), v.get("make", ""), v.get("model", ""),
                v.get("price", ""), v.get("mileage", ""), len(frames),
                f"{public_base}/view/{vin}",
            )])
            manifest = io.BytesIO(manifest_text.getvalue().encode("utf-8"))
            ftp.storbinary("STOR manifest.csv", manifest)
    except Exception as e:
        raise HTTPException(502, f"FTP push failed after {len(pushed)} files: {e}")
    _ensure_post_log()
    conn = _db()
    conn.execute("INSERT INTO post_log (vin, channel, posted_at, meta, user_email) VALUES (?,?,?,?,?)",
                 (vin, "ftp_push", datetime.now().isoformat(), f"{len(pushed)} frames -> {c['host']}", email))
    conn.commit(); conn.close()
    return {"status": "pushed", "vin": vin, "files": len(pushed) + 1, "host": c["host"]}


# =============================================================================
# B8 — AI VEHICLE DESCRIPTIONS (vAuto AutoWriter equivalent)
#       Local Ollama first (no cost, private), clean template fallback.
# =============================================================================
@router.get("/api/vehicle/{vin}/description")
def generate_description(vin: str, request: Request, tone: str = "professional"):
    _actor_email(request)
    check_auth_rate(request, "description", 20)
    tone = re.sub(r"[^A-Za-z -]", "", tone)[:40].strip() or "professional"
    v = _vehicle(vin)
    title = f"{v.get('year','')} {v.get('make','')} {v.get('model','')} {v.get('trim_level','') or ''}".strip()
    facts = [x for x in [
        f"{v.get('mileage')} miles" if v.get("mileage") else "",
        v.get("engine") or "", v.get("transmission") or "",
        v.get("drivetrain") or "", v.get("fuel_type") or "",
        f"{v.get('color')} exterior" if v.get("color") else "",
    ] if x]
    # Try local Ollama (:11434) — free, private, fast
    try:
        import urllib.request as _ur
        prompt = (f"Write a {tone}, truthful 90-120 word used-car listing description for: "
                  f"{title}. Facts: {', '.join(facts)}. No invented features, no pricing "
                  f"claims, no ALL CAPS, no emojis. End with a call to action to view the "
                  f"interactive 360 tour.")
        req = _ur.Request("http://127.0.0.1:11434/api/generate",
                          data=json.dumps({"model": os.getenv("LOTVUE_OLLAMA_MODEL", "llama3.2"),
                                           "prompt": prompt, "stream": False}).encode(),
                          headers={"Content-Type": "application/json"})
        with _ur.urlopen(req, timeout=90) as r:
            raw = r.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise ValueError("Local model response exceeds 64 KB")
        text = json.loads(raw).get("response", "").strip()
        # strip <think> blocks some local models emit
        if "</think>" in text:
            text = text.split("</think>", 1)[1].strip()
        # sanity gate: corrupted/garbage model output must never reach a listing
        letters = sum(c.isalpha() or c.isspace() for c in text)
        if 80 <= len(text) <= 1200 and letters / max(1, len(text)) > 0.82:
            return {"vin": vin.upper(), "source": "ollama", "description": text}
    except Exception:
        pass
    # Honest template fallback — facts only
    desc = (f"{title} with {', '.join(facts[:4]) if facts else 'full details on request'}. "
            f"Inspected and lot-ready. Explore every angle in our interactive 360° tour, "
            f"then book a test drive — this one moves fast.")
    return {"vin": vin.upper(), "source": "template", "description": desc}


# =============================================================================
# REGISTRATION
# =============================================================================
def register_distribution(app):
    _ensure_post_log()
    app.include_router(router)
    print("[B-Distribution] registered: CSV import, FB pack/catalog+guardrails, "
          "Google vehicle feed, eBay draft/publish, Craigslist pack, Postiz, DMS consumers")
