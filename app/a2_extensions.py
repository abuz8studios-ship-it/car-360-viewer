"""
A2 Gap Extensions — LotVue360
Adds the features found missing in the 2026-06-11 parity audit vs LOTVUE360_EXACT_BUILD_SPEC.md:
  1. Lead email notification (SMTP, env-configured, graceful no-op)
  2. Leads CSV export
  3. Export formats: QR / GIF / MP4 / single frame / multi-frame ZIP
  4. Background library seeding (40 procedural backgrounds, files written to disk)
  5. Condition report: severity/note on hotspots + printable PDF
  6. JWT auth (python-jose) layered over the existing users table

Wired in from main.py via:  from a2_extensions import register_extensions; register_extensions(app)
"""

import io
import os
import csv
import json
import math
import zipfile
import sqlite3
import smtplib
import threading
import subprocess
from pathlib import Path
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Header
from fastapi.responses import Response, FileResponse, StreamingResponse
from pydantic import BaseModel
from PIL import Image, ImageDraw, ImageFilter

BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / "storage"
STATIC_DIR = BASE_DIR / "static"
DB_PATH = BASE_DIR / "vehicles.db"
FFMPEG = Path(os.getenv("LOTVUE_FFMPEG", r".\AI\ffmpeg\ffmpeg.exe"))

router = APIRouter()


def _db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _frames_dir(vin: str):
    """Same resolution order as the viewer: processed spin frames first, then raw exterior."""
    vdir = STORAGE_DIR / vin.upper()
    spin = vdir / "spin"
    if spin.exists() and any(spin.glob("spin_*.jpg")):
        return spin, sorted(spin.glob("spin_*.jpg"))
    ext = vdir / "exterior"
    if ext.exists():
        return ext, sorted(ext.glob("*.jpg"))
    return None, []


# =============================================================================
# 1. LEAD EMAIL NOTIFICATION
# =============================================================================
def notify_lead(lead: dict):
    """Email the dealer about a new lead. Config via env:
    LOTVUE_SMTP_HOST / LOTVUE_SMTP_PORT / LOTVUE_SMTP_USER / LOTVUE_SMTP_PASS /
    LOTVUE_SMTP_FROM / LOTVUE_NOTIFY_EMAIL. No-op (logged) when unconfigured."""
    host = os.getenv("LOTVUE_SMTP_HOST", "")
    to_addr = os.getenv("LOTVUE_NOTIFY_EMAIL", "")
    if not to_addr:
        try:
            conn = _db()
            row = conn.execute("SELECT email FROM dealers WHERE email != '' ORDER BY id LIMIT 1").fetchone()
            conn.close()
            if row:
                to_addr = row["email"]
        except Exception:
            pass
    if not host or not to_addr:
        print(f"[Lead-Notify] SMTP unconfigured (LOTVUE_SMTP_HOST/LOTVUE_NOTIFY_EMAIL) — lead {lead.get('id')} stored, no email sent")
        return False

    def _send():
        try:
            body = (
                f"New lead on LotVue360\n\n"
                f"Vehicle VIN: {lead.get('vin','')}\n"
                f"Name:  {lead.get('name','')}\n"
                f"Email: {lead.get('email','')}\n"
                f"Phone: {lead.get('phone','')}\n"
                f"Wants video walkaround: {'yes' if lead.get('wants_video') else 'no'}\n"
                f"Wants test drive: {'yes' if lead.get('wants_test_drive') else 'no'}\n\n"
                f"Message:\n{lead.get('message','')}\n\n"
                f"Received: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            )
            msg = MIMEText(body)
            msg["Subject"] = f"[LotVue360 Lead] {lead.get('name','?')} — {lead.get('vin','')}"
            msg["From"] = os.getenv("LOTVUE_SMTP_FROM", os.getenv("LOTVUE_SMTP_USER", "lotvue@localhost"))
            msg["To"] = to_addr
            port = int(os.getenv("LOTVUE_SMTP_PORT", "587"))
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.starttls()
                user = os.getenv("LOTVUE_SMTP_USER", "")
                if user:
                    s.login(user, os.getenv("LOTVUE_SMTP_PASS", ""))
                s.sendmail(msg["From"], [to_addr], msg.as_string())
            print(f"[Lead-Notify] emailed {to_addr} for lead {lead.get('id')}")
        except Exception as e:
            print(f"[Lead-Notify] FAILED: {e}")

    threading.Thread(target=_send, daemon=True).start()
    _notify_lead_sms(lead)
    return True


def _notify_lead_sms(lead: dict):
    """Instant SMS lead alert via Twilio — speed-to-lead is the metric dealers
    obsess over (leads answered <1h convert 7x better). Dark until TWILIO_* set."""
    sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    token = os.getenv("TWILIO_AUTH_TOKEN", "")
    from_n = os.getenv("TWILIO_FROM_NUMBER", "")
    to_n = os.getenv("LOTVUE_NOTIFY_SMS", "")
    if not (sid and token and from_n and to_n):
        return False

    def _send_sms():
        try:
            import urllib.request, urllib.parse, base64
            body = (f"LotVue lead: {lead.get('name','?')} {lead.get('phone','')} "
                    f"re {lead.get('vin','')}"
                    f"{' wants test drive' if lead.get('wants_test_drive') else ''}")
            data = urllib.parse.urlencode({"From": from_n, "To": to_n, "Body": body[:320]}).encode()
            req = urllib.request.Request(
                f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                data=data, method="POST")
            cred = base64.b64encode(f"{sid}:{token}".encode()).decode()
            req.add_header("Authorization", f"Basic {cred}")
            with urllib.request.urlopen(req, timeout=20) as r:
                print(f"[Lead-SMS] sent, http {r.status}")
        except Exception as e:
            print(f"[Lead-SMS] FAILED: {e}")

    threading.Thread(target=_send_sms, daemon=True).start()
    return True


# =============================================================================
# 2. LEADS CSV EXPORT
# =============================================================================
@router.get("/api/leads-export.csv")
async def export_leads_csv(vin: Optional[str] = None, status: Optional[str] = None):
    conn = _db()
    q = "SELECT * FROM leads WHERE 1=1"
    params = []
    if vin:
        q += " AND vin=?"; params.append(vin.upper())
    if status:
        q += " AND status=?"; params.append(status)
    q += " ORDER BY created_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["id", "vin", "name", "email", "phone", "message", "wants_video", "wants_test_drive", "source", "status", "created_at"]
    w.writerow(cols)
    for r in rows:
        w.writerow([r[c] if c in r.keys() else "" for c in cols])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=lotvue_leads.csv"})


# =============================================================================
# 3. EXPORT FORMATS — QR / GIF / MP4 / FRAME / ZIP
# =============================================================================
@router.get("/api/vehicle/{vin}/export/formats")
async def export_formats(vin: str):
    _, frames = _frames_dir(vin)
    return {
        "vin": vin.upper(), "frame_count": len(frames),
        "formats": {
            "qr": f"/api/vehicle/{vin}/export/qr.png",
            "gif": f"/api/vehicle/{vin}/export/spin.gif",
            "mp4": f"/api/vehicle/{vin}/export/spin.mp4",
            "frame": f"/api/vehicle/{vin}/export/frame/0",
            "zip": f"/api/vehicle/{vin}/export/frames.zip",
        },
    }


@router.get("/api/vehicle/{vin}/export/qr.png")
async def export_qr(vin: str, size: int = 8):
    import qrcode
    base = os.getenv("LOTVUE_PUBLIC_URL", "http://127.0.0.1:8001").rstrip("/")
    img = qrcode.make(f"{base}/view/{vin.upper()}", box_size=max(2, min(size, 20)))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png",
                    headers={"Content-Disposition": f"inline; filename={vin.upper()}_qr.png"})


@router.get("/api/vehicle/{vin}/export/spin.gif")
async def export_gif(vin: str, width: int = 640, fps: int = 12):
    fdir, frames = _frames_dir(vin)
    if not frames:
        raise HTTPException(404, "No frames for this vehicle")
    out_dir = STORAGE_DIR / vin.upper() / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"spin_{width}_{fps}.gif"
    newest_frame = max(f.stat().st_mtime for f in frames)
    if not out.exists() or out.stat().st_mtime < newest_frame:
        imgs = []
        for f in frames:
            im = Image.open(f).convert("RGB")
            h = int(im.height * width / im.width)
            imgs.append(im.resize((width, h)))
        imgs[0].save(out, save_all=True, append_images=imgs[1:],
                     duration=int(1000 / max(1, fps)), loop=0, optimize=True)
    return FileResponse(out, media_type="image/gif", filename=f"{vin.upper()}_spin.gif")


@router.get("/api/vehicle/{vin}/export/spin.mp4")
async def export_mp4(vin: str, fps: int = 12, loops: int = 3):
    fdir, frames = _frames_dir(vin)
    if not frames:
        raise HTTPException(404, "No frames for this vehicle")
    if not FFMPEG.exists():
        raise HTTPException(500, f"ffmpeg not found at {FFMPEG}")
    out_dir = STORAGE_DIR / vin.upper() / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"spin_{fps}.mp4"
    newest_frame = max(f.stat().st_mtime for f in frames)
    if not out.exists() or out.stat().st_mtime < newest_frame:
        # concat list looped for a longer rotation video
        lst = out_dir / "frames.txt"
        with open(lst, "w", encoding="utf-8") as fh:
            for _ in range(max(1, loops)):
                for f in frames:
                    fh.write(f"file '{f.as_posix()}'\nduration {1/max(1,fps):.4f}\n")
            fh.write(f"file '{frames[0].as_posix()}'\n")
        cmd = [str(FFMPEG), "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
               "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
               "-c:v", "libx264", "-preset", "fast", "-crf", "22", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0 or not out.exists():
            raise HTTPException(500, f"ffmpeg failed: {r.stderr[-400:]}")
    return FileResponse(out, media_type="video/mp4", filename=f"{vin.upper()}_spin.mp4")


@router.get("/api/vehicle/{vin}/export/frame/{n}")
async def export_frame(vin: str, n: int):
    _, frames = _frames_dir(vin)
    if not frames:
        raise HTTPException(404, "No frames for this vehicle")
    n = max(0, min(n, len(frames) - 1))
    return FileResponse(frames[n], media_type="image/jpeg",
                        filename=f"{vin.upper()}_frame_{n+1:02d}.jpg")


@router.get("/api/vehicle/{vin}/export/frames.zip")
async def export_zip(vin: str):
    _, frames = _frames_dir(vin)
    if not frames:
        raise HTTPException(404, "No frames for this vehicle")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, f in enumerate(frames):
            z.write(f, f"{vin.upper()}_frame_{i+1:02d}.jpg")
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": f"attachment; filename={vin.upper()}_frames.zip"})


# =============================================================================
# 4. BACKGROUND LIBRARY — 40 procedural backgrounds, written to disk
# =============================================================================
_BG_DEFS = [
    # slug, name, category, top RGB, bottom RGB, style
    ("studio_shadow",    "Studio Shadow",    "studio",   (245, 245, 247), (190, 192, 198), "vignette"),
    ("studio_white",     "Studio White",     "studio",   (255, 255, 255), (235, 236, 240), "plain"),
    ("studio_dark",      "Studio Dark",      "studio",   (52, 56, 64),    (18, 20, 26),    "vignette"),
    ("showroom_floor",   "Showroom Floor",   "showroom", (236, 238, 242), (170, 175, 185), "floor"),
    ("outdoor_sunset",   "Outdoor Sunset",   "outdoor",  (255, 183, 120), (90, 60, 90),    "horizon"),
    ("outdoor_overcast", "Outdoor Overcast", "outdoor",  (214, 220, 228), (140, 148, 158), "horizon"),
    ("concrete_floor",   "Concrete Floor",   "studio",   (200, 200, 200), (130, 132, 136), "floor"),
    ("glossy_black",     "Glossy Black",     "studio",   (40, 42, 48),    (5, 5, 8),       "floor"),
    ("studio_silver",    "Studio Silver",    "studio",   (230, 232, 238), (168, 172, 182), "vignette"),
    ("studio_pearl",     "Studio Pearl",     "studio",   (250, 248, 245), (216, 212, 206), "vignette"),
    ("studio_graphite",  "Studio Graphite",  "studio",   (96, 100, 110),  (40, 42, 50),    "vignette"),
    ("studio_blue",      "Studio Blue",      "studio",   (210, 224, 240), (130, 160, 200), "vignette"),
    ("studio_warm",      "Studio Warm",      "studio",   (248, 240, 228), (208, 190, 165), "vignette"),
    ("studio_cool",      "Studio Cool",      "studio",   (228, 236, 244), (160, 178, 198), "vignette"),
    ("studio_red_accent","Studio Red Accent","studio",   (240, 228, 228), (170, 120, 120), "vignette"),
    ("studio_gold",      "Studio Gold",      "studio",   (246, 238, 218), (200, 165, 92),  "vignette"),
    ("showroom_lights",  "Showroom Lights",  "showroom", (242, 244, 248), (150, 158, 172), "lights"),
    ("showroom_glass",   "Showroom Glass",   "showroom", (226, 234, 242), (148, 162, 178), "floor"),
    ("showroom_lux",     "Luxury Showroom",  "showroom", (238, 234, 226), (132, 124, 108), "lights"),
    ("showroom_modern",  "Modern Showroom",  "showroom", (246, 246, 248), (178, 182, 190), "floor"),
    ("showroom_classic", "Classic Showroom", "showroom", (240, 234, 222), (160, 148, 128), "floor"),
    ("showroom_night",   "Showroom Night",   "showroom", (60, 66, 80),    (16, 18, 26),    "lights"),
    ("outdoor_sky",      "Open Sky",         "outdoor",  (170, 205, 240), (235, 242, 250), "horizon"),
    ("outdoor_dawn",     "Dawn",             "outdoor",  (250, 210, 170), (150, 160, 200), "horizon"),
    ("outdoor_dusk",     "Dusk",             "outdoor",  (130, 110, 160), (40, 40, 70),    "horizon"),
    ("outdoor_forest",   "Forest Road",      "outdoor",  (180, 200, 170), (90, 115, 85),   "horizon"),
    ("outdoor_desert",   "Desert",           "outdoor",  (245, 222, 180), (190, 155, 110), "horizon"),
    ("outdoor_coast",    "Coastline",        "outdoor",  (175, 215, 235), (120, 165, 190), "horizon"),
    ("outdoor_mountain", "Mountain View",    "outdoor",  (205, 215, 230), (120, 135, 155), "horizon"),
    ("outdoor_city",     "City Street",      "outdoor",  (208, 210, 218), (110, 114, 126), "lights"),
    ("outdoor_night_city","City Night",      "outdoor",  (50, 55, 75),    (12, 14, 24),    "lights"),
    ("outdoor_autumn",   "Autumn",           "outdoor",  (240, 205, 160), (170, 120, 80),  "horizon"),
    ("seasonal_winter",  "Winter Snow",      "seasonal", (240, 245, 250), (200, 212, 224), "horizon"),
    ("seasonal_spring",  "Spring",           "seasonal", (225, 240, 220), (160, 195, 160), "horizon"),
    ("seasonal_summer",  "Summer",           "seasonal", (185, 220, 245), (240, 230, 180), "horizon"),
    ("seasonal_sale_red","Sale Red",         "seasonal", (235, 90, 80),   (140, 30, 30),   "vignette"),
    ("seasonal_sale_blue","Sale Blue",       "seasonal", (90, 140, 230),  (30, 55, 130),   "vignette"),
    ("seasonal_holiday", "Holiday",          "seasonal", (200, 60, 60),   (40, 90, 60),    "vignette"),
    ("seasonal_newyear", "New Year Gold",    "seasonal", (250, 235, 200), (180, 140, 60),  "lights"),
    ("seasonal_july4",   "Independence",     "seasonal", (220, 230, 245), (140, 60, 70),   "horizon"),
]

W, H = 1920, 1080
TW, TH = 320, 180


def _render_bg(top, bottom, style) -> Image.Image:
    img = Image.new("RGB", (W, H))
    px = img.load()
    for y in range(H):
        t = y / (H - 1)
        # ease the blend for a softer studio look
        tt = t * t * (3 - 2 * t)
        r = int(top[0] + (bottom[0] - top[0]) * tt)
        g = int(top[1] + (bottom[1] - top[1]) * tt)
        b = int(top[2] + (bottom[2] - top[2]) * tt)
        for x in range(0, W, 4):
            for xx in range(x, min(x + 4, W)):
                px[xx, y] = (r, g, b)
    d = ImageDraw.Draw(img, "RGBA")
    if style == "floor":
        d.rectangle([0, int(H * 0.72), W, H], fill=(0, 0, 0, 36))
        d.line([0, int(H * 0.72), W, int(H * 0.72)], fill=(255, 255, 255, 40), width=2)
    elif style == "horizon":
        d.rectangle([0, int(H * 0.66), W, H], fill=(0, 0, 0, 26))
    elif style == "lights":
        for i in range(6):
            cx = int(W * (i + 0.5) / 6)
            d.ellipse([cx - 130, -60, cx + 130, 90], fill=(255, 255, 255, 38))
        d.rectangle([0, int(H * 0.74), W, H], fill=(0, 0, 0, 40))
    elif style == "vignette":
        v = Image.new("L", (W, H), 0)
        dv = ImageDraw.Draw(v)
        dv.ellipse([-W * 0.25, -H * 0.35, W * 1.25, H * 1.35], fill=110)
        v = v.filter(ImageFilter.GaussianBlur(180))
        dark = Image.new("RGB", (W, H), (0, 0, 0))
        img = Image.composite(img, Image.blend(img, dark, 0.35), v)
    return img


def ensure_background_assets():
    """Generate the 40-background library on disk and register every entry in the DB."""
    bg_dir = STATIC_DIR / "backgrounds"
    thumb_dir = bg_dir / "thumbs"
    bg_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    made = 0
    for slug, name, cat, top, bottom, style in _BG_DEFS:
        f = bg_dir / f"{slug}.jpg"
        t = thumb_dir / f"{slug}.jpg"
        if not f.exists():
            img = _render_bg(top, bottom, style)
            img.save(f, "JPEG", quality=88)
            img.resize((TW, TH)).save(t, "JPEG", quality=80)
            made += 1
        elif not t.exists():
            Image.open(f).resize((TW, TH)).save(t, "JPEG", quality=80)
        conn.execute(
            """INSERT INTO backgrounds (name, slug, url, thumbnail_url, category, is_system)
               VALUES (?,?,?,?,?,1)
               ON CONFLICT(slug) DO UPDATE SET url=excluded.url, thumbnail_url=excluded.thumbnail_url,
                                               category=excluded.category, is_active=1""",
            (name, slug, f"/static/backgrounds/{slug}.jpg", f"/static/backgrounds/thumbs/{slug}.jpg", cat),
        )
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM backgrounds WHERE is_system=1 AND url != ''").fetchone()[0]
    conn.close()
    print(f"[Backgrounds] library ready: {n} system backgrounds ({made} rendered this boot)")


# =============================================================================
# 5. CONDITION REPORT — severity on hotspots + printable PDF
# =============================================================================
CONDITION_TYPES = ["scratch", "dent", "chip", "tear", "stain", "crack", "rust", "other"]
SEVERITIES = ["minor", "moderate", "severe"]


class ConditionUpdate(BaseModel):
    condition_type: Optional[str] = ""
    severity: Optional[str] = ""
    note: Optional[str] = ""


@router.post("/api/hotspot/{hotspot_id}/condition")
async def set_hotspot_condition(hotspot_id: int, body: ConditionUpdate):
    if body.severity and body.severity not in SEVERITIES:
        raise HTTPException(400, f"severity must be one of {SEVERITIES}")
    if body.condition_type and body.condition_type not in CONDITION_TYPES:
        raise HTTPException(400, f"condition_type must be one of {CONDITION_TYPES}")
    conn = _db()
    cur = conn.execute(
        "UPDATE hotspots SET condition_type=?, severity=?, note=? WHERE id=?",
        (body.condition_type or "", body.severity or "", body.note or "", hotspot_id),
    )
    conn.commit()
    found = cur.rowcount
    conn.close()
    if not found:
        raise HTTPException(404, "Hotspot not found")
    return {"status": "ok", "hotspot_id": hotspot_id}


@router.get("/api/vehicle/{vin}/condition-report.pdf")
async def condition_report_pdf(vin: str):
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet

    vin = vin.upper()
    conn = _db()
    veh = conn.execute("SELECT * FROM vehicles WHERE vin=?", (vin,)).fetchone()
    spots = conn.execute("SELECT * FROM hotspots WHERE vin=? ORDER BY photo_filename, id", (vin,)).fetchall()
    conn.close()
    if not veh:
        raise HTTPException(404, "Vehicle not found")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, topMargin=0.7 * inch, bottomMargin=0.7 * inch)
    styles = getSampleStyleSheet()
    title = f"{veh['year']} {veh['make']} {veh['model']}".strip() or vin
    story = [
        Paragraph(f"Vehicle Condition Report — {title}", styles["Title"]),
        Paragraph(f"VIN: {vin} &nbsp;&nbsp; Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", styles["Normal"]),
        Spacer(1, 14),
    ]
    if spots:
        data = [["#", "Frame", "Type", "Severity", "Tag / Note"]]
        for i, s in enumerate(spots, 1):
            k = s.keys()
            ctype = (s["condition_type"] if "condition_type" in k else "") or "—"
            sev = (s["severity"] if "severity" in k else "") or "—"
            note = (s["note"] if "note" in k else "") or ""
            tagnote = s["tag"] + (f" — {note}" if note else "")
            data.append([str(i), s["photo_filename"], ctype, sev, tagnote])
        tbl = Table(data, colWidths=[0.4 * inch, 1.6 * inch, 0.9 * inch, 0.9 * inch, 3.4 * inch])
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2E75B6")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#B8C4D0")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(tbl)
        sev_counts = {}
        for s in spots:
            k = s.keys()
            sv = (s["severity"] if "severity" in k else "") or "unrated"
            sev_counts[sv] = sev_counts.get(sv, 0) + 1
        story.append(Spacer(1, 12))
        story.append(Paragraph(
            "Summary: " + ", ".join(f"{v} {k}" for k, v in sev_counts.items()), styles["Normal"]))
    else:
        story.append(Paragraph("No condition markers recorded for this vehicle. "
                               "The vehicle has no documented damage hotspots.", styles["Normal"]))
    story.append(Spacer(1, 18))
    story.append(Paragraph("Generated by LotVue360 — lot photography & digital merchandising.", styles["Italic"]))
    doc.build(story)
    buf.seek(0)
    return Response(buf.getvalue(), media_type="application/pdf",
                    headers={"Content-Disposition": f"inline; filename={vin}_condition_report.pdf"})


# =============================================================================
# 6. JWT AUTH (python-jose) — layered over existing users table
# =============================================================================
from jose import jwt, JWTError

_SECRET_FILE = BASE_DIR / ".jwt_secret"


def _jwt_secret() -> str:
    env = os.getenv("LOTVUE_JWT_SECRET", "")
    if env:
        return env
    if _SECRET_FILE.exists():
        return _SECRET_FILE.read_text().strip()
    import secrets
    s = secrets.token_hex(32)
    _SECRET_FILE.write_text(s)
    return s


JWT_ALG = "HS256"
ACCESS_TTL_H = 24
REFRESH_TTL_D = 30


class TokenRequest(BaseModel):
    email: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


def _make_tokens(email: str, role: str = "user"):
    now = datetime.utcnow()
    access = jwt.encode({"sub": email, "role": role, "type": "access",
                         "exp": now + timedelta(hours=ACCESS_TTL_H)}, _jwt_secret(), algorithm=JWT_ALG)
    refresh = jwt.encode({"sub": email, "type": "refresh",
                          "exp": now + timedelta(days=REFRESH_TTL_D)}, _jwt_secret(), algorithm=JWT_ALG)
    return {"access_token": access, "refresh_token": refresh, "token_type": "bearer",
            "expires_in": ACCESS_TTL_H * 3600}


@router.post("/api/auth/token")
async def auth_token(body: TokenRequest, response: Response):
    import hashlib
    conn = _db()
    user = conn.execute("SELECT * FROM users WHERE email=?", (body.email.lower().strip(),)).fetchone()
    conn.close()
    if not user or user["password_hash"] != hashlib.sha256(body.password.encode()).hexdigest():
        raise HTTPException(401, "Invalid email or password")
    tokens = _make_tokens(user["email"])
    # browser sessions: HttpOnly cookie so dashboard fetch() works without headers
    response.set_cookie("lotvue_token", tokens["access_token"], httponly=True,
                        samesite="lax", max_age=ACCESS_TTL_H * 3600)
    return tokens


@router.post("/api/auth/logout")
async def auth_logout(response: Response):
    response.delete_cookie("lotvue_token")
    return {"status": "signed_out"}


@router.post("/api/auth/refresh")
async def auth_refresh(body: RefreshRequest):
    try:
        payload = jwt.decode(body.refresh_token, _jwt_secret(), algorithms=[JWT_ALG])
        if payload.get("type") != "refresh":
            raise HTTPException(401, "Not a refresh token")
        return _make_tokens(payload["sub"])
    except JWTError:
        raise HTTPException(401, "Invalid or expired refresh token")


def current_user(authorization: str = Header(default="")):
    """FastAPI dependency: validates Bearer token, returns user row dict."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token")
    try:
        payload = jwt.decode(authorization[7:], _jwt_secret(), algorithms=[JWT_ALG])
        if payload.get("type") != "access":
            raise HTTPException(401, "Not an access token")
    except JWTError:
        raise HTTPException(401, "Invalid or expired token")
    conn = _db()
    user = conn.execute("SELECT id, email, name, plan, credits FROM users WHERE email=?",
                        (payload["sub"],)).fetchone()
    conn.close()
    if not user:
        raise HTTPException(401, "User no longer exists")
    return {k: user[k] for k in user.keys()}


@router.get("/api/auth/me")
async def auth_me(authorization: str = Header(default="")):
    return current_user(authorization)


# =============================================================================
# REGISTRATION
# =============================================================================
def _migrate():
    conn = sqlite3.connect(str(DB_PATH))
    for col in ["condition_type TEXT DEFAULT ''", "severity TEXT DEFAULT ''", "note TEXT DEFAULT ''"]:
        try:
            conn.execute(f"ALTER TABLE hotspots ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass  # already present
    conn.commit()
    conn.close()


def register_extensions(app):
    _migrate()
    try:
        ensure_background_assets()
    except Exception as e:
        print(f"[Backgrounds] seeding failed (non-fatal): {e}")
    app.include_router(router)
    print("[A2-Extensions] registered: leads CSV, exports (qr/gif/mp4/frame/zip), "
          "condition report PDF, JWT auth, background library")
