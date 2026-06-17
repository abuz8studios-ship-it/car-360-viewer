r"""
LotVue Pro — AI-Powered Dealer Platform
Cinematic AI photo studio + 360 viewer + video walkaround + market pricing
Replaces: DealerCenter + Spyne + vAuto + video service + manual posting
Features: Multi-tenant, auth, credits, Stripe, AI photo studio, 360 viewer

Requirements (run once):
  E:\\Miniconda3\\envs\\lotvue\\python.exe -m pip install fastapi uvicorn python-multipart Pillow aiosqlite opencv-python rembg pillow-heif stripe
"""

import os
import sys
import re
import io
import json
import shutil
import sqlite3
import subprocess
import threading
import numpy as np
from pathlib import Path
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image

# ── Load app/.env into process env (no python-dotenv dependency) ─────────────
# Must run before FEATURES_ENABLED / STRIPE_* are read below.
_ENV_FILE = Path(__file__).resolve().parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# --- Try HEIC support, gracefully degrade ---
HEIC_SUPPORTED = False
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIC_SUPPORTED = True
except ImportError:
    pass

# --- OpenCV for frame interpolation ---
import cv2

# --- GPU Detection ---
GPU_INFO = {"has_gpu": False, "gpus": [], "cuda": False}

def detect_gpu():
    try:
        import torch
        GPU_INFO["cuda"] = torch.cuda.is_available()
        if torch.cuda.is_available():
            for i in range(torch.cuda.device_count()):
                name = torch.cuda.get_device_name(i)
                mem = torch.cuda.get_device_properties(i).total_memory / (1024**3)
                GPU_INFO["gpus"].append({"id": i, "name": name, "memory_gb": round(mem, 1)})
            GPU_INFO["has_gpu"] = True
    except ImportError:
        pass
    try:
        import GPUtil
        gpus = GPUtil.getGPUs()
        if gpus:
            GPU_INFO["has_gpu"] = True
            for g in gpus:
                GPU_INFO["gpus"].append({"id": g.id, "name": g.name, "memory_gb": round(g.memoryTotal/1024, 1)})
    except ImportError:
        pass
    return GPU_INFO

detect_gpu()
print(f"[LotVue Pro] GPU Detection: {GPU_INFO}")

# =============================================================================
# DUAL GPU CONFIGURATION (Phase 1)
# =============================================================================
# GPU 0 (cuda:0) → Background removal / rembg / AI processing
# GPU 1 (cuda:1) → Rendering / video generation / interpolation
GPU_0_DEVICE = "cuda:0" if GPU_INFO.get("has_gpu") else "cpu"
GPU_1_DEVICE = "cuda:1" if GPU_INFO.get("has_gpu") and len(GPU_INFO.get("gpus", [])) > 1 else GPU_0_DEVICE

# Set torch to use GPU 1 for rendering
try:
    import torch
    if torch.cuda.device_count() > 1:
        torch.cuda.set_device(1)
        print(f"[LotVue Pro] GPU 1 assigned for rendering: {torch.cuda.get_device_name(1)}")
except Exception as e:
    print(f"[LotVue Pro] GPU assignment: {e}")

print(f"[LotVue Pro] GPU Config: AI/Background={GPU_0_DEVICE}, Rendering={GPU_1_DEVICE}")

# =============================================================================
# FEATURES ENABLED
# =============================================================================
FEATURES_ENABLED = {
    "auth": os.environ.get("LOTVUE_FEATURE_AUTH", "0") == "1",
    "payments": os.environ.get("LOTVUE_FEATURE_PAYMENTS", "0") == "1",
    "watermark": os.environ.get("LOTVUE_FEATURE_WATERMARK", "0") == "1",
}

# --- Background Removal (rembg) ---
REMBG_AVAILABLE = False
_REMBG_SESSION = None

def get_rembg_session():
    """Get rembg session with GPU 0 assignment."""
    global _REMBG_SESSION
    if _REMBG_SESSION is None:
        try:
            from rembg import remove
            # Force rembg to use GPU 0
            _REMBG_SESSION = remove
        except ImportError:
            pass
    return _REMBG_SESSION

try:
    from rembg import remove as remove_bg
    REMBG_AVAILABLE = True
except ImportError:
    pass

# Showroom storage
BASE_DIR = Path(__file__).resolve().parent

# Showroom storage
SHOWROOM_DIR = BASE_DIR / "storage" / "_showrooms"

# =============================================================================
# CONFIG
# =============================================================================
def _resolve_tool(env_key, name):
    """Find ffmpeg/ffprobe: env override -> bundled app/bin -> dev workstation -> PATH.
    Lets the standalone bundle ship its own ffmpeg (app/bin) and still work wherever
    it's installed, while the dev workstation keeps using E:\\ABU\\AI\\ffmpeg."""
    p = os.environ.get(env_key, "")
    if p and os.path.exists(p):
        return p
    for cand in (BASE_DIR / "bin" / name,
                 BASE_DIR.parents[1] / "AI" / "ffmpeg" / name):
        if cand.exists():
            return str(cand)
    return p or name  # bare name -> resolved via PATH if present

FFMPEG = _resolve_tool("FFMPEG_PATH", "ffmpeg.exe")
FFPROBE = _resolve_tool("FFPROBE_PATH", "ffprobe.exe")
STORAGE_DIR = BASE_DIR / "storage"
RAW_DIR = STORAGE_DIR / "raw"
FRAMES_DIR = STORAGE_DIR / "frames"
PREVIEW_DIR = STORAGE_DIR / "preview"
CLEAN_DIR = STORAGE_DIR / "clean"
THUMBNAILS_DIR = STORAGE_DIR / "thumbnails"
DB_PATH = BASE_DIR / "vehicles.db"
NUM_EXTERIOR_FRAMES = 36  # 10° per frame = smooth spin
MAX_VIDEO_MB = 500
MAX_PHOTO_MB = 30

# =============================================================================
# PRICING PLANS (Phase 2)
# =============================================================================
PRICING_PLANS = {
    "free": {
        "label": "Free",
        "credits": 10,
        "price": 0,
        "features": ["10 free credits", "Watermarked output", "Basic viewer"]
    },
    "pay_per_use": {
        "label": "Pay Per Use",
        "credits": 1,
        "price": 2.99,
        "features": ["Single render", "No watermark", "HD output"]
    },
    "pro_bundle": {
        "label": "Pro Bundle",
        "credits": 10,
        "price": 19.99,
        "per_credit": 1.99,
        "features": ["10 renders", "No watermark", "HD output", "Priority processing"]
    },
    "dealership": {
        "label": "Dealership Monthly",
        "credits": -1,  # unlimited
        "price": 99.99,
        "features": ["Unlimited renders", "White-label branding", "API access", "Multi-location"]
    },
    "enterprise": {
        "label": "Enterprise",
        "credits": -1,
        "price": 299.99,
        "features": ["Everything in Dealership", "Custom backgrounds", "Dedicated support", "SLA"]
    }
}

# =============================================================================
# PHASE 5: TENANT SYSTEM - BACKGROUND PRESETS
# =============================================================================
BACKGROUND_PRESETS = {
    "showroom": {
        "name": "Showroom",
        "description": "Clean white studio floor, soft gradient",
        "colors": ["#ffffff", "#f5f5f5", "#e8e8e8"]
    },
    "luxury": {
        "name": "Luxury",
        "description": "Dark gray with rim lighting",
        "colors": ["#1a1a1a", "#2d2d2d", "#0a0a0a"]
    },
    "outdoor": {
        "name": "Outdoor",
        "description": "Sky gradient with ground shadow",
        "colors": ["#87CEEB", "#f0f8ff", "#e0e0e0"]
    },
    "fashion_runway": {
        "name": "Fashion Runway",
        "description": "White runway with subtle gradient",
        "colors": ["#ffffff", "#fafafa", "#f0f0f0"]
    },
    "fashion_studio": {
        "name": "Fashion Studio",
        "description": "Solid neutral gray",
        "colors": ["#888888", "#999999", "#777777"]
    },
    "garage": {
        "name": "Modern Garage",
        "description": "Concrete floor with overhead lighting feel",
        "colors": ["#3a3a3a", "#4a4a4a", "#2a2a2a"]
    }
}

STORAGE_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)
FRAMES_DIR.mkdir(parents=True, exist_ok=True)
PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
CLEAN_DIR.mkdir(parents=True, exist_ok=True)
THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)
SHOWROOM_DIR.mkdir(parents=True, exist_ok=True)

# =============================================================================
# BACKGROUND REMOVAL + SHOWROOM COMPOSITING
# =============================================================================

def auto_enhance(img: Image.Image) -> Image.Image:
    """Auto-enhance like Google Photos/iPhone - better contrast, color, sharpness."""
    from PIL import ImageEnhance
    
    # Slightly boost contrast
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(1.1)
    
    # Slight color vibrancy boost
    enhancer = ImageEnhance.Color(img)
    img = enhancer.enhance(1.15)
    
    # Sharpen a bit
    enhancer = ImageEnhance.Sharpness(img)
    img = enhancer.enhance(1.2)
    
    return img


def process_exterior_photo(input_bytes: bytes, output_path: Path, showroom_path: Path | None = None, keep_original: bool = False):
    """Save exterior photo - optionally keep original environment (like Impel)."""
    # Always keep original - like Impel! No background removal, no changes
    # Just auto-enhance the original photo
    img = Image.open(io.BytesIO(input_bytes)).convert("RGB")
    img = auto_enhance(img)
    img = img.resize((1920, 1080), Image.LANCZOS)
    img.save(output_path, "JPEG", quality=92)


def process_interior_photo(input_bytes: bytes, output_path: Path):
    """Save interior photo - keep original, just enhance."""
    img = Image.open(io.BytesIO(input_bytes)).convert("RGB")
    img = auto_enhance(img)
    img.save(output_path, "JPEG", quality=92)


WATERMARK_PREVIEW_TEXT = "Preview • LotVue Pro"

def apply_diagonal_watermark(img: Image.Image, text: str = "LOTVUE PRO PREVIEW", opacity: int = 77) -> Image.Image:
    """Apply diagonal repeating watermark across the entire image (Phase 1 upgrade)."""
    if not FEATURES_ENABLED.get("watermark", False):
        return img
    try:
        from PIL import ImageDraw
        overlay = img.copy().convert("RGBA")
        txt_layer = Image.new("RGBA", overlay.size, (255, 255, 255, 0))
        draw = ImageDraw.Draw(txt_layer)
        
        font_size = max(20, img.width // 50)
        try:
            from PIL import ImageFont
            font = ImageFont.truetype("arial.ttf", font_size)
        except Exception:
            font = ImageFont.load_default()
        
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        
        spacing_x = int(text_w * 2.5)
        spacing_y = int(text_h * 4)
        angle = -30
        
        for y in range(-int(text_h), img.height + int(text_h), spacing_y):
            for x in range(-int(text_w), img.width + int(text_w), spacing_x):
                draw.text((x, y), text, font=font, fill=(255, 255, 255, opacity))
        
        small_text = "lotvuepro"
        small_size = max(12, img.width // 120)
        try:
            small_font = ImageFont.truetype("arial.ttf", small_size)
        except:
            small_font = ImageFont.load_default()
        
        draw.text((img.width - 100, img.height - 30), small_text, font=small_font, fill=(255, 255, 255, opacity))
        
        composite = Image.alpha_composite(overlay, txt_layer)
        return composite.convert("RGB")
    except Exception as e:
        print(f"Diagonal watermark error: {e}")
        return img

def apply_watermark(img: Image.Image, text: str = WATERMARK_PREVIEW_TEXT, opacity: int = 60) -> Image.Image:
    """Apply a watermark text to an image."""
    if not FEATURES_ENABLED.get("watermark", False):
        return img
    try:
        from PIL import ImageDraw
        overlay = img.copy().convert("RGBA")
        txt_layer = Image.new("RGBA", overlay.size, (255, 255, 255, 0))
        draw = ImageDraw.Draw(txt_layer)
        
        font_size = max(16, img.width // 40)
        try:
            from PIL import ImageFont
            font = ImageFont.truetype("arial.ttf", font_size)
        except Exception:
            font = ImageFont.load_default()
        
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        
        margin = 20
        x = img.width - text_w - margin
        y = img.height - text_h - margin
        
        draw.text((x, y), text, font=font, fill=(255, 255, 255, opacity))
        
        composite = Image.alpha_composite(overlay, txt_layer)
        return composite.convert("RGB")
    except Exception as e:
        print(f"Watermark error: {e}")
        return img


def get_dealer_showroom(dealer_name: str = "default") -> Path | None:
    """Get showroom background for a dealer."""
    showroom_file = SHOWROOM_DIR / f"{dealer_name}.jpg"
    if showroom_file.exists():
        return showroom_file
    return None

# =============================================================================
# APP
# =============================================================================
app = FastAPI(title="LotVue Pro — AI-Powered Dealer Platform")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# =============================================================================
# DATABASE
# =============================================================================
def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vehicles (
            vin TEXT PRIMARY KEY,
            year TEXT DEFAULT '',
            make TEXT DEFAULT '',
            model TEXT DEFAULT '',
            color TEXT DEFAULT '',
            price TEXT DEFAULT '',
            mileage TEXT DEFAULT '',
            exterior_frames INTEGER DEFAULT 0,
            interior_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            watermark_mode TEXT DEFAULT 'preview',
            user_email TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hotspots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vin TEXT NOT NULL,
            photo_filename TEXT NOT NULL,
            tag TEXT NOT NULL,
            hotspot_x REAL DEFAULT 0.5,
            hotspot_y REAL DEFAULT 0.5
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dealers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            display_name TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            email TEXT DEFAULT '',
            address TEXT DEFAULT '',
            website TEXT DEFAULT '',
            logo_url TEXT DEFAULT '',
            brand_color TEXT DEFAULT '#58a6ff',
            plan TEXT DEFAULT 'free',
            created_at TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            name TEXT DEFAULT '',
            password_hash TEXT DEFAULT '',
            plan TEXT DEFAULT 'free',
            credits INTEGER DEFAULT 5,
            stripe_customer_id TEXT DEFAULT '',
            created_at TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL DEFAULT 0,
            currency TEXT DEFAULT 'usd',
            plan TEXT DEFAULT 'single',
            credits_purchased INTEGER DEFAULT 0,
            stripe_payment_id TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT '',
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS credit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount INTEGER DEFAULT 0,
            action TEXT DEFAULT '',
            vin TEXT DEFAULT '',
            created_at TEXT DEFAULT '',
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vehicle_id INTEGER,
            dealer_id INTEGER DEFAULT 1,
            vin TEXT,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT,
            message TEXT,
            wants_video INTEGER DEFAULT 0,
            wants_test_drive INTEGER DEFAULT 0,
            source TEXT DEFAULT 'viewer',
            status TEXT DEFAULT 'new',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gallery_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vehicle_id INTEGER,
            vin TEXT NOT NULL,
            file_path TEXT NOT NULL,
            thumbnail_path TEXT,
            category TEXT DEFAULT 'exterior',
            caption TEXT,
            sort_order INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS interior_panoramas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vehicle_id INTEGER,
            vin TEXT NOT NULL,
            file_path TEXT NOT NULL,
            scene_name TEXT DEFAULT 'Front Seat',
            is_equirectangular INTEGER DEFAULT 1,
            default_yaw REAL DEFAULT 0,
            default_pitch REAL DEFAULT 0,
            default_hfov REAL DEFAULT 120,
            sort_order INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    # Phase 2A + 2B: Add new columns to vehicles table if not present
    for col_def in [
        "job_id TEXT DEFAULT ''",
        "video_url TEXT DEFAULT ''",
        "video_type TEXT DEFAULT ''",
        "show_interior INTEGER DEFAULT 0",
        "show_gallery INTEGER DEFAULT 1",
        "show_video INTEGER DEFAULT 0",
        "dealer_logo_url TEXT DEFAULT ''",
        # Phase 2B columns
        "turntable_style TEXT DEFAULT 'studio_shadow'",
        "background_id TEXT DEFAULT ''",
        "trim_level TEXT DEFAULT ''",
        "engine TEXT DEFAULT ''",
        "transmission TEXT DEFAULT ''",
        "drivetrain TEXT DEFAULT ''",
        "fuel_type TEXT DEFAULT ''",
        "body_style TEXT DEFAULT ''",
        "stock_number TEXT DEFAULT ''",
        "description TEXT DEFAULT ''",
        "workflow_status TEXT DEFAULT 'intake'",
    ]:
        try:
            conn.execute(f"ALTER TABLE vehicles ADD COLUMN {col_def}")
        except Exception:
            pass

    # Phase 2B: Backgrounds library table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS backgrounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dealer_id INTEGER DEFAULT 0,
            name TEXT NOT NULL,
            slug TEXT UNIQUE NOT NULL,
            url TEXT NOT NULL,
            thumbnail_url TEXT DEFAULT '',
            category TEXT DEFAULT 'studio',
            is_active INTEGER DEFAULT 1,
            is_system INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Seed system backgrounds if empty
    bg_count = conn.execute("SELECT COUNT(*) FROM backgrounds WHERE is_system=1").fetchone()[0]
    if bg_count == 0:
        system_bgs = [
            ('Studio Shadow', 'studio_shadow', '/static/backgrounds/studio_shadow.jpg', '/static/backgrounds/thumbs/studio_shadow.jpg', 'studio'),
            ('Studio White', 'studio_white', '/static/backgrounds/studio_white.jpg', '/static/backgrounds/thumbs/studio_white.jpg', 'studio'),
            ('Studio Dark', 'studio_dark', '/static/backgrounds/studio_dark.jpg', '/static/backgrounds/thumbs/studio_dark.jpg', 'studio'),
            ('Showroom Floor', 'showroom_floor', '/static/backgrounds/showroom_floor.jpg', '/static/backgrounds/thumbs/showroom_floor.jpg', 'showroom'),
            ('Outdoor Sunset', 'outdoor_sunset', '/static/backgrounds/outdoor_sunset.jpg', '/static/backgrounds/thumbs/outdoor_sunset.jpg', 'outdoor'),
            ('Outdoor Overcast', 'outdoor_overcast', '/static/backgrounds/outdoor_overcast.jpg', '/static/backgrounds/thumbs/outdoor_overcast.jpg', 'outdoor'),
            ('Concrete Floor', 'concrete_floor', '/static/backgrounds/concrete_floor.jpg', '/static/backgrounds/thumbs/concrete_floor.jpg', 'studio'),
            ('Glossy Black', 'glossy_black', '/static/backgrounds/glossy_black.jpg', '/static/backgrounds/thumbs/glossy_black.jpg', 'studio'),
            ('None (Transparent)', 'none', '', '', 'studio'),
        ]
        for name, slug, url, thumb, cat in system_bgs:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO backgrounds (name, slug, url, thumbnail_url, category, is_system) VALUES (?,?,?,?,?,1)",
                    (name, slug, url, thumb, cat)
                )
            except Exception:
                pass

    conn.commit()
    conn.close()

init_db()

def db_upsert_vehicle(vin, year="", make="", model="", ext_frames=0, int_count=0, watermark_mode="preview", user_email="",
                       trim_level="", engine="", transmission="", drivetrain="", fuel_type="", body_style="",
                       stock_number="", turntable_style="studio_shadow", job_id=""):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        INSERT INTO vehicles (vin, year, make, model, exterior_frames, interior_count, created_at, watermark_mode, user_email,
                              trim_level, engine, transmission, drivetrain, fuel_type, body_style, stock_number, turntable_style, job_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(vin) DO UPDATE SET
            year=excluded.year, make=excluded.make, model=excluded.model,
            exterior_frames=excluded.exterior_frames, interior_count=excluded.interior_count,
            watermark_mode=excluded.watermark_mode, user_email=excluded.user_email,
            trim_level=CASE WHEN excluded.trim_level != '' THEN excluded.trim_level ELSE trim_level END,
            engine=CASE WHEN excluded.engine != '' THEN excluded.engine ELSE engine END,
            transmission=CASE WHEN excluded.transmission != '' THEN excluded.transmission ELSE transmission END,
            drivetrain=CASE WHEN excluded.drivetrain != '' THEN excluded.drivetrain ELSE drivetrain END,
            fuel_type=CASE WHEN excluded.fuel_type != '' THEN excluded.fuel_type ELSE fuel_type END,
            body_style=CASE WHEN excluded.body_style != '' THEN excluded.body_style ELSE body_style END,
            stock_number=CASE WHEN excluded.stock_number != '' THEN excluded.stock_number ELSE stock_number END,
            job_id=CASE WHEN excluded.job_id != '' THEN excluded.job_id ELSE job_id END
    """, (vin, year, make, model, ext_frames, int_count, datetime.now().isoformat(), watermark_mode, user_email,
          trim_level, engine, transmission, drivetrain, fuel_type, body_style, stock_number, turntable_style, job_id))
    conn.commit()
    conn.close()

def db_get_vehicles():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM vehicles WHERE status='active' ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def db_get_vehicles_by_dealer(dealer_name: str) -> list[dict]:
    """Get vehicles for a specific dealer."""
    dealer = db_get_dealer(dealer_name)
    if not dealer:
        return []
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM vehicles WHERE user_email=? AND status='active' ORDER BY created_at DESC", 
                        (dealer.get("email", ""),)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def db_get_vehicles_by_email(user_email: str) -> list[dict]:
    """Get vehicles for a specific user email."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM vehicles WHERE user_email=? AND status='active' ORDER BY created_at DESC", 
                        (user_email,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def db_get_vehicle(vin):
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM vehicles WHERE vin=?", (vin,)).fetchone()
    conn.close()
    return dict(row) if row else None

def db_get_vehicle_by_job_id(job_id: str):
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM vehicles WHERE job_id=?", (job_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def db_delete_vehicle(vin):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE vehicles SET status='deleted' WHERE vin=?", (vin,))
    conn.commit()
    conn.close()

def db_update_counts(vin):
    vehicle_dir = STORAGE_DIR / vin
    ext = len(list((vehicle_dir / "exterior").glob("*.jpg"))) if (vehicle_dir / "exterior").exists() else 0
    intr = len(list((vehicle_dir / "interior").glob("*.jpg"))) if (vehicle_dir / "interior").exists() else 0
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE vehicles SET exterior_frames=?, interior_count=? WHERE vin=?", (ext, intr, vin))
    conn.commit()
    conn.close()

def db_get_dealer(name: str = "default"):
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM dealers WHERE name=?", (name,)).fetchone()
    conn.close()
    return dict(row) if row else None

def db_upsert_dealer(name: str, display_name="", phone="", email="", address="", website="", logo_url="", brand_color="#58a6ff"):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        INSERT INTO dealers (name, display_name, phone, email, address, website, logo_url, brand_color, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            display_name=excluded.display_name, phone=excluded.phone, email=excluded.email,
            address=excluded.address, website=excluded.website, logo_url=excluded.logo_url,
            brand_color=excluded.brand_color
    """, (name, display_name, phone, email, address, website, logo_url, brand_color, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def db_list_dealers():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM dealers ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def db_get_user(email: str):
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    conn.close()
    return dict(row) if row else None

def db_create_user(email: str, name: str = "", password_hash: str = ""):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        INSERT OR IGNORE INTO users (email, name, password_hash, credits, plan, created_at)
        VALUES (?, ?, ?, 5, 'free', ?)
    """, (email, name, password_hash, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def db_update_user_credits(email: str, credits: int):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE users SET credits=? WHERE email=?", (credits, email))
    conn.commit()
    conn.close()

def db_add_credits(email: str, amount: int, action: str, vin: str = ""):
    user = db_get_user(email)
    if not user:
        db_create_user(email)
        user = db_get_user(email)
    new_credits = user.get("credits", 0) + amount
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE users SET credits=? WHERE email=?", (new_credits, email))
    conn.execute("""
        INSERT INTO credit_log (user_id, amount, action, vin, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (user["id"], amount, action, vin, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return new_credits

def db_get_user_credits(email: str) -> int:
    user = db_get_user(email)
    if not user:
        return 5
    return user.get("credits", 5)


def generate_clean_frames(vin: str) -> bool:
    """Generate clean (no watermark) versions of all frames for a vehicle."""
    vehicle_dir = STORAGE_DIR / vin
    exterior_dir = vehicle_dir / "exterior"
    
    if not exterior_dir.exists():
        return False
    
    try:
        # Get original frames (from raw if exists, else use existing)
        raw_dir = RAW_DIR / vin
        if raw_dir.exists():
            source_dir = raw_dir
        else:
            source_dir = exterior_dir
        
        frames = sorted(source_dir.glob("frame_*.jpg"))
        if not frames:
            return False
        
        # Process each frame - no watermark, higher quality
        for frame_path in frames:
            try:
                img = Image.open(frame_path).convert("RGB")
                img = auto_enhance(img)
                img = img.resize((1920, 1080), Image.LANCZOS)
                
                # Save as clean (no watermark)
                clean_path = exterior_dir / frame_path.name
                img.save(clean_path, "JPEG", quality=98)
            except Exception as e:
                print(f"Clean frame error {frame_path}: {e}")
        
        # Also regenerate spin frames
        spin_dir = vehicle_dir / "spin"
        if spin_dir.exists():
            interpolate_frames(exterior_dir, spin_dir, 36)
        
        return True
    except Exception as e:
        print(f"Generate clean frames error: {e}")
        return False

# =============================================================================
# PHASE 2: CREDIT CHECKING SYSTEM
# =============================================================================
def check_user_can_render(email: str) -> dict:
    """
    Check if user can render a vehicle.
    Returns: {"allowed": bool, "reason": str, "credits": int, "is_subscribed": bool, "plan": str}
    """
    if not FEATURES_ENABLED.get("payments", False):
        return {"allowed": True, "reason": "Payments disabled", "credits": -1, "is_subscribed": False, "plan": "free"}
    if not email:
        return {"allowed": True, "reason": "No user - free mode", "credits": 0, "is_subscribed": False, "plan": "free"}
    
    user = db_get_user(email)
    if not user:
        return {"allowed": True, "reason": "New user - free credits", "credits": 10, "is_subscribed": False, "plan": "free"}
    
    credits = user.get("credits", 0)
    is_subscribed = user.get("is_subscribed", False)
    plan = user.get("plan", "free")
    plan_type = user.get("plan_type", "free")
    
    # Unlimited plan (dealership/enterprise)
    if plan_type in ["dealership", "enterprise"]:
        return {"allowed": True, "reason": "Unlimited plan", "credits": -1, "is_subscribed": True, "plan": plan_type}
    
    # Subscribed users
    if is_subscribed and plan_type in ["pro_monthly", "dealership", "enterprise"]:
        return {"allowed": True, "reason": "Active subscription", "credits": -1, "is_subscribed": True, "plan": plan_type}
    
    # Has credits
    if credits > 0:
        return {"allowed": True, "reason": f"{credits} credits available", "credits": credits, "is_subscribed": False, "plan": plan_type}
    
    # No credits
    return {"allowed": False, "reason": "No credits - payment required", "credits": 0, "is_subscribed": False, "plan": plan_type}


def deduct_render_credit(email: str, vin: str) -> bool:
    """Deduct 1 credit for a render. Returns True if successful."""
    if not email:
        return True  # No user = free
    
    user = db_get_user(email)
    if not user:
        return True  # New user gets free render
    
    plan_type = user.get("plan_type", "free")
    
    # Unlimited plans don't deduct
    if plan_type in ["dealership", "enterprise"]:
        return True
    
    if user.get("is_subscribed", False):
        return True  # Subscribed users don't deduct
    
    credits = user.get("credits", 0)
    if credits <= 0:
        return False
    
    new_credits = credits - 1
    db_update_user_credits(email, new_credits)
    db_add_credits(email, -1, "render", vin)
    return True


def db_record_payment(user_id: int, amount: float, plan: str, credits: int, stripe_id: str, status: str):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        INSERT INTO payments (user_id, amount, plan, credits_purchased, stripe_payment_id, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user_id, amount, plan, credits, stripe_id, status, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def db_update_dealer_plan(dealer_name: str, plan: str):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE dealers SET plan=? WHERE name=?", (plan, dealer_name))
    conn.commit()
    conn.close()

# =============================================================================
# PHASE 5: TENANT DATABASE FUNCTIONS
# =============================================================================
def db_create_dealer(name: str, email: str = "", display_name: str = "", background_preset: str = "showroom") -> int:
    """Create a new dealer/tenant."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        INSERT OR IGNORE INTO dealers (name, display_name, email, background_preset, plan, created_at)
        VALUES (?, ?, ?, 'free', ?)
    """, (name, display_name or name, email, datetime.now().isoformat()))
    conn.commit()
    row = conn.execute("SELECT id FROM dealers WHERE name=?", (name,)).fetchone()
    conn.close()
    return row[0] if row else None

def db_get_dealer(name: str) -> dict | None:
    """Get dealer by name."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM dealers WHERE name=?", (name,)).fetchone()
    conn.close()
    return dict(row) if row else None

def db_get_dealer_by_email(email: str) -> dict | None:
    """Get dealer by user email."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM dealers WHERE user_email=?", (email,)).fetchone()
    conn.close()
    return dict(row) if row else None

def db_update_dealer(dealer_name: str, **kwargs):
    """Update dealer fields."""
    conn = sqlite3.connect(str(DB_PATH))
    for key, value in kwargs.items():
        if key in ["display_name", "phone", "email", "address", "website", "logo_url", "brand_color", "background_preset", "plan"]:
            conn.execute(f"UPDATE dealers SET {key}=? WHERE name=?", (value, dealer_name))
    conn.commit()
    conn.close()

def db_list_dealers() -> list[dict]:
    """List all dealers."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM dealers ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

PLANS = {
    "free": {"credits": 5, "price": 0, "label": "Free"},
    "single": {"credits": 1, "price": 2.99, "label": "Single ($2.99)"},
    "starter": {"credits": 10, "price": 19.99, "label": "Starter (10 for $19.99)"},
    "dealership": {"credits": 999999, "price": 75.00, "label": "Dealership Monthly ($75/mo)"},
}

def check_credits(user_email: str) -> tuple[bool, int]:
    user = db_get_user(user_email)
    if not user:
        return True, 5
    credits = user.get("credits", 5)
    if user.get("plan") == "dealership":
        return True, 999999
    return credits > 0, credits

def use_credit(user_email: str, vin: str) -> bool:
    user = db_get_user(user_email)
    if not user:
        db_create_user(user_email)
        return True
    if user.get("plan") == "dealership":
        return True
    credits = user.get("credits", 5)
    if credits <= 0:
        return False
    db_add_credits(user_email, -1, "vehicle_upload", vin)
    return True


# =============================================================================
# API — WATERMARK
# =============================================================================
@app.post("/api/watermark/{vin}")
async def set_watermark(vin: str, mode: str = Form("clean")):
    if not FEATURES_ENABLED.get("watermark", False):
        return {"status": "ok", "mode": "clean", "note": "watermark feature disabled"}
    vin = vin.upper()
    if mode not in ("preview", "clean"):
        raise HTTPException(status_code=400, detail="Mode must be 'preview' or 'clean'")
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE vehicles SET watermark_mode=? WHERE vin=?", (mode, vin))
    conn.commit()
    conn.close()
    
    if mode == "clean":
        vehicle_dir = STORAGE_DIR / vin
        spin_dir = vehicle_dir / "spin"
        if spin_dir.exists():
            for f in spin_dir.glob("spin_*.jpg"):
                try:
                    img = Image.open(f).convert("RGB")
                    img.save(f, "JPEG", quality=92)
                except:
                    pass
        ext_dir = vehicle_dir / "exterior"
        if ext_dir.exists():
            for f in ext_dir.glob("frame_*.jpg"):
                try:
                    img = Image.open(f).convert("RGB")
                    img.save(f, "JPEG", quality=92)
                except:
                    pass
    
    return {"status": "ok", "mode": mode}


@app.get("/api/watermark/{vin}")
async def get_watermark(vin: str):
    if not FEATURES_ENABLED.get("watermark", False):
        return {"watermark_mode": "clean"}
    vin = vin.upper()
    vehicle = db_get_vehicle(vin)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    return {"watermark_mode": vehicle.get("watermark_mode", "preview")}


@app.get("/api/plans")
async def get_plans():
    return {"plans": PLANS}


# =============================================================================
# API — STRIPE PAYMENTS
# =============================================================================
STRIPE_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

@app.get("/api/stripe/config")
async def stripe_config():
    return {"publishable_key": STRIPE_PUBLISHABLE}


@app.post("/api/create-checkout-session")
async def create_checkout_session(
    plan_id: str = Form(...),
    user_email: str = Form("")
):
    if not FEATURES_ENABLED.get("payments", False):
        return {"status": "disabled", "error": "Payments feature is disabled"}
    if not STRIPE_KEY:
        return {"status": "error", "error": "Stripe not configured"}
    
    plan = PLANS.get(plan_id)
    if not plan:
        return {"status": "error", "error": "Invalid plan"}
    
    try:
        import stripe
        stripe.api_key = STRIPE_KEY
        
        price_data = {
            "single": "price_single_xxxxx",
            "starter": "price_starter_xxxxx",
            "dealership": "price_dealership_xxxxx",
        }
        
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": f"LotVue Pro - {plan['label']}"},
                    "unit_amount": int(plan["price"] * 100),
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=f"{os.environ.get('LOTVUE_PUBLIC_URL', 'http://127.0.0.1:8001').rstrip('/')}/?payment=success&plan={plan_id}",
            cancel_url=f"{os.environ.get('LOTVUE_PUBLIC_URL', 'http://127.0.0.1:8001').rstrip('/')}/?payment=cancelled",
            customer_email=user_email if user_email else None,
            metadata={"plan_id": plan_id, "credits": str(plan["credits"]), "user_email": user_email}
        )
        return {"status": "ok", "session_url": session.url, "session_id": session.id}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    if not FEATURES_ENABLED.get("payments", False):
        return JSONResponse({"status": "disabled", "error": "Payments feature is disabled"}, status_code=503)
    if not STRIPE_KEY:
        return JSONResponse({"error": "Stripe not configured"}, status_code=500)
    
    import stripe
    stripe.api_key = STRIPE_KEY
    
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    
    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
        else:
            event = json.loads(payload)
        
        if event.get("type") == "checkout.session.completed":
            session = event["data"]["object"]
            payment_type = session.get("metadata", {}).get("type", "")
            plan_id = session.get("metadata", {}).get("plan_id", "single")
            credits = int(session.get("metadata", {}).get("credits", "1"))
            user_email = session.get("metadata", {}).get("user_email", session.get("customer_email", ""))
            
            if payment_type == "unlock_vehicle":
                # Vehicle unlock payment
                vin = session.get("metadata", {}).get("vin", "")
                if vin:
                    conn = sqlite3.connect(str(DB_PATH))
                    conn.execute("UPDATE vehicles SET is_paid=1, watermark_mode='clean', payment_id=? WHERE vin=?", 
                                  (session.get("id", ""), vin))
                    conn.commit()
                    conn.close()
                    
                    # Generate clean frames (no watermark)
                    import threading
                    threading.Thread(target=generate_clean_frames, args=(vin,)).start()
                    print(f"[LotVue Pro] Vehicle unlocked: {vin}")
            
            elif payment_type == "subscription":
                # Subscription payment - activate plan
                plan_type = session.get("metadata", {}).get("plan_type", "dealership")
                if user_email:
                    user = db_get_user(user_email)
                    if user:
                        conn = sqlite3.connect(str(DB_PATH))
                        conn.execute("UPDATE users SET is_subscribed=1, plan_type=? WHERE email=?", 
                                      (plan_type, user_email))
                        conn.commit()
                        conn.close()
            
            elif user_email:
                # Credit purchase (default)
                db_add_credits(user_email, credits, f"stripe_purchase_{plan_id}", "")
                user = db_get_user(user_email)
                if user:
                    db_record_payment(
                        user["id"],
                        session.get("amount_total", 0) / 100,
                        plan_id,
                        credits,
                        session.get("id", ""),
                        "completed"
                    )
        
        return {"status": "received"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


# =============================================================================
# PHASE 3: STRIPE SUBSCRIPTION & RENDER UNLOCK
# =============================================================================
@app.post("/api/stripe/create-subscription")
async def create_subscription(
    user_email: str = Form(...),
    plan_type: str = Form("dealership")
):
    """Create monthly subscription (dealership/enterprise)."""
    if not FEATURES_ENABLED.get("payments", False):
        return {"status": "disabled", "error": "Payments feature is disabled"}
    if not STRIPE_KEY:
        return {"error": "Stripe not configured"}
    
    plan = PRICING_PLANS.get(plan_type)
    if not plan:
        return {"error": "Invalid plan"}
    
    try:
        import stripe
        stripe.api_key = STRIPE_KEY
        
        # Create or get customer
        customer = None
        user = db_get_user(user_email)
        if user and user.get("stripe_customer_id"):
            customer = stripe.Customer.retrieve(user["stripe_customer_id"])
        else:
            customer = stripe.Customer.create(email=user_email)
        
        # Create subscription price (you'd create this in Stripe dashboard)
        price_id = "price_monthly_subscription"  # Replace with actual Stripe price ID
        
        subscription = stripe.Subscription.create(
            customer=customer.id,
            items=[{"price": price_id, "quantity": 1}],
            metadata={"user_email": user_email, "plan_type": plan_type}
        )
        
        return {"status": "ok", "subscription_id": subscription.id, "client_secret": subscription.get("latest_invoice", {}).get("payment_intent", {}).get("client_secret")}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/render/unlock")
async def unlock_render(
    vin: str = Form(...),
    user_email: str = Form("")
):
    """Create payment link to unlock a rendered vehicle (remove watermark)."""
    if not FEATURES_ENABLED.get("payments", False):
        return {"status": "disabled", "error": "Payments feature is disabled"}
    if not STRIPE_KEY:
        return {"error": "Stripe not configured"}
    
    vehicle = db_get_vehicle(vin)
    if not vehicle:
        return {"error": "Vehicle not found"}
    
    if vehicle.get("is_paid"):
        return {"status": "already_paid", "message": "Vehicle already unlocked"}
    
    try:
        import stripe
        stripe.api_key = STRIPE_KEY
        
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": f"LotVue Pro - Unlock {vin}"},
                    "unit_amount": 299,  # $2.99
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=f"/view/{vin}?payment=success",
            cancel_url=f"/view/{vin}?payment=cancelled",
            metadata={"vin": vin, "user_email": user_email, "type": "unlock_vehicle"}
        )
        
        return {"status": "ok", "payment_url": session.url, "session_id": session.id}
    except Exception as e:
        return {"error": str(e)}


# =============================================================================
# PHASE 4: RENDER STATUS API
# =============================================================================
@app.get("/api/render/{vin}/status")
async def get_render_status(vin: str):
    """Check render progress for a vehicle."""
    vehicle = db_get_vehicle(vin)
    if not vehicle:
        return {"error": "Vehicle not found"}
    
    vehicle_dir = STORAGE_DIR / vin
    spin_dir = vehicle_dir / "spin"
    exterior_dir = vehicle_dir / "exterior"
    
    spin_frames = len(list(spin_dir.glob("spin_*.jpg"))) if spin_dir.exists() else 0
    exterior_frames = len(list(exterior_dir.glob("frame_*.jpg"))) if exterior_dir.exists() else 0
    
    return {
        "vin": vin,
        "status": "ready" if spin_frames >= 36 else "processing",
        "spin_frames": spin_frames,
        "exterior_frames": exterior_frames,
        "watermark_mode": vehicle.get("watermark_mode", "preview"),
        "is_paid": bool(vehicle.get("is_paid", 0)),
        "created_at": vehicle.get("created_at", "")
    }


# =============================================================================
# PHASE 6: API ENDPOINTS (COMPLETE)
# =============================================================================
@app.get("/api/render/{vin}/preview")
async def get_render_preview(vin: str):
    """Get watermarked preview URL for a vehicle."""
    vehicle = db_get_vehicle(vin)
    if not vehicle:
        return {"error": "Vehicle not found"}
    
    spin_dir = STORAGE_DIR / vin / "spin"
    if spin_dir.exists() and list(spin_dir.glob("spin_*.jpg")):
        return {
            "vin": vin,
            "preview_url": f"/storage/{vin}/spin/spin_0001.jpg",
            "watermark_mode": vehicle.get("watermark_mode", "preview")
        }
    
    exterior_dir = STORAGE_DIR / vin / "exterior"
    if exterior_dir.exists() and list(exterior_dir.glob("frame_*.jpg")):
        return {
            "vin": vin,
            "preview_url": f"/storage/{vin}/exterior/frame_0001.jpg",
            "watermark_mode": vehicle.get("watermark_mode", "preview")
        }
    
    return {"error": "No frames found"}


@app.get("/api/render/{vin}/clean")
async def get_render_clean(vin: str, user_email: str = ""):
    """Get clean (no watermark) URL - paid only."""
    vehicle = db_get_vehicle(vin)
    if not vehicle:
        return {"error": "Vehicle not found"}
    
    # Check if paid
    if not vehicle.get("is_paid", 0):
        # Check if user has unlimited plan
        if user_email:
            check = check_user_can_render(user_email)
            if not check.get("is_subscribed"):
                return {"error": "Payment required", "payment_url": f"/view/{vin}?unlock=1"}
        else:
            return {"error": "Payment required", "payment_url": f"/view/{vin}?unlock=1"}
    
    spin_dir = STORAGE_DIR / vin / "spin"
    if spin_dir.exists() and list(spin_dir.glob("spin_*.jpg")):
        return {
            "vin": vin,
            "clean_url": f"/storage/{vin}/spin/spin_0001.jpg",
            "is_paid": bool(vehicle.get("is_paid", 0))
        }
    
    return {"error": "No frames found"}


@app.get("/api/inventory")
async def get_inventory(dealer: str = "", user_email: str = ""):
    """List all renders for a tenant/user."""
    if dealer:
        vehicles = db_get_vehicles_by_dealer(dealer)
    elif user_email:
        vehicles = db_get_vehicles_by_email(user_email)
    else:
        vehicles = db_get_vehicles()
    
    return {
        "vehicles": [
            {
                "vin": v.get("vin"),
                "year": v.get("year", ""),
                "make": v.get("make", ""),
                "model": v.get("model", ""),
                "exterior_frames": v.get("exterior_frames", 0),
                "interior_count": v.get("interior_count", 0),
                "watermark_mode": v.get("watermark_mode", "preview"),
                "is_paid": bool(v.get("is_paid", 0)),
                "created_at": v.get("created_at", ""),
                "thumbnail": f"/storage/{v.get('vin')}/thumbnail.jpg"
            }
            for v in vehicles
        ]
    }


@app.delete("/api/render/{vin}")
async def delete_render(vin: str, user_email: str = ""):
    """Delete a render."""
    vehicle = db_get_vehicle(vin)
    if not vehicle:
        return {"error": "Vehicle not found"}
    
    # Check ownership
    if user_email and vehicle.get("user_email") != user_email:
        return {"error": "Not authorized"}
    
    # Delete files
    vehicle_dir = STORAGE_DIR / vin
    if vehicle_dir.exists():
        shutil.rmtree(vehicle_dir)
    
    # Delete from DB
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE vehicles SET status='deleted' WHERE vin=?", (vin,))
    conn.commit()
    conn.close()
    
    return {"status": "ok", "vin": vin}


@app.get("/api/user/{email}")
async def get_user(email: str):
    user = db_get_user(email)
    if not user:
        return {"email": email, "credits": 5, "plan": "free"}
    return {
        "email": user.get("email"),
        "name": user.get("name"),
        "credits": user.get("credits", 5),
        "plan": user.get("plan", "free"),
    }


@app.post("/api/register")
async def register_user(
    email: str = Form(...),
    name: str = Form(""),
    password: str = Form("")
):
    if not FEATURES_ENABLED.get("auth", False):
        return {"status": "disabled", "error": "Auth feature is disabled"}
    import hashlib
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    db_create_user(email, name, pw_hash)
    return {"status": "ok", "email": email}


@app.post("/api/login")
async def login_user(
    response: Response,
    email: str = Form(...),
    password: str = Form("")
):
    if not FEATURES_ENABLED.get("auth", False):
        return {"status": "disabled", "error": "Auth feature is disabled"}
    import hashlib
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    user = db_get_user(email)
    if not user or user.get("password_hash") != pw_hash:
        return {"status": "error", "error": "Invalid credentials"}
    # issue the same JWT session cookie the auth gate checks
    try:
        from a2_extensions import _make_tokens, ACCESS_TTL_H
        tokens = _make_tokens(user["email"])
        response.set_cookie("lotvue_token", tokens["access_token"], httponly=True,
                            samesite="lax", max_age=ACCESS_TTL_H * 3600)
    except Exception as e:
        print(f"[Auth] cookie issue failed (login still ok): {e}")
    return {"status": "ok", "email": user["email"], "credits": user.get("credits", 5), "plan": user.get("plan", "free")}


@app.post("/api/set-plan")
async def set_user_plan(
    email: str = Form(...),
    plan: str = Form("free")
):
    if not FEATURES_ENABLED.get("auth", False):
        return {"status": "disabled", "error": "Auth feature is disabled"}
    if plan not in PLANS:
        return {"status": "error", "error": "Invalid plan"}
    user = db_get_user(email)
    if not user:
        db_create_user(email)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE users SET plan=? WHERE email=?", (plan, email))
    conn.commit()
    conn.close()
    if plan == "dealership":
        db_update_dealer_plan("default", "dealership")
    return {"status": "ok", "plan": plan}


# =============================================================================
# API — HEALTH CHECK
# =============================================================================
@app.get("/api/health")
async def health_check():
    import os
    vg = db_get_vehicles()
    return {
        "status": "ok",
        "app": "LotVue Pro",
        "version": "2.0.0",
        "gpu": GPU_INFO,
        "rembg_available": REMBG_AVAILABLE,
        "heic_available": HEIC_SUPPORTED,
        "storage_dir": str(STORAGE_DIR),
        "storage_exists": STORAGE_DIR.exists(),
        "vehicle_count": len(vg),
        "ffmpeg_path": FFMPEG if os.path.exists(FFMPEG) else "not found",
        "ffprobe_path": FFPROBE if os.path.exists(FFPROBE) else "not found",
        "timestamp": datetime.now().isoformat()
    }


# =============================================================================
# API — USER CREDIT STATUS (Phase 2)
# =============================================================================
@app.get("/api/credits")
async def get_user_credits(email: str = ""):
    """Get user's credit status and plan info."""
    if not email:
        return {"error": "email required", "credits": 0}
    
    user = db_get_user(email)
    if not user:
        return {
            "email": email,
            "credits": 10,
            "plan": "free",
            "is_subscribed": False,
            "subscription_end": ""
        }
    
    return {
        "email": email,
        "credits": user.get("credits", 0),
        "plan": user.get("plan", "free"),
        "plan_type": user.get("plan_type", "free"),
        "is_subscribed": bool(user.get("is_subscribed", False)),
        "subscription_end": user.get("subscription_end", "")
    }


# =============================================================================
# PHASE 5: TENANT API ENDPOINTS
# =============================================================================
@app.get("/api/tenant/presets")
async def get_background_presets():
    """Get available background presets."""
    return {"presets": BACKGROUND_PRESETS}


@app.post("/api/tenant/dealer")
async def create_dealer(
    name: str = Form(...),
    email: str = Form(""),
    display_name: str = Form(""),
    background_preset: str = Form("showroom")
):
    """Create a new dealer/tenant."""
    if db_get_dealer(name):
        return {"error": "Dealer already exists"}
    
    dealer_id = db_create_dealer(name, email, display_name, background_preset)
    return {"status": "ok", "dealer_id": dealer_id, "name": name}


@app.get("/api/tenant/dealer/{dealer_name}")
async def get_dealer(dealer_name: str):
    """Get dealer details."""
    dealer = db_get_dealer(dealer_name)
    if not dealer:
        return {"error": "Dealer not found"}
    return dealer


@app.get("/api/tenant/dealers")
async def list_dealers():
    """List all dealers."""
    return {"dealers": db_list_dealers()}


@app.put("/api/tenant/dealer/{dealer_name}")
async def update_dealer(
    dealer_name: str,
    display_name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    address: str = Form(""),
    website: str = Form(""),
    logo_url: str = Form(""),
    brand_color: str = Form(""),
    background_preset: str = Form(""),
    plan: str = Form("")
):
    """Update dealer settings."""
    dealer = db_get_dealer(dealer_name)
    if not dealer:
        return {"error": "Dealer not found"}
    
    updates = {}
    if display_name: updates["display_name"] = display_name
    if phone: updates["phone"] = phone
    if email: updates["email"] = email
    if address: updates["address"] = address
    if website: updates["website"] = website
    if logo_url: updates["logo_url"] = logo_url
    if brand_color: updates["brand_color"] = brand_color
    if background_preset: updates["background_preset"] = background_preset
    if plan: updates["plan"] = plan
    
    if updates:
        db_update_dealer(dealer_name, **updates)
    
    return {"status": "ok", "dealer": db_get_dealer(dealer_name)}


@app.get("/api/tenant/me")
async def get_my_dealer(email: str = ""):
    """Get dealer for current user."""
    if not email:
        return {"error": "email required"}
    
    dealer = db_get_dealer_by_email(email)
    if not dealer:
        return {"error": "No dealer found", "has_dealer": False}
    
    return {"dealer": dealer, "has_dealer": True}


# =============================================================================
# HELPERS
# =============================================================================
def validate_vin(vin: str) -> str:
    """Validate and normalize VIN. Returns uppercase VIN or raises."""
    vin = vin.upper().strip()
    if len(vin) != 17:
        raise HTTPException(status_code=400, detail="VIN must be 17 characters")
    if re.search(r'[IOQ]', vin):
        raise HTTPException(status_code=400, detail="VIN cannot contain I, O, or Q")
    if not re.match(r'^[A-HJ-NPR-Z0-9]{17}$', vin):
        raise HTTPException(status_code=400, detail="VIN must be alphanumeric (no I/O/Q)")
    return vin


def get_video_duration(video_path: Path) -> float:
    """Get video duration in seconds using ffprobe."""
    cmd = [
        FFPROBE, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise Exception(f"ffprobe failed: {result.stderr}")
    return float(result.stdout.strip())


def extract_frames(video_path: Path, output_dir: Path, num_frames: int = 36):
    """
    Extract EXACTLY num_frames evenly spaced from video.
    
    FIXED: Big Pickle used fps=24 which means "24 frames per second" 
    (producing thousands of frames). This version calculates the correct
    interval to get exactly num_frames total.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Clear any existing frames
    for old in output_dir.glob("frame_*.jpg"):
        old.unlink()
    
    # Get duration
    duration = get_video_duration(video_path)
    
    if duration <= 0:
        raise Exception("Video has zero duration")
    
    # Calculate fps to get exactly num_frames
    target_fps = num_frames / duration
    
    output_pattern = str(output_dir / "frame_%04d.jpg")
    cmd = [
        FFMPEG, "-i", str(video_path),
        "-vf", f"fps={target_fps}",
        "-frames:v", str(num_frames),
        "-q:v", "2",  # High quality JPEG
        "-y",
        output_pattern
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise Exception(f"FFmpeg failed: {result.stderr}")
    
    frames = sorted(output_dir.glob("frame_*.jpg"))
    
    # Safety trim if we got extras
    for f in frames[num_frames:]:
        f.unlink()
    
    # Process all frames with background removal
    process_all_frames_background(output_dir)
    
    return min(len(frames), num_frames)


def composite_turntable(bg_img: Image.Image, car: Image.Image, car_x: int, car_y: int,
                         turntable_style: str = "studio_shadow") -> Image.Image:
    """
    Composite a turntable/shadow ellipse under the car BEFORE pasting the car.
    turntable_style: 'studio_shadow' | 'concrete' | 'glossy_black' | 'none'
    """
    if turntable_style == "none":
        return bg_img

    # Calculate ellipse dimensions based on car footprint
    car_w, car_h = car.size
    ellipse_w = int(car_w * 0.85)
    ellipse_h = int(car_h * 0.18)
    ellipse_x = car_x + (car_w - ellipse_w) // 2
    ellipse_y = car_y + car_h - ellipse_h // 2

    # Create shadow layer
    shadow_layer = Image.new("RGBA", bg_img.size, (0, 0, 0, 0))
    from PIL import ImageDraw, ImageFilter
    draw = ImageDraw.Draw(shadow_layer)

    if turntable_style == "studio_shadow":
        # Soft dark ellipse — studio floor shadow
        draw.ellipse(
            [ellipse_x, ellipse_y, ellipse_x + ellipse_w, ellipse_y + ellipse_h],
            fill=(0, 0, 0, 140)
        )
        shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=18))
    elif turntable_style == "concrete":
        # Harder, lighter shadow for concrete look
        draw.ellipse(
            [ellipse_x, ellipse_y, ellipse_x + ellipse_w, ellipse_y + ellipse_h],
            fill=(60, 60, 60, 100)
        )
        shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=10))
    elif turntable_style == "glossy_black":
        # Reflection-style: mirror gradient ellipse
        draw.ellipse(
            [ellipse_x, ellipse_y, ellipse_x + ellipse_w, ellipse_y + ellipse_h],
            fill=(20, 20, 20, 180)
        )
        shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=8))

    # Composite shadow onto background
    bg_rgba = bg_img.convert("RGBA")
    bg_rgba = Image.alpha_composite(bg_rgba, shadow_layer)
    return bg_rgba.convert("RGB")


def auto_align_frames(frames: list, canvas_w: int = 1920, canvas_h: int = 1080) -> list:
    """
    Auto-align car frames so the car is consistently centered and sized.
    Uses bounding box of non-transparent pixels from RGBA frames.
    Returns list of (car_image_RGBA, x_offset, y_offset) tuples.
    """
    bboxes = []
    for car in frames:
        bbox = car.getbbox()  # bounding box of non-transparent area
        if bbox:
            bboxes.append(bbox)
        else:
            bboxes.append((0, 0, car.width, car.height))

    if not bboxes:
        return [(car, (canvas_w - car.width) // 2, (canvas_h - car.height) // 2) for car in frames]

    # Find median car width and height for consistent scaling
    widths = [b[2] - b[0] for b in bboxes]
    heights = [b[3] - b[1] for b in bboxes]
    median_w = sorted(widths)[len(widths) // 2]
    median_h = sorted(heights)[len(heights) // 2]

    # Target: car fills ~85% of canvas width, centered
    target_w = int(canvas_w * 0.85)
    scale = min(target_w / max(median_w, 1), (canvas_h * 0.80) / max(median_h, 1))

    aligned = []
    for car, bbox in zip(frames, bboxes):
        # Crop to bounding box
        cropped = car.crop(bbox)
        new_w = int(cropped.width * scale)
        new_h = int(cropped.height * scale)
        if new_w > 0 and new_h > 0:
            resized = cropped.resize((new_w, new_h), Image.LANCZOS)
        else:
            resized = cropped
        x = (canvas_w - resized.width) // 2
        y = int(canvas_h * 0.08)  # 8% from top — consistent vertical position
        aligned.append((resized, x, y))

    return aligned


def process_all_frames_background(frames_dir: Path, watermark_mode: str = "preview",
                                   background_path: str = None, turntable_style: str = "studio_shadow",
                                   bg_color: tuple = (26, 26, 26)):
    """Prepare frames for the 360 spin. Two modes (env LOTVUE_RENDER_MODE):
      - 'natural': KEEP the photographed background. Just auto-orient (EXIF),
        normalize every frame to ONE uniform size so the spin doesn't jitter, and
        apply a light sharpen. No cutout, no studio floor, no watermark — cleanest
        look for a real walkaround video / on-lot photos.
      - 'studio' (default): rembg cutout + auto-align + turntable shadow on a clean
        backdrop (the GLO3D-style look; needs rembg).
    """
    frames = sorted(list(frames_dir.glob("frame_*.jpg")) + list(frames_dir.glob("spin_*.jpg")))
    if not frames:
        print(f"[Render] No frames found in {frames_dir}")
        return

    render_mode = os.environ.get("LOTVUE_RENDER_MODE", "studio").lower()
    watermark_on = (watermark_mode == "preview") and FEATURES_ENABLED.get("watermark", False)

    # ── NATURAL mode: keep the real background; clean + normalize only ──────────
    if render_mode == "natural":
        from PIL import ImageOps, ImageFilter
        first = ImageOps.exif_transpose(Image.open(frames[0])).convert("RGB")
        fw, fh = first.size
        scale = min(1.0, 1600 / max(fw, fh))                 # cap longest side at 1600px
        TW, TH = (int(fw * scale) // 2) * 2, (int(fh * scale) // 2) * 2
        for frame_path in frames:
            try:
                im = ImageOps.exif_transpose(Image.open(frame_path)).convert("RGB")
                im = ImageOps.fit(im, (TW, TH), Image.LANCZOS, centering=(0.5, 0.5))
                im = im.filter(ImageFilter.UnsharpMask(radius=1.4, percent=85, threshold=2))
                if watermark_on:
                    im = apply_diagonal_watermark(im, "LOTVUE PRO PREVIEW", 60)
                im.save(frame_path, "JPEG", quality=94)
            except Exception as e:
                print(f"[Render] natural frame failed {frame_path.name}: {e}")
        print(f"[Render] natural mode: kept background, normalized {len(frames)} frames to {TW}x{TH}.")
        return

    # ── STUDIO mode: rembg cutout + composite (needs rembg) ─────────────────────
    if not REMBG_AVAILABLE:
        print("[BG Removal] rembg not available — skipping")
        return

    CANVAS_W, CANVAS_H = 1920, 1080

    # Load background image if provided
    bg_base = None
    if background_path and Path(background_path).exists():
        try:
            bg_base = Image.open(background_path).convert("RGB").resize((CANVAS_W, CANVAS_H), Image.LANCZOS)
        except Exception as e:
            print(f"[BG Removal] Could not load background image: {e}")

    # Step 1: Remove background from all frames, collect RGBA cars
    print(f"[BG Removal] Processing {len(frames)} frames...")
    car_frames = []
    for frame_path in frames:
        try:
            with open(frame_path, "rb") as f:
                raw = f.read()
            result_bytes = remove_bg(raw)
            car = Image.open(io.BytesIO(result_bytes)).convert("RGBA")
            car_frames.append((frame_path, car))
        except Exception as e:
            print(f"[BG Removal] Failed on {frame_path.name}: {e}")
            car_frames.append((frame_path, None))

    # Step 2: Auto-align all valid frames
    valid_cars = [car for _, car in car_frames if car is not None]
    if valid_cars:
        aligned = auto_align_frames(valid_cars, CANVAS_W, CANVAS_H)
    else:
        return

    # Step 3: Composite each frame onto background + turntable
    aligned_idx = 0
    for frame_path, car in car_frames:
        if car is None:
            continue
        try:
            resized_car, x, y = aligned[aligned_idx]
            aligned_idx += 1

            # Build background
            if bg_base:
                bg = bg_base.copy()
            else:
                bg = Image.new("RGB", (CANVAS_W, CANVAS_H), bg_color)

            # Composite turntable shadow UNDER car
            bg = composite_turntable(bg, resized_car, x, y, turntable_style)

            # Paste car on top
            bg_rgba = bg.convert("RGBA")
            bg_rgba.paste(resized_car, (x, y), resized_car)
            final = bg_rgba.convert("RGB")

            # Apply watermark (only when the watermark feature is enabled)
            if watermark_on:
                final = apply_diagonal_watermark(final, "LOTVUE PRO PREVIEW", 60)

            final.save(frame_path, "JPEG", quality=92)
        except Exception as e:
            print(f"[BG Removal] Composite failed for {frame_path.name}: {e}")

    print(f"[BG Removal] Done. {aligned_idx} frames processed.")


def interpolate_optical_flow(img_a: np.ndarray, img_b: np.ndarray, t: float) -> np.ndarray:
    """
    Interpolate between two images using Farneback optical flow.
    t: 0.0 -> img_a, 1.0 -> img_b
    """
    gray_a = cv2.cvtColor(img_a, cv2.COLOR_BGR2GRAY)
    gray_b = cv2.cvtColor(img_b, cv2.COLOR_BGR2GRAY)

    flow = cv2.calcOpticalFlowFarneback(gray_a, gray_b, None,
                                          pyr_scale=0.5, levels=3, winsize=15,
                                          iterations=3, poly_n=5, poly_sigma=1.2, flags=0)

    h, w = flow.shape[:2]
    # Vectorized remap-grid build (was pure-Python nested per-pixel loops over
    # ~2M px/frame — the #1 wall for photo->spin). meshgrid is mathematically
    # identical and ~1000x faster.
    grid_x, grid_y = np.meshgrid(np.arange(w, dtype=np.float32),
                                 np.arange(h, dtype=np.float32))

    flow_t = flow * t
    map_x = grid_x + flow_t[..., 0]
    map_y = grid_y + flow_t[..., 1]

    flow_inv_t = -flow * (1.0 - t)
    map_x_inv = grid_x + flow_inv_t[..., 0]
    map_y_inv = grid_y + flow_inv_t[..., 1]

    warped_a = cv2.remap(img_a, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT_101)
    warped_b = cv2.remap(img_b, map_x_inv, map_y_inv, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT_101)

    blended = cv2.addWeighted(warped_a, 1.0 - t, warped_b, t, 0)
    return blended


def interpolate_frames(input_dir: Path, output_dir: Path, target_frames: int = 36,
                       manifest: dict | None = None, use_optical_flow: bool = True):
    """
    Take N input frames, generate intermediate frames for smooth rotation.
    If manifest is provided, uses gap degrees to distribute target_frames proportionally.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    for old in output_dir.glob("spin_*.jpg"):
        old.unlink()

    frames_list = sorted(input_dir.glob("frame_*.jpg"))
    n = len(frames_list)
    if n == 0:
        return 0

    # If already enough frames, just copy
    if n >= target_frames:
        for i, f in enumerate(frames_list[:target_frames]):
            shutil.copy(f, output_dir / f"spin_{i+1:04d}.jpg")
        return min(n, target_frames)

    images = []
    for f in frames_list:
        img = cv2.imread(str(f))
        if img is not None:
            images.append(img)

    n = len(images)
    if n == 0:
        return 0

    # Determine frames per gap
    if manifest and "gaps" in manifest:
        gaps = manifest["gaps"]
        gap_frames = []
        total_degrees = sum(g["degrees"] for g in gaps)
        for g in gaps:
            frames_for_gap = max(1, round(target_frames * g["degrees"] / total_degrees))
            gap_frames.append(frames_for_gap)
        # Adjust to hit target exactly
        while sum(gap_frames) < target_frames:
            gap_frames[gap_frames.index(min(gap_frames))] += 1
        while sum(gap_frames) > target_frames:
            gap_frames[gap_frames.index(max(gap_frames))] -= 1
    else:
        total_gaps = n
        base = target_frames // total_gaps
        extra = target_frames % total_gaps
        gap_frames = [base + (1 if i < extra else 0) for i in range(total_gaps)]

    output_idx = 1
    for i in range(n):
        img_a = images[i]
        img_b = images[(i + 1) % n]
        count = gap_frames[i]

        for j in range(count):
            t = j / count if count > 0 else 0
            if use_optical_flow and 0 < t < 1:
                try:
                    blended = interpolate_optical_flow(img_a, img_b, t)
                except Exception:
                    blended = cv2.addWeighted(img_a, 1.0 - t, img_b, t, 0)
            else:
                blended = cv2.addWeighted(img_a, 1.0 - t, img_b, t, 0)
            out_path = output_dir / f"spin_{output_idx:04d}.jpg"
            cv2.imwrite(str(out_path), blended, [cv2.IMWRITE_JPEG_QUALITY, 92])
            output_idx += 1

    return output_idx - 1


def convert_photo_to_jpg(raw_bytes: bytes, filename: str, save_path: Path,
                          max_width: int = 1920, max_height: int = 1080,
                          quality: int = 92, sharpen: bool = True):
    """
    Phase 2B upgraded image processor:
    - Converts any format (HEIC, PNG, WEBP, JPG) to JPEG
    - Auto-rotates based on EXIF orientation
    - Resizes to max 1920x1080 maintaining aspect ratio
    - Applies mild unsharp mask for crispness
    - Strips all EXIF metadata (privacy + size reduction)
    """
    from PIL import ImageOps, ImageFilter
    ext = Path(filename).suffix.lower()

    if ext in [".heic", ".heif"] and not HEIC_SUPPORTED:
        raise HTTPException(
            status_code=400,
            detail="HEIC not supported. On iPhone: Settings \u2192 Camera \u2192 Formats \u2192 Most Compatible. Then retake photos as JPG."
        )

    try:
        img = Image.open(io.BytesIO(raw_bytes))

        # Auto-rotate based on EXIF orientation (fixes sideways iPhone photos)
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass

        # Convert to RGB (handles RGBA, palette, etc.)
        if img.mode != "RGB":
            img = img.convert("RGB")

        # Resize: maintain aspect ratio, cap at max_width x max_height
        orig_w, orig_h = img.size
        if orig_w > max_width or orig_h > max_height:
            ratio = min(max_width / orig_w, max_height / orig_h)
            new_w = int(orig_w * ratio)
            new_h = int(orig_h * ratio)
            img = img.resize((new_w, new_h), Image.LANCZOS)

        # Mild unsharp mask for crispness (especially after resize)
        if sharpen:
            img = img.filter(ImageFilter.UnsharpMask(radius=0.8, percent=110, threshold=3))

        # Save clean JPEG (no EXIF — strips all metadata)
        clean_img = Image.new("RGB", img.size)
        clean_img.paste(img)
        clean_img.save(save_path, "JPEG", quality=quality, optimize=True)

    except HTTPException:
        raise
    except Exception as e:
        # Fallback: save raw if it's already a standard format
        if ext in [".jpg", ".jpeg", ".png", ".webp"]:
            with open(save_path, "wb") as f:
                f.write(raw_bytes)
        else:
            raise HTTPException(status_code=400, detail=f"Cannot process image: {str(e)}")


def generate_thumbnail(source_dir: Path, thumb_path: Path, size=(480, 320)):
    """Generate a thumbnail from the first exterior frame."""
    frames = sorted(source_dir.glob("frame_*.jpg"))
    if not frames:
        frames = sorted(source_dir.glob("*.jpg"))  # fallback: any jpg
    if not frames:
        return False
    try:
        img = Image.open(frames[0])
        img.thumbnail(size, Image.LANCZOS)
        img.save(thumb_path, "JPEG", quality=75)
        return True
    except Exception:
        return False


# =============================================================================
# SHARED CSS
# =============================================================================
BASE_CSS = """
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { 
        font-family: 'Segoe UI', -apple-system, sans-serif; 
        background: #f0f4f8; color: #1a2332; min-height: 100vh;
        -webkit-font-smoothing: antialiased;
    }
    .header { 
        background: linear-gradient(135deg, #0d1117 0%, #161b22 100%); 
        padding: 1.2rem 2rem; 
        border-bottom: 1px solid #30363d;
        display: flex; justify-content: space-between; align-items: center;
    }
    .logo { 
        font-size: 1.4rem; font-weight: 700; letter-spacing: -0.5px;
        color: #58a6ff;
    }
    .logo span { color: #ffffff; }
        .container { max-width: 1100px; margin: 0 auto; padding: 1.5rem; }
    .btn { 
        background: #238636; border: 1px solid #2ea043;
        padding: 0.6rem 1.2rem; color: white; border-radius: 6px; 
        cursor: pointer; font-weight: 600; font-size: 0.9rem;
        text-decoration: none; display: inline-block; transition: all 0.15s;
    }
    .btn:hover { background: #2ea043; }
    .btn-danger { background: #da3633; border-color: #f85149; }
    .btn-danger:hover { background: #f85149; }
    .btn-outline { background: transparent; border: 1px solid #30363d; color: #8b949e; }
    .btn-outline:hover { background: #21262d; border-color: #484f58; }
    .back-link { color: #58a6ff; text-decoration: none; font-size: 0.9rem; }
    .back-link:hover { text-decoration: underline; }
    .hotspot-dot { position: absolute; width: 28px; height: 28px; border-radius: 50%; background: rgba(88,166,255,0.25); border: 2px solid rgba(88,166,255,0.7); cursor: pointer; pointer-events: all; transform: translate(-50%, -50%); animation: pulse 2s ease-in-out infinite; z-index: 10; }
    @keyframes pulse { 0%,100% { transform: translate(-50%, -50%) scale(1); opacity:1; } 50% { transform: translate(-50%, -50%) scale(1.25); opacity:0.6; } }
    .feature-btns { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-top: 1rem; }
    .feature-btn { background: #238636; border: none; padding: 0.5rem 1rem; color: white; border-radius: 6px; cursor: pointer; font-size: 0.9rem; }
"""

# =============================================================================
# ROUTES — DASHBOARD (Phase 1A Upgrade)
# =============================================================================
@app.get("/")
async def dashboard(request: Request):
    vehicles = db_get_vehicles()

    # Storage used
    total_bytes = 0
    try:
        for p in STORAGE_DIR.rglob("*"):
            if p.is_file() and "_showrooms" not in str(p):
                total_bytes += p.stat().st_size
    except Exception:
        pass
    storage_gb = round(total_bytes / (1024 ** 3), 2)

    # Status logic per vehicle — workflow_status overrides if present
    def get_status(v):
        ws = v.get("workflow_status", "")
        if ws and ws != "intake":
            return ws
        ext = v.get("exterior_frames", 0)
        if ext >= 8:
            return "published" if ws == "published" else "review"
        elif ext > 0:
            return "photos"
        return "intake"

    total_vehicles = len(vehicles)
    total_360 = sum(1 for v in vehicles if get_status(v) == "ready")

    # Build vehicle cards
    cards_html = ""
    for v in vehicles:
        vin = v["vin"]
        thumb_path = STORAGE_DIR / vin / "thumbnail.jpg"
        thumb_src = f"/storage/{vin}/thumbnail.jpg" if thumb_path.exists() else ""
        thumb_html = (
            f'<img src="{thumb_src}" class="card-thumb">'
            if thumb_src
            else '<div class="card-thumb no-thumb">No Preview</div>'
        )
        price = v.get("price", "")
        mileage = v.get("mileage", "")
        color = v.get("color", "")
        status = get_status(v)
        status_labels = {
            "intake": ("Intake", "#6c757d"),
            "photos": ("Photos", "#d29922"),
            "processing": ("Processing", "#2E75B6"),
            "review": ("Review", "#e67e22"),
            "published": ("Published", "#27ae60"),
        }
        slabel, scolor = status_labels[status]
        vin_short = vin[-6:] if len(vin) >= 6 else vin
        year_make_model = f"{v.get('year','')} {v.get('make','')} {v.get('model','')}".strip() or vin
        meta_parts = []
        if price: meta_parts.append(f'<span style="color:#3fb950;font-weight:700;">{price}</span>')
        if mileage: meta_parts.append(f'<span>{mileage} mi</span>')
        if color: meta_parts.append(f'<span>{color}</span>')
        meta_html = ' &middot; '.join(meta_parts) if meta_parts else ''
        cards_html += f"""
        <div class="vcard" data-vin="{vin}" data-make="{v.get('make','').lower()}" data-model="{v.get('model','').lower()}" data-year="{v.get('year','')}" data-status="{status}">
            <div style="position:relative;">
                {thumb_html}
                <span class="status-badge" style="background:{scolor}20;color:{scolor};border:1px solid {scolor}40;">{slabel}</span>
                <label class="card-check"><input type="checkbox" class="bulk-cb" value="{vin}"></label>
            </div>
            <div class="card-body">
                <div class="card-title">{year_make_model}</div>
                <div class="card-vin">VIN: ...{vin_short}</div>
                {f'<div class="card-meta">{meta_html}</div>' if meta_html else ''}
                <div class="card-counts">{v.get('exterior_frames',0)} exterior &middot; {v.get('interior_count',0)} interior</div>
                <div class="card-actions">
                    <a href="/view/{vin}" class="btn" style="font-size:0.8rem;padding:0.35rem 0.8rem;">View 360</a>
                    <a href="/view3d/{vin}" class="btn btn-outline" style="font-size:0.8rem;padding:0.35rem 0.8rem;">3D Viewer</a>
                    <a href="/tag/{vin}" class="btn btn-outline" style="font-size:0.8rem;padding:0.35rem 0.8rem;">Tag</a>
                    <button onclick="generateCommercial('{vin}', this)" id="comm-btn-{vin}" class="btn" style="font-size:0.8rem;padding:0.35rem 0.8rem;background:#7c3aed;border:none;color:white;" title="Generate AI Commercial">&#127916;</button>
                    <button onclick="showPricing('{vin}')" class="btn btn-outline" style="font-size:0.8rem;padding:0.35rem 0.8rem;" title="Market Pricing">&#128200;</button>
                    <button onclick="showSyndication('{vin}')" class="btn btn-outline" style="font-size:0.8rem;padding:0.35rem 0.8rem;" title="Export Listing">&#128228;</button>
                    <button onclick="deleteVehicle('{vin}')" class="btn btn-danger" style="font-size:0.8rem;padding:0.35rem 0.8rem;">&#128465;</button>
                </div>
                <div id="comm-status-{vin}" style="display:none;margin-top:0.5rem;font-size:0.78rem;padding:0.4rem 0.6rem;border-radius:4px;background:#1a1a2e;color:#a78bfa;border:1px solid #7c3aed40;"></div>
            </div>
        </div>
        """

    if not cards_html:
        cards_html = '<div class="empty-state"><div style="font-size:3rem;margin-bottom:1rem;">🚗</div><div style="font-size:1.1rem;color:#8b949e;margin-bottom:1.5rem;">No vehicles yet.</div><a href="/capture" class="btn">+ Add Your First Vehicle</a></div>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LotVue Pro — Dashboard</title>
    <link rel="manifest" href="/static/manifest.json">
    <meta name="theme-color" content="#1e3a5f">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <style>
        * {{ margin:0; padding:0; box-sizing:border-box; }}
        body {{ font-family:'DM Sans','Segoe UI',-apple-system,sans-serif; background:#F8FAFC; color:#2C3E50; min-height:100vh; }}
        .header {{ background:linear-gradient(135deg,#2E75B6 0%,#1a5a9e 100%); padding:1rem 2rem; display:flex; justify-content:space-between; align-items:center; box-shadow:0 2px 8px rgba(0,0,0,0.1); }}
        .logo {{ font-size:1.5rem; font-weight:800; color:#ffffff; letter-spacing:-0.5px; }}
        .nav {{ display:flex; gap:0.5rem; align-items:center; }}
        .nav a {{ color:rgba(255,255,255,0.8); text-decoration:none; padding:0.4rem 0.9rem; border-radius:6px; font-size:0.9rem; transition:all 0.15s; }}
        .nav a:hover {{ background:rgba(255,255,255,0.15); color:#fff; }}
        .nav .btn-add {{ background:#2E75B6; color:#fff; font-weight:700; padding:0.5rem 1.1rem; border-radius:6px; font-size:0.9rem; text-decoration:none; transition:all 0.15s; border:2px solid rgba(255,255,255,0.3); }}
        .nav .btn-add:hover {{ background:#1a5a9e; }}
        .container {{ max-width:1280px; margin:0 auto; padding:1.5rem 2rem; }}
        .stats-bar {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:1rem; margin-bottom:1.5rem; }}
        .stat {{ background:#ffffff; border:1px solid #d0d7de; border-radius:10px; padding:1rem 1.5rem; }}
        .stat-val {{ font-size:1.8rem; font-weight:800; color:#2E75B6; }}
        .stat-lbl {{ font-size:0.72rem; color:#6e7781; text-transform:uppercase; letter-spacing:0.6px; margin-top:2px; }}
        .toolbar {{ display:flex; gap:0.8rem; align-items:center; margin-bottom:1.2rem; flex-wrap:wrap; }}
        .search-box {{ flex:1; min-width:200px; position:relative; }}
        .search-box input {{ width:100%; padding:0.55rem 0.9rem 0.55rem 2.2rem; border:1px solid #d0d7de; border-radius:8px; font-size:0.9rem; background:#fff; color:#1a2332; outline:none; }}
        .search-box input:focus {{ border-color:#2E75B6; box-shadow:0 0 0 3px rgba(46,117,182,0.12); }}
        .search-icon {{ position:absolute; left:0.7rem; top:50%; transform:translateY(-50%); color:#8b949e; font-size:0.9rem; }}
        .filter-select {{ padding:0.55rem 0.9rem; border:1px solid #d0d7de; border-radius:8px; font-size:0.9rem; background:#fff; color:#1a2332; cursor:pointer; outline:none; }}
        .filter-select:focus {{ border-color:#2d5a8e; }}
        .bulk-bar {{ background:#1e3a5f; color:#fff; padding:0.6rem 1rem; border-radius:8px; display:none; align-items:center; gap:1rem; font-size:0.9rem; }}
        .bulk-bar.visible {{ display:flex; }}
        .bulk-del {{ background:#da3633; border:none; color:#fff; padding:0.4rem 1rem; border-radius:6px; cursor:pointer; font-weight:600; font-size:0.85rem; }}
        .bulk-del:hover {{ background:#f85149; }}
        .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(290px,1fr)); gap:1.2rem; }}
        .vcard {{ background:#ffffff; border:1px solid #d0d7de; border-radius:10px; overflow:hidden; transition:box-shadow 0.2s,transform 0.2s; }}
        .vcard:hover {{ box-shadow:0 4px 16px rgba(30,58,95,0.12); transform:translateY(-2px); }}
        .card-thumb {{ width:100%; height:185px; object-fit:cover; display:block; }}
        .no-thumb {{ width:100%; height:185px; background:#f0f4f8; display:flex; align-items:center; justify-content:center; color:#8b949e; font-size:0.9rem; }}
        .status-badge {{ position:absolute; top:0.6rem; right:0.6rem; padding:0.2rem 0.6rem; border-radius:20px; font-size:0.72rem; font-weight:700; letter-spacing:0.3px; }}
        .card-check {{ position:absolute; top:0.6rem; left:0.6rem; cursor:pointer; }}
        .card-check input {{ width:18px; height:18px; cursor:pointer; accent-color:#2d5a8e; }}
        .card-body {{ padding:1rem; }}
        .card-title {{ font-weight:700; color:#1a2332; font-size:1rem; margin-bottom:0.2rem; }}
        .card-vin {{ color:#8b949e; font-size:0.75rem; font-family:monospace; margin-bottom:0.4rem; }}
        .card-meta {{ color:#57606a; font-size:0.8rem; margin-bottom:0.4rem; }}
        .card-counts {{ color:#8b949e; font-size:0.78rem; margin-bottom:0.8rem; }}
        .card-actions {{ display:flex; gap:0.4rem; flex-wrap:wrap; }}
        .btn {{ background:#2E75B6; border:none; padding:0.4rem 0.8rem; color:white; border-radius:6px; cursor:pointer; font-weight:600; font-size:0.82rem; text-decoration:none; display:inline-block; transition:all 0.15s; }}
        .btn:hover {{ background:#1a5a9e; }}
        .btn-outline {{ background:transparent; border:1px solid #d0d7de; color:#57606a; }}
        .btn-outline:hover {{ background:#f0f4f8; border-color:#8b949e; }}
        .btn-danger {{ background:#da3633; }}
        .btn-danger:hover {{ background:#f85149; }}
        .empty-state {{ text-align:center; padding:5rem 2rem; color:#8b949e; }}
        @media(max-width:600px) {{ .container {{ padding:1rem; }} .stats-bar {{ grid-template-columns:repeat(2,1fr); }} }}
    </style>
</head>
<body>
    <div class="header">
        <div class="logo">LotVue Pro</div>
        <nav class="nav">
            <a href="/">Dashboard</a>
            <a href="/capture">Upload</a>
            <a href="/settings">Settings</a>
            <a href="/health" target="_blank">System</a>
            <a href="/capture" class="btn-add">+ Add Vehicle</a>
        </nav>
    </div>
    <div class="container">
        <div class="stats-bar">
            <div class="stat"><div class="stat-val">{total_vehicles}</div><div class="stat-lbl">Total Vehicles</div></div>
            <div class="stat"><div class="stat-val">{total_360}</div><div class="stat-lbl">360 Ready</div></div>
            <div class="stat"><div class="stat-val">${sum(int(v.get('price',0) or 0) for v in vehicles):,}</div><div class="stat-lbl">Total Inventory Value</div></div>
            <div class="stat"><div class="stat-val">{sum(1 for v in vehicles if get_status(v) == 'published')}</div><div class="stat-lbl">Published</div></div>
            <div class="stat"><div class="stat-val">{storage_gb} GB</div><div class="stat-lbl">Storage Used</div></div>
        </div>
        <div class="toolbar">
            <div class="search-box">
                <span class="search-icon">&#128269;</span>
                <input type="text" id="searchInput" placeholder="Search by make, model, year, VIN..." oninput="filterCards()">
            </div>
            <select class="filter-select" id="makeFilter" onchange="filterCards()"><option value="">All Makes</option>{''.join(f'<option value="{m.lower()}">{m}</option>' for m in sorted(set(v.get("make","") for v in vehicles if v.get("make",""))))}</select>
            <select class="filter-select" id="statusFilter" onchange="filterCards()">
                <option value="">All Status</option>
                <option value="intake">Intake</option>
                <option value="photos">Photos</option>
                <option value="processing">Processing</option>
                <option value="review">Review</option>
                <option value="published">Published</option>
            </select>
        </div>
        <div style="display:flex;gap:0.5rem;margin-bottom:1rem;flex-wrap:wrap;">
            <span style="font-size:0.85rem;color:#6c757d;align-self:center;margin-right:0.3rem;">Export:</span>
            <a href="/api/syndication/facebook-csv" class="btn btn-outline" style="font-size:0.78rem;padding:0.3rem 0.7rem;" download>Facebook CSV</a>
            <a href="/api/syndication/xml-feed" class="btn btn-outline" style="font-size:0.78rem;padding:0.3rem 0.7rem;" download>XML Feed</a>
            <a href="/api/syndication/json" class="btn btn-outline" style="font-size:0.78rem;padding:0.3rem 0.7rem;" target="_blank">JSON API</a>
        </div>
        <div class="bulk-bar" id="bulkBar">
            <span id="bulkCount">0 selected</span>
            <button class="bulk-del" onclick="bulkDelete()">Delete Selected</button>
            <button onclick="clearSelection()" style="background:transparent;border:1px solid rgba(255,255,255,0.3);color:#fff;padding:0.3rem 0.8rem;border-radius:6px;cursor:pointer;font-size:0.85rem;">Clear</button>
        </div>
        <div class="grid" id="vehicleGrid">{cards_html}</div>
    </div>
    <script>
        // Search + filter
        function filterCards() {{
            const q = document.getElementById('searchInput').value.toLowerCase();
            const make = document.getElementById('makeFilter').value;
            const status = document.getElementById('statusFilter').value;
            document.querySelectorAll('.vcard').forEach(c => {{
                const vin = c.dataset.vin.toLowerCase();
                const cm = c.dataset.make;
                const cy = c.dataset.year;
                const cmod = c.dataset.model;
                const cs = c.dataset.status;
                const matchQ = !q || vin.includes(q) || cm.includes(q) || cmod.includes(q) || cy.includes(q);
                const matchMake = !make || cm === make;
                const matchStatus = !status || cs === status;
                c.style.display = (matchQ && matchMake && matchStatus) ? '' : 'none';
            }});
        }}
        // Bulk selection
        function updateBulkBar() {{
            const checked = document.querySelectorAll('.bulk-cb:checked');
            const bar = document.getElementById('bulkBar');
            document.getElementById('bulkCount').textContent = checked.length + ' selected';
            bar.classList.toggle('visible', checked.length > 0);
        }}
        document.querySelectorAll('.bulk-cb').forEach(cb => cb.addEventListener('change', updateBulkBar));
        function clearSelection() {{
            document.querySelectorAll('.bulk-cb').forEach(cb => cb.checked = false);
            updateBulkBar();
        }}
        // custom confirm — native confirm() is suppressed in Edge/Chrome app-mode windows
        function lvConfirm(msg) {{
            return new Promise(function(resolve) {{
                var ov = document.createElement('div');
                ov.style.cssText = 'position:fixed;inset:0;background:rgba(20,30,45,.55);display:flex;align-items:center;justify-content:center;z-index:99999';
                ov.innerHTML = '<div style="background:#fff;border-radius:14px;max-width:380px;padding:1.4rem;box-shadow:0 12px 40px rgba(0,0,0,.3);text-align:center;font-family:sans-serif">' +
                    '<div style="font-size:1rem;color:#2C3E50;margin-bottom:1.1rem;line-height:1.4">' + msg + '</div>' +
                    '<button id="lvNo" style="padding:.6rem 1.2rem;border:1.5px solid #DDE5EE;background:#fff;color:#2C3E50;border-radius:9px;font-weight:600;cursor:pointer;margin-right:.6rem">Cancel</button>' +
                    '<button id="lvYes" style="padding:.6rem 1.2rem;border:none;background:#C0392B;color:#fff;border-radius:9px;font-weight:700;cursor:pointer">Delete</button></div>';
                document.body.appendChild(ov);
                ov.querySelector('#lvYes').onclick = function() {{ ov.remove(); resolve(true); }};
                ov.querySelector('#lvNo').onclick = function() {{ ov.remove(); resolve(false); }};
                ov.onclick = function(e) {{ if (e.target === ov) {{ ov.remove(); resolve(false); }} }};
            }});
        }}
        async function bulkDelete() {{
            const vins = [...document.querySelectorAll('.bulk-cb:checked')].map(cb => cb.value);
            if (!vins.length) return;
            if (!(await lvConfirm('Delete <b>' + vins.length + '</b> vehicle(s) and all their photos?'))) return;
            await Promise.all(vins.map(v => fetch('/api/vehicle/' + v, {{ method: 'DELETE', credentials: 'same-origin' }})));
            location.reload();
        }}
        // Single delete
        async function deleteVehicle(vin) {{
            if (!(await lvConfirm('Delete the entire vehicle <b>' + vin + '</b> and all its photos?<br>This cannot be undone.'))) return;
            const r = await fetch('/api/vehicle/' + vin, {{ method: 'DELETE', credentials: 'same-origin' }});
            if (r.ok) {{ location.reload(); }}
            else if (r.status === 401) {{ location.href = '/login?next=/dashboard'; }}
            else {{ alert('Delete failed (' + r.status + ')'); }}
        }}

        // ── AI Commercial Generator ──────────────────────────────────────────
        async function generateCommercial(vin, btn) {{
            const statusDiv = document.getElementById('comm-status-' + vin);
            const origText = btn.innerHTML;

            // Check if already done — show video player
            try {{
                const check = await fetch('/api/vehicle/' + vin + '/commercial-status');
                const checkData = await check.json();
                if (checkData.status === 'done' && checkData.url) {{
                    showCommercialPlayer(vin, checkData.url, checkData.size_mb);
                    return;
                }}
            }} catch(e) {{}}

            btn.disabled = true;
            btn.innerHTML = '&#9203; Starting...';
            if (statusDiv) {{ statusDiv.style.display = 'block'; statusDiv.textContent = 'Initializing pipeline...'; }}

            try {{
                const res = await fetch('/api/vehicle/' + vin + '/generate-commercial', {{ method: 'POST' }});
                const data = await res.json();

                if (data.status === 'already_running') {{
                    if (statusDiv) statusDiv.textContent = 'Already running — polling...';
                    pollCommercialStatus(vin, btn, origText, statusDiv);
                    return;
                }}

                if (data.status === 'started') {{
                    if (statusDiv) statusDiv.textContent = 'Pipeline started. Generating video...';
                    pollCommercialStatus(vin, btn, origText, statusDiv);
                }} else {{
                    btn.disabled = false;
                    btn.innerHTML = origText;
                    if (statusDiv) {{ statusDiv.style.color = '#f87171'; statusDiv.textContent = 'Error: ' + (data.detail || data.message || 'Unknown error'); }}
                }}
            }} catch(e) {{
                btn.disabled = false;
                btn.innerHTML = origText;
                if (statusDiv) {{ statusDiv.style.color = '#f87171'; statusDiv.textContent = 'Request failed: ' + e.message; }}
            }}
        }}

        function pollCommercialStatus(vin, btn, origText, statusDiv) {{
            const STEPS = {{
                'starting': 'Initializing...',
                'Loading WAN 2.2 I2V model': 'Loading AI model (WAN 2.2)...',
                'Generating cinematic slideshow (fallback)': 'Generating cinematic video...',
                'Generating video frames (WAN I2V)': 'AI generating video frames...',
                'Saving video frames': 'Saving video...',
                'Generating voiceover (Kokoro TTS)': 'Generating voiceover...',
                'Assembling commercial (FFmpeg)': 'Assembling final commercial...',
                'complete': 'Done!'
            }};

            const interval = setInterval(async () => {{
                try {{
                    const res = await fetch('/api/vehicle/' + vin + '/commercial-status');
                    const data = await res.json();

                    const stepLabel = STEPS[data.step] || data.step || 'Processing...';
                    const pct = data.progress || 0;

                    if (statusDiv) {{
                        statusDiv.style.color = '#a78bfa';
                        statusDiv.textContent = stepLabel + ' (' + pct + '%)';
                    }}
                    if (btn) btn.innerHTML = '&#9203; ' + pct + '%';

                    if (data.status === 'done') {{
                        clearInterval(interval);
                        if (btn) {{ btn.disabled = false; btn.innerHTML = '&#9654; Watch'; }}
                        if (statusDiv) {{ statusDiv.style.color = '#4ade80'; statusDiv.textContent = 'Commercial ready! ' + (data.size_mb || '') + ' MB'; }}
                        if (data.url) showCommercialPlayer(vin, data.url, data.size_mb);
                    }} else if (data.status === 'error') {{
                        clearInterval(interval);
                        if (btn) {{ btn.disabled = false; btn.innerHTML = origText; }}
                        if (statusDiv) {{ statusDiv.style.color = '#f87171'; statusDiv.textContent = 'Error: ' + (data.error || 'Unknown'); }}
                    }}
                }} catch(e) {{
                    // Network error — keep polling
                }}
            }}, 3000);
        }}

        function showCommercialPlayer(vin, url, sizeMb) {{
            // Create modal with video player
            const existing = document.getElementById('commercial-modal');
            if (existing) existing.remove();

            const modal = document.createElement('div');
            modal.id = 'commercial-modal';
            modal.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.85);z-index:9999;display:flex;align-items:center;justify-content:center;';
            modal.innerHTML = `
                <div style="background:#1a1a2e;border:1px solid #7c3aed;border-radius:12px;padding:1.5rem;max-width:780px;width:95%;position:relative;">
                    <button onclick="document.getElementById('commercial-modal').remove()" style="position:absolute;top:0.75rem;right:0.75rem;background:none;border:none;color:#8b949e;font-size:1.4rem;cursor:pointer;">&times;</button>
                    <h3 style="color:#a78bfa;margin-bottom:1rem;font-size:1.1rem;">&#127916; AI Commercial Ready</h3>
                    <video controls autoplay style="width:100%;border-radius:8px;background:#000;" src="${{url}}"></video>
                    <div style="margin-top:1rem;display:flex;gap:0.75rem;flex-wrap:wrap;">
                        <a href="${{url}}" download="commercial_${{vin}}.mp4" style="background:#7c3aed;color:white;padding:0.5rem 1rem;border-radius:6px;text-decoration:none;font-size:0.85rem;">&#11015; Download MP4 ${{sizeMb ? '(' + sizeMb + ' MB)' : ''}}</a>
                        <button onclick="regenerateCommercial('${{vin}}')" style="background:#374151;color:#d1d5db;border:none;padding:0.5rem 1rem;border-radius:6px;cursor:pointer;font-size:0.85rem;">&#8635; Regenerate</button>
                    </div>
                </div>
            `;
            document.body.appendChild(modal);
            modal.addEventListener('click', e => {{ if (e.target === modal) modal.remove(); }});
        }}

        async function regenerateCommercial(vin) {{
            document.getElementById('commercial-modal')?.remove();
            await fetch('/api/vehicle/' + vin + '/commercial', {{ method: 'DELETE' }});
            const btn = document.getElementById('comm-btn-' + vin);
            if (btn) generateCommercial(vin, btn);
        }}

        // Market Pricing popup
        async function showPricing(vin) {{
            const modal = document.createElement('div');
            modal.id = 'pricing-modal';
            modal.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;z-index:9999;';
            modal.innerHTML = '<div style="background:#F8FAFC;border-radius:12px;padding:2rem;max-width:480px;width:90%;position:relative;"><div style="text-align:center;color:#6c757d;">Loading pricing...</div></div>';
            document.body.appendChild(modal);
            modal.addEventListener('click', e => {{ if (e.target === modal) modal.remove(); }});

            try {{
                const res = await fetch('/api/vehicle/' + vin + '/market-price');
                const data = await res.json();
                const p = data.pricing;
                if (!p) {{ modal.querySelector('div > div').textContent = 'No pricing data available'; return; }}

                modal.querySelector('div').innerHTML = `
                    <button onclick="document.getElementById('pricing-modal').remove()" style="position:absolute;top:0.75rem;right:0.75rem;background:none;border:none;color:#8b949e;font-size:1.4rem;cursor:pointer;">&times;</button>
                    <h3 style="color:#2E75B6;margin-bottom:1rem;">&#128200; Market Price Intelligence</h3>
                    <div style="text-align:center;margin:1rem 0;">
                        <div style="font-size:2rem;font-weight:800;color:#2E75B6;">$${{p.market_average?.toLocaleString()}}</div>
                        <div style="color:#6c757d;font-size:0.85rem;">Market Average</div>
                        <div style="color:#6c757d;font-size:0.85rem;margin-top:4px;">Range: $${{p.suggested_low?.toLocaleString()}} — $${{p.suggested_high?.toLocaleString()}}</div>
                    </div>
                    ${{p.asking_price ? '<div style="text-align:center;margin:1rem 0;padding:0.75rem;border-radius:8px;background:' + p.rating_color + '15;border:1px solid ' + p.rating_color + '40;"><span style="color:' + p.rating_color + ';font-weight:700;font-size:1.1rem;">' + p.rating + '</span><div style="color:#6c757d;font-size:0.82rem;margin-top:4px;">Asking: $' + p.asking_price?.toLocaleString() + '</div></div>' : ''}}
                    <div style="font-size:0.82rem;color:#6c757d;margin-top:1rem;">
                        <div style="display:flex;justify-content:space-between;margin-bottom:4px;"><span>Body Type</span><span style="color:#2C3E50;font-weight:600;">${{p.factors?.body_type || '-'}}</span></div>
                        <div style="display:flex;justify-content:space-between;margin-bottom:4px;"><span>Make Premium</span><span style="color:#2C3E50;font-weight:600;">${{p.factors?.make_premium || '-'}}x</span></div>
                        <div style="display:flex;justify-content:space-between;margin-bottom:4px;"><span>Year Depreciation</span><span style="color:#2C3E50;font-weight:600;">${{p.factors?.year_depreciation || '-'}}x</span></div>
                        <div style="display:flex;justify-content:space-between;"><span>Mileage Factor</span><span style="color:#2C3E50;font-weight:600;">${{p.factors?.mileage_factor || '-'}}x</span></div>
                    </div>
                `;
            }} catch(e) {{
                modal.querySelector('div > div').textContent = 'Error loading pricing: ' + e.message;
            }}
        }}

        // Syndication export popup
        async function showSyndication(vin) {{
            const modal = document.createElement('div');
            modal.id = 'syndication-modal';
            modal.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;z-index:9999;';
            modal.innerHTML = `
                <div style="background:#F8FAFC;border-radius:12px;padding:2rem;max-width:420px;width:90%;position:relative;">
                    <button onclick="document.getElementById('syndication-modal').remove()" style="position:absolute;top:0.75rem;right:0.75rem;background:none;border:none;color:#8b949e;font-size:1.4rem;cursor:pointer;">&times;</button>
                    <h3 style="color:#2E75B6;margin-bottom:1rem;">&#128228; Export Listing</h3>
                    <div style="display:flex;flex-direction:column;gap:0.75rem;">
                        <a href="/api/vehicle/${{vin}}/craigslist" target="_blank" style="display:flex;align-items:center;gap:0.75rem;padding:0.75rem 1rem;border:1px solid #d0d7de;border-radius:8px;text-decoration:none;color:#2C3E50;transition:all 0.15s;">
                            <span style="font-size:1.3rem;">&#128196;</span>
                            <div><div style="font-weight:600;font-size:0.9rem;">Craigslist HTML</div><div style="font-size:0.78rem;color:#6c757d;">Copy-paste ready posting</div></div>
                        </a>
                        <a href="/api/vehicle/${{vin}}/embed" target="_blank" style="display:flex;align-items:center;gap:0.75rem;padding:0.75rem 1rem;border:1px solid #d0d7de;border-radius:8px;text-decoration:none;color:#2C3E50;transition:all 0.15s;">
                            <span style="font-size:1.3rem;">&#128187;</span>
                            <div><div style="font-weight:600;font-size:0.9rem;">Website Embed</div><div style="font-size:0.78rem;color:#6c757d;">HTML widget for your site</div></div>
                        </a>
                        <a href="/view3d/${{vin}}" target="_blank" style="display:flex;align-items:center;gap:0.75rem;padding:0.75rem 1rem;border:1px solid #d0d7de;border-radius:8px;text-decoration:none;color:#2C3E50;transition:all 0.15s;">
                            <span style="font-size:1.3rem;">&#127922;</span>
                            <div><div style="font-weight:600;font-size:0.9rem;">3D Viewer Link</div><div style="font-size:0.78rem;color:#6c757d;">Shareable cinematic viewer</div></div>
                        </a>
                    </div>
                    <div style="margin-top:1rem;padding-top:0.75rem;border-top:1px solid #e2e8f0;font-size:0.78rem;color:#6c757d;">
                        Bulk exports: <a href="/api/syndication/facebook-csv" style="color:#2E75B6;">Facebook CSV</a> &middot; <a href="/api/syndication/xml-feed" style="color:#2E75B6;">XML Feed</a> &middot; <a href="/api/syndication/json" style="color:#2E75B6;" target="_blank">JSON API</a>
                    </div>
                </div>
            `;
            document.body.appendChild(modal);
            modal.addEventListener('click', e => {{ if (e.target === modal) modal.remove(); }});
        }}

        if ('serviceWorker' in navigator) {{
            navigator.serviceWorker.register('/static/service-worker.js');
        }}
    </script>
    <!-- Dealer Tools + Quick Start helper (window sticker, buyers guide, showroom, syndication) -->
    <script src="/static/lv_dashboard_tools.js?v=20260612b" defer></script>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/dashboard")
async def dashboard_alias(request: Request):
    return await dashboard(request)


# =============================================================================
# ROUTES — CAPTURE GUIDE
# =============================================================================
@app.get("/capture-guide")
async def capture_guide():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>How to Capture 360° Photos — LotVue Pro</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Segoe UI', -apple-system, sans-serif; background: #f0f4f8; color: #1a2332; min-height: 100vh; }
        .header { background: linear-gradient(135deg, #e8edf2 0%, #dde3ea 100%); padding: 1rem 2rem; border-bottom: 1px solid #c5cdd6; display: flex; justify-content: space-between; align-items: center; }
        .logo { font-size: 1.4rem; font-weight: 700; color: #58a6ff; }
        .container { max-width: 900px; margin: 0 auto; padding: 2rem; }
        .card { background: #dde3ea; border: 1px solid #c5cdd6; border-radius: 8px; padding: 1.5rem; margin-bottom: 1.5rem; }
        h1 { font-size: 1.8rem; margin-bottom: 0.5rem; }
        .subtitle { color: #8b949e; margin-bottom: 2rem; }
        h2 { color: #1a2332; font-size: 1.1rem; margin-bottom: 1rem; }
        .tips { display: grid; gap: 1rem; }
        .tip { display: flex; gap: 1rem; align-items: flex-start; }
        .tip-icon { font-size: 1.5rem; min-width: 40px; }
        .tip-text h3 { color: #1a2332; font-size: 1rem; margin-bottom: 0.3rem; }
        .tip-text p { color: #8b949e; font-size: 0.9rem; line-height: 1.5; }
        .diagram { background: #e8edf2; border-radius: 8px; padding: 2rem; text-align: center; margin: 1.5rem 0; }
        .diagram svg { max-width: 100%; height: auto; }
        .btn { display: inline-block; background: #238636; border: none; padding: 0.8rem 1.5rem; color: white; border-radius: 6px; cursor: pointer; font-weight: 600; text-decoration: none; font-size: 1rem; }
        .btn:hover { background: #2ea043; }
        .btn-outline { background: transparent; border: 1px solid #b0bac5; color: #8b949e; }
        .steps { counter-reset: step; }
        .step { display: flex; gap: 1rem; margin-bottom: 1.2rem; counter-increment: step; }
        .step-num { 
            width: 32px; height: 32px; background: #238636; border-radius: 50%; 
            display: flex; align-items: center; justify-content: center; 
            font-weight: 600; font-size: 0.9rem; flex-shrink: 0;
        }
        .step-content h3 { color: #1a2332; font-size: 1rem; margin-bottom: 0.3rem; }
        .step-content p { color: #8b949e; font-size: 0.9rem; }
        .warning { background: #1c1209; border: 1px solid #3d2e00; border-radius: 6px; padding: 1rem; color: #d29922; font-size: 0.9rem; }
    </style>
</head>
<body>
    <div class="header">
        <div class="logo">LotVue Pro</div>
        <a href="/capture" class="btn">Start Capturing</a>
    </div>
    <div class="container">
        <h1>How to Capture 360° Photos</h1>
        <p class="subtitle">Follow these tips to create stunning 360° views of any vehicle</p>
        
        <div class="card">
            <h2>📱 Quick Tips</h2>
            <div class="tips">
                <div class="tip">
                    <div class="tip-icon">☀️</div>
                    <div class="tip-text">
                        <h3>Good Lighting is Key</h3>
                        <p>Park in shade or on a cloudy day. Avoid direct sunlight which creates harsh shadows. Overcast days are perfect!</p>
                    </div>
                </div>
                <div class="tip">
                    <div class="tip-icon">📏</div>
                    <div class="tip-text">
                        <h3>Maintain Distance</h3>
                        <p>Stay 10-15 feet (3-5 meters) from the vehicle. Too close causes perspective distortion. Too far loses detail.</p>
                    </div>
                </div>
                <div class="tip">
                    <div class="tip-icon">🔄</div>
                    <div class="tip-text">
                        <h3>Consistent Height</h3>
                        <p>Keep your phone at bumper level (about 3 feet / 1 meter from ground). This shows the best angle of the car.</p>
                    </div>
                </div>
                <div class="tip">
                    <div class="tip-icon">👟</div>
                    <div class="tip-text">
                        <h3>Walk in a Circle</h3>
                        <p>Walk around the car in a smooth circle while taking photos. Keep the car centered in your frame.</p>
                    </div>
                </div>
                <div class="tip">
                    <div class="tip-icon">🔒</div>
                    <div class="tip-text">
                        <h3>Hold Steady</h3>
                        <p>Hold your phone horizontally (landscape) with both hands. Tap to shoot rather than pressing the volume button to reduce shake.</p>
                    </div>
                </div>
            </div>
        </div>
        
        <div class="card">
            <h2>📐 Photo Positions</h2>
            <div class="diagram">
                <svg viewBox="0 0 600 200" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <rect x="200" y="40" width="200" height="100" rx="8" fill="#c5cdd6" stroke="#58a6ff" stroke-width="2"/>
                    <text x="300" y="95" fill="#e0e0e0" text-anchor="middle" font-size="14">CAR</text>
                    <circle cx="300" cy="160" r="6" fill="#58a6ff"/>
                    <text x="300" y="180" fill="#8b949e" text-anchor="middle" font-size="12">Start Here</text>
                    <path d="M300 160 Q 420 160 480 100" stroke="#58a6ff" stroke-width="2" stroke-dasharray="5,5" fill="none"/>
                    <path d="M300 160 Q 180 160 120 100" stroke="#58a6ff" stroke-width="2" stroke-dasharray="5,5" fill="none"/>
                    <path d="M300 160 Q 300 60 300 30" stroke="#58a6ff" stroke-width="2" stroke-dasharray="5,5" fill="none"/>
                    <circle cx="480" cy="100" r="4" fill="#238636"/>
                    <text x="480" y="120" fill="#8b949e" text-anchor="middle" font-size="10">Front</text>
                    <circle cx="120" cy="100" r="4" fill="#238636"/>
                    <text x="120" y="120" fill="#8b949e" text-anchor="middle" font-size="10">Rear</text>
                    <circle cx="300" cy="30" r="4" fill="#238636"/>
                    <text x="300" y="20" fill="#8b949e" text-anchor="middle" font-size="10">Side</text>
                    <text x="380" y="145" fill="#58a6ff" font-size="10">Walk →</text>
                    <text x="220" y="145" fill="#58a6ff" font-size="10">← Walk</text>
                </svg>
            </div>
            <p style="color:#8b949e;font-size:0.9rem;">Take 4-8 photos in a circle around the car. Start from any point and walk clockwise or counter-clockwise.</p>
        </div>
        
        <div class="card">
            <h2>📋 Step by Step</h2>
            <div class="steps">
                <div class="step">
                    <div class="step-num">1</div>
                    <div class="step-content">
                        <h3>Position the Car</h3>
                        <p>Park on a clean, level surface. Remove any debris or shadows if possible.</p>
                    </div>
                </div>
                <div class="step">
                    <div class="step-num">2</div>
                    <div class="step-content">
                        <h3>Set Up Your Phone</h3>
                        <p>Open your camera app, switch to photo mode (not portrait), and hold phone horizontally.</p>
                    </div>
                </div>
                <div class="step">
                    <div class="step-num">3</div>
                    <div class="step-content">
                        <h3>Take Exterior Photos</h3>
                        <p>Start at the front corner. Walk in a circle, stopping every 45-90 degrees to take a photo. Take 4-8 photos total.</p>
                    </div>
                </div>
                <div class="step">
                    <div class="step-num">4</div>
                    <div class="step-content">
                        <h3>Take Interior Photos (Optional)</h3>
                        <p>For interior 360, sit in the driver seat and take 4 photos pointing in each direction (front, left, back, right).</p>
                    </div>
                </div>
                <div class="step">
                    <div class="step-num">5</div>
                    <div class="step-content">
                        <h3>Upload to LotVue Pro</h3>
                        <p>Go to /capture, enter the VIN, and upload your photos. We'll create the 360 experience automatically!</p>
                    </div>
                </div>
            </div>
        </div>
        
        <div class="warning">
            <strong>iPhone Note:</strong> Go to Settings → Camera → Formats → Most Compatible to ensure photos are saved as JPG, not HEIC. LotVue Pro works best with JPG files.
        </div>
        
        <div style="text-align:center;margin-top:2rem;">
            <a href="/capture" class="btn">Start Capturing Now</a>
        </div>
    </div>
</body>
</html>"""
    return HTMLResponse(html)


# =============================================================================
# ROUTES — CAPTURE (LotVue360 — dark minimal batch upload)
# =============================================================================
@app.get("/capture")
async def capture_page():
    heic_note = "" if HEIC_SUPPORTED else '<div class="heic-warn"><strong>HEIC not supported.</strong> iPhone: Settings &rarr; Camera &rarr; Formats &rarr; Most Compatible.</div>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LotVue Pro — Upload</title>
<style>
  *{{margin:0;padding:0;box-sizing:border-box;}}
  html,body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0d1117;color:#e6edf3;min-height:100vh;}}
  .header{{display:flex;align-items:center;justify-content:space-between;padding:0.8rem 1.2rem;background:#161b22;border-bottom:1px solid #21262d;}}
  .logo{{font-size:1.1rem;font-weight:800;}} .logo span{{color:#58a6ff;}}
  .back{{color:#8b949e;text-decoration:none;font-size:0.85rem;}}
  .back:hover{{color:#e6edf3;}}
  .container{{max-width:720px;margin:0 auto;padding:1.5rem 1rem;}}
  .heic-warn{{background:#341a00;border:1px solid #3d2e00;color:#d29922;padding:0.7rem 1rem;border-radius:8px;margin-bottom:1rem;font-size:0.85rem;}}
  .card{{background:#161b22;border:1px solid #21262d;border-radius:12px;padding:1.25rem;margin-bottom:1rem;}}
  .card h2{{font-size:1.05rem;font-weight:700;margin-bottom:0.25rem;color:#e6edf3;}}
  .subtitle{{color:#8b949e;font-size:0.85rem;margin-bottom:1rem;}}
  .fg{{margin-bottom:0.8rem;}}
  .fg label{{display:block;font-size:0.78rem;color:#8b949e;margin-bottom:0.3rem;font-weight:600;}}
  .fg input{{width:100%;padding:0.55rem 0.8rem;background:#0d1117;border:1px solid #30363d;border-radius:8px;color:#e6edf3;font-size:0.95rem;outline:none;}}
  .fg input:focus{{border-color:#58a6ff;box-shadow:0 0 0 3px rgba(88,166,255,0.12);}}
  .row2{{display:grid;grid-template-columns:1fr 1fr;gap:0.8rem;}}
  .dropzone{{border:2px dashed #30363d;border-radius:12px;padding:2rem;text-align:center;cursor:pointer;transition:all .2s;}}
  .dropzone:hover,.dropzone.dragover{{border-color:#58a6ff;background:#0d1117;}}
  .dropzone .dz-icon{{font-size:2.2rem;margin-bottom:0.4rem;}}
  .dropzone .dz-text{{color:#c9d1d9;font-size:0.95rem;}}
  .dropzone .dz-hint{{color:#8b949e;font-size:0.8rem;margin-top:0.3rem;}}
  .preview-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(72px,1fr));gap:0.4rem;margin-top:0.8rem;}}
  .preview-item{{aspect-ratio:1;border-radius:6px;overflow:hidden;border:1px solid #21262d;}}
  .preview-item img{{width:100%;height:100%;object-fit:cover;}}
  .file-count{{color:#58a6ff;font-weight:700;font-size:0.9rem;margin-top:0.4rem;}}
  .progress-wrap{{margin-top:1rem;display:none;}}
  .progress-bar{{height:6px;background:#21262d;border-radius:3px;overflow:hidden;}}
  .progress-fill{{height:100%;background:linear-gradient(90deg,#238636,#3fb950);width:0%;transition:width .3s;}}
  .progress-text{{display:flex;justify-content:space-between;margin-top:0.4rem;font-size:0.85rem;color:#8b949e;}}
  .btn-row{{display:flex;gap:0.6rem;margin-top:1rem;}}
  .btn{{padding:0.55rem 1.1rem;border-radius:8px;font-weight:700;font-size:0.9rem;cursor:pointer;border:none;transition:all .15s;}}
  .btn-primary{{background:#238636;color:#fff;}} .btn-primary:hover{{background:#2ea043;}}
  .btn-primary:disabled{{opacity:0.5;cursor:not-allowed;}}
  .btn-secondary{{background:#21262d;color:#c9d1d9;border:1px solid #30363d;}}
  .btn-secondary:hover{{background:#30363d;}}
  .type-selector{{display:grid;grid-template-columns:1fr 1fr;gap:0.6rem;margin-bottom:1rem;}}
  .type-card{{border:1px solid #30363d;border-radius:10px;padding:1rem;text-align:center;cursor:pointer;transition:all .15s;}}
  .type-card:hover{{border-color:#58a6ff;}}
  .type-card.selected{{border-color:#58a6ff;background:#0d1117;}}
  .type-card .label{{font-weight:700;color:#e6edf3;font-size:0.9rem;}}
  .type-card .desc{{color:#8b949e;font-size:0.75rem;margin-top:0.15rem;}}
  @media(max-width:600px){{.row2{{grid-template-columns:1fr;}} .type-selector{{grid-template-columns:1fr;}}}}
</style>
</head>
<body>
  <div class="header">
    <div class="logo">LotVue<span>360</span></div>
    <a href="/" class="back">&larr; Dashboard</a>
  </div>
  <div class="container">
    {heic_note}

    <div class="card" id="step1">
      <h2>Upload Media</h2>
      <p class="subtitle">Select photos or a walkaround video. Photos work best.</p>
      <div class="type-selector">
        <div class="type-card selected" id="typePhotos" onclick="selectType('photos')">
          <div class="label">&#128247; Photos</div>
          <div class="desc">4+ JPG / PNG</div>
        </div>
        <div class="type-card" id="typeVideo" onclick="selectType('video')">
          <div class="label">&#127909; Video</div>
          <div class="desc">MP4 walkaround</div>
        </div>
      </div>

      <div id="photosSection">
        <div class="dropzone" id="photoDZ" onclick="document.getElementById('photoInput').click()">
          <div class="dz-icon">&#128206;</div>
          <div class="dz-text">Drop exterior photos here or click to browse</div>
          <div class="dz-hint">Minimum 4 photos · JPG / PNG · Max 30 MB each</div>
        </div>
        <input type="file" id="photoInput" accept="image/*" multiple style="display:none">
        <div id="photoCount" class="file-count"></div>
        <div class="preview-grid" id="photoPreviewGrid"></div>
      </div>

      <div id="videoSection" style="display:none;">
        <div class="dropzone" id="videoDZ" onclick="document.getElementById('videoInput').click()">
          <div class="dz-icon">&#127909;</div>
          <div class="dz-text">Drop walkaround video here or click to browse</div>
          <div class="dz-hint">MP4 / MOV — up to 500 MB</div>
        </div>
        <input type="file" id="videoInput" accept="video/*" style="display:none">
        <div id="videoCount" class="file-count"></div>
      </div>
    </div>

    <div class="card" id="step2">
      <h2>Processing</h2>
      <p class="subtitle">Ordering photos, generating frames, and building the viewer.</p>
      <div class="progress-wrap" id="progressWrap" style="display:block;">
        <div class="progress-bar"><div class="progress-fill" id="progressBar"></div></div>
        <div class="progress-text">
          <span id="progressLabel">Preparing...</span>
          <span id="progressPct">0%</span>
        </div>
      </div>
      <div class="btn-row" id="actionRow">
        <button class="btn btn-secondary" onclick="resetUpload()">Cancel</button>
        <button class="btn btn-primary" id="submitBtn" onclick="startProcess()">Create 360 Viewer</button>
      </div>
    </div>
  </div>

  <script>
    let photoFiles = [];
    let videoFile = null;
    let uploadType = 'photos';

    function selectType(t) {{
      uploadType = t;
      document.getElementById('typePhotos').classList.toggle('selected', t === 'photos');
      document.getElementById('typeVideo').classList.toggle('selected', t === 'video');
      document.getElementById('photosSection').style.display = t === 'photos' ? '' : 'none';
      document.getElementById('videoSection').style.display = t === 'video' ? '' : 'none';
    }}

    function setupDZ(dzId, inputId, onFiles) {{
      const dz = document.getElementById(dzId);
      dz.addEventListener('dragover', e => {{ e.preventDefault(); dz.classList.add('dragover'); }});
      dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
      dz.addEventListener('drop', e => {{ e.preventDefault(); dz.classList.remove('dragover'); onFiles(Array.from(e.dataTransfer.files)); }});
      document.getElementById(inputId).addEventListener('change', e => onFiles(Array.from(e.target.files)));
    }}

    function showPreviews(files, gridId, countId, label) {{
      const grid = document.getElementById(gridId);
      grid.innerHTML = '';
      files.forEach(f => {{
        if (!f.type.startsWith('image/')) return;
        const div = document.createElement('div'); div.className = 'preview-item';
        const img = document.createElement('img'); img.src = URL.createObjectURL(f);
        div.appendChild(img); grid.appendChild(div);
      }});
      document.getElementById(countId).textContent = files.length + ' ' + label + ' selected';
    }}

    setupDZ('photoDZ', 'photoInput', files => {{
      photoFiles = files.filter(f => f.type.startsWith('image/'));
      showPreviews(photoFiles, 'photoPreviewGrid', 'photoCount', 'photo(s)');
    }});
    setupDZ('videoDZ', 'videoInput', files => {{
      const f = files.find(f => f.type.startsWith('video/'));
      if (f) {{ videoFile = f; document.getElementById('videoCount').textContent = f.name + ' (' + (f.size/1024/1024).toFixed(1) + ' MB)'; }}
    }});

    function setProgress(pct, label) {{
      document.getElementById('progressBar').style.width = pct + '%';
      document.getElementById('progressLabel').textContent = label;
      document.getElementById('progressPct').textContent = pct + '%';
    }}

    function resetUpload() {{
      photoFiles = []; videoFile = null;
      document.getElementById('photoInput').value = '';
      document.getElementById('videoInput').value = '';
      document.getElementById('photoCount').textContent = '';
      document.getElementById('videoCount').textContent = '';
      document.getElementById('photoPreviewGrid').innerHTML = '';
      setProgress(0, 'Preparing...');
      document.getElementById('submitBtn').disabled = false;
    }}

    async function startProcess() {{
      const hasPhotos = uploadType === 'photos' && photoFiles.length >= 4;
      const hasVideo = uploadType === 'video' && videoFile;
      if (!hasPhotos && !hasVideo) {{
        alert('Please select at least 4 photos or a video.');
        return;
      }}

      const btn = document.getElementById('submitBtn');
      btn.disabled = true;

      try {{
        let jobId = '';
        let vin = '';

        if (hasPhotos) {{
          setProgress(10, 'Uploading photos...');
          const fd = new FormData();
          photoFiles.forEach(f => fd.append('photos', f));
          const r = await fetch('/api/upload', {{ method: 'POST', body: fd }});
          if (!r.ok) throw new Error((await r.json()).detail || 'Upload failed');
          const d = await r.json();
          jobId = d.job_id; vin = d.vin;
        }} else {{
          setProgress(10, 'Uploading video...');
          const fd = new FormData();
          fd.append('video', videoFile);
          const r = await fetch('/api/upload', {{ method: 'POST', body: fd }});
          if (!r.ok) throw new Error((await r.json()).detail || 'Upload failed');
          const d = await r.json();
          jobId = d.job_id; vin = d.vin;
        }}

        setProgress(40, 'Ordering photos...');
        const r1 = await fetch('/api/process/' + jobId + '/order', {{ method: 'POST' }});
        if (!r1.ok) throw new Error('Ordering failed');

        setProgress(70, 'Generating frames...');
        const r2 = await fetch('/api/process/' + jobId + '/interpolate', {{ method: 'POST' }});
        if (!r2.ok) throw new Error('Interpolation failed');

        setProgress(100, 'Building viewer...');
        setTimeout(() => window.location.href = '/viewer/' + jobId, 400);
      }} catch(e) {{
        alert('Error: ' + e.message);
        btn.disabled = false;
      }}
    }}
  </script>
</body>
</html>"""
    return HTMLResponse(html)


# =============================================================================
# ANGLE ESTIMATION + ORDERING
# =============================================================================
def estimate_angles_orb(raw_dir: Path) -> tuple[list[dict], float]:
    """
    Estimate relative camera angles for a circular capture using ORB feature matching.
    Returns ordered_frames list and average confidence.
    """
    frames = sorted(raw_dir.glob("frame_*.jpg"))
    if not frames:
        return [], 0.0

    n = len(frames)
    if n < 2:
        return [{"file": f.name, "estimated_angle": 0, "confidence": 0.5} for f in frames], 0.5

    orb = cv2.ORB_create(nfeatures=2000)
    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)

    kps_desc = []
    for f in frames:
        img = cv2.imread(str(f), cv2.IMREAD_GRAYSCALE)
        if img is None:
            kp, des = [], None
        else:
            kp, des = orb.detectAndCompute(img, None)
        kps_desc.append((kp, des))

    relative_shifts = []
    match_counts = []

    for i in range(n):
        kp1, des1 = kps_desc[i]
        kp2, des2 = kps_desc[(i + 1) % n]
        if des1 is None or des2 is None or len(kp1) < 10 or len(kp2) < 10:
            relative_shifts.append(0.0)
            match_counts.append(0)
            continue

        matches = bf.knnMatch(des1, des2, k=2)
        good = []
        for m_n in matches:
            if len(m_n) == 2 and m_n[0].distance < 0.75 * m_n[1].distance:
                good.append(m_n[0])

        if len(good) < 10:
            relative_shifts.append(0.0)
            match_counts.append(len(good))
            continue

        dxs = []
        for m in good:
            x1 = kp1[m.queryIdx].pt[0]
            x2 = kp2[m.trainIdx].pt[0]
            dxs.append(x2 - x1)

        median_dx = sorted(dxs)[len(dxs) // 2]
        # Normalize by image width (assume all same size, use first frame)
        img_w = cv2.imread(str(frames[0])).shape[1]
        shift_norm = median_dx / img_w
        relative_shifts.append(shift_norm)
        match_counts.append(len(good))

    # Integrate shifts into absolute angles
    abs_angles = [0.0]
    for shift in relative_shifts[:-1]:
        abs_angles.append(abs_angles[-1] + shift)

    # Normalize to 0-360 (circular assumption: total = 360)
    total_span = abs_angles[-1] + relative_shifts[-1]
    if abs(total_span) < 1e-6:
        total_span = 1.0

    normalized = [(a / total_span) * 360.0 % 360.0 for a in abs_angles]

    # Confidence per frame based on match count
    max_matches = max(match_counts) if match_counts else 1
    confidences = [min(1.0, c / 50.0) for c in match_counts]
    avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0

    # Build result
    result = []
    for f, angle, conf in zip(frames, normalized, confidences):
        result.append({"file": f.name, "estimated_angle": round(angle, 2), "confidence": round(conf, 2)})

    # Sort by angle
    result.sort(key=lambda x: x["estimated_angle"])
    return result, round(avg_confidence, 2)


def build_manifest(job_id: str, vin: str, ordered_frames: list[dict]) -> dict:
    n = len(ordered_frames)
    gaps = []
    for i in range(n):
        a1 = ordered_frames[i]["estimated_angle"]
        a2 = ordered_frames[(i + 1) % n]["estimated_angle"]
        if i == n - 1:
            deg = (360.0 - a1) + a2
        else:
            deg = a2 - a1
        gaps.append({"from": a1, "to": a2, "degrees": round(deg, 2)})

    return {
        "job_id": job_id,
        "vin": vin,
        "ordered_frames": ordered_frames,
        "total_input": n,
        "angle_coverage": 360,
        "gaps": gaps,
    }


@app.post("/api/process/{job_id}/interpolate")
async def process_interpolate(job_id: str):
    vehicle = db_get_vehicle_by_job_id(job_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Job not found")

    vin = vehicle["vin"]
    exterior_dir = STORAGE_DIR / vin / "exterior"
    spin_dir = STORAGE_DIR / vin / "spin"
    manifest_path = STORAGE_DIR / vin / "manifest.json"

    if not exterior_dir.exists():
        raise HTTPException(status_code=400, detail="No ordered exterior frames found")

    manifest = None
    if manifest_path.exists():
        with open(manifest_path, "r") as f:
            manifest = json.load(f)

    spin_dir.mkdir(exist_ok=True)
    for old in spin_dir.glob("*.jpg"):
        old.unlink()

    actual = interpolate_frames(exterior_dir, spin_dir, target_frames=36, manifest=manifest, use_optical_flow=True)

    # Generate thumbnail
    first_spin = sorted(spin_dir.glob("spin_*.jpg"))
    if first_spin:
        generate_thumbnail(spin_dir, STORAGE_DIR / vin / "thumbnail.jpg")

    db_update_counts(vin)

    return {
        "status": "interpolated",
        "job_id": job_id,
        "vin": vin,
        "spin_frames": actual,
    }


@app.post("/api/process/{job_id}/order")
async def process_order(job_id: str):
    vehicle = db_get_vehicle_by_job_id(job_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Job not found")

    vin = vehicle["vin"]
    raw_dir = STORAGE_DIR / vin / "raw"
    exterior_dir = STORAGE_DIR / vin / "exterior"
    manifest_path = STORAGE_DIR / vin / "manifest.json"

    # VIDEO path: /api/upload extracts frames straight into exterior/ (already
    # ordered + background-processed) and leaves raw/ empty. The photo-mode logic
    # below WIPES exterior and recopies from raw — which would destroy the video's
    # frames (the "No frames found" bug). So if raw/ is empty but exterior already
    # has frames, just order the existing exterior frames by filename, keep them
    # in place, and let interpolate build the spin from them.
    raw_frames = sorted(raw_dir.glob("frame_*.jpg")) if raw_dir.exists() else []
    pre_extracted = sorted(exterior_dir.glob("*.jpg")) if exterior_dir.exists() else []
    if not raw_frames and pre_extracted:
        ordered_frames = [
            {"file": f.name, "estimated_angle": round(i * 360.0 / len(pre_extracted), 2),
             "confidence": 0.1}
            for i, f in enumerate(pre_extracted)
        ]
        manifest = build_manifest(job_id, vin, ordered_frames)
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)
        return {"status": "ordered", "job_id": job_id, "vin": vin,
                "confidence": 0.1, "source": "video", "frames": len(pre_extracted)}

    if not raw_dir.exists() or not raw_frames:
        raise HTTPException(status_code=400, detail="No raw photos found")

    ordered_frames, confidence = estimate_angles_orb(raw_dir)

    # Fallback to filename order if low confidence
    if confidence < 0.25 or len(ordered_frames) < 4:
        frames = sorted(raw_dir.glob("frame_*.jpg"))
        n = len(frames)
        step = 360.0 / max(n, 1)
        ordered_frames = [
            {"file": f.name, "estimated_angle": round(i * step, 2), "confidence": 0.1}
            for i, f in enumerate(frames)
        ]

    manifest = build_manifest(job_id, vin, ordered_frames)
    manifest_path = STORAGE_DIR / vin / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    # Also copy ordered frames to exterior/ for downstream processing
    exterior_dir = STORAGE_DIR / vin / "exterior"
    exterior_dir.mkdir(exist_ok=True)
    for old in exterior_dir.glob("*.jpg"):
        old.unlink()
    for i, entry in enumerate(ordered_frames):
        src = raw_dir / entry["file"]
        dst = exterior_dir / f"frame_{i+1:04d}.jpg"
        shutil.copy(src, dst)

    return {"status": "ordered", "job_id": job_id, "vin": vin, "confidence": confidence, "manifest_path": str(manifest_path)}


# =============================================================================
# API — CREATE VEHICLE (for photo-only mode, no video)
# =============================================================================
@app.post("/api/create-vehicle")
async def create_vehicle(
    vin: str = Form(...),
    year: str = Form(""),
    make: str = Form(""),
    model: str = Form(""),
    color: str = Form(""),
    price: str = Form(""),
    mileage: str = Form(""),
):
    vin = validate_vin(vin)

    vehicle_dir = STORAGE_DIR / vin
    exterior_dir = vehicle_dir / "exterior"
    interior_dir = vehicle_dir / "interior"
    vehicle_dir.mkdir(parents=True, exist_ok=True)
    exterior_dir.mkdir(exist_ok=True)
    interior_dir.mkdir(exist_ok=True)

    # Clear any old exterior frames if re-uploading
    for old in exterior_dir.glob("*.jpg"):
        old.unlink()

    # Auto-decode VIN from NHTSA if year/make/model not provided
    decoded = {}
    if not year or not make or not model:
        try:
            import urllib.request
            url = f"https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{vin}?format=json"
            with urllib.request.urlopen(url, timeout=8) as resp:
                data = json.loads(resp.read().decode())
            if data.get("Results"):
                r = data["Results"][0]
                def _clean(val):
                    return val.strip() if val and val.strip() not in ["", "0", "Not Applicable"] else ""
                decoded = {
                    "year": _clean(r.get("ModelYear", "")),
                    "make": _clean(r.get("Make", "")),
                    "model": _clean(r.get("Model", "")),
                    "trim_level": _clean(r.get("Trim", "")),
                    "body_style": _clean(r.get("BodyClass", "")),
                    "engine": (_clean(r.get("DisplacementL", "")) + ("L " if _clean(r.get("DisplacementL", "")) else "") + _clean(r.get("EngineCylinders", "")) + (" cyl" if _clean(r.get("EngineCylinders", "")) else "")).strip(),
                    "transmission": _clean(r.get("TransmissionStyle", "")),
                    "drivetrain": _clean(r.get("DriveType", "")),
                    "fuel_type": _clean(r.get("FuelTypePrimary", "")),
                }
                if not year: year = decoded.get("year", "")
                if not make: make = decoded.get("make", "")
                if not model: model = decoded.get("model", "")
        except Exception as e:
            print(f"[VIN Auto-Decode] Error for {vin}: {e}")

    db_upsert_vehicle(
        vin, year, make, model, 0, 0,
        trim_level=decoded.get("trim_level", ""),
        engine=decoded.get("engine", ""),
        transmission=decoded.get("transmission", ""),
        drivetrain=decoded.get("drivetrain", ""),
        fuel_type=decoded.get("fuel_type", ""),
        body_style=decoded.get("body_style", ""),
    )

    # Set initial workflow status + color/price/mileage
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE vehicles SET workflow_status='intake', color=?, price=?, mileage=? WHERE vin=?",
                 (color, price, mileage, vin))
    conn.commit()
    conn.close()

    return {"status": "ok", "vin": vin, "decoded": decoded, "workflow_status": "intake"}


# =============================================================================
# API — UPLOAD EXTERIOR PHOTO (ordered, for photo-based 360)
# =============================================================================
@app.post("/api/upload/exterior")
async def upload_exterior_photo(
    vin: str = Form(""),
    photo: UploadFile = File(...),
    index: str = Form("0"),
):
    # Auto-generate VIN if not provided
    if not vin or len(vin) < 17:
        import uuid
        vin = str(uuid.uuid4())[:17].upper().replace("-", "0")
    
    vin = validate_vin(vin)
    vehicle_dir = STORAGE_DIR / vin
    
    # Auto-create vehicle directory if not exists
    if not vehicle_dir.exists():
        vehicle_dir.mkdir(parents=True, exist_ok=True)
        (vehicle_dir / "exterior").mkdir(exist_ok=True)
        (vehicle_dir / "interior").mkdir(exist_ok=True)
        db_upsert_vehicle(vin, "", "", "", 0, 0)
    
    exterior_dir = vehicle_dir / "exterior"
    exterior_dir.mkdir(exist_ok=True)
    
    raw = await photo.read()
    
    if len(raw) > MAX_PHOTO_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"Photo too large (max {MAX_PHOTO_MB}MB)")
    
    # Use index for ordering — frame_0001.jpg, frame_0002.jpg, etc.
    idx = int(index) + 1  # 0-indexed from frontend → 1-indexed filename
    photo_path = exterior_dir / f"frame_{idx:04d}.jpg"
    
    # Process with background removal
    showroom = get_dealer_showroom()
    process_exterior_photo(raw, photo_path, showroom)
    
    # Update thumbnail from first frame
    if idx == 1:
        generate_thumbnail(exterior_dir, vehicle_dir / "thumbnail.jpg")
    
    # Update counts
    db_update_counts(vin)
    
    return {"status": "ok", "vin": vin, "filename": photo_path.name, "index": idx}


# =============================================================================
# VIEWER — STATIC FRAME SERVING
# =============================================================================
@app.get("/static/{job_id}/frames/{filename}")
async def serve_frame(job_id: str, filename: str):
    vehicle = db_get_vehicle_by_job_id(job_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Job not found")
    vin = vehicle["vin"]
    frame_path = STORAGE_DIR / vin / "spin" / filename
    if not frame_path.exists():
        raise HTTPException(status_code=404, detail="Frame not found")
    return FileResponse(frame_path)


# =============================================================================
# VIEWER — INTERACTIVE 360 PAGE
# =============================================================================
@app.get("/viewer/{job_id}")
async def viewer_page(job_id: str, request: Request):
    vehicle = db_get_vehicle_by_job_id(job_id)
    if not vehicle:
        return HTMLResponse("<h1>Viewer not found</h1>", status_code=404)

    vin = vehicle["vin"]
    spin_dir = STORAGE_DIR / vin / "spin"
    if not spin_dir.exists():
        return HTMLResponse("<h1>Frames not ready</h1>", status_code=404)

    frames = sorted([f.name for f in spin_dir.glob("spin_*.jpg")])
    if not frames:
        return HTMLResponse("<h1>No frames found</h1>", status_code=404)

    frame_urls = [f"/static/{job_id}/frames/{f}" for f in frames]
    total = len(frame_urls)
    frame_urls_json = json.dumps(frame_urls)
    share_url = f"{str(request.base_url).rstrip('/')}/viewer/{job_id}"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>LotVue Pro — Viewer</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  html, body {{ width:100%; height:100%; overflow:hidden; background:#0d1117; color:#e6edf3; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; }}
  .chrome {{ position:fixed; inset:0; display:flex; flex-direction:column; pointer-events:none; }}
  .chrome > * {{ pointer-events:auto; }}
  .header {{ display:flex; align-items:center; justify-content:space-between; padding:0.6rem 1rem; background:rgba(13,17,23,0.85); backdrop-filter:blur(8px); border-bottom:1px solid #21262d; }}
  .logo {{ font-weight:800; font-size:1.05rem; color:#fff; letter-spacing:-0.3px; }}
  .logo span {{ color:#58a6ff; }}
  .viewer {{ flex:1; position:relative; background:#f5f7fa; display:flex; align-items:center; justify-content:center; overflow:hidden; }}
  .viewer img {{ position:absolute; max-width:100%; max-height:100%; object-fit:contain; display:none; user-select:none; -webkit-user-drag:none; }}
  .viewer img.active {{ display:block; }}
  .loader {{ position:absolute; inset:0; display:flex; flex-direction:column; align-items:center; justify-content:center; background:#f5f7fa; color:#1a2332; z-index:50; transition:opacity .3s; }}
  .loader.hidden {{ opacity:0; pointer-events:none; }}
  .loader-bar {{ width:220px; height:6px; background:#d0d7de; border-radius:3px; margin-top:1rem; overflow:hidden; }}
  .loader-fill {{ height:100%; background:#238636; width:0%; transition:width .2s; }}
  .controls {{ position:absolute; bottom:0; left:0; right:0; display:flex; align-items:center; justify-content:center; gap:0.6rem; padding:0.7rem 1rem; background:linear-gradient(to top, rgba(13,17,23,0.9), rgba(13,17,23,0)); }}
  .btn {{ background:rgba(33,38,45,0.9); border:1px solid #30363d; color:#e6edf3; padding:0.45rem 0.9rem; border-radius:6px; font-size:0.85rem; cursor:pointer; transition:all .15s; }}
  .btn:hover {{ background:#30363d; }}
  .btn.active {{ background:#238636; border-color:#2ea043; }}
  .hint {{ position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); background:rgba(0,0,0,0.6); color:#fff; padding:0.5rem 1rem; border-radius:20px; font-size:0.85rem; opacity:0; transition:opacity .4s; pointer-events:none; z-index:10; }}
  .hint.show {{ opacity:1; }}
  @media(max-width:600px){{ .logo{{font-size:0.95rem;}} .btn{{padding:0.4rem 0.7rem;font-size:0.8rem;}} }}
</style>
</head>
<body>
<div class="chrome">
  <div class="header">
    <div class="logo">LotVue<span>360</span></div>
    <button class="btn" onclick="copyShare()">&#128279; Share</button>
  </div>
  <div class="viewer" id="viewer">
    <div class="loader" id="loader">
      <div>Loading frames…</div>
      <div class="loader-bar"><div class="loader-fill" id="loaderFill"></div></div>
      <div style="margin-top:0.5rem;font-size:0.8rem;color:#57606a;" id="loaderText">0 / {total}</div>
    </div>
    <div class="hint" id="hint">&#8592; Drag to rotate &#8594;</div>
  </div>
  <div class="controls">
    <button class="btn" id="autoBtn" onclick="toggleAuto()">&#9654; Play</button>
    <button class="btn" onclick="toggleFullscreen()">&#x26F6; Fullscreen</button>
  </div>
</div>
<script>
const FRAME_URLS = {frame_urls_json};
const viewer = document.getElementById('viewer');
const loader = document.getElementById('loader');
const loaderFill = document.getElementById('loaderFill');
const loaderText = document.getElementById('loaderText');
const hint = document.getElementById('hint');
const autoBtn = document.getElementById('autoBtn');

let frames = [];
let loaded = 0;
let cur = 0;
let dragging = false;
let startX = 0;
let startFrame = 0;
let velocity = 0;
let lastX = 0;
let lastTime = 0;
let autoInt = null;
let momentumInt = null;

function show(idx) {{
  idx = ((idx % FRAME_URLS.length) + FRAME_URLS.length) % FRAME_URLS.length;
  frames.forEach((img, i) => img.classList.toggle('active', i === idx));
  cur = idx;
}}

function preload() {{
  FRAME_URLS.forEach((url, i) => {{
    const img = document.createElement('img');
    img.src = url;
    img.alt = '';
    img.onload = () => {{
      loaded++;
      loaderFill.style.width = Math.round((loaded / FRAME_URLS.length) * 100) + '%';
      loaderText.textContent = loaded + ' / ' + FRAME_URLS.length;
      if (loaded === FRAME_URLS.length) {{
        loader.classList.add('hidden');
        hint.classList.add('show');
        setTimeout(() => hint.classList.remove('show'), 2500);
        startAuto();
      }}
    }};
    img.onerror = () => {{
      loaded++;
      if (loaded === FRAME_URLS.length) loader.classList.add('hidden');
    }};
    viewer.appendChild(img);
    frames.push(img);
  }});
  if (frames.length) frames[0].classList.add('active');
}}

function startAuto() {{
  if (autoInt) return;
  autoBtn.innerHTML = '&#9209; Pause';
  autoBtn.classList.add('active');
  autoInt = setInterval(() => {{
    cur = (cur + 1) % FRAME_URLS.length;
    show(cur);
  }}, 60);
}}

function stopAuto() {{
  if (!autoInt) return;
  clearInterval(autoInt);
  autoInt = null;
  autoBtn.innerHTML = '&#9654; Play';
  autoBtn.classList.remove('active');
}}

function toggleAuto() {{
  if (autoInt) stopAuto(); else startAuto();
}}

function toggleFullscreen() {{
  if (!document.fullscreenElement) document.documentElement.requestFullscreen().catch(()=>{{}});
  else document.exitFullscreen().catch(()=>{{}});
}}

function copyShare() {{
  navigator.clipboard.writeText('{share_url}').then(() => alert('Link copied to clipboard'));
}}

viewer.addEventListener('mousedown', e => {{
  stopAuto();
  if (momentumInt) {{ clearInterval(momentumInt); momentumInt = null; }}
  dragging = true;
  startX = e.clientX;
  startFrame = cur;
  velocity = 0; lastX = e.clientX; lastTime = Date.now();
}});

viewer.addEventListener('mousemove', e => {{
  if (!dragging) return;
  const dx = e.clientX - startX;
  velocity = (e.clientX - lastX) / (Date.now() - lastTime || 1) * 16;
  lastX = e.clientX; lastTime = Date.now();
  const fd = Math.round(dx / (viewer.clientWidth / FRAME_URLS.length));
  const n = ((startFrame - fd) % FRAME_URLS.length + FRAME_URLS.length) % FRAME_URLS.length;
  show(n);
}});

viewer.addEventListener('mouseup', () => {{
  dragging = false;
  if (Math.abs(velocity) > 0.3) {{
    momentumInt = setInterval(() => {{
      velocity *= 0.93;
      if (Math.abs(velocity) < 0.1) {{ clearInterval(momentumInt); momentumInt = null; return; }}
      cur = ((cur - Math.sign(velocity)) % FRAME_URLS.length + FRAME_URLS.length) % FRAME_URLS.length;
      show(cur);
    }}, 16);
  }}
}});
viewer.addEventListener('mouseleave', () => {{ dragging = false; }});

viewer.addEventListener('touchstart', e => {{
  stopAuto();
  if (momentumInt) {{ clearInterval(momentumInt); momentumInt = null; }}
  dragging = true;
  startX = e.touches[0].clientX;
  startFrame = cur;
  velocity = 0; lastX = e.touches[0].clientX; lastTime = Date.now();
}}, {{passive:true}});

viewer.addEventListener('touchmove', e => {{
  if (!dragging) return;
  e.preventDefault();
  const dx = e.touches[0].clientX - startX;
  velocity = (e.touches[0].clientX - lastX) / (Date.now() - lastTime || 1) * 16;
  lastX = e.touches[0].clientX; lastTime = Date.now();
  const fd = Math.round(dx / (viewer.clientWidth / FRAME_URLS.length));
  const n = ((startFrame - fd) % FRAME_URLS.length + FRAME_URLS.length) % FRAME_URLS.length;
  show(n);
}}, {{passive:false}});

viewer.addEventListener('touchend', () => {{
  dragging = false;
  if (Math.abs(velocity) > 0.3) {{
    momentumInt = setInterval(() => {{
      velocity *= 0.93;
      if (Math.abs(velocity) < 0.1) {{ clearInterval(momentumInt); momentumInt = null; return; }}
      cur = ((cur - Math.sign(velocity)) % FRAME_URLS.length + FRAME_URLS.length) % FRAME_URLS.length;
      show(cur);
    }}, 16);
  }}
}});

preload();
</script>
</body>
</html>"""
    return HTMLResponse(html)


# =============================================================================
# API — UPLOAD MEDIA (video or batch photos)
# =============================================================================
@app.post("/api/upload")
async def upload_media(
    vin: str = Form(""),
    year: str = Form(""),
    make: str = Form(""),
    model: str = Form(""),
    color: str = Form(""),
    video: Optional[UploadFile] = File(None),
    photos: Optional[List[UploadFile]] = File(None),
):
    import uuid
    job_id = str(uuid.uuid4())

    # Auto-generate VIN if not provided
    if not vin or len(vin) < 17:
        vin = f"LV360{job_id.replace('-','')[:12].upper()}"

    vin = validate_vin(vin)

    vehicle_dir = STORAGE_DIR / vin
    raw_dir = vehicle_dir / "raw"
    exterior_dir = vehicle_dir / "exterior"
    interior_dir = vehicle_dir / "interior"
    vehicle_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(exist_ok=True)
    exterior_dir.mkdir(exist_ok=True)
    interior_dir.mkdir(exist_ok=True)

    # ── Photo batch branch ───────────────────────────────────────────────────
    if photos and len(photos) > 0:
        if len(photos) < 4:
            raise HTTPException(status_code=400, detail="Upload at least 4 photos")
        if len(photos) > 72:
            raise HTTPException(status_code=400, detail="Max 72 photos allowed")

        valid_exts = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
        photo_count = 0

        for i, photo in enumerate(photos):
            ext = Path(photo.filename).suffix.lower()
            if ext not in valid_exts:
                raise HTTPException(status_code=400, detail=f"Invalid file type: {photo.filename}")

            raw_bytes = await photo.read()
            if len(raw_bytes) > MAX_PHOTO_MB * 1024 * 1024:
                raise HTTPException(status_code=413, detail=f"Photo too large (max {MAX_PHOTO_MB}MB)")

            try:
                img = Image.open(io.BytesIO(raw_bytes))
                img = img.convert("RGB")
                w, h = img.size
                if w < 800 and h < 800:
                    raise HTTPException(status_code=400, detail=f"{photo.filename} resolution too small (min 800px)")
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Cannot process {photo.filename}: {e}")

            # Save original to raw/
            save_path = raw_dir / f"frame_{i+1:04d}.jpg"
            img.save(save_path, "JPEG", quality=95)
            photo_count += 1

        db_upsert_vehicle(vin, year, make, model, photo_count, 0, job_id=job_id)

        return {"job_id": job_id, "photo_count": photo_count, "status": "uploaded", "vin": vin}

    # ── Video branch ─────────────────────────────────────────────────────────
    if not video:
        raise HTTPException(status_code=400, detail="No video or photos provided")

    video_path = vehicle_dir / "video_temp.mp4"
    content = await video.read()

    if len(content) > MAX_VIDEO_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"Video too large (max {MAX_VIDEO_MB}MB)")

    with open(video_path, "wb") as f:
        f.write(content)

    try:
        num_extracted = extract_frames(video_path, exterior_dir, NUM_EXTERIOR_FRAMES)
    except Exception as e:
        if video_path.exists():
            video_path.unlink()
        raise HTTPException(status_code=500, detail=f"Frame extraction failed: {str(e)}")

    if video_path.exists():
        video_path.unlink()

    generate_thumbnail(exterior_dir, vehicle_dir / "thumbnail.jpg")
    db_upsert_vehicle(vin, year, make, model, num_extracted, 0, job_id=job_id)

    return {"job_id": job_id, "vin": vin, "frames_extracted": num_extracted, "status": "uploaded"}


# =============================================================================
# API — UPLOAD INTERIOR PHOTO
# =============================================================================
@app.post("/api/upload/interior")
async def upload_interior(
    vin: str = Form(""),
    photo: UploadFile = File(...)
):
    # Auto-generate VIN if not provided
    if not vin or len(vin) < 17:
        import uuid
        vin = str(uuid.uuid4())[:17].upper().replace("-", "0")
    
    vin = validate_vin(vin)
    vehicle_dir = STORAGE_DIR / vin
    
    # Auto-create vehicle directory if not exists
    if not vehicle_dir.exists():
        vehicle_dir.mkdir(parents=True, exist_ok=True)
        (vehicle_dir / "exterior").mkdir(exist_ok=True)
        (vehicle_dir / "interior").mkdir(exist_ok=True)
        db_upsert_vehicle(vin, "", "", "", 0, 0)
    
    interior_dir = vehicle_dir / "interior"
    interior_dir.mkdir(exist_ok=True)
    
    raw = await photo.read()
    
    if len(raw) > MAX_PHOTO_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"Photo too large (max {MAX_PHOTO_MB}MB)")
    
    existing = len(list(interior_dir.glob("*.jpg")))
    photo_path = interior_dir / f"interior_{existing+1:03d}.jpg"
    
    # Just save the photo as-is (no processing)
    if not raw or len(raw) < 100:
        raise HTTPException(status_code=400, detail="Empty or invalid image file")
    img = Image.open(io.BytesIO(raw))
    img = img.convert("RGB")
    img.save(photo_path, "JPEG", quality=92)
    
    # Update counts
    db_update_counts(vin)
    
    return {"status": "ok", "filename": photo_path.name}


# =============================================================================
# PHASE 2A: UPLOAD ENDPOINTS — PANORAMA, GALLERY, VIDEO
# =============================================================================

@app.post("/api/upload/panorama")
async def upload_panorama(
    vin: str = Form(""),
    scene_name: str = Form("Front Seat"),
    photo: UploadFile = File(...)
):
    """Upload an equirectangular interior panorama for Pannellum viewer."""
    if not vin or len(vin) < 5:
        import uuid
        vin = str(uuid.uuid4())[:17].upper().replace("-", "0")
    vin = validate_vin(vin)
    vehicle_dir = STORAGE_DIR / vin
    pano_dir = vehicle_dir / "panoramas"
    pano_dir.mkdir(parents=True, exist_ok=True)

    raw = await photo.read()
    if len(raw) > 100 * 1024 * 1024:  # 100MB max for panoramas
        raise HTTPException(status_code=413, detail="Panorama too large (max 100MB)")

    existing = len(list(pano_dir.glob("*.jpg")) + list(pano_dir.glob("*.jpeg")) + list(pano_dir.glob("*.png")))
    pano_path = pano_dir / f"pano_{existing+1:03d}.jpg"

    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        # Validate 2:1 aspect ratio (equirectangular requirement)
        w, h = img.size
        if w > 4096:
            img = img.resize((4096, int(4096 * h / w)), Image.LANCZOS)
        img.save(pano_path, "JPEG", quality=92)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid image: {e}")

    # Register in DB
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        INSERT INTO interior_panoramas (vin, file_path, scene_name, is_equirectangular, sort_order, created_at)
        VALUES (?, ?, ?, 1, ?, ?)
    """, (vin, str(pano_path), scene_name, existing + 1, datetime.now().isoformat()))
    conn.execute("UPDATE vehicles SET show_interior=1 WHERE vin=?", (vin,))
    conn.commit()
    conn.close()

    return {"status": "ok", "filename": pano_path.name, "vin": vin, "scene_name": scene_name}


@app.post("/api/upload/gallery")
async def upload_gallery_photo(
    vin: str = Form(""),
    category: str = Form("exterior"),
    caption: str = Form(""),
    photo: UploadFile = File(...)
):
    """Upload a gallery photo (shown in Gallery tab)."""
    if not vin or len(vin) < 5:
        import uuid
        vin = str(uuid.uuid4())[:17].upper().replace("-", "0")
    vin = validate_vin(vin)
    vehicle_dir = STORAGE_DIR / vin
    gallery_dir = vehicle_dir / "gallery"
    gallery_dir.mkdir(parents=True, exist_ok=True)

    raw = await photo.read()
    if len(raw) > MAX_PHOTO_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"Photo too large (max {MAX_PHOTO_MB}MB)")

    existing = len(list(gallery_dir.glob("*.jpg")))
    photo_path = gallery_dir / f"gallery_{existing+1:03d}.jpg"

    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        # Auto-orient from EXIF
        try:
            from PIL import ImageOps
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass
        # Resize to max 2000px wide
        if img.width > 2000:
            ratio = 2000 / img.width
            img = img.resize((2000, int(img.height * ratio)), Image.LANCZOS)
        img.save(photo_path, "JPEG", quality=90)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid image: {e}")

    # Register in DB
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        INSERT INTO gallery_photos (vin, file_path, category, caption, sort_order, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (vin, str(photo_path), category, caption, existing + 1, datetime.now().isoformat()))
    conn.execute("UPDATE vehicles SET show_gallery=1 WHERE vin=?", (vin,))
    conn.commit()
    conn.close()

    return {"status": "ok", "filename": photo_path.name, "vin": vin, "category": category}


@app.post("/api/upload/video-url")
async def set_vehicle_video_url(
    vin: str = Form(""),
    video_url: str = Form(""),
    video_type: str = Form("youtube"),
):
    """Set a YouTube URL or direct MP4 URL as the vehicle video."""
    if not vin:
        raise HTTPException(status_code=400, detail="VIN required")
    vin = validate_vin(vin)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "UPDATE vehicles SET video_url=?, video_type=?, show_video=1 WHERE vin=?",
        (video_url, video_type, vin)
    )
    conn.commit()
    conn.close()
    return {"status": "ok", "vin": vin, "video_url": video_url, "video_type": video_type}


# =============================================================================
# API — FINALIZE (run interpolation + background removal)
# =============================================================================
@app.post("/api/finalize/{vin}")
async def finalize_vehicle(vin: str):
    """Run frame interpolation after all photos uploaded."""
    vin = vin.upper()
    vehicle_dir = STORAGE_DIR / vin
    
    if not vehicle_dir.exists():
        raise HTTPException(status_code=404, detail="Vehicle not found")
    
    exterior_dir = vehicle_dir / "exterior"
    spin_dir = vehicle_dir / "spin"
    
    if not exterior_dir.exists():
        raise HTTPException(status_code=400, detail="No exterior photos found")
    
    # Count exterior frames
    frame_count = len(list(exterior_dir.glob("frame_*.jpg")))
    
    if frame_count == 0:
        raise HTTPException(status_code=400, detail="No exterior frames found")
    
    spin_dir.mkdir(exist_ok=True)
    for old in spin_dir.glob("*.jpg"):
        old.unlink()

    def _finalize_work():
        """Everything heavy runs off the event loop: interpolation, thumbnail,
        DB counts, then the rembg/turntable chain. The POST returns instantly
        (Cloudflare cuts blocked responses at ~100s)."""
        marker = spin_dir / ".processing"
        try:
            marker.write_text(datetime.now().isoformat())
        except OSError:
            pass
        try:
            all_frames = sorted(exterior_dir.glob("frame_*.jpg"))
            # Smart interpolation: if < 18 frames, interpolate up to 36; otherwise copy as-is
            if frame_count < 18:
                interpolate_frames(exterior_dir, spin_dir, target_frames=36)
            else:
                for i, f in enumerate(all_frames):
                    shutil.copy(f, spin_dir / f"spin_{i+1:04d}.jpg")

            if sorted(spin_dir.glob("spin_*.jpg")):
                generate_thumbnail(spin_dir, vehicle_dir / "thumbnail.jpg")
            db_update_counts(vin)

            vehicle = db_get_vehicle(vin)
            wm_mode = vehicle.get("watermark_mode", "preview") if vehicle else "preview"
            turntable_style = vehicle.get("turntable_style", "studio_shadow") if vehicle else "studio_shadow"
            background_slug = vehicle.get("background_id", "") if vehicle else ""

            bg_path = None
            if background_slug and background_slug != "none":
                conn = sqlite3.connect(str(DB_PATH))
                bg_row = conn.execute("SELECT url FROM backgrounds WHERE slug=?", (background_slug,)).fetchone()
                conn.close()
                if bg_row and bg_row[0]:
                    bg_url = bg_row[0]
                    if bg_url.startswith("/static/"):
                        bg_path = str(Path(__file__).parent / "static" / bg_url[len("/static/"):])

            process_all_frames_background(spin_dir, wm_mode, bg_path, turntable_style)
        except Exception as e:
            print(f"[Finalize] background work failed for {vin}: {e}")
        finally:
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                pass

    threading.Thread(target=_finalize_work, daemon=True).start()

    return {"status": "started", "original_frames": frame_count, "processing_bg": True,
            "message": "Processing in background. Poll /api/finalize-status/{vin}."}


@app.get("/api/finalize-status/{vin}")
async def finalize_status(vin: str):
    """Check if background removal is done for a vehicle."""
    vin = vin.upper()
    spin_dir = STORAGE_DIR / vin / "spin"
    frames = list(spin_dir.glob("spin_*.jpg")) if spin_dir.exists() else []
    processing = (spin_dir / ".processing").exists()
    return {"vin": vin, "spin_frames": len(frames), "processing": processing,
            "ready": len(frames) > 0 and not processing}


# =============================================================================
# API — UPLOAD DEALER SHOWROOM BACKGROUND
# =============================================================================
@app.post("/api/showroom")
async def upload_showroom(
    dealer_name: str = Form("default"),
    photo: UploadFile = File(...)
):
    """Dealer uploads showroom background - used for all vehicles."""
    SHOWROOM_DIR.mkdir(exist_ok=True)
    
    raw = await photo.read()
    
    if len(raw) > MAX_PHOTO_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"Photo too large (max {MAX_PHOTO_MB}MB)")
    
    save_path = SHOWROOM_DIR / f"{dealer_name}.jpg"
    
    try:
        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")
        img.save(save_path, "JPEG", quality=90)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot process image: {str(e)}")
    
    return {"status": "ok", "showroom": save_path.name}


@app.get("/api/showroom")
async def get_showroom(dealer_name: str = "default"):
    """Get showroom background for a dealer."""
    showroom_path = SHOWROOM_DIR / f"{dealer_name}.jpg"
    if showroom_path.exists():
        return {"exists": True, "showroom": showroom_path.name}
    return {"exists": False}


# =============================================================================
# API — HOTSPOTS
# =============================================================================
@app.post("/api/hotspot")
async def add_hotspot(
    vin: str = Form(...),
    photo_filename: str = Form(...),
    tag: str = Form(...),
    x: str = Form("0.5"),
    y: str = Form("0.5")
):
    """Add a hotspot to a photo."""
    vin = vin.upper()
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.execute(
        "INSERT INTO hotspots (vin, photo_filename, tag, hotspot_x, hotspot_y) VALUES (?, ?, ?, ?, ?)",
        (vin, photo_filename, tag, float(x), float(y))
    )
    conn.commit()
    hotspot_id = cursor.lastrowid
    conn.close()
    return {"status": "ok", "id": hotspot_id}


@app.get("/api/hotspots/{vin}")
async def get_hotspots(vin: str):
    """Get all hotspots for a vehicle."""
    vin = vin.upper()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cursor = conn.execute(
        "SELECT id, photo_filename, tag, hotspot_x, hotspot_y FROM hotspots WHERE vin = ?",
        (vin,)
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {"id": r["id"], "photo": r["photo_filename"], "tag": r["tag"], "x": r["hotspot_x"], "y": r["hotspot_y"]}
        for r in rows
    ]


@app.delete("/api/hotspot/{hotspot_id}")
async def delete_hotspot(hotspot_id: int):
    """Delete a hotspot."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("DELETE FROM hotspots WHERE id = ?", (hotspot_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}


# =============================================================================
# ROUTES — TAGGING PAGE
# =============================================================================
@app.get("/tag/{vin}")
async def tag_page(vin: str):
    """Tag hotspots on vehicle photos."""
    vin = vin.upper()
    vehicle_dir = STORAGE_DIR / vin
    
    if not vehicle_dir.exists():
        return HTMLResponse("<h1>Vehicle not found</h1>", status_code=404)
    
    exterior_dir = vehicle_dir / "exterior"
    frames = sorted([f.name for f in exterior_dir.glob("frame_*.jpg")]) if exterior_dir.exists() else []
    
    if not frames:
        return HTMLResponse("<h1>No exterior photos found</h1>", status_code=404)
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Tag Features - LotVue Pro</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: 'Segoe UI', sans-serif; background: #e8edf2; color: #1a2332; min-height: 100vh; }}
        .header {{ background: #dde3ea; padding: 1rem 2rem; border-bottom: 1px solid #c5cdd6; display: flex; justify-content: space-between; align-items: center; }}
        .logo {{ font-size: 1.2rem; font-weight: 700; color: #58a6ff; }}
        .container {{ max-width: 1200px; margin: 0 auto; padding: 1.5rem; }}
        .card {{ background: #dde3ea; border: 1px solid #c5cdd6; border-radius: 8px; padding: 1.5rem; margin-bottom: 1.5rem; }}
        .card h2 {{ color: #1a2332; margin-bottom: 1rem; }}
        .frames-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 0.8rem; }}
        .frame-thumb {{ aspect-ratio: 16/9; border-radius: 6px; overflow: hidden; cursor: pointer; border: 2px solid transparent; transition: all 0.2s; }}
        .frame-thumb:hover {{ border-color: #58a6ff; }}
        .frame-thumb.selected {{ border-color: #58a6ff; box-shadow: 0 0 12px rgba(88,166,255,0.4); }}
        .frame-thumb img {{ width: 100%; height: 100%; object-fit: cover; }}
        .frame-num {{ position: absolute; bottom: 4px; left: 4px; background: rgba(0,0,0,0.7); padding: 2px 6px; border-radius: 4px; font-size: 0.75rem; }}
        .selected-viewer {{ position: relative; margin-top: 1rem; }}
        .selected-viewer img {{ width: 100%; max-height: 400px; object-fit: contain; border-radius: 8px; background: #000; cursor: crosshair; }}
        .tag-form {{ display: flex; gap: 1rem; align-items: center; margin-top: 1rem; flex-wrap: wrap; }}
        .tag-form select {{ padding: 0.6rem; background: #010409; border: 1px solid #c5cdd6; border-radius: 6px; color: #1a2332; font-size: 1rem; }}
        .btn {{ background: #238636; border: none; padding: 0.6rem 1.2rem; color: white; border-radius: 6px; cursor: pointer; font-weight: 600; }}
        .btn:hover {{ background: #2ea043; }}
        .btn-outline {{ background: transparent; border: 1px solid #b0bac5; color: #8b949e; }}
        .hotspots-list {{ margin-top: 1.5rem; }}
        .hotspot-item {{ display: flex; justify-content: space-between; align-items: center; background: #c5cdd6; padding: 0.8rem 1rem; border-radius: 6px; margin-bottom: 0.5rem; }}
        .hotspot-item .tag-badge {{ background: #58a6ff; color: #e8edf2; padding: 2px 8px; border-radius: 4px; font-size: 0.85rem; font-weight: 600; }}
        .nav-btns {{ display: flex; gap: 1rem; justify-content: center; margin-top: 2rem; }}
    </style>
</head>
<body>
    <div class="header">
        <div class="logo">LotVue Pro - Tag Features</div>
        <a href="/" style="color:#58a6ff;text-decoration:none;">&larr; Dashboard</a>
    </div>
    <div class="container">
        <div class="card">
            <h2>Select a Photo to Tag</h2>
            <p style="color:#8b949e;margin-bottom:1rem;">Click a photo below, then click on the car where you want the hotspot.</p>
            <div class="frames-grid" id="framesGrid">
"""
    
    for i, frame in enumerate(frames):
        html += f'<div class="frame-thumb" onclick="selectFrame({i})"><img src="/storage/{vin}/exterior/{frame}"><span class="frame-num">{i+1}</span></div>'
    
    html += f"""
            </div>
        </div>
        
        <div class="card" id="selectedCard" style="display:none;">
            <h2>Tag This Photo</h2>
            <div class="selected-viewer">
                <img id="selectedImg" src="" onclick="placeHotspot(event)">
            </div>
            <div class="tag-form">
                <select id="tagSelect">
                    <option value="">-- Select Feature --</option>
                    <option value="hood_open">Hood Open</option>
                    <option value="trunk_open">Trunk Open</option>
                    <option value="driver_door">Driver Door</option>
                    <option value="passenger_door">Passenger Door</option>
                    <option value="engine_bay">Engine Bay</option>
                    <option value="back_seat">Back Seat</option>
                    <option value="dashboard">Dashboard</option>
                    <option value="steering_wheel">Steering Wheel</option>
                    <option value="radio_infotainment">Radio/Infotainment</option>
                    <option value="rear_camera">Rear Camera on Screen</option>
                    <option value="headlights_on">Headlights On</option>
                    <option value="wipers">Wipers</option>
                    <option value="blinkers">Blinkers</option>
                </select>
                <button class="btn" onclick="saveHotspot()">Save Hotspot</button>
            </div>
            <p id="clickHint" style="color:#8b949e;margin-top:0.5rem;font-size:0.85rem;">Click on the car in the image above to place the hotspot</p>
        </div>
        
        <div class="card">
            <h2>Current Hotspots</h2>
            <div class="hotspots-list" id="hotspotsList">
                <p style="color:#8b949e;">No hotspots tagged yet.</p>
            </div>
        </div>
        
        <div class="nav-btns">
            <a href="/view/{vin}" class="btn">Skip &rarr; View 360</a>
            <a href="/view/{vin}" class="btn">Done &rarr; View 360</a>
        </div>
    </div>
    
    <script>
        const frames = {json.dumps(frames)};
        let selectedIdx = 0;
        let clickX = 0.5, clickY = 0.5;
        
        async function loadHotspots() {{
            const r = await fetch('/api/hotspots/{vin}');
            const hotspots = await r.json();
            const list = document.getElementById('hotspotsList');
            if (hotspots.length === 0) {{
                list.innerHTML = '<p style="color:#8b949e;">No hotspots tagged yet.</p>';
                return;
            }}
            list.innerHTML = hotspots.map(h => `
                <div class="hotspot-item">
                    <span>📷 ${{h.photo}} → <span class="tag-badge">${{h.tag.replace('_', ' ')}}</span></span>
                    <button class="btn-outline" onclick="deleteHotspot(${{h.id}})" style="padding:4px 8px;">Delete</button>
                </div>
            `).join('');
        }}
        
        function selectFrame(idx) {{
            selectedIdx = idx;
            document.querySelectorAll('.frame-thumb').forEach((el, i) => {{
                el.classList.toggle('selected', i === idx);
            }});
            document.getElementById('selectedImg').src = '/storage/{vin}/exterior/' + frames[idx];
            document.getElementById('selectedCard').style.display = 'block';
        }}
        
        function placeHotspot(e) {{
            const rect = e.target.getBoundingClientRect();
            clickX = (e.clientX - rect.left) / rect.width;
            clickY = (e.clientY - rect.top) / rect.height;
            document.getElementById('clickHint').textContent = 'Hotspot placed at (' + Math.round(clickX*100) + '%, ' + Math.round(clickY*100) + '%)';
        }}
        
        async function saveHotspot() {{
            const tag = document.getElementById('tagSelect').value;
            if (!tag) {{
                const hint = document.getElementById('clickHint');
                hint.textContent = '⚠️ Please select a feature tag first';
                hint.style.color = '#d29922';
                setTimeout(() => {{ hint.style.color = ''; hint.textContent = 'Click on the car in the image above to place the hotspot'; }}, 2500);
                return;
            }}
            
            const fd = new FormData();
            fd.append('vin', '{vin}');
            fd.append('photo_filename', frames[selectedIdx]);
            fd.append('tag', tag);
            fd.append('x', clickX.toString());
            fd.append('y', clickY.toString());
            
            await fetch('/api/hotspot', {{ method: 'POST', body: fd }});
            document.getElementById('clickHint').textContent = 'Click on the car in the image above to place the hotspot';
            loadHotspots();
        }}
        
        async function deleteHotspot(id) {{
            await fetch('/api/hotspot/' + id, {{ method: 'DELETE' }});
            loadHotspots();
        }}
        
        loadHotspots();
    </script>
</body>
</html>
"""
    return HTMLResponse(html)


# =============================================================================
# ROUTES — 360 VIEWER
# =============================================================================
@app.get("/view/{vin}")
async def view_vehicle(vin: str, request: Request):
    try:
        vin = vin.upper()
        vehicle_dir = STORAGE_DIR / vin

        if not vehicle_dir.exists():
            return HTMLResponse("<h1>Vehicle not found</h1>", status_code=404)

        # ── Exterior frames ──────────────────────────────────────────────────
        spin_dir = vehicle_dir / "spin"
        if spin_dir.exists() and any(spin_dir.glob("spin_*.jpg")):
            ext_frames = sorted([f.name for f in spin_dir.glob("spin_*.jpg")])
            frame_path_prefix = f"/storage/{vin}/spin"
        else:
            ext_dir = vehicle_dir / "exterior"
            ext_frames = sorted([f.name for f in ext_dir.glob("*.jpg")]) if ext_dir.exists() else []
            frame_path_prefix = f"/storage/{vin}/exterior"

        # ── Interior photos ──────────────────────────────────────────────────
        int_dir = vehicle_dir / "interior"
        int_photos = sorted([f.name for f in int_dir.glob("*.jpg")]) if int_dir.exists() else []

        # ── Interior panoramas (equirectangular) ─────────────────────────────
        pano_dir = vehicle_dir / "panoramas"
        pano_files = sorted([f.name for f in pano_dir.glob("*.jpg")] +
                            [f.name for f in pano_dir.glob("*.jpeg")] +
                            [f.name for f in pano_dir.glob("*.png")]) if pano_dir.exists() else []

        # ── Gallery photos ───────────────────────────────────────────────────
        gallery_dir = vehicle_dir / "gallery"
        gallery_files = sorted([f.name for f in gallery_dir.glob("*.jpg")] +
                               [f.name for f in gallery_dir.glob("*.jpeg")] +
                               [f.name for f in gallery_dir.glob("*.png")]) if gallery_dir.exists() else []
        # Fallback: use exterior frames as gallery if no dedicated gallery
        if not gallery_files:
            gallery_files = ext_frames
            gallery_prefix = frame_path_prefix
        else:
            gallery_prefix = f"/storage/{vin}/gallery"

        # ── DB info ──────────────────────────────────────────────────────────
        try:
            info = db_get_vehicle(vin) or {"vin": vin, "year": "", "make": "", "model": ""}
        except Exception:
            info = {"vin": vin, "year": "", "make": "", "model": ""}

        title = f"{info.get('year','')} {info.get('make','')} {info.get('model','')}".strip() or vin
        price = info.get("price", "")
        mileage = info.get("mileage", "")
        color = info.get("color", "")
        video_url = info.get("video_url", "") or ""
        video_type = info.get("video_type", "") or ""
        dealer_logo = info.get("dealer_logo_url", "") or ""

        # ── Build JS arrays ──────────────────────────────────────────────────
        import json as _json
        import time as _time
        _cb = int(_time.time())
        ext_urls_js = _json.dumps([f"{frame_path_prefix}/{f}?v={_cb}" for f in ext_frames])
        int_urls_js = _json.dumps([f"/storage/{vin}/interior/{f}" for f in int_photos])
        pano_urls_js = _json.dumps([{"src": f"/storage/{vin}/panoramas/{f}", "name": f"Scene {i+1}"} for i, f in enumerate(pano_files)])
        gallery_urls_js = _json.dumps([f"{gallery_prefix}/{f}" for f in gallery_files])

        # ── Tab visibility ───────────────────────────────────────────────────
        has_interior = len(int_photos) > 0 or len(pano_files) > 0
        has_gallery = len(gallery_files) > 0
        has_video = bool(video_url)
        base_url = str(request.base_url).rstrip("/")
        share_url = f"{base_url}/view/{vin}"
        embed_code = f'&lt;iframe src="{base_url}/embed/{vin}" width="100%" height="600" frameborder="0" allowfullscreen&gt;&lt;/iframe&gt;'
        # Social sharing metadata (og: / twitter:)
        og_image = f"{base_url}{frame_path_prefix}/{ext_frames[0]}" if ext_frames else ""
        og_desc = " · ".join(x for x in [
            f"${price}" if price else "",
            f"{mileage} miles" if mileage else "",
            str(color) if color else "",
        ] if x) or f"360° interactive viewer for {title}"
        # Alias: vehicle = info (for template references to vehicle.get(...))
        vehicle = info
    except Exception as e:
        import traceback
        return HTMLResponse(f"<h1>Error loading view</h1><pre>{traceback.format_exc()}</pre>", status_code=500)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — LotVue Pro</title>
<meta property="og:type" content="website">
<meta property="og:title" content="{title} — LotVue360">
<meta property="og:description" content="{og_desc}">
<meta property="og:image" content="{og_image}">
<meta property="og:url" content="{share_url}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{title} — LotVue360">
<meta name="twitter:description" content="{og_desc}">
<meta name="twitter:image" content="{og_image}">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/pannellum@2.5.6/build/pannellum.css"/>
<script src="https://cdn.jsdelivr.net/npm/pannellum@2.5.6/build/pannellum.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
/* ── Reset & Base ─────────────────────────────────────────────────────── */
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0a1628;color:#f5f0e8;min-height:100vh;}}
/* ── Layout ───────────────────────────────────────────────────────────── */
.viewer-wrap{{max-width:1100px;margin:0 auto;padding:1rem;}}
/* ── Header ───────────────────────────────────────────────────────────── */
.viewer-header{{display:flex;align-items:center;justify-content:space-between;padding:0.75rem 1rem;background:#0f2040;border:1px solid #1a2e50;border-radius:10px 10px 0 0;gap:1rem;}}
.dealer-logo{{height:36px;max-width:120px;object-fit:contain;}}
.dealer-logo-placeholder{{font-family:'Playfair Display',serif;font-size:1.1rem;font-weight:700;color:#c9a84c;letter-spacing:1px;}}
.vehicle-title{{flex:1;text-align:center;}}
.vehicle-title h1{{font-size:1.1rem;font-weight:700;color:#f5f0e8;}}
.vehicle-title .vin-badge{{font-size:0.7rem;color:#a0aab4;font-family:monospace;}}
.header-actions{{display:flex;gap:0.5rem;}}
.btn-icon{{background:#163060;border:1px solid #1a2e50;color:#a0aab4;padding:0.4rem 0.7rem;border-radius:6px;cursor:pointer;font-size:0.8rem;transition:all 0.2s;}}
.btn-icon:hover{{background:#1a2e50;color:#f5f0e8;}}
/* ── Main viewer area ─────────────────────────────────────────────────── */
.viewer-body{{display:flex;gap:0;background:#0a1628;border-left:1px solid #1a2e50;border-right:1px solid #1a2e50;}}
.view-area{{flex:1;min-height:480px;position:relative;background:#060e1a;}}
/* ── Specs sidebar ────────────────────────────────────────────────────── */
.specs-sidebar{{width:220px;background:#0f2040;border-left:1px solid #1a2e50;padding:1rem;display:flex;flex-direction:column;gap:0.75rem;overflow-y:auto;}}
.specs-sidebar h3{{font-size:0.8rem;text-transform:uppercase;letter-spacing:1px;color:#a0aab4;margin-bottom:0.25rem;}}
.bg-grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:6px;margin-top:4px;}}
.bg-thumb{{width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:4px;cursor:pointer;border:2px solid transparent;transition:border-color 0.2s;}}
.bg-thumb.active{{border-color:#c9a84c;}}
.bg-thumb-none{{width:100%;aspect-ratio:16/9;background:linear-gradient(135deg,#0a1628 50%,#163060 50%);border-radius:4px;cursor:pointer;border:2px solid transparent;display:flex;align-items:center;justify-content:center;font-size:0.65rem;color:#a0aab4;transition:border-color 0.2s;}}
.bg-thumb-none.active{{border-color:#c9a84c;}}
.spec-row{{display:flex;flex-direction:column;gap:0.1rem;}}
.spec-label{{font-size:0.7rem;color:#a0aab4;}}
.spec-value{{font-size:0.9rem;font-weight:600;color:#f5f0e8;}}
.price-display{{font-size:1.4rem;font-weight:800;color:#2ab89a;}}
.cta-btn{{background:#1a8a7a;color:#fff;border:none;padding:0.6rem 1rem;border-radius:6px;cursor:pointer;font-weight:600;font-size:0.85rem;width:100%;margin-top:auto;transition:background 0.2s;}}
.cta-btn:hover{{background:#2ab89a;}}
/* ── Tab bar ──────────────────────────────────────────────────────────── */
.tab-bar{{display:flex;background:#0f2040;border:1px solid #1a2e50;border-top:none;}}
.tab-btn{{flex:1;padding:0.7rem 0.5rem;background:transparent;border:none;color:#a0aab4;cursor:pointer;font-size:0.8rem;font-weight:600;text-align:center;border-bottom:3px solid transparent;transition:all 0.2s;display:flex;flex-direction:column;align-items:center;gap:0.2rem;}}
.tab-btn:hover{{color:#f5f0e8;background:#163060;}}
.tab-btn.active{{color:#c9a84c;border-bottom-color:#c9a84c;background:#0a1628;}}
.tab-btn .tab-icon{{font-size:1.1rem;}}
.tab-btn.hidden{{display:none;}}
/* ── Tab panels ───────────────────────────────────────────────────────── */
.tab-panel{{display:none;width:100%;height:100%;}}
.tab-panel.active{{display:block;}}
/* ── Exterior spin ────────────────────────────────────────────────────── */
#spin-container{{width:100%;height:480px;position:relative;background:#060e1a;cursor:grab;touch-action:none;user-select:none;overflow:hidden;}}
#spin-container:active{{cursor:grabbing;}}
#spin-container img{{width:100%;height:100%;object-fit:contain;position:absolute;inset:0;pointer-events:none;display:none;}}
.frame-counter{{position:absolute;top:0.75rem;right:0.75rem;background:rgba(10,22,40,0.85);color:#f5f0e8;padding:0.25rem 0.6rem;border-radius:12px;font-size:0.75rem;font-family:monospace;z-index:10;border:1px solid #1a2e50;}}
.spin-hint{{position:absolute;bottom:0.75rem;left:50%;transform:translateX(-50%);background:rgba(10,22,40,0.85);color:#a0aab4;padding:0.3rem 0.9rem;border-radius:12px;font-size:0.75rem;z-index:10;pointer-events:none;transition:opacity 0.5s;border:1px solid #1a2e50;}}
.spin-controls{{position:absolute;bottom:0.75rem;right:0.75rem;display:flex;gap:0.4rem;z-index:10;}}
.spin-btn{{background:rgba(10,22,40,0.85);border:1px solid #1a2e50;color:#f5f0e8;width:32px;height:32px;border-radius:6px;cursor:pointer;font-size:0.9rem;display:flex;align-items:center;justify-content:center;}}
.spin-btn:hover{{background:rgba(201,168,76,0.2);}}
/* ── Hotspot dots ─────────────────────────────────────────────────────── */
.hotspot-dot{{position:absolute;width:22px;height:22px;background:rgba(201,168,76,0.9);border:2px solid #fff;border-radius:50%;cursor:pointer;transform:translate(-50%,-50%);z-index:20;animation:pulse 2s infinite;}}
@keyframes pulse{{0%,100%{{box-shadow:0 0 0 0 rgba(201,168,76,0.6);}}50%{{box-shadow:0 0 0 8px rgba(201,168,76,0);}}}}
.hs-tooltip{{position:absolute;background:#0f2040;color:#f5f0e8;padding:0.4rem 0.8rem;border-radius:6px;font-size:0.8rem;font-weight:600;z-index:30;pointer-events:none;white-space:nowrap;transform:translate(-50%,-130%);box-shadow:0 4px 12px rgba(0,0,0,0.4);border:1px solid #1a2e50;}}
.hs-tooltip::after{{content:'';position:absolute;top:100%;left:50%;transform:translateX(-50%);border:6px solid transparent;border-top-color:#0f2040;}}
/* ── Feature pills ────────────────────────────────────────────────────── */
.feature-pills{{display:none;flex-wrap:wrap;gap:0.4rem;padding:0.6rem 1rem;background:#0a1628;border-left:1px solid #1a2e50;border-right:1px solid #1a2e50;}}
.feature-pill{{background:#163060;border:1px solid #1a2e50;color:#a0aab4;padding:0.25rem 0.7rem;border-radius:20px;font-size:0.72rem;cursor:pointer;transition:all 0.2s;}}
.feature-pill:hover{{background:#c9a84c;color:#0a1628;border-color:#c9a84c;}}
/* ── Frame strip ──────────────────────────────────────────────────────── */
.frame-strip{{display:flex;gap:4px;padding:6px;background:#0a1628;border:1px solid #1a2e50;border-top:none;overflow-x:auto;scrollbar-width:thin;scrollbar-color:#1a2e50 #0a1628;}}
.frame-strip::-webkit-scrollbar{{height:4px;}}
.frame-strip::-webkit-scrollbar-track{{background:#0a1628;}}
.frame-strip::-webkit-scrollbar-thumb{{background:#1a2e50;border-radius:2px;}}
.frame-thumb{{width:60px;height:38px;flex-shrink:0;border-radius:3px;overflow:hidden;cursor:pointer;border:2px solid transparent;transition:border-color 0.15s;}}
.frame-thumb:hover{{border-color:#c9a84c;}}
.frame-thumb.active{{border-color:#c9a84c;}}
.frame-thumb img{{width:100%;height:100%;object-fit:cover;}}
/* ── Interior panorama ────────────────────────────────────────────────── */
#pano-container{{width:100%;height:480px;position:relative;}}
.pano-scene-selector{{position:absolute;top:0.75rem;left:0.75rem;z-index:100;background:rgba(10,22,40,0.85);border:1px solid #1a2e50;color:#f5f0e8;padding:0.4rem 0.7rem;border-radius:6px;font-size:0.8rem;cursor:pointer;}}
.int-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:0.5rem;padding:0.75rem;max-height:480px;overflow-y:auto;}}
.int-thumb{{aspect-ratio:16/10;border-radius:6px;overflow:hidden;cursor:pointer;border:2px solid transparent;transition:border-color 0.2s;}}
.int-thumb:hover{{border-color:#c9a84c;}}
.int-thumb img{{width:100%;height:100%;object-fit:cover;}}
/* ── Gallery ──────────────────────────────────────────────────────────── */
.gallery-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:0.5rem;padding:0.75rem;max-height:480px;overflow-y:auto;}}
.gallery-item{{aspect-ratio:16/10;border-radius:6px;overflow:hidden;cursor:pointer;position:relative;border:2px solid transparent;transition:border-color 0.2s;}}
.gallery-item:hover{{border-color:#c9a84c;}}
.gallery-item img{{width:100%;height:100%;object-fit:cover;}}
/* ── Video tab ────────────────────────────────────────────────────────── */
.video-container{{width:100%;height:480px;display:flex;align-items:center;justify-content:center;background:#060e1a;}}
.video-container video{{max-width:100%;max-height:100%;}}
.video-container iframe{{width:100%;height:100%;border:none;}}
.no-video{{text-align:center;color:#a0aab4;padding:2rem;}}
.no-video .no-video-icon{{font-size:3rem;margin-bottom:1rem;}}
/* ── Lead form ────────────────────────────────────────────────────────── */
.lead-form-wrap{{padding:1.5rem;max-height:480px;overflow-y:auto;}}
.lead-form-wrap h2{{font-size:1.1rem;font-weight:700;margin-bottom:0.25rem;}}
.lead-form-wrap p{{font-size:0.85rem;color:#a0aab4;margin-bottom:1.25rem;}}
.form-row{{display:grid;grid-template-columns:1fr 1fr;gap:0.75rem;margin-bottom:0.75rem;}}
.form-row.full{{grid-template-columns:1fr;}}
.form-group label{{display:block;font-size:0.75rem;color:#a0aab4;margin-bottom:0.3rem;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;}}
.form-group input,.form-group textarea,.form-group select{{width:100%;background:#163060;border:1px solid #1a2e50;color:#f5f0e8;padding:0.6rem 0.75rem;border-radius:6px;font-size:0.9rem;outline:none;transition:border-color 0.2s;}}
.form-group input:focus,.form-group textarea:focus{{border-color:#c9a84c;box-shadow:0 0 0 3px rgba(201,168,76,0.12);}}
.form-group textarea{{resize:vertical;min-height:80px;}}
.checkbox-row{{display:flex;gap:1rem;margin-bottom:1rem;flex-wrap:wrap;}}
.checkbox-label{{display:flex;align-items:center;gap:0.4rem;font-size:0.85rem;cursor:pointer;}}
.checkbox-label input{{width:auto;}}
.submit-btn{{background:#1a8a7a;color:#fff;border:none;padding:0.75rem 2rem;border-radius:6px;font-size:0.95rem;font-weight:700;cursor:pointer;width:100%;transition:background 0.2s;}}
.submit-btn:hover{{background:#2ab89a;}}
.submit-btn:disabled{{background:#163060;color:#a0aab4;cursor:not-allowed;}}
.form-success{{text-align:center;padding:2rem;}}
.form-success .success-icon{{font-size:3rem;margin-bottom:1rem;}}
.form-success h3{{color:#2ab89a;font-size:1.2rem;margin-bottom:0.5rem;}}
/* ── Footer ───────────────────────────────────────────────────────────── */
.viewer-footer{{display:flex;align-items:center;justify-content:space-between;padding:0.6rem 1rem;background:#0f2040;border:1px solid #1a2e50;border-top:none;border-radius:0 0 10px 10px;}}
.powered-by{{font-size:0.7rem;color:#a0aab4;}}
.powered-by a{{color:#c9a84c;text-decoration:none;}}
.footer-actions{{display:flex;gap:0.5rem;}}
/* ── Share panel ──────────────────────────────────────────────────────── */
.share-panel{{display:none;position:absolute;bottom:3rem;right:1rem;background:#0f2040;border:1px solid #1a2e50;border-radius:8px;padding:1rem;z-index:200;min-width:280px;box-shadow:0 8px 24px rgba(0,0,0,0.5);}}
.share-panel.open{{display:block;}}
.share-panel h4{{font-size:0.85rem;margin-bottom:0.75rem;color:#f5f0e8;}}
.share-url{{display:flex;gap:0.4rem;margin-bottom:0.75rem;}}
.share-url input{{flex:1;background:#163060;border:1px solid #1a2e50;color:#f5f0e8;padding:0.4rem 0.6rem;border-radius:4px;font-size:0.75rem;}}
.share-url button{{background:#1a8a7a;border:none;color:#fff;padding:0.4rem 0.7rem;border-radius:4px;cursor:pointer;font-size:0.75rem;}}
.embed-code{{background:#163060;border:1px solid #1a2e50;border-radius:4px;padding:0.5rem;font-size:0.7rem;font-family:monospace;color:#a0aab4;word-break:break-all;margin-bottom:0.5rem;}}
.share-btns{{display:flex;gap:0.4rem;flex-wrap:wrap;}}
.share-btn{{background:#163060;border:1px solid #1a2e50;color:#a0aab4;padding:0.35rem 0.7rem;border-radius:4px;cursor:pointer;font-size:0.75rem;transition:all 0.2s;}}
.share-btn:hover{{background:#1a2e50;color:#f5f0e8;}}
/* ── Lightbox ─────────────────────────────────────────────────────────── */
#lightbox{{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.92);z-index:1000;align-items:center;justify-content:center;}}
#lightbox.open{{display:flex;}}
#lightbox img{{max-width:90vw;max-height:90vh;object-fit:contain;border-radius:4px;}}
#lightbox-close{{position:absolute;top:1rem;right:1rem;background:rgba(255,255,255,0.1);border:none;color:#fff;font-size:1.5rem;width:40px;height:40px;border-radius:50%;cursor:pointer;}}
/* ── Toast ────────────────────────────────────────────────────────────── */
.toast{{position:fixed;bottom:2rem;left:50%;transform:translateX(-50%);background:#0f2040;color:#f5f0e8;padding:0.6rem 1.4rem;border-radius:8px;font-size:0.9rem;font-weight:600;z-index:3000;box-shadow:0 4px 16px rgba(0,0,0,0.3);opacity:0;transition:opacity 0.3s;pointer-events:none;border:1px solid #1a2e50;}}
.toast.show{{opacity:1;}}
/* ── Responsive ───────────────────────────────────────────────────────── */
@media(max-width:768px){{
    .specs-sidebar{{display:none;}}
    .viewer-body{{flex-direction:column;}}
    #spin-container{{height:300px;}}
    #pano-container{{height:300px;}}
    .video-container{{height:300px;}}
    .tab-btn span{{display:none;}}
    .form-row{{grid-template-columns:1fr;}}
}}
</style>
</head>
<body>
<div class="viewer-wrap">

  <!-- HEADER -->
  <div class="viewer-header">
    {'<img class="dealer-logo" src="' + dealer_logo + '" alt="Dealer">' if dealer_logo else '<div class="dealer-logo-placeholder">LotVue Pro</div>'}
    <div class="vehicle-title">
      <h1>{title}</h1>
      <div class="vin-badge">VIN: {vin}</div>
    </div>
    <div class="header-actions">
      <button class="btn-icon" onclick="toggleFullscreen()" id="fsBtn">&#x26F6; Fullscreen</button>
      <button class="btn-icon" onclick="toggleShare()">&#x1F517; Share</button>
    </div>
  </div>

  <!-- BODY: view area + specs sidebar -->
  <div class="viewer-body">
    <div class="view-area" id="viewArea">

      <!-- TAB PANEL: Exterior 360 Spin -->
      <div class="tab-panel active" id="panel-exterior">
        <div id="spin-container">
          <!-- frames injected by JS -->
          <div class="frame-counter" id="frameCounter">0 / 0</div>
          <div class="frame-counter" id="zoomBadge" style="display:none;top:2.6rem;"></div>
          <div class="spin-hint" id="spinHint">&#8592; Drag to spin &#8594; &nbsp;·&nbsp; scroll/pinch to zoom</div>
          <div class="spin-controls">
            <button class="spin-btn" onclick="spinStep(-1)" title="Previous">&#8592;</button>
            <button class="spin-btn" id="autoBtn" onclick="toggleAuto()" title="Autoplay">&#9654;</button>
            <button class="spin-btn" onclick="spinStep(1)" title="Next">&#8594;</button>
          </div>
        </div>
      </div>

      <!-- TAB PANEL: Interior -->
      <div class="tab-panel" id="panel-interior">
        <div id="pano-container">
          <!-- Pannellum mounts here if panorama exists, else grid -->
        </div>
        <div class="int-grid" id="intGrid" style="display:none;"></div>
      </div>

      <!-- TAB PANEL: Gallery -->
      <div class="tab-panel" id="panel-gallery">
        <div class="gallery-grid" id="galleryGrid"></div>
      </div>

      <!-- TAB PANEL: Video -->
      <div class="tab-panel" id="panel-video">
        <div class="video-container" id="videoContainer">
          <div class="no-video">
            <div class="no-video-icon">&#127910;</div>
            <p>No video available for this vehicle.</p>
          </div>
        </div>
      </div>

      <!-- TAB PANEL: Inquiry -->
      <div class="tab-panel" id="panel-inquiry">
        <div class="lead-form-wrap">
          <h2>Request Information</h2>
          <p>Interested in this vehicle? Fill out the form and we'll get back to you shortly.</p>
          <div id="leadFormInner">
            <form id="leadForm" onsubmit="submitLead(event)">
              <div class="form-row">
                <div class="form-group"><label>Full Name *</label><input type="text" name="name" required placeholder="John Smith"></div>
                <div class="form-group"><label>Email *</label><input type="email" name="email" required placeholder="john@example.com"></div>
              </div>
              <div class="form-row">
                <div class="form-group"><label>Phone</label><input type="tel" name="phone" placeholder="(555) 000-0000"></div>
                <div class="form-group"><label>Best Time to Call</label><input type="text" name="call_time" placeholder="Anytime / Morning / Afternoon"></div>
              </div>
              <div class="form-row full">
                <div class="form-group"><label>Message</label><textarea name="message" placeholder="I'm interested in this vehicle..."></textarea></div>
              </div>
              <div class="checkbox-row">
                <label class="checkbox-label"><input type="checkbox" name="wants_video"> Request Walk-Around Video</label>
                <label class="checkbox-label"><input type="checkbox" name="wants_test_drive"> Schedule Test Drive</label>
                <label class="checkbox-label"><input type="checkbox" name="wants_trade"> Trade-In Inquiry</label>
              </div>
              <button type="submit" class="submit-btn" id="submitBtn">Send Inquiry</button>
            </form>
          </div>
        </div>
      </div>

      <!-- Share panel (absolute positioned) -->
      <div class="share-panel" id="sharePanel">
        <h4>Share This Vehicle</h4>
        <div class="share-url">
          <input type="text" id="shareUrlInput" value="{share_url}" readonly>
          <button onclick="copyShareUrl()">Copy</button>
        </div>
        <div class="embed-code" id="embedCodeBlock">{embed_code}</div>
        <div class="share-btns">
          <button class="share-btn" onclick="shareWhatsApp()">WhatsApp</button>
          <button class="share-btn" onclick="shareFacebook()">Facebook</button>
          <button class="share-btn" onclick="shareEmail()">Email</button>
          <button class="share-btn" onclick="copyEmbed()">Copy Embed</button>
        </div>
      </div>

    </div><!-- /view-area -->

    <!-- SPECS SIDEBAR -->
    <div class="specs-sidebar">
      <h3>Vehicle Details</h3>
      {'<div class="spec-row"><div class="spec-label">Price</div><div class="price-display">' + price + '</div></div>' if price else ''}
      {'<div class="spec-row"><div class="spec-label">Mileage</div><div class="spec-value">' + mileage + ' mi</div></div>' if mileage else ''}
      {'<div class="spec-row"><div class="spec-label">Color</div><div class="spec-value">' + color + '</div></div>' if color else ''}
      {'<div class="spec-row"><div class="spec-label">Trim</div><div class="spec-value">' + (vehicle.get("trim_level","") or "") + '</div></div>' if vehicle.get("trim_level") else ''}
      {'<div class="spec-row"><div class="spec-label">Engine</div><div class="spec-value">' + (vehicle.get("engine","") or "") + '</div></div>' if vehicle.get("engine") else ''}
      {'<div class="spec-row"><div class="spec-label">Trans.</div><div class="spec-value">' + (vehicle.get("transmission","") or "") + '</div></div>' if vehicle.get("transmission") else ''}
      {'<div class="spec-row"><div class="spec-label">Drive</div><div class="spec-value">' + (vehicle.get("drivetrain","") or "") + '</div></div>' if vehicle.get("drivetrain") else ''}
      {'<div class="spec-row"><div class="spec-label">Fuel</div><div class="spec-value">' + (vehicle.get("fuel_type","") or "") + '</div></div>' if vehicle.get("fuel_type") else ''}
      {'<div class="spec-row"><div class="spec-label">Body</div><div class="spec-value">' + (vehicle.get("body_style","") or "") + '</div></div>' if vehicle.get("body_style") else ''}
      <div class="spec-row"><div class="spec-label">VIN</div><div class="spec-value" style="font-size:0.75rem;font-family:monospace;">{vin}</div></div>
      <div class="spec-row"><div class="spec-label">Frames</div><div class="spec-value">{len(ext_frames)}</div></div>
      <!-- BACKGROUND SELECTOR -->
      <div class="bg-selector-wrap" id="bgSelectorWrap">
        <h3 style="margin-top:0.75rem;">Background</h3>
        <div class="bg-grid" id="bgGrid">Loading...</div>
      </div>
      <button class="cta-btn" onclick="switchTab('inquiry')">Request Info</button>
    </div>
  </div><!-- /viewer-body -->

  <!-- TAB BAR -->
  <div class="tab-bar" id="tabBar">
    <button class="tab-btn active" id="tab-exterior" onclick="switchTab('exterior')">
      <span class="tab-icon">&#x1F504;</span><span>Exterior</span>
    </button>
    <button class="tab-btn {'hidden' if not has_interior else ''}" id="tab-interior" onclick="switchTab('interior')">
      <span class="tab-icon">&#x1F3E0;</span><span>Interior</span>
    </button>
    <button class="tab-btn {'hidden' if not has_gallery else ''}" id="tab-gallery" onclick="switchTab('gallery')">
      <span class="tab-icon">&#x1F4F7;</span><span>Gallery</span>
    </button>
    <button class="tab-btn {'hidden' if not has_video else ''}" id="tab-video" onclick="switchTab('video')">
      <span class="tab-icon">&#x1F3AC;</span><span>Video</span>
    </button>
    <button class="tab-btn" id="tab-inquiry" onclick="switchTab('inquiry')">
      <span class="tab-icon">&#x1F4CB;</span><span>Inquiry</span>
    </button>
  </div>

  <!-- FEATURE PILLS (hotspot tags) -->
  <div class="feature-pills" id="featurePills"></div>

  <!-- FRAME STRIP -->
  <div class="frame-strip" id="frameStrip"></div>

  <!-- FOOTER -->
  <div class="viewer-footer">
    <div class="powered-by">Powered by <a href="https://lotvuepro.com" target="_blank">LotVue Pro</a></div>
    <div class="footer-actions">
      <button class="btn-icon" onclick="switchTab('inquiry')">&#x1F4AC; Inquire</button>
    </div>
  </div>

</div><!-- /viewer-wrap -->

<!-- LIGHTBOX -->
<div id="lightbox">
  <button id="lightbox-close" onclick="closeLB()">&#x2715;</button>
  <img id="lightbox-img" src="" alt="">
</div>

<!-- TOAST -->
<div class="toast" id="toast"></div>

<!-- GLO3D buyer tools: AI chat + lead magnets (trade-in/price/history/test-drive/video) -->
<div id="lv-glo3d-root" data-vin="{vin}" data-title="{title}"></div>
<script src="/static/glo3d_tools.js?v=20260612b" defer></script>

<script>
// ── Data from server ────────────────────────────────────────────────────────
const EXT_URLS = {ext_urls_js};
const INT_URLS = {int_urls_js};
const PANO_URLS = {pano_urls_js};
const GALLERY_URLS = {gallery_urls_js};
const VIDEO_URL = {_json.dumps(video_url)};
const VIDEO_TYPE = {_json.dumps(video_type)};
const VIN = "{vin}";

// ── Toast helper ────────────────────────────────────────────────────────────
function showToast(msg, ms=2500) {{
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), ms);
}}

// ── Tab switching ───────────────────────────────────────────────────────────
let activeTab = 'exterior';
function switchTab(tab) {{
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('panel-' + tab).classList.add('active');
  const btn = document.getElementById('tab-' + tab);
  if (btn) btn.classList.add('active');
  activeTab = tab;
  if (tab === 'interior') initInterior();
  if (tab === 'gallery') initGallery();
  if (tab === 'video') initVideo();
  // Track analytics
  trackEvent('tab_switch', {{tab}});
}}

// ── Exterior 360 Spin Engine ────────────────────────────────────────────────
let spinFrames = [], spinCur = 0, spinDragging = false, spinStartX = 0, spinStartFrame = 0;
let autoInterval = null, autoRunning = false;
let spinLoaded = 0;

function initSpin() {{
  const container = document.getElementById('spin-container');
  if (!EXT_URLS.length) {{
    container.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#8b949e;font-size:1rem;">No exterior frames available</div>';
    return;
  }}
  // Create img elements
  EXT_URLS.forEach((url, i) => {{
    const img = document.createElement('img');
    img.src = url;
    img.dataset.idx = i;
    img.style.display = 'none';
    img.onload = () => {{
      spinLoaded++;
      if (spinLoaded === EXT_URLS.length) {{
        showSpinFrame(0);
        startAuto();
      }}
    }};
    img.onerror = () => spinLoaded++;
    container.insertBefore(img, container.querySelector('.frame-counter'));
    spinFrames.push(img);
  }});
  updateFrameCounter();
  buildFrameStrip();
  // Drag events
  container.addEventListener('mousedown', spinMouseDown);
  container.addEventListener('mousemove', spinMouseMove);
  container.addEventListener('mouseup', spinMouseUp);
  container.addEventListener('mouseleave', spinMouseUp);
  container.addEventListener('touchstart', spinTouchStart, {{passive: true}});
  container.addEventListener('touchmove', spinTouchMove, {{passive: false}});
  container.addEventListener('touchend', spinTouchEnd);
  // Zoom: mouse wheel, double-click cycle, pinch (handled in touch handlers)
  container.addEventListener('wheel', spinWheel, {{passive: false}});
  container.addEventListener('dblclick', () => {{
    zoomLevel = zoomLevel >= 4 ? 1 : zoomLevel * 2;
    if (zoomLevel === 1) {{ panX = 0; panY = 0; }}
    applyZoom();
  }});
  // Keyboard
  document.addEventListener('keydown', e => {{
    if (activeTab !== 'exterior') return;
    if (e.key === 'ArrowLeft') spinStep(-1);
    if (e.key === 'ArrowRight') spinStep(1);
  }});
}}

function showSpinFrame(idx) {{
  spinFrames.forEach(f => f.style.display = 'none');
  if (spinFrames[idx]) spinFrames[idx].style.display = 'block';
  spinCur = idx;
  updateFrameCounter();
  updateStripHighlight(idx);
  if (window.hotspotManager) window.hotspotManager.update(idx);
}}

function updateFrameCounter() {{
  document.getElementById('frameCounter').textContent = `${{spinCur + 1}} / ${{EXT_URLS.length}}`;
}}

function spinStep(dir) {{
  const n = ((spinCur + dir) % EXT_URLS.length + EXT_URLS.length) % EXT_URLS.length;
  showSpinFrame(n);
}}

// ── Zoom state ──────────────────────────────────────────────────────────────
let zoomLevel = 1, panX = 0, panY = 0;
let spinStartY = 0, panStartX = 0, panStartY = 0;
let pinchStartDist = 0, pinchStartZoom = 1, pinching = false;

function applyZoom() {{
  const z = zoomLevel;
  const limit = container_width() * (z - 1) / 2;
  panX = Math.max(-limit, Math.min(limit, panX));
  panY = Math.max(-limit, Math.min(limit, panY));
  spinFrames.forEach(f => {{
    f.style.transform = z > 1 ? `translate(${{panX}}px, ${{panY}}px) scale(${{z}})` : '';
  }});
  const zb = document.getElementById('zoomBadge');
  if (zb) {{ zb.textContent = z > 1 ? z.toFixed(1) + 'x' : ''; zb.style.display = z > 1 ? 'block' : 'none'; }}
}}

function spinWheel(e) {{
  e.preventDefault();
  stopAuto();
  const old = zoomLevel;
  zoomLevel = Math.max(1, Math.min(4, zoomLevel * (e.deltaY < 0 ? 1.18 : 0.85)));
  if (zoomLevel <= 1.02) {{ zoomLevel = 1; panX = 0; panY = 0; }}
  if (zoomLevel !== old) applyZoom();
}}

function spinMouseDown(e) {{
  stopAuto();
  spinDragging = true;
  spinStartX = e.clientX;
  spinStartY = e.clientY;
  panStartX = panX; panStartY = panY;
  spinStartFrame = spinCur;
  document.getElementById('spinHint').style.opacity = '0';
}}
function spinMouseMove(e) {{
  if (!spinDragging || !EXT_URLS.length) return;
  if (zoomLevel > 1) {{
    panX = panStartX + (e.clientX - spinStartX);
    panY = panStartY + (e.clientY - spinStartY);
    applyZoom();
    return;
  }}
  const dx = e.clientX - spinStartX;
  const delta = Math.round(dx / (container_width() / EXT_URLS.length));
  const n = ((spinStartFrame - delta) % EXT_URLS.length + EXT_URLS.length) % EXT_URLS.length;
  showSpinFrame(n);
}}
function spinMouseUp() {{ spinDragging = false; }}
function spinTouchStart(e) {{
  stopAuto();
  if (e.touches.length === 2) {{
    pinching = true;
    spinDragging = false;
    pinchStartDist = Math.hypot(
      e.touches[0].clientX - e.touches[1].clientX,
      e.touches[0].clientY - e.touches[1].clientY);
    pinchStartZoom = zoomLevel;
    return;
  }}
  spinDragging = true;
  spinStartX = e.touches[0].clientX;
  spinStartY = e.touches[0].clientY;
  panStartX = panX; panStartY = panY;
  spinStartFrame = spinCur;
  document.getElementById('spinHint').style.opacity = '0';
}}
function spinTouchMove(e) {{
  if (pinching && e.touches.length === 2) {{
    e.preventDefault();
    const d = Math.hypot(
      e.touches[0].clientX - e.touches[1].clientX,
      e.touches[0].clientY - e.touches[1].clientY);
    zoomLevel = Math.max(1, Math.min(4, pinchStartZoom * d / pinchStartDist));
    if (zoomLevel <= 1.02) {{ zoomLevel = 1; panX = 0; panY = 0; }}
    applyZoom();
    return;
  }}
  if (!spinDragging || !EXT_URLS.length) return;
  e.preventDefault();
  if (zoomLevel > 1) {{
    panX = panStartX + (e.touches[0].clientX - spinStartX);
    panY = panStartY + (e.touches[0].clientY - spinStartY);
    applyZoom();
    return;
  }}
  const dx = e.touches[0].clientX - spinStartX;
  const delta = Math.round(dx / (container_width() / EXT_URLS.length));
  const n = ((spinStartFrame - delta) % EXT_URLS.length + EXT_URLS.length) % EXT_URLS.length;
  showSpinFrame(n);
}}
function spinTouchEnd(e) {{
  if (e.touches.length < 2) pinching = false;
  if (e.touches.length === 0) spinDragging = false;
}}
function container_width() {{
  return document.getElementById('spin-container').offsetWidth || 800;
}}

function startAuto() {{
  if (autoRunning) return;
  autoRunning = true;
  document.getElementById('autoBtn').textContent = '⏸';
  var steps = 0, maxSteps = EXT_URLS.length * 2; // ~2 rotations then stop (no infinite spin)
  autoInterval = setInterval(() => {{
    spinStep(1);
    steps++;
    if (steps >= maxSteps) stopAuto();
  }}, 90);
}}
function stopAuto() {{
  if (!autoRunning) return;
  autoRunning = false;
  clearInterval(autoInterval);
  autoInterval = null;
  document.getElementById('autoBtn').textContent = '▶';
}}
function toggleAuto() {{
  autoRunning ? stopAuto() : startAuto();
}}

// ── Frame strip ─────────────────────────────────────────────────────────────
function buildFrameStrip() {{
  const strip = document.getElementById('frameStrip');
  EXT_URLS.forEach((url, i) => {{
    const thumb = document.createElement('div');
    thumb.className = 'frame-thumb' + (i === 0 ? ' active' : '');
    thumb.dataset.idx = i;
    thumb.innerHTML = `<img src="${{url}}" loading="lazy" alt="frame ${{i+1}}">`;
    thumb.onclick = () => {{ stopAuto(); showSpinFrame(i); }};
    strip.appendChild(thumb);
  }});
}}
function updateStripHighlight(idx) {{
  document.querySelectorAll('.frame-thumb').forEach((t, i) => {{
    t.classList.toggle('active', i === idx);
  }});
  const active = document.querySelector('.frame-thumb.active');
  if (active) active.scrollIntoView({{behavior:'smooth', block:'nearest', inline:'center'}});
}}

// ── Interior tab ────────────────────────────────────────────────────────────
let panoViewer = null, panoInited = false;
function initInterior() {{
  if (panoInited) return;
  panoInited = true;
  if (PANO_URLS.length > 0) {{
    // Equirectangular panorama with Pannellum
    document.getElementById('pano-container').style.display = 'block';
    document.getElementById('intGrid').style.display = 'none';
    const scenes = {{}};
    PANO_URLS.forEach((p, i) => {{
      scenes['scene' + i] = {{
        type: 'equirectangular',
        panorama: p.src,
        title: p.name,
        autoLoad: i === 0,
        hfov: 120,
        minHfov: 50,
        maxHfov: 150
      }};
    }});
    panoViewer = pannellum.viewer('pano-container', {{
      default: {{ firstScene: 'scene0', sceneFadeDuration: 1000, autoLoad: true }},
      scenes: scenes
    }});
    // Scene selector
    if (PANO_URLS.length > 1) {{
      const sel = document.createElement('select');
      sel.className = 'pano-scene-selector';
      PANO_URLS.forEach((p, i) => {{
        const opt = document.createElement('option');
        opt.value = 'scene' + i;
        opt.textContent = p.name;
        sel.appendChild(opt);
      }});
      sel.onchange = () => panoViewer.loadScene(sel.value);
      document.getElementById('pano-container').appendChild(sel);
    }}
  }} else if (INT_URLS.length > 0) {{
    // Fallback: photo grid
    document.getElementById('pano-container').style.display = 'none';
    const grid = document.getElementById('intGrid');
    grid.style.display = 'grid';
    INT_URLS.forEach(url => {{
      const div = document.createElement('div');
      div.className = 'int-thumb';
      div.innerHTML = `<img src="${{url}}" loading="lazy" alt="interior">`;
      div.onclick = () => openLB(url);
      grid.appendChild(div);
    }});
  }} else {{
    document.getElementById('pano-container').innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#8b949e;">No interior photos available</div>';
  }}
}}

// ── Gallery tab ─────────────────────────────────────────────────────────────
let galleryInited = false;
function initGallery() {{
  if (galleryInited) return;
  galleryInited = true;
  const grid = document.getElementById('galleryGrid');
  if (!GALLERY_URLS.length) {{
    grid.innerHTML = '<div style="padding:2rem;color:#8b949e;text-align:center;">No gallery photos available</div>';
    return;
  }}
  GALLERY_URLS.forEach((url, i) => {{
    const item = document.createElement('div');
    item.className = 'gallery-item';
    item.innerHTML = `<img src="${{url}}" loading="lazy" alt="photo ${{i+1}}">`;
    item.onclick = () => openLB(url);
    grid.appendChild(item);
  }});
}}

// ── Video tab ───────────────────────────────────────────────────────────────
let videoInited = false;
function initVideo() {{
  if (videoInited) return;
  videoInited = true;
  const container = document.getElementById('videoContainer');
  if (!VIDEO_URL) return;
  if (VIDEO_TYPE === 'youtube' || VIDEO_URL.includes('youtube.com') || VIDEO_URL.includes('youtu.be')) {{
    let vid = VIDEO_URL.includes('youtu.be/') ? VIDEO_URL.split('youtu.be/')[1].split('?')[0]
             : new URLSearchParams(new URL(VIDEO_URL).search).get('v');
    container.innerHTML = `<iframe src="https://www.youtube.com/embed/${{vid}}?autoplay=0&rel=0" allowfullscreen></iframe>`;
  }} else {{
    container.innerHTML = `<video controls src="${{VIDEO_URL}}" style="max-width:100%;max-height:100%;"></video>`;
  }}
}}

// ── Lead form ───────────────────────────────────────────────────────────────
async function submitLead(e) {{
  e.preventDefault();
  const btn = document.getElementById('submitBtn');
  btn.disabled = true;
  btn.textContent = 'Sending...';
  const fd = new FormData(e.target);
  const data = {{
    vin: VIN,
    name: fd.get('name'),
    email: fd.get('email'),
    phone: fd.get('phone') || '',
    message: fd.get('message') || '',
    wants_video: fd.get('wants_video') ? 1 : 0,
    wants_test_drive: fd.get('wants_test_drive') ? 1 : 0,
    source: 'viewer'
  }};
  try {{
    const res = await fetch('/api/leads', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(data)
    }});
    if (res.ok) {{
      document.getElementById('leadFormInner').innerHTML = `
        <div class="form-success">
          <div class="success-icon">&#x2705;</div>
          <h3>Inquiry Sent!</h3>
          <p>Thank you, ${{data.name}}. We'll contact you at ${{data.email}} shortly.</p>
        </div>`;
      trackEvent('lead_submitted', {{vin: VIN}});
    }} else {{
      btn.disabled = false;
      btn.textContent = 'Send Inquiry';
      showToast('Error sending inquiry. Please try again.');
    }}
  }} catch(err) {{
    btn.disabled = false;
    btn.textContent = 'Send Inquiry';
    showToast('Network error. Please try again.');
  }}
}}

// ── Hotspots ────────────────────────────────────────────────────────────────
(async () => {{
  let hotspotsData = [];
  try {{ hotspotsData = await fetch('/api/hotspots/' + VIN).then(r => r.json()); }} catch(e) {{}}
  if (!hotspotsData.length) return;
  const byFrame = {{}};
  hotspotsData.forEach(h => {{
    const key = h.photo || h.photo_filename;
    if (!byFrame[key]) byFrame[key] = [];
    byFrame[key].push(h);
  }});
  // Feature pills
  const uniqueTags = [...new Set(hotspotsData.map(h => h.tag))];
  if (uniqueTags.length) {{
    const pills = document.getElementById('featurePills');
    pills.style.display = 'flex';
    uniqueTags.forEach(tag => {{
      const pill = document.createElement('button');
      pill.className = 'feature-pill';
      pill.textContent = tag.replace(/_/g,' ').toUpperCase();
      pill.onclick = () => {{
        const entry = hotspotsData.find(h => h.tag === tag);
        if (entry) {{
          const fn = entry.photo || entry.photo_filename;
          const idx = EXT_URLS.findIndex(u => u.endsWith(fn));
          if (idx >= 0) {{ stopAuto(); showSpinFrame(idx); }}
        }}
        showToast(tag.replace(/_/g,' ').toUpperCase());
      }};
      pills.appendChild(pill);
    }});
  }}
  function renderHotspots(frameIdx) {{
    const container = document.getElementById('spin-container');
    container.querySelectorAll('.hotspot-dot,.hs-tooltip').forEach(d => d.remove());
    const frameName = EXT_URLS[frameIdx] ? EXT_URLS[frameIdx].split('/').pop() : '';
    const dots = byFrame[frameName] || [];
    dots.forEach(h => {{
      const dot = document.createElement('div');
      dot.className = 'hotspot-dot';
      dot.style.left = (h.x * 100) + '%';
      dot.style.top = (h.y * 100) + '%';
      dot.onclick = (ev) => {{
        ev.stopPropagation();
        container.querySelectorAll('.hs-tooltip').forEach(t => t.remove());
        const tip = document.createElement('div');
        tip.className = 'hs-tooltip';
        tip.style.left = (h.x * 100) + '%';
        tip.style.top = (h.y * 100) + '%';
        tip.textContent = h.tag.replace(/_/g,' ').toUpperCase();
        container.appendChild(tip);
        setTimeout(() => tip.remove(), 3500);
      }};
      container.appendChild(dot);
    }});
  }}
  window.hotspotManager = {{ update: renderHotspots }};
}})();

// ── Fullscreen ──────────────────────────────────────────────────────────────
function toggleFullscreen() {{
  const el = document.getElementById('viewArea');
  if (!document.fullscreenElement) {{
    el.requestFullscreen && el.requestFullscreen();
    document.getElementById('fsBtn').textContent = '⊠ Exit Fullscreen';
  }} else {{
    document.exitFullscreen && document.exitFullscreen();
    document.getElementById('fsBtn').textContent = '⛶ Fullscreen';
  }}
}}

// ── Share panel ─────────────────────────────────────────────────────────────
function toggleShare() {{
  document.getElementById('sharePanel').classList.toggle('open');
}}
function copyShareUrl() {{
  navigator.clipboard.writeText(document.getElementById('shareUrlInput').value)
    .then(() => showToast('Link copied!'));
}}
function copyEmbed() {{
  const code = document.getElementById('embedCodeBlock').textContent;
  navigator.clipboard.writeText(code).then(() => showToast('Embed code copied!'));
}}
function shareWhatsApp() {{
  window.open('https://wa.me/?text=' + encodeURIComponent(document.getElementById('shareUrlInput').value), '_blank');
}}
function shareFacebook() {{
  window.open('https://www.facebook.com/sharer/sharer.php?u=' + encodeURIComponent(document.getElementById('shareUrlInput').value), '_blank');
}}
function shareEmail() {{
  window.location.href = 'mailto:?subject=Check out this vehicle&body=' + encodeURIComponent(document.getElementById('shareUrlInput').value);
}}
document.addEventListener('click', e => {{
  const panel = document.getElementById('sharePanel');
  if (panel.classList.contains('open') && !panel.contains(e.target) && !e.target.closest('[onclick="toggleShare()"]')) {{
    panel.classList.remove('open');
  }}
}});

// ── Lightbox ────────────────────────────────────────────────────────────────
function openLB(src) {{
  document.getElementById('lightbox-img').src = src;
  document.getElementById('lightbox').classList.add('open');
}}
function closeLB() {{
  document.getElementById('lightbox').classList.remove('open');
}}
document.getElementById('lightbox').addEventListener('click', e => {{
  if (e.target === document.getElementById('lightbox')) closeLB();
}});

// ── Analytics beacon ────────────────────────────────────────────────────────
function trackEvent(event_type, data={{}}) {{
  fetch('/api/analytics/event', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{vin: VIN, event_type, data, ts: Date.now()}})
  }}).catch(() => {{}});
}}

// ── Init ─────────────────────────────────────────────────────────────────────
initSpin();
trackEvent('viewer_open', {{vin: VIN}});

// ── Background Selector ─────────────────────────────────────────────────────
let activeBgSlug = '{vehicle.get("background_id", "") or ""}';

async function loadBackgrounds() {{
  const grid = document.getElementById('bgGrid');
  if (!grid) return;
  try {{
    const res = await fetch('/api/backgrounds');
    const bgs = await res.json();
    if (!bgs.length) {{ grid.innerHTML = '<span style="color:#8b949e;font-size:0.75rem;">No backgrounds</span>'; return; }}
    grid.innerHTML = '';
    bgs.forEach(bg => {{
      if (bg.slug === 'none') {{
        const el = document.createElement('div');
        el.className = 'bg-thumb-none' + (activeBgSlug === 'none' ? ' active' : '');
        el.textContent = 'None';
        el.onclick = () => selectBg('none', el);
        grid.appendChild(el);
      }} else if (bg.thumbnail_url) {{
        const img = document.createElement('img');
        img.className = 'bg-thumb' + (activeBgSlug === bg.slug ? ' active' : '');
        img.src = bg.thumbnail_url;
        img.title = bg.name;
        img.alt = bg.name;
        img.onerror = () => img.style.display = 'none';
        img.onclick = () => selectBg(bg.slug, img);
        grid.appendChild(img);
      }}
    }});
  }} catch(e) {{
    grid.innerHTML = '<span style="color:#8b949e;font-size:0.75rem;">Unavailable</span>';
  }}
}}

async function selectBg(slug, el) {{
  // Update active state UI
  document.querySelectorAll('.bg-thumb,.bg-thumb-none').forEach(e => e.classList.remove('active'));
  el.classList.add('active');
  activeBgSlug = slug;

  // Save to server
  try {{
    const fd = new FormData();
    fd.append('background_slug', slug);
    fd.append('turntable_style', slug === 'none' ? 'none' : 'studio_shadow');
    await fetch('/api/vehicle/' + VIN + '/background', {{method:'POST', body:fd}});
    showToast('Background updated. Re-finalize to apply to frames.');
  }} catch(e) {{
    showToast('Failed to save background.');
  }}
}}

// Load backgrounds on init
loadBackgrounds();
</script>
</body>
</html>"""
    return HTMLResponse(html)

# =============================================================================
# ROUTES — EMBED (iframe-ready, minimal chrome)
# =============================================================================
@app.get("/embed/{vin}")
async def embed_vehicle(vin: str):
    vin = vin.upper()
    vehicle_dir = STORAGE_DIR / vin
    
    if not vehicle_dir.exists():
        return HTMLResponse("<h1>Vehicle not found</h1>", status_code=404)
    
    # Prefer spin directory (interpolated frames), fallback to exterior
    spin_dir = vehicle_dir / "spin"
    if spin_dir.exists() and any(spin_dir.glob("spin_*.jpg")):
        ext_frames = sorted([f.name for f in spin_dir.glob("spin_*.jpg")])
        frame_path_prefix = f"/storage/{vin}/spin"
    else:
        ext_frames = sorted([f.name for f in (vehicle_dir / "exterior").glob("*.jpg")]) if (vehicle_dir / "exterior").exists() else []
        frame_path_prefix = f"/storage/{vin}/exterior"
    
    int_photos = sorted([f.name for f in (vehicle_dir / "interior").glob("*.jpg")]) if (vehicle_dir / "interior").exists() else []
    
    info = db_get_vehicle(vin) or {"vin": vin, "year": "", "make": "", "model": ""}
    title = f"{info.get('year','')} {info.get('make','')} {info.get('model','')}".strip() or vin
    
    frame_tags = "\n".join(
        f'<img src="{frame_path_prefix}/{f}" style="display:none" data-idx="{i}">'
        for i, f in enumerate(ext_frames)
    )
    interior_tags = "\n".join(
        f'<div style="border-radius:4px;overflow:hidden;"><img src="/storage/{vin}/interior/{p}" loading="lazy" style="width:100%;height:100%;object-fit:cover;"></div>'
        for p in int_photos
    )
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Playfair+Display:wght@700&display=swap');
        * {{ margin:0; padding:0; box-sizing:border-box; }}
        body {{ font-family:'Inter',sans-serif; background:#0a1628; color:#f5f0e8; overflow-x:hidden; }}
        .tabs {{ display:flex; background:#0f2040; border-bottom:1px solid #1a2e50; }}
        .tab {{ flex:1; padding:0.7rem; background:transparent; border:none; color:#a0aab4; cursor:pointer; text-align:center; font-size:0.9rem; font-family:'Inter',sans-serif; }}
        .tab.active {{ color:#c9a84c; box-shadow:inset 0 -2px 0 #c9a84c; }}
        .tc {{ display:none; }} .tc.active {{ display:block; }}
        #spin {{ width:100%; height:420px; cursor:grab; position:relative; background:#0a1628; touch-action:none; user-select:none; }}
        #spin:active {{ cursor:grabbing; }}
        #spin img {{ width:100%; height:100%; object-fit:contain; position:absolute; inset:0; pointer-events:none; }}
        .hint {{ position:absolute; bottom:0.6rem; left:50%; transform:translateX(-50%); background:rgba(15,32,64,0.85); padding:0.3rem 0.8rem; border-radius:12px; font-size:0.75rem; color:#a0aab4; border:1px solid #1a2e50; }}
        .ig {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(140px,1fr)); gap:0.5rem; padding:0.5rem; }}
        .ig div {{ aspect-ratio:16/10; }}
        .brand {{ text-align:center; padding:0.4rem; font-size:0.7rem; color:#a0aab4; }}
        @media(max-width:600px) {{ #spin {{ height:260px; }} }}
    </style>
</head>
<body>
    <div class="tabs">
        <button class="tab active" onclick="st('e')">Exterior 360&deg;</button>
        <button class="tab" onclick="st('i')">Interior</button>
    </div>
    <div id="e" class="tc active">
        <div id="spin">
            {frame_tags}
            <div class="hint" id="hint">&larr; Drag to spin &rarr;</div>
        </div>
    </div>
    <div id="i" class="tc">
        <div class="ig">{interior_tags}</div>
    </div>
    <div class="brand">Powered by LotVue Pro</div>
    <script>
        const F=document.querySelectorAll('#spin img');
        let c=0,d=false,sx=0,sf=0,ai=null;
        // Preload
        let ld=0;
        F.forEach(img=>{{const p=new Image();p.onload=()=>{{ld++;if(ld===F.length){{if(F.length)F[0].style.display='block';ar();}}}};p.onerror=()=>ld++;p.src=img.src;}});
        function ar(){{let n=0;ai=setInterval(()=>{{c=(c+1)%F.length;sh();n++;if(n>=F.length){{clearInterval(ai);ai=null;}}}},70);}}
        function sa(){{if(ai){{clearInterval(ai);ai=null;}}}}
        function sh(){{F.forEach(f=>f.style.display='none');if(F[c])F[c].style.display='block';}}
        const S=document.getElementById('spin');
        S.onmousedown=e=>{{sa();d=true;sx=e.clientX;sf=c;document.getElementById('hint').style.display='none';}};
        S.onmousemove=e=>{{if(!d)return;const dx=e.clientX-sx,fd=Math.round(dx/15);if(fd){{c=((sf-fd)%F.length+F.length)%F.length;sh();}}}};
        S.onmouseup=()=>d=false;S.onmouseleave=()=>d=false;
        S.ontouchstart=e=>{{sa();d=true;sx=e.touches[0].clientX;sf=c;document.getElementById('hint').style.display='none';}};
        S.ontouchmove=e=>{{if(!d)return;const dx=e.touches[0].clientX-sx,fd=Math.round(dx/15);if(fd){{c=((sf-fd)%F.length+F.length)%F.length;sh();}}}};
        S.ontouchend=()=>d=false;
        function st(id){{document.querySelectorAll('.tab').forEach((t,i)=>t.classList.toggle('active',id==='e'?i===0:i===1));document.querySelectorAll('.tc').forEach(t=>t.classList.remove('active'));document.getElementById(id).classList.add('active');}}
    </script>
</body>
</html>"""
    return HTMLResponse(html)


# =============================================================================
# ROUTES — DEALER SETTINGS
# =============================================================================
@app.get("/settings")
async def dealer_settings():
    dealer = db_get_dealer("default") or {"name": "default", "display_name": "", "phone": "", "email": "", "address": "", "website": "", "logo_url": "", "brand_color": "#58a6ff"}
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dealer Settings — LotVue Pro</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: 'Segoe UI', -apple-system, sans-serif; background: #f0f4f8; color: #1a2332; min-height: 100vh; }}
        .header {{ background: linear-gradient(135deg, #e8edf2 0%, #dde3ea 100%); padding: 1.2rem 2rem; border-bottom: 1px solid #c5cdd6; display: flex; justify-content: space-between; align-items: center; }}
        .logo {{ font-size: 1.4rem; font-weight: 700; color: {dealer.get('brand_color', '#58a6ff')}; }}
        .container {{ max-width: 800px; margin: 0 auto; padding: 2rem; }}
        .card {{ background: #dde3ea; border: 1px solid #c5cdd6; border-radius: 8px; padding: 1.5rem; margin-bottom: 1.5rem; }}
        .card h2 {{ color: #1a2332; margin-bottom: 1rem; font-size: 1.2rem; }}
        .form-group {{ margin-bottom: 1rem; }}
        .form-group label {{ display: block; color: #8b949e; margin-bottom: 0.4rem; font-size: 0.85rem; }}
        .form-group input {{ width: 100%; padding: 0.7rem; background: #010409; border: 1px solid #c5cdd6; border-radius: 6px; color: #1a2332; font-size: 1rem; }}
        .form-group input:focus {{ outline: none; border-color: #58a6ff; }}
        .btn {{ background: #238636; border: none; padding: 0.7rem 1.4rem; color: white; border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 1rem; }}
        .btn:hover {{ background: #2ea043; }}
        .btn-outline {{ background: transparent; border: 1px solid #b0bac5; color: #8b949e; }}
        .color-picker {{ display: flex; gap: 0.5rem; align-items: center; }}
        .color-picker input[type="color"] {{ width: 50px; height: 40px; border: none; border-radius: 4px; cursor: pointer; }}
        .color-picker input[type="text"] {{ flex: 1; }}
        .showroom-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 1rem; }}
        .showroom-thumb {{ aspect-ratio: 16/9; border-radius: 6px; overflow: hidden; border: 2px solid #c5cdd6; cursor: pointer; }}
        .showroom-thumb.selected {{ border-color: #58a6ff; }}
        .showroom-thumb img {{ width: 100%; height: 100%; object-fit: cover; }}
        .nav {{ display: flex; gap: 1rem; margin-bottom: 1rem; }}
        .nav a {{ color: #58a6ff; text-decoration: none; }}
        .success-msg {{ background: #238636; color: white; padding: 0.7rem 1rem; border-radius: 6px; margin-bottom: 1rem; display: none; }}
    </style>
</head>
<body>
    <div class="header">
        <div class="logo">LotVue Pro — Settings</div>
        <a href="/" style="color:#58a6ff;text-decoration:none;">&larr; Dashboard</a>
    </div>
    <div class="container">
        <div class="nav">
            <a href="/settings">&larr; Back to Dashboard</a>
        </div>
        
        <div class="success-msg" id="successMsg">Settings saved!</div>
        
        <div class="card">
            <h2>🏢 Dealer Information</h2>
            <form id="dealerForm">
                <div class="form-group">
                    <label>Display Name</label>
                    <input type="text" name="display_name" value="{dealer.get('display_name', '')}" placeholder="Your Dealership Name">
                </div>
                <div class="form-group">
                    <label>Phone</label>
                    <input type="tel" name="phone" value="{dealer.get('phone', '')}" placeholder="(555) 123-4567">
                </div>
                <div class="form-group">
                    <label>Email</label>
                    <input type="email" name="email" value="{dealer.get('email', '')}" placeholder="sales@dealership.com">
                </div>
                <div class="form-group">
                    <label>Address</label>
                    <input type="text" name="address" value="{dealer.get('address', '')}" placeholder="123 Main St, City, ST 12345">
                </div>
                <div class="form-group">
                    <label>Website</label>
                    <input type="url" name="website" value="{dealer.get('website', '')}" placeholder="https://www.dealership.com">
                </div>
                <div class="form-group">
                    <label>Brand Color</label>
                    <div class="color-picker">
                        <input type="color" name="brand_color" value="{dealer.get('brand_color', '#58a6ff')}">
                        <input type="text" name="brand_color_text" value="{dealer.get('brand_color', '#58a6ff')}" pattern="^#[0-9A-Fa-f]{{6}}$">
                    </div>
                </div>
                <button type="submit" class="btn">Save Settings</button>
            </form>
        </div>
    </div>
    
    <script>
        document.getElementById('dealerForm').onsubmit = async e => {{
            e.preventDefault();
            const fd = new FormData(e.target);
            await fetch('/api/dealer', {{ method: 'POST', body: fd }});
            document.getElementById('successMsg').style.display = 'block';
            setTimeout(() => document.getElementById('successMsg').style.display = 'none', 3000);
        }};
        
        const colorInput = document.querySelector('input[name="brand_color"]');
        const colorText = document.querySelector('input[name="brand_color_text"]');
        colorInput.oninput = () => colorText.value = colorInput.value;
        colorText.oninput = () => colorInput.value = colorText.value;
    </script>
</body>
</html>"""
    return HTMLResponse(html)


# =============================================================================
# API — VEHICLE WORKFLOW STATUS
# =============================================================================
VALID_WORKFLOW_STATUSES = ["intake", "photos", "processing", "review", "published"]

@app.put("/api/vehicle/{vin}/workflow")
async def update_workflow_status(vin: str, request: Request):
    """Update vehicle workflow status: intake → photos → processing → review → published"""
    body = await request.json()
    new_status = body.get("status", "").lower()
    if new_status not in VALID_WORKFLOW_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {VALID_WORKFLOW_STATUSES}")
    vehicle = db_get_vehicle(vin)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE vehicles SET workflow_status=? WHERE vin=?", (new_status, vin))
    conn.commit()
    conn.close()
    return {"status": "ok", "vin": vin, "workflow_status": new_status}


# =============================================================================
# API — DEALER
# =============================================================================
@app.post("/api/dealer")
async def save_dealer(
    display_name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    address: str = Form(""),
    website: str = Form(""),
    brand_color: str = Form("#58a6ff")
):
    db_upsert_dealer("default", display_name=display_name, phone=phone, email=email, address=address, website=website, brand_color=brand_color)
    return {"status": "ok"}


@app.get("/api/dealer")
async def get_dealer():
    dealer = db_get_dealer("default")
    return dealer or {"name": "default", "display_name": "", "brand_color": "#58a6ff"}


@app.get("/api/vehicles")
async def get_vehicles_api():
    vehicles = db_get_vehicles()
    return vehicles


# =============================================================================
# API — DELETE
# =============================================================================
@app.delete("/api/vehicle/{vin}")
async def delete_vehicle(vin: str):
    vin = vin.upper()
    vehicle_dir = STORAGE_DIR / vin
    
    if vehicle_dir.exists():
        shutil.rmtree(vehicle_dir)
    
    db_delete_vehicle(vin)
    return {"status": "ok"}


# =============================================================================
# API — UPDATE VEHICLE METADATA (price, mileage, color)
# =============================================================================
@app.post("/api/vehicle/{vin}/meta")
async def update_vehicle_meta(
    vin: str,
    price: str = Form(""),
    mileage: str = Form(""),
    color: str = Form(""),
):
    vin = vin.upper()
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "UPDATE vehicles SET price=?, mileage=?, color=? WHERE vin=?",
        (price, mileage, color, vin)
    )
    conn.commit()
    conn.close()
    return {"status": "ok", "vin": vin}


# =============================================================================
# =============================================================================
# ROUTES — PHASE 2B: VIN DECODER, BACKGROUNDS API, AUTO-ALIGNMENT
# =============================================================================

@app.get("/api/vin/{vin}/decode")
async def decode_vin(vin: str):
    """Decode VIN using NHTSA vPIC API (free). Returns year/make/model/trim/engine/transmission/etc."""
    vin = vin.upper().strip()
    if len(vin) < 11:
        raise HTTPException(status_code=400, detail="VIN must be at least 11 characters")

    try:
        import urllib.request
        url = f"https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{vin}?format=json"
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read().decode())

        if not data.get("Results"):
            return {"status": "error", "message": "No results from NHTSA"}

        r = data["Results"][0]

        def clean(val):
            return val.strip() if val and val.strip() not in ["", "0", "Not Applicable"] else ""

        decoded = {
            "vin": vin,
            "year": clean(r.get("ModelYear", "")),
            "make": clean(r.get("Make", "")),
            "model": clean(r.get("Model", "")),
            "trim_level": clean(r.get("Trim", "")),
            "body_style": clean(r.get("BodyClass", "")),
            "engine": clean(r.get("DisplacementL", "")) + ("L " if clean(r.get("DisplacementL", "")) else "") + clean(r.get("EngineCylinders", "")) + (" cyl" if clean(r.get("EngineCylinders", "")) else ""),
            "transmission": clean(r.get("TransmissionStyle", "")),
            "drivetrain": clean(r.get("DriveType", "")),
            "fuel_type": clean(r.get("FuelTypePrimary", "")),
            "doors": clean(r.get("Doors", "")),
            "plant_country": clean(r.get("PlantCountry", "")),
            "error_code": clean(r.get("ErrorCode", "")),
        }

        # Clean up engine string
        decoded["engine"] = decoded["engine"].strip()

        # Auto-save to DB if VIN exists
        existing = db_get_vehicle(vin)
        if existing:
            conn = sqlite3.connect(str(DB_PATH))
            conn.execute("""
                UPDATE vehicles SET
                    year=CASE WHEN ? != '' THEN ? ELSE year END,
                    make=CASE WHEN ? != '' THEN ? ELSE make END,
                    model=CASE WHEN ? != '' THEN ? ELSE model END,
                    trim_level=CASE WHEN ? != '' THEN ? ELSE trim_level END,
                    body_style=CASE WHEN ? != '' THEN ? ELSE body_style END,
                    engine=CASE WHEN ? != '' THEN ? ELSE engine END,
                    transmission=CASE WHEN ? != '' THEN ? ELSE transmission END,
                    drivetrain=CASE WHEN ? != '' THEN ? ELSE drivetrain END,
                    fuel_type=CASE WHEN ? != '' THEN ? ELSE fuel_type END
                WHERE vin=?
            """, (
                decoded['year'], decoded['year'],
                decoded['make'], decoded['make'],
                decoded['model'], decoded['model'],
                decoded['trim_level'], decoded['trim_level'],
                decoded['body_style'], decoded['body_style'],
                decoded['engine'], decoded['engine'],
                decoded['transmission'], decoded['transmission'],
                decoded['drivetrain'], decoded['drivetrain'],
                decoded['fuel_type'], decoded['fuel_type'],
                vin
            ))
            conn.commit()
            conn.close()

        # Auto-calculate market pricing
        pricing = None
        try:
            from ai_studio.market_pricing import estimate_market_price
            price_input = {**decoded}
            if existing:
                price_input["price"] = existing.get("price", 0)
                price_input["mileage"] = existing.get("mileage", 0)
            pricing = estimate_market_price(price_input)
        except Exception as pe:
            print(f"[VIN Decode] Market pricing failed: {pe}")

        return {"status": "ok", "decoded": decoded, "pricing": pricing}

    except Exception as e:
        # Return partial data if NHTSA is unreachable
        print(f"[VIN Decode] Error for {vin}: {e}")
        return {"status": "error", "message": str(e), "decoded": {}}


@app.get("/api/backgrounds")
async def get_backgrounds(category: str = None):
    """Return the backgrounds library (system + dealer custom)."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    if category:
        rows = conn.execute(
            "SELECT * FROM backgrounds WHERE is_active=1 AND category=? ORDER BY is_system DESC, name",
            (category,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM backgrounds WHERE is_active=1 ORDER BY is_system DESC, category, name"
        ).fetchall()
    conn.close()
    return [{k: row[k] for k in row.keys()} for row in rows]


@app.post("/api/vehicle/{vin}/background")
async def set_vehicle_background(
    vin: str,
    background_slug: str = Form(""),
    turntable_style: str = Form("studio_shadow"),
):
    """Set the background and turntable style for a vehicle."""
    vin = vin.upper()
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "UPDATE vehicles SET background_id=?, turntable_style=? WHERE vin=?",
        (background_slug, turntable_style, vin)
    )
    conn.commit()
    conn.close()
    return {"status": "ok", "vin": vin, "background_slug": background_slug, "turntable_style": turntable_style}


# =============================================================================
# ROUTES — PHASE 2A: VIEWER DATA, LEADS, ANALYTICS
# =============================================================================
# =============================================================================

@app.get("/api/vehicles/{vin}/viewer")
async def get_vehicle_viewer_data(vin: str):
    """Returns all data needed to hydrate the 5-tab viewer widget."""
    vin = vin.upper()
    vehicle_dir = STORAGE_DIR / vin
    if not vehicle_dir.exists():
        raise HTTPException(status_code=404, detail="Vehicle not found")

    # Exterior frames
    spin_dir = vehicle_dir / "spin"
    if spin_dir.exists() and any(spin_dir.glob("spin_*.jpg")):
        ext_frames = sorted([f.name for f in spin_dir.glob("spin_*.jpg")])
        frame_prefix = f"/storage/{vin}/spin"
    else:
        ext_dir = vehicle_dir / "exterior"
        ext_frames = sorted([f.name for f in ext_dir.glob("*.jpg")]) if ext_dir.exists() else []
        frame_prefix = f"/storage/{vin}/exterior"
    ext_urls = [f"{frame_prefix}/{f}" for f in ext_frames]

    # Interior photos
    int_dir = vehicle_dir / "interior"
    int_photos = sorted([f.name for f in int_dir.glob("*.jpg")]) if int_dir.exists() else []
    int_urls = [f"/storage/{vin}/interior/{f}" for f in int_photos]

    # Interior panoramas
    pano_dir = vehicle_dir / "panoramas"
    pano_files = []
    if pano_dir.exists():
        for ext in ["*.jpg", "*.jpeg", "*.png"]:
            pano_files += sorted([f.name for f in pano_dir.glob(ext)])
    pano_urls = [{"src": f"/storage/{vin}/panoramas/{f}", "name": f"Scene {i+1}"} for i, f in enumerate(pano_files)]

    # Gallery photos
    gallery_dir = vehicle_dir / "gallery"
    gallery_files = []
    if gallery_dir.exists():
        for ext in ["*.jpg", "*.jpeg", "*.png"]:
            gallery_files += sorted([f.name for f in gallery_dir.glob(ext)])
    gallery_urls = [f"/storage/{vin}/gallery/{f}" for f in gallery_files]
    if not gallery_urls:
        gallery_urls = ext_urls  # fallback to exterior frames

    # DB info
    info = db_get_vehicle(vin) or {"vin": vin, "year": "", "make": "", "model": ""}

    return {
        "vin": vin,
        "title": f"{info.get('year','')} {info.get('make','')} {info.get('model','')}".strip() or vin,
        "year": info.get("year", ""),
        "make": info.get("make", ""),
        "model": info.get("model", ""),
        "price": info.get("price", ""),
        "mileage": info.get("mileage", ""),
        "color": info.get("color", ""),
        "video_url": info.get("video_url", ""),
        "video_type": info.get("video_type", ""),
        "dealer_logo_url": info.get("dealer_logo_url", ""),
        "exterior_frames": ext_urls,
        "interior_photos": int_urls,
        "interior_panoramas": pano_urls,
        "gallery_photos": gallery_urls,
        "has_interior": len(int_urls) > 0 or len(pano_urls) > 0,
        "has_gallery": len(gallery_urls) > 0,
        "has_video": bool(info.get("video_url", "")),
        "frame_count": len(ext_urls),
    }


# =============================================================================
# ROUTE — CINEMATIC 3D VIEWER (Module 3)
# =============================================================================
@app.get("/view3d/{vin}")
async def view3d(vin: str):
    """Cinematic Three.js 360 viewer for a vehicle."""
    info = db_get_vehicle(vin)
    if not info:
        raise HTTPException(status_code=404, detail="Vehicle not found")

    # Check for studio photos first, fall back to raw exterior
    studio_dir = STORAGE_DIR / vin / "studio"
    exterior_dir = STORAGE_DIR / vin / "exterior"

    if studio_dir.exists() and list(studio_dir.glob("*.jpg")):
        frame_urls = [f"/storage/{vin}/studio/{f.name}" for f in sorted(studio_dir.glob("*.jpg"))]
    elif exterior_dir.exists():
        frame_urls = [f"/storage/{vin}/exterior/{f.name}" for f in sorted(exterior_dir.glob("*.jpg"))]
    else:
        frame_urls = []

    year_make_model = f"{info.get('year','')} {info.get('make','')} {info.get('model','')}".strip()
    price = info.get('price', '')
    mileage = info.get('mileage', '')

    frames_json = json.dumps(frame_urls)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{year_make_model or vin} — LotVue Pro 3D Viewer</title>
    <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * {{ margin:0; padding:0; box-sizing:border-box; }}
        body {{ font-family:'Inter',sans-serif; background:#0a1628; color:#f5f0e8; }}
        .viewer-header {{
            background:linear-gradient(135deg,#0f2040 0%,#0a1628 100%);
            padding:0.8rem 1.5rem; display:flex; justify-content:space-between; align-items:center;
            border-bottom:1px solid #1a2e50;
        }}
        .viewer-header .logo {{ font-family:'Playfair Display',serif; color:#c9a84c; font-weight:700; font-size:1.1rem; }}
        .viewer-header .vehicle-info {{ color:rgba(245,240,232,0.9); font-size:0.9rem; }}
        #viewer-container {{
            width:100%; height:calc(100vh - 120px); min-height:400px;
            background:#0a1628; position:relative;
        }}
        .info-bar {{
            background:#0f2040; border-top:1px solid #1a2e50; padding:0.8rem 1.5rem;
            display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:0.5rem;
        }}
        .info-bar .title {{ font-weight:700; font-size:1.1rem; color:#f5f0e8; }}
        .info-bar .meta {{ color:#a0aab4; font-size:0.85rem; }}
        .info-bar .price {{ color:#c9a84c; font-weight:700; font-size:1.2rem; }}
        .drag-hint {{
            position:absolute; bottom:20px; left:50%; transform:translateX(-50%);
            background:rgba(15,32,64,0.85); color:#f5f0e8; padding:6px 16px;
            border-radius:20px; font-size:12px; pointer-events:none; z-index:5;
            opacity:1; transition:opacity 1s; border:1px solid #1a2e50;
        }}
        .frame-counter {{
            position:absolute; top:12px; right:12px;
            background:rgba(201,168,76,0.85); color:#0a1628; padding:4px 10px;
            border-radius:12px; font-size:11px; font-weight:600; z-index:5;
        }}
    </style>
</head>
<body>
    <div class="viewer-header">
        <div class="logo">LotVue Pro</div>
        <div class="vehicle-info">{year_make_model}</div>
    </div>
    <div id="viewer-container">
        <div class="frame-counter" id="frameCounter">1 / {len(frame_urls)}</div>
        <div class="drag-hint" id="dragHint">Drag to rotate &bull; Scroll to zoom</div>
    </div>
    <div class="info-bar">
        <div>
            <div class="title">{year_make_model or 'Vehicle'}</div>
            <div class="meta">{f'{mileage} miles' if mileage else ''} {f'&bull; VIN: ...{vin[-6:]}' if len(vin)>=6 else ''}</div>
        </div>
        {f'<div class="price">${price}</div>' if price else ''}
    </div>

    <script src="/static/viewer3d.js"></script>
    <script>
        const frames = {frames_json};
        const container = document.getElementById('viewer-container');
        const viewer = new LotVueViewer(container, frames, {{
            cinematicIntro: true,
            autoRotate: true,
            showFloor: true,
        }});

        // Frame counter — poll from animation loop
        setInterval(() => {{
            document.getElementById('frameCounter').textContent =
                (viewer.currentFrame + 1) + ' / ' + frames.length;
        }}, 100);

        // Hide drag hint after 4 seconds
        setTimeout(() => {{
            const hint = document.getElementById('dragHint');
            if (hint) hint.style.opacity = '0';
        }}, 4000);
    </script>
</body>
</html>"""
    return HTMLResponse(html)


from pydantic import BaseModel as _BaseModel
from typing import Optional as _Optional

class LeadSubmission(_BaseModel):
    vin: str
    name: str
    email: str
    phone: _Optional[str] = ""
    message: _Optional[str] = ""
    wants_video: _Optional[int] = 0
    wants_test_drive: _Optional[int] = 0
    source: _Optional[str] = "viewer"

@app.post("/api/leads")
async def submit_lead(lead: LeadSubmission):
    """Receives a lead inquiry from the viewer widget and stores it."""
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.execute("""
        INSERT INTO leads (vin, name, email, phone, message, wants_video, wants_test_drive, source, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        lead.vin.upper(), lead.name, lead.email, lead.phone or "",
        lead.message or "", lead.wants_video or 0, lead.wants_test_drive or 0,
        lead.source or "viewer", datetime.now().isoformat()
    ))
    lead_id = cur.lastrowid
    conn.commit()
    conn.close()
    print(f"[Lead] New inquiry: {lead.name} <{lead.email}> for VIN {lead.vin}")
    try:
        from a2_extensions import notify_lead
        notify_lead({"id": lead_id, "vin": lead.vin.upper(), "name": lead.name,
                     "email": lead.email, "phone": lead.phone or "", "message": lead.message or "",
                     "wants_video": lead.wants_video, "wants_test_drive": lead.wants_test_drive})
    except Exception as e:
        print(f"[Lead-Notify] hook error (lead still stored): {e}")
    return {"status": "ok", "message": "Inquiry received", "lead_id": lead_id}


@app.get("/api/leads")
async def get_leads(vin: str = None):
    """Returns all leads, optionally filtered by VIN."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    if vin:
        rows = conn.execute("SELECT * FROM leads WHERE vin=? ORDER BY created_at DESC", (vin.upper(),)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM leads ORDER BY created_at DESC LIMIT 200").fetchall()
    conn.close()
    return [{k: row[k] for k in row.keys()} for row in rows]


class AnalyticsEvent(_BaseModel):
    vin: str
    event_type: str
    data: dict = {}
    ts: _Optional[int] = None

@app.post("/api/analytics/event")
async def track_analytics_event(event: AnalyticsEvent):
    """Lightweight analytics beacon — logs viewer events to console and future DB."""
    print(f"[Analytics] {event.event_type} | VIN={event.vin} | {event.data}")
    return {"status": "ok"}


# =============================================================================
# API — AI VIDEO WALKAROUND (Module 4)
# =============================================================================
@app.post("/api/vehicle/{vin}/walkaround")
async def generate_vehicle_walkaround(vin: str):
    """Start AI video walkaround generation for a vehicle."""
    info = db_get_vehicle(vin)
    if not info:
        raise HTTPException(status_code=404, detail="Vehicle not found")

    from ai_studio.video_walkaround import generate_walkaround, get_walkaround_status

    status = get_walkaround_status(vin)
    if status.get("status") == "processing":
        return {"status": "already_running", "progress": status.get("progress", 0)}

    def _run():
        generate_walkaround(vin, info)

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    return {"status": "started", "vin": vin}


@app.get("/api/vehicle/{vin}/walkaround-status")
async def walkaround_status(vin: str):
    """Get walkaround generation status."""
    from ai_studio.video_walkaround import get_walkaround_status
    status = get_walkaround_status(vin)

    # Also check if video already exists
    video_path = STORAGE_DIR / vin / "walkaround" / "walkaround.mp4"
    if video_path.exists() and status.get("status") != "processing":
        size_mb = round(video_path.stat().st_size / (1024 * 1024), 1)
        return {
            "status": "done",
            "url": f"/storage/{vin}/walkaround/walkaround.mp4",
            "size_mb": size_mb,
        }

    return status


# =============================================================================
# ROUTES — MARKET PRICING INTELLIGENCE
# =============================================================================

@app.get("/api/vehicle/{vin}/market-price")
async def get_market_price(vin: str):
    """Estimate market value for a vehicle based on specs."""
    from ai_studio.market_pricing import estimate_market_price

    vehicle = db_get_vehicle(vin)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")

    pricing = estimate_market_price(vehicle)
    return {"status": "ok", "vin": vin, "pricing": pricing}


@app.post("/api/vehicle/{vin}/set-price")
async def set_vehicle_price(vin: str, request: Request):
    """Set asking price and get updated market comparison."""
    from ai_studio.market_pricing import estimate_market_price

    body = await request.json()
    price = body.get("price", 0)

    vehicle = db_get_vehicle(vin)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")

    # Update price in DB
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE vehicles SET price=? WHERE vin=?", (price, vin))
    conn.commit()
    conn.close()

    # Re-fetch and price
    vehicle = db_get_vehicle(vin)
    pricing = estimate_market_price(vehicle)
    return {"status": "ok", "vin": vin, "price": price, "pricing": pricing}


# =============================================================================
# ROUTES — LISTING SYNDICATION
# =============================================================================

@app.get("/api/syndication/facebook-csv")
async def syndication_facebook():
    """Export all published vehicles as Facebook Marketplace catalog CSV."""
    from ai_studio.listing_syndication import export_facebook_csv

    vehicles = db_get_vehicles()
    published = [v for v in vehicles if v.get("workflow_status") == "published" or v.get("status") == "active"]
    if not published:
        published = vehicles  # fallback: export all if none published

    csv_data = export_facebook_csv(published)
    return Response(content=csv_data, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=facebook_catalog.csv"})


@app.get("/api/syndication/xml-feed")
async def syndication_xml():
    """Export vehicles as XML feed for CarGurus/AutoTrader/Cars.com."""
    from ai_studio.listing_syndication import export_xml_feed

    vehicles = db_get_vehicles()
    published = [v for v in vehicles if v.get("workflow_status") == "published" or v.get("status") == "active"]
    if not published:
        published = vehicles

    xml_data = export_xml_feed(published)
    return Response(content=xml_data, media_type="application/xml",
                    headers={"Content-Disposition": "attachment; filename=inventory_feed.xml"})


@app.get("/api/syndication/json")
async def syndication_json():
    """Export vehicles as JSON API feed."""
    from ai_studio.listing_syndication import export_json_api

    vehicles = db_get_vehicles()
    published = [v for v in vehicles if v.get("workflow_status") == "published" or v.get("status") == "active"]
    if not published:
        published = vehicles

    return export_json_api(published)


@app.get("/api/vehicle/{vin}/craigslist")
async def syndication_craigslist(vin: str):
    """Generate Craigslist-ready HTML posting for a vehicle."""
    from ai_studio.listing_syndication import export_craigslist_html

    vehicle = db_get_vehicle(vin)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")

    html = export_craigslist_html(vehicle)
    return {"status": "ok", "vin": vin, "html": html}


@app.get("/api/vehicle/{vin}/embed")
async def syndication_embed(vin: str):
    """Generate embeddable HTML widget for dealer websites."""
    from ai_studio.listing_syndication import export_embed_html

    vehicle = db_get_vehicle(vin)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")

    html = export_embed_html(vehicle)
    return {"status": "ok", "vin": vin, "embed_html": html}


# =============================================================================
# ROUTES — AI COMMERCIAL GENERATOR
# =============================================================================

import threading as _threading
import subprocess as _subprocess
import json as _json_mod

# Paths for commercial generator
# lotvue env is fine here: WAN GPU work happens inside the ComfyUI server (:8188)
# via wan_comfy_api; Kokoro TTS is verified working in the lotvue env.
_COMFYUI_PYTHON = os.environ.get("COMFYUI_PYTHON", r"E:\Miniconda3\envs\lotvue\python.exe")
_COMMERCIAL_SCRIPT = str(BASE_DIR / "commercial_pipeline.py")


def _run_commercial_pipeline_bg(vin: str, vehicle: dict, image_path: str):
    """Run commercial pipeline in background thread"""
    commercial_dir = os.path.join(str(STORAGE_DIR), vin, "commercial")
    os.makedirs(commercial_dir, exist_ok=True)
    status_file = os.path.join(commercial_dir, "status.json")
    try:
        with open(status_file, "w") as f:
            _json_mod.dump({"status": "running", "step": "starting", "progress": 0, "vin": vin}, f)

        result = _subprocess.run(
            [
                _COMFYUI_PYTHON, _COMMERCIAL_SCRIPT,
                "--vin", vin,
                "--image", image_path,
                "--year", str(vehicle.get("year", "")),
                "--make", str(vehicle.get("make", "")),
                "--model", str(vehicle.get("model", "")),
                "--trim", str(vehicle.get("trim_level", "")),
                "--engine", str(vehicle.get("engine", "")),
                "--transmission", str(vehicle.get("transmission", "")),
                "--price", str(vehicle.get("price", "")),
                "--dealer", str(vehicle.get("dealer_name") or vehicle.get("display_name") or "ABUZ8 Auto Demo")
            ],
            capture_output=True, text=True, timeout=900
        )
        if result.returncode != 0:
            with open(status_file, "w") as f:
                _json_mod.dump({"status": "error", "vin": vin, "error": result.stderr[-500:]}, f)
        else:
            print(f"[Commercial] Pipeline completed for VIN {vin}")
    except Exception as e:
        with open(status_file, "w") as f:
            _json_mod.dump({"status": "error", "vin": vin, "error": str(e)}, f)
        print(f"[Commercial] Error for VIN {vin}: {e}")


@app.post("/api/vehicle/{vin}/generate-commercial")
async def generate_commercial_endpoint(vin: str):
    """Start AI commercial generation for a vehicle. Runs async in background."""
    vehicle = db_get_vehicle(vin)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")

    commercial_dir = os.path.join(str(STORAGE_DIR), vin, "commercial")
    status_file = os.path.join(commercial_dir, "status.json")

    # Check if already running
    if os.path.exists(status_file):
        with open(status_file) as f:
            current = _json_mod.load(f)
        if current.get("status") == "running":
            return {"status": "already_running", "message": "Commercial generation already in progress"}

    # Find best image for this vehicle — check spin/, exterior/, then thumbnail
    image_path = None
    for subdir in ["spin", "exterior"]:
        candidate_dir = os.path.join(str(STORAGE_DIR), vin, subdir)
        if os.path.exists(candidate_dir):
            frames = sorted([f for f in os.listdir(candidate_dir) if f.lower().endswith((".jpg", ".jpeg", ".png"))])
            if frames:
                idx = max(0, len(frames) // 4)
                image_path = os.path.join(candidate_dir, frames[idx])
                break
    # Last resort: thumbnail
    if not image_path or not os.path.exists(image_path):
        thumb = os.path.join(str(STORAGE_DIR), vin, "thumbnail.jpg")
        if os.path.exists(thumb):
            image_path = thumb
    if not image_path or not os.path.exists(image_path):
        raise HTTPException(status_code=400, detail="No vehicle images found. Upload exterior photos first.")

    t = _threading.Thread(
        target=_run_commercial_pipeline_bg,
        args=(vin, vehicle, image_path),
        daemon=True
    )
    t.start()

    return {
        "status": "started",
        "vin": vin,
        "message": "Commercial generation started. Poll /api/vehicle/{vin}/commercial-status for progress."
    }


@app.get("/api/vehicle/{vin}/commercial-status")
async def commercial_status_endpoint(vin: str):
    """Poll commercial generation status. Returns progress and URL when done."""
    commercial_dir = os.path.join(str(STORAGE_DIR), vin, "commercial")
    status_file = os.path.join(commercial_dir, "status.json")

    if not os.path.exists(status_file):
        return {"status": "not_started", "vin": vin}

    with open(status_file) as f:
        data = _json_mod.load(f)

    if data.get("status") == "done":
        mp4_path = os.path.join(commercial_dir, "commercial.mp4")
        if os.path.exists(mp4_path):
            data["url"] = f"/storage/{vin}/commercial/commercial.mp4"
            data["size_mb"] = round(os.path.getsize(mp4_path) / (1024 * 1024), 1)

    return data


@app.delete("/api/vehicle/{vin}/commercial")
async def delete_commercial_endpoint(vin: str):
    """Delete generated commercial and reset status."""
    commercial_dir = os.path.join(str(STORAGE_DIR), vin, "commercial")
    if os.path.exists(commercial_dir):
        import shutil as _shutil
        _shutil.rmtree(commercial_dir)
    return {"status": "deleted", "vin": vin}


# =============================================================================
# ROUTES — PUBLIC URL
# =============================================================================
@app.get("/public-url")
async def public_url_page():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Public URL — LotVue Pro</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Playfair+Display:wght@700&display=swap');
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Inter', sans-serif; background: #0a1628; color: #f5f0e8; min-height: 100vh; }
        .header { background: linear-gradient(135deg, #0f2040 0%, #0a1628 100%); padding: 1rem 2rem; border-bottom: 1px solid #1a2e50; }
        .logo { font-size: 1.4rem; font-weight: 700; color: #c9a84c; font-family: 'Playfair Display', serif; }
        .container { max-width: 800px; margin: 0 auto; padding: 2rem; }
        .card { background: #0d1a30; border: 1px solid #1a2e50; border-radius: 8px; padding: 1.5rem; margin-bottom: 1.5rem; }
        h1 { font-size: 1.8rem; margin-bottom: 0.5rem; font-family: 'Playfair Display', serif; color: #f5f0e8; }
        .subtitle { color: #a0aab4; margin-bottom: 2rem; }
        h2 { color: #f5f0e8; font-size: 1.1rem; margin-bottom: 1rem; }
        code { background: #163060; padding: 0.2rem 0.5rem; border-radius: 4px; font-family: monospace; color: #c9a84c; }
        pre { background: #0f2040; padding: 1rem; border-radius: 6px; overflow-x: auto; font-size: 0.85rem; color: #a0aab4; border: 1px solid #1a2e50; }
        .warning { background: rgba(201,168,76,0.1); border: 1px solid rgba(201,168,76,0.3); border-radius: 6px; padding: 1rem; color: #e8d48b; margin-bottom: 1.5rem; }
    </style>
</head>
<body>
    <div class="header">
        <div class="logo">LotVue Pro — Public URL</div>
    </div>
    <div class="container">
        <h1>Share Your 360 Views</h1>
        <p class="subtitle">Make your vehicle showcases accessible to anyone, anywhere</p>
        
        <div class="warning">
            <strong>Note:</strong> Your local server is currently only accessible on your computer. To share links with customers, you need a public URL.
        </div>
        
        <div class="card">
            <h2>Option 1: Cloudflare Tunnel (Recommended)</h2>
            <p style="color:#a0aab4;margin-bottom:1rem;">Free, permanent URL with your own domain.</p>
            <pre>1. Download Cloudflare WARP: https://cloudflare.com/warp
2. Run: cloudflared tunnel create lotvue
3. Run: cloudflared tunnel route dns lotvue yourname.example.com
4. Run: cloudflared tunnel run lotvue</pre>
        </div>
        
        <div class="card">
            <h2>Option 2: ngrok (Quick Setup)</h2>
            <p style="color:#a0aab4;margin-bottom:1rem;">Get a temporary public URL in seconds.</p>
            <pre>1. Download ngrok: https://ngrok.com/download
2. Run: ngrok http 8001
3. Copy the URL shown (e.g., https://abc123.ngrok.io)</pre>
        </div>
        
        <div class="card">
            <h2>Current Local URL</h2>
            <code>http://localhost:8001</code>
            <p style="color:#a0aab4;margin-top:1rem;">This works on your computer only.</p>
        </div>
    </div>
</body>
</html>"""
    return HTMLResponse(html)


# =============================================================================
# API — AI PHOTO STUDIO (Module 2)
# =============================================================================
@app.post("/api/vehicle/{vin}/process-photos")
async def process_vehicle_photos(vin: str, request: Request):
    """
    Run the AI Photo Studio pipeline on all exterior photos for a vehicle.
    Enhances photos: bg removal → showroom bg → enhance → plate blur.
    """
    vehicle = db_get_vehicle(vin)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")

    vehicle_dir = STORAGE_DIR / vin
    exterior_dir = vehicle_dir / "exterior"
    studio_dir = vehicle_dir / "studio"
    studio_dir.mkdir(exist_ok=True)

    if not exterior_dir.exists() or not list(exterior_dir.glob("*.jpg")):
        raise HTTPException(status_code=400, detail="No exterior photos found")

    # Parse options
    try:
        body = await request.json()
    except:
        body = {}
    background = body.get("background", "studio_white")
    do_upscale = body.get("upscale", False)

    # Update workflow status to processing
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE vehicles SET workflow_status='processing' WHERE vin=?", (vin,))
    conn.commit()
    conn.close()

    # Run AI pipeline in background thread
    import threading

    def _run_pipeline():
        try:
            from ai_studio import PhotoProcessor
            processor = PhotoProcessor(background_name=background, upscale=do_upscale)
            results = processor.process_batch(str(exterior_dir), str(studio_dir))
            success = sum(1 for r in results if r["status"] == "ok")
            print(f"[AI Studio] {vin}: {success}/{len(results)} photos processed")

            # Update to review status
            conn2 = sqlite3.connect(str(DB_PATH))
            conn2.execute("UPDATE vehicles SET workflow_status='review' WHERE vin=?", (vin,))
            conn2.commit()
            conn2.close()
        except Exception as e:
            print(f"[AI Studio] Pipeline error for {vin}: {e}")
            import traceback; traceback.print_exc()

    thread = threading.Thread(target=_run_pipeline, daemon=True)
    thread.start()

    return {
        "status": "processing",
        "vin": vin,
        "message": f"AI photo studio processing {len(list(exterior_dir.glob('*.jpg')))} photos",
        "background": background,
        "upscale": do_upscale,
    }


@app.get("/api/vehicle/{vin}/studio-photos")
async def get_studio_photos(vin: str):
    """Get list of AI-processed studio photos for a vehicle."""
    studio_dir = STORAGE_DIR / vin / "studio"
    if not studio_dir.exists():
        return {"photos": [], "count": 0}
    photos = sorted(studio_dir.glob("*.jpg"))
    return {
        "photos": [f"/storage/{vin}/studio/{p.name}" for p in photos],
        "count": len(photos),
    }


@app.get("/api/ai-studio/backgrounds")
async def list_studio_backgrounds():
    """List available AI studio backgrounds."""
    bg_dir = BASE_DIR / "ai_studio" / "backgrounds"
    if not bg_dir.exists():
        return {"backgrounds": []}
    bgs = []
    for f in sorted(bg_dir.glob("*.jpg")):
        name = f.stem.replace("_", " ").title()
        bgs.append({"slug": f.stem, "name": name, "path": f"/ai-studio-bg/{f.name}"})
    return {"backgrounds": bgs}


# Serve studio photos
app.mount("/storage", StaticFiles(directory=str(STORAGE_DIR)), name="vehicle-storage")

# Serve AI studio backgrounds
_AI_STUDIO_BG_DIR = BASE_DIR / "ai_studio" / "backgrounds"
if _AI_STUDIO_BG_DIR.exists():
    app.mount("/ai-studio-bg", StaticFiles(directory=str(_AI_STUDIO_BG_DIR)), name="ai-studio-bg")


# =============================================================================
# ROUTES — AI COMMERCIAL
# =============================================================================
COMFY_HOST = "http://127.0.0.1:8188"

@app.get("/ai-commercial")
async def ai_commercial_page():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI Commercial — LOTVUE</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Segoe UI', -apple-system, sans-serif; background: #f0f4f8; color: #1a2332; min-height: 100vh; }
        .header { background: linear-gradient(135deg, #e8edf2 0%, #dde3ea 100%); padding: 1rem 2rem; border-bottom: 1px solid #c5cdd6; display: flex; justify-content: space-between; align-items: center; }
        .logo { font-size: 1.4rem; font-weight: 700; color: #58a6ff; }
        .container { max-width: 900px; margin: 0 auto; padding: 2rem; }
        .card { background: #dde3ea; border: 1px solid #c5cdd6; border-radius: 8px; padding: 1.5rem; margin-bottom: 1.5rem; }
        h1 { font-size: 1.8rem; margin-bottom: 0.5rem; }
        .subtitle { color: #8b949e; margin-bottom: 2rem; }
        h2 { color: #1a2332; font-size: 1.1rem; margin-bottom: 1rem; }
        code { background: #e8edf2; padding: 0.2rem 0.5rem; border-radius: 4px; font-family: monospace; color: #58a6ff; }
        pre { background: #e8edf2; padding: 1rem; border-radius: 6px; overflow-x: auto; font-size: 0.85rem; color: #8b949e; }
        .btn { display: inline-block; background: #238636; border: none; padding: 0.7rem 1.2rem; color: white; border-radius: 6px; cursor: pointer; font-weight: 600; text-decoration: none; }
        .btn:disabled { background: #30363d; cursor: not-allowed; }
        .btn-outline { background: transparent; border: 1px solid #b0bac5; color: #8b949e; }
        .form-group { margin-bottom: 1rem; }
        .form-group label { display: block; color: #8b949e; margin-bottom: 0.4rem; }
        .form-group input, .form-group textarea, .form-group select { 
            width: 100%; padding: 0.7rem; background: #010409; border: 1px solid #c5cdd6; 
            border-radius: 6px; color: #1a2332; font-size: 1rem; 
        }
        .form-group textarea { min-height: 80px; resize: vertical; }
        .status { padding: 1rem; border-radius: 6px; margin-top: 1rem; }
        .status.pending { background: #1c1209; border: 1px solid #3d2e00; color: #d29922; }
        .status.ready { background: #0c1f0c; border: 1px solid #1a472a; color: #3fb950; }
        .status.error { background: #1f0f0f; border: 1px solid #3d1a1a; color: #f85149; }
        .video-preview video { width: 100%; max-width: 600px; border-radius: 8px; margin-top: 1rem; }
        .nav { display: flex; gap: 1rem; margin-bottom: 1rem; }
        .nav a { color: #58a6ff; text-decoration: none; }
    </style>
</head>
<body>
    <div class="header">
        <div class="logo">LOTVUE — AI Commercial</div>
    </div>
    <div class="container">
        <div class="nav">
            <a href="/">&larr; Dashboard</a>
        </div>
        
        <h1>Generate AI Commercial</h1>
        <p class="subtitle">Create stunning video ads for your vehicles using AI</p>
        
        <div class="card">
            <h2>🎬 Quick Generate</h2>
            <form id="generateForm">
                <div class="form-group">
                    <label>Select Vehicle</label>
                    <select id="vehicleSelect">
                        <option value="">-- Select a vehicle --</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Commercial Script (leave empty for auto-generated)</label>
                    <textarea id="scriptInput" placeholder="e.g., Check out this beautiful 2024 BMW X5 with low mileage..."></textarea>
                </div>
                <div class="form-group">
                    <label>Video Style</label>
                    <select id="styleSelect">
                        <option value="cinematic">Cinematic</option>
                        <option value="social">Social Media (9:16)</option>
                        <option value="showroom">Showroom Showcase</option>
                    </select>
                </div>
                <button type="submit" class="btn" id="generateBtn">Generate Commercial</button>
            </form>
            <div id="status" class="status pending" style="display:none;"></div>
            <div id="videoPreview" class="video-preview" style="display:none;">
                <video controls id="videoPlayer"></video>
                <br>
                <a id="downloadLink" class="btn btn-outline" download>Download Video</a>
            </div>
        </div>
        
        <div class="card">
            <h2>🔧 ComfyUI Integration</h2>
            <p style="color:#8b949e;margin-bottom:1rem;">This feature uses your local ComfyUI installation to generate videos.</p>
            <pre>ComfyUI Status: <span id="comfyStatus">Checking...</span></pre>
            <p style="color:#8b949e;margin-top:1rem;font-size:0.9rem;">
                Make sure ComfyUI is running at <code>http://127.0.0.1:8188</code>
            </p>
        </div>
        
        <div class="card">
            <h2>📝 How It Works</h2>
            <ol style="color:#8b949e;padding-left:1.5rem;line-height:1.8;">
                <li>Select a vehicle from your inventory</li>
                <li>Enter a script or let AI generate one</li>
                <li>Choose your video style</li>
                <li>Click Generate to create your commercial</li>
                <li>Download and share on social media!</li>
            </ol>
        </div>
    </div>
    
    <script>
        // Load vehicles
        fetch('/api/vehicles')
            .then(r => r.json())
            .then(vehicles => {
                const select = document.getElementById('vehicleSelect');
                vehicles.forEach(v => {
                    const opt = document.createElement('option');
                    opt.value = v.vin;
                    opt.textContent = v.year + ' ' + v.make + ' ' + v.model + ' (' + v.vin.slice(-6) + ')';
                    select.appendChild(opt);
                });
            });
        
        // Check ComfyUI status
        fetch('http://127.0.0.1:8188/system_stats')
            .then(() => document.getElementById('comfyStatus').textContent = '✅ Connected')
            .catch(() => document.getElementById('comfyStatus').textContent = '❌ Not running');
        
        // Generate form
        document.getElementById('generateForm').onsubmit = async e => {
            e.preventDefault();
            const btn = document.getElementById('generateBtn');
            const status = document.getElementById('status');
            const preview = document.getElementById('videoPreview');
            
            btn.disabled = true;
            btn.textContent = 'Generating...';
            status.style.display = 'block';
            status.className = 'status pending';
            status.textContent = 'Connecting to ComfyUI and generating video... This may take a few minutes.';
            preview.style.display = 'none';
            
            try {
                const vin = document.getElementById('vehicleSelect').value;
                const script = document.getElementById('scriptInput').value;
                const style = document.getElementById('styleSelect').value;
                
                const response = await fetch('/api/generate-commercial', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({vin, script, style})
                });
                
                const result = await response.json();
                
                if (result.status === 'ok') {
                    status.className = 'status ready';
                    status.textContent = 'Video generated successfully!';
                    document.getElementById('videoPlayer').src = result.video_url;
                    document.getElementById('downloadLink').href = result.video_url;
                    preview.style.display = 'block';
                } else {
                    status.className = 'status error';
                    status.textContent = 'Error: ' + result.error;
                }
            } catch (err) {
                status.className = 'status error';
                status.textContent = 'Connection failed. Is ComfyUI running?';
            }
            
            btn.disabled = false;
            btn.textContent = 'Generate Commercial';
        };
    </script>
</body>
</html>"""
    return HTMLResponse(html)


# =============================================================================
# API — AI COMMERCIAL
# =============================================================================
@app.post("/api/generate-commercial")
async def generate_commercial(request: Request):
    try:
        body = await request.json()
        vin = body.get('vin', '')
        script = body.get('script', '')
        style = body.get('style', 'cinematic')
        
        if not vin:
            return {"status": "error", "error": "No vehicle selected"}
        
        import urllib.request
        try:
            urllib.request.urlopen(f"{COMFY_HOST}/system_stats", timeout=2)
        except:
            return {"status": "error", "error": "ComfyUI is not running. Start it first."}
        
        return {
            "status": "ok",
            "message": "Commercial generation initiated",
            "video_url": f"/storage/{vin}/commercial.mp4"
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


# =============================================================================
# ROUTES — MOBILE CAPTURE (Phase 8)
# =============================================================================
@app.get("/capture-mobile")
async def capture_mobile():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>LotVue Pro — Mobile Capture</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'DM Sans', 'Segoe UI', -apple-system, sans-serif; background: #F8FAFC; color: #2C3E50; min-height: 100vh; }
        .header { background: linear-gradient(135deg,#2E75B6 0%,#1a5a9e 100%); padding: 1rem 1.5rem; display: flex; justify-content: space-between; align-items: center; }
        .logo { font-size: 1.2rem; font-weight: 800; color: #ffffff; }
        .container { max-width: 600px; margin: 0 auto; padding: 1.5rem; }
        h1 { font-size: 1.5rem; margin-bottom: 0.5rem; color: #2C3E50; }
        .subtitle { color: #6c757d; margin-bottom: 2rem; font-size: 0.95rem; }

        .step-card { background: #ffffff; border: 1px solid #d0d7de; border-radius: 12px; padding: 1.5rem; margin-bottom: 1rem; }
        .step-num { display: inline-block; width: 28px; height: 28px; background: #2E75B6; color: #fff; border-radius: 50%; text-align: center; line-height: 28px; font-weight: 700; font-size: 0.85rem; margin-right: 0.8rem; }
        .step-title { font-size: 1rem; font-weight: 600; margin-bottom: 0.5rem; color: #2C3E50; }
        .step-desc { color: #6c757d; font-size: 0.9rem; line-height: 1.5; }

        .position-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.5rem; margin-top: 1rem; }
        .pos-item { background: #f0f4f8; border: 2px solid #d0d7de; border-radius: 8px; padding: 0.6rem 0.3rem; text-align: center; font-size: 0.75rem; color: #6c757d; }
        .pos-item.active { border-color: #27ae60; background: rgba(39,174,96,0.1); color: #27ae60; }
        .pos-item span { display: block; font-size: 1.4rem; margin-bottom: 0.2rem; }

        .camera-preview { background: #e8ecf0; border-radius: 12px; aspect-ratio: 4/3; display: flex; align-items: center; justify-content: center; margin-bottom: 1rem; position: relative; overflow: hidden; border: 2px solid #d0d7de; }
        .camera-hint { color: #6c757d; font-size: 0.9rem; text-align: center; padding: 2rem; }
        .camera-guide { position: absolute; inset: 0; pointer-events: none; }
        .camera-video { width: 100%; height: 100%; object-fit: cover; border-radius: 10px; }
        .capture-btn { position: absolute; bottom: 1.5rem; left: 50%; transform: translateX(-50%); width: 70px; height: 70px; border-radius: 50%; background: #fff; border: 4px solid #2E75B6; cursor: pointer; box-shadow: 0 2px 8px rgba(0,0,0,0.2); }
        .capture-btn:active { background: #e8ecf0; }
        .camera-mode { display: none; }
        .camera-mode.active { display: block; }
        .car-outline { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); width: 60%; opacity: 0.3; }
        .car-outline svg { width: 100%; height: auto; }
        .position-indicator { position: absolute; bottom: 1rem; left: 50%; transform: translateX(-50%); background: rgba(46,117,182,0.9); padding: 0.4rem 1rem; border-radius: 20px; font-size: 0.85rem; color: #fff; }

        .controls { display: flex; gap: 1rem; justify-content: center; margin-top: 1rem; }
        .btn { background: #2E75B6; border: none; padding: 0.8rem 1.5rem; color: white; border-radius: 8px; cursor: pointer; font-weight: 600; font-size: 1rem; text-decoration: none; display: inline-block; }
        .btn:hover { background: #1a5a9e; }
        .btn-outline { background: transparent; border: 1px solid #d0d7de; color: #6c757d; }

        .input-group { margin-bottom: 1rem; }
        .input-group label { display: block; color: #6c757d; font-size: 0.85rem; margin-bottom: 0.3rem; }
        .input-group input { width: 100%; padding: 0.7rem; background: #fff; border: 1px solid #d0d7de; border-radius: 6px; color: #2C3E50; font-size: 1rem; }
        .input-group input:focus { outline: none; border-color: #2E75B6; box-shadow: 0 0 0 3px rgba(46,117,182,0.12); }

        .tip-box { background: rgba(46,117,182,0.06); border: 1px solid rgba(46,117,182,0.2); border-radius: 8px; padding: 1rem; margin-top: 1rem; }
        .tip-box p { color: #2E75B6; font-size: 0.85rem; line-height: 1.5; }

        .progress-bar { height: 4px; background: #e2e8f0; border-radius: 2px; margin-top: 1rem; overflow: hidden; }
        .progress-fill { height: 100%; background: #27ae60; transition: width 0.3s; }

        .upload-area { background: #ffffff; border: 2px dashed #d0d7de; border-radius: 12px; padding: 2rem; text-align: center; cursor: pointer; transition: all 0.2s; }
        .upload-area:hover { border-color: #2E75B6; background: rgba(46,117,182,0.03); }
        .upload-area p { color: #6c757d; font-size: 0.9rem; }
        .upload-area input { display: none; }
    </style>
</head>
<body>
    <div class="header">
        <div class="logo">LotVue Pro</div>
        <a href="/" style="color:rgba(255,255,255,0.8);text-decoration:none;font-size:0.9rem;">Dashboard</a>
    </div>
    <div class="container">
        <h1>Mobile Capture</h1>
        <p class="subtitle">Take 8 photos walking around your car</p>
        
        <div class="step-card">
            <div class="step-title"><span class="step-num">1</span>Start Position</div>
            <div class="step-desc">Stand at the front-right corner of the car, about 10 feet away. Hold phone horizontally.</div>
        </div>
        
        <div class="position-grid">
            <div class="pos-item active"><span>1</span>Front</div>
            <div class="pos-item"><span>2</span>Front-R</div>
            <div class="pos-item"><span>3</span>Side</div>
            <div class="pos-item"><span>4</span>Rear-R</div>
            <div class="pos-item"><span>5</span>Rear</div>
            <div class="pos-item"><span>6</span>Rear-L</div>
            <div class="pos-item"><span>7</span>Side-L</div>
            <div class="pos-item"><span>8</span>Front-L</div>
        </div>
        
        <div class="step-card" style="margin-top:1.5rem;">
            <div class="step-title"><span class="step-num">2</span>Take Photos</div>
            <div class="step-desc">Tap each position below in order. Walk clockwise around the car, stopping at each spot to take a photo.</div>
            <div class="camera-preview" id="cameraPreview">
                <div class="camera-hint">Tap the positions below to mark them</div>
                <div class="camera-guide">
                    <div class="car-outline">
                        <svg viewBox="0 0 200 100" fill="none" xmlns="http://www.w3.org/2000/svg">
                            <rect x="20" y="40" width="160" height="50" rx="10" fill="#c0c8d0"/>
                            <rect x="40" y="25" width="80" height="25" rx="5" fill="#c0c8d0"/>
                            <circle cx="50" cy="90" r="15" fill="none" stroke="#c0c8d0" stroke-width="3"/>
                            <circle cx="150" cy="90" r="15" fill="none" stroke="#c0c8d0" stroke-width="3"/>
                        </svg>
                    </div>
                </div>
                <div class="position-indicator" id="posIndicator">Position 1 of 8</div>
            </div>
            
            <div class="controls" id="controls">
                <button class="btn btn-outline" onclick="resetCapture()" style="font-size:0.85rem;padding:0.5rem 1rem;">Reset</button>
                <button class="btn" onclick="markPosition()" style="padding:0.6rem 2rem;">Next Position</button>
            </div>
            
            <div style="text-align:center;margin-top:1.5rem;">
                <button class="btn" id="openCameraBtn" onclick="openCamera()" style="width:100%;">📷 Use Camera</button>
            </div>
            
            <div class="camera-preview camera-mode" id="cameraMode">
                <video id="video" class="camera-video" autoplay playsinline></video>
                <canvas id="canvas" style="display:none;"></canvas>
                <button class="capture-btn" onclick="capturePhoto()"></button>
                <div class="position-indicator" id="camPosIndicator">Position 1 of 8</div>
            </div>
            
            <div style="display:flex;gap:0.5rem;margin-top:1rem;justify-content:center;" id="camControls">
                <button class="btn btn-outline" onclick="closeCamera()" style="font-size:0.85rem;padding:0.5rem 1rem;">Close Camera</button>
            </div>
            
            <div class="progress-bar">
                <div class="progress-fill" id="progressFill" style="width:0%"></div>
            </div>
        </div>
        
        <div class="upload-area" id="uploadArea" onclick="document.getElementById('fileInput').click()" style="display:none;">
            <p>Tap to upload your 8 photos</p>
            <input type="file" id="fileInput" accept="image/*" multiple onchange="handleFiles(this.files)">
        </div>
        
        <div class="input-group" style="margin-top:1rem;">
            <label>VIN (optional — auto-generated if blank)</label>
            <input type="text" id="vinInput" placeholder="Leave blank for auto VIN" maxlength="17">
        </div>
        
        <button class="btn" id="uploadBtn" onclick="uploadAll()" style="width:100%;margin-top:1rem;display:none;">Upload &amp; Create 360</button>
        
        <div class="tip-box">
            <p><strong>Tip:</strong> For best results, shoot on a cloudy day or in shade. Avoid direct sunlight. Keep the car centered in each photo.</p>
        </div>
        
        <div class="tip-box" style="background:rgba(39,174,96,0.08);border-color:rgba(39,174,96,0.25);">
            <p style="color:#27ae60;"><strong>Install as App:</strong><br>
            iPhone: Tap Share → "Add to Home Screen"<br>
            Android: Tap menu → "Install App" or "Add to Home Screen"</p>
        </div>
        
        <div class="step-card" style="margin-top:1rem;">
            <div class="step-title"><span class="step-num">3</span>Upload to Dashboard</div>
            <div class="step-desc">Once all 8 positions are marked, upload your photos. We'll create the 360 spin automatically.</div>
        </div>
    </div>
    
    <script>
        let currentPos = 1;
        let capturedPositions = [];
        const totalPositions = 8;
        
        function markPosition() {{
            if (currentPos <= totalPositions) {{
                capturedPositions.push(currentPos);
                document.querySelectorAll('.pos-item')[currentPos - 1].classList.add('active');
                document.getElementById('progressFill').style.width = ((currentPos / totalPositions) * 100) + '%';
                
                if (currentPos < totalPositions) {{
                    currentPos++;
                    document.getElementById('posIndicator').textContent = 'Position ' + currentPos + ' of ' + totalPositions;
                    document.querySelectorAll('.pos-item')[currentPos - 1].classList.add('active');
                }}
                
                if (capturedPositions.length === totalPositions) {{
                    document.getElementById('controls').style.display = 'none';
                    document.getElementById('uploadArea').style.display = 'block';
                    document.getElementById('uploadBtn').style.display = 'block';
                    document.getElementById('posIndicator').textContent = 'All 8 positions captured!';
                    document.getElementById('posIndicator').style.background = 'rgba(35,134,54,0.8)';
                }}
            }}
        }}
        
        // Camera capture functions
        let videoStream = null;
        
        async function openCamera() {{
            try {{
                const stream = await navigator.mediaDevices.getUserMedia({{ video: {{ facingMode: 'environment' }} }});
                videoStream = stream;
                const video = document.getElementById('video');
                video.srcObject = stream;
                document.getElementById('cameraMode').classList.add('active');
                document.getElementById('openCameraBtn').style.display = 'none';
                document.getElementById('posIndicator').textContent = 'Position 1 of 8 - Tap Capture';
            }} catch (e) {{
                alert('Camera access denied. Please allow camera permissions.');
            }}
        }}
        
        function closeCamera() {{
            if (videoStream) {{
                videoStream.getTracks().forEach(track => track.stop());
                videoStream = null;
            }}
            document.getElementById('cameraMode').classList.remove('active');
            document.getElementById('openCameraBtn').style.display = 'block';
        }}
        
        function capturePhoto() {{
            const video = document.getElementById('video');
            const canvas = document.getElementById('canvas');
            canvas.width = video.videoWidth;
            canvas.height = video.videoHeight;
            const ctx = canvas.getContext('2d');
            ctx.drawImage(video, 0, 0);
            
            canvas.toBlob(blob => {{
                const file = new File([blob], 'position_' + currentPos + '.jpg', {{ type: 'image/jpeg' }});
                photoFiles.push(file);
                
                capturedPositions.push(currentPos);
                document.querySelectorAll('.pos-item')[currentPos - 1].classList.add('active');
                document.getElementById('progressFill').style.width = ((capturedPositions.length / totalPositions) * 100) + '%';
                
                if (currentPos < totalPositions) {{
                    currentPos++;
                    document.getElementById('camPosIndicator').textContent = 'Position ' + currentPos + ' of 8';
                }}
                
                if (capturedPositions.length === totalPositions) {{
                    closeCamera();
                    document.getElementById('controls').style.display = 'none';
                    document.getElementById('uploadArea').style.display = 'block';
                    document.getElementById('uploadBtn').style.display = 'block';
                    document.getElementById('posIndicator').textContent = 'All 8 positions captured!';
                    document.getElementById('posIndicator').style.background = 'rgba(39,174,96,0.9)';
                    // Show preview
                    const preview = document.getElementById('cameraPreview');
                    const img = document.createElement('img');
                    img.src = URL.createObjectURL(photoFiles[0]);
                    img.style.cssText = 'width:100%;height:100%;object-fit:cover;border-radius:10px;';
                    preview.innerHTML = '';
                    preview.appendChild(img);
                }}
            }}, 'image/jpeg', 0.9);
        }}
        
        function resetCapture() {{
            currentPos = 1;
            capturedPositions = [];
            document.querySelectorAll('.pos-item').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.pos-item')[0].classList.add('active');
            document.getElementById('posIndicator').textContent = 'Position 1 of ' + totalPositions;
            document.getElementById('posIndicator').style.background = 'rgba(46,117,182,0.9)';
            document.getElementById('progressFill').style.width = '0%';
            document.getElementById('controls').style.display = 'flex';
            document.getElementById('uploadArea').style.display = 'none';
            document.getElementById('uploadBtn').style.display = 'none';
        }}
        
        // Gyroscope level indicator
        let levelEnabled = false;
        function startLevel() {{
            if (window.DeviceOrientationEvent && typeof DeviceOrientationEvent.requestPermission === 'function') {{
                DeviceOrientationEvent.requestPermission().then(state => {{
                    if (state === 'granted') enableLevel();
                }});
            }} else if (window.DeviceOrientationEvent) {{
                enableLevel();
            }}
        }}

        function enableLevel() {{
            levelEnabled = true;
            const indicator = document.createElement('div');
            indicator.id = 'levelIndicator';
            indicator.style.cssText = 'position:fixed;top:50%;right:12px;transform:translateY(-50%);width:40px;height:120px;background:rgba(46,117,182,0.15);border:2px solid #2E75B6;border-radius:20px;z-index:100;display:flex;align-items:center;justify-content:center;';
            indicator.innerHTML = '<div id="levelBubble" style="width:20px;height:20px;border-radius:50%;background:#27ae60;transition:transform 0.1s;"></div>';
            document.body.appendChild(indicator);

            window.addEventListener('deviceorientation', (e) => {{
                const beta = Math.max(-30, Math.min(30, e.beta || 0));
                const offset = (beta / 30) * 40;
                const bubble = document.getElementById('levelBubble');
                if (bubble) {{
                    bubble.style.transform = 'translateY(' + offset + 'px)';
                    bubble.style.background = Math.abs(beta) < 5 ? '#27ae60' : Math.abs(beta) < 15 ? '#e67e22' : '#e74c3c';
                }}
            }});
        }}

        // Auto-start level when camera opens
        const origOpenCamera = openCamera;
        openCamera = async function() {{
            await origOpenCamera();
            if (!levelEnabled) startLevel();
        }};

        let photoFiles = [];
        function handleFiles(files) {{
            photoFiles = Array.from(files).slice(0, totalPositions);
            if (photoFiles.length > 0) {{
                const preview = document.getElementById('cameraPreview');
                const img = document.createElement('img');
                img.src = URL.createObjectURL(photoFiles[0]);
                img.style.cssText = 'width:100%;height:100%;object-fit:cover;border-radius:10px;';
                preview.innerHTML = '';
                preview.appendChild(img);
            }}
        }}
        
        async function uploadAll() {{
            if (photoFiles.length === 0) {{ alert('Please select photos first'); return; }}
            const vin = document.getElementById('vinInput').value.trim();
            const btn = document.getElementById('uploadBtn');
            btn.disabled = true;
            btn.textContent = 'Uploading...';
            
            try {{
                let usedVin = '';
                for (let i = 0; i < photoFiles.length; i++) {{
                    const pf = new FormData();
                    pf.append('vin', vin);
                    pf.append('photo', photoFiles[i]);
                    pf.append('index', i.toString());
                    const r = await fetch('/api/upload/exterior', {{ method: 'POST', body: pf }});
                    if (r.ok) {{
                        const data = await r.json();
                        if (data.vin) usedVin = data.vin;
                    }}
                }}
                
                await fetch('/api/finalize/' + usedVin, {{ method: 'POST' }});
                btn.textContent = 'Done!';
                setTimeout(() => window.location.href = '/view/' + usedVin, 500);
            }} catch(e) {{
                alert('Error: ' + e.message);
                btn.disabled = false;
                btn.textContent = 'Upload & Create 360';
            }}
        }}
    </script>
</body>
</html>"""
    return HTMLResponse(html)


# =============================================================================
# ROUTES — PRICING PAGE (Phase 7)
# =============================================================================
@app.get("/pricing")
async def pricing_page():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LotVue Pro — Pricing</title>
    <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Inter', 'Segoe UI', -apple-system, sans-serif; background: #0a1628; color: #f5f0e8; min-height: 100vh; }
        .header { background: linear-gradient(135deg,#0f2040 0%,#0a1628 100%); padding: 1rem 2rem; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #1a2e50; }
        .logo { font-family: 'Playfair Display', serif; font-size: 1.4rem; font-weight: 700; color: #c9a84c; }
        .container { max-width: 900px; margin: 0 auto; padding: 3rem 1.5rem; text-align: center; }
        h1 { font-family: 'Playfair Display', serif; font-size: 2.2rem; margin-bottom: 1rem; color: #f5f0e8; }
        .subtitle { color: #a0aab4; font-size: 1.1rem; margin-bottom: 3rem; }

        .plans { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1.5rem; margin: 2rem 0; }
        .plan { background: #0d1a30; border: 1px solid #1a2e50; border-radius: 12px; padding: 2rem; text-align: center; transition: all 0.2s; }
        .plan:hover { border-color: #c9a84c40; box-shadow: 0 4px 20px rgba(201,168,76,0.08); }
        .plan.featured { border-color: #c9a84c; box-shadow: 0 0 20px rgba(201,168,76,0.15); }
        .plan-badge { background: #c9a84c; color: #0a1628; padding: 0.2rem 0.6rem; border-radius: 20px; font-size: 0.7rem; font-weight: 700; display: inline-block; margin-bottom: 0.5rem; }
        .plan-name { font-size: 1.2rem; font-weight: 600; color: #f5f0e8; margin-bottom: 0.5rem; }
        .plan-price { font-size: 2rem; font-weight: 700; color: #c9a84c; margin-bottom: 0.3rem; }
        .plan-price span { font-size: 0.9rem; font-weight: 400; color: #a0aab4; }
        .plan-credits { color: #a0aab4; font-size: 0.85rem; margin-bottom: 1.5rem; }
        .plan-features { text-align: left; margin-bottom: 1.5rem; }
        .plan-features li { color: #a0aab4; font-size: 0.85rem; margin-bottom: 0.5rem; list-style: none; padding-left: 1.2rem; position: relative; }
        .plan-features li::before { content: '✓'; position: absolute; left: 0; color: #2ab89a; }

        .btn { display: inline-block; background: #1a8a7a; border: none; padding: 0.8rem 1.5rem; color: white; border-radius: 8px; cursor: pointer; font-weight: 600; font-size: 1rem; text-decoration: none; transition: background 0.2s; }
        .btn:hover { background: #2ab89a; }
        .btn-outline { background: transparent; border: 1px solid #1a2e50; color: #a0aab4; }
        .btn-outline:hover { border-color: #c9a84c; color: #c9a84c; }
        .btn-full { width: 100%; }

        .faq { text-align: left; margin-top: 3rem; }
        .faq h2 { font-family: 'Playfair Display', serif; color: #f5f0e8; font-size: 1.3rem; margin-bottom: 1.5rem; }
        .faq-item { background: #0f2040; border: 1px solid #1a2e50; border-radius: 8px; padding: 1.2rem; margin-bottom: 0.8rem; }
        .faq-q { color: #f5f0e8; font-weight: 600; margin-bottom: 0.5rem; }
        .faq-a { color: #a0aab4; font-size: 0.9rem; line-height: 1.5; }

        @media (max-width: 600px) { .plans { grid-template-columns: 1fr; } }
    </style>
</head>
<body>
    <div class="header">
        <div class="logo">LotVue Pro</div>
        <a href="/" style="color:#c9a84c;text-decoration:none;">Dashboard</a>
    </div>
    <div class="container">
        <h1>Simple, Transparent Pricing</h1>
        <p class="subtitle">Start free. Upgrade when you need more.</p>
        
        <div class="plans">
            <div class="plan">
                <div class="plan-name">Free</div>
                <div class="plan-price">$0</div>
                <div class="plan-credits">5 credits included</div>
                <ul class="plan-features">
                    <li>5 vehicle 360 views</li>
                    <li>Background removal</li>
                    <li>Shareable links</li>
                    <li>Embed code</li>
                </ul>
                <a href="/register" class="btn btn-outline btn-full">Get Started</a>
            </div>
            
            <div class="plan">
                <div class="plan-name">Single</div>
                <div class="plan-price">$2.99</div>
                <div class="plan-credits">1 vehicle</div>
                <ul class="plan-features">
                    <li>1 vehicle 360 view</li>
                    <li>No watermark</li>
                    <li>All features included</li>
                </ul>
                <button class="btn btn-full" onclick="buyPlan('single')">Buy Now</button>
            </div>
            
            <div class="plan">
                <div class="plan-name">Starter</div>
                <div class="plan-price">$19.99</div>
                <div class="plan-credits">10 vehicles</div>
                <ul class="plan-features">
                    <li>10 vehicle 360 views</li>
                    <li>No watermark</li>
                    <li>Priority processing</li>
                    <li>Email support</li>
                </ul>
                <button class="btn btn-full" onclick="buyPlan('starter')">Buy Now</button>
            </div>
            
            <div class="plan featured">
                <div class="plan-badge">MOST POPULAR</div>
                <div class="plan-name">Dealership</div>
                <div class="plan-price">$75<span>/mo</span></div>
                <div class="plan-credits">Unlimited vehicles</div>
                <ul class="plan-features">
                    <li>Unlimited vehicles</li>
                    <li>No watermark</li>
                    <li>Multi-user access</li>
                    <li>Custom branding</li>
                    <li>Priority support</li>
                    <li>API access</li>
                </ul>
                <button class="btn btn-full" onclick="buyPlan('dealership')">Start Free Trial</button>
            </div>
        </div>
        
        <div class="faq">
            <h2>Frequently Asked Questions</h2>
            <div class="faq-item">
                <div class="faq-q">What is a credit?</div>
                <div class="faq-a">Each vehicle you create a 360 view for uses 1 credit. You get 5 free credits when you sign up.</div>
            </div>
            <div class="faq-item">
                <div class="faq-q">Can I remove the watermark?</div>
                <div class="faq-a">Yes! Purchase any paid plan to remove watermarks from your 360 views and access clean, professional images.</div>
            </div>
            <div class="faq-item">
                <div class="faq-q">What happens to my vehicles if I cancel?</div>
                <div class="faq-a">Your vehicles remain accessible. You'll keep any 360 views you've already created. You just won't be able to create new ones without credits.</div>
            </div>
            <div class="faq-item">
                <div class="faq-q">Is there a free trial for Dealership?</div>
                <div class="faq-a">Yes! Contact us for a 14-day free trial of the Dealership plan with all features enabled.</div>
            </div>
        </div>
    </div>
    
    <script>
        async function buyPlan(planId) {{
            const email = prompt('Enter your email to purchase:');
            if (!email) return;
            try {{
                const r = await fetch('/api/create-checkout-session', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
                    body: 'plan_id=' + planId + '&user_email=' + encodeURIComponent(email)
                }});
                const data = await r.json();
                if (data.session_url) {{
                    window.location.href = data.session_url;
                }} else {{
                    alert('Stripe not configured yet. Email us at support@auto360.app to purchase.');
                }}
            }} catch(e) {{
                alert('Error: ' + e.message);
            }}
        }}
    </script>
</body>
</html>"""
    return HTMLResponse(html)


# =============================================================================
# ROUTES — AUTH PAGES (Phase 7)
# =============================================================================
@app.get("/register")
async def register_page():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LotVue Pro — Sign Up</title>
    <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Inter', 'Segoe UI', sans-serif; background: #0a1628; color: #f5f0e8; min-height: 100vh; display: flex; align-items: center; justify-content: center; }
        .card { background: #0f2040; border: 1px solid #1a2e50; border-radius: 12px; padding: 2.5rem; width: 100%; max-width: 400px; box-shadow: 0 8px 32px rgba(0,0,0,0.3); }
        .logo { font-family: 'Playfair Display', serif; font-size: 1.6rem; font-weight: 700; color: #c9a84c; text-align: center; margin-bottom: 1.5rem; }
        h1 { font-size: 1.3rem; color: #f5f0e8; text-align: center; margin-bottom: 0.5rem; }
        .subtitle { color: #a0aab4; text-align: center; margin-bottom: 2rem; font-size: 0.9rem; }
        .fg { margin-bottom: 1.2rem; }
        .fg label { display: block; color: #a0aab4; font-size: 0.85rem; margin-bottom: 0.3rem; }
        .fg input { width: 100%; padding: 0.7rem; background: #0a1628; border: 1px solid #1a2e50; border-radius: 6px; color: #f5f0e8; font-size: 1rem; }
        .fg input:focus { outline: none; border-color: #c9a84c; box-shadow: 0 0 0 3px rgba(201,168,76,0.15); }
        .btn { width: 100%; padding: 0.8rem; background: #1a8a7a; border: none; border-radius: 6px; color: white; font-size: 1rem; font-weight: 600; cursor: pointer; transition: background 0.2s; }
        .btn:hover { background: #2ab89a; }
        .links { text-align: center; margin-top: 1.5rem; font-size: 0.9rem; }
        .links a { color: #c9a84c; text-decoration: none; }
        .links a:hover { color: #e8d48b; }
        .success { background: rgba(42,184,154,0.15); border: 1px solid #1a8a7a; border-radius: 6px; padding: 0.8rem; color: #2ab89a; text-align: center; margin-bottom: 1rem; display: none; }
    </style>
</head>
<body>
    <div class="card">
        <div class="logo">LotVue Pro</div>
        <h1>Create Account</h1>
        <p class="subtitle">5 free credits. No credit card required.</p>
        <div class="success" id="successMsg">Account created! Redirecting...</div>
        <form id="regForm" onsubmit="doRegister(event)">
            <div class="fg">
                <label>Email</label>
                <input type="email" name="email" required placeholder="you@dealer.com">
            </div>
            <div class="fg">
                <label>Name</label>
                <input type="text" name="name" placeholder="Your name">
            </div>
            <div class="fg">
                <label>Password</label>
                <input type="password" name="password" required placeholder="Create a password">
            </div>
            <button type="submit" class="btn">Create Account</button>
        </form>
        <div class="links">
            <a href="/login">Already have an account? Sign in</a><br>
            <a href="/">Back to Dashboard</a>
        </div>
    </div>
    <script>
        async function doRegister(e) {
            e.preventDefault();
            const fd = new FormData(e.target);
            const params = new URLSearchParams();
            for (const [k, v] of fd.entries()) params.append(k, v);
            try {
                const r = await fetch('/api/register', { method: 'POST', body: params });
                const data = await r.json();
                if (data.status === 'ok') {
                    document.getElementById('successMsg').style.display = 'block';
                    setTimeout(() => window.location.href = '/login', 1000);
                } else {
                    alert(data.error || 'Registration failed');
                }
            } catch (err) {
                alert('Connection error — try again');
            }
        }
    </script>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/login")
async def login_page():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LotVue Pro — Sign In</title>
    <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Inter', 'Segoe UI', sans-serif; background: #0a1628; color: #f5f0e8; min-height: 100vh; display: flex; align-items: center; justify-content: center; }
        .card { background: #0f2040; border: 1px solid #1a2e50; border-radius: 12px; padding: 2.5rem; width: 100%; max-width: 400px; box-shadow: 0 8px 32px rgba(0,0,0,0.3); }
        .logo { font-family: 'Playfair Display', serif; font-size: 1.6rem; font-weight: 700; color: #c9a84c; text-align: center; margin-bottom: 1.5rem; }
        h1 { font-size: 1.3rem; color: #f5f0e8; text-align: center; margin-bottom: 0.5rem; }
        .subtitle { color: #a0aab4; text-align: center; margin-bottom: 2rem; font-size: 0.9rem; }
        .fg { margin-bottom: 1.2rem; }
        .fg label { display: block; color: #a0aab4; font-size: 0.85rem; margin-bottom: 0.3rem; }
        .fg input { width: 100%; padding: 0.7rem; background: #0a1628; border: 1px solid #1a2e50; border-radius: 6px; color: #f5f0e8; font-size: 1rem; }
        .fg input:focus { outline: none; border-color: #c9a84c; box-shadow: 0 0 0 3px rgba(201,168,76,0.15); }
        .btn { width: 100%; padding: 0.8rem; background: #1a8a7a; border: none; border-radius: 6px; color: white; font-size: 1rem; font-weight: 600; cursor: pointer; transition: background 0.2s; }
        .btn:hover { background: #2ab89a; }
        .error { background: rgba(248,81,73,0.15); border: 1px solid #da3633; border-radius: 6px; padding: 0.8rem; color: #f85149; text-align: center; margin-bottom: 1rem; display: none; }
        .links { text-align: center; margin-top: 1.5rem; font-size: 0.9rem; }
        .links a { color: #c9a84c; text-decoration: none; }
        .links a:hover { color: #e8d48b; }
    </style>
</head>
<body>
    <div class="card">
        <div class="logo">LotVue Pro</div>
        <h1>Sign In</h1>
        <p class="subtitle">Welcome back!</p>
        <div class="error" id="errorMsg">Invalid credentials</div>
        <form id="loginForm" onsubmit="doLogin(event)">
            <div class="fg">
                <label>Email</label>
                <input type="email" name="email" required placeholder="you@dealer.com">
            </div>
            <div class="fg">
                <label>Password</label>
                <input type="password" name="password" required placeholder="Your password">
            </div>
            <button type="submit" class="btn">Sign In</button>
        </form>
        <div class="links">
            <a href="/register">Create account</a><br>
            <a href="/">Back to Dashboard</a>
        </div>
    </div>
    <script>
        async function doLogin(e) {
            e.preventDefault();
            const btn = e.target.querySelector('button');
            btn.textContent = 'Signing in…'; btn.disabled = true;
            const fd = new FormData(e.target);
            const params = new URLSearchParams();
            for (const [k, v] of fd.entries()) params.append(k, v);
            try {
                const r = await fetch('/api/login', { method: 'POST', body: params });
                const data = await r.json();
                if (data.status === 'ok') {
                    const next = new URLSearchParams(location.search).get('next') || '/dashboard';
                    window.location.href = next;
                } else {
                    document.getElementById('errorMsg').textContent = data.error || 'Invalid email or password';
                    document.getElementById('errorMsg').style.display = 'block';
                    btn.textContent = 'Sign In'; btn.disabled = false;
                }
            } catch (err) {
                document.getElementById('errorMsg').textContent = 'Connection error — try again';
                document.getElementById('errorMsg').style.display = 'block';
                btn.textContent = 'Sign In'; btn.disabled = false;
            }
        }
    </script>
</body>
</html>"""
    return HTMLResponse(html)


# =============================================================================
# AHMAD ONBOARDING — Phase 9
# Auto-creates Mohammed's ABUZ8 Auto Demo account
# =============================================================================
def setup_ahmad():
    """Run once to set up Ahmad's dealer account."""
    dealer_email = "ahmad@abuz8ai.com"
    dealer_name = "abuz8autodemo"
    dealer_display = "ABUZ8 Auto Demo"
    dealer_website = "abuz8ai.com"
    
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        INSERT OR IGNORE INTO dealers (name, display_name, email, website, plan, brand_color, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (dealer_name, dealer_display, dealer_email, dealer_website, "dealership", "#3fb950", datetime.now().isoformat()))
    
    conn.execute("""
        INSERT OR IGNORE INTO users (email, name, plan, credits, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (dealer_email, dealer_display, "dealership", 999999, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    print(f"[LotVue Pro] Ahmad onboarded: {dealer_email} - {dealer_display}")

# Demo/personal seeding only when explicitly enabled — a clean shipped build
# (LOTVUE_SEED_DEMO unset) stays free of any personal account.
if os.environ.get("LOTVUE_SEED_DEMO", "0") == "1":
    try:
        setup_ahmad()
    except Exception as e:
        print(f"[LotVue Pro] demo setup skipped: {e}")


# =============================================================================
# MODULE 9 — AI LEAD ENGINE (BDC Automation)
# Cloning best of 360autoai + Flai + BDC.ai
# =============================================================================

@app.post("/api/leads/submit")
async def submit_lead(request: Request):
    """Submit a new lead and auto-score + schedule follow-ups."""
    from ai_studio.lead_engine import score_lead, generate_lead_response, schedule_follow_up_sequence, build_digital_dossier
    body = await request.json()
    name = body.get("name", "")
    email = body.get("email", "")
    phone = body.get("phone", "")
    vin = body.get("vin", "")
    message = body.get("message", "")
    source = body.get("source", "website")
    wants_test_drive = body.get("wants_test_drive", 0)
    wants_video = body.get("wants_video", 0)

    if not email and not phone:
        raise HTTPException(status_code=400, detail="Email or phone required")

    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.execute("""
        INSERT INTO leads (vin, name, email, phone, message, source, wants_test_drive, wants_video)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (vin, name, email, phone, message, source, wants_test_drive, wants_video))
    lead_id = cursor.lastrowid
    conn.commit()
    conn.close()

    lead = {
        "id": lead_id, "name": name, "email": email, "phone": phone,
        "vin": vin, "message": message, "source": source,
        "wants_test_drive": wants_test_drive, "wants_video": wants_video,
    }

    # Auto-score
    vehicle = db_get_vehicle(vin) if vin else None
    scoring = score_lead(lead, vehicle)

    # Auto-generate response
    response_msg = generate_lead_response(lead, vehicle)

    # Schedule follow-up sequence
    follow_ups = schedule_follow_up_sequence(lead_id, scoring["grade"])

    # Build digital dossier if vehicle exists
    dossier = None
    if vehicle:
        dossier = build_digital_dossier(lead, vehicle)

    # dealer intel (scoring, dossier, auto-response) only goes back to a
    # signed-in caller — an anonymous buyer just gets a confirmation
    is_dealer = False
    try:
        from auth_gate import _bearer_or_cookie
        from a2_extensions import _jwt_secret, JWT_ALG
        from jose import jwt as _jwt
        tok = _bearer_or_cookie(request)
        if tok:
            is_dealer = _jwt.decode(tok, _jwt_secret(), algorithms=[JWT_ALG]).get("type") == "access"
    except Exception:
        is_dealer = False
    if not is_dealer:
        return {"status": "ok", "lead_id": lead_id, "message": "Thanks — the dealer will reach out shortly."}
    return {
        "status": "ok",
        "lead_id": lead_id,
        "scoring": scoring,
        "auto_response": response_msg,
        "follow_ups_scheduled": len(follow_ups),
        "dossier": dossier,
    }


@app.get("/api/leads")
async def list_leads(status: str = None, limit: int = 50):
    """List all leads with optional status filter."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    query = "SELECT * FROM leads WHERE 1=1"
    params = []
    if status:
        query += " AND status=?"
        params.append(status)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return {"status": "ok", "leads": [dict(r) for r in rows], "count": len(rows)}


@app.get("/api/leads/{lead_id}")
async def get_lead_detail(lead_id: int):
    """Get full lead detail with scoring and follow-up history."""
    from ai_studio.lead_engine import score_lead, build_digital_dossier
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    lead = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not lead:
        conn.close()
        raise HTTPException(status_code=404, detail="Lead not found")
    lead = dict(lead)

    vehicle = None
    if lead.get("vin"):
        vehicle = db_get_vehicle(lead["vin"])

    scoring = score_lead(lead, vehicle)
    dossier = build_digital_dossier(lead, vehicle) if vehicle else None

    # Get follow-ups
    follow_ups = []
    try:
        rows = conn.execute("SELECT * FROM follow_ups WHERE lead_id=? ORDER BY scheduled_for", (lead_id,)).fetchall()
        follow_ups = [dict(r) for r in rows]
    except:
        pass

    # Get appointments
    appointments = []
    try:
        rows = conn.execute("SELECT * FROM appointments WHERE lead_id=? ORDER BY scheduled_at", (lead_id,)).fetchall()
        appointments = [dict(r) for r in rows]
    except:
        pass

    conn.close()
    return {
        "status": "ok",
        "lead": lead,
        "scoring": scoring,
        "dossier": dossier,
        "follow_ups": follow_ups,
        "appointments": appointments,
    }


@app.get("/api/leads/{lead_id}/response")
async def generate_lead_response_endpoint(lead_id: int, response_type: str = "initial"):
    """Generate AI response for a lead (SMS + email)."""
    from ai_studio.lead_engine import generate_lead_response
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    lead = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    conn.close()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    lead = dict(lead)
    vehicle = db_get_vehicle(lead.get("vin", "")) if lead.get("vin") else None
    response = generate_lead_response(lead, vehicle, response_type=response_type)
    return {"status": "ok", "response": response}


@app.get("/api/lead-analytics")
async def lead_analytics():
    """Get lead funnel analytics."""
    from ai_studio.lead_engine import get_lead_analytics
    analytics = get_lead_analytics()
    return {"status": "ok", "analytics": analytics}


@app.get("/api/appointments/slots")
async def available_appointment_slots(days: int = 7):
    """Get available appointment time slots."""
    from ai_studio.lead_engine import get_available_slots
    slots = get_available_slots(days_ahead=days)
    return {"status": "ok", "slots": slots, "count": len(slots)}


@app.post("/api/appointments/book")
async def book_appointment_endpoint(request: Request):
    """Book an appointment for a lead."""
    from ai_studio.lead_engine import book_appointment
    body = await request.json()
    lead_id = body.get("lead_id")
    slot = body.get("datetime")
    apt_type = body.get("type", "test_drive")
    notes = body.get("notes", "")
    if not lead_id or not slot:
        raise HTTPException(status_code=400, detail="lead_id and datetime required")
    result = book_appointment(lead_id, slot, apt_type, notes)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return {"status": "ok", "appointment": result}


@app.get("/api/followup-queue")
async def follow_up_queue():
    """Get pending follow-ups ready to send."""
    from ai_studio.lead_engine import get_follow_up_queue
    queue = get_follow_up_queue()
    return {"status": "ok", "queue": queue, "count": len(queue)}


@app.get("/api/dossier/{vin}")
async def get_dossier(vin: str, lead_id: int = None):
    """Get digital dossier for a vehicle (optionally personalized for a lead)."""
    from ai_studio.lead_engine import build_digital_dossier
    vehicle = db_get_vehicle(vin)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    lead = {}
    if lead_id:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        conn.close()
        if row:
            lead = dict(row)
    dossier = build_digital_dossier(lead, vehicle)
    return {"status": "ok", "dossier": dossier}


# =============================================================================
# MODULE 10 — AI VIDEO ADS + PERSONALIZED LEAD RESPONSE
# Cloning best of Impel AI + AIVO360
# =============================================================================

@app.post("/api/vehicle/{vin}/video-ad")
async def generate_video_ad(vin: str, request: Request):
    """Generate a video ad script for a vehicle."""
    from ai_studio.video_ads import generate_video_ad_script
    vehicle = db_get_vehicle(vin)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    body = await request.json()
    ad_type = body.get("type", "listing")
    script = generate_video_ad_script(vehicle, ad_type=ad_type)
    return {"status": "ok", "script": script}


@app.post("/api/leads/{lead_id}/personalized-video")
async def personalized_video_response(lead_id: int):
    """Generate personalized video response for a lead (Impel AI style)."""
    from ai_studio.video_ads import generate_personalized_response
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    lead = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    conn.close()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    lead = dict(lead)
    vehicle = db_get_vehicle(lead.get("vin", "")) if lead.get("vin") else None
    if not vehicle:
        raise HTTPException(status_code=404, detail="No vehicle linked to this lead")
    result = generate_personalized_response(lead, vehicle)
    return {"status": "ok", **result}


@app.get("/api/vehicle/{vin}/video-ads")
async def list_video_ads(vin: str):
    """List generated video ads for a vehicle."""
    from ai_studio.video_ads import get_video_ad_status
    result = get_video_ad_status(vin)
    return {"status": "ok", **result}


# =============================================================================
# MODULE 11 — AI BACKGROUND STUDIO
# Cloning best of autofox + AIVO360
# =============================================================================

@app.get("/api/backgrounds/scenes")
async def list_background_scenes():
    """List all available background scenes."""
    from ai_studio.background_studio import list_scenes
    scenes = list_scenes()
    return {"status": "ok", "scenes": scenes}


@app.post("/api/vehicle/{vin}/background")
async def apply_background(vin: str, request: Request):
    """Apply AI background replacement to all photos for a vehicle."""
    from ai_studio.background_studio import process_vehicle_photos
    body = await request.json()
    scene_id = body.get("scene", "clean-studio")
    result = process_vehicle_photos(vin, scene_id)
    if "error" in result and result.get("processed", 0) == 0:
        raise HTTPException(status_code=404, detail=result["error"])
    return {"status": "ok", **result}


@app.post("/api/photo/remove-background")
async def remove_bg_endpoint(file: UploadFile = File(...)):
    """Remove background from a single uploaded photo."""
    from ai_studio.background_studio import remove_background
    import tempfile
    # Save upload
    tmp = Path(tempfile.mkdtemp()) / file.filename
    with open(tmp, "wb") as f:
        content = await file.read()
        f.write(content)
    # Process
    output = remove_background(str(tmp))
    return FileResponse(output, media_type="image/png", filename="no_background.png")


@app.post("/api/photo/composite")
async def composite_endpoint(request: Request):
    """Composite a no-background photo onto a scene."""
    from ai_studio.background_studio import composite_on_scene
    body = await request.json()
    vehicle_png = body.get("image_path")
    scene_id = body.get("scene", "clean-studio")
    if not vehicle_png:
        raise HTTPException(status_code=400, detail="image_path required")
    result = composite_on_scene(vehicle_png, scene_id)
    return {"status": "ok", "output": result}


# =============================================================================
# MODULE 12 — REPUTATION MANAGER
# Cloning 360autoai reputation management
# =============================================================================

@app.get("/api/reviews")
async def list_reviews(platform: str = None, status: str = None, limit: int = 50):
    """List reviews with optional filtering."""
    from ai_studio.reputation_manager import get_reviews
    reviews = get_reviews(platform, status, limit)
    return {"status": "ok", "reviews": reviews, "count": len(reviews)}


@app.post("/api/reviews")
async def add_review_endpoint(request: Request):
    """Add a review and get auto-generated response."""
    from ai_studio.reputation_manager import add_review
    body = await request.json()
    platform = body.get("platform", "google")
    reviewer = body.get("reviewer_name", "Customer")
    rating = body.get("rating", 5)
    text = body.get("review_text", "")
    result = add_review(platform, reviewer, rating, text)
    return {"status": "ok", **result}


@app.post("/api/reviews/{review_id}/respond")
async def respond_review(review_id: int, request: Request):
    """Save a response to a review."""
    from ai_studio.reputation_manager import respond_to_review
    body = await request.json()
    response_text = body.get("response", "")
    if not response_text:
        raise HTTPException(status_code=400, detail="response text required")
    result = respond_to_review(review_id, response_text)
    return {"status": "ok", **result}


@app.get("/api/reviews/{review_id}/generate-response")
async def generate_review_response(review_id: int):
    """Auto-generate an AI response for a review."""
    from ai_studio.reputation_manager import generate_review_response as gen_resp
    # reviews live in lotvue.db (reputation_manager's DB), not vehicles.db
    conn = sqlite3.connect(str(BASE_DIR / "lotvue.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Review not found")
    review = dict(row)
    response = gen_resp(review)
    return {"status": "ok", "suggested_response": response}


@app.get("/api/reputation/dashboard")
async def reputation_dashboard():
    """Get reputation analytics dashboard data."""
    from ai_studio.reputation_manager import get_reputation_dashboard
    dashboard = get_reputation_dashboard()
    return {"status": "ok", "dashboard": dashboard}


# =============================================================================
# STATIC FILE SERVING
# =============================================================================
app.mount("/storage", StaticFiles(directory=str(STORAGE_DIR)), name="storage")
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


# =============================================================================
# TRUE 3D (Gaussian Splatting) — walkaround video -> orbitable 3D scene
# Trains on the GPU workstation (Brush + COLMAP); buyers view the .ply in-browser.
# =============================================================================
@app.get("/splat3d/{vin}")
async def splat3d_viewer(vin: str):
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"/static/splat_viewer.html?vin={vin.upper()}")


@app.get("/3d-studio")
async def studio_3d_page():
    from fastapi.responses import HTMLResponse
    html = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LotVue360 — True 3D Studio</title><style>
*{margin:0;padding:0;box-sizing:border-box;font-family:-apple-system,'Segoe UI',Roboto,sans-serif}
body{background:#F8FAFC;color:#2C3E50;padding:2rem 1rem}
.wrap{max-width:680px;margin:0 auto;background:#fff;border:1px solid #E3EAF2;border-radius:16px;padding:2rem}
h1{font-size:1.5rem;color:#2E75B6}h1 span{color:#C8A55C}
p.sub{color:#7A8CA0;margin:.4rem 0 1.5rem}
label{display:block;font-weight:600;margin:1rem 0 .4rem}
input[type=text],input[type=file]{width:100%;padding:.7rem;border:1.5px solid #DDE5EE;border-radius:10px;font-size:1rem}
button{margin-top:1.5rem;width:100%;background:#2E75B6;color:#fff;border:0;padding:.9rem;border-radius:10px;font-size:1.05rem;font-weight:700;cursor:pointer}
button:disabled{background:#9bb6d0;cursor:wait}
.bar{height:8px;background:#E3EAF2;border-radius:4px;overflow:hidden;margin-top:1rem;display:none}
.bar>div{height:100%;width:0;background:linear-gradient(90deg,#2E75B6,#58a6ff);transition:width .3s}
#status{margin-top:1rem;font-weight:600}
#view{display:none;margin-top:1rem;text-align:center}
#view a{display:inline-block;background:#C8A55C;color:#fff;padding:.8rem 1.6rem;border-radius:10px;text-decoration:none;font-weight:700}
.tips{margin-top:1.5rem;font-size:.85rem;color:#7A8CA0;line-height:1.5;background:#F8FAFC;padding:1rem;border-radius:10px}
.styles{display:flex;gap:.5rem;margin-top:.4rem}
.opt{display:flex!important;align-items:center;justify-content:center;gap:.4rem;flex:1;margin:0;font-weight:600;font-size:.92rem;background:#F1F6FB;border:1.5px solid #DDE5EE;border-radius:10px;padding:.6rem .5rem;cursor:pointer}
.opt input{width:auto;margin:0}
.opt.sel{border-color:#2E75B6;background:#E8F1FB;color:#2E75B6}
.styles+.hint{display:block;font-size:.8rem;color:#7A8CA0;margin-top:.4rem;font-weight:400}
#dl a{display:inline-block;margin-left:.6rem;background:#fff;color:#2E75B6;border:1.5px solid #2E75B6;padding:.8rem 1.2rem;border-radius:10px;text-decoration:none;font-weight:700}
.brandrow{display:flex;gap:.6rem;margin-top:.4rem}.brandrow>div{flex:1}
.blab{display:block;font-size:.8rem;color:#7A8CA0;margin:0 0 .25rem;font-weight:600}
button.ghost{margin-top:.6rem;width:auto;background:#fff;color:#2E75B6;border:1.5px solid #2E75B6;padding:.55rem 1rem;font-size:.92rem;font-weight:700;border-radius:10px;cursor:pointer}
button.ghost:disabled{opacity:.6;cursor:wait}
#brandState{font-weight:600}
</style></head><body><div class="wrap">
<h1>LotVue<span>360</span> — True 3D Studio</h1>
<p class="sub">Upload a walkaround <b>video</b> OR a <b>set of photos</b>. You always get a draggable 360° spin — and it auto-upgrades to true orbit-anywhere 3D when the capture has enough motion. Runs on this workstation's GPU.</p>
<label>Vehicle VIN / ID</label>
<input id="vin" type="text" placeholder="e.g. 1HGBH41JXMN109186 (any ID works)">
<label>Walkaround video — or select multiple photos (20+ recommended)</label>
<input id="vid" type="file" accept="video/*,image/*" multiple>
<label>Background style</label>
<div class="styles">
  <label class="opt sel"><input type="radio" name="style" value="natural" checked onchange="selStyle()">Real lot</label>
  <label class="opt"><input type="radio" name="style" value="studio" onchange="selStyle()">Studio floor</label>
  <label class="opt"><input type="radio" name="style" value="branded" onchange="selStyle()">Branded</label>
</div>
<span class="hint">Real lot = your actual lot, edges softened. Studio = clean studio floor. Branded = your own backdrop + logo.</span>
<div id="backdropPanel" style="display:none">
  <label>Studio backdrop</label>
  <div class="styles" id="backdrops">
    <label class="opt sel"><input type="radio" name="backdrop" value="light" checked onchange="syncSel()">Light</label>
    <label class="opt"><input type="radio" name="backdrop" value="white" onchange="syncSel()">White</label>
    <label class="opt"><input type="radio" name="backdrop" value="silver" onchange="syncSel()">Silver</label>
    <label class="opt"><input type="radio" name="backdrop" value="warm" onchange="syncSel()">Warm</label>
    <label class="opt"><input type="radio" name="backdrop" value="blue" onchange="syncSel()">Blue</label>
  </div>
</div>
<div id="brandPanel" style="display:none">
  <label>Your branding <span id="brandState" class="hint" style="display:inline"></span></label>
  <div class="brandrow">
    <div><span class="blab">Backdrop image</span><input id="bgFile" type="file" accept="image/*"></div>
    <div><span class="blab">Logo (PNG, transparent)</span><input id="logoFile" type="file" accept="image/*"></div>
  </div>
  <button id="saveBrand" type="button" class="ghost" onclick="saveBrand()">Save branding</button>
  <span class="hint">Your backdrop replaces the studio floor; your logo sits bottom-right. Saved once, reused for every Branded spin.</span>
</div>
<button id="go" onclick="go()">Create 360 / 3D</button>
<div class="bar" id="bar"><div id="prog"></div></div>
<div id="status"></div>
<div id="view"><a id="vlink" href="#" target="_blank">🔄 Open Interactive 3D</a><span id="dl" style="display:none"><a href="#" download>⬇ Download MP4</a></span></div>
<div class="tips"><b>You'll always get a 360° spin.</b> For full <b>orbit-anywhere 3D</b>: capture a slow, complete circle around the car — a 20–40s video, or 30+ overlapping photos — keep the whole car + some ground in frame, move smoothly, even lighting. ~5–15 min on the GPU.</div>
</div><script>
let vin='';
function syncSel(){
  document.querySelectorAll('.opt').forEach(function(o){
    o.classList.toggle('sel', o.querySelector('input').checked);
  });
}
function selStyle(){
  syncSel();
  var s=(document.querySelector('input[name=style]:checked')||{}).value||'natural';
  document.getElementById('backdropPanel').style.display=(s==='studio')?'block':'none';
  document.getElementById('brandPanel').style.display=(s==='branded')?'block':'none';
  if(s==='branded'){ loadBrandState(); }
}
async function loadBrandState(){
  try{
    const r=await fetch('/api/brand/assets',{credentials:'same-origin'});
    if(!r.ok) return;
    const j=await r.json();
    const parts=[]; if(j.background) parts.push('backdrop ✓'); if(j.logo) parts.push('logo ✓');
    document.getElementById('brandState').textContent = parts.length ? ('— saved: '+parts.join(', ')) : '— none saved yet';
  }catch(e){}
}
async function saveBrand(){
  const bg=document.getElementById('bgFile').files[0];
  const lg=document.getElementById('logoFile').files[0];
  if(!bg && !lg){ alert('Choose a backdrop image and/or a logo'); return; }
  const fd=new FormData();
  if(bg) fd.append('background', bg);
  if(lg) fd.append('logo', lg);
  const btn=document.getElementById('saveBrand'); btn.disabled=true; btn.textContent='Saving…';
  try{
    const r=await fetch('/api/brand/assets',{method:'POST',body:fd,credentials:'same-origin'});
    const j=await r.json().catch(function(){return {}});
    if(r.ok){ btn.textContent='Saved ✓'; loadBrandState(); }
    else { alert(j.detail||('Save failed — HTTP '+r.status)); btn.textContent='Save branding'; }
  }catch(e){ alert('Error: '+e); btn.textContent='Save branding'; }
  btn.disabled=false;
}
async function go(){
  const files=document.getElementById('vid').files;
  vin=(document.getElementById('vin').value||'').trim().toUpperCase();
  if(!vin){alert('Enter a VIN/ID');return}
  if(!files||!files.length){alert('Choose a walkaround video or a set of photos');return}
  const vids=[...files].filter(f=>f.type.indexOf('video/')===0);
  const imgs=[...files].filter(f=>f.type.indexOf('image/')===0);
  const style=(document.querySelector('input[name=style]:checked')||{}).value||'natural';
  const backdrop=(document.querySelector('input[name=backdrop]:checked')||{}).value||'light';
  const fd=new FormData();
  fd.append('style', style);
  fd.append('backdrop', backdrop);
  if(imgs.length>=4){ imgs.forEach(f=>fd.append('photos',f)); }
  else if(vids.length){ fd.append('video',vids[0]); }
  else { alert('Upload one video, or at least 4 photos'); return; }
  const btn=document.getElementById('go');btn.disabled=true;btn.textContent='Uploading…';
  // hide any prior run's viewer/MP4 buttons so they can't serve the previous vehicle
  document.getElementById('view').style.display='none';
  document.getElementById('dl').style.display='none';
  document.getElementById('prog').style.width='0%';
  try{
    const r=await fetch('/api/vehicle/'+encodeURIComponent(vin)+'/generate-3d',{method:'POST',body:fd,credentials:'same-origin'});
    if(r.status===401){location.href='/login?next=/3d-studio';return}
    const j=await r.json().catch(function(){return {}});
    if(!r.ok){document.getElementById('status').textContent='⚠ '+(j.detail||('Upload failed — HTTP '+r.status));btn.disabled=false;btn.textContent='Create 360 / 3D';return}
    document.getElementById('bar').style.display='block';
    document.getElementById('status').textContent='Processing on the GPU…';
    btn.textContent='Working…';
    poll();
  }catch(e){document.getElementById('status').textContent='Error: '+e;btn.disabled=false;btn.textContent='Create 360 / 3D';}
}
async function poll(){
  try{
    const r=await fetch('/api/vehicle/'+encodeURIComponent(vin)+'/3d-status',{credentials:'same-origin'});
    const j=await r.json();
    document.getElementById('prog').style.width=(j.progress||0)+'%';
    if(j.status==='done'){
      document.getElementById('prog').style.width='100%';
      let msg = j.splat_ready ? '✅ True 3D ready — orbit it anywhere!' : '✅ 360° spin ready.';
      if(!j.splat_ready && j.note){ msg += '  ℹ '+j.note; }
      document.getElementById('status').textContent=msg;
      const v=document.getElementById('view');v.style.display='block';
      document.getElementById('vlink').textContent = j.splat_ready ? '🔄 Open Interactive 3D' : '🔄 Open 360 Viewer';
      document.getElementById('vlink').href='/splat3d/'+vin;
      const dl=document.getElementById('dl');
      if(j.spin_mp4){ dl.style.display='inline'; dl.querySelector('a').href=j.spin_mp4; }
      else { dl.style.display='none'; }
      document.getElementById('go').disabled=false;document.getElementById('go').textContent='Create 360 / 3D';
      return;
    }
    if(j.status==='error'){document.getElementById('status').textContent='⚠ '+(j.error||'failed');document.getElementById('go').disabled=false;document.getElementById('go').textContent='Create 360 / 3D';return;}
    document.getElementById('status').textContent=(j.step||j.status||'working')+' ('+(j.progress||0)+'%)';
  }catch(e){}
  setTimeout(poll,5000);
}
</script></body></html>"""
    return HTMLResponse(html)


@app.get("/api/vehicle/{vin}/3d-status")
async def vehicle_3d_status(vin: str):
    # sanitize to a safe folder name (no path traversal) — same rule as generate-3d
    vin = re.sub(r"[^A-Za-z0-9_-]", "", vin or "").upper()[:32]
    sp = STORAGE_DIR / vin / "splat"
    data = {}
    st = sp / "status.json"
    if st.exists():
        try:
            data = json.loads(st.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["ready"] = (sp / "scene.ply").exists()
    if data["ready"]:
        data.setdefault("url", f"/storage/{vin}/splat/scene.ply")
        data.setdefault("viewer", f"/splat3d/{vin}")
    if (STORAGE_DIR / vin / "spin" / "spin.mp4").exists():
        data["spin_mp4"] = f"/storage/{vin}/spin/spin.mp4"
    return data


@app.post("/api/vehicle/{vin}/generate-3d")
async def generate_3d(vin: str, video: Optional[UploadFile] = File(None),
                      photos: Optional[List[UploadFile]] = File(None),
                      steps: str = Form("7000"),
                      style: str = Form("natural"),
                      backdrop: str = Form("light")):
    # Accept any vehicle ID (real VIN or freeform) — sanitize to a safe folder name.
    vin = re.sub(r"[^A-Za-z0-9_-]", "", vin or "").upper()[:32]
    if not vin:
        raise HTTPException(status_code=400, detail="Provide a vehicle VIN or ID")
    style = (style or "natural").strip().lower()
    if style not in ("natural", "studio", "branded"):
        style = "natural"
    backdrop = (backdrop or "light").strip().lower()
    if backdrop not in ("light", "white", "silver", "warm", "blue"):
        backdrop = "light"
    vdir = STORAGE_DIR / vin
    (vdir / "splat").mkdir(parents=True, exist_ok=True)
    try:
        steps_i = max(2000, min(int(steps), 30000))
    except ValueError:
        steps_i = 7000

    # Either a walkaround VIDEO or a SET OF PHOTOS — both always produce a 360 spin,
    # and auto-upgrade to true 3D when the capture supports it.
    real_photos = [p for p in (photos or []) if getattr(p, "filename", "")]
    if real_photos:
        pdir = vdir / "_3dphotos"
        if pdir.exists():
            shutil.rmtree(pdir, ignore_errors=True)
        pdir.mkdir(parents=True, exist_ok=True)
        saved = 0
        for i, p in enumerate(real_photos):
            data = await p.read()
            if not data or len(data) > MAX_PHOTO_MB * 1024 * 1024:
                continue
            try:  # convert ANY format (incl. HEIC/webp) to jpg
                img = Image.open(io.BytesIO(data)).convert("RGB")
                img.save(pdir / f"p_{i+1:04d}.jpg", "JPEG", quality=92)
                saved += 1
            except Exception:
                continue
        if saved < 4:
            raise HTTPException(status_code=400, detail="Upload at least 4 readable photos.")
        mode_args = ["--images", str(pdir)]
    elif video is not None and getattr(video, "filename", ""):
        walk = vdir / "walkaround.mp4"
        content = await video.read()
        if len(content) > MAX_VIDEO_MB * 1024 * 1024 * 6:
            raise HTTPException(status_code=413, detail="Walkaround video too large")
        with open(walk, "wb") as f:
            f.write(content)
        mode_args = ["--video", str(walk)]
    else:
        raise HTTPException(status_code=400, detail="Upload a walkaround video or 4+ photos.")

    (vdir / "splat" / "status.json").write_text(
        json.dumps({"status": "running", "step": "queued", "progress": 1, "vin": vin}), encoding="utf-8")
    subprocess.Popen(
        [sys.executable, str(BASE_DIR / "gaussian_3d.py"), "--vin", vin, *mode_args,
         "--steps", str(steps_i), "--style", style, "--backdrop", backdrop],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(BASE_DIR))
    return {"status": "started", "vin": vin, "style": style, "backdrop": backdrop,
            "status_url": f"/api/vehicle/{vin}/3d-status", "viewer": f"/splat3d/{vin}"}


# ── Dealer branding assets (for the "branded" turntable style) ───────────────
@app.get("/api/brand/assets")
async def brand_assets_status():
    """Report which dealer-branding assets are currently on disk (for the studio UI)."""
    bdir = STORAGE_DIR / "_brand"
    bg = None
    for n in ("background.jpg", "background.jpeg", "background.png"):
        if (bdir / n).exists():
            bg = f"/storage/_brand/{n}"; break
    logo = "/storage/_brand/logo.png" if (bdir / "logo.png").exists() else None
    return {"background": bg, "logo": logo}


@app.post("/api/brand/assets")
async def brand_assets_upload(background: Optional[UploadFile] = File(None),
                              logo: Optional[UploadFile] = File(None)):
    """Save the dealer's backdrop and/or logo used by the 'branded' turntable style.
    Background is normalized to a JPG; logo keeps transparency as PNG."""
    bdir = STORAGE_DIR / "_brand"
    bdir.mkdir(parents=True, exist_ok=True)
    saved = {}
    if background is not None and getattr(background, "filename", ""):
        data = await background.read()
        if len(data) > MAX_PHOTO_MB * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"Background too large (max {MAX_PHOTO_MB}MB)")
        try:
            img = Image.open(io.BytesIO(data)).convert("RGB")
        except Exception:
            raise HTTPException(status_code=400, detail="Background isn't a readable image")
        for n in ("background.jpg", "background.jpeg", "background.png"):
            (bdir / n).unlink(missing_ok=True)        # drop stale variants
        img.save(bdir / "background.jpg", "JPEG", quality=92)
        saved["background"] = "/storage/_brand/background.jpg"
    if logo is not None and getattr(logo, "filename", ""):
        data = await logo.read()
        if len(data) > MAX_PHOTO_MB * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"Logo too large (max {MAX_PHOTO_MB}MB)")
        try:
            img = Image.open(io.BytesIO(data)).convert("RGBA")   # keep transparency
        except Exception:
            raise HTTPException(status_code=400, detail="Logo isn't a readable image")
        img.save(bdir / "logo.png", "PNG")
        saved["logo"] = "/storage/_brand/logo.png"
    if not saved:
        raise HTTPException(status_code=400, detail="Upload a background and/or a logo image")
    return {"status": "ok", "saved": saved}


@app.post("/api/brand/clear")
async def brand_assets_clear(target: str = Form("all")):
    """Remove dealer-branding assets (target = all | background | logo)."""
    bdir = STORAGE_DIR / "_brand"
    targets = ("background", "logo") if target == "all" else (target,)
    removed = []
    if "background" in targets:
        for n in ("background.jpg", "background.jpeg", "background.png"):
            p = bdir / n
            if p.exists():
                p.unlink(); removed.append(n)
    if "logo" in targets:
        p = bdir / "logo.png"
        if p.exists():
            p.unlink(); removed.append("logo.png")
    return {"status": "ok", "removed": removed}


# =============================================================================
# A2 GAP EXTENSIONS (leads CSV, exports, condition PDF, JWT, backgrounds)
# =============================================================================
from a2_extensions import register_extensions
register_extensions(app)

from b_distribution import register_distribution
register_distribution(app)

from c_intelligence import register_intelligence
register_intelligence(app)

from mobile_app import register_mobile
register_mobile(app)

from g_glo3d import register_glo3d
register_glo3d(app)

from f_control import register_control
register_control(app)


# =============================================================================
# LEAD FOLLOW-UP — one-click unsubscribe (public) + opt-in autonomous dispatcher
# =============================================================================
@app.get("/api/unsubscribe")
async def lead_unsubscribe(e: str = "", p: str = ""):
    from fastapi.responses import HTMLResponse
    try:
        from ai_studio.lead_engine import record_opt_out
        record_opt_out(email=e, phone=p)
    except Exception as ex:
        print(f"[Unsubscribe] {ex}")
    return HTMLResponse(
        "<html><body style='font-family:system-ui;background:#F8FAFC;color:#2C3E50;"
        "text-align:center;padding:4rem'><h2>You're unsubscribed</h2>"
        "<p>You won't receive further follow-up messages. You can still contact the dealer anytime.</p>"
        "</body></html>")


@app.on_event("startup")
async def _start_followup_dispatcher():
    """Autonomous BDC drip sender. DEFAULT OFF — set LOTVUE_AUTO_FOLLOWUP=1 to enable.
    Polls the follow_ups queue and sends due email/SMS touches (compliant: opt-out +
    unsubscribe footer). Email needs SMTP, SMS needs Twilio; otherwise it's a no-op."""
    if os.getenv("LOTVUE_AUTO_FOLLOWUP", "0") != "1":
        print("[Follow-up] autonomous dispatcher OFF (set LOTVUE_AUTO_FOLLOWUP=1 to enable)")
        return
    import threading, time as _t

    def _loop():
        from ai_studio.lead_engine import send_due_follow_ups
        interval = int(os.getenv("LOTVUE_FOLLOWUP_INTERVAL_S", "300"))
        while True:
            try:
                summary = send_due_follow_ups()
                if summary.get("sent") or summary.get("dealer_tasks"):
                    print(f"[Follow-up] dispatch: {summary}")
            except Exception as ex:
                print(f"[Follow-up] dispatcher error: {ex}")
            _t.sleep(interval)

    threading.Thread(target=_loop, daemon=True).start()
    print("[Follow-up] autonomous dispatcher ON")


from auth_gate import install_auth_gate
install_auth_gate(app)


# =============================================================================
# RUN
# =============================================================================
if __name__ == "__main__":
    import uvicorn
    PORT = int(os.getenv("LOTVUE_PORT", 8001))
    uvicorn.run(app, host="0.0.0.0", port=PORT)
