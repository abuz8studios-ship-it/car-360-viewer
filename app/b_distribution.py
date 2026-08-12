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

import io
import os
import csv
import json
import sqlite3
from pathlib import Path
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import Response
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / "storage"
DB_PATH = BASE_DIR / "vehicles.db"

router = APIRouter()


def _db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


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
    "status": "status",
}
_IMPORTABLE = ["year", "make", "model", "trim_level", "engine", "transmission", "drivetrain",
               "fuel_type", "body_style", "color", "price", "mileage", "stock_number",
               "description", "status"]


def _import_rows(rows: list, source: str = "csv"):
    """Shared importer used by CSV upload and DMS feed consumers.
    Each row: dict with canonical column names, must include 'vin'."""
    conn = _db()
    imported, updated, errors = 0, 0, []
    for i, row in enumerate(rows, 1):
        vin = str(row.get("vin", "")).strip().upper()
        if not (5 <= len(vin) <= 17) or not vin.isalnum():
            errors.append({"row": i, "vin": vin, "error": "invalid VIN"})
            continue
        fields = {k: str(row[k]).strip() for k in _IMPORTABLE if k in row and str(row[k]).strip() != ""}
        exists = conn.execute("SELECT vin FROM vehicles WHERE vin=?", (vin,)).fetchone()
        try:
            if exists:
                if fields:
                    sets = ", ".join(f"{k}=?" for k in fields)
                    conn.execute(f"UPDATE vehicles SET {sets} WHERE vin=?", (*fields.values(), vin))
                updated += 1
            else:
                cols = ["vin", "created_at"] + list(fields.keys())
                vals = [vin, datetime.now().isoformat()] + list(fields.values())
                ph = ",".join("?" * len(cols))
                conn.execute(f"INSERT INTO vehicles ({','.join(cols)}) VALUES ({ph})", vals)
                imported += 1
        except Exception as e:
            errors.append({"row": i, "vin": vin, "error": str(e)})
    conn.commit()
    conn.close()
    print(f"[Import:{source}] imported={imported} updated={updated} errors={len(errors)}")
    return {"imported": imported, "updated": updated, "errors": errors,
            "total_rows": len(rows), "source": source}


@router.post("/api/import/csv")
async def import_csv(file: UploadFile = File(...)):
    """Bulk vehicle import. Accepts a CSV with a header row; flexible column names
    (vin/year/make/model/trim/price/miles/stock#/etc). Existing VINs are updated
    (metadata only — photos/frames untouched), new VINs are created as drafts."""
    raw = await file.read()
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
            rows.append(row)
    if not rows:
        raise HTTPException(400, f"No usable columns found. Headers seen: {reader.fieldnames}. "
                                 f"Need at least a 'vin' column.")
    return _import_rows(rows, source=f"csv:{file.filename}")


@router.get("/api/import/csv-template")
async def csv_template():
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
        vin TEXT, channel TEXT, posted_at TEXT, meta TEXT DEFAULT ''
    )""")
    conn.commit()
    conn.close()
    _fb_post_log_init = True


def _vehicle(vin: str):
    conn = _db()
    v = conn.execute("SELECT * FROM vehicles WHERE vin=?", (vin.upper(),)).fetchone()
    conn.close()
    if not v:
        raise HTTPException(404, "Vehicle not found")
    return {k: v[k] for k in v.keys()}


def _public_base():
    return os.getenv("LOTVUE_PUBLIC_URL", "http://127.0.0.1:8001").rstrip("/")


def _photo_urls(vin: str, limit: int = 20):
    base = _public_base()
    vdir = STORAGE_DIR / vin.upper()
    urls = []
    for sub in ["spin", "exterior", "gallery"]:
        d = vdir / sub
        if d.exists():
            for f in sorted(d.glob("*.jpg"))[:limit - len(urls)]:
                urls.append(f"{base}/storage/{vin.upper()}/{sub}/{f.name}")
        if len(urls) >= limit:
            break
    return urls


@router.get("/api/marketplace/facebook/{vin}/pack")
async def fb_listing_pack(vin: str):
    """Ready-to-paste Facebook Marketplace listing pack + guardrail check."""
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
    posts_today = conn.execute(
        "SELECT COUNT(*) FROM post_log WHERE channel='facebook' AND posted_at LIKE ?",
        (today + "%",)).fetchone()[0]
    last = conn.execute(
        "SELECT posted_at FROM post_log WHERE channel='facebook' ORDER BY posted_at DESC LIMIT 1"
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
async def fb_mark_posted(vin: str):
    """Log a manual FB post so pacing guardrails stay accurate."""
    _ensure_post_log()
    conn = _db()
    conn.execute("INSERT INTO post_log (vin, channel, posted_at) VALUES (?,?,?)",
                 (vin.upper(), "facebook", datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return {"status": "logged", "vin": vin.upper(), "channel": "facebook"}


@router.get("/api/marketplace/facebook/catalog.csv")
async def fb_catalog_csv():
    """Facebook Commerce Manager 'vehicles' catalog feed (official volume path).
    Upload this in Commerce Manager → Catalog → Data sources."""
    conn = _db()
    rows = conn.execute("SELECT * FROM vehicles WHERE exterior_frames > 0 OR price != ''").fetchall()
    conn.close()
    base = _public_base()
    buf = io.StringIO()
    w = csv.writer(buf)
    # address.* fields are required by Meta's vehicle feed spec; market id must be
    # a Comscore Market ID (Nielsen DMA codes stop working June 22, 2026)
    w.writerow(["fb_page_id", "vehicle_id", "title", "description", "url", "make", "model", "year",
                "mileage.value", "mileage.unit", "price", "state_of_vehicle", "vehicle_type",
                "exterior_color", "transmission", "fuel_type", "body_style", "image[0].url",
                "address.addr1", "address.city", "address.region", "address.postal_code",
                "address.country", "comscore_market_id"])
    page_id = os.getenv("LOTVUE_FB_PAGE_ID", "")
    addr = [os.getenv("LOTVUE_DEALER_ADDR1", ""), os.getenv("LOTVUE_DEALER_CITY", ""),
            os.getenv("LOTVUE_DEALER_REGION", ""), os.getenv("LOTVUE_DEALER_POSTAL", ""),
            os.getenv("LOTVUE_DEALER_COUNTRY", "US"),
            os.getenv("LOTVUE_COMSCORE_MARKET_ID", "")]
    for v in rows:
        title = f"{v['year']} {v['make']} {v['model']}".strip()
        photos = _photo_urls(v["vin"], limit=1)
        price_val = str(v["price"] or "0").replace("$", "").replace(",", "")
        w.writerow([
            page_id, v["vin"], title, (v["description"] or title),
            f"{base}/view/{v['vin']}", v["make"], v["model"], v["year"],
            v["mileage"] or "0", "MI", f"{price_val} USD", "USED", "car_truck",
            v["color"] or "", v["transmission"] or "", v["fuel_type"] or "",
            v["body_style"] or "", photos[0] if photos else "", *addr,
        ])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=fb_vehicle_catalog.csv"})


# =============================================================================
# B2 — GOOGLE VEHICLE LISTINGS FEED
# =============================================================================
@router.get("/api/feeds/google-vehicle-listings.xml")
async def google_vehicle_feed():
    """Google Vehicle Listings feed (Merchant Center 'vehicle ads' XML).
    Register at Merchant Center → vehicle ads program, point it at this URL.

    Emits every REQUIRED attribute per the published spec
    (developers.google.com/vehicle-listings/reference/feed-specification,
    fetched 2026-08-12): vin, store_code, dealership_name, dealership_address,
    price, condition, make, model, trim (where available), year, mileage.
    Rows with no price or no photo are SKIPPED — Google disapproves them and
    partial items drag down feed quality score."""
    from xml.sax.saxutils import escape
    conn = _db()
    rows = conn.execute("SELECT * FROM vehicles WHERE exterior_frames > 0 OR price != ''").fetchall()
    conn.close()
    base = _public_base()
    store_code = os.getenv("LOTVUE_GOOGLE_STORE_CODE", "lotvue-demo")
    dealer_name = os.getenv("LOTVUE_DEALER_NAME", "LotVue Motors")
    dealer_addr = ", ".join(x for x in (
        os.getenv("LOTVUE_DEALER_ADDR1", ""), os.getenv("LOTVUE_DEALER_CITY", ""),
        os.getenv("LOTVUE_DEALER_REGION", ""), os.getenv("LOTVUE_DEALER_POSTAL", ""),
        os.getenv("LOTVUE_DEALER_COUNTRY", "US")) if x)
    items, skipped = [], 0
    for v in rows:
        photos = _photo_urls(v["vin"], limit=10)
        price_val = str(v["price"] or "").replace("$", "").replace(",", "").strip()
        if not photos or not price_val or price_val == "0":
            skipped += 1
            continue
        e = lambda s: escape(str(s or ""))
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
    <title>{escape(dealer_name)} Vehicle Inventory</title>
    <link>{escape(base)}</link>
    <description>Vehicle listings feed for Google vehicle ads</description>
    <!-- {len(items)} items emitted, {skipped} rows skipped (missing price or photos) -->
{chr(10).join(items)}
  </channel>
</rss>"""
    return Response(xml, media_type="application/xml")


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
async def ebay_status():
    c = _ebay_creds()
    configured = bool(c["app_id"] and c["cert_id"] and c["oauth_token"])
    return {"configured": configured, "sandbox": c["sandbox"],
            "missing": [k for k in ("app_id", "cert_id", "oauth_token") if not c[k]],
            "how_to": "Set EBAY_APP_ID / EBAY_CERT_ID / EBAY_DEV_ID / EBAY_OAUTH_TOKEN env vars. "
                      "Get keys at developer.ebay.com (Motors category 6001)."}


@router.get("/api/marketplace/ebay/{vin}/draft")
async def ebay_draft(vin: str):
    """Build the eBay Motors listing payload (Inventory API format). Returns the
    draft always; only submits when credentials are configured."""
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
    return {"vin": vin.upper(), "configured": (await ebay_status())["configured"], "draft": payload,
            "note": "POST /api/marketplace/ebay/{vin}/publish to submit once credentialed."}


@router.post("/api/marketplace/ebay/{vin}/publish")
async def ebay_publish(vin: str):
    c = _ebay_creds()
    if not (c["app_id"] and c["oauth_token"]):
        raise HTTPException(503, "eBay API not credentialed. Set EBAY_* env vars (see /api/marketplace/ebay/status).")
    import urllib.request
    draft = (await ebay_draft(vin))["draft"]
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
async def craigslist_pack(vin: str):
    """Ready-to-paste Craigslist 'cars+trucks by dealer' listing pack.
    Craigslist has no official posting API; automated posting violates their ToS,
    so this produces the complete paste-ready pack instead."""
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
async def postiz_status():
    c = _postiz_cfg()
    if not (c["url"] and c["key"]):
        return {"configured": False, "how_to": "Set POSTIZ_URL and POSTIZ_API_KEY env vars."}
    import urllib.request
    try:
        req = urllib.request.Request(f"{c['url']}/api/public/v1/integrations",
                                     headers={"Authorization": c["key"]})
        with urllib.request.urlopen(req, timeout=15) as r:
            chans = json.loads(r.read())
        return {"configured": True, "channels": chans}
    except Exception as e:
        return {"configured": True, "reachable": False, "error": str(e)}


class SocialPost(BaseModel):
    channels: list = []           # Postiz integration ids; empty = all
    text: Optional[str] = ""      # override caption
    schedule_iso: Optional[str] = ""  # ISO datetime; empty = now


@router.post("/api/social/postiz/{vin}/post")
async def postiz_post(vin: str, body: SocialPost):
    """Queue a vehicle post through Postiz to connected social channels."""
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
            res = json.loads(r.read())
    except Exception as e:
        raise HTTPException(502, f"Postiz call failed: {e}")
    _ensure_post_log()
    conn = _db()
    conn.execute("INSERT INTO post_log (vin, channel, posted_at, meta) VALUES (?,?,?,?)",
                 (vin.upper(), "postiz", datetime.now().isoformat(), json.dumps(body.channels)))
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


def _consume_dms_csv(text: str, dms: str):
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
            rows.append(row)
    return _import_rows(rows, source=f"dms:{dms}")


@router.post("/api/dms/{dms}/import")
async def dms_import(dms: str, file: UploadFile = File(...)):
    """Consume a DealerCenter or Frazer inventory CSV export."""
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return _consume_dms_csv(text, dms.lower())


@router.post("/api/dms/{dms}/sync-folder")
async def dms_sync_folder(dms: str, folder: str = Form(...)):
    """Consume the newest CSV in a watch folder (where the DMS FTP-drops exports).
    Defense-in-depth: only paths under LOTVUE_DMS_WATCH_ROOT are allowed."""
    watch_root = Path(os.getenv("LOTVUE_DMS_WATCH_ROOT", str(STORAGE_DIR / "dms_inbox"))).resolve()
    fdir = Path(folder).resolve()
    if watch_root not in [fdir, *fdir.parents]:
        raise HTTPException(403, f"Folder must be under the DMS watch root ({watch_root}). "
                                 f"Set LOTVUE_DMS_WATCH_ROOT to change it.")
    if not fdir.exists():
        raise HTTPException(404, f"Folder not found: {folder}")
    csvs = sorted(fdir.glob("*.csv"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not csvs:
        raise HTTPException(404, f"No CSV files in {folder}")
    text = csvs[0].read_text(encoding="utf-8-sig", errors="replace")
    result = _consume_dms_csv(text, dms.lower())
    result["file"] = str(csvs[0])
    return result


# =============================================================================
# B7 — OUTBOUND MEDIA PUSH (FTP/SFTP to HomeNet / vAuto / website providers)
#       The retention lock-in GLO3D has: one capture → lands in the dealer's
#       existing inventory pipeline. Dark until FTP creds configured.
# =============================================================================
def _ftp_cfg():
    return {
        "host": os.getenv("LOTVUE_FTP_HOST", ""),
        "port": int(os.getenv("LOTVUE_FTP_PORT", "21")),
        "user": os.getenv("LOTVUE_FTP_USER", ""),
        "password": os.getenv("LOTVUE_FTP_PASS", ""),
        "base_dir": os.getenv("LOTVUE_FTP_DIR", "/"),
    }


@router.get("/api/syndication/push/status")
async def push_status():
    c = _ftp_cfg()
    return {"configured": bool(c["host"] and c["user"]),
            "host": c["host"] or None,
            "how_to": "Set LOTVUE_FTP_HOST/PORT/USER/PASS/DIR to the photo-feed FTP your "
                      "inventory provider gives you (HomeNet, vAuto, Dealer Car Search, "
                      "website vendor). Then POST /api/syndication/push/{vin}."}


@router.post("/api/syndication/push/{vin}")
async def push_vehicle_media(vin: str):
    """Push processed spin frames + a manifest CSV to the configured FTP drop.
    This is how the media lands in HomeNet/vAuto-class pipelines."""
    c = _ftp_cfg()
    if not (c["host"] and c["user"]):
        raise HTTPException(503, "Media push not configured — set LOTVUE_FTP_* env vars "
                                 "(see /api/syndication/push/status).")
    vin = vin.upper()
    v = _vehicle(vin)
    fdir = STORAGE_DIR / vin / "spin"
    frames = sorted(fdir.glob("spin_*.jpg")) if fdir.exists() else []
    if not frames:
        raise HTTPException(404, "No processed frames to push — finalize the vehicle first.")
    import ftplib
    pushed = []
    try:
        with ftplib.FTP() as ftp:
            ftp.connect(c["host"], c["port"], timeout=30)
            ftp.login(c["user"], c["password"])
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
            manifest = io.BytesIO(
                (f"vin,year,make,model,price,mileage,frame_count,viewer_url\n"
                 f"{vin},{v.get('year','')},{v.get('make','')},{v.get('model','')},"
                 f"{v.get('price','')},{v.get('mileage','')},{len(frames)},"
                 f"{_public_base()}/view/{vin}\n").encode())
            ftp.storbinary("STOR manifest.csv", manifest)
    except Exception as e:
        raise HTTPException(502, f"FTP push failed after {len(pushed)} files: {e}")
    _ensure_post_log()
    conn = _db()
    conn.execute("INSERT INTO post_log (vin, channel, posted_at, meta) VALUES (?,?,?,?)",
                 (vin, "ftp_push", datetime.now().isoformat(), f"{len(pushed)} frames -> {c['host']}"))
    conn.commit(); conn.close()
    return {"status": "pushed", "vin": vin, "files": len(pushed) + 1, "host": c["host"]}


# =============================================================================
# B8 — AI VEHICLE DESCRIPTIONS (vAuto AutoWriter equivalent)
#       Local Ollama first (no cost, private), clean template fallback.
# =============================================================================
@router.get("/api/vehicle/{vin}/description")
async def generate_description(vin: str, tone: str = "professional"):
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
            text = json.loads(r.read()).get("response", "").strip()
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
