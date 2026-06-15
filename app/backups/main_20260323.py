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

# --- Background Removal (rembg) ---
REMBG_AVAILABLE = False
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
DB_PATH = Path(r".\360Car\app\vehicles.db")
NUM_EXTERIOR_FRAMES = 36  # 10° per frame = smooth spin
MAX_VIDEO_MB = 500
MAX_PHOTO_MB = 30

STORAGE_DIR.mkdir(parents=True, exist_ok=True)
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
    conn.commit()
    conn.close()

init_db()

def db_upsert_vehicle(vin, year="", make="", model="", ext_frames=0, int_count=0, watermark_mode="preview", user_email=""):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        INSERT INTO vehicles (vin, year, make, model, exterior_frames, interior_count, created_at, watermark_mode, user_email)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(vin) DO UPDATE SET
            year=excluded.year, make=excluded.make, model=excluded.model,
            exterior_frames=excluded.exterior_frames, interior_count=excluded.interior_count,
            watermark_mode=excluded.watermark_mode, user_email=excluded.user_email
    """, (vin, year, make, model, ext_frames, int_count, datetime.now().isoformat(), watermark_mode, user_email))
    conn.commit()
    conn.close()

def db_get_vehicles():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM vehicles WHERE status='active' ORDER BY created_at DESC").fetchall()
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
            plan_id = session.get("metadata", {}).get("plan_id", "single")
            credits = int(session.get("metadata", {}).get("credits", "1"))
            user_email = session.get("metadata", {}).get("user_email", session.get("customer_email", ""))
            
            if user_email:
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


def process_all_frames_background(frames_dir: Path, watermark_mode: str = "preview"):
    """Process all frames in directory with background removal."""
    if not REMBG_AVAILABLE:
        return
    
    frames = sorted(list(frames_dir.glob("frame_*.jpg")) + list(frames_dir.glob("spin_*.jpg")))
    for frame_path in frames:
        try:
            with open(frame_path, "rb") as f:
                raw = f.read()
            result_bytes = remove_bg(raw)
            car = Image.open(io.BytesIO(result_bytes)).convert("RGBA")
            
            car.thumbnail((1920, 1080), Image.LANCZOS)
            
            bg = Image.new("RGB", (1920, 1080), (26, 26, 26))
            x = (1920 - car.width) // 2
            y = (1080 - car.height) // 2
            bg.paste(car, (x, y), car)
            
            if watermark_mode == "preview":
                bg = apply_watermark(bg, WATERMARK_PREVIEW_TEXT, 55)
            
            bg.save(frame_path, "JPEG", quality=92)
        except Exception as e:
            print(f"Background removal failed for {frame_path}: {e}")


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


def convert_photo_to_jpg(raw_bytes: bytes, filename: str, save_path: Path):
    """Convert any image format (including HEIC) to JPG."""
    ext = Path(filename).suffix.lower()
    
    if ext in [".heic", ".heif"] and not HEIC_SUPPORTED:
        raise HTTPException(
            status_code=400,
            detail="HEIC not supported. On iPhone: Settings → Camera → Formats → Most Compatible. Then retake photos as JPG."
        )
    
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img = img.convert("RGB")
        img.save(save_path, "JPEG", quality=90)
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
# ROUTES — DASHBOARD
# =============================================================================
@app.get("/")
async def dashboard():
    vehicles = db_get_vehicles()
    
    cards_html = ""
    if not vehicles:
        cards_html = '<div style="text-align:center;padding:4rem;color:#484f58;">No vehicles yet. Add your first one.</div>'
    else:
        for v in vehicles:
            vin = v["vin"]
            thumb_path = STORAGE_DIR / vin / "thumbnail.jpg"
            thumb_src = f"/storage/{vin}/thumbnail.jpg" if thumb_path.exists() else ""
            thumb_html = f'<img src="{thumb_src}" style="width:100%;height:180px;object-fit:cover;border-radius:6px 6px 0 0;">' if thumb_src else '<div style="width:100%;height:180px;background:#21262d;border-radius:6px 6px 0 0;display:flex;align-items:center;justify-content:center;color:#484f58;font-size:0.9rem;">No preview</div>'
            price = v.get('price', '')
            price_display = f'<div style="color:#3fb950;font-size:1.1rem;font-weight:700;margin-bottom:0.3rem;">{price}</div>' if price else ''
            cards_html += f"""
            <div style="background:#161b22;border:1px solid #30363d;border-radius:8px;overflow:hidden;">
                {thumb_html}
                <div style="padding:1rem;">
                    {price_display}
                    <div style="font-weight:600;color:#c9d1d9;font-size:1.05rem;">{v['year']} {v['make']} {v['model']}</div>
                    <div style="color:#484f58;font-size:0.75rem;margin:0.3rem 0 0.8rem;font-family:monospace;">{vin}</div>
                    <div style="color:#6e7681;font-size:0.8rem;margin-bottom:0.8rem;">
                        {v['exterior_frames']} exterior &middot; {v['interior_count']} interior
                    </div>
                    <div style="display:flex;gap:0.4rem;flex-wrap:wrap;">
                        <a href="/view/{vin}" class="btn" style="font-size:0.8rem;padding:0.4rem 0.8rem;">View 360</a>
                        <a href="/embed/{vin}" target="_blank" class="btn btn-outline" style="font-size:0.8rem;padding:0.4rem 0.8rem;">Embed</a>
                        <a href="/capture-mobile" class="btn btn-outline" style="font-size:0.75rem;padding:0.3rem 0.6rem;">Mobile Capture</a>
                        <button onclick="deleteVehicle('{vin}')" class="btn btn-danger" style="font-size:0.8rem;padding:0.4rem 0.8rem;">Delete</button>
                    </div>
                </div>
            </div>
            """
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Lotvue360 — Dashboard</title>
    <link rel="manifest" href="/static/manifest.json">
    <meta name="theme-color" content="#0d1117">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <style>
        {BASE_CSS}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 1.2rem; margin-top: 1.5rem; }}
        .stats {{ display: flex; gap: 1rem; margin: 1.2rem 0; flex-wrap: wrap; }}
        .stat {{ background: #161b22; border: 1px solid #30363d; padding: 0.8rem 1.5rem; border-radius: 8px; min-width: 140px; }}
        .stat-val {{ font-size: 1.6rem; font-weight: 700; color: #58a6ff; }}
        .stat-lbl {{ font-size: 0.75rem; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; margin-top: 2px; }}
        .credits-display {{ background: #161b22; border: 1px solid #30363d; padding: 0.8rem 1.5rem; border-radius: 8px; margin-left: auto; }}
        .credits-display .credits-val {{ font-size: 1.3rem; font-weight: 700; color: #3fb950; }}
        .credits-display .credits-lbl {{ font-size: 0.7rem; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }}
    </style>
</head>
<body>
    <div class="header">
        <div class="logo">Lotvue360</div>
        <div style="display:flex;gap:0.8rem;">
            <a href="/health" class="btn btn-outline" target="_blank">System</a>
            <a href="/settings" class="btn btn-outline">Settings</a>
            <a href="/capture" class="btn">+ Add Vehicle</a>
        </div>
    </div>
    <div class="container">
        <div class="stats">
            <div class="stat">
                <div class="stat-val">{len(vehicles)}</div>
                <div class="stat-lbl">Vehicles</div>
            </div>
            <div class="stat">
                <div class="stat-val">{sum(v.get('exterior_frames',0) for v in vehicles)}</div>
                <div class="stat-lbl">360 Views</div>
            </div>
        </div>
        <div class="grid">{cards_html}</div>
    </div>
    <script>
        async function deleteVehicle(vin) {{
            if (!confirm('Delete ' + vin + '?')) return;
            const r = await fetch('/api/vehicle/' + vin, {{ method: 'DELETE' }});
            if (r.ok) location.reload();
        }}
        if ('serviceWorker' in navigator) {{
            navigator.serviceWorker.register('/static/service-worker.js');
        }}
    </script>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/dashboard")
async def dashboard_alias():
    return await dashboard()


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
# ROUTES — CAPTURE
# =============================================================================
@app.get("/capture")
async def capture_page():
    heic_note = "" if HEIC_SUPPORTED else '<div style="background:#1c1209;border:1px solid #3d2e00;border-radius:6px;padding:0.8rem;margin-bottom:1rem;font-size:0.85rem;color:#d29922;">iPhone users: Go to Settings &rarr; Camera &rarr; Formats &rarr; Most Compatible. This ensures photos are JPG, not HEIC.</div>'
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LOTVUE — Add Vehicle</title>
    <style>
        {BASE_CSS}
        .card {{ background: #e8edf2; border: 1px solid #c5cdd6; border-radius: 6px; padding: 1.5rem; margin-bottom: 1.2rem; }}
        .card h2 {{ color: #1a2332; font-size: 1.1rem; margin-bottom: 1rem; font-weight: 600; }}
        .fg {{ margin-bottom: 1rem; }}
        .fg label {{ display: block; margin-bottom: 0.3rem; color: #8b949e; font-size: 0.85rem; }}
        .fg input {{ 
            width: 100%; padding: 0.6rem 0.8rem; background: #010409; 
            border: 1px solid #c5cdd6; border-radius: 6px; color: #1a2332; font-size: 0.95rem; 
        }}
        .fg input:focus {{ outline: none; border-color: #58a6ff; box-shadow: 0 0 0 3px rgba(88,166,255,0.15); }}
        .dropzone {{ 
            border: 2px dashed #c5cdd6; border-radius: 8px; padding: 2.5rem; 
            text-align: center; cursor: pointer; transition: all 0.2s; 
        }}
        .dropzone:hover {{ border-color: #58a6ff; background: rgba(88,166,255,0.04); }}
        .dropzone.dragover {{ border-color: #58a6ff; background: rgba(88,166,255,0.08); }}
        .dropzone p {{ color: #484f58; font-size: 0.9rem; }}
        .dropzone .icon {{ font-size: 2rem; margin-bottom: 0.5rem; }}
        .file-item {{ 
            background: #dde3ea; padding: 0.4rem 0.8rem; border-radius: 4px; 
            margin-top: 0.4rem; display: flex; justify-content: space-between; 
            font-size: 0.85rem; color: #8b949e; 
        }}
        .progress {{ margin-top: 1rem; display: none; }}
        .progress-bar {{ height: 6px; background: #c5cdd6; border-radius: 3px; overflow: hidden; }}
        .progress-fill {{ height: 100%; background: #238636; width: 0%; transition: width 0.3s; }}
        .progress-text {{ margin-top: 0.4rem; color: #484f58; font-size: 0.85rem; }}
        .submit-btn {{ 
            width: 100%; padding: 0.8rem; font-size: 1rem; margin-top: 0.5rem;
            background: #238636; border: 1px solid #2ea043; color: white;
            border-radius: 6px; cursor: pointer; font-weight: 600; transition: all 0.15s;
        }}
        .submit-btn:hover {{ background: #2ea043; }}
        .submit-btn:disabled {{ opacity: 0.4; cursor: not-allowed; }}
    </style>
</head>
    <body>
    <div class="header">
        <div class="logo">LOTVUE</div>
        <a href="/" class="back-link">&larr; Dashboard</a>
    </div>
    <div class="container" style="max-width:700px;">
        {heic_note}
        <div class="card" id="showroomSection">
            <h2>🏠 Dealer Showroom Background (Optional)</h2>
            <p style="color:#8b949e;font-size:0.85rem;margin-bottom:1rem;">Upload your showroom/garage interior ONCE. Every car will appear on this background.</p>
            <div class="dropzone" id="showroomDropzone">
                <div class="icon">🏠</div>
                <p>Click to upload custom background<br><small>or choose from dropdown below</small></p>
                <input type="file" id="showroomInput" accept="image/*" style="display:none">
            </div>
            <div class="file-list" id="showroomList"></div>
            <div style="margin-top:0.8rem;">
                <select id="bgPreset" onchange="document.getElementById('showroomInput').click()" style="width:100%;background:#dde3ea;color:#1a2332;padding:0.5rem;border:1px solid #30363d;border-radius:4px;">
                    <option value="">— Or choose preset —</option>
                    <option value="showroom">Showroom (dark)</option>
                    <option value="garage">Garage</option>
                    <option value="studio">Photo Studio</option>
                    <option value="outdoor">Outdoor Lot</option>
                    <option value="neutral">Neutral Gray</option>
                </select>
            </div>
        </div>

        <div class="card">
            <h2>Exterior 360&deg;</h2>
            <p style="color:#8b949e;font-size:0.85rem;margin-bottom:1rem;">Choose ONE: Upload a walkaround video OR select photos in order.</p>
            
            <div style="margin-bottom:1rem;">
                <label style="color:#8b949e;font-size:0.85rem;">Option 1: Walkaround Video</label>
                <input type="file" id="videoInput" accept="video/*" style="color:#8b949e;margin-top:0.3rem;">
                <div id="videoList" style="color:#484f58;font-size:0.8rem;margin-top:0.3rem;"></div>
            </div>
            
            <div style="margin-bottom:1rem;text-align:center;color:#484f58;">— OR —</div>
            
            <div>
                <label style="color:#8b949e;font-size:0.85rem;">Option 2: Photos (8-36)</label>
                <input type="file" id="extPhotoInput" accept="image/*" multiple style="color:#8b949e;margin-top:0.3rem;">
                <div id="extPhotoList" style="color:#484f58;font-size:0.8rem;margin-top:0.3rem;"></div>
            </div>
        </div>

        <div class="card">
            <h2>Interior Photos (optional)</h2>
            <p style="color:#8b949e;font-size:0.85rem;margin-bottom:1rem;">Dashboard, seats, steering wheel, etc.</p>
            <input type="file" id="photoInput" accept="image/*" multiple style="color:#8b949e;margin-bottom:1rem;">
            <div id="photoList" style="color:#484f58;font-size:0.85rem;"></div>
        </div>

        <button class="submit-btn" id="submitBtn" onclick="uploadAll()">Create 360 Viewer</button>
        <div class="progress" id="progress">
            <div class="progress-bar"><div class="progress-fill" id="progressBar"></div></div>
            <div class="progress-text" id="progressText">Preparing...</div>
        </div>
    </div>

    <script>
        let photoFiles = [];
        let extPhotoFiles = [];
        let videoFile = null;
        
        // --- Showroom upload ---
        document.getElementById('showroomInput').onchange = function(e) {{
            if (e.target.files[0]) {{
                const fd = new FormData();
                fd.append('photo', e.target.files[0]);
                fetch('/api/showroom', {{ method: 'POST', body: fd }})
                    .then(r => r.json())
                    .then(data => {{
                        document.getElementById('showroomList').innerHTML = '<span style="color:#238636">Background uploaded!</span>';
                    }})
                    .catch(err => {{
                        document.getElementById('showroomList').innerHTML = '<span style="color:#f85149">Upload failed</span>';
                    }});
            }}
        }};

        // --- Video upload ---
        document.getElementById('videoInput').onchange = function(e) {{
            if (e.target.files[0]) {{
                videoFile = e.target.files[0];
                document.getElementById('videoList').textContent = videoFile.name + ' (' + (videoFile.size/1024/1024).toFixed(1) + ' MB)';
                // Clear photo files if video selected
                extPhotoFiles = [];
                document.getElementById('extPhotoList').textContent = '';
            }}
        }};

        // --- Exterior photos ---
        document.getElementById('extPhotoInput').onchange = function(e) {{
            extPhotoFiles = Array.from(e.target.files);
            document.getElementById('extPhotoList').textContent = extPhotoFiles.length + ' exterior photo(s) selected';
            // Clear video if photos selected
            videoFile = null;
            document.getElementById('videoList').textContent = '';
        }};

        // --- Interior photos ---
        document.getElementById('photoInput').onchange = function(e) {{
            photoFiles = Array.from(e.target.files);
            document.getElementById('photoList').textContent = photoFiles.length + ' interior photo(s) selected';
        }};

        // --- Upload ---
        async function uploadAll() {{
            // Check video OR photos
            const hasVideo = videoFile && videoFile.size > 0;
            const hasPhotos = extPhotoFiles.length > 0;
            
            if (!hasVideo && !hasPhotos) {{ alert('Upload a video OR at least 4 exterior photos'); return; }}
            if (hasPhotos && extPhotoFiles.length < 4) {{ alert('Upload at least 4 exterior photos'); return; }}

            const btn = document.getElementById('submitBtn');
            const bar = document.getElementById('progressBar');
            const txt = document.getElementById('progressText');
            btn.disabled = true;
            document.getElementById('progress').style.display = 'block';

            try {{
                let usedVin = '';
                
                if (hasVideo) {{
                    // --- VIDEO PATH ---
                    txt.textContent = 'Uploading video...';
                    bar.style.width = '10%';
                    const fd = new FormData();
                    fd.append('vin', '');
                    fd.append('video', videoFile);
                    
                    const r1 = await fetch('/api/upload', {{ method: 'POST', body: fd }});
                    if (!r1.ok) {{
                        const err = await r1.json();
                        throw new Error(err.detail || 'Video upload failed');
                    }}
                    
                    const data1 = await r1.json();
                    if (data1.vin) usedVin = data1.vin;
                    if (usedVin) {{ txt.textContent = 'Vehicle ID: ' + usedVin; }}
                    
                    bar.style.width = '50%';
                    
                    // Run frame interpolation
                    txt.textContent = 'Generating smooth 360 spin...';
                    const fin = await fetch('/api/finalize/' + usedVin, {{ method: 'POST' }});
                }} else {{
                    // --- PHOTOS PATH ---
                    for (let i = 0; i < extPhotoFiles.length; i++) {{
                        txt.textContent = 'Uploading exterior photo ' + (i+1) + '/' + extPhotoFiles.length;
                        bar.style.width = (5 + (i / extPhotoFiles.length) * 45) + '%';
                        const pf = new FormData();
                        pf.append('vin', usedVin);
                        pf.append('photo', extPhotoFiles[i]);
                        pf.append('index', i.toString());
                        const r = await fetch('/api/upload/exterior', {{ method: 'POST', body: pf }});
                        if (r.ok) {{
                            const data = await r.json();
                            if (data.vin) usedVin = data.vin;
                        }}
                        if (usedVin && i === 0) {{ txt.textContent = 'Vehicle ID: ' + usedVin; }}
                    }}
                    bar.style.width = '50%';
                    
                    // Run frame interpolation
                    txt.textContent = 'Generating smooth 360 spin...';
                    try {{
                        const fin = await fetch('/api/finalize/' + usedVin, {{ method: 'POST' }});
                    }} catch(e) {{ console.warn('Finalize skipped:', e); }}
                }}
                
                // Upload interior photos
                for (let i = 0; i < photoFiles.length; i++) {{
                    txt.textContent = 'Uploading interior photo ' + (i+1) + '/' + photoFiles.length;
                    bar.style.width = (50 + (i / photoFiles.length) * 45) + '%';
                    const pf = new FormData();
                    pf.append('vin', usedVin);
                    pf.append('photo', photoFiles[i]);
                    const r2 = await fetch('/api/upload/interior', {{ method: 'POST', body: pf }});
                    if (!r2.ok) {{
                        const err = await r2.json();
                        console.warn('Photo upload warning:', err.detail);
                    }}
                }}

                bar.style.width = '100%';
                txt.textContent = 'Done! Redirecting to viewer...';
                setTimeout(() => window.location.href = '/view/' + usedVin, 800);
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
    t = threading.Thread(target=process_all_frames_background, args=(spin_dir, wm_mode), daemon=True)
    t.start()
    
    return {"status": "ok", "spin_frames": actual, "original_frames": frame_count, "processing_bg": True}


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
            if (!tag) {{ alert('Please select a feature'); return; }}
            
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
    
    # Check if interior has enough for 360 (4+ photos)
    has_int_360 = len(int_photos) >= 4
    
    info = db_get_vehicle(vin) or {"vin": vin, "year": "", "make": "", "model": ""}
    title = f"{info.get('year','')} {info.get('make','')} {info.get('model','')}".strip() or vin
    
    # Build frame img tags for exterior
    frame_tags = "\n".join(
        f'<img src="{frame_path_prefix}/{f}" style="display:none" data-idx="{i}" alt="frame {i}">'
        for i, f in enumerate(ext_frames)
    )
    
    # Build frame img tags for interior 360
    int_frame_tags = "\n".join(
        f'<img src="/storage/{vin}/interior/{f}" style="display:none" data-idx="{i}" alt="int {i}">'
        for i, f in enumerate(int_photos)
    )
    
    # Build interior gallery
    interior_tags = "\n".join(
        f'<div class="int-thumb" onclick="openLB(\'/storage/{vin}/interior/{p}\')"><img src="/storage/{vin}/interior/{p}" loading="lazy" alt="interior"></div>'
        for p in int_photos
    )
    
    base_url = str(request.base_url).rstrip("/")
    share_url = f"{base_url}/view/{vin}"
    embed_code = f'<iframe src="{base_url}/embed/{vin}" width="100%" height="600" frameborder="0" allowfullscreen></iframe>'
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} — 360 View</title>
    <style>
        {BASE_CSS}
        .tabs {{ display: flex; gap: 2px; margin-bottom: 1rem; }}
        .tab {{ 
            padding: 0.6rem 1.2rem; background: #e8edf2; border: 1px solid #c5cdd6; 
            color: #484f58; cursor: pointer; font-size: 0.9rem; border-radius: 6px 6px 0 0;
            transition: all 0.15s;
        }}
        .tab.active {{ background: #dde3ea; color: #58a6ff; border-bottom-color: #dde3ea; }}
        .tab-content {{ display: none; }}
        .tab-content.active {{ display: block; }}
        
        .viewer-wrap {{ background: #e8edf2; border: 1px solid #c5cdd6; border-radius: 6px; overflow: hidden; position: relative; }}
        #spin {{
            width: 100%; height: 500px; cursor: grab; position: relative; 
            overflow: hidden; background: #1a1a1a;
            touch-action: none; -webkit-user-select: none; user-select: none;
        }}
        #spin:active {{ cursor: grabbing; }}
        #spin .spin-inner {{
            width: 100%; height: 100%; position: absolute; left: 0; top: 0;
            display: flex; align-items: center; justify-content: center;
            transition: transform 0.05s linear;
        }}
        #spin .spin-inner img {{ 
            width: 100%; height: 100%; object-fit: cover; 
            pointer-events: none; 
        }}
        .spin-hint {{ 
            position: absolute; bottom: 0.8rem; left: 50%; transform: translateX(-50%);
            background: rgba(0,0,0,0.7); padding: 0.4rem 1rem; border-radius: 20px; 
            font-size: 0.8rem; color: #8b949e; pointer-events: none; transition: opacity 0.5s; z-index: 10;
        }}
        .zoom-hint {{
            position: absolute; top: 0.8rem; left: 50%; transform: translateX(-50%);
            background: rgba(0,0,0,0.7); padding: 0.3rem 0.8rem; border-radius: 20px; 
            font-size: 0.75rem; color: #58a6ff; pointer-events: none; opacity: 0; transition: opacity 0.3s; z-index: 10;
        }}
        .zoom-hint.visible {{ opacity: 1; }}
        .watermark-badge {{
            position: absolute; bottom: 2.5rem; right: 1rem;
            background: rgba(88,166,255,0.15); border: 1px solid rgba(88,166,255,0.3);
            padding: 0.3rem 0.8rem; border-radius: 20px;
            font-size: 0.7rem; color: #58a6ff; z-index: 10; pointer-events: none;
        }}
        .watermark-badge.clean {{ color: #3fb950; background: rgba(63,185,80,0.15); border-color: rgba(63,185,80,0.3); }}
        .spin-loader {{
            position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
            color: #484f58; font-size: 0.9rem;
        }}
        
        .int-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 0.8rem; }}
        .int-thumb {{ 
            aspect-ratio: 16/10; border-radius: 6px; overflow: hidden; 
            cursor: pointer; transition: transform 0.15s; border: 1px solid #c5cdd6;
        }}
        .int-thumb:hover {{ transform: scale(1.02); border-color: #58a6ff; }}
        .int-thumb img {{ width: 100%; height: 100%; object-fit: cover; }}
        
        .lb {{ 
            display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.95); 
            z-index: 1000; justify-content: center; align-items: center; 
        }}
        .lb.open {{ display: flex; }}
        .lb img {{ max-width: 92%; max-height: 92%; object-fit: contain; border-radius: 4px; }}
        .lb-close {{ position: absolute; top: 1rem; right: 1.5rem; font-size: 2rem; cursor: pointer; color: #8b949e; }}
        
        .share-box {{ 
            background: #e8edf2; border: 1px solid #c5cdd6; border-radius: 6px; 
            padding: 1.2rem; margin-top: 1rem; 
        }}
        .share-box h3 {{ color: #8b949e; font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 0.5rem; }}
        .share-field {{ 
            background: #010409; border: 1px solid #c5cdd6; padding: 0.5rem 0.8rem; 
            border-radius: 4px; font-family: monospace; font-size: 0.8rem; color: #8b949e;
            word-break: break-all; margin-bottom: 0.8rem;
        }}
        .copy-btn {{ font-size: 0.8rem; padding: 0.3rem 0.8rem; }}
        
        @media (max-width: 768px) {{
            #spin {{ height: 300px; }}
            #spin-int {{ height: 300px; }}
            .int-grid {{ grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); }}
        }}
        #spin-int {{
            width: 100%; height: 500px; cursor: grab; position: relative;
            overflow: hidden; background: #010409;
            touch-action: none; -webkit-user-select: none; user-select: none;
        }}
        #spin-int:active {{ cursor: grabbing; }}
        #spin-int .spin-inner {{
            width: 100%; height: 100%; position: absolute; left: 0; top: 0;
            display: flex; align-items: center; justify-content: center;
            transition: transform 0.1s ease-out;
        }}
        #spin-int .spin-inner img {{
            max-width: 100%; max-height: 100%; object-fit: contain; pointer-events: none;
        }}
    </style>
</head>
<body>
    <div class="header">
        <div class="logo">{title}</div>
        <div style="display:flex;align-items:center;gap:1rem;">
            <select id="bgSelect" onchange="changeBackground(this.value)" style="background:#dde3ea;color:#1a2332;padding:0.4rem 0.8rem;border:1px solid #30363d;border-radius:4px;font-size:0.85rem;">
                <option value="">No Background</option>
                <option value="#1a1a1a">Dark Gray</option>
                <option value="#e5e5e5">Light Gray</option>
                <option value="#f5f5dc">Light Cream</option>
                <option value="#2d2d2d">Charcoal</option>
                <option value="#ffffff">White</option>
                <option value="#0a0a0a">Black</option>
            </select>
            <a href="/" class="back-link">&larr; Dashboard</a>
        </div>
    </div>
    <div class="container">
        <div class="tabs">
            <button class="tab active" onclick="showTab('ext')">Exterior 360&deg;</button>
            <button class="tab" onclick="showTab('int360')">Interior 360&deg;</button>
            <button class="tab" onclick="showTab('int')">Interior Gallery</button>
        </div>
        
        <div id="ext" class="tab-content active">
            <div class="viewer-wrap">
                <div id="spin">
                    <div class="zoom-hint" id="zoomHint">Scroll to zoom • Double-tap to reset</div>
                    <div class="spin-loader" id="loader">Loading 0/{len(ext_frames)}...</div>
                    <div class="spin-inner" id="spinInner">
                        {frame_tags}
                    </div>
                    <div class="spin-hint" id="hint" style="opacity:0">&larr; Drag to spin &rarr;</div>
                    <div class="watermark-badge" id="wmBadge">PREVIEW</div>
                </div>
            </div>
        </div>
        
        <div id="int360" class="tab-content">
            <div class="viewer-wrap">
                <div id="spin-int">
                    <div class="zoom-hint" id="zoomHintInt">Scroll to zoom • Double-tap to reset</div>
                    <div class="spin-loader" id="loader-int">Loading 0/{len(int_photos)}...</div>
                    <div class="spin-inner" id="spinInnerInt">
                        {int_frame_tags}
                    </div>
                    <div class="spin-hint" id="hint-int" style="opacity:0">&larr; Drag to spin inside &rarr;</div>
                </div>
            </div>
        </div>
        
        <div id="int" class="tab-content">
            <div class="int-grid">{interior_tags}</div>
        </div>
        
        <div class="share-box">
            <h3>Share Link</h3>
            <div class="share-field" id="shareLink">{share_url}</div>
            <button class="btn btn-outline copy-btn" onclick="copyText('shareLink')">Copy Link</button>
            
            <h3 style="margin-top:1rem;">Embed Code</h3>
            <div class="share-field" id="embedCode">{embed_code}</div>
            <button class="btn btn-outline copy-btn" onclick="copyText('embedCode')">Copy Embed</button>
        </div>
    </div>
    
    <div class="lb" id="lb" onclick="closeLB()">
        <span class="lb-close">&times;</span>
        <img id="lbImg" src="">
    </div>

    <script src="/static/viewer360.js"></script>
    <script>
        // Initialize exterior 360 viewer
        const extViewer = init360Viewer('spin', 'spinInner', '#spin .spin-inner img', 'loader', 'hint', 'zoomHint');
        
        // --- Watermark badge ---
        fetch('/api/watermark/{vin}')
            .then(r => r.json())
            .then(data => {{
                const badge = document.getElementById('wmBadge');
                if (badge) {{
                    if (data.watermark_mode === 'clean') {{
                        badge.textContent = 'CLEAN';
                        badge.className = 'watermark-badge clean';
                    }} else {{
                        badge.textContent = 'PREVIEW';
                        badge.className = 'watermark-badge';
                    }}
                }}
            }});
        
        // --- Background selector ---
        function changeBackground(bg) {{
            const spin = document.getElementById('spin');
            if (!bg) {{
                spin.style.background = '';
            }} else if (bg.startsWith('#')) {{
                // Solid color
                spin.style.background = bg;
                spin.style.backgroundImage = '';
            }} else {{
                // Image
                spin.style.backgroundImage = 'url(' + bg + ')';
                spin.style.backgroundSize = 'cover';
                spin.style.backgroundPosition = 'center';
            }}
        }}
        
        // --- Tabs ---
        function showTab(id) {{
            const tabMap = {{'ext': 0, 'int360': 1, 'int': 2}};
            document.querySelectorAll('.tab').forEach((t, i) => {{
                t.classList.toggle('active', i === tabMap[id]);
            }});
            document.querySelectorAll('.tab-content').forEach(tc => tc.classList.remove('active'));
            document.getElementById(id).classList.add('active');
        }}
        
        // Initialize interior 360 viewer when tab is shown
        let intViewerInited = false;
        const intTabBtn = document.querySelectorAll('.tab')[1];
        intTabBtn.addEventListener('click', () => {{
            if (!intViewerInited) {{
                intViewerInited = true;
                setTimeout(() => init360Viewer('spin-int', 'spinInnerInt', '#spin-int .spin-inner img', 'loader-int', 'hint-int', 'zoomHintInt'), 100);
            }}
        }});
        
        // --- Lightbox ---
        function openLB(src) {{ document.getElementById('lbImg').src = src; document.getElementById('lb').classList.add('open'); }}
        function closeLB() {{ document.getElementById('lb').classList.remove('open'); }}
        
        // --- Copy ---
        function copyText(id) {{
            navigator.clipboard.writeText(document.getElementById(id).textContent);
            event.target.textContent = 'Copied!';
            setTimeout(() => event.target.textContent = id === 'shareLink' ? 'Copy Link' : 'Copy Embed', 1500);
        }}
        
        // --- Hotspots ---
        (async () => {{
            const hotspotsData = await fetch('/api/hotspots/{vin}').then(r => r.json());
            const hotspots = {{}};
            hotspotsData.forEach(h => {{
                if (!hotspots[h.photo]) hotspots[h.photo] = [];
                hotspots[h.photo].push(h);
            }});
            
            function renderHotspots() {{
                const currentFrame = extViewer && extViewer.cur ? extViewer.cur() : 0;
                const spin = document.getElementById('spin');
                if (!spin) return;
                spin.querySelectorAll('.hotspot-dot').forEach(d => d.remove());
                const existingPopup = document.getElementById('hotspot-popup');
                if (existingPopup) existingPopup.remove();
                
                const frameName = 'spin_' + String(currentFrame + 1).padStart(4, '0') + '.jpg';
                if (!hotspots[frameName]) return;
                
                hotspots[frameName].forEach(h => {{
                    const dot = document.createElement('div');
                    dot.className = 'hotspot-dot';
                    dot.style.left = (h.x * 100) + '%';
                    dot.style.top = (h.y * 100) + '%';
                    dot.title = h.tag.replace('_', ' ');
                    dot.onclick = (e) => {{
                        e.stopPropagation();
                        const popup = document.getElementById('hotspot-popup');
                        if (popup) popup.remove();
                        const newPopup = document.createElement('div');
                        newPopup.id = 'hotspot-popup';
                        newPopup.style.cssText = 'position:absolute;background:#1e3a5f;color:#fff;padding:0.6rem 1rem;border-radius:8px;font-size:0.85rem;font-weight:600;z-index:100;pointer-events:none;box-shadow:0 4px 12px rgba(0,0,0,0.5);transform:translate(-50%,-100%);';
                        newPopup.style.left = (h.x * 100) + '%';
                        newPopup.style.top = (h.y * 100) + '%';
                        newPopup.textContent = h.tag.replace(/_/g, ' ').toUpperCase();
                        spin.appendChild(newPopup);
                        setTimeout(() => newPopup.remove(), 3000);
                    }};
                    spin.appendChild(dot);
                }});
            }}
            window.hotspotManager = {{ update: renderHotspots }};
            
            // Feature buttons
            const uniqueTags = [...new Set(hotspotsData.map(h => h.tag))];
            if (uniqueTags.length > 0) {{
                const btnContainer = document.createElement('div');
                btnContainer.className = 'feature-btns';
                uniqueTags.forEach(tag => {{
                    const btn = document.createElement('button');
                    btn.className = 'feature-btn';
                    btn.textContent = tag.replace('_', ' ').toUpperCase();
                    btn.onclick = () => alert('Feature: ' + tag.replace('_', ' ').toUpperCase());
                    btnContainer.appendChild(btn);
                }});
                const wrap = document.querySelector('.viewer-wrap');
                if (wrap) wrap.appendChild(btnContainer);
            }}
        }})();
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
2. Run: ngrok http 8000
3. Copy the URL shown (e.g., https://abc123.ngrok.io)</pre>
        </div>
        
        <div class="card">
            <h2>Current Local URL</h2>
            <code>http://localhost:8000</code>
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
    uvicorn.run(app, host="0.0.0.0", port=8001)
