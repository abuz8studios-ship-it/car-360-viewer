r"""
Lotvue360 — Car 360 Viewer SaaS Platform (formerly LOTVUE)
Upgraded from LOTVUE MVP
Features: Multi-tenant, auth, credits, Stripe, watermark, mobile capture

Requirements (run once):
  E:\\Miniconda3\\envs\\lotvue\\python.exe -m pip install fastapi uvicorn python-multipart Pillow aiosqlite opencv-python rembg pillow-heif stripe
"""

import os
import re
import io
import json
import shutil
import sqlite3
import subprocess
import threading
from pathlib import Path
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image

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
print(f"[Lotvue360] GPU Detection: {GPU_INFO}")

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
        print(f"[Lotvue360] GPU 1 assigned for rendering: {torch.cuda.get_device_name(1)}")
except Exception as e:
    print(f"[Lotvue360] GPU assignment: {e}")

print(f"[Lotvue360] GPU Config: AI/Background={GPU_0_DEVICE}, Rendering={GPU_1_DEVICE}")

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
SHOWROOM_DIR = Path(r".\360Car\app\storage\_showrooms")

# =============================================================================
# CONFIG
# =============================================================================
FFMPEG = r".\AI\ffmpeg\ffmpeg.exe"
FFPROBE = r".\AI\ffmpeg\ffprobe.exe"
STORAGE_DIR = Path(r".\360Car\app\storage")
RAW_DIR = STORAGE_DIR / "raw"
FRAMES_DIR = STORAGE_DIR / "frames"
PREVIEW_DIR = STORAGE_DIR / "preview"
CLEAN_DIR = STORAGE_DIR / "clean"
THUMBNAILS_DIR = STORAGE_DIR / "thumbnails"
DB_PATH = Path(r".\360Car\app\vehicles.db")
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


WATERMARK_PREVIEW_TEXT = "Preview • Lotvue360"

def apply_diagonal_watermark(img: Image.Image, text: str = "LOTVUE360 PREVIEW", opacity: int = 77) -> Image.Image:
    """Apply diagonal repeating watermark across the entire image (Phase 1 upgrade)."""
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
        
        small_text = "lotvue360"
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
app = FastAPI(title="Lotvue360 — 360 Car Viewer SaaS")
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
                       stock_number="", turntable_style="studio_shadow"):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        INSERT INTO vehicles (vin, year, make, model, exterior_frames, interior_count, created_at, watermark_mode, user_email,
                              trim_level, engine, transmission, drivetrain, fuel_type, body_style, stock_number, turntable_style)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            stock_number=CASE WHEN excluded.stock_number != '' THEN excluded.stock_number ELSE stock_number END
    """, (vin, year, make, model, ext_frames, int_count, datetime.now().isoformat(), watermark_mode, user_email,
          trim_level, engine, transmission, drivetrain, fuel_type, body_style, stock_number, turntable_style))
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
                    "product_data": {"name": f"Lotvue360 - {plan['label']}"},
                    "unit_amount": int(plan["price"] * 100),
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=f"/?payment=success&plan={plan_id}",
            cancel_url="/?payment=cancelled",
            customer_email=user_email if user_email else None,
            metadata={"plan_id": plan_id, "credits": str(plan["credits"]), "user_email": user_email}
        )
        return {"status": "ok", "session_url": session.url, "session_id": session.id}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
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
                    print(f"[Lotvue360] Vehicle unlocked: {vin}")
            
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
                    "product_data": {"name": f"Lotvue360 - Unlock {vin}"},
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
    import hashlib
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    db_create_user(email, name, pw_hash)
    return {"status": "ok", "email": email}


@app.post("/api/login")
async def login_user(
    email: str = Form(...),
    password: str = Form("")
):
    import hashlib
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    user = db_get_user(email)
    if not user or user.get("password_hash") != pw_hash:
        return {"status": "error", "error": "Invalid credentials"}
    return {"status": "ok", "email": user["email"], "credits": user.get("credits", 5), "plan": user.get("plan", "free")}


@app.post("/api/set-plan")
async def set_user_plan(
    email: str = Form(...),
    plan: str = Form("free")
):
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
        "app": "Lotvue360",
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
    """Process all frames: bg removal + auto-align + turntable composite + optional background image."""
    if not REMBG_AVAILABLE:
        print("[BG Removal] rembg not available — skipping")
        return

    frames = sorted(list(frames_dir.glob("frame_*.jpg")) + list(frames_dir.glob("spin_*.jpg")))
    if not frames:
        print(f"[BG Removal] No frames found in {frames_dir}")
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

            # Apply watermark
            if watermark_mode == "preview":
                final = apply_diagonal_watermark(final, "LOTVUE360 PREVIEW", 60)

            final.save(frame_path, "JPEG", quality=92)
        except Exception as e:
            print(f"[BG Removal] Composite failed for {frame_path.name}: {e}")

    print(f"[BG Removal] Done. {aligned_idx} frames processed.")


def interpolate_frames(input_dir: Path, output_dir: Path, target_frames: int = 30):
    """
    Take N input frames, generate intermediate frames for smooth rotation.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Clear existing spin frames
    for old in output_dir.glob("spin_*.jpg"):
        old.unlink()
    
    frames_list = sorted(input_dir.glob("frame_*.jpg"))
    n = len(frames_list)
    
    if n == 0:
        return 0
    
    # If already enough frames, just copy
    if n >= target_frames:
        for i, f in enumerate(sorted(frames_list)[:target_frames]):
            shutil.copy(f, output_dir / f"spin_{i+1:04d}.jpg")
        return min(n, target_frames)
    
    # Read all input frames
    images = []
    for f in frames_list:
        img = cv2.imread(str(f))
        if img is not None:
            images.append(img)
    
    n = len(images)
    if n == 0:
        return 0
    
    # Calculate interpolations per gap (including wrap-around)
    total_gaps = n  # last frame wraps to first
    base_per_gap = target_frames // total_gaps
    extra = target_frames % total_gaps
    
    output_idx = 1
    
    for i in range(n):
        img_a = images[i]
        img_b = images[(i + 1) % n]  # wrap: last→first
        
        # How many frames for this gap (including the start frame)
        gap_frames = base_per_gap + (1 if i < extra else 0)
        
        for j in range(gap_frames):
            alpha = j / gap_frames if gap_frames > 0 else 0
            blended = cv2.addWeighted(img_a, 1.0 - alpha, img_b, alpha, 0)
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

    # Status logic per vehicle
    def get_status(v):
        ext = v.get("exterior_frames", 0)
        if ext >= 8:
            return "ready"
        elif ext > 0:
            return "incomplete"
        return "processing"

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
        status_labels = {"ready": ("Ready", "#3fb950"), "incomplete": ("Incomplete", "#d29922"), "processing": ("Processing", "#58a6ff")}
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
                    <a href="/tag/{vin}" class="btn btn-outline" style="font-size:0.8rem;padding:0.35rem 0.8rem;">Tag</a>
                    <a href="/embed/{vin}" target="_blank" class="btn btn-outline" style="font-size:0.8rem;padding:0.35rem 0.8rem;">Embed</a>
                    <button onclick="generateCommercial('{vin}', this)" id="comm-btn-{vin}" class="btn" style="font-size:0.8rem;padding:0.35rem 0.8rem;background:#7c3aed;border:none;color:white;" title="Generate AI Commercial">&#127916; Commercial</button>
                    <button onclick="deleteVehicle('{vin}')" class="btn btn-danger" style="font-size:0.8rem;padding:0.35rem 0.8rem;">Delete</button>
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
    <title>LOTVUE — Dashboard</title>
    <link rel="manifest" href="/static/manifest.json">
    <meta name="theme-color" content="#1e3a5f">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <style>
        * {{ margin:0; padding:0; box-sizing:border-box; }}
        body {{ font-family:'Segoe UI',-apple-system,sans-serif; background:#f0f4f8; color:#1a2332; min-height:100vh; }}
        .header {{ background:linear-gradient(135deg,#1e3a5f 0%,#2d5a8e 100%); padding:1rem 2rem; display:flex; justify-content:space-between; align-items:center; box-shadow:0 2px 8px rgba(0,0,0,0.15); }}
        .logo {{ font-size:1.5rem; font-weight:800; color:#ffffff; letter-spacing:-0.5px; }}
        .nav {{ display:flex; gap:0.5rem; align-items:center; }}
        .nav a {{ color:rgba(255,255,255,0.8); text-decoration:none; padding:0.4rem 0.9rem; border-radius:6px; font-size:0.9rem; transition:all 0.15s; }}
        .nav a:hover {{ background:rgba(255,255,255,0.15); color:#fff; }}
        .nav .btn-add {{ background:#3fb950; color:#fff; font-weight:700; padding:0.5rem 1.1rem; border-radius:6px; font-size:0.9rem; text-decoration:none; transition:all 0.15s; }}
        .nav .btn-add:hover {{ background:#2ea043; }}
        .container {{ max-width:1280px; margin:0 auto; padding:1.5rem 2rem; }}
        .stats-bar {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:1rem; margin-bottom:1.5rem; }}
        .stat {{ background:#ffffff; border:1px solid #d0d7de; border-radius:10px; padding:1rem 1.5rem; }}
        .stat-val {{ font-size:1.8rem; font-weight:800; color:#1e3a5f; }}
        .stat-lbl {{ font-size:0.72rem; color:#6e7781; text-transform:uppercase; letter-spacing:0.6px; margin-top:2px; }}
        .toolbar {{ display:flex; gap:0.8rem; align-items:center; margin-bottom:1.2rem; flex-wrap:wrap; }}
        .search-box {{ flex:1; min-width:200px; position:relative; }}
        .search-box input {{ width:100%; padding:0.55rem 0.9rem 0.55rem 2.2rem; border:1px solid #d0d7de; border-radius:8px; font-size:0.9rem; background:#fff; color:#1a2332; outline:none; }}
        .search-box input:focus {{ border-color:#2d5a8e; box-shadow:0 0 0 3px rgba(45,90,142,0.12); }}
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
        .btn {{ background:#2d5a8e; border:none; padding:0.4rem 0.8rem; color:white; border-radius:6px; cursor:pointer; font-weight:600; font-size:0.82rem; text-decoration:none; display:inline-block; transition:all 0.15s; }}
        .btn:hover {{ background:#1e3a5f; }}
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
        <div class="logo">LOTVUE</div>
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
            <div class="stat"><div class="stat-val">{sum(v.get('interior_count',0) for v in vehicles)}</div><div class="stat-lbl">Interior Sets</div></div>
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
                <option value="ready">Ready</option>
                <option value="incomplete">Incomplete</option>
                <option value="processing">Processing</option>
            </select>
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
        async function bulkDelete() {{
            const vins = [...document.querySelectorAll('.bulk-cb:checked')].map(cb => cb.value);
            if (!vins.length) return;
            if (!confirm('Delete ' + vins.length + ' vehicle(s)?')) return;
            await Promise.all(vins.map(v => fetch('/api/vehicle/' + v, {{method:'DELETE'}})));
            location.reload();
        }}
        // Single delete
        async function deleteVehicle(vin) {{
            if (!confirm('Delete ' + vin + '?')) return;
            const r = await fetch('/api/vehicle/' + vin, {{ method: 'DELETE' }});
            if (r.ok) location.reload();
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
            modal.innerHTML = \`
                <div style="background:#1a1a2e;border:1px solid #7c3aed;border-radius:12px;padding:1.5rem;max-width:780px;width:95%;position:relative;">
                    <button onclick="document.getElementById('commercial-modal').remove()" style="position:absolute;top:0.75rem;right:0.75rem;background:none;border:none;color:#8b949e;font-size:1.4rem;cursor:pointer;">&times;</button>
                    <h3 style="color:#a78bfa;margin-bottom:1rem;font-size:1.1rem;">&#127916; AI Commercial Ready</h3>
                    <video controls autoplay style="width:100%;border-radius:8px;background:#000;" src="${{url}}"></video>
                    <div style="margin-top:1rem;display:flex;gap:0.75rem;flex-wrap:wrap;">
                        <a href="${{url}}" download="commercial_${{vin}}.mp4" style="background:#7c3aed;color:white;padding:0.5rem 1rem;border-radius:6px;text-decoration:none;font-size:0.85rem;">&#11015; Download MP4 \${{sizeMb ? '(' + sizeMb + ' MB)' : ''}}</a>
                        <button onclick="regenerateCommercial('${{vin}}')" style="background:#374151;color:#d1d5db;border:none;padding:0.5rem 1rem;border-radius:6px;cursor:pointer;font-size:0.85rem;">&#8635; Regenerate</button>
                    </div>
                </div>
            \`;
            document.body.appendChild(modal);
            modal.addEventListener('click', e => {{ if (e.target === modal) modal.remove(); }});
        }}

        async function regenerateCommercial(vin) {{
            document.getElementById('commercial-modal')?.remove();
            await fetch('/api/vehicle/' + vin + '/commercial', {{ method: 'DELETE' }});
            const btn = document.getElementById('comm-btn-' + vin);
            if (btn) generateCommercial(vin, btn);
        }}

        if ('serviceWorker' in navigator) {{
            navigator.serviceWorker.register('/static/service-worker.js');
        }}
    </script>
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
    <title>How to Capture 360° Photos — LOTVUE</title>
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
        <div class="logo">LOTVUE</div>
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
                        <h3>Upload to LOTVUE</h3>
                        <p>Go to /capture, enter the VIN, and upload your photos. We'll create the 360 experience automatically!</p>
                    </div>
                </div>
            </div>
        </div>
        
        <div class="warning">
            <strong>iPhone Note:</strong> Go to Settings → Camera → Formats → Most Compatible to ensure photos are saved as JPG, not HEIC. LOTVUE works best with JPG files.
        </div>
        
        <div style="text-align:center;margin-top:2rem;">
            <a href="/capture" class="btn">Start Capturing Now</a>
        </div>
    </div>
</body>
</html>"""
    return HTMLResponse(html)


# =============================================================================
# ROUTES — CAPTURE (Phase 1B Upgrade — 4-step guided flow)
# =============================================================================
@app.get("/capture")
async def capture_page():
    heic_note = "" if HEIC_SUPPORTED else '<div class="heic-warn">iPhone users: Go to Settings &rarr; Camera &rarr; Formats &rarr; Most Compatible. This ensures photos are JPG, not HEIC.</div>'
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LOTVUE — Add Vehicle</title>
    <style>
        * {{ margin:0; padding:0; box-sizing:border-box; }}
        body {{ font-family:'Segoe UI',-apple-system,sans-serif; background:#f0f4f8; color:#1a2332; min-height:100vh; }}
        .header {{ background:linear-gradient(135deg,#1e3a5f 0%,#2d5a8e 100%); padding:1rem 2rem; display:flex; justify-content:space-between; align-items:center; }}
        .logo {{ font-size:1.5rem; font-weight:800; color:#fff; }}
        .back-link {{ color:rgba(255,255,255,0.8); text-decoration:none; font-size:0.9rem; }}
        .back-link:hover {{ color:#fff; }}
        .container {{ max-width:760px; margin:0 auto; padding:2rem 1.5rem; }}
        .heic-warn {{ background:#fff8e6; border:1px solid #f0b429; border-radius:8px; padding:0.8rem 1rem; margin-bottom:1.5rem; font-size:0.85rem; color:#7a4f00; }}
        /* Steps indicator */
        .steps-indicator {{ display:flex; align-items:center; margin-bottom:2rem; }}
        .step-dot {{ width:32px; height:32px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-weight:700; font-size:0.85rem; flex-shrink:0; transition:all 0.3s; }}
        .step-dot.active {{ background:#2d5a8e; color:#fff; }}
        .step-dot.done {{ background:#3fb950; color:#fff; }}
        .step-dot.pending {{ background:#e0e7ef; color:#8b949e; }}
        .step-label {{ font-size:0.75rem; color:#8b949e; margin-top:0.25rem; text-align:center; white-space:nowrap; }}
        .step-item {{ display:flex; flex-direction:column; align-items:center; }}
        .step-line {{ flex:1; height:2px; background:#e0e7ef; margin:0 0.5rem; margin-bottom:1rem; }}
        .step-line.done {{ background:#3fb950; }}
        /* Cards */
        .card {{ background:#fff; border:1px solid #d0d7de; border-radius:10px; padding:1.5rem; margin-bottom:1.5rem; display:none; }}
        .card.active {{ display:block; }}
        .card h2 {{ font-size:1.1rem; font-weight:700; color:#1e3a5f; margin-bottom:0.3rem; }}
        .card .subtitle {{ color:#8b949e; font-size:0.85rem; margin-bottom:1.2rem; }}
        /* Form fields */
        .fg {{ margin-bottom:1rem; }}
        .fg label {{ display:block; margin-bottom:0.35rem; color:#57606a; font-size:0.85rem; font-weight:600; }}
        .fg input, .fg select {{ width:100%; padding:0.6rem 0.9rem; border:1px solid #d0d7de; border-radius:8px; font-size:0.95rem; color:#1a2332; background:#fff; outline:none; transition:border-color 0.15s; }}
        .fg input:focus, .fg select:focus {{ border-color:#2d5a8e; box-shadow:0 0 0 3px rgba(45,90,142,0.1); }}
        .row2 {{ display:grid; grid-template-columns:1fr 1fr; gap:1rem; }}
        /* Upload type selector */
        .type-selector {{ display:grid; grid-template-columns:1fr 1fr; gap:1rem; margin-bottom:1.2rem; }}
        .type-card {{ border:2px solid #d0d7de; border-radius:10px; padding:1.2rem; cursor:pointer; text-align:center; transition:all 0.2s; }}
        .type-card:hover {{ border-color:#2d5a8e; background:#f0f4f8; }}
        .type-card.selected {{ border-color:#2d5a8e; background:#eef3fb; }}
        .type-card .icon {{ font-size:2rem; margin-bottom:0.5rem; }}
        .type-card .label {{ font-weight:700; color:#1a2332; font-size:0.95rem; }}
        .type-card .desc {{ color:#8b949e; font-size:0.8rem; margin-top:0.2rem; }}
        /* Dropzone */
        .dropzone {{ border:2px dashed #d0d7de; border-radius:10px; padding:2.5rem; text-align:center; cursor:pointer; transition:all 0.2s; }}
        .dropzone:hover, .dropzone.dragover {{ border-color:#2d5a8e; background:#eef3fb; }}
        .dropzone .dz-icon {{ font-size:2.5rem; margin-bottom:0.5rem; }}
        .dropzone .dz-text {{ color:#57606a; font-size:0.9rem; }}
        .dropzone .dz-hint {{ color:#8b949e; font-size:0.8rem; margin-top:0.3rem; }}
        /* File previews */
        .preview-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(80px,1fr)); gap:0.5rem; margin-top:1rem; }}
        .preview-item {{ aspect-ratio:1; border-radius:6px; overflow:hidden; position:relative; border:1px solid #d0d7de; }}
        .preview-item img {{ width:100%; height:100%; object-fit:cover; }}
        .preview-item .rm {{ position:absolute; top:2px; right:2px; background:rgba(0,0,0,0.6); color:#fff; border:none; border-radius:50%; width:18px; height:18px; font-size:0.7rem; cursor:pointer; display:flex; align-items:center; justify-content:center; }}
        .file-count {{ color:#2d5a8e; font-weight:700; font-size:0.9rem; margin-top:0.5rem; }}
        /* Progress */
        .progress-wrap {{ margin-top:1.5rem; display:none; }}
        .progress-bar {{ height:8px; background:#e0e7ef; border-radius:4px; overflow:hidden; }}
        .progress-fill {{ height:100%; background:linear-gradient(90deg,#2d5a8e,#3fb950); width:0%; transition:width 0.4s; border-radius:4px; }}
        .progress-text {{ margin-top:0.5rem; color:#57606a; font-size:0.85rem; text-align:center; }}
        .progress-pct {{ font-weight:700; color:#2d5a8e; }}
        /* Buttons */
        .btn-row {{ display:flex; gap:0.8rem; margin-top:1.5rem; }}
        .btn {{ padding:0.65rem 1.4rem; border-radius:8px; font-weight:700; font-size:0.95rem; cursor:pointer; border:none; transition:all 0.15s; }}
        .btn-primary {{ background:#2d5a8e; color:#fff; }}
        .btn-primary:hover {{ background:#1e3a5f; }}
        .btn-primary:disabled {{ opacity:0.4; cursor:not-allowed; }}
        .btn-secondary {{ background:#f0f4f8; color:#57606a; border:1px solid #d0d7de; }}
        .btn-secondary:hover {{ background:#e0e7ef; }}
        .btn-success {{ background:#3fb950; color:#fff; width:100%; padding:0.8rem; font-size:1rem; }}
        .btn-success:hover {{ background:#2ea043; }}
        .btn-success:disabled {{ opacity:0.4; cursor:not-allowed; }}
        /* Review */
        .review-item {{ display:flex; justify-content:space-between; padding:0.5rem 0; border-bottom:1px solid #f0f4f8; font-size:0.9rem; }}
        .review-item .label {{ color:#8b949e; }}
        .review-item .value {{ color:#1a2332; font-weight:600; }}
        /* Capture guide link */
        .guide-link {{ display:inline-flex; align-items:center; gap:0.4rem; color:#2d5a8e; text-decoration:none; font-size:0.85rem; margin-top:0.5rem; }}
        .guide-link:hover {{ text-decoration:underline; }}
        @media(max-width:600px) {{ .row2 {{ grid-template-columns:1fr; }} .type-selector {{ grid-template-columns:1fr; }} }}
    </style>
</head>
<body>
    <div class="header">
        <div class="logo">LOTVUE</div>
        <a href="/" class="back-link">&larr; Dashboard</a>
    </div>
    <div class="container">
        {heic_note}
        <!-- Steps Indicator -->
        <div class="steps-indicator" id="stepsIndicator">
            <div class="step-item"><div class="step-dot active" id="sd1">1</div><div class="step-label">Vehicle Info</div></div>
            <div class="step-line" id="sl1"></div>
            <div class="step-item"><div class="step-dot pending" id="sd2">2</div><div class="step-label">Exterior 360</div></div>
            <div class="step-line" id="sl2"></div>
            <div class="step-item"><div class="step-dot pending" id="sd3">3</div><div class="step-label">Interior</div></div>
            <div class="step-line" id="sl3"></div>
            <div class="step-item"><div class="step-dot pending" id="sd4">4</div><div class="step-label">Review</div></div>
        </div>

        <!-- Step 1: Vehicle Info -->
        <div class="card active" id="step1">
            <h2>Step 1 — Vehicle Information</h2>
            <p class="subtitle">All fields are optional. Fill in what you know.</p>
            <div class="row2">
                <div class="fg"><label>Year</label><input type="text" id="f_year" placeholder="e.g. 2023" maxlength="4"></div>
                <div class="fg"><label>Make</label><input type="text" id="f_make" placeholder="e.g. Toyota"></div>
            </div>
            <div class="row2">
                <div class="fg"><label>Model</label><input type="text" id="f_model" placeholder="e.g. Camry"></div>
                <div class="fg"><label>Color</label><input type="text" id="f_color" placeholder="e.g. Pearl White"></div>
            </div>
            <div class="row2">
                <div class="fg"><label>Price</label><input type="text" id="f_price" placeholder="e.g. $24,500"></div>
                <div class="fg"><label>Mileage</label><input type="text" id="f_mileage" placeholder="e.g. 32,000"></div>
            </div>
            <div class="btn-row">
                <button class="btn btn-primary" onclick="goStep(2)">Next: Exterior 360 &rarr;</button>
            </div>
        </div>

        <!-- Step 2: Exterior Upload -->
        <div class="card" id="step2">
            <h2>Step 2 — Exterior 360&deg;</h2>
            <p class="subtitle">Upload a walkaround video OR photos taken in order around the car.</p>
            <a href="/capture-guide" class="guide-link" target="_blank">&#128247; How to shoot for best results</a>
            <div class="type-selector" style="margin-top:1rem;">
                <div class="type-card selected" id="typeVideo" onclick="selectType('video')">
                    <div class="icon">🎥</div>
                    <div class="label">Walkaround Video</div>
                    <div class="desc">Walk around the car while recording</div>
                </div>
                <div class="type-card" id="typePhotos" onclick="selectType('photos')">
                    <div class="icon">📸</div>
                    <div class="label">Photos (8–36)</div>
                    <div class="desc">Take photos every 10° around the car</div>
                </div>
            </div>
            <!-- Video upload -->
            <div id="videoSection">
                <div class="dropzone" id="videoDZ" onclick="document.getElementById('videoInput').click()">
                    <div class="dz-icon">🎥</div>
                    <div class="dz-text">Click or drag video here</div>
                    <div class="dz-hint">MP4, MOV, AVI — up to 500MB</div>
                </div>
                <input type="file" id="videoInput" accept="video/*" style="display:none">
                <div id="videoInfo" class="file-count"></div>
            </div>
            <!-- Photo upload -->
            <div id="photosSection" style="display:none;">
                <div class="dropzone" id="extDZ" onclick="document.getElementById('extPhotoInput').click()">
                    <div class="dz-icon">📸</div>
                    <div class="dz-text">Click or drag exterior photos here</div>
                    <div class="dz-hint">JPG, PNG — 8 to 36 photos in order</div>
                </div>
                <input type="file" id="extPhotoInput" accept="image/*" multiple style="display:none">
                <div id="extPhotoCount" class="file-count"></div>
                <div class="preview-grid" id="extPreviewGrid"></div>
            </div>
            <div class="btn-row">
                <button class="btn btn-secondary" onclick="goStep(1)">&larr; Back</button>
                <button class="btn btn-primary" onclick="goStep(3)">Next: Interior &rarr;</button>
            </div>
        </div>

        <!-- Step 3: Interior -->
        <div class="card" id="step3">
            <h2>Step 3 — Interior Photos</h2>
            <p class="subtitle">Optional. Dashboard, seats, steering wheel, infotainment, etc.</p>
            <div class="dropzone" id="intDZ" onclick="document.getElementById('photoInput').click()">
                <div class="dz-icon">👌</div>
                <div class="dz-text">Click or drag interior photos here</div>
                <div class="dz-hint">JPG, PNG — any number of photos</div>
            </div>
            <input type="file" id="photoInput" accept="image/*" multiple style="display:none">
            <div id="intPhotoCount" class="file-count"></div>
            <div class="preview-grid" id="intPreviewGrid"></div>
            <div class="btn-row">
                <button class="btn btn-secondary" onclick="goStep(2)">&larr; Back</button>
                <button class="btn btn-primary" onclick="goStep(4)">Next: Review &rarr;</button>
            </div>
        </div>

        <!-- Step 4: Review & Submit -->
        <div class="card" id="step4">
            <h2>Step 4 — Review &amp; Submit</h2>
            <p class="subtitle">Confirm your details before uploading.</p>
            <div id="reviewContent"></div>
            <div class="progress-wrap" id="progressWrap">
                <div class="progress-bar"><div class="progress-fill" id="progressBar"></div></div>
                <div class="progress-text"><span id="progressText">Preparing...</span> <span class="progress-pct" id="progressPct"></span></div>
            </div>
            <div class="btn-row" id="submitBtnRow">
                <button class="btn btn-secondary" onclick="goStep(3)">&larr; Back</button>
                <button class="btn btn-success" id="submitBtn" onclick="uploadAll()">&#128640; Create 360 Viewer</button>
            </div>
        </div>
    </div>

    <script>
        let photoFiles = [];
        let extPhotoFiles = [];
        let videoFile = null;
        let currentStep = 1;
        let uploadType = 'video';

        function goStep(n) {{
            if (n === 4) buildReview();
            document.querySelectorAll('.card').forEach(c => c.classList.remove('active'));
            document.getElementById('step' + n).classList.add('active');
            currentStep = n;
            updateStepDots(n);
            window.scrollTo({{top:0, behavior:'smooth'}});
        }}

        function updateStepDots(n) {{
            for (let i = 1; i <= 4; i++) {{
                const dot = document.getElementById('sd' + i);
                const line = document.getElementById('sl' + i);
                if (i < n) {{ dot.className = 'step-dot done'; dot.textContent = '\u2713'; }}
                else if (i === n) {{ dot.className = 'step-dot active'; dot.textContent = i; }}
                else {{ dot.className = 'step-dot pending'; dot.textContent = i; }}
                if (line) line.className = 'step-line' + (i < n ? ' done' : '');
            }}
        }}

        function selectType(t) {{
            uploadType = t;
            document.getElementById('typeVideo').classList.toggle('selected', t === 'video');
            document.getElementById('typePhotos').classList.toggle('selected', t === 'photos');
            document.getElementById('videoSection').style.display = t === 'video' ? '' : 'none';
            document.getElementById('photosSection').style.display = t === 'photos' ? '' : 'none';
        }}

        // Drag and drop
        function setupDZ(dzId, inputId, onFiles) {{
            const dz = document.getElementById(dzId);
            dz.addEventListener('dragover', e => {{ e.preventDefault(); dz.classList.add('dragover'); }});
            dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
            dz.addEventListener('drop', e => {{
                e.preventDefault(); dz.classList.remove('dragover');
                onFiles(Array.from(e.dataTransfer.files));
            }});
            document.getElementById(inputId).addEventListener('change', e => onFiles(Array.from(e.target.files)));
        }}

        function showPreviews(files, gridId, countId, label) {{
            const grid = document.getElementById(gridId);
            grid.innerHTML = '';
            files.forEach((f, i) => {{
                if (!f.type.startsWith('image/')) return;
                const div = document.createElement('div'); div.className = 'preview-item';
                const img = document.createElement('img');
                img.src = URL.createObjectURL(f);
                div.appendChild(img);
                grid.appendChild(div);
            }});
            document.getElementById(countId).textContent = files.length + ' ' + label + ' selected';
        }}

        setupDZ('videoDZ', 'videoInput', files => {{
            const f = files.find(f => f.type.startsWith('video/'));
            if (f) {{ videoFile = f; document.getElementById('videoInfo').textContent = f.name + ' (' + (f.size/1024/1024).toFixed(1) + ' MB)'; }}
        }});
        setupDZ('extDZ', 'extPhotoInput', files => {{
            extPhotoFiles = files.filter(f => f.type.startsWith('image/'));
            showPreviews(extPhotoFiles, 'extPreviewGrid', 'extPhotoCount', 'exterior photo(s)');
        }});
        setupDZ('intDZ', 'photoInput', files => {{
            photoFiles = files.filter(f => f.type.startsWith('image/'));
            showPreviews(photoFiles, 'intPreviewGrid', 'intPhotoCount', 'interior photo(s)');
        }});

        function buildReview() {{
            const items = [
                ['Year', document.getElementById('f_year').value || '—'],
                ['Make', document.getElementById('f_make').value || '—'],
                ['Model', document.getElementById('f_model').value || '—'],
                ['Color', document.getElementById('f_color').value || '—'],
                ['Price', document.getElementById('f_price').value || '—'],
                ['Mileage', document.getElementById('f_mileage').value || '—'],
                ['Upload Type', uploadType === 'video' ? 'Walkaround Video' : 'Photos'],
                ['Exterior Files', uploadType === 'video' ? (videoFile ? videoFile.name : 'None') : extPhotoFiles.length + ' photos'],
                ['Interior Photos', photoFiles.length + ' photos'],
            ];
            document.getElementById('reviewContent').innerHTML = items.map(([l,v]) =>
                `<div class="review-item"><span class="label">${{l}}</span><span class="value">${{v}}</span></div>`
            ).join('');
        }}

        function setProgress(pct, text) {{
            document.getElementById('progressBar').style.width = pct + '%';
            document.getElementById('progressText').textContent = text;
            document.getElementById('progressPct').textContent = pct + '%';
        }}

        async function uploadAll() {{
            const hasVideo = uploadType === 'video' && videoFile;
            const hasPhotos = uploadType === 'photos' && extPhotoFiles.length > 0;
            if (!hasVideo && !hasPhotos) {{ alert('Please add exterior content in Step 2'); goStep(2); return; }}
            if (hasPhotos && extPhotoFiles.length < 4) {{ alert('Upload at least 4 exterior photos'); goStep(2); return; }}

            const btn = document.getElementById('submitBtn');
            btn.disabled = true;
            document.getElementById('submitBtnRow').querySelector('.btn-secondary').disabled = true;
            document.getElementById('progressWrap').style.display = 'block';

            const year = document.getElementById('f_year').value;
            const make = document.getElementById('f_make').value;
            const model = document.getElementById('f_model').value;
            const color = document.getElementById('f_color').value;
            const price = document.getElementById('f_price').value;
            const mileage = document.getElementById('f_mileage').value;

            try {{
                let usedVin = '';

                if (hasVideo) {{
                    setProgress(5, 'Uploading video...');
                    const fd = new FormData();
                    fd.append('vin', '');
                    fd.append('video', videoFile);
                    fd.append('year', year); fd.append('make', make); fd.append('model', model);
                    const r1 = await fetch('/api/upload', {{ method: 'POST', body: fd }});
                    if (!r1.ok) throw new Error((await r1.json()).detail || 'Video upload failed');
                    const d1 = await r1.json();
                    if (d1.vin) usedVin = d1.vin;
                    setProgress(50, 'Vehicle ID: ' + usedVin + ' — Generating 360 spin...');
                    await fetch('/api/finalize/' + usedVin, {{ method: 'POST' }});
                }} else {{
                    for (let i = 0; i < extPhotoFiles.length; i++) {{
                        const pct = Math.round(5 + (i / extPhotoFiles.length) * 50);
                        setProgress(pct, 'Uploading exterior photo ' + (i+1) + '/' + extPhotoFiles.length);
                        const pf = new FormData();
                        pf.append('vin', usedVin);
                        pf.append('photo', extPhotoFiles[i]);
                        pf.append('index', i.toString());
                        pf.append('year', year); pf.append('make', make); pf.append('model', model);
                        const r = await fetch('/api/upload/exterior', {{ method: 'POST', body: pf }});
                        if (r.ok) {{ const d = await r.json(); if (d.vin) usedVin = d.vin; }}
                    }}
                    setProgress(55, 'Generating smooth 360 spin...');
                    try {{ await fetch('/api/finalize/' + usedVin, {{ method: 'POST' }}); }} catch(e) {{}}
                }}

                // Update vehicle metadata
                if (usedVin && (price || mileage || color)) {{
                    const mf = new FormData();
                    mf.append('price', price); mf.append('mileage', mileage); mf.append('color', color);
                    await fetch('/api/vehicle/' + usedVin + '/meta', {{ method: 'POST', body: mf }}).catch(()=>{{}});
                }}

                // Upload interior photos
                for (let i = 0; i < photoFiles.length; i++) {{
                    const pct = Math.round(60 + (i / Math.max(photoFiles.length,1)) * 35);
                    setProgress(pct, 'Uploading interior photo ' + (i+1) + '/' + photoFiles.length);
                    const pf = new FormData();
                    pf.append('vin', usedVin);
                    pf.append('photo', photoFiles[i]);
                    await fetch('/api/upload/interior', {{ method: 'POST', body: pf }});
                }}

                setProgress(100, 'Done! Vehicle ID: ' + usedVin);
                btn.textContent = '\u2713 Done!';
                setTimeout(() => window.location.href = '/view/' + usedVin, 900);
            }} catch(e) {{
                alert('Upload error: ' + e.message);
                btn.disabled = false;
                btn.textContent = '\ud83d\ude80 Create 360 Viewer';
            }}
        }}
    </script>
</body>
</html>"""
    return HTMLResponse(html)


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
    
    db_upsert_vehicle(vin, year, make, model, 0, 0)
    
    return {"status": "ok", "vin": vin}


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
# API — UPLOAD VIDEO + METADATA
# =============================================================================
@app.post("/api/upload")
async def upload_video(
    vin: str = Form(""),
    year: str = Form(""),
    make: str = Form(""),
    model: str = Form(""),
    color: str = Form(""),
    video: UploadFile = File(...)
):
    # Auto-generate VIN if not provided
    if not vin or len(vin) < 17:
        import uuid
        vin = str(uuid.uuid4())[:17].upper().replace("-", "0")
    
    vin = validate_vin(vin)
    
    vehicle_dir = STORAGE_DIR / vin
    exterior_dir = vehicle_dir / "exterior"
    interior_dir = vehicle_dir / "interior"
    vehicle_dir.mkdir(parents=True, exist_ok=True)
    exterior_dir.mkdir(exist_ok=True)
    interior_dir.mkdir(exist_ok=True)
    
    # Save video temporarily
    video_path = vehicle_dir / "video_temp.mp4"
    content = await video.read()
    
    if len(content) > MAX_VIDEO_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"Video too large (max {MAX_VIDEO_MB}MB)")
    
    with open(video_path, "wb") as f:
        f.write(content)
    
    # Extract frames
    try:
        num_extracted = extract_frames(video_path, exterior_dir, NUM_EXTERIOR_FRAMES)
    except Exception as e:
        # Cleanup on failure
        if video_path.exists():
            video_path.unlink()
        raise HTTPException(status_code=500, detail=f"Frame extraction failed: {str(e)}")
    
    # Cleanup temp video
    if video_path.exists():
        video_path.unlink()
    
    # Generate thumbnail from first frame
    generate_thumbnail(exterior_dir, vehicle_dir / "thumbnail.jpg")
    
    # Save to DB
    db_upsert_vehicle(vin, year, make, model, num_extracted, 0)
    
    return {"status": "ok", "vin": vin, "frames_extracted": num_extracted}


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
    
    # Get all exterior frames
    all_frames = sorted(exterior_dir.glob("frame_*.jpg"))
    
    # Smart interpolation: if < 18 frames, interpolate up to 36; otherwise copy as-is
    if frame_count < 18:
        actual = interpolate_frames(exterior_dir, spin_dir, target_frames=36)
    else:
        for i, f in enumerate(all_frames):
            shutil.copy(f, spin_dir / f"spin_{i+1:04d}.jpg")
        actual = frame_count
    
    # Generate thumbnail immediately
    first_spin = sorted(spin_dir.glob("spin_*.jpg"))
    if first_spin:
        generate_thumbnail(spin_dir, vehicle_dir / "thumbnail.jpg")
    
    # Update DB counts
    db_update_counts(vin)
    
    # Run background removal in background thread (doesn't block response)
    vehicle = db_get_vehicle(vin)
    wm_mode = vehicle.get("watermark_mode", "preview") if vehicle else "preview"
    turntable_style = vehicle.get("turntable_style", "studio_shadow") if vehicle else "studio_shadow"
    background_slug = vehicle.get("background_id", "") if vehicle else ""

    # Resolve background image path from slug
    bg_path = None
    if background_slug and background_slug != "none":
        conn = sqlite3.connect(str(DB_PATH))
        bg_row = conn.execute("SELECT url FROM backgrounds WHERE slug=?", (background_slug,)).fetchone()
        conn.close()
        if bg_row and bg_row[0]:
            # Convert URL to local path
            bg_url = bg_row[0]  # e.g. /static/backgrounds/studio_shadow.jpg
            if bg_url.startswith("/static/"):
                bg_path = str(Path(__file__).parent / "static" / bg_url[len("/static/"):])

    t = threading.Thread(
        target=process_all_frames_background,
        args=(spin_dir, wm_mode, bg_path, turntable_style),
        daemon=True
    )
    t.start()

    return {"status": "ok", "spin_frames": actual, "original_frames": frame_count, "processing_bg": True,
            "turntable_style": turntable_style, "background_slug": background_slug}


@app.get("/api/finalize-status/{vin}")
async def finalize_status(vin: str):
    """Check if background removal is done for a vehicle."""
    vin = vin.upper()
    spin_dir = STORAGE_DIR / vin / "spin"
    frames = list(spin_dir.glob("spin_*.jpg")) if spin_dir.exists() else []
    return {"vin": vin, "spin_frames": len(frames), "ready": len(frames) > 0}


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
    <title>Tag Features - LOTVUE</title>
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
        <div class="logo">LOTVUE - Tag Features</div>
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
        ext_urls_js = _json.dumps([f"{frame_path_prefix}/{f}" for f in ext_frames])
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
<title>{title} — LotVue360</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/pannellum@2.5.6/build/pannellum.css"/>
<script src="https://cdn.jsdelivr.net/npm/pannellum@2.5.6/build/pannellum.js"></script>
<style>
/* ── Reset & Base ─────────────────────────────────────────────────────── */
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0d1117;color:#e6edf3;min-height:100vh;}}
/* ── Layout ───────────────────────────────────────────────────────────── */
.viewer-wrap{{max-width:1100px;margin:0 auto;padding:1rem;}}
/* ── Header ───────────────────────────────────────────────────────────── */
.viewer-header{{display:flex;align-items:center;justify-content:space-between;padding:0.75rem 1rem;background:#161b22;border:1px solid #30363d;border-radius:10px 10px 0 0;gap:1rem;}}
.dealer-logo{{height:36px;max-width:120px;object-fit:contain;}}
.dealer-logo-placeholder{{font-size:1.1rem;font-weight:700;color:#58a6ff;letter-spacing:1px;}}
.vehicle-title{{flex:1;text-align:center;}}
.vehicle-title h1{{font-size:1.1rem;font-weight:700;color:#e6edf3;}}
.vehicle-title .vin-badge{{font-size:0.7rem;color:#8b949e;font-family:monospace;}}
.header-actions{{display:flex;gap:0.5rem;}}
.btn-icon{{background:#21262d;border:1px solid #30363d;color:#8b949e;padding:0.4rem 0.7rem;border-radius:6px;cursor:pointer;font-size:0.8rem;transition:all 0.2s;}}
.btn-icon:hover{{background:#30363d;color:#e6edf3;}}
/* ── Main viewer area ─────────────────────────────────────────────────── */
.viewer-body{{display:flex;gap:0;background:#0d1117;border-left:1px solid #30363d;border-right:1px solid #30363d;}}
.view-area{{flex:1;min-height:480px;position:relative;background:#010409;}}
/* ── Specs sidebar ────────────────────────────────────────────────────── */
.specs-sidebar{{width:220px;background:#161b22;border-left:1px solid #30363d;padding:1rem;display:flex;flex-direction:column;gap:0.75rem;overflow-y:auto;}}
.specs-sidebar h3{{font-size:0.8rem;text-transform:uppercase;letter-spacing:1px;color:#8b949e;margin-bottom:0.25rem;}}
.bg-grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:6px;margin-top:4px;}}
.bg-thumb{{width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:4px;cursor:pointer;border:2px solid transparent;transition:border-color 0.2s;}}
.bg-thumb.active{{border-color:#58a6ff;}}
.bg-thumb-none{{width:100%;aspect-ratio:16/9;background:linear-gradient(135deg,#1a1a1a 50%,#2a2a2a 50%);border-radius:4px;cursor:pointer;border:2px solid transparent;display:flex;align-items:center;justify-content:center;font-size:0.65rem;color:#8b949e;transition:border-color 0.2s;}}
.bg-thumb-none.active{{border-color:#58a6ff;}}
.spec-row{{display:flex;flex-direction:column;gap:0.1rem;}}
.spec-label{{font-size:0.7rem;color:#8b949e;}}
.spec-value{{font-size:0.9rem;font-weight:600;color:#e6edf3;}}
.price-display{{font-size:1.4rem;font-weight:800;color:#3fb950;}}
.cta-btn{{background:#238636;color:#fff;border:none;padding:0.6rem 1rem;border-radius:6px;cursor:pointer;font-weight:600;font-size:0.85rem;width:100%;margin-top:auto;transition:background 0.2s;}}
.cta-btn:hover{{background:#2ea043;}}
/* ── Tab bar ──────────────────────────────────────────────────────────── */
.tab-bar{{display:flex;background:#161b22;border:1px solid #30363d;border-top:none;}}
.tab-btn{{flex:1;padding:0.7rem 0.5rem;background:transparent;border:none;color:#8b949e;cursor:pointer;font-size:0.8rem;font-weight:600;text-align:center;border-bottom:3px solid transparent;transition:all 0.2s;display:flex;flex-direction:column;align-items:center;gap:0.2rem;}}
.tab-btn:hover{{color:#e6edf3;background:#21262d;}}
.tab-btn.active{{color:#58a6ff;border-bottom-color:#58a6ff;background:#0d1117;}}
.tab-btn .tab-icon{{font-size:1.1rem;}}
.tab-btn.hidden{{display:none;}}
/* ── Tab panels ───────────────────────────────────────────────────────── */
.tab-panel{{display:none;width:100%;height:100%;}}
.tab-panel.active{{display:block;}}
/* ── Exterior spin ────────────────────────────────────────────────────── */
#spin-container{{width:100%;height:480px;position:relative;background:#010409;cursor:grab;touch-action:none;user-select:none;overflow:hidden;}}
#spin-container:active{{cursor:grabbing;}}
#spin-container img{{width:100%;height:100%;object-fit:contain;position:absolute;inset:0;pointer-events:none;display:none;}}
.frame-counter{{position:absolute;top:0.75rem;right:0.75rem;background:rgba(0,0,0,0.7);color:#e6edf3;padding:0.25rem 0.6rem;border-radius:12px;font-size:0.75rem;font-family:monospace;z-index:10;}}
.spin-hint{{position:absolute;bottom:0.75rem;left:50%;transform:translateX(-50%);background:rgba(0,0,0,0.7);color:#8b949e;padding:0.3rem 0.9rem;border-radius:12px;font-size:0.75rem;z-index:10;pointer-events:none;transition:opacity 0.5s;}}
.spin-controls{{position:absolute;bottom:0.75rem;right:0.75rem;display:flex;gap:0.4rem;z-index:10;}}
.spin-btn{{background:rgba(0,0,0,0.7);border:1px solid #30363d;color:#e6edf3;width:32px;height:32px;border-radius:6px;cursor:pointer;font-size:0.9rem;display:flex;align-items:center;justify-content:center;}}
.spin-btn:hover{{background:rgba(88,166,255,0.2);}}
/* ── Hotspot dots ─────────────────────────────────────────────────────── */
.hotspot-dot{{position:absolute;width:22px;height:22px;background:rgba(88,166,255,0.9);border:2px solid #fff;border-radius:50%;cursor:pointer;transform:translate(-50%,-50%);z-index:20;animation:pulse 2s infinite;}}
@keyframes pulse{{0%,100%{{box-shadow:0 0 0 0 rgba(88,166,255,0.6);}}50%{{box-shadow:0 0 0 8px rgba(88,166,255,0);}}}}
.hs-tooltip{{position:absolute;background:#1e3a5f;color:#fff;padding:0.4rem 0.8rem;border-radius:6px;font-size:0.8rem;font-weight:600;z-index:30;pointer-events:none;white-space:nowrap;transform:translate(-50%,-130%);box-shadow:0 4px 12px rgba(0,0,0,0.4);}}
.hs-tooltip::after{{content:'';position:absolute;top:100%;left:50%;transform:translateX(-50%);border:6px solid transparent;border-top-color:#1e3a5f;}}
/* ── Feature pills ────────────────────────────────────────────────────── */
.feature-pills{{display:none;flex-wrap:wrap;gap:0.4rem;padding:0.6rem 1rem;background:#0d1117;border-left:1px solid #30363d;border-right:1px solid #30363d;}}
.feature-pill{{background:#21262d;border:1px solid #30363d;color:#8b949e;padding:0.25rem 0.7rem;border-radius:20px;font-size:0.72rem;cursor:pointer;transition:all 0.2s;}}
.feature-pill:hover{{background:#58a6ff;color:#fff;border-color:#58a6ff;}}
/* ── Frame strip ──────────────────────────────────────────────────────── */
.frame-strip{{display:flex;gap:4px;padding:6px;background:#0d1117;border:1px solid #30363d;border-top:none;overflow-x:auto;scrollbar-width:thin;scrollbar-color:#30363d #0d1117;}}
.frame-strip::-webkit-scrollbar{{height:4px;}}
.frame-strip::-webkit-scrollbar-track{{background:#0d1117;}}
.frame-strip::-webkit-scrollbar-thumb{{background:#30363d;border-radius:2px;}}
.frame-thumb{{width:60px;height:38px;flex-shrink:0;border-radius:3px;overflow:hidden;cursor:pointer;border:2px solid transparent;transition:border-color 0.15s;}}
.frame-thumb:hover{{border-color:#58a6ff;}}
.frame-thumb.active{{border-color:#58a6ff;}}
.frame-thumb img{{width:100%;height:100%;object-fit:cover;}}
/* ── Interior panorama ────────────────────────────────────────────────── */
#pano-container{{width:100%;height:480px;position:relative;}}
.pano-scene-selector{{position:absolute;top:0.75rem;left:0.75rem;z-index:100;background:rgba(0,0,0,0.7);border:1px solid #30363d;color:#e6edf3;padding:0.4rem 0.7rem;border-radius:6px;font-size:0.8rem;cursor:pointer;}}
.int-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:0.5rem;padding:0.75rem;max-height:480px;overflow-y:auto;}}
.int-thumb{{aspect-ratio:16/10;border-radius:6px;overflow:hidden;cursor:pointer;border:2px solid transparent;transition:border-color 0.2s;}}
.int-thumb:hover{{border-color:#58a6ff;}}
.int-thumb img{{width:100%;height:100%;object-fit:cover;}}
/* ── Gallery ──────────────────────────────────────────────────────────── */
.gallery-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:0.5rem;padding:0.75rem;max-height:480px;overflow-y:auto;}}
.gallery-item{{aspect-ratio:16/10;border-radius:6px;overflow:hidden;cursor:pointer;position:relative;border:2px solid transparent;transition:border-color 0.2s;}}
.gallery-item:hover{{border-color:#58a6ff;}}
.gallery-item img{{width:100%;height:100%;object-fit:cover;}}
/* ── Video tab ────────────────────────────────────────────────────────── */
.video-container{{width:100%;height:480px;display:flex;align-items:center;justify-content:center;background:#010409;}}
.video-container video{{max-width:100%;max-height:100%;}}
.video-container iframe{{width:100%;height:100%;border:none;}}
.no-video{{text-align:center;color:#8b949e;padding:2rem;}}
.no-video .no-video-icon{{font-size:3rem;margin-bottom:1rem;}}
/* ── Lead form ────────────────────────────────────────────────────────── */
.lead-form-wrap{{padding:1.5rem;max-height:480px;overflow-y:auto;}}
.lead-form-wrap h2{{font-size:1.1rem;font-weight:700;margin-bottom:0.25rem;}}
.lead-form-wrap p{{font-size:0.85rem;color:#8b949e;margin-bottom:1.25rem;}}
.form-row{{display:grid;grid-template-columns:1fr 1fr;gap:0.75rem;margin-bottom:0.75rem;}}
.form-row.full{{grid-template-columns:1fr;}}
.form-group label{{display:block;font-size:0.75rem;color:#8b949e;margin-bottom:0.3rem;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;}}
.form-group input,.form-group textarea,.form-group select{{width:100%;background:#21262d;border:1px solid #30363d;color:#e6edf3;padding:0.6rem 0.75rem;border-radius:6px;font-size:0.9rem;outline:none;transition:border-color 0.2s;}}
.form-group input:focus,.form-group textarea:focus{{border-color:#58a6ff;}}
.form-group textarea{{resize:vertical;min-height:80px;}}
.checkbox-row{{display:flex;gap:1rem;margin-bottom:1rem;flex-wrap:wrap;}}
.checkbox-label{{display:flex;align-items:center;gap:0.4rem;font-size:0.85rem;cursor:pointer;}}
.checkbox-label input{{width:auto;}}
.submit-btn{{background:#238636;color:#fff;border:none;padding:0.75rem 2rem;border-radius:6px;font-size:0.95rem;font-weight:700;cursor:pointer;width:100%;transition:background 0.2s;}}
.submit-btn:hover{{background:#2ea043;}}
.submit-btn:disabled{{background:#21262d;color:#8b949e;cursor:not-allowed;}}
.form-success{{text-align:center;padding:2rem;}}
.form-success .success-icon{{font-size:3rem;margin-bottom:1rem;}}
.form-success h3{{color:#3fb950;font-size:1.2rem;margin-bottom:0.5rem;}}
/* ── Footer ───────────────────────────────────────────────────────────── */
.viewer-footer{{display:flex;align-items:center;justify-content:space-between;padding:0.6rem 1rem;background:#161b22;border:1px solid #30363d;border-top:none;border-radius:0 0 10px 10px;}}
.powered-by{{font-size:0.7rem;color:#8b949e;}}
.powered-by a{{color:#58a6ff;text-decoration:none;}}
.footer-actions{{display:flex;gap:0.5rem;}}
/* ── Share panel ──────────────────────────────────────────────────────── */
.share-panel{{display:none;position:absolute;bottom:3rem;right:1rem;background:#161b22;border:1px solid #30363d;border-radius:8px;padding:1rem;z-index:200;min-width:280px;box-shadow:0 8px 24px rgba(0,0,0,0.5);}}
.share-panel.open{{display:block;}}
.share-panel h4{{font-size:0.85rem;margin-bottom:0.75rem;color:#e6edf3;}}
.share-url{{display:flex;gap:0.4rem;margin-bottom:0.75rem;}}
.share-url input{{flex:1;background:#21262d;border:1px solid #30363d;color:#e6edf3;padding:0.4rem 0.6rem;border-radius:4px;font-size:0.75rem;}}
.share-url button{{background:#238636;border:none;color:#fff;padding:0.4rem 0.7rem;border-radius:4px;cursor:pointer;font-size:0.75rem;}}
.embed-code{{background:#21262d;border:1px solid #30363d;border-radius:4px;padding:0.5rem;font-size:0.7rem;font-family:monospace;color:#8b949e;word-break:break-all;margin-bottom:0.5rem;}}
.share-btns{{display:flex;gap:0.4rem;flex-wrap:wrap;}}
.share-btn{{background:#21262d;border:1px solid #30363d;color:#8b949e;padding:0.35rem 0.7rem;border-radius:4px;cursor:pointer;font-size:0.75rem;transition:all 0.2s;}}
.share-btn:hover{{background:#30363d;color:#e6edf3;}}
/* ── Lightbox ─────────────────────────────────────────────────────────── */
#lightbox{{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.92);z-index:1000;align-items:center;justify-content:center;}}
#lightbox.open{{display:flex;}}
#lightbox img{{max-width:90vw;max-height:90vh;object-fit:contain;border-radius:4px;}}
#lightbox-close{{position:absolute;top:1rem;right:1rem;background:rgba(255,255,255,0.1);border:none;color:#fff;font-size:1.5rem;width:40px;height:40px;border-radius:50%;cursor:pointer;}}
/* ── Toast ────────────────────────────────────────────────────────────── */
.toast{{position:fixed;bottom:2rem;left:50%;transform:translateX(-50%);background:#1e3a5f;color:#fff;padding:0.6rem 1.4rem;border-radius:8px;font-size:0.9rem;font-weight:600;z-index:3000;box-shadow:0 4px 16px rgba(0,0,0,0.3);opacity:0;transition:opacity 0.3s;pointer-events:none;}}
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
    {'<img class="dealer-logo" src="' + dealer_logo + '" alt="Dealer">' if dealer_logo else '<div class="dealer-logo-placeholder">LotVue360</div>'}
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
          <div class="spin-hint" id="spinHint">&#8592; Drag to spin &#8594;</div>
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
    <div class="powered-by">Powered by <a href="https://lotvue360.com" target="_blank">LotVue360</a></div>
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
  container.addEventListener('touchend', spinMouseUp);
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

function spinMouseDown(e) {{
  stopAuto();
  spinDragging = true;
  spinStartX = e.clientX;
  spinStartFrame = spinCur;
  document.getElementById('spinHint').style.opacity = '0';
}}
function spinMouseMove(e) {{
  if (!spinDragging || !EXT_URLS.length) return;
  const dx = e.clientX - spinStartX;
  const delta = Math.round(dx / (container_width() / EXT_URLS.length));
  const n = ((spinStartFrame - delta) % EXT_URLS.length + EXT_URLS.length) % EXT_URLS.length;
  showSpinFrame(n);
}}
function spinMouseUp() {{ spinDragging = false; }}
function spinTouchStart(e) {{
  stopAuto();
  spinDragging = true;
  spinStartX = e.touches[0].clientX;
  spinStartFrame = spinCur;
  document.getElementById('spinHint').style.opacity = '0';
}}
function spinTouchMove(e) {{
  if (!spinDragging || !EXT_URLS.length) return;
  e.preventDefault();
  const dx = e.touches[0].clientX - spinStartX;
  const delta = Math.round(dx / (container_width() / EXT_URLS.length));
  const n = ((spinStartFrame - delta) % EXT_URLS.length + EXT_URLS.length) % EXT_URLS.length;
  showSpinFrame(n);
}}
function container_width() {{
  return document.getElementById('spin-container').offsetWidth || 800;
}}

function startAuto() {{
  if (autoRunning) return;
  autoRunning = true;
  document.getElementById('autoBtn').textContent = '⏸';
  autoInterval = setInterval(() => spinStep(1), 60);
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
        * {{ margin:0; padding:0; box-sizing:border-box; }}
        body {{ font-family:'Segoe UI',sans-serif; background:#f0f4f8; color:#1a2332; overflow-x:hidden; }}
        .tabs {{ display:flex; background:#e8edf2; border-bottom:1px solid #c5cdd6; }}
        .tab {{ flex:1; padding:0.7rem; background:transparent; border:none; color:#484f58; cursor:pointer; text-align:center; font-size:0.9rem; }}
        .tab.active {{ color:#58a6ff; box-shadow:inset 0 -2px 0 #58a6ff; }}
        .tc {{ display:none; }} .tc.active {{ display:block; }}
        #spin {{ width:100%; height:420px; cursor:grab; position:relative; background:#010409; touch-action:none; user-select:none; }}
        #spin:active {{ cursor:grabbing; }}
        #spin img {{ width:100%; height:100%; object-fit:contain; position:absolute; inset:0; pointer-events:none; }}
        .hint {{ position:absolute; bottom:0.6rem; left:50%; transform:translateX(-50%); background:rgba(0,0,0,0.7); padding:0.3rem 0.8rem; border-radius:12px; font-size:0.75rem; color:#8b949e; }}
        .ig {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(140px,1fr)); gap:0.5rem; padding:0.5rem; }}
        .ig div {{ aspect-ratio:16/10; }}
        .brand {{ text-align:center; padding:0.4rem; font-size:0.7rem; color:#c5cdd6; }}
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
    <div class="brand">Powered by LOTVUE</div>
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
    <title>Dealer Settings — LOTVUE</title>
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
        <div class="logo">LOTVUE — Settings</div>
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

        return {"status": "ok", "decoded": decoded}

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
    conn.execute("""
        INSERT INTO leads (vin, name, email, phone, message, wants_video, wants_test_drive, source, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        lead.vin.upper(), lead.name, lead.email, lead.phone or "",
        lead.message or "", lead.wants_video or 0, lead.wants_test_drive or 0,
        lead.source or "viewer", datetime.now().isoformat()
    ))
    conn.commit()
    conn.close()
    print(f"[Lead] New inquiry: {lead.name} <{lead.email}> for VIN {lead.vin}")
    return {"status": "ok", "message": "Inquiry received"}


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
# ROUTES — AI COMMERCIAL GENERATOR
# =============================================================================

import threading as _threading
import subprocess as _subprocess
import json as _json_mod

# Paths for commercial generator
_COMFYUI_PYTHON = r".\ComfyUI\.venv\Scripts\python.exe"
_COMMERCIAL_SCRIPT = r".\360Car\app\commercial_pipeline.py"


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
                "--dealer", "our dealership"
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

    # Find best image for this vehicle
    spin_dir = os.path.join(str(STORAGE_DIR), vin, "spin")
    image_path = None
    if os.path.exists(spin_dir):
        frames = sorted([f for f in os.listdir(spin_dir) if f.lower().endswith((".jpg", ".jpeg", ".png"))])
        if frames:
            idx = max(0, len(frames) // 4)
            image_path = os.path.join(spin_dir, frames[idx])

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
    <title>Public URL — LOTVUE</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Segoe UI', -apple-system, sans-serif; background: #f0f4f8; color: #1a2332; min-height: 100vh; }
        .header { background: linear-gradient(135deg, #e8edf2 0%, #dde3ea 100%); padding: 1rem 2rem; border-bottom: 1px solid #c5cdd6; }
        .logo { font-size: 1.4rem; font-weight: 700; color: #58a6ff; }
        .container { max-width: 800px; margin: 0 auto; padding: 2rem; }
        .card { background: #dde3ea; border: 1px solid #c5cdd6; border-radius: 8px; padding: 1.5rem; margin-bottom: 1.5rem; }
        h1 { font-size: 1.8rem; margin-bottom: 0.5rem; }
        .subtitle { color: #8b949e; margin-bottom: 2rem; }
        h2 { color: #1a2332; font-size: 1.1rem; margin-bottom: 1rem; }
        code { background: #e8edf2; padding: 0.2rem 0.5rem; border-radius: 4px; font-family: monospace; color: #58a6ff; }
        pre { background: #e8edf2; padding: 1rem; border-radius: 6px; overflow-x: auto; font-size: 0.85rem; color: #8b949e; }
        .warning { background: #1c1209; border: 1px solid #3d2e00; border-radius: 6px; padding: 1rem; color: #d29922; margin-bottom: 1.5rem; }
    </style>
</head>
<body>
    <div class="header">
        <div class="logo">LOTVUE — Public URL</div>
    </div>
    <div class="container">
        <h1>Share Your 360 Views</h1>
        <p class="subtitle">Make your vehicle showcases accessible to anyone, anywhere</p>
        
        <div class="warning">
            <strong>Note:</strong> Your local server is currently only accessible on your computer. To share links with customers, you need a public URL.
        </div>
        
        <div class="card">
            <h2>Option 1: Cloudflare Tunnel (Recommended)</h2>
            <p style="color:#8b949e;margin-bottom:1rem;">Free, permanent URL with your own domain.</p>
            <pre>1. Download Cloudflare WARP: https://cloudflare.com/warp
2. Run: cloudflared tunnel create lotvue
3. Run: cloudflared tunnel route dns lotvue yourname.example.com
4. Run: cloudflared tunnel run lotvue</pre>
        </div>
        
        <div class="card">
            <h2>Option 2: ngrok (Quick Setup)</h2>
            <p style="color:#8b949e;margin-bottom:1rem;">Get a temporary public URL in seconds.</p>
            <pre>1. Download ngrok: https://ngrok.com/download
2. Run: ngrok http 8001
3. Copy the URL shown (e.g., https://abc123.ngrok.io)</pre>
        </div>
        
        <div class="card">
            <h2>Current Local URL</h2>
            <code>http://localhost:8001</code>
            <p style="color:#8b949e;margin-top:1rem;">This works on your computer only.</p>
        </div>
    </div>
</body>
</html>"""
    return HTMLResponse(html)


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
    <title>Lotvue360 — Mobile Capture</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Segoe UI', -apple-system, sans-serif; background: #0d1117; color: #c9d1d9; min-height: 100vh; }
        .header { background: #161b22; padding: 1rem 1.5rem; border-bottom: 1px solid #30363d; display: flex; justify-content: space-between; align-items: center; }
        .logo { font-size: 1.2rem; font-weight: 700; color: #58a6ff; }
        .container { max-width: 600px; margin: 0 auto; padding: 1.5rem; }
        h1 { font-size: 1.5rem; margin-bottom: 0.5rem; color: #f0f6fc; }
        .subtitle { color: #8b949e; margin-bottom: 2rem; font-size: 0.95rem; }
        
        .step-card { background: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 1.5rem; margin-bottom: 1rem; }
        .step-num { display: inline-block; width: 28px; height: 28px; background: #238636; border-radius: 50%; text-align: center; line-height: 28px; font-weight: 700; font-size: 0.85rem; margin-right: 0.8rem; }
        .step-title { font-size: 1rem; font-weight: 600; margin-bottom: 0.5rem; color: #f0f6fc; }
        .step-desc { color: #8b949e; font-size: 0.9rem; line-height: 1.5; }
        
        .position-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.5rem; margin-top: 1rem; }
        .pos-item { background: #21262d; border: 2px solid #30363d; border-radius: 8px; padding: 0.6rem 0.3rem; text-align: center; font-size: 0.75rem; color: #8b949e; }
        .pos-item.active { border-color: #238636; background: rgba(35,134,54,0.15); color: #3fb950; }
        .pos-item span { display: block; font-size: 1.4rem; margin-bottom: 0.2rem; }
        
        .camera-preview { background: #000; border-radius: 12px; aspect-ratio: 4/3; display: flex; align-items: center; justify-content: center; margin-bottom: 1rem; position: relative; overflow: hidden; border: 2px solid #30363d; }
        .camera-hint { color: #484f58; font-size: 0.9rem; text-align: center; padding: 2rem; }
        .camera-guide { position: absolute; inset: 0; pointer-events: none; }
        .camera-video { width: 100%; height: 100%; object-fit: cover; border-radius: 10px; }
        .capture-btn { position: absolute; bottom: 1.5rem; left: 50%; transform: translateX(-50%); width: 70px; height: 70px; border-radius: 50%; background: #fff; border: 4px solid #238636; cursor: pointer; }
        .capture-btn:active { background: #ccc; }
        .camera-mode { display: none; }
        .camera-mode.active { display: block; }
        .car-outline { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); width: 60%; opacity: 0.3; }
        .car-outline svg { width: 100%; height: auto; }
        .position-indicator { position: absolute; bottom: 1rem; left: 50%; transform: translateX(-50%); background: rgba(0,0,0,0.7); padding: 0.4rem 1rem; border-radius: 20px; font-size: 0.85rem; color: #58a6ff; }
        
        .controls { display: flex; gap: 1rem; justify-content: center; margin-top: 1rem; }
        .btn { background: #238636; border: none; padding: 0.8rem 1.5rem; color: white; border-radius: 8px; cursor: pointer; font-weight: 600; font-size: 1rem; text-decoration: none; display: inline-block; }
        .btn:hover { background: #2ea043; }
        .btn-outline { background: transparent; border: 1px solid #30363d; color: #8b949e; }
        
        .input-group { margin-bottom: 1rem; }
        .input-group label { display: block; color: #8b949e; font-size: 0.85rem; margin-bottom: 0.3rem; }
        .input-group input { width: 100%; padding: 0.7rem; background: #0d1117; border: 1px solid #30363d; border-radius: 6px; color: #c9d1d9; font-size: 1rem; }
        .input-group input:focus { outline: none; border-color: #58a6ff; }
        
        .tip-box { background: rgba(88,166,255,0.1); border: 1px solid rgba(88,166,255,0.3); border-radius: 8px; padding: 1rem; margin-top: 1rem; }
        .tip-box p { color: #58a6ff; font-size: 0.85rem; line-height: 1.5; }
        
        .progress-bar { height: 4px; background: #30363d; border-radius: 2px; margin-top: 1rem; overflow: hidden; }
        .progress-fill { height: 100%; background: #238636; transition: width 0.3s; }
        
        .upload-area { background: #161b22; border: 2px dashed #30363d; border-radius: 12px; padding: 2rem; text-align: center; cursor: pointer; transition: all 0.2s; }
        .upload-area:hover { border-color: #58a6ff; background: rgba(88,166,255,0.05); }
        .upload-area p { color: #8b949e; font-size: 0.9rem; }
        .upload-area input { display: none; }
    </style>
</head>
<body>
    <div class="header">
        <div class="logo">Lotvue360</div>
        <a href="/" style="color:#58a6ff;text-decoration:none;font-size:0.9rem;">Dashboard</a>
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
                            <rect x="20" y="40" width="160" height="50" rx="10" fill="#30363d"/>
                            <rect x="40" y="25" width="80" height="25" rx="5" fill="#30363d"/>
                            <circle cx="50" cy="90" r="15" fill="none" stroke="#30363d" stroke-width="3"/>
                            <circle cx="150" cy="90" r="15" fill="none" stroke="#30363d" stroke-width="3"/>
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
        
        <div class="tip-box" style="background:rgba(35,134,54,0.15);border-color:rgba(35,134,54,0.4);">
            <p style="color:#3fb950;"><strong>📱 Install as App:</strong><br>
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
                    document.getElementById('posIndicator').style.background = 'rgba(35,134,54,0.8)';
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
            document.getElementById('posIndicator').style.background = 'rgba(0,0,0,0.7)';
            document.getElementById('progressFill').style.width = '0%';
            document.getElementById('controls').style.display = 'flex';
            document.getElementById('uploadArea').style.display = 'none';
            document.getElementById('uploadBtn').style.display = 'none';
        }}
        
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
    <title>Lotvue360 — Pricing</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Segoe UI', -apple-system, sans-serif; background: #0d1117; color: #c9d1d9; min-height: 100vh; }
        .header { background: #161b22; padding: 1rem 2rem; border-bottom: 1px solid #30363d; display: flex; justify-content: space-between; align-items: center; }
        .logo { font-size: 1.4rem; font-weight: 700; color: #58a6ff; }
        .container { max-width: 900px; margin: 0 auto; padding: 3rem 1.5rem; text-align: center; }
        h1 { font-size: 2.2rem; margin-bottom: 1rem; color: #f0f6fc; }
        .subtitle { color: #8b949e; font-size: 1.1rem; margin-bottom: 3rem; }
        
        .plans { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1.5rem; margin: 2rem 0; }
        .plan { background: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 2rem; text-align: center; transition: all 0.2s; }
        .plan.featured { border-color: #238636; box-shadow: 0 0 20px rgba(35,134,54,0.15); }
        .plan-badge { background: #238636; color: white; padding: 0.2rem 0.6rem; border-radius: 20px; font-size: 0.7rem; font-weight: 600; display: inline-block; margin-bottom: 0.5rem; }
        .plan-name { font-size: 1.2rem; font-weight: 600; color: #f0f6fc; margin-bottom: 0.5rem; }
        .plan-price { font-size: 2rem; font-weight: 700; color: #58a6ff; margin-bottom: 0.3rem; }
        .plan-price span { font-size: 0.9rem; font-weight: 400; color: #8b949e; }
        .plan-credits { color: #8b949e; font-size: 0.85rem; margin-bottom: 1.5rem; }
        .plan-features { text-align: left; margin-bottom: 1.5rem; }
        .plan-features li { color: #8b949e; font-size: 0.85rem; margin-bottom: 0.5rem; list-style: none; padding-left: 1.2rem; position: relative; }
        .plan-features li::before { content: '✓'; position: absolute; left: 0; color: #3fb950; }
        
        .btn { display: inline-block; background: #238636; border: none; padding: 0.8rem 1.5rem; color: white; border-radius: 8px; cursor: pointer; font-weight: 600; font-size: 1rem; text-decoration: none; }
        .btn:hover { background: #2ea043; }
        .btn-outline { background: transparent; border: 1px solid #30363d; color: #8b949e; }
        .btn-full { width: 100%; }
        
        .faq { text-align: left; margin-top: 3rem; }
        .faq h2 { color: #f0f6fc; font-size: 1.3rem; margin-bottom: 1.5rem; }
        .faq-item { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 1.2rem; margin-bottom: 0.8rem; }
        .faq-q { color: #f0f6fc; font-weight: 600; margin-bottom: 0.5rem; }
        .faq-a { color: #8b949e; font-size: 0.9rem; line-height: 1.5; }
        
        @media (max-width: 600px) { .plans { grid-template-columns: 1fr; } }
    </style>
</head>
<body>
    <div class="header">
        <div class="logo">Lotvue360</div>
        <a href="/" style="color:#58a6ff;text-decoration:none;">Dashboard</a>
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
    <title>Lotvue360 — Sign Up</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Segoe UI', sans-serif; background: #0d1117; color: #c9d1d9; min-height: 100vh; display: flex; align-items: center; justify-content: center; }
        .card { background: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 2.5rem; width: 100%; max-width: 400px; }
        .logo { font-size: 1.6rem; font-weight: 700; color: #58a6ff; text-align: center; margin-bottom: 1.5rem; }
        h1 { font-size: 1.3rem; color: #f0f6fc; text-align: center; margin-bottom: 0.5rem; }
        .subtitle { color: #8b949e; text-align: center; margin-bottom: 2rem; font-size: 0.9rem; }
        .fg { margin-bottom: 1.2rem; }
        .fg label { display: block; color: #8b949e; font-size: 0.85rem; margin-bottom: 0.3rem; }
        .fg input { width: 100%; padding: 0.7rem; background: #0d1117; border: 1px solid #30363d; border-radius: 6px; color: #c9d1d9; font-size: 1rem; }
        .fg input:focus { outline: none; border-color: #58a6ff; }
        .btn { width: 100%; padding: 0.8rem; background: #238636; border: none; border-radius: 6px; color: white; font-size: 1rem; font-weight: 600; cursor: pointer; }
        .btn:hover { background: #2ea043; }
        .links { text-align: center; margin-top: 1.5rem; font-size: 0.9rem; }
        .links a { color: #58a6ff; text-decoration: none; }
        .success { background: rgba(63,185,80,0.15); border: 1px solid #238636; border-radius: 6px; padding: 0.8rem; color: #3fb950; text-align: center; margin-bottom: 1rem; display: none; }
    </style>
</head>
<body>
    <div class="card">
        <div class="logo">Lotvue360</div>
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
        async function doRegister(e) {{
            e.preventDefault();
            const fd = new FormData(e.target);
            const r = await fetch('/api/register', {{ method: 'POST', body: fd }});
            const data = await r.json();
            if (data.status === 'ok') {{
                document.getElementById('successMsg').style.display = 'block';
                sessionStorage.setItem('user_email', data.email);
                setTimeout(() => window.location.href = '/', 1000);
            }} else {{
                alert(data.error || 'Registration failed');
            }}
        }}
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
    <title>Lotvue360 — Sign In</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Segoe UI', sans-serif; background: #0d1117; color: #c9d1d9; min-height: 100vh; display: flex; align-items: center; justify-content: center; }
        .card { background: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 2.5rem; width: 100%; max-width: 400px; }
        .logo { font-size: 1.6rem; font-weight: 700; color: #58a6ff; text-align: center; margin-bottom: 1.5rem; }
        h1 { font-size: 1.3rem; color: #f0f6fc; text-align: center; margin-bottom: 0.5rem; }
        .subtitle { color: #8b949e; text-align: center; margin-bottom: 2rem; font-size: 0.9rem; }
        .fg { margin-bottom: 1.2rem; }
        .fg label { display: block; color: #8b949e; font-size: 0.85rem; margin-bottom: 0.3rem; }
        .fg input { width: 100%; padding: 0.7rem; background: #0d1117; border: 1px solid #30363d; border-radius: 6px; color: #c9d1d9; font-size: 1rem; }
        .fg input:focus { outline: none; border-color: #58a6ff; }
        .btn { width: 100%; padding: 0.8rem; background: #238636; border: none; border-radius: 6px; color: white; font-size: 1rem; font-weight: 600; cursor: pointer; }
        .btn:hover { background: #2ea043; }
        .error { background: rgba(248,81,73,0.15); border: 1px solid #da3633; border-radius: 6px; padding: 0.8rem; color: #f85149; text-align: center; margin-bottom: 1rem; display: none; }
        .links { text-align: center; margin-top: 1.5rem; font-size: 0.9rem; }
        .links a { color: #58a6ff; text-decoration: none; }
    </style>
</head>
<body>
    <div class="card">
        <div class="logo">Lotvue360</div>
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
        async function doLogin(e) {{
            e.preventDefault();
            const fd = new FormData(e.target);
            const r = await fetch('/api/login', {{ method: 'POST', body: fd }});
            const data = await r.json();
            if (data.status === 'ok') {{
                sessionStorage.setItem('user_email', data.email);
                sessionStorage.setItem('user_credits', data.credits);
                sessionStorage.setItem('user_plan', data.plan);
                window.location.href = '/';
            }} else {{
                document.getElementById('errorMsg').style.display = 'block';
            }}
        }}
    </script>
</body>
</html>"""
    return HTMLResponse(html)


# =============================================================================
# CLIENT_A ONBOARDING — Phase 9
# Auto-creates [CLIENT_A]'s [CLIENT_A] Dealership account
# =============================================================================
def setup_client_a():
    """Run once to set up [CLIENT_A]'s dealer account."""
    dealer_email = "[REDACTED_EMAIL]"
    dealer_name = "client-a"
    dealer_display = "[CLIENT_A] Dealership"
    dealer_website = "client-a.com"
    
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
    print(f"[Lotvue360] [CLIENT_A] onboarded: {dealer_email} - {dealer_display}")

try:
    setup_client_a()
except Exception as e:
    print(f"[Lotvue360] [CLIENT_A] setup skipped: {e}")


# =============================================================================
# STATIC FILE SERVING
# =============================================================================
app.mount("/storage", StaticFiles(directory=str(STORAGE_DIR)), name="storage")
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


# =============================================================================
# RUN
# =============================================================================
if __name__ == "__main__":
    import uvicorn
    PORT = int(os.getenv("LOTVUE_PORT", 8001))
    uvicorn.run(app, host="0.0.0.0", port=PORT)
