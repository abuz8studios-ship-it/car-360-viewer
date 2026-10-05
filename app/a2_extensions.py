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

import csv
import io
import os
import re
import shutil
import smtplib
import sqlite3
import subprocess
import tempfile
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from app_paths import BASE_DIR, DATA_DIR, DB_PATH, STORAGE_DIR
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from PIL import Image, ImageDraw, ImageFilter
from pydantic import BaseModel, Field
from security import (
    hash_password,
    needs_rehash,
    secure_cookie_for_request,
    verify_password,
)

STATIC_DIR = BASE_DIR / "static"
def _ffmpeg_path():
    configured = os.getenv("LOTVUE_FFMPEG") or os.getenv("FFMPEG_PATH")
    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path.is_file():
            return configured_path
        located = shutil.which(configured)
        if located:
            return Path(located)
    executable = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    bundled = BASE_DIR / "bin" / executable
    if bundled.is_file():
        return bundled
    legacy = BASE_DIR.parents[1] / "AI" / "ffmpeg" / "ffmpeg.exe"
    if os.name == "nt" and legacy.is_file():
        return legacy
    return Path(shutil.which(executable) or executable)

FFMPEG = _ffmpeg_path()

router = APIRouter()
_LEAD_NOTIFY_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="lotvue-notify")
_LEAD_NOTIFY_SLOTS = threading.BoundedSemaphore(16)


def _submit_lead_notification(target) -> bool:
    """Keep SMTP/Twilio work off request threads with a bounded queue."""
    if not _LEAD_NOTIFY_SLOTS.acquire(blocking=False):
        print("[Lead-Notify] notification queue full; lead remains stored")
        return False
    try:
        future = _LEAD_NOTIFY_EXECUTOR.submit(target)
    except Exception:
        _LEAD_NOTIFY_SLOTS.release()
        return False
    future.add_done_callback(lambda _future: _LEAD_NOTIFY_SLOTS.release())
    return True


def _db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


MAX_EXPORT_FRAMES = 72
MAX_EXPORT_INPUT_BYTES = 512 * 1024 * 1024


def _frames_dir(vin: str):
    """Resolve a bounded set of regular vehicle frames; never follow storage symlinks."""
    vin = (vin or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{1,32}", vin):
        return None, []
    storage_root = STORAGE_DIR.resolve()
    vdir = STORAGE_DIR / vin
    if vdir.is_symlink() or vdir.resolve().parent != storage_root or not vdir.is_dir():
        return None, []

    for subdir, pattern in (("spin", "spin_*.jpg"), ("exterior", "*.jpg")):
        directory = vdir / subdir
        if directory.is_symlink() or not directory.is_dir():
            continue
        resolved_dir = directory.resolve()
        frames = []
        for candidate in sorted(directory.glob(pattern)):
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            if (candidate.is_symlink() or resolved.parent != resolved_dir or not resolved.is_file()
                    or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}\.jpg", candidate.name)):
                continue
            frames.append(resolved)
            if len(frames) >= MAX_EXPORT_FRAMES:
                break
        if frames:
            return resolved_dir, frames
    return None, []


# =============================================================================
# 1. LEAD EMAIL NOTIFICATION
# =============================================================================
def _tenant_lead_contact(vin: str) -> tuple[str, str]:
    vin = str(vin or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{1,32}", vin):
        return "", ""
    try:
        conn = _db()
        row = conn.execute(
            "SELECT d.email, d.phone FROM vehicles v LEFT JOIN dealers d "
            "ON LOWER(COALESCE(NULLIF(d.user_email,''),d.email,''))=LOWER(v.user_email) "
            "WHERE v.vin=? AND v.status='active' ORDER BY d.id LIMIT 1", (vin,)
        ).fetchone()
        conn.close()
        return (str(row["email"] or "").strip(), str(row["phone"] or "").strip()) if row else ("", "")
    except Exception:
        return "", ""


def notify_lead(lead: dict):
    """Send lead notices only to the dealer linked to the active vehicle."""
    host = os.getenv("LOTVUE_SMTP_HOST", "")
    to_addr, _tenant_phone = _tenant_lead_contact(lead.get("vin", ""))
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

    _submit_lead_notification(_send)
    _notify_lead_sms(lead)
    return True


def _notify_lead_sms(lead: dict):
    """Instant SMS lead alert via Twilio — speed-to-lead is the metric dealers
    obsess over (leads answered <1h convert 7x better). Dark until TWILIO_* set."""
    sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    token = os.getenv("TWILIO_AUTH_TOKEN", "")
    from_n = os.getenv("TWILIO_FROM_NUMBER", "")
    _tenant_email, to_n = _tenant_lead_contact(lead.get("vin", ""))
    if not (sid and token and from_n and to_n):
        return False

    def _send_sms():
        try:
            import base64
            import urllib.parse
            import urllib.request
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

    return _submit_lead_notification(_send_sms)


def _safe_csv_value(value):
    """Prefix spreadsheet formulas even when whitespace precedes the operator."""
    if isinstance(value, str) and value.lstrip(" \t\r\n")[:1] in {"=", "+", "-", "@"}:
        return "'" + value
    return value


# =============================================================================
# 2. LEADS CSV EXPORT
# =============================================================================
@router.get("/api/leads-export.csv")
def export_leads_csv(request: Request, vin: Optional[str] = None, status: Optional[str] = None):
    """Export only the authenticated dealer's lead rows, with spreadsheet formula guards."""
    token = request.headers.get("authorization", "")
    token = token[7:].strip() if token.lower().startswith("bearer ") else request.cookies.get("lotvue_token", "")
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALG])
    except JWTError as exc:
        raise HTTPException(401, "Sign in required") from exc
    email = str(payload.get("sub", "")).strip().lower()
    if payload.get("type") != "access" or not email:
        raise HTTPException(401, "Sign in required")
    if vin and not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", vin.strip()):
        raise HTTPException(400, "Invalid vehicle identifier")
    if status and len(status) > 40:
        raise HTTPException(400, "Status filter is too long")
    conn = _db()
    if not conn.execute("SELECT 1 FROM users WHERE LOWER(email)=?", (email,)).fetchone():
        conn.close()
        raise HTTPException(401, "User no longer exists")
    clauses = []
    params = []
    owner_email = os.getenv("LOTVUE_OWNER_EMAIL", "").strip().lower()
    if not owner_email or email != owner_email:
        clauses.append("EXISTS (SELECT 1 FROM vehicles v WHERE v.vin=leads.vin AND LOWER(v.user_email)=?)")
        params.append(email)
    if vin:
        clauses.append("leads.vin=?")
        params.append(vin.strip().upper())
    if status:
        clauses.append("leads.status=?")
        params.append(status)
    where = " AND ".join(clauses) or "1=1"
    rows = conn.execute(
        f"SELECT * FROM leads WHERE {where} ORDER BY created_at DESC, id DESC LIMIT 10000",
        params,
    ).fetchall()
    conn.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["id", "vin", "name", "email", "phone", "message", "wants_video", "wants_test_drive", "source", "status", "created_at"]
    w.writerow(cols)
    for row in rows:
        values = []
        for col in cols:
            value = row[col] if col in row.keys() else ""
            values.append(_safe_csv_value(value))
        w.writerow(values)
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=lotvue_leads.csv"})


# =============================================================================
# 3. EXPORT FORMATS — QR / GIF / MP4 / FRAME / ZIP
# =============================================================================
@router.get("/api/vehicle/{vin}/export/formats")
def export_formats(vin: str):
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
def export_qr(vin: str, size: int = 8):
    import qrcode
    base = os.getenv("LOTVUE_PUBLIC_URL", "http://127.0.0.1:8001").rstrip("/")
    img = qrcode.make(f"{base}/view/{vin.upper()}", box_size=max(2, min(size, 20)))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png",
                    headers={"Content-Disposition": f"inline; filename={vin.upper()}_qr.png"})


@router.get("/api/vehicle/{vin}/export/spin.gif")
def export_gif(vin: str, width: int = 640, fps: int = 12):
    """Build a bounded GIF without retaining full-resolution originals in memory."""
    _frame_dir, frames = _frames_dir(vin)
    if not frames:
        raise HTTPException(404, "No frames for this vehicle")
    width = max(160, min(width, 640))
    fps = max(1, min(fps, 30))
    if sum(frame.stat().st_size for frame in frames) > MAX_EXPORT_INPUT_BYTES:
        raise HTTPException(413, "Frame set is too large to export")

    vin = vin.strip().upper()
    out_dir = STORAGE_DIR / vin / "exports"
    if out_dir.is_symlink():
        raise HTTPException(404, "Export directory is unavailable")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"spin_{width}_{fps}.gif"
    newest_frame = max(frame.stat().st_mtime for frame in frames)
    if not out.exists() or out.stat().st_mtime < newest_frame:
        temp_out = out_dir / f".spin-{uuid.uuid4().hex}.gif"
        images = []
        try:
            for frame_path in frames:
                with Image.open(frame_path) as source:
                    image_width, image_height = source.size
                    if (image_width <= 0 or image_height <= 0
                            or image_width * image_height > 40_000_000):
                        raise HTTPException(413, "A frame exceeds the safe image pixel limit")
                    source.thumbnail((width, int(width * 1.5)), Image.Resampling.LANCZOS)
                    images.append(source.convert("RGB"))
            images[0].save(temp_out, format="GIF", save_all=True, append_images=images[1:],
                           duration=max(1, int(1000 / fps)), loop=0, optimize=True)
            os.replace(temp_out, out)
        finally:
            for image in images:
                image.close()
            temp_out.unlink(missing_ok=True)
    return FileResponse(out, media_type="image/gif", filename=f"{vin}_spin.gif")


@router.get("/api/vehicle/{vin}/export/spin.mp4")
def export_mp4(vin: str, fps: int = 12, loops: int = 3):
    """Encode a short bounded spin clip in a worker thread."""
    _frame_dir, frames = _frames_dir(vin)
    if not frames:
        raise HTTPException(404, "No frames for this vehicle")
    if not FFMPEG.exists():
        raise HTTPException(500, f"ffmpeg not found at {FFMPEG}")
    fps = max(1, min(fps, 30))
    loops = max(1, min(loops, 5))
    if sum(frame.stat().st_size for frame in frames) > MAX_EXPORT_INPUT_BYTES:
        raise HTTPException(413, "Frame set is too large to export")

    vin = vin.strip().upper()
    out_dir = STORAGE_DIR / vin / "exports"
    if out_dir.is_symlink():
        raise HTTPException(404, "Export directory is unavailable")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"spin_{fps}.mp4"
    newest_frame = max(frame.stat().st_mtime for frame in frames)
    if not out.exists() or out.stat().st_mtime < newest_frame:
        temp_out = out_dir / f".spin-{uuid.uuid4().hex}.mp4"
        list_path = out_dir / f".frames-{uuid.uuid4().hex}.txt"
        try:
            with list_path.open("w", encoding="utf-8") as stream:
                for _ in range(loops):
                    for frame_path in frames:
                        path_text = frame_path.as_posix()
                        if any(char in path_text for char in ("\n", "\r", "'")):
                            raise HTTPException(400, "Storage path cannot be represented for video export")
                        stream.write(f"file '{path_text}'\nduration {1 / fps:.4f}\n")
                first_path = frames[0].as_posix()
                stream.write(f"file '{first_path}'\n")
            command = [str(FFMPEG), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                       "-i", str(list_path), "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
                       "-c:v", "libx264", "-preset", "fast", "-crf", "22", str(temp_out)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
            if result.returncode != 0 or not temp_out.is_file() or temp_out.stat().st_size <= 0:
                raise HTTPException(500, f"ffmpeg failed: {(result.stderr or '')[-400:]}")
            os.replace(temp_out, out)
        finally:
            list_path.unlink(missing_ok=True)
            temp_out.unlink(missing_ok=True)
    return FileResponse(out, media_type="video/mp4", filename=f"{vin}_spin.mp4")


@router.get("/api/vehicle/{vin}/export/frame/{n}")
def export_frame(vin: str, n: int):
    _, frames = _frames_dir(vin)
    if not frames:
        raise HTTPException(404, "No frames for this vehicle")
    n = max(0, min(n, len(frames) - 1))
    return FileResponse(frames[n], media_type="image/jpeg",
                        filename=f"{vin.upper()}_frame_{n+1:02d}.jpg")


@router.get("/api/vehicle/{vin}/export/frames.zip")
def export_zip(vin: str):
    """Stream source frames to a disk-backed ZIP; avoid whole-archive RAM copies."""
    _frame_dir, frames = _frames_dir(vin)
    if not frames:
        raise HTTPException(404, "No frames for this vehicle")
    total_bytes = sum(frame.stat().st_size for frame in frames)
    if total_bytes > MAX_EXPORT_INPUT_BYTES:
        raise HTTPException(413, "Frame set is too large to export")

    vin = vin.strip().upper()
    out_dir = STORAGE_DIR / vin / "exports"
    if out_dir.is_symlink():
        raise HTTPException(404, "Export directory is unavailable")
    out_dir.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".frames-", suffix=".zip", dir=out_dir)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_STORED) as archive:
            for index, frame_path in enumerate(frames, 1):
                archive.write(frame_path, f"{vin}_frame_{index:02d}.jpg")
        from starlette.background import BackgroundTask
        return FileResponse(
            temp_path, media_type="application/zip", filename=f"{vin}_frames.zip",
            background=BackgroundTask(temp_path.unlink, missing_ok=True),
        )
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


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
    # The old Python-per-pixel loop performed over 80 million assignments while
    # generating the 40-asset catalog. Vectorize the row gradient and broadcast
    # it across the width; only one ~6 MiB RGB image is held at a time.
    progress = np.arange(H, dtype=np.float64) / (H - 1)
    eased = progress * progress * (3 - 2 * progress)
    top_rgb = np.asarray(top, dtype=np.float64)
    bottom_rgb = np.asarray(bottom, dtype=np.float64)
    rows = (top_rgb + (bottom_rgb - top_rgb) * eased[:, None]).astype(np.uint8)
    pixels = np.broadcast_to(rows[:, None, :], (H, W, 3)).copy()
    img = Image.fromarray(pixels)
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
def set_hotspot_condition(hotspot_id: int, body: ConditionUpdate, request: Request):
    token = request.headers.get("authorization", "")
    token = token[7:].strip() if token.lower().startswith("bearer ") else request.cookies.get("lotvue_token", "")
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALG])
    except JWTError as exc:
        raise HTTPException(401, "Sign in required") from exc
    email = str(payload.get("sub", "")).strip().lower()
    if payload.get("type") != "access" or not email:
        raise HTTPException(401, "Sign in required")
    if body.severity and body.severity not in SEVERITIES:
        raise HTTPException(400, f"severity must be one of {SEVERITIES}")
    if body.condition_type and body.condition_type not in CONDITION_TYPES:
        raise HTTPException(400, f"condition_type must be one of {CONDITION_TYPES}")
    conn = _db()
    if not conn.execute("SELECT 1 FROM users WHERE LOWER(email)=?", (email,)).fetchone():
        conn.close()
        raise HTTPException(401, "User no longer exists")
    hotspot = conn.execute("SELECT vin FROM hotspots WHERE id=?", (hotspot_id,)).fetchone()
    if not hotspot:
        conn.close()
        raise HTTPException(404, "Hotspot not found")
    owner_email = os.getenv("LOTVUE_OWNER_EMAIL", "").strip().lower()
    if not (owner_email and email == owner_email):
        vehicle = conn.execute(
            "SELECT 1 FROM vehicles WHERE vin=? AND status='active' AND LOWER(user_email)=?",
            (str(hotspot["vin"]).upper(), email),
        ).fetchone()
        if not vehicle:
            conn.close()
            raise HTTPException(404, "Hotspot not found")
    cur = conn.execute(
        "UPDATE hotspots SET condition_type=?, severity=?, note=? WHERE id=?",
        (body.condition_type or "", body.severity or "", (body.note or "")[:500], hotspot_id),
    )
    conn.commit()
    found = cur.rowcount
    conn.close()
    if not found:
        raise HTTPException(404, "Hotspot not found")
    return {"status": "ok", "hotspot_id": hotspot_id}


@router.get("/api/vehicle/{vin}/condition-report.pdf")
def condition_report_pdf(vin: str):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

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
from jose import JWTError, jwt

_SECRET_FILE = DATA_DIR / ".jwt_secret"


def _jwt_secret() -> str:
    env = os.getenv("LOTVUE_JWT_SECRET", "")
    if env:
        if len(env.encode("utf-8")) < 32:
            raise RuntimeError("LOTVUE_JWT_SECRET must contain at least 32 bytes")
        return env
    try:
        existing = _SECRET_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    if existing:
        if len(existing.encode("utf-8")) < 32:
            raise RuntimeError(f"JWT secret file is too short: {_SECRET_FILE}")
        return existing

    import secrets
    generated = secrets.token_hex(32)
    try:
        # Exclusive create prevents multiple workers from issuing tokens with
        # different keys during simultaneous first startup; 0600 limits access.
        fd = os.open(str(_SECRET_FILE), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = _SECRET_FILE.read_text(encoding="utf-8").strip()
        if len(existing.encode("utf-8")) < 32:
            raise RuntimeError(f"JWT secret file is too short: {_SECRET_FILE}")
        return existing
    with os.fdopen(fd, "w", encoding="utf-8") as secret_file:
        secret_file.write(generated)
        secret_file.flush()
        os.fsync(secret_file.fileno())
    return generated


JWT_ALG = "HS256"
ACCESS_TTL_H = 24
REFRESH_TTL_D = 30


class TokenRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=1024)


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
def auth_token(body: TokenRequest, request: Request, response: Response):
    from auth_rate import check_auth_rate
    check_auth_rate(request, "login", 8)
    email = body.email.lower().strip()
    conn = _db()
    user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user or not verify_password(body.password, user["password_hash"]):
        conn.close()
        raise HTTPException(401, "Invalid email or password")
    if needs_rehash(user["password_hash"]):
        conn.execute("UPDATE users SET password_hash=? WHERE email=?",
                     (hash_password(body.password), email))
        conn.commit()
    conn.close()
    tokens = _make_tokens(user["email"])
    # Browser sessions use an HttpOnly cookie, marked Secure on HTTPS.
    response.set_cookie("lotvue_token", tokens["access_token"], httponly=True,
                        secure=secure_cookie_for_request(request), samesite="lax",
                        path="/", max_age=ACCESS_TTL_H * 3600)
    return tokens


@router.post("/api/auth/logout")
def auth_logout(response: Response):
    response.delete_cookie("lotvue_token")
    return {"status": "signed_out"}


@router.post("/api/auth/refresh")
def auth_refresh(body: RefreshRequest):
    try:
        payload = jwt.decode(body.refresh_token, _jwt_secret(), algorithms=[JWT_ALG])
    except JWTError:
        raise HTTPException(401, "Invalid or expired refresh token")
    if not isinstance(payload, dict) or payload.get("type") != "refresh":
        raise HTTPException(401, "Not a refresh token")
    subject = str(payload.get("sub", "")).strip().lower()
    if not subject:
        raise HTTPException(401, "Invalid or expired refresh token")
    # A refresh token must not outlive its account: without this check a removed
    # user can mint fresh access tokens for the whole refresh lifetime.
    conn = _db()
    try:
        row = conn.execute(
            "SELECT email FROM users WHERE LOWER(email)=?", (subject,)
        ).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        conn.close()
    if not row:
        raise HTTPException(401, "User no longer exists")
    return _make_tokens(row["email"])


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
def auth_me(authorization: str = Header(default="")):
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
