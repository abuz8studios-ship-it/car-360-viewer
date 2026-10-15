r"""
LotVue Pro — AI-Powered Dealer Platform
Cinematic AI photo studio + 360 viewer + video walkaround + market pricing
Replaces: DealerCenter + Spyne + vAuto + video service + manual posting
Features: Multi-tenant, auth, credits, Stripe, AI photo studio, 360 viewer

Runtime dependencies: install the repository-root requirements.txt with Python 3.11+.
Optional background-removal/AI dependencies are documented in requirements-ai.txt.
"""

import os
import sys
import re
import io
import json
import heapq
import html as html_lib
import shutil
import sqlite3
import hashlib
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager, closing
from concurrent.futures import ThreadPoolExecutor
import numpy as np
from pathlib import Path
from datetime import datetime
from typing import List, Optional
from urllib.parse import quote, unquote, urlsplit

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool
from PIL import Image

from security import hash_password, is_password_hash, needs_rehash, verify_password
from auth_rate import check_auth_rate
from video_urls import normalize_video_source

# ── Load app/.env into process env (no python-dotenv dependency) ─────────────
# Must run before FEATURES_ENABLED / STRIPE_* are read below.
_env_data_root = os.getenv("LOTVUE_DATA_DIR", "").strip()
_env_root_path = Path(_env_data_root).expanduser() if _env_data_root else None
if _env_root_path is not None and not _env_root_path.is_absolute():
    _env_root_path = Path(__file__).resolve().parent / _env_root_path
_ENV_FILE = (
    _env_root_path / ".env"
    if _env_root_path is not None else Path(__file__).resolve().parent / ".env"
)
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from app_paths import BASE_DIR, DB_PATH, STORAGE_DIR
from background_catalog import (
    STATIC_BACKGROUND_DIR,
    background_asset_path,
    list_background_assets,
    scene_asset_slug,
)

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
    # The auth gate is enabled by default, so keep registration/login enabled
    # unless an operator explicitly disables new-account features.
    "auth": os.environ.get("LOTVUE_FEATURE_AUTH", "1") == "1",
    "payments": os.environ.get("LOTVUE_FEATURE_PAYMENTS", "0") == "1",
    "watermark": os.environ.get("LOTVUE_FEATURE_WATERMARK", "0") == "1",
}
# PRD pricing is still owner-unapproved. Keep both public plan disclosure and
# all charge paths disabled until a single canonical offer is approved.
PRICING_APPROVED = os.environ.get("LOTVUE_PRICING_APPROVED", "0") == "1"


def _pricing_catalog_consistent() -> bool:
    """Require one matching amount for checkout and subscription plan tiers."""
    checkout_plans = globals().get("PLANS")
    subscription_plans = globals().get("PRICING_PLANS")
    if not isinstance(checkout_plans, dict) or not isinstance(subscription_plans, dict):
        return False
    pairs = (("single", "pay_per_use"), ("starter", "pro_bundle"), ("dealership", "dealership"))
    try:
        return all(
            key in checkout_plans and subscription_key in subscription_plans
            and round(float(checkout_plans[key]["price"]), 2)
                == round(float(subscription_plans[subscription_key]["price"]), 2)
            for key, subscription_key in pairs
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _pricing_is_approved() -> bool:
    return bool(PRICING_APPROVED and _pricing_catalog_consistent())


def _payments_available() -> bool:
    return bool(FEATURES_ENABLED.get("payments", False) and _pricing_is_approved())

# --- Background Removal (rembg) ---
# The runtime package is not sufficient: a model must be explicitly selected and
# its license reviewed by the operator before any weights are downloaded or used.
from rembg_runtime import is_enabled as _rembg_is_enabled
from rembg_runtime import remove as remove_bg
from rembg_runtime import require_session as _require_rembg_session

REMBG_AVAILABLE = _rembg_is_enabled()
_REMBG_SESSION = None


def get_rembg_session():
    """Return the explicitly selected model session, or None while disabled."""
    global _REMBG_SESSION
    if not REMBG_AVAILABLE:
        return None
    if _REMBG_SESSION is None:
        _REMBG_SESSION = _require_rembg_session()
    return _REMBG_SESSION

# Showroom storage
SHOWROOM_DIR = STORAGE_DIR / "_showrooms"

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

_MEDIA_BIN_SUFFIX = ".exe" if os.name == "nt" else ""
FFMPEG = _resolve_tool("FFMPEG_PATH", f"ffmpeg{_MEDIA_BIN_SUFFIX}")
FFPROBE = _resolve_tool("FFPROBE_PATH", f"ffprobe{_MEDIA_BIN_SUFFIX}")
RAW_DIR = STORAGE_DIR / "raw"
FRAMES_DIR = STORAGE_DIR / "frames"
PREVIEW_DIR = STORAGE_DIR / "preview"
CLEAN_DIR = STORAGE_DIR / "clean"
THUMBNAILS_DIR = STORAGE_DIR / "thumbnails"
NUM_EXTERIOR_FRAMES = 36  # 10° per frame = smooth spin
def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


MAX_VIDEO_MB = _bounded_env_int("LOTVUE_MAX_VIDEO_MB", 500, 1, 2048)
MAX_PHOTO_MB = _bounded_env_int("LOTVUE_MAX_PHOTO_MB", 30, 1, 256)
MAX_PHOTO_BATCH_MB = _bounded_env_int("LOTVUE_MAX_PHOTO_BATCH_MB", 400, 1, 4096)
MAX_UPLOAD_IMAGE_PIXELS = _bounded_env_int("LOTVUE_MAX_IMAGE_PIXELS", 40_000_000, 1_000_000, 100_000_000)
UPLOAD_CHUNK_BYTES = 1024 * 1024
_LEAD_RATE_BUCKETS: dict[str, tuple[float, int]] = {}
_LEAD_RATE_LOCK = threading.Lock()
_DASHBOARD_STORAGE_CACHE: dict[str, tuple[float, int]] = {}
_DASHBOARD_STORAGE_LOCK = threading.Lock()
_ANALYTICS_RATE_BUCKETS: dict[str, tuple[float, int]] = {}
_ANALYTICS_RATE_LOCK = threading.Lock()
_BACKGROUND_WORKERS = _bounded_env_int("LOTVUE_BACKGROUND_WORKERS", 2, 1, 4)
_BACKGROUND_MAX_JOBS = _bounded_env_int("LOTVUE_BACKGROUND_MAX_JOBS", 4, _BACKGROUND_WORKERS, 16)
_MAX_CONCURRENT_UPLOADS = _bounded_env_int("LOTVUE_MAX_CONCURRENT_UPLOADS", 2, 1, 8)
_BACKGROUND_EXECUTOR = ThreadPoolExecutor(
    max_workers=_BACKGROUND_WORKERS, thread_name_prefix="lotvue-job"
)
_BACKGROUND_JOB_SLOTS = threading.BoundedSemaphore(_BACKGROUND_MAX_JOBS)
_BACKGROUND_JOBS_LOCK = threading.Lock()
_BACKGROUND_JOB_KEYS: set[str] = set()
_COMFYUI_SCENE_MAX_JOBS = 1
_COMFYUI_SCENE_JOB_SLOTS = threading.BoundedSemaphore(_COMFYUI_SCENE_MAX_JOBS)


def _write_gaussian_status(vin: str, payload: dict) -> None:
    status_file = STORAGE_DIR / vin / "splat" / "status.json"
    temp_file = status_file.with_name(f".status-{uuid.uuid4().hex}.tmp")
    try:
        temp_file.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temp_file, status_file)
    finally:
        temp_file.unlink(missing_ok=True)


def _run_gaussian_3d_job(vin: str, mode_args: list[str], steps: int,
                         style: str, backdrop: str) -> None:
    """Run one bounded Gaussian reconstruction as managed background work."""
    command = [
        sys.executable, str(BASE_DIR / "gaussian_3d.py"), "--vin", vin,
        *mode_args, "--steps", str(steps), "--style", style, "--backdrop", backdrop,
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(BASE_DIR),
            timeout=24 * 60 * 60,
            check=False,
        )
        if result.returncode != 0:
            _write_gaussian_status(vin, {
                "status": "error", "step": "failed", "progress": 0,
                "vin": vin, "error": f"3D reconstruction exited with status {result.returncode}",
            })
    except Exception as exc:
        try:
            _write_gaussian_status(vin, {
                "status": "error", "step": "failed", "progress": 0,
                "vin": vin, "error": f"{type(exc).__name__}: {str(exc)[:400]}",
            })
        except OSError:
            pass


def _background_job_running(job_key: str) -> bool:
    with _BACKGROUND_JOBS_LOCK:
        return str(job_key) in _BACKGROUND_JOB_KEYS


def _submit_background_job(job_key: str, target, *args, **kwargs) -> str:
    """Submit bounded background work with process-local per-key deduplication."""
    key = str(job_key)
    with _BACKGROUND_JOBS_LOCK:
        if key in _BACKGROUND_JOB_KEYS:
            return "already_running"
        if not _BACKGROUND_JOB_SLOTS.acquire(blocking=False):
            return "busy"
        _BACKGROUND_JOB_KEYS.add(key)
    try:
        future = _BACKGROUND_EXECUTOR.submit(target, *args, **kwargs)
    except Exception:
        with _BACKGROUND_JOBS_LOCK:
            _BACKGROUND_JOB_KEYS.discard(key)
            _BACKGROUND_JOB_SLOTS.release()
        raise

    def _finished(done):
        try:
            error = done.exception()
            if error:
                print(f"[Background] job {key} failed: {type(error).__name__}: {error}")
        except Exception:
            pass
        finally:
            with _BACKGROUND_JOBS_LOCK:
                _BACKGROUND_JOB_KEYS.discard(key)
                _BACKGROUND_JOB_SLOTS.release()

    future.add_done_callback(_finished)
    return "started"


def _check_public_analytics_rate(request: Request) -> None:
    import hashlib
    import time

    host = request.client.host if request.client else "unknown"
    key = hashlib.sha256(f"analytics:{host}".encode("utf-8", errors="ignore")).hexdigest()
    now = time.monotonic()
    with _ANALYTICS_RATE_LOCK:
        start, count = _ANALYTICS_RATE_BUCKETS.get(key, (now, 0))
        if now - start >= 60:
            start, count = now, 0
        if count >= 120:
            raise HTTPException(status_code=429, detail="Too many viewer events", headers={"Retry-After": "60"})
        _ANALYTICS_RATE_BUCKETS[key] = (start, count + 1)
        size = len(_ANALYTICS_RATE_BUCKETS)
        if size > 4096 and size % 256 == 0:
            expired = [client for client, (window, _) in _ANALYTICS_RATE_BUCKETS.items()
                       if now - window >= 60]
            for client in expired:
                _ANALYTICS_RATE_BUCKETS.pop(client, None)
            excess = len(_ANALYTICS_RATE_BUCKETS) - 4096
            for client in list(_ANALYTICS_RATE_BUCKETS)[:max(0, excess)]:
                _ANALYTICS_RATE_BUCKETS.pop(client, None)


def _check_public_lead_rate(request: Request) -> None:
    """Small per-process anti-spam guard for public lead endpoints (10/min/IP)."""
    import hashlib
    import time

    host = request.client.host if request.client else "unknown"
    key = hashlib.sha256(host.encode("utf-8", errors="ignore")).hexdigest()
    now = time.monotonic()
    with _LEAD_RATE_LOCK:
        start, count = _LEAD_RATE_BUCKETS.get(key, (now, 0))
        if now - start >= 60:
            start, count = now, 0
        if count >= 10:
            raise HTTPException(
                status_code=429,
                detail="Too many inquiries from this network; try again shortly",
                headers={"Retry-After": "60"},
            )
        _LEAD_RATE_BUCKETS[key] = (start, count + 1)
        if len(_LEAD_RATE_BUCKETS) > 4096:
            expired = [client for client, (window, _) in _LEAD_RATE_BUCKETS.items()
                       if now - window >= 60]
            for client in expired:
                _LEAD_RATE_BUCKETS.pop(client, None)

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
    """Apply small photographic corrections while releasing each intermediate."""
    from PIL import ImageEnhance

    output = img.copy()
    for enhancer_type, factor in (
        (ImageEnhance.Contrast, 1.08),
        (ImageEnhance.Color, 1.08),
        (ImageEnhance.Sharpness, 1.10),
    ):
        enhanced = enhancer_type(output).enhance(factor)
        output.close()
        output = enhanced
    return output


def process_exterior_photo(input_bytes: bytes, output_path: Path, showroom_path: Path | None = None, keep_original: bool = False):
    """Save a landscape viewer frame without distorting the original photo."""
    from PIL import ImageOps

    img = None
    canvas = None
    enhanced = None
    try:
        with Image.open(io.BytesIO(input_bytes)) as source:
            width, height = source.size
            if width <= 0 or height <= 0 or width * height > MAX_UPLOAD_IMAGE_PIXELS:
                raise ValueError("Image exceeds the safe pixel limit")
            source.draft("RGB", (1920, 1080))
            source.thumbnail((1920, 1080), Image.Resampling.LANCZOS)
            oriented = ImageOps.exif_transpose(source)
            try:
                img = oriented.convert("RGB")
            finally:
                if oriented is not source:
                    oriented.close()
        # Keep a fixed 16:9 viewer frame, but contain rather than stretch/crop the car.
        canvas = Image.new("RGB", (1920, 1080), (22, 25, 29))
        canvas.paste(img, ((1920 - img.width) // 2, (1080 - img.height) // 2))
        enhanced = auto_enhance(canvas)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        enhanced.save(output_path, "JPEG", quality=90, optimize=True, progressive=True)
    finally:
        if img is not None:
            img.close()
        if canvas is not None:
            canvas.close()
        if enhanced is not None:
            enhanced.close()


def process_interior_photo(input_bytes: bytes, output_path: Path):
    """Save an orientation-corrected, bounded interior photo."""
    from PIL import ImageOps

    img = None
    enhanced = None
    try:
        with Image.open(io.BytesIO(input_bytes)) as source:
            width, height = source.size
            if width <= 0 or height <= 0 or width * height > MAX_UPLOAD_IMAGE_PIXELS:
                raise ValueError("Image exceeds the safe pixel limit")
            source.draft("RGB", (1920, 1920))
            source.thumbnail((1920, 1920), Image.Resampling.LANCZOS)
            oriented = ImageOps.exif_transpose(source)
            try:
                img = oriented.convert("RGB")
            finally:
                if oriented is not source:
                    oriented.close()
        enhanced = auto_enhance(img)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        enhanced.save(output_path, "JPEG", quality=90, optimize=True)
    finally:
        if img is not None:
            img.close()
        if enhanced is not None:
            enhanced.close()


WATERMARK_PREVIEW_TEXT = "Preview • LotVue Pro"

def apply_diagonal_watermark(img: Image.Image, text: str = "LOTVUE PRO PREVIEW", opacity: int = 77) -> Image.Image:
    """Apply diagonal repeating watermark across the entire image (Phase 1 upgrade)."""
    if not FEATURES_ENABLED.get("watermark", False):
        return img
    try:
        from PIL import ImageDraw
        overlay = img.copy().convert("RGBA")
        canvas_size = (max(1, img.width * 2), max(1, img.height * 2))
        txt_layer = Image.new("RGBA", canvas_size, (255, 255, 255, 0))
        draw = ImageDraw.Draw(txt_layer)

        font_size = max(20, img.width // 50)
        try:
            from PIL import ImageFont
            font = ImageFont.truetype("arial.ttf", font_size)
        except OSError:
            font = ImageFont.load_default()

        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = max(1, bbox[2] - bbox[0])
        text_h = max(1, bbox[3] - bbox[1])
        spacing_x = max(1, int(text_w * 2.5))
        spacing_y = max(1, int(text_h * 4))
        angle = -30

        for y in range(-text_h, canvas_size[1] + text_h, spacing_y):
            for x in range(-text_w, canvas_size[0] + text_w, spacing_x):
                draw.text((x, y), text, font=font, fill=(255, 255, 255, opacity))

        txt_layer = txt_layer.rotate(angle, resample=Image.Resampling.BICUBIC)
        left, top = img.width // 2, img.height // 2
        txt_layer = txt_layer.crop((left, top, left + img.width, top + img.height))
        draw = ImageDraw.Draw(txt_layer)

        small_text = "lotvuepro"
        small_size = max(12, img.width // 120)
        try:
            small_font = ImageFont.truetype("arial.ttf", small_size)
        except OSError:
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


def _showroom_file(user_email: str, dealer_name: str = "default") -> Path:
    """Resolve a tenant-specific showroom asset using a nonreversible directory key."""
    dealer_name = (dealer_name or "default").strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,64}", dealer_name):
        raise HTTPException(status_code=400, detail="Invalid showroom name")
    tenant_key = hashlib.sha256(user_email.strip().lower().encode("utf-8")).hexdigest()[:32]
    return SHOWROOM_DIR / tenant_key / f"{dealer_name}.jpg"


def get_dealer_showroom(user_email: str, dealer_name: str = "default") -> Path | None:
    """Get only the authenticated tenant's showroom background, if uploaded."""
    showroom_file = _showroom_file(user_email, dealer_name)
    return showroom_file if showroom_file.is_file() else None

# =============================================================================
# APP
# =============================================================================
@asynccontextmanager
async def _lifespan(_app: FastAPI):
    stop_event = _start_followup_dispatcher()
    try:
        yield
    finally:
        if stop_event is not None:
            stop_event.set()


app = FastAPI(title="LotVue Pro — AI-Powered Dealer Platform", lifespan=_lifespan)
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
    # migrations: damage layer (condition_type/severity/note) + optional close-up
    hs_cols = {r[1] for r in conn.execute("PRAGMA table_info(hotspots)")}
    for col, ddl in (("condition_type", "TEXT DEFAULT ''"), ("severity", "TEXT DEFAULT ''"),
                     ("note", "TEXT DEFAULT ''"), ("close_up", "TEXT DEFAULT ''")):
        if col not in hs_cols:
            conn.execute(f"ALTER TABLE hotspots ADD COLUMN {col} {ddl}")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dealers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            display_name TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            email TEXT DEFAULT '',
            user_email TEXT DEFAULT '',
            address TEXT DEFAULT '',
            website TEXT DEFAULT '',
            logo_url TEXT DEFAULT '',
            brand_color TEXT DEFAULT '#58a6ff',
            background_preset TEXT DEFAULT 'showroom',
            plan TEXT DEFAULT 'free',
            created_at TEXT DEFAULT ''
        )
    """)
    dealer_cols = {row[1] for row in conn.execute("PRAGMA table_info(dealers)")}
    for col_name, col_ddl in (
        ("user_email", "TEXT DEFAULT ''"),
        ("background_preset", "TEXT DEFAULT 'showroom'"),
    ):
        if col_name not in dealer_cols:
            conn.execute(f"ALTER TABLE dealers ADD COLUMN {col_name} {col_ddl}")
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
    for col_def in ("plan_type TEXT DEFAULT 'free'", "is_subscribed INTEGER DEFAULT 0"):
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stripe_webhook_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'processing',
            received_at TEXT NOT NULL DEFAULT '',
            processed_at TEXT DEFAULT '',
            error TEXT DEFAULT ''
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pano_hotspots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vin TEXT NOT NULL,
            pano_file TEXT DEFAULT '',
            yaw REAL DEFAULT 0,
            pitch REAL DEFAULT 0,
            label TEXT NOT NULL,
            description TEXT DEFAULT '',
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
        "is_paid INTEGER DEFAULT 0",
        "payment_id TEXT DEFAULT ''",
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

    # Seed/migrate every packaged studio asset, not just the stale eight-row
    # catalog. The same filesystem-backed catalog drives Photo Studio and the
    # 360 compositor; nonexistent thumbnails are never published.
    for asset in list_background_assets(include_none=True):
        values = (
            asset["name"], asset["slug"], asset["url"], asset["thumbnail_url"],
            asset["category"],
        )
        try:
            existing = conn.execute(
                "SELECT is_system FROM backgrounds WHERE slug=?", (asset["slug"],)
            ).fetchone()
            if existing and int(existing[0] or 0) == 1:
                conn.execute(
                    "UPDATE backgrounds SET name=?,url=?,thumbnail_url=?,category=?,is_active=1 "
                    "WHERE slug=? AND is_system=1",
                    (asset["name"], asset["url"], asset["thumbnail_url"],
                     asset["category"], asset["slug"]),
                )
            elif not existing:
                conn.execute(
                    "INSERT OR IGNORE INTO backgrounds "
                    "(name,slug,url,thumbnail_url,category,is_system) VALUES (?,?,?,?,?,1)",
                    values,
                )
        except sqlite3.Error as exc:
            print(f"[Database] background catalog seed skipped: {type(exc).__name__}")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS vehicle_pipeline_jobs (
            vin TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            step TEXT NOT NULL DEFAULT 'queued',
            progress INTEGER NOT NULL DEFAULT 0,
            total INTEGER NOT NULL DEFAULT 0,
            succeeded INTEGER NOT NULL DEFAULT 0,
            degraded INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (vin, kind)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_vehicle_pipeline_jobs_updated "
        "ON vehicle_pipeline_jobs(vin, updated_at DESC)"
    )

    # Common tenant/status filters use composite indexes so inventory and feed
    # queries stay bounded as dealers add vehicles and posting history grows.
    for index_sql in (
        "CREATE INDEX IF NOT EXISTS idx_vehicles_user_status ON vehicles(user_email, status)",
        "CREATE INDEX IF NOT EXISTS idx_vehicles_lower_user_status "
        "ON vehicles(LOWER(COALESCE(user_email,'')), status, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_vehicles_status_created ON vehicles(status, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_dealers_user_email ON dealers(user_email)",
        "CREATE INDEX IF NOT EXISTS idx_leads_vin_created ON leads(vin, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_hotspots_vin ON hotspots(vin)",
    ):
        try:
            conn.execute(index_sql)
        except sqlite3.Error as exc:
            print(f"[Database] index creation skipped: {exc}")

    conn.commit()
    conn.close()

init_db()

def ensure_owner_account():
    """Boot-time self-heal for the owner login, driven entirely by app/.env.

    The owner's users row reverted to a stale state across restarts twice
    (2026-07-05, 2026-07-06) even though no code path in this app ever
    UPDATEs password_hash — the DB file itself gets clobbered from outside
    the server (external tooling working on file-level copies). So instead
    of trusting the row, re-assert it from .env on every boot: set
    LOTVUE_OWNER_EMAIL + LOTVUE_OWNER_PASSWORD_HASH (sha256 hex) there.
    A clean shipped build regenerates .env without these keys
    (make_clean_build.py), so no personal account ever leaves this machine.
    """
    email = os.getenv("LOTVUE_OWNER_EMAIL", "").strip().lower()
    pw_hash = os.getenv("LOTVUE_OWNER_PASSWORD_HASH", "").strip()
    if not email:
        return
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or not is_password_hash(pw_hash):
        print("[Auth] owner bootstrap disabled: configure a valid LOTVUE_OWNER_EMAIL and password hash")
        return
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id, password_hash FROM users WHERE email=?", (email,)).fetchone()
    drift = False
    if row is None:
        conn.execute("""
            INSERT INTO users (email, name, password_hash, plan, credits, created_at)
            VALUES (?, ?, ?, 'dealership', -1, ?)
        """, (email, os.getenv("LOTVUE_OWNER_NAME", "Owner"), pw_hash, datetime.now().isoformat()))
        drift = True
        print(f"[Auth] owner account was MISSING - recreated {email} from .env")
    elif row["password_hash"] != pw_hash:
        # Do not downgrade a password upgraded during login when the deployment
        # still has its legacy SHA-256 bootstrap hash in the environment.
        if re.fullmatch(r"[0-9a-fA-F]{64}", pw_hash) and not needs_rehash(row["password_hash"]):
            print("[Auth] stored owner hash is stronger than the legacy bootstrap hash; update LOTVUE_OWNER_PASSWORD_HASH")
        else:
            conn.execute("UPDATE users SET password_hash=? WHERE email=?", (pw_hash, email))
            drift = True
            print(f"[Auth] owner credential DRIFT detected (user id {row['id']}) - re-asserted from .env")
    if drift:
        try:
            conn.execute("""
                UPDATE users SET plan='dealership', plan_type='dealership',
                                 credits=-1, is_subscribed=1 WHERE email=?
            """, (email,))
        except sqlite3.OperationalError:
            pass  # plan_type/is_subscribed columns absent on a fresh schema
    conn.commit()
    conn.close()

ensure_owner_account()

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
            watermark_mode=excluded.watermark_mode,
            user_email=CASE WHEN COALESCE(user_email,'')='' THEN excluded.user_email ELSE user_email END,
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
    tenant_email = str(dealer.get("user_email") or dealer.get("email") or "").strip().lower()
    rows = conn.execute("SELECT * FROM vehicles WHERE LOWER(COALESCE(user_email,''))=? "
                        "AND status='active' ORDER BY created_at DESC", (tenant_email,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def db_get_vehicles_by_email(user_email: str) -> list[dict]:
    """Get vehicles for a specific user email."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM vehicles WHERE LOWER(COALESCE(user_email,''))=? "
                        "AND status='active' ORDER BY created_at DESC", ((user_email or "").strip().lower(),)).fetchall()
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
    row = conn.execute("SELECT * FROM vehicles WHERE job_id=? AND status='active'", (job_id,)).fetchone()
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

def db_upsert_dealer(name: str, display_name="", phone="", email="", address="", website="", logo_url="", brand_color="#58a6ff", user_email=""):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        INSERT INTO dealers
            (name, display_name, phone, email, user_email, address, website, logo_url, brand_color, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            display_name=excluded.display_name, phone=excluded.phone, email=excluded.email,
            address=excluded.address, website=excluded.website, logo_url=excluded.logo_url,
            brand_color=excluded.brand_color,
            user_email=CASE WHEN excluded.user_email != '' THEN excluded.user_email ELSE user_email END
    """, (name, display_name, phone, email, user_email, address, website, logo_url, brand_color, datetime.now().isoformat()))
    conn.commit()
    conn.close()


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
    """Rebuild unwatermarked derivatives from the vehicle's retained source frames."""
    import tempfile

    try:
        vin = validate_vehicle_id(vin)
        storage_root = STORAGE_DIR.resolve()
        vehicle_path = STORAGE_DIR / vin
        if vehicle_path.is_symlink():
            return False
        vehicle_dir = vehicle_path.resolve(strict=True)
        if vehicle_dir.parent != storage_root or not vehicle_dir.is_dir():
            return False
    except (HTTPException, OSError, RuntimeError, ValueError):
        return False

    exterior_dir = vehicle_dir / "exterior"
    source_dirs = (
        vehicle_dir / "source",  # clean video frames retained before studio processing
        vehicle_dir / "raw",     # original, normalized dealer photo uploads
        RAW_DIR / vin,            # compatibility with older deployments
        exterior_dir,
    )
    source_dir = None
    frames = []
    for candidate in source_dirs:
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        try:
            resolved_dir = candidate.resolve(strict=True)
            if resolved_dir.parent != vehicle_dir:
                # The legacy raw directory is under STORAGE_DIR/raw/{vin}.
                if candidate != RAW_DIR / vin or resolved_dir.parent != RAW_DIR.resolve():
                    continue
            files = sorted(
                path for path in resolved_dir.glob("frame_*.jpg")
                if not path.is_symlink()
                and re.fullmatch(r"frame_[A-Za-z0-9_-]{1,80}\.jpg", path.name)
                and path.is_file()
                and path.resolve(strict=True).parent == resolved_dir
            )
        except (OSError, RuntimeError):
            continue
        if files:
            source_dir, frames = resolved_dir, files[:72]
            break
    if not source_dir or not frames:
        return False

    exterior_stage = Path(tempfile.mkdtemp(prefix=".clean-exterior-", dir=vehicle_dir))
    spin_stage = None
    try:
        for frame in frames:
            if frame.stat().st_size > 100 * 1024 * 1024:
                raise ValueError(f"Source frame exceeds processing limit: {frame.name}")
            process_exterior_photo(frame.read_bytes(), exterior_stage / frame.name)
        _replace_directory(exterior_stage, exterior_dir)

        spin_stage = Path(tempfile.mkdtemp(prefix=".clean-spin-", dir=vehicle_dir))
        manifest = None
        manifest_path = vehicle_dir / "manifest.json"
        if manifest_path.is_file() and not manifest_path.is_symlink():
            try:
                if manifest_path.stat().st_size <= 1_000_000:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError):
                manifest = None
        if not interpolate_frames(exterior_dir, spin_stage, target_frames=36, manifest=manifest):
            return False

        vehicle = db_get_vehicle(vin)
        turntable_style = str(vehicle.get("turntable_style", "studio_shadow") if vehicle else "studio_shadow")
        background_path = None
        background_slug = str(vehicle.get("background_id", "") if vehicle else "")
        if background_slug and background_slug != "none":
            with sqlite3.connect(str(DB_PATH), timeout=5) as conn:
                row = conn.execute("SELECT url FROM backgrounds WHERE slug=?", (background_slug,)).fetchone()
            if row and row[0] and str(row[0]).startswith("/static/"):
                candidate = Path(__file__).parent / "static" / str(row[0])[len("/static/"):]
                resolved_static = (Path(__file__).parent / "static").resolve()
                resolved_candidate = candidate.resolve()
                if resolved_candidate.is_relative_to(resolved_static) and resolved_candidate.is_file():
                    background_path = str(resolved_candidate)

        process_all_frames_background(spin_stage, "clean", background_path, turntable_style)
        spin_dir = vehicle_dir / "spin"
        _replace_directory(spin_stage, spin_dir)
        spin_stage = None
        generate_thumbnail(spin_dir, vehicle_dir / "thumbnail.jpg")
        db_update_counts(vin)
        return True
    except Exception as exc:
        print(f"Generate clean frames error for {vin}: {type(exc).__name__}: {exc}")
        return False
    finally:
        if exterior_stage.exists():
            shutil.rmtree(exterior_stage, ignore_errors=True)
        if spin_stage is not None and spin_stage.exists():
            shutil.rmtree(spin_stage, ignore_errors=True)

def _write_clean_frame_status(vin: str, status: str, error: str = "") -> None:
    vin = validate_vehicle_id(vin)
    storage_root = STORAGE_DIR.resolve()
    vehicle_dir = STORAGE_DIR / vin
    if vehicle_dir.is_symlink():
        raise OSError("Vehicle storage cannot be a symlink")
    vehicle_dir.mkdir(parents=True, exist_ok=True)
    resolved_vehicle_dir = vehicle_dir.resolve(strict=True)
    if resolved_vehicle_dir.parent != storage_root or resolved_vehicle_dir.name != vin:
        raise OSError("Vehicle storage path is invalid")
    status_path = resolved_vehicle_dir / "clean-frame-status.json"
    temp_path = status_path.with_name(f".clean-frame-status-{uuid.uuid4().hex}.tmp")
    payload = {"vin": vin, "status": status, "updated_at": datetime.now().isoformat()}
    if error:
        payload["error"] = error[:400]
    try:
        temp_path.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temp_path, status_path)
    finally:
        temp_path.unlink(missing_ok=True)


def _run_clean_frames_job(vin: str) -> None:
    """Run paid clean derivatives as tracked bounded background work."""
    try:
        _write_clean_frame_status(vin, "running")
        if not generate_clean_frames(vin):
            _write_clean_frame_status(
                vin, "error", "Clean source frames were unavailable or could not be processed"
            )
            return
        _write_clean_frame_status(vin, "done")
    except Exception as exc:
        try:
            _write_clean_frame_status(vin, "error", f"{type(exc).__name__}: {exc}")
        except OSError:
            pass
        print(f"[Clean Frames] {vin}: {type(exc).__name__}: {exc}")


# =============================================================================
# PHASE 2: CREDIT CHECKING SYSTEM
# =============================================================================
def check_user_can_render(email: str) -> dict:
    """
    Check if user can render a vehicle.
    Returns: {"allowed": bool, "reason": str, "credits": int, "is_subscribed": bool, "plan": str}
    """
    if not _payments_available():
        return {"allowed": True, "reason": "Payments disabled or pricing unapproved", "credits": -1, "is_subscribed": False, "plan": "free"}
    if not email:
        return {"allowed": True, "reason": "No user - free mode", "credits": 0, "is_subscribed": False, "plan": "free"}
    
    user = db_get_user(email)
    if not user:
        return {"allowed": True, "reason": "New user - free credits", "credits": 10, "is_subscribed": False, "plan": "free"}
    
    credits = user.get("credits", 0)
    is_subscribed = user.get("is_subscribed", False)
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
def db_create_dealer(
    name: str,
    email: str = "",
    display_name: str = "",
    background_preset: str = "showroom",
    user_email: str = "",
) -> int | None:
    """Create a dealer and bind it to an authenticated account email."""
    name = (name or "").strip()[:120]
    email = (email or "").strip().lower()[:254]
    owner_email = (user_email or email).strip().lower()[:254]
    if not name:
        return None
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        INSERT OR IGNORE INTO dealers
            (name, display_name, email, user_email, background_preset, plan, created_at)
        VALUES (?, ?, ?, ?, ?, 'free', ?)
    """, (name, display_name or name, email, owner_email, background_preset or "showroom",
          datetime.now().isoformat()))
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
    """Get dealer by its owner account email (case-insensitive)."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM dealers WHERE LOWER(COALESCE(user_email,''))=? ORDER BY id LIMIT 1",
        ((email or "").strip().lower(),),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _account_dealer(email: str, *, create: bool = False) -> dict | None:
    """Resolve the current account's tenant without trusting user-supplied names."""
    email = (email or "").strip().lower()
    if not email:
        return None
    dealer = db_get_dealer_by_email(email)
    if dealer or not create:
        return dealer
    import hashlib
    name = "account-" + hashlib.sha256(email.encode("utf-8")).hexdigest()[:20]
    db_create_dealer(name, email=email, display_name="", user_email=email)
    return db_get_dealer(name)


def _require_dealer_owner(request: Request, dealer_name: str) -> dict:
    dealer = db_get_dealer(dealer_name)
    if not dealer:
        raise HTTPException(status_code=404, detail="Dealer not found")
    email = _authenticated_user_email(request)
    owner_email = os.getenv("LOTVUE_OWNER_EMAIL", "").strip().lower()
    tenant_email = str(dealer.get("user_email", "") or "").strip().lower()
    if not (owner_email and email == owner_email) and tenant_email != email:
        raise HTTPException(status_code=404, detail="Dealer not found")
    return dealer


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
    "single": {"credits": 1, "price": 2.99, "label": "Single"},
    "starter": {"credits": 10, "price": 19.99, "label": "Starter (10 vehicles)"},
    "dealership": {"credits": 999999, "price": 75.00, "label": "Dealership Monthly"},
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
def set_watermark(vin: str, request: Request, mode: str = Form("clean")):
    vin = validate_vehicle_id(vin)
    _require_vehicle_owner(request, vin)
    vehicle = db_get_vehicle(vin)
    if not vehicle or vehicle.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")
    if not FEATURES_ENABLED.get("watermark", False):
        return {"status": "ok", "mode": "clean", "note": "watermark feature disabled"}
    if mode not in ("preview", "clean"):
        raise HTTPException(status_code=400, detail="Mode must be 'preview' or 'clean'")
    if mode == "clean":
        _write_clean_frame_status(vin, "queued")
        submission = _submit_background_job(
            f"clean-frames:{vin}", _run_clean_frames_job, vin
        )
        if submission == "busy":
            _write_clean_frame_status(vin, "error", "Background processing capacity is full")
            raise HTTPException(status_code=429, detail="Background processing capacity is full",
                                headers={"Retry-After": "30"})

    with sqlite3.connect(str(DB_PATH), timeout=10) as conn:
        conn.execute("UPDATE vehicles SET watermark_mode=? WHERE vin=? AND status='active'", (mode, vin))

    if mode == "clean":
        return {"status": "processing", "mode": mode, "vin": vin}
    return {"status": "ok", "mode": mode}


@app.get("/api/watermark/{vin}")
def get_watermark(vin: str, request: Request):
    vin = validate_vehicle_id(vin)
    _require_vehicle_owner(request, vin)
    vehicle = db_get_vehicle(vin)
    if not vehicle or vehicle.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")
    if not FEATURES_ENABLED.get("watermark", False):
        return {"watermark_mode": "clean"}
    return {"watermark_mode": vehicle.get("watermark_mode", "preview")}


@app.get("/api/plans")
def get_plans():
    if not _pricing_is_approved():
        return {"status": "pending_approval", "plans": []}
    return {"status": "ok", "plans": PLANS}


# =============================================================================
# API — STRIPE PAYMENTS
# =============================================================================
STRIPE_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

@app.get("/api/stripe/config")
def stripe_config():
    return {"publishable_key": STRIPE_PUBLISHABLE}


@app.post("/api/create-checkout-session")
def create_checkout_session(
    request: Request,
    plan_id: str = Form(...),
    user_email: str = Form("")
):
    user_email = _authenticated_user_email(request)
    if not _payments_available():
        return {"status": "disabled", "error": "Payments feature is disabled"}
    if not STRIPE_KEY:
        return {"status": "error", "error": "Stripe not configured"}
    
    plan = PLANS.get(plan_id)
    if not plan:
        return {"status": "error", "error": "Invalid plan"}
    
    try:
        import stripe
        stripe.api_key = STRIPE_KEY
        
        public_base = os.environ.get("LOTVUE_PUBLIC_URL", str(request.base_url)).rstrip("/")
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
            success_url=f"{public_base}/?payment=success&plan={plan_id}",
            cancel_url=f"{public_base}/?payment=cancelled",
            customer_email=user_email if user_email else None,
            metadata={"plan_id": plan_id, "credits": str(plan["credits"]), "user_email": user_email}
        )
        return {"status": "ok", "session_url": session.url, "session_id": session.id}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _apply_stripe_event(event: dict) -> tuple[bool, list[str]]:
    """Apply a verified Stripe event once, transactionally with its idempotency row."""
    event_id = str(event.get("id") or "").strip()
    event_type = str(event.get("type") or "").strip()
    if not event_id or len(event_id) > 255 or not event_type:
        raise ValueError("Stripe event is missing a valid id or type")

    now = datetime.now().isoformat()
    clean_frames_for = []
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        inserted = conn.execute(
            "INSERT OR IGNORE INTO stripe_webhook_events "
            "(event_id,event_type,status,received_at) VALUES (?,?, 'processing',?)",
            (event_id, event_type, now),
        )
        if inserted.rowcount == 0:
            existing = conn.execute(
                "SELECT status FROM stripe_webhook_events WHERE event_id=?", (event_id,)
            ).fetchone()
            if existing and existing["status"] == "processed":
                conn.commit()
                return False, []
            conn.execute(
                "UPDATE stripe_webhook_events SET event_type=?,status='processing',received_at=?,error='' "
                "WHERE event_id=?",
                (event_type, now, event_id),
            )

        if event_type in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
            session = (event.get("data") or {}).get("object") or {}
            metadata = session.get("metadata") or {}
            session_id = str(session.get("id") or "").strip()
            payment_status = session.get("payment_status")
            if session_id and payment_status in {None, "paid", "no_payment_required"}:
                payment_type = str(metadata.get("type") or "")
                purchaser_email = str(
                    metadata.get("user_email") or session.get("customer_email") or ""
                ).strip().lower()
                if payment_type == "unlock_vehicle":
                    vin = str(metadata.get("vin") or "").strip().upper()
                    if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin) and purchaser_email:
                        vehicle = conn.execute(
                            "SELECT user_email,status,is_paid FROM vehicles WHERE vin=?", (vin,)
                        ).fetchone()
                        owner_email = os.getenv("LOTVUE_OWNER_EMAIL", "").strip().lower()
                        actual_owner = str(vehicle["user_email"] or "").strip().lower() if vehicle else ""
                        if (vehicle and vehicle["status"] == "active"
                                and purchaser_email in {actual_owner, owner_email}):
                            if not bool(vehicle["is_paid"]):
                                updated = conn.execute(
                                    "UPDATE vehicles SET is_paid=1,watermark_mode='clean',payment_id=? "
                                    "WHERE vin=? AND status='active' AND COALESCE(is_paid,0)=0",
                                    (session_id, vin),
                                )
                                if updated.rowcount:
                                    clean_frames_for.append(vin)
                            else:
                                # An earlier webhook attempt may have recorded payment
                                # before the bounded image-work queue had capacity.
                                clean_frames_for.append(vin)
                elif payment_type == "subscription":
                    plan_type = str(metadata.get("plan_type") or "dealership")
                    if plan_type not in {"pro_monthly", "dealership", "enterprise"}:
                        plan_type = "dealership"
                    conn.execute(
                        "UPDATE users SET is_subscribed=1,plan_type=? WHERE LOWER(email)=?",
                        (plan_type, purchaser_email),
                    )
                elif purchaser_email:
                    plan_id = str(metadata.get("plan_id") or "single")
                    plan = PLANS.get(plan_id)
                    if plan and plan_id != "free" and float(plan.get("price", 0) or 0) > 0:
                        try:
                            credits = int(plan.get("credits", 0))
                            amount = max(0, int(session.get("amount_total", 0) or 0)) / 100.0
                        except (TypeError, ValueError, OverflowError):
                            credits, amount = 0, 0.0
                        user = conn.execute(
                            "SELECT id FROM users WHERE LOWER(email)=?", (purchaser_email,)
                        ).fetchone()
                        payment_exists = conn.execute(
                            "SELECT 1 FROM payments WHERE stripe_payment_id=? LIMIT 1", (session_id,)
                        ).fetchone()
                        if user and credits > 0 and not payment_exists:
                            conn.execute(
                                "UPDATE users SET credits=COALESCE(credits,0)+? WHERE id=?",
                                (credits, user["id"]),
                            )
                            conn.execute(
                                "INSERT INTO credit_log (user_id,amount,action,vin,created_at) "
                                "VALUES (?,?,?,?,?)",
                                (user["id"], credits, f"stripe_purchase_{plan_id}", "", now),
                            )
                            conn.execute(
                                "INSERT INTO payments "
                                "(user_id,amount,plan,credits_purchased,stripe_payment_id,status,created_at) "
                                "VALUES (?,?,?,?,?,'completed',?)",
                                (user["id"], amount, plan_id, credits, session_id, now),
                            )

        elif event_type in {
            "customer.subscription.created",
            "customer.subscription.updated",
            "customer.subscription.deleted",
        }:
            subscription = (event.get("data") or {}).get("object") or {}
            metadata = subscription.get("metadata") or {}
            email = str(metadata.get("user_email") or "").strip().lower()
            if email:
                if event_type == "customer.subscription.deleted":
                    conn.execute(
                        "UPDATE users SET is_subscribed=0,plan_type='free' WHERE LOWER(email)=?", (email,)
                    )
                elif str(subscription.get("status") or "") in {"active", "trialing"}:
                    plan_type = str(metadata.get("plan_type") or "dealership")
                    if plan_type not in {"pro_monthly", "dealership", "enterprise"}:
                        plan_type = "dealership"
                    conn.execute(
                        "UPDATE users SET is_subscribed=1,plan_type=? WHERE LOWER(email)=?",
                        (plan_type, email),
                    )

        conn.execute(
            "UPDATE stripe_webhook_events SET status='processed',processed_at=?,error='' WHERE event_id=?",
            (datetime.now().isoformat(), event_id),
        )
        conn.commit()
        return True, clean_frames_for
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _mark_stripe_event_retry(event_id: str, reason: str) -> None:
    """Keep a committed payment event retryable if its required job cannot queue."""
    with closing(sqlite3.connect(str(DB_PATH), timeout=10)) as conn, conn:
        conn.execute(
            "UPDATE stripe_webhook_events SET status='retry',error=? WHERE event_id=?",
            (str(reason)[:400], event_id),
        )


@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    if not _payments_available():
        return JSONResponse({"status": "disabled", "error": "Payments feature is disabled"}, status_code=503)
    if not STRIPE_KEY:
        return JSONResponse({"error": "Stripe not configured"}, status_code=500)
    if not STRIPE_WEBHOOK_SECRET:
        return JSONResponse({"error": "Verified webhook signing secret is required"}, status_code=503)

    try:
        import stripe
        stripe.api_key = STRIPE_KEY
        payload = await _read_limited_body(request, 1024 * 1024)
        event = stripe.Webhook.construct_event(
            payload, request.headers.get("stripe-signature", ""), STRIPE_WEBHOOK_SECRET
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    try:
        processed, unlock_vins = await run_in_threadpool(_apply_stripe_event, event)
    except Exception as exc:
        print(f"[Stripe] webhook processing failed: {type(exc).__name__}: {exc}")
        return JSONResponse({"error": "Webhook processing failed; retry is safe"}, status_code=500)

    for vin in unlock_vins:
        try:
            await run_in_threadpool(_write_clean_frame_status, vin, "queued")
            submission = _submit_background_job(
                f"clean-frames:{vin}", _run_clean_frames_job, vin
            )
        except Exception as exc:
            await run_in_threadpool(
                _mark_stripe_event_retry, str(event.get("id") or ""), type(exc).__name__
            )
            print(f"[Stripe] clean-frame job could not be queued for {vin}: {type(exc).__name__}")
            return JSONResponse(
                {"error": "Payment recorded; clean-frame processing will be retried"},
                status_code=503,
                headers={"Retry-After": "30"},
            )
        if submission == "busy":
            await run_in_threadpool(
                _mark_stripe_event_retry, str(event.get("id") or ""), "background queue full"
            )
            print(f"[Stripe] clean-frame queue full after vehicle unlock for {vin}")
            return JSONResponse(
                {"error": "Payment recorded; clean-frame processing will be retried"},
                status_code=503,
                headers={"Retry-After": "30"},
            )
    return {"status": "received" if processed else "already_processed"}


# =============================================================================
# PHASE 3: STRIPE SUBSCRIPTION & RENDER UNLOCK
# =============================================================================
@app.post("/api/stripe/create-subscription")
def create_subscription(
    request: Request,
    user_email: str = Form(""),
    plan_type: str = Form("dealership")
):
    """Create monthly subscription for the signed-in account only."""
    user_email = _authenticated_user_email(request)
    if not _payments_available():
        return {"status": "disabled", "error": "Payments feature is disabled"}
    if not STRIPE_KEY:
        return {"error": "Stripe not configured"}
    
    # Only the published monthly dealership tier is eligible here. Other entries
    # in the legacy subscription catalog have no approved public counterpart.
    plan = PRICING_PLANS.get(plan_type) if plan_type == "dealership" else None
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
        
        subscription = stripe.Subscription.create(
            customer=customer.id,
            items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": f"LotVue Pro - {plan['label']}"},
                    "unit_amount": int(round(float(plan["price"]) * 100)),
                    "recurring": {"interval": "month"},
                },
                "quantity": 1,
            }],
            metadata={"user_email": user_email, "plan_type": plan_type}
        )
        
        return {"status": "ok", "subscription_id": subscription.id, "client_secret": subscription.get("latest_invoice", {}).get("payment_intent", {}).get("client_secret")}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/render/unlock")
def unlock_render(
    request: Request,
    vin: str = Form(...),
    user_email: str = Form("")
):
    """Create payment link to unlock a vehicle owned by the signed-in account."""
    vin = validate_vehicle_id(vin)
    vehicle = _require_vehicle_owner(request, vin)
    user_email = _authenticated_user_email(request)
    if not _payments_available():
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
                    "unit_amount": int(round(float(PLANS["single"]["price"]) * 100)),
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
def get_render_status(vin: str, request: Request):
    """Check render progress only for an active vehicle owned by the requester."""
    vin = validate_vehicle_id(vin)
    _require_vehicle_owner(request, vin)
    vehicle = db_get_vehicle(vin)
    if not vehicle or vehicle.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")
    
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
def get_render_preview(vin: str, request: Request):
    """Get a preview URL only for an active vehicle owned by the requester."""
    vin = validate_vehicle_id(vin)
    _require_vehicle_owner(request, vin)
    vehicle = db_get_vehicle(vin)
    if not vehicle or vehicle.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")
    
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
def get_render_clean(vin: str, request: Request):
    """Get a clean URL only for a paid vehicle owned by the requester."""
    vin = validate_vehicle_id(vin)
    _require_vehicle_owner(request, vin)
    user_email = _authenticated_user_email(request)
    vehicle = db_get_vehicle(vin)
    if not vehicle or vehicle.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")
    
    # Check if paid
    if not vehicle.get("is_paid", 0):
        # Check if user has unlimited plan
        if user_email:
            check = check_user_can_render(user_email)
            if not check.get("is_subscribed"):
                return {"error": "Payment required", "payment_url": f"/view/{vin}?unlock=1"}
        else:
            return {"error": "Payment required", "payment_url": f"/view/{vin}?unlock=1"}
    
    clean_status_path = STORAGE_DIR / vin / "clean-frame-status.json"
    if clean_status_path.is_file() and not clean_status_path.is_symlink():
        try:
            if clean_status_path.stat().st_size <= 16 * 1024:
                clean_status = json.loads(clean_status_path.read_text(encoding="utf-8"))
                if clean_status.get("status") in {"queued", "running"}:
                    return {"status": "processing", "vin": vin, "is_paid": bool(vehicle.get("is_paid", 0))}
                if clean_status.get("status") == "error":
                    return {"status": "error", "vin": vin,
                            "error": str(clean_status.get("error") or "Clean frames could not be generated")[:400]}
        except (OSError, ValueError, AttributeError):
            pass

    spin_dir = STORAGE_DIR / vin / "spin"
    if spin_dir.exists() and list(spin_dir.glob("spin_*.jpg")):
        return {
            "vin": vin,
            "clean_url": f"/storage/{vin}/spin/spin_0001.jpg",
            "is_paid": bool(vehicle.get("is_paid", 0))
        }
    
    return {"error": "No frames found"}


@app.get("/api/inventory")
def get_inventory(request: Request, dealer: str = "", user_email: str = ""):
    """List inventory for the authenticated tenant; caller-supplied tenant filters are ignored."""
    actor_email = _authenticated_user_email(request)
    owner_email = os.getenv("LOTVUE_OWNER_EMAIL", "").strip().lower()
    vehicles = db_get_vehicles() if owner_email and actor_email == owner_email else db_get_vehicles_by_email(actor_email)
    
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
def delete_render(vin: str, request: Request):
    """Delete an active render only when the requester owns its vehicle."""
    vin = validate_vehicle_id(vin)
    _require_vehicle_owner(request, vin)
    vehicle = db_get_vehicle(vin)
    if not vehicle or vehicle.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")

    vehicle_dir = (STORAGE_DIR / vin).resolve()
    if vehicle_dir.parent != STORAGE_DIR.resolve():
        raise HTTPException(status_code=400, detail="Invalid vehicle path")
    if vehicle_dir.exists():
        shutil.rmtree(vehicle_dir)

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE vehicles SET status='deleted' WHERE vin=?", (vin,))
    conn.commit()
    conn.close()
    
    return {"status": "ok", "vin": vin}


@app.get("/api/user/{email}")
def get_user(email: str, request: Request):
    actor_email = _authenticated_user_email(request)
    if email.strip().lower() != actor_email:
        raise HTTPException(status_code=404, detail="User not found")
    email = actor_email
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
def register_user(
    request: Request,
    email: str = Form(...),
    name: str = Form(""),
    password: str = Form("")
):
    if not FEATURES_ENABLED.get("auth", False):
        raise HTTPException(status_code=503, detail="Account registration is temporarily disabled")
    check_auth_rate(request, "register", 4)
    email = email.strip().lower()
    name = name.strip()
    if len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=400, detail="Enter a valid email address")
    if len(name) > 120:
        raise HTTPException(status_code=400, detail="Name must be 120 characters or fewer")
    if len(password) < 12 or len(password) > 1024:
        raise HTTPException(status_code=400, detail="Password must be 12–1,024 characters")
    owner_email = os.getenv("LOTVUE_OWNER_EMAIL", "").strip().lower()
    if owner_email and email == owner_email:
        raise HTTPException(status_code=409, detail="This address is reserved for the platform owner")
    if db_get_user(email):
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    try:
        pw_hash = hash_password(password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db_create_user(email, name, pw_hash)
    # INSERT OR IGNORE is retained for legacy account-creation call sites. Check
    # the result here so a simultaneous registration cannot look like success.
    created = db_get_user(email)
    if not created or created.get("password_hash") != pw_hash:
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    return {"status": "ok", "email": email}


@app.post("/api/login")
def login_user(
    request: Request,
    response: Response,
    email: str = Form(...),
    password: str = Form("")
):
    if not FEATURES_ENABLED.get("auth", False):
        raise HTTPException(status_code=503, detail="Authentication is temporarily disabled")
    check_auth_rate(request, "login", 8)
    email = email.strip().lower()
    user = db_get_user(email)
    stored_hash = user.get("password_hash") if user else None
    if not verify_password(password, stored_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if needs_rehash(stored_hash):
        upgraded = hash_password(password)
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("UPDATE users SET password_hash=? WHERE email=?", (upgraded, email))
        conn.commit()
        conn.close()
        user["password_hash"] = upgraded
    # issue the same HttpOnly JWT session cookie the auth gate checks
    try:
        from a2_extensions import _make_tokens, ACCESS_TTL_H
        from security import secure_cookie_for_request
        tokens = _make_tokens(user["email"])
        response.set_cookie("lotvue_token", tokens["access_token"], httponly=True,
                            secure=secure_cookie_for_request(request), samesite="lax",
                            path="/", max_age=ACCESS_TTL_H * 3600)
    except Exception as e:
        print(f"[Auth] cookie issue failed: {e}")
        raise HTTPException(status_code=503, detail="Unable to create a secure session") from e
    return {"status": "ok", "email": user["email"], "credits": user.get("credits", 5), "plan": user.get("plan", "free")}


@app.post("/api/set-plan")
def set_user_plan(
    email: str = Form(...),
    plan: str = Form("free")
):
    # Never let a browser-provided email/plan grant credits or a subscription.
    # Plan state is changed only by a verified Stripe webhook or an operator.
    if not _payments_available():
        raise HTTPException(status_code=503, detail="Billing is not configured")
    raise HTTPException(status_code=403, detail="Plan changes must complete through verified checkout")


# =============================================================================
# API — HEALTH CHECK
# =============================================================================
@app.get("/api/health")
def health_check():
    """Minimal liveness response; do not publish tenant counts or filesystem paths."""
    return {
        "status": "ok",
        "app": "LotVue Pro",
        "version": "2.0.0",
        "heic_available": HEIC_SUPPORTED,
    }


# =============================================================================
# API — USER CREDIT STATUS (Phase 2)
# =============================================================================
@app.get("/api/credits")
def get_user_credits(request: Request, email: str = ""):
    """Get the signed-in user's credit status; query-string identities are ignored."""
    email = _authenticated_user_email(request)
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
def get_background_presets():
    """Get available background presets."""
    return {"presets": BACKGROUND_PRESETS}


@app.post("/api/tenant/dealer")
def create_dealer(
    request: Request,
    name: str = Form(...),
    email: str = Form(""),
    display_name: str = Form(""),
    background_preset: str = Form("showroom")
):
    """Create the signed-in account's dealer tenant; never accept an owner email."""
    actor_email = _authenticated_user_email(request)
    display_name = (display_name or name or "").strip()
    contact_email = (email or actor_email).strip().lower()
    if not display_name or len(display_name) > 120:
        raise HTTPException(status_code=400, detail="Dealer name must be 1–120 characters")
    if len(contact_email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", contact_email):
        raise HTTPException(status_code=400, detail="Enter a valid dealer email")
    if background_preset not in BACKGROUND_PRESETS:
        raise HTTPException(status_code=400, detail="Unknown background preset")
    if _account_dealer(actor_email):
        raise HTTPException(status_code=409, detail="This account already has a dealer tenant")
    import hashlib
    tenant_name = "account-" + hashlib.sha256(actor_email.encode("utf-8")).hexdigest()[:20]
    dealer_id = db_create_dealer(
        tenant_name, contact_email, display_name, background_preset, user_email=actor_email
    )
    return {"status": "ok", "dealer_id": dealer_id, "name": tenant_name}


@app.get("/api/tenant/dealer/{dealer_name}")
def get_tenant_dealer(dealer_name: str, request: Request):
    """Get dealer details for the owning tenant only."""
    return _require_dealer_owner(request, dealer_name)


@app.get("/api/tenant/dealers")
def list_dealers(request: Request):
    """List only this account's dealer, unless the configured owner is signed in."""
    email = _authenticated_user_email(request)
    owner_email = os.getenv("LOTVUE_OWNER_EMAIL", "").strip().lower()
    dealers = db_list_dealers() if owner_email and email == owner_email else []
    if not dealers:
        dealer = _account_dealer(email)
        if dealer:
            dealers = [dealer]
    return {"dealers": dealers}


@app.put("/api/tenant/dealer/{dealer_name}")
def update_dealer(
    dealer_name: str,
    request: Request,
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
    """Update an owned dealer's profile; billing state is never client-writable."""
    _require_dealer_owner(request, dealer_name)
    if plan:
        raise HTTPException(status_code=403, detail="Plan changes require verified billing")
    for field_name, value, maximum in (
        ("display name", display_name, 120), ("phone", phone, 40), ("email", email, 254),
        ("address", address, 300), ("website", website, 300), ("logo", logo_url, 500),
    ):
        if len(value or "") > maximum:
            raise HTTPException(status_code=400, detail=f"{field_name} is too long")
    if brand_color and not re.fullmatch(r"#[0-9A-Fa-f]{6}", brand_color):
        raise HTTPException(status_code=400, detail="Brand color must be a six-digit hex color")
    if background_preset and background_preset not in BACKGROUND_PRESETS:
        raise HTTPException(status_code=400, detail="Unknown background preset")

    updates = {}
    if display_name: updates["display_name"] = display_name
    if phone: updates["phone"] = phone
    if email: updates["email"] = email
    if address: updates["address"] = address
    if website: updates["website"] = website
    if logo_url: updates["logo_url"] = logo_url
    if brand_color: updates["brand_color"] = brand_color
    if background_preset: updates["background_preset"] = background_preset
    if updates:
        db_update_dealer(dealer_name, **updates)
    return {"status": "ok", "dealer": db_get_dealer(dealer_name)}


@app.get("/api/tenant/me")
def get_my_dealer(request: Request):
    """Get the signed-in user's own dealer (email query parameters are ignored)."""
    email = _authenticated_user_email(request)
    dealer = _account_dealer(email)
    if not dealer:
        return {"error": "No dealer found", "has_dealer": False}
    return {"dealer": dealer, "has_dealer": True}


# =============================================================================
# HELPERS
# =============================================================================
def validate_vehicle_id(value: str) -> str:
    """Normalize a VIN or internal ID used in storage paths and HTML."""
    vehicle_id = (value or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{1,32}", vehicle_id):
        raise HTTPException(status_code=400, detail="Invalid vehicle identifier")
    return vehicle_id


def _json_for_script(value) -> str:
    """Serialize data so strings cannot terminate an inline script element."""
    return (json.dumps(value, ensure_ascii=True)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def _authenticated_user_email(request: Request) -> str:
    """Return the subject of a valid access token (cookie or bearer), never form data."""
    from auth_gate import _bearer_or_cookie
    from a2_extensions import JWT_ALG, _jwt_secret
    from jose import JWTError, jwt

    token = _bearer_or_cookie(request)
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALG])
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Sign in required") from exc
    email = str(payload.get("sub", "")).strip().lower()
    if payload.get("type") != "access" or not email:
        raise HTTPException(status_code=401, detail="Sign in required")
    # The auth gate already proved this account exists for this request; reuse
    # that verdict instead of re-opening the database on every call.
    if getattr(request.state, "lotvue_user_email", None) == email:
        return email
    if not db_get_user(email):
        raise HTTPException(status_code=401, detail="Sign in required")
    return email


def _require_vehicle_access(
    request: Request,
    vehicle_id: str,
    *,
    allow_create: bool = False,
) -> tuple[str, Optional[dict]]:
    """Resolve the actor and enforce tenant ownership before vehicle reads/writes."""
    vehicle_id = validate_vehicle_id(vehicle_id)
    email = _authenticated_user_email(request)
    storage_root = STORAGE_DIR.resolve()
    vehicle_path = STORAGE_DIR / vehicle_id
    try:
        if vehicle_path.is_symlink():
            raise HTTPException(status_code=404, detail="Vehicle not found")
        if vehicle_path.exists():
            resolved_vehicle_path = vehicle_path.resolve(strict=True)
            if (resolved_vehicle_path.parent != storage_root
                    or resolved_vehicle_path.name != vehicle_id
                    or not resolved_vehicle_path.is_dir()):
                raise HTTPException(status_code=404, detail="Vehicle not found")
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=404, detail="Vehicle not found") from exc
    vehicle = db_get_vehicle(vehicle_id)
    if not vehicle or vehicle.get("status", "active") != "active":
        if allow_create and not vehicle:
            return email, None
        raise HTTPException(status_code=404, detail="Vehicle not found")
    owner_email = os.getenv("LOTVUE_OWNER_EMAIL", "").strip().lower()
    vehicle_email = str(vehicle.get("user_email", "") or "").strip().lower()
    if not (owner_email and email == owner_email) and vehicle_email != email:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    return email, vehicle


def _require_vehicle_owner(request: Request, vehicle_id: str) -> dict:
    """Find a vehicle and ensure the authenticated tenant owns it."""
    _, vehicle = _require_vehicle_access(request, vehicle_id)
    return vehicle or {}


def _safe_vehicle_directory(vin: str) -> Path:
    """Resolve an existing vehicle directory without following storage symlinks."""
    vin = validate_vehicle_id(vin)
    storage_root = Path(STORAGE_DIR).resolve(strict=True)
    vehicle_path = Path(STORAGE_DIR) / vin
    if vehicle_path.is_symlink():
        raise HTTPException(status_code=404, detail="Vehicle not found")
    try:
        vehicle_dir = vehicle_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=404, detail="Vehicle not found") from exc
    if (vehicle_dir.parent != storage_root or vehicle_dir.name != vin
            or not vehicle_dir.is_dir()):
        raise HTTPException(status_code=404, detail="Vehicle not found")
    return vehicle_dir


def _ensure_safe_vehicle_subdir(vin: str, subdir: str, *, create: bool = False) -> Path:
    """Resolve a known vehicle media directory; reject symlinks and escapes."""
    if subdir not in {
        "raw", "source", "_3dphotos", "exterior", "spin", "spin_hd", "interior", "panoramas",
        "gallery", "hotspot_photos", "walkaround", "video", "videos", "commercial",
        "video_ads", "exports", "studio", "studio_spin", "splat", "3d",
    }:
        raise HTTPException(status_code=400, detail="Invalid vehicle media directory")
    vehicle_dir = _safe_vehicle_directory(vin)
    directory = vehicle_dir / subdir
    if directory.is_symlink():
        raise HTTPException(status_code=404, detail="Vehicle media not found")
    if create:
        try:
            directory.mkdir(exist_ok=True)
        except OSError as exc:
            raise HTTPException(status_code=500, detail="Vehicle media directory unavailable") from exc
    try:
        resolved = directory.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=404, detail="Vehicle media not found") from exc
    if resolved.parent != vehicle_dir or resolved.name != subdir or not resolved.is_dir():
        raise HTTPException(status_code=404, detail="Vehicle media not found")
    return resolved


def _vehicle_media_file_exists(vin: str, subdir: str, filename: str) -> bool:
    try:
        directory = _ensure_safe_vehicle_subdir(vin, subdir)
        candidate = directory / filename
        if candidate.is_symlink():
            return False
        resolved = candidate.resolve(strict=True)
        return resolved.parent == directory and resolved.is_file()
    except HTTPException:
        return False


def _ensure_brand_storage_dir() -> Path:
    """Create the reserved brand-media directory without following a symlink."""
    storage_root = Path(STORAGE_DIR).resolve(strict=True)
    brand_dir = Path(STORAGE_DIR) / "_brand"
    if brand_dir.is_symlink():
        raise HTTPException(status_code=404, detail="Brand storage is unavailable")
    brand_dir.mkdir(exist_ok=True)
    resolved = brand_dir.resolve(strict=True)
    if resolved.parent != storage_root or resolved.name != "_brand" or not resolved.is_dir():
        raise HTTPException(status_code=404, detail="Brand storage is unavailable")
    return resolved


def _resolve_vehicle_image_ref(image_ref: str) -> tuple[str, Path]:
    """Resolve an image reference only when every path component is a regular child."""
    storage_root = Path(STORAGE_DIR).resolve(strict=True)
    if image_ref.startswith("/storage/"):
        relative_text = image_ref[len("/storage/"):]
        parts = relative_text.split("/")
        candidate = Path(STORAGE_DIR).joinpath(*parts)
    else:
        candidate = Path(image_ref).expanduser()
        if any(part in {".", ".."} for part in candidate.parts):
            raise HTTPException(status_code=400, detail="Image path contains traversal")
        try:
            relative = candidate.absolute().relative_to(storage_root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Image must be in vehicle storage") from exc
        parts = list(relative.parts)
        candidate = storage_root.joinpath(*parts)
    if (len(parts) < 2 or any(part in {"", ".", ".."} for part in parts)
            or any(part.startswith(".") for part in parts)):
        raise HTTPException(status_code=400, detail="Image must belong to a vehicle")
    vin = validate_vehicle_id(parts[0])
    current = storage_root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise HTTPException(status_code=400, detail="Symlinked images cannot be composited")
    try:
        resolved = current.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Image must be in vehicle storage") from exc
    if not resolved.is_relative_to(storage_root) or not resolved.is_file():
        raise HTTPException(status_code=400, detail="Image must be in vehicle storage")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", resolved.stem):
        raise HTTPException(status_code=400, detail="Invalid image filename")
    return vin, resolved


def _prepare_vehicle_upload(
    vin: str,
    user_email: str,
    existing_vehicle: Optional[dict],
    subdirs: tuple[str, ...],
    *,
    year: str = "",
    make: str = "",
    model: str = "",
    job_id: str = "",
) -> Path:
    """Initialize tenant-owned upload folders and its first vehicle row."""
    vin = validate_vehicle_id(vin)
    vehicle_path = Path(STORAGE_DIR) / vin
    if existing_vehicle is None and vehicle_path.exists():
        raise HTTPException(status_code=409, detail="Vehicle storage already exists; contact support")
    vehicle_path.mkdir(parents=True, exist_ok=True)
    vehicle_dir = _safe_vehicle_directory(vin)
    for subdir in subdirs:
        _ensure_safe_vehicle_subdir(vin, subdir, create=True)
    if existing_vehicle is None:
        db_upsert_vehicle(
            vin, year, make, model, 0, 0, user_email=user_email, job_id=job_id
        )
    return vehicle_dir


def _count_matching_files(directory: Path, suffixes: tuple[str, ...]) -> int:
    try:
        with os.scandir(directory) as entries:
            return sum(
                1 for entry in entries
                if entry.is_file(follow_symlinks=False)
                and Path(entry.name).suffix.lower() in suffixes
            )
    except OSError:
        return 0


def _remove_directory_if_exists(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _unlink_file(path: Path) -> None:
    path.unlink(missing_ok=True)


def _register_panorama_upload(vin: str, path: Path, scene_name: str, sort_order: int) -> None:
    with closing(sqlite3.connect(str(DB_PATH))) as conn, conn:
        conn.execute(
            "INSERT INTO interior_panoramas "
            "(vin, file_path, scene_name, is_equirectangular, sort_order, created_at) "
            "VALUES (?, ?, ?, 1, ?, ?)",
            (vin, str(path), scene_name, sort_order, datetime.now().isoformat()),
        )
        conn.execute("UPDATE vehicles SET show_interior=1 WHERE vin=?", (vin,))


def _register_gallery_upload(
    vin: str, path: Path, category: str, caption: str, sort_order: int
) -> None:
    with closing(sqlite3.connect(str(DB_PATH))) as conn, conn:
        conn.execute(
            "INSERT INTO gallery_photos "
            "(vin, file_path, category, caption, sort_order, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (vin, str(path), category, caption, sort_order, datetime.now().isoformat()),
        )
        conn.execute("UPDATE vehicles SET show_gallery=1 WHERE vin=?", (vin,))


def _insert_hotspot(
    vin: str,
    photo_filename: str,
    tag: str,
    x: float,
    y: float,
    condition_type: str,
    severity: str,
    note: str,
    close_up: str,
) -> int:
    with closing(sqlite3.connect(str(DB_PATH))) as conn, conn:
        cursor = conn.execute(
            "INSERT INTO hotspots (vin, photo_filename, tag, hotspot_x, hotspot_y, "
            "condition_type, severity, note, close_up) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (vin, photo_filename, tag, x, y, condition_type, severity, note, close_up),
        )
        return int(cursor.lastrowid or 0)


def _insert_public_lead(
    dealer_id: int,
    vin: str,
    name: str,
    email: str,
    phone: str,
    message: str,
    source: str,
    wants_test_drive: int,
    wants_video: int,
) -> int:
    with closing(sqlite3.connect(str(DB_PATH))) as conn, conn:
        cursor = conn.execute(
            "INSERT INTO leads "
            "(dealer_id, vin, name, email, phone, message, source, wants_test_drive, wants_video) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (dealer_id, vin, name, email, phone, message, source, wants_test_drive, wants_video),
        )
        return int(cursor.lastrowid or 0)


def _set_vehicle_workflow_status(
    vin: str, workflow_status: str, *, only_if_current: str | None = None
) -> bool:
    with closing(sqlite3.connect(str(DB_PATH))) as conn, conn:
        if only_if_current is None:
            cursor = conn.execute(
                "UPDATE vehicles SET workflow_status=? WHERE vin=?", (workflow_status, vin)
            )
        else:
            cursor = conn.execute(
                "UPDATE vehicles SET workflow_status=? WHERE vin=? AND workflow_status=?",
                (workflow_status, vin, only_if_current),
            )
        return bool(cursor.rowcount)


def _write_workflow_status(vin: str, workflow_status: str) -> None:
    _set_vehicle_workflow_status(vin, workflow_status)


_PIPELINE_JOB_KINDS = {"spin", "photo_studio", "background_studio"}
_PIPELINE_JOB_STATES = {"queued", "running", "completed", "degraded", "error"}


def _write_pipeline_job(
    vin: str,
    kind: str,
    status: str,
    *,
    step: str = "",
    progress: int = 0,
    total: int = 0,
    succeeded: int = 0,
    degraded: int = 0,
    failed: int = 0,
    message: str = "",
) -> None:
    """Persist a privacy-safe, bounded summary for the latest vehicle pipeline job."""
    vin = validate_vehicle_id(vin)
    if kind not in _PIPELINE_JOB_KINDS or status not in _PIPELINE_JOB_STATES:
        raise ValueError("Unknown vehicle pipeline job kind or status")
    def bounded(value: int) -> int:
        return max(0, min(int(value), 10_000))

    step = re.sub(r"[^a-z0-9_-]", "", str(step).lower())[:40]
    message = str(message or "")[:240]
    with closing(sqlite3.connect(str(DB_PATH), timeout=10)) as conn, conn:
        conn.execute(
            "INSERT INTO vehicle_pipeline_jobs "
            "(vin,kind,status,step,progress,total,succeeded,degraded,failed,message,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(vin,kind) DO UPDATE SET status=excluded.status,step=excluded.step,"
            "progress=excluded.progress,total=excluded.total,succeeded=excluded.succeeded,"
            "degraded=excluded.degraded,failed=excluded.failed,message=excluded.message,"
            "updated_at=excluded.updated_at",
            (vin, kind, status, step, max(0, min(100, bounded(progress))), bounded(total),
             bounded(succeeded), bounded(degraded), bounded(failed), message,
             datetime.now().isoformat()),
        )


def _read_pipeline_jobs(vin: str) -> list[dict]:
    vin = validate_vehicle_id(vin)
    with closing(sqlite3.connect(str(DB_PATH), timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT kind,status,step,progress,total,succeeded,degraded,failed,message,updated_at "
            "FROM vehicle_pipeline_jobs WHERE vin=? ORDER BY updated_at DESC",
            (vin,),
        ).fetchall()
    return [dict(row) for row in rows]


def _start_vehicle_processing(vin: str) -> bool:
    with closing(sqlite3.connect(str(DB_PATH), timeout=10)) as conn, conn:
        cursor = conn.execute(
            "UPDATE vehicles SET workflow_status='processing' "
            "WHERE vin=? AND status='active' AND COALESCE(workflow_status,'')!='processing'",
            (vin,),
        )
        return bool(cursor.rowcount)


def _write_vehicle_price(vin: str, price: str) -> None:
    with closing(sqlite3.connect(str(DB_PATH))) as conn, conn:
        conn.execute("UPDATE vehicles SET price=? WHERE vin=?", (price, vin))


def _read_lead_record(lead_id: int) -> dict | None:
    with closing(sqlite3.connect(str(DB_PATH))) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        return dict(row) if row else None


def _require_lead_owner(request: Request, lead: dict) -> str:
    """Authorize dealer lead access through the linked vehicle's tenant."""
    email = _authenticated_user_email(request)
    owner_email = os.getenv("LOTVUE_OWNER_EMAIL", "").strip().lower()
    if owner_email and email == owner_email:
        return email
    vin = str(lead.get("vin", "") or "").strip().upper()
    vehicle = db_get_vehicle(vin) if vin else None
    if not vehicle or str(vehicle.get("user_email", "") or "").strip().lower() != email:
        raise HTTPException(status_code=404, detail="Lead not found")
    return email


def _require_platform_owner(request: Request) -> str:
    email = _authenticated_user_email(request)
    owner_email = os.getenv("LOTVUE_OWNER_EMAIL", "").strip().lower()
    if not owner_email:
        raise HTTPException(status_code=503, detail="Platform owner account is not configured")
    if email != owner_email:
        raise HTTPException(status_code=403, detail="Platform owner access required")
    return email


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


class _MultipartPartLimiter:
    """Count multipart headers incrementally without buffering part payloads."""

    MAX_HEADER_BYTES = 16 * 1024
    MAX_FIELD_BYTES = 64 * 1024

    def __init__(self, boundary: bytes, max_files: int, max_fields: int):
        self.boundary = b"--" + boundary
        self.marker = b"\r\n" + self.boundary
        self.max_files = max_files
        self.max_fields = max_fields
        self.files = 0
        self.fields = 0
        self.first = True
        self.state = "boundary"
        self.buffer = bytearray()
        self.current_part_is_file = False
        self.current_field_bytes = 0
        self.exceeded = False

    def feed(self, chunk: bytes) -> bool:
        """Return false as soon as a part-count/header limit is crossed."""
        if self.exceeded:
            return False
        self.buffer.extend(chunk)
        while True:
            if self.state == "done":
                return True
            if self.state == "boundary":
                marker = self.boundary
                index = self.buffer.find(marker)
                if index < 0:
                    keep = len(marker) + 2
                    if len(self.buffer) > keep:
                        del self.buffer[:-keep]
                    return True
                if index:
                    del self.buffer[:index]
                if len(self.buffer) < len(marker) + 2:
                    return True
                suffix_at = len(marker)
                suffix = bytes(self.buffer[suffix_at:suffix_at + 2])
                if suffix == b"--":
                    self.state = "done"
                    self.buffer.clear()
                    return True
                if suffix != b"\r\n":
                    del self.buffer[0]
                    continue
                del self.buffer[:suffix_at + 2]
                self.state = "headers"
                self.first = False
                continue
            if self.state == "headers":
                end = self.buffer.find(b"\r\n\r\n")
                if end < 0:
                    if len(self.buffer) > self.MAX_HEADER_BYTES:
                        self.exceeded = True
                        return False
                    return True
                header_block = bytes(self.buffer[:end])
                del self.buffer[:end + 4]
                disposition = next((line for line in header_block.split(b"\r\n")
                                    if line.lower().startswith(b"content-disposition:")), b"")
                self.current_part_is_file = bool(
                    re.search(rb"(?:^|;)\s*filename\*?\s*=", disposition, re.I)
                )
                if self.current_part_is_file:
                    self.files += 1
                    over_limit = self.files > self.max_files
                else:
                    self.fields += 1
                    over_limit = self.fields > self.max_fields
                self.current_field_bytes = 0
                if over_limit:
                    self.exceeded = True
                    return False
                self.state = "body"
                continue
            if self.state == "body":
                index = self.buffer.find(self.marker)
                if index < 0:
                    keep = len(self.marker) + 2
                    flushed = max(0, len(self.buffer) - keep)
                    if not self.current_part_is_file:
                        self.current_field_bytes += flushed
                        if self.current_field_bytes > self.MAX_FIELD_BYTES:
                            self.exceeded = True
                            return False
                    if flushed:
                        del self.buffer[:flushed]
                    return True
                if not self.current_part_is_file:
                    # The CRLF immediately before a boundary is framing; counting
                    # its two bytes conservatively keeps the bound fail-closed.
                    self.current_field_bytes += index + 2
                    if self.current_field_bytes > self.MAX_FIELD_BYTES + 2:
                        self.exceeded = True
                        return False
                del self.buffer[:index + 2]
                self.state = "boundary"
                continue


class _UploadBodyLimitMiddleware:
    """Bound request bytes and multipart part counts before Starlette parses them."""

    def __init__(self, app):
        self.app = app
        self.upload_slots = threading.BoundedSemaphore(_MAX_CONCURRENT_UPLOADS)
        photo = MAX_PHOTO_MB * 1024 * 1024
        video = MAX_VIDEO_MB * 1024 * 1024
        self.rules = (
            (re.compile(r"^/api/vehicle/[A-Za-z0-9_-]{1,32}/generate-3d$"), max(video, MAX_PHOTO_BATCH_MB * 1024 * 1024) + 16 * 1024 * 1024, 73, 4, True),
            (re.compile(r"^/api/upload$"), max(video, MAX_PHOTO_BATCH_MB * 1024 * 1024) + 16 * 1024 * 1024, 73, 6, True),
            (re.compile(r"^/api/(?:import/csv|dms/[A-Za-z0-9_-]{1,32}/import)$"), 12 * 1024 * 1024, 1, 0, True),
            (re.compile(r"^/api/upload/(?:exterior|interior|gallery|panorama)$"), photo + 2 * 1024 * 1024, 1, 3, True),
            (re.compile(r"^/api/(?:showroom|photo/remove-background)$"), photo + 2 * 1024 * 1024, 1, 1, True),
            (re.compile(r"^/api/hotspot$"), photo + 2 * 1024 * 1024, 1, 8, True),
            (re.compile(r"^/api/pano-hotspot$"), 2 * 1024 * 1024, 0, 6, False),
            (re.compile(r"^/api/upload/video-url$"), 2 * 1024 * 1024, 0, 3, False),
            (re.compile(r"^/api/brand/assets$"), 2 * photo + 4 * 1024 * 1024, 2, 0, True),
            (re.compile(r"^/api/brand/clear$"), 2 * 1024 * 1024, 0, 1, False),
        )

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method", "").upper() not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        # Keep normal API/form bodies small by default. Only explicit media
        # upload routes get larger bounded budgets above.
        rule = next((item for item in self.rules if item[0].fullmatch(path)), None)
        limit = rule[1] if rule else 2 * 1024 * 1024
        max_files = rule[2] if rule and len(rule) > 2 else 8
        max_fields = rule[3] if rule and len(rule) > 3 else 32

        headers = dict(scope.get("headers", []))
        declared = headers.get(b"content-length")
        if declared:
            try:
                if int(declared) > limit:
                    return await self._send_too_large(send)
            except ValueError:
                return await self._send_too_large(send, status=400, detail="Invalid Content-Length")

        upload_slot = bool(rule and len(rule) > 4 and rule[4])
        multipart_guard = None
        content_type = headers.get(b"content-type", b"")
        if content_type.lower().startswith(b"multipart/form-data"):
            try:
                from python_multipart.multipart import parse_options_header
                _, options = parse_options_header(content_type)
                boundary = options.get(b"boundary")
                if boundary and len(boundary) <= 200:
                    multipart_guard = _MultipartPartLimiter(boundary, max_files, max_fields)
            except (ImportError, ValueError, TypeError):
                multipart_guard = None  # Let Starlette return its normal malformed-form response.

        if upload_slot and not self.upload_slots.acquire(blocking=False):
            return await self._send_busy(send)

        received = 0
        rejection_detail = ""
        response_started = False
        sent_replacement = False

        async def limited_receive():
            nonlocal received, rejection_detail
            message = await receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                received += len(body)
                if received > limit:
                    rejection_detail = "Request body exceeds the upload limit"
                    return {"type": "http.disconnect"}
                if multipart_guard is not None and not multipart_guard.feed(body):
                    rejection_detail = "Multipart request exceeds its file, field, or part-header limit"
                    return {"type": "http.disconnect"}
            return message

        async def limited_send(message):
            nonlocal response_started, sent_replacement
            if rejection_detail:
                if message.get("type") == "http.response.start" and not sent_replacement and not response_started:
                    sent_replacement = True
                    response_started = True
                    await self._send_too_large(send, detail=rejection_detail)
                return
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except Exception:
            if rejection_detail:
                if not response_started and not sent_replacement:
                    await self._send_too_large(send, detail=rejection_detail)
                return
            raise
        finally:
            if upload_slot:
                self.upload_slots.release()

    @staticmethod
    async def _send_too_large(send, status: int = 413, detail: str = "Request body exceeds the upload limit"):
        payload = json.dumps({"detail": detail}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())],
        })
        await send({"type": "http.response.body", "body": payload})

    @staticmethod
    async def _send_busy(send):
        payload = json.dumps({"detail": "Upload capacity is full; retry shortly"}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 503,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
                (b"retry-after", b"30"),
            ],
        })
        await send({"type": "http.response.body", "body": payload})


app.add_middleware(_UploadBodyLimitMiddleware)


async def _read_limited_body(request: Request, limit_bytes: int) -> bytes:
    """Read request content incrementally with a strict aggregate byte limit."""
    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit_bytes:
            raise HTTPException(status_code=413, detail="Request body exceeds the size limit")
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_json_object_limited(request: Request, limit_bytes: int = 8192) -> dict:
    raw = await _read_limited_body(request, limit_bytes)
    try:
        value = json.loads(raw or b"{}")
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")
    return value


async def _read_upload_limited(upload: UploadFile, limit_bytes: int) -> bytes:
    """Read a small media upload in bounded chunks, rejecting before over-allocation."""
    chunks = []
    total = 0
    while True:
        chunk = await upload.read(min(UPLOAD_CHUNK_BYTES, limit_bytes - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > limit_bytes:
            raise HTTPException(status_code=413, detail="Uploaded file exceeds the size limit")
        chunks.append(chunk)
    if not total:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    return b"".join(chunks)


async def _save_upload_limited(upload: UploadFile, destination: Path, limit_bytes: int) -> int:
    """Stream a large upload atomically to disk; peak application memory is one chunk."""
    import uuid
    await run_in_threadpool(destination.parent.mkdir, parents=True, exist_ok=True)
    temp_path = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.upload")
    total = 0
    try:
        out = await run_in_threadpool(temp_path.open, "wb")
        try:
            while True:
                chunk = await upload.read(min(UPLOAD_CHUNK_BYTES, limit_bytes - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit_bytes:
                    raise HTTPException(status_code=413, detail="Uploaded file exceeds the size limit")
                await run_in_threadpool(out.write, chunk)
        finally:
            await run_in_threadpool(out.close)
        if not total:
            raise HTTPException(status_code=400, detail="Uploaded file is empty")
        await run_in_threadpool(os.replace, temp_path, destination)
        return total
    except Exception:
        await run_in_threadpool(_unlink_file, temp_path)
        raise


def _verify_uploaded_image(raw_bytes: bytes, filename: str) -> None:
    """Verify upload headers and dimensions without decoding into a retained image."""
    try:
        with Image.open(io.BytesIO(raw_bytes)) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_UPLOAD_IMAGE_PIXELS:
                raise HTTPException(status_code=413, detail=f"{filename} exceeds the image pixel limit")
            image.verify()
    except HTTPException:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise HTTPException(status_code=413, detail=f"{filename} exceeds the safe image size") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid image upload") from exc


def _decode_uploaded_image(
    raw_bytes: bytes,
    filename: str,
    max_size: tuple[int, int] | None = None,
) -> Image.Image:
    """Decode an upload safely, optionally downsampling before the RGB copy."""
    from PIL import ImageOps
    try:
        with Image.open(io.BytesIO(raw_bytes)) as source:
            width, height = source.size
            if width <= 0 or height <= 0 or width * height > MAX_UPLOAD_IMAGE_PIXELS:
                raise HTTPException(status_code=413,
                                    detail=f"{filename} exceeds the image pixel limit")
            if max_size is not None:
                source.draft("RGB", max_size)
                source.thumbnail(max_size, Image.Resampling.LANCZOS)
            else:
                source.load()
            oriented = ImageOps.exif_transpose(source)
            try:
                return oriented.convert("RGB")
            finally:
                if oriented is not source:
                    oriented.close()
    except HTTPException:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise HTTPException(status_code=413,
                            detail=f"{filename} exceeds the safe image size") from exc
    except Exception as exc:
        raise HTTPException(status_code=400,
                            detail=f"Cannot process {filename}: invalid image") from exc


def _decode_uploaded_rgba(raw_bytes: bytes, filename: str) -> Image.Image:
    """Decode a transparent logo within the same pixel limits as photo uploads."""
    from PIL import ImageOps
    try:
        with Image.open(io.BytesIO(raw_bytes)) as source:
            width, height = source.size
            if width <= 0 or height <= 0 or width * height > MAX_UPLOAD_IMAGE_PIXELS:
                raise HTTPException(status_code=413, detail=f"{filename} exceeds the image pixel limit")
            source.load()
            oriented = ImageOps.exif_transpose(source)
            try:
                return oriented.convert("RGBA")
            finally:
                if oriented is not source:
                    oriented.close()
    except HTTPException:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise HTTPException(status_code=413, detail=f"{filename} exceeds the safe image size") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Cannot process {filename}: invalid image") from exc


def _process_batch_photo(raw_bytes: bytes, filename: str, destination: Path) -> tuple[int, int]:
    """Decode and stage one photo without blocking the async request loop."""
    image = _decode_uploaded_image(raw_bytes, filename)
    try:
        width, height = image.size
        if width < 800 and height < 800:
            raise HTTPException(status_code=400, detail=f"{filename} resolution too small (min 800px)")
        image.save(destination, "JPEG", quality=92, optimize=True)
        return width, height
    finally:
        image.close()


def _process_3d_photo(raw_bytes: bytes, filename: str, destination: Path) -> bool:
    """Decode one 3D capture photo and save only useful, bounded images."""
    image = _decode_uploaded_image(raw_bytes, filename)
    try:
        if min(image.size) < 256:
            return False
        image.save(destination, "JPEG", quality=92, optimize=True)
        return True
    finally:
        image.close()


def _process_panorama_image(raw_bytes: bytes, filename: str, destination: Path) -> None:
    """Bound, aspect-correct and atomically save an interior panorama."""
    import uuid

    image = _decode_uploaded_image(raw_bytes, filename, max_size=(4096, 2048))
    temp_path = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        if min(image.size) < 256:
            raise HTTPException(status_code=400, detail="Panorama is too small (minimum dimension 256px)")
        image.thumbnail((4096, 2048), Image.Resampling.LANCZOS)
        width, height = image.size
        if abs((width / max(height, 1)) - 2.0) > 0.10:
            if width >= 2 * height:
                target_height = max((width + 1) // 2, 1)
                canvas = Image.new("RGB", (width, target_height), (32, 34, 38))
                canvas.paste(image, (0, (target_height - height) // 2))
            else:
                target_width = max(2 * height, 1)
                canvas = Image.new("RGB", (target_width, height), (32, 34, 38))
                canvas.paste(image, ((target_width - width) // 2, 0))
            image.close()
            image = canvas
        image.save(temp_path, "JPEG", quality=90, optimize=True)
        os.replace(temp_path, destination)
    finally:
        image.close()
        temp_path.unlink(missing_ok=True)


def _process_gallery_image(raw_bytes: bytes, filename: str, destination: Path) -> None:
    """Decode and atomically save a bandwidth-bounded gallery derivative."""
    import uuid

    image = _decode_uploaded_image(raw_bytes, filename, max_size=(2000, 2000))
    temp_path = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        image.thumbnail((2000, 2000), Image.Resampling.LANCZOS)
        image.save(temp_path, "JPEG", quality=88, optimize=True)
        os.replace(temp_path, destination)
    finally:
        image.close()
        temp_path.unlink(missing_ok=True)


def _process_brand_background(raw_bytes: bytes, filename: str, destination: Path) -> None:
    """Normalize a platform showroom backdrop off-loop and replace it atomically."""
    import uuid

    image = _decode_uploaded_image(raw_bytes, filename, max_size=(6000, 4000))
    temp_path = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        image.thumbnail((6000, 4000), Image.Resampling.LANCZOS)
        image.save(temp_path, "JPEG", quality=90, optimize=True)
        os.replace(temp_path, destination)
    finally:
        image.close()
        temp_path.unlink(missing_ok=True)


def _process_brand_logo(raw_bytes: bytes, filename: str, destination: Path) -> None:
    """Normalize a transparent dealer logo and replace it atomically."""
    import uuid

    image = _decode_uploaded_rgba(raw_bytes, filename)
    temp_path = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        image.thumbnail((2400, 1600), Image.Resampling.LANCZOS)
        image.save(temp_path, "PNG", optimize=True)
        os.replace(temp_path, destination)
    finally:
        image.close()
        temp_path.unlink(missing_ok=True)


def _replace_directory(source: Path, destination: Path) -> None:
    """Replace a directory with rollback protection on the same filesystem."""
    backup = destination.with_name(f".{destination.name}.backup-{uuid.uuid4().hex}")
    moved_old = False
    try:
        if destination.exists():
            os.replace(destination, backup)
            moved_old = True
        os.replace(source, destination)
    except Exception:
        if moved_old and backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise
    else:
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Atomically write a bounded internal JSON manifest beside its destination."""
    if path.is_symlink():
        raise OSError("Manifest destination cannot be a symlink")
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(str(temp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


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


def extract_frames(
    video_path: Path,
    output_dir: Path,
    num_frames: int = 36,
    clean_source_dir: Path | None = None,
):
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
    frames = frames[:num_frames]

    # Retain clean source frames before background removal or watermarking. Paid
    # unlocks can then rebuild the clean spin instead of trying to erase pixels
    # already baked into a processed JPEG.
    if clean_source_dir is not None:
        import tempfile
        if clean_source_dir.is_symlink():
            raise ValueError("Clean source directory cannot be a symlink")
        clean_source_dir.parent.mkdir(parents=True, exist_ok=True)
        source_stage = Path(tempfile.mkdtemp(prefix=".source-stage-", dir=clean_source_dir.parent))
        try:
            for frame in frames:
                shutil.copyfile(frame, source_stage / frame.name)
            _replace_directory(source_stage, clean_source_dir)
        finally:
            if source_stage.exists():
                shutil.rmtree(source_stage, ignore_errors=True)

    # Keep extracted frames as clean, normalized source material. Background
    # rendering now runs exactly once on the final staged spin after ordering and
    # interpolation, avoiding double watermarking/cut-out and partial writes.
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
                                   bg_color: tuple = (26, 26, 26),
                                   render_mode: str | None = None) -> dict:
    """Render one staged 360 set and return measured outcome metadata.

    ``studio`` performs rembg cut-out and local compositing. If rembg is absent,
    it deliberately falls back to ``natural`` normalization and reports
    ``degraded`` instead of silently leaving the input unchanged.
    """
    frames = sorted(list(frames_dir.glob("frame_*.jpg")) + list(frames_dir.glob("spin_*.jpg")))
    if not frames:
        return {"status": "error", "mode": "none", "processed": 0,
                "failed": 0, "warning": "No frames were available to render."}

    requested_mode = (render_mode or os.environ.get("LOTVUE_RENDER_MODE", "studio")).lower()
    if requested_mode not in {"natural", "studio"}:
        raise ValueError("LOTVUE_RENDER_MODE must be natural or studio")
    fallback_reason = ""
    actual_mode = requested_mode
    if requested_mode == "studio" and not REMBG_AVAILABLE:
        actual_mode = "natural"
        fallback_reason = "Background removal is not installed; the photographed backgrounds were retained."
    watermark_on = (watermark_mode == "preview") and FEATURES_ENABLED.get("watermark", False)

    # ── NATURAL mode: keep the real background; clean + normalize only ──────────
    if actual_mode == "natural":
        from PIL import ImageOps, ImageFilter
        with Image.open(frames[0]) as source:
            first = ImageOps.exif_transpose(source).convert("RGB")
        fw, fh = first.size
        first.close()
        scale = min(1.0, 1600 / max(fw, fh))
        TW, TH = max(2, int(fw * scale) // 2 * 2), max(2, int(fh * scale) // 2 * 2)
        completed = 0
        for frame_path in frames:
            im = fitted = sharpened = final = None
            try:
                with Image.open(frame_path) as source:
                    im = ImageOps.exif_transpose(source).convert("RGB")
                fitted = ImageOps.fit(im, (TW, TH), Image.Resampling.LANCZOS,
                                      centering=(0.5, 0.5))
                sharpened = fitted.filter(
                    ImageFilter.UnsharpMask(radius=1.4, percent=85, threshold=2)
                )
                final = (apply_diagonal_watermark(sharpened, "LOTVUE PRO PREVIEW", 60)
                         if watermark_on else sharpened.copy())
                final.save(frame_path, "JPEG", quality=94)
                completed += 1
            except Exception as exc:
                print(f"[Render] natural frame failed {frame_path.name}: {type(exc).__name__}")
            finally:
                for image in (final, sharpened, fitted, im):
                    if image is not None:
                        image.close()
        print(f"[Render] natural mode: kept backgrounds, normalized {completed}/{len(frames)} frames to {TW}x{TH}.")
        failed = len(frames) - completed
        status = "error" if completed == 0 else (
            "degraded" if fallback_reason or failed else "completed"
        )
        return {"status": status, "mode": "natural", "requested_mode": requested_mode,
                "processed": completed, "failed": failed,
                "warning": fallback_reason or ("Some frames could not be normalized." if failed else "")}

    # ── STUDIO mode: rembg cutout + composite (needs rembg) ─────────────────────

    CANVAS_W, CANVAS_H = 1920, 1080
    from PIL import ImageOps
    import statistics
    import tempfile

    # Downsample a user-provided backdrop before retaining it. The source may be
    # a camera-resolution image; only the 1920x1080 compositor needs to persist.
    bg_base = None
    if background_path and Path(background_path).exists():
        try:
            with Image.open(background_path) as source:
                source.draft("RGB", (CANVAS_W, CANVAS_H))
                bg_small = source.convert("RGB")
            bg_small.thumbnail((CANVAS_W, CANVAS_H), Image.LANCZOS)
            bg_base = ImageOps.fit(bg_small, (CANVAS_W, CANVAS_H), Image.LANCZOS)
            bg_small.close()
        except Exception as e:
            print(f"[BG Removal] Could not load background image: {e}")

    # Cutouts are persisted as bounded-size temporary PNGs. Keep only the
    # current frame in memory while finding a global median crop/scale, then
    # reload and composite each frame in a second pass.
    print(f"[BG Removal] Processing {len(frames)} frames...")
    cutouts = []
    completed = 0
    try:
        with tempfile.TemporaryDirectory(prefix=".cutout-", dir=str(frames_dir)) as temp_name:
            temp_dir = Path(temp_name)
            for frame_path in frames:
                try:
                    raw = frame_path.read_bytes()
                    result_bytes = remove_bg(raw)
                    with Image.open(io.BytesIO(result_bytes)) as source:
                        car = source.convert("RGBA")
                    car.thumbnail((CANVAS_W, CANVAS_H), Image.LANCZOS)
                    bbox = car.getbbox() or (0, 0, car.width, car.height)
                    cutout_path = temp_dir / f"cutout_{len(cutouts):04d}.png"
                    car.save(cutout_path, "PNG", compress_level=1)
                    car.close()
                    del raw, result_bytes
                    cutouts.append((frame_path, cutout_path, bbox))
                except Exception as e:
                    print(f"[BG Removal] Failed on {frame_path.name}: {e}")

            if not cutouts:
                return {"status": "error", "mode": "studio", "processed": 0,
                        "failed": len(frames), "warning": "Background segmentation failed for every frame."}

            widths = [bbox[2] - bbox[0] for _, _, bbox in cutouts]
            heights = [bbox[3] - bbox[1] for _, _, bbox in cutouts]
            median_w = statistics.median(widths)
            median_h = statistics.median(heights)
            target_w = int(CANVAS_W * 0.85)
            scale = min(target_w / max(median_w, 1),
                        (CANVAS_H * 0.80) / max(median_h, 1))

            for frame_path, cutout_path, bbox in cutouts:
                try:
                    with Image.open(cutout_path) as source:
                        car = source.convert("RGBA")
                    cropped = car.crop(bbox)
                    car.close()
                    new_size = (max(1, int(cropped.width * scale)),
                                max(1, int(cropped.height * scale)))
                    resized_car = cropped.resize(new_size, Image.LANCZOS)
                    cropped.close()
                    x = (CANVAS_W - resized_car.width) // 2
                    y = int(CANVAS_H * 0.08)

                    bg = bg_base.copy() if bg_base is not None else Image.new(
                        "RGB", (CANVAS_W, CANVAS_H), bg_color)
                    bg_shadowed = composite_turntable(bg, resized_car, x, y, turntable_style)
                    bg.close()
                    bg_rgba = bg_shadowed.convert("RGBA")
                    bg_shadowed.close()
                    bg_rgba.paste(resized_car, (x, y), resized_car)
                    final = bg_rgba.convert("RGB")
                    bg_rgba.close()
                    resized_car.close()

                    if watermark_on:
                        watermarked = apply_diagonal_watermark(final, "LOTVUE PRO PREVIEW", 60)
                        final.close()
                        final = watermarked
                    final.save(frame_path, "JPEG", quality=92)
                    final.close()
                    completed += 1
                except Exception as e:
                    print(f"[BG Removal] Composite failed for {frame_path.name}: {e}")
    finally:
        if bg_base is not None:
            bg_base.close()

    failed = len(frames) - completed
    status = "error" if completed == 0 else ("degraded" if failed else "completed")
    print(f"[BG Removal] Done. {completed}/{len(frames)} frames processed.")
    return {"status": status, "mode": "studio", "requested_mode": requested_mode,
            "processed": completed, "failed": failed,
            "warning": "Some frame cut-outs or composites failed." if failed else ""}


def _optical_flow_context(img_a: np.ndarray, img_b: np.ndarray):
    """Compute pair-level flow/grid once; reuse it for every intermediate t."""
    gray_a = cv2.cvtColor(img_a, cv2.COLOR_BGR2GRAY)
    gray_b = cv2.cvtColor(img_b, cv2.COLOR_BGR2GRAY)
    flow = cv2.calcOpticalFlowFarneback(
        gray_a, gray_b, None, pyr_scale=0.5, levels=3, winsize=15,
        iterations=3, poly_n=5, poly_sigma=1.2, flags=0)
    h, w = flow.shape[:2]
    grid_x, grid_y = np.meshgrid(np.arange(w, dtype=np.float32),
                                 np.arange(h, dtype=np.float32))
    return flow, grid_x, grid_y


def interpolate_optical_flow(img_a: np.ndarray, img_b: np.ndarray, t: float,
                              flow_context=None) -> np.ndarray:
    """Interpolate between two same-sized BGR images at t in [0, 1]."""
    if img_a.shape != img_b.shape:
        raise ValueError("optical-flow frames must have matching dimensions")
    flow, grid_x, grid_y = flow_context or _optical_flow_context(img_a, img_b)
    flow_t = flow * t
    map_x = grid_x + flow_t[..., 0]
    map_y = grid_y + flow_t[..., 1]
    flow_inv_t = -flow * (1.0 - t)
    map_x_inv = grid_x + flow_inv_t[..., 0]
    map_y_inv = grid_y + flow_inv_t[..., 1]
    warped_a = cv2.remap(img_a, map_x, map_y, cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_REFLECT_101)
    warped_b = cv2.remap(img_b, map_x_inv, map_y_inv, cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_REFLECT_101)
    return cv2.addWeighted(warped_a, 1.0 - t, warped_b, t, 0)


def interpolate_frames(input_dir: Path, output_dir: Path, target_frames: int = 36,
                       manifest: dict | None = None, use_optical_flow: bool = True):
    """Create a bounded-memory, fixed-size 360 spin from ordered JPEG frames."""
    try:
        target_frames = int(target_frames)
    except (TypeError, ValueError) as exc:
        raise ValueError("target_frames must be an integer") from exc
    if not 1 <= target_frames <= 120:
        raise ValueError("target_frames must be between 1 and 120")

    output_dir.mkdir(parents=True, exist_ok=True)
    for old in output_dir.glob("spin_*.jpg"):
        old.unlink()

    candidates = sorted(input_dir.glob("frame_*.jpg"))
    if not candidates:
        return 0

    # Verify only headers first, then decode at most two resized frames at once.
    # Dealer uploads can be 40 MP; a 1600 px long edge is already ample for a
    # web spin and cuts interpolation scratch memory by an order of magnitude.
    valid_paths = []
    source_sizes = {}
    for path in candidates:
        try:
            with Image.open(path) as source:
                width, height = source.size
                source.verify()
            if width > 0 and height > 0:
                valid_paths.append(path)
                source_sizes[path] = (width, height)
        except Exception as exc:
            print(f"[Interpolate] Skipping unreadable frame {path.name}: {exc}")

    n = len(valid_paths)
    if not n:
        return 0
    first_width, first_height = source_sizes[valid_paths[0]]
    scale = min(1.0, 1600 / max(first_width, first_height))
    target_size = (max(2, int(round(first_width * scale))),
                   max(2, int(round(first_height * scale))))

    def read_frame(path):
        width, height = source_sizes[path]
        ratio = max(width / target_size[0], height / target_size[1])
        reduction = 8 if ratio >= 8 else 4 if ratio >= 4 else 2 if ratio >= 2 else 1
        flags = {1: cv2.IMREAD_COLOR, 2: cv2.IMREAD_REDUCED_COLOR_2,
                 4: cv2.IMREAD_REDUCED_COLOR_4, 8: cv2.IMREAD_REDUCED_COLOR_8}[reduction]
        image = cv2.imread(str(path), flags)
        if image is None:
            return None
        if (image.shape[1], image.shape[0]) != target_size:
            image = cv2.resize(image, target_size, interpolation=cv2.INTER_AREA)
        return image

    # If there are already enough frames, normalize and copy only the requested
    # number without allocating an in-memory list of full-resolution images.
    if n >= target_frames:
        output_idx = 0
        selected_paths = [valid_paths[round(i * (n - 1) / max(target_frames - 1, 1))]
                          for i in range(target_frames)]
        for path in selected_paths:
            image = read_frame(path)
            if image is None:
                continue
            output_idx += 1
            out_path = output_dir / f"spin_{output_idx:04d}.jpg"
            if not cv2.imwrite(str(out_path), image, [cv2.IMWRITE_JPEG_QUALITY, 92]):
                raise OSError(f"Could not write interpolated frame {out_path}")
        return output_idx

    # Respect nonuniform measured capture angles only when the manifest exactly
    # matches the successfully decoded frame set; otherwise distribute evenly.
    gap_frames = None
    gaps = manifest.get("gaps") if isinstance(manifest, dict) else None
    if isinstance(gaps, list) and len(gaps) == n:
        try:
            degrees = [float(g.get("degrees", 0)) for g in gaps]
            total_degrees = sum(degrees)
            if all(np.isfinite(value) and value > 0 for value in degrees) and total_degrees > 0:
                gap_frames = [max(1, round(target_frames * value / total_degrees))
                              for value in degrees]
                while sum(gap_frames) < target_frames:
                    gap_frames[gap_frames.index(min(gap_frames))] += 1
                while sum(gap_frames) > target_frames:
                    reducible = [i for i, value in enumerate(gap_frames) if value > 1]
                    if not reducible:
                        break
                    index = max(reducible, key=gap_frames.__getitem__)
                    gap_frames[index] -= 1
        except (TypeError, ValueError, AttributeError):
            gap_frames = None
    if gap_frames is None:
        base, extra = divmod(target_frames, n)
        gap_frames = [base + (1 if i < extra else 0) for i in range(n)]

    first_frame = read_frame(valid_paths[0])
    if first_frame is None:
        return 0
    current = first_frame
    output_idx = 1
    for i in range(n):
        if i == n - 1:
            following = first_frame
        else:
            following = read_frame(valid_paths[i + 1])
            if following is None:
                print(f"[Interpolate] Skipping unreadable frame {valid_paths[i + 1].name}")
                continue
        count = gap_frames[i]
        flow_context = None
        if use_optical_flow and count > 1:
            try:
                flow_context = _optical_flow_context(current, following)
            except Exception as exc:
                print(f"[Interpolate] Optical flow unavailable for gap {i}: {exc}")

        for j in range(count):
            t = j / count
            if flow_context is not None and 0 < t < 1:
                try:
                    blended = interpolate_optical_flow(current, following, t, flow_context)
                except Exception as exc:
                    print(f"[Interpolate] Flow warp failed for gap {i}: {exc}")
                    blended = cv2.addWeighted(current, 1.0 - t, following, t, 0)
            else:
                blended = cv2.addWeighted(current, 1.0 - t, following, t, 0)
            out_path = output_dir / f"spin_{output_idx:04d}.jpg"
            if not cv2.imwrite(str(out_path), blended, [cv2.IMWRITE_JPEG_QUALITY, 92]):
                raise OSError(f"Could not write interpolated frame {out_path}")
            output_idx += 1
            del blended

        if i < n - 1:
            current = following
        else:
            del current, following

    del first_frame
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
    """Generate a bounded thumbnail from the first regular exterior/spin frame."""
    frames = sorted(
        (path for path in source_dir.glob("frame_*.jpg")
         if path.is_file() and not path.is_symlink()),
        key=lambda path: path.name,
    )
    if not frames:
        frames = sorted(
            (path for path in source_dir.glob("*.jpg")
             if path.is_file() and not path.is_symlink()),
            key=lambda path: path.name,
        )
    if not frames:
        return False
    thumbnail = None
    try:
        with Image.open(frames[0]) as source:
            thumbnail = source.convert("RGB")
        thumbnail.thumbnail(size, Image.Resampling.LANCZOS)
        thumbnail.save(thumb_path, "JPEG", quality=75)
        return True
    except Exception:
        return False
    finally:
        if thumbnail is not None:
            thumbnail.close()


def _bounded_frame_count(directory: Path, prefix: str, cap: int = 120) -> int:
    """Count matching regular frame names while retaining at most cap+1 names."""
    try:
        with os.scandir(directory) as entries:
            names = heapq.nsmallest(
                cap + 1,
                (entry.name for entry in entries
                 if not entry.is_symlink()
                 and entry.is_file(follow_symlinks=False)
                 and entry.name.startswith(prefix)
                 and entry.name.endswith(".jpg")
                 and re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_-]{1,80}\.jpg", entry.name)),
            )
        return min(len(names), cap)
    except OSError:
        return 0


def _vehicle_background_path(background_slug: str) -> str | None:
    """Resolve a selected packaged backdrop without trusting its database URL."""
    slug = (background_slug or "").strip()
    if not slug or slug == "none":
        return None
    with closing(sqlite3.connect(str(DB_PATH), timeout=5)) as conn:
        row = conn.execute(
            "SELECT 1 FROM backgrounds WHERE slug=? AND is_active=1 AND is_system=1",
            (slug,),
        ).fetchone()
    if not row:
        return None
    asset = background_asset_path(slug, STATIC_BACKGROUND_DIR)
    return str(asset) if asset is not None else None


def _run_vehicle_spin_job(vin: str, manifest: dict | None = None) -> None:
    """Build a full staged spin, render it once, and atomically publish the set."""
    vin = validate_vehicle_id(vin)
    stage_dir = None
    staged_thumbnail = None
    try:
        import tempfile

        vehicle_dir = _safe_vehicle_directory(vin)
        exterior_dir = _ensure_safe_vehicle_subdir(vin, "exterior")
        spin_dir = _ensure_safe_vehicle_subdir(vin, "spin", create=True)
        source_count = _bounded_frame_count(exterior_dir, "frame_", 72)
        _write_pipeline_job(
            vin, "spin", "running", step="interpolate", progress=5,
            total=NUM_EXTERIOR_FRAMES,
        )
        stage_dir = Path(tempfile.mkdtemp(prefix=".spin-stage-", dir=vehicle_dir))
        interpolated = interpolate_frames(
            exterior_dir, stage_dir, target_frames=NUM_EXTERIOR_FRAMES,
            manifest=manifest, use_optical_flow=True,
        )
        if interpolated <= 0:
            raise ValueError("No valid exterior frames could be interpolated")
        _write_pipeline_job(
            vin, "spin", "running", step="render", progress=65,
            total=NUM_EXTERIOR_FRAMES,
        )

        vehicle = db_get_vehicle(vin) or {}
        watermark_mode = vehicle.get("watermark_mode", "preview") or "preview"
        turntable_style = vehicle.get("turntable_style", "studio_shadow") or "studio_shadow"
        background_slug = vehicle.get("background_id", "") or ""
        selected_render_mode = os.environ.get("LOTVUE_RENDER_MODE", "studio").lower()
        if background_slug == "none":
            selected_render_mode = "natural"
        render = process_all_frames_background(
            stage_dir,
            watermark_mode,
            _vehicle_background_path(background_slug),
            turntable_style,
            render_mode=selected_render_mode,
        )
        if not isinstance(render, dict) or render.get("status") == "error":
            raise RuntimeError("360 frame rendering failed")
        spin_count = _bounded_frame_count(stage_dir, "spin_", 120)
        if spin_count <= 0:
            raise RuntimeError("360 render produced no viewer frames")

        spin_manifest = {
            "count": spin_count,
            "source_frames": source_count,
            "engine": "opencv-frame-interpolation",
            "render_mode": render.get("mode", "unknown"),
            "render_status": render.get("status", "unknown"),
            "background_removed": bool(render.get("mode") == "studio"),
        }
        (stage_dir / "manifest.json").write_text(
            json.dumps(spin_manifest, separators=(",", ":")), encoding="utf-8"
        )

        staged_thumbnail = vehicle_dir / f".thumbnail-{uuid.uuid4().hex}.jpg"
        thumbnail_ok = generate_thumbnail(stage_dir, staged_thumbnail)
        if not thumbnail_ok:
            staged_thumbnail.unlink(missing_ok=True)
            staged_thumbnail = None

        _replace_directory(stage_dir, spin_dir)
        stage_dir = None
        if staged_thumbnail is not None:
            os.replace(staged_thumbnail, vehicle_dir / "thumbnail.jpg")
            staged_thumbnail = None
        db_update_counts(vin)

        warnings = []
        if render.get("warning"):
            warnings.append(str(render["warning"]))
        if not thumbnail_ok:
            warnings.append("The 360 spin is ready, but its thumbnail could not be updated.")
        final_status = "degraded" if render.get("status") == "degraded" or not thumbnail_ok else "completed"
        message = " ".join(warnings) if warnings else "360 spin ready for review."
        _write_pipeline_job(
            vin, "spin", final_status, step="ready", progress=100,
            total=NUM_EXTERIOR_FRAMES, succeeded=spin_count,
            degraded=1 if final_status == "degraded" else 0,
            message=message,
        )
    except Exception as exc:
        print(f"[Finalize] spin build failed for {vin}: {type(exc).__name__}: {exc}")
        try:
            _write_pipeline_job(
                vin, "spin", "error", step="failed", progress=0,
                message="The 360 build failed. Existing viewer output, if any, was preserved; review the capture and retry.",
                failed=1,
            )
        except sqlite3.Error:
            pass
    finally:
        if stage_dir is not None and stage_dir.exists():
            shutil.rmtree(stage_dir, ignore_errors=True)
        if staged_thumbnail is not None:
            staged_thumbnail.unlink(missing_ok=True)


def _queue_vehicle_spin_job(vin: str, manifest: dict | None = None) -> str:
    """Submit the canonical spin renderer and persist queue/busy outcomes."""
    vin = validate_vehicle_id(vin)
    key = f"finalize:{vin}"
    if _background_job_running(key):
        return "already_running"
    _write_pipeline_job(
        vin, "spin", "queued", step="queued", progress=0,
        total=NUM_EXTERIOR_FRAMES, message="360 media build is queued.",
    )
    submission = _submit_background_job(key, _run_vehicle_spin_job, vin, manifest)
    if submission == "busy":
        _write_pipeline_job(
            vin, "spin", "error", step="queued", progress=0,
            total=NUM_EXTERIOR_FRAMES,
            message="Background processing capacity is full; retry in a moment.",
        )
    return submission


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
def dashboard(request: Request):
    actor_email = _authenticated_user_email(request)
    owner_email = os.getenv("LOTVUE_OWNER_EMAIL", "").strip().lower()
    vehicles = db_get_vehicles() if owner_email and actor_email == owner_email else db_get_vehicles_by_email(actor_email)

    # Scope storage accounting to this tenant and cache the recursive stat scan.
    import time as _time
    cache_key = "__owner__" if owner_email and actor_email == owner_email else actor_email
    now = _time.monotonic()
    with _DASHBOARD_STORAGE_LOCK:
        cached = _DASHBOARD_STORAGE_CACHE.get(cache_key)
    if cached and now - cached[0] < 30:
        total_bytes = cached[1]
    else:
        total_bytes = 0
        for vehicle in vehicles:
            vehicle_dir = STORAGE_DIR / str(vehicle.get("vin", ""))
            try:
                for root, dirs, files in os.walk(vehicle_dir, followlinks=False):
                    dirs[:] = [name for name in dirs if not name.startswith(".")]
                    for filename in files:
                        try:
                            total_bytes += (Path(root) / filename).stat().st_size
                        except OSError:
                            continue
            except OSError:
                continue
        with _DASHBOARD_STORAGE_LOCK:
            _DASHBOARD_STORAGE_CACHE[cache_key] = (now, total_bytes)
    storage_gb = round(total_bytes / (1024 ** 3), 2)

    # Derive displayed states from bounded metadata plus the viewer frame set.
    def get_status(v):
        ws = v.get("workflow_status", "")
        if ws in {"processing", "review", "published"}:
            return ws
        if int(v.get("exterior_frames", 0) or 0) >= 8:
            return "review"
        if int(v.get("exterior_frames", 0) or 0) > 0:
            return "photos"
        return "intake"

    total_vehicles = len(vehicles)
    total_360 = sum(
        1 for vehicle in vehicles
        if len(list((STORAGE_DIR / str(vehicle.get("vin", "")) / "spin").glob("spin_*.jpg"))) >= NUM_EXTERIOR_FRAMES
    )

    # Build vehicle cards
    cards_html = ""
    for v in vehicles:
        try:
            vin = validate_vehicle_id(str(v.get("vin", "")))
        except HTTPException:
            continue
        safe_year = html_lib.escape(str(v.get("year", "") or ""), quote=True)
        raw_make = str(v.get("make", "") or "")
        raw_model = str(v.get("model", "") or "")
        safe_make = html_lib.escape(raw_make, quote=True)
        safe_model = html_lib.escape(raw_model, quote=True)
        make_query = html_lib.escape(raw_make.lower(), quote=True)
        model_query = html_lib.escape(raw_model.lower(), quote=True)
        thumb_path = STORAGE_DIR / vin / "thumbnail.jpg"
        thumb_src = f"/storage/{vin}/thumbnail.jpg" if thumb_path.is_file() else ""
        thumb_html = (
            f'<img src="{thumb_src}" class="card-thumb" alt="Vehicle preview">'
            if thumb_src
            else '<div class="card-thumb no-thumb">No Preview</div>'
        )
        price = html_lib.escape(str(v.get("price", "") or ""), quote=True)
        mileage = html_lib.escape(str(v.get("mileage", "") or ""), quote=True)
        color = html_lib.escape(str(v.get("color", "") or ""), quote=True)
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
        year_make_model = f"{safe_year} {safe_make} {safe_model}".strip() or vin
        meta_parts = []
        if price: meta_parts.append(f'<span style="color:#3fb950;font-weight:700;">{price}</span>')
        if mileage: meta_parts.append(f'<span>{mileage} mi</span>')
        if color: meta_parts.append(f'<span>{color}</span>')
        meta_html = ' &middot; '.join(meta_parts) if meta_parts else ''
        cards_html += f"""
        <div class="vcard" data-vin="{vin}" data-make="{make_query}" data-model="{model_query}" data-year="{safe_year}" data-status="{status}">
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
            <a href="/studio">Dealer Studio</a>
            <a href="/settings">Settings</a>
            <a href="/health">System</a>
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
    <script src="/static/lv_dashboard_tools.js?v=20260705a" defer></script>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/dashboard")
def dashboard_alias(request: Request):
    return dashboard(request)


# =============================================================================
# ROUTES — CAPTURE GUIDE
# =============================================================================
@app.get("/capture-guide")
def capture_guide():
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
def capture_page():
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

    function showGatePanel(gate) {{
      let el = document.getElementById('gatePanel');
      if (!el) {{
        el = document.createElement('div');
        el.id = 'gatePanel';
        el.style.cssText = 'margin:12px 0;padding:12px 14px;border:1px solid #e6a23c;background:#fdf6ec;color:#7a4d00;border-radius:8px;font-size:14px;text-align:left';
        const btn = document.getElementById('submitBtn');
        btn.parentNode.insertBefore(el, btn);
      }}
      const reasons = (gate && gate.reasons) || [];
      el.innerHTML = '<b>Capture quality check failed:</b><ul style="margin:6px 0 8px;padding-left:18px">' +
        reasons.map(x => '<li>' + String(x).replace(/</g,'&lt;') + '</li>').join('') +
        '</ul>Fix the capture and retry, or <button type="button" id="gateForceBtn" style="margin-left:4px">Upload anyway</button>';
      document.getElementById('gateForceBtn').onclick = () => {{ el.remove(); startProcess(true); }};
    }}

    async function gateAwareUpload(fd, force) {{
      if (force) fd.append('force', '1');
      const r = await fetch('/api/upload', {{ method: 'POST', body: fd }});
      let d = null;
      try {{ d = await r.json(); }} catch(e) {{ d = {{}}; }}
      if (r.status === 422 && d.detail && d.detail.gate) {{
        showGatePanel(d.detail.gate);
        return null;
      }}
      if (!r.ok) {{
        const det = d.detail;
        throw new Error(typeof det === 'string' ? det : (det && JSON.stringify(det)) || 'Upload failed');
      }}
      return d;
    }}

    async function startProcess(force) {{
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
        let d = null;

        if (hasPhotos) {{
          setProgress(10, 'Uploading photos...');
          const fd = new FormData();
          photoFiles.forEach(f => fd.append('photos', f));
          d = await gateAwareUpload(fd, force);
        }} else {{
          setProgress(10, 'Uploading video...');
          const fd = new FormData();
          fd.append('video', videoFile);
          d = await gateAwareUpload(fd, force);
        }}
        if (!d) {{ setProgress(0, 'Capture rejected — see reasons above'); btn.disabled = false; return; }}
        jobId = d.job_id; vin = d.vin;

        setProgress(40, 'Ordering photos...');
        const r1 = await fetch('/api/process/' + jobId + '/order', {{ method: 'POST' }});
        if (!r1.ok) throw new Error('Ordering failed');

        setProgress(65, 'Queueing the shared 360 renderer...');
        const r2 = await fetch('/api/process/' + jobId + '/interpolate', {{ method: 'POST', credentials: 'same-origin' }});
        const r2Data = await r2.json().catch(() => ({{}}));
        if (!r2.ok) throw new Error((r2Data && r2Data.detail) || 'Could not start the 360 renderer');

        setProgress(70, 'Rendering the 360 viewer...');
        let finished = false;
        for (let attempt = 0; attempt < 360; attempt++) {{
          await new Promise(resolve => setTimeout(resolve, 1500));
          const statusResponse = await fetch('/api/finalize-status/' + encodeURIComponent(vin), {{ credentials: 'same-origin' }});
          const statusData = await statusResponse.json().catch(() => ({{}}));
          if (!statusResponse.ok) throw new Error(statusData.detail || 'Could not read 360 job status');
          const pct = Math.min(98, statusData.progress || (statusData.spin_frames ? Math.round(statusData.spin_frames / 36 * 90) : 70));
          setProgress(pct, statusData.message || 'Rendering 360 frames...');
          if (statusData.status === 'error') throw new Error(statusData.message || '360 rendering failed');
          if (statusData.status === 'completed' || statusData.status === 'degraded') {{
            if (!statusData.ready) throw new Error('Render finished without a viewer frame');
            finished = true;
            break;
          }}
        }}
        if (!finished) throw new Error('The 360 render is still processing. Retry from Dealer Studio.');
        setProgress(100, 'Viewer ready for review');
        setTimeout(() => window.location.href = '/viewer/' + encodeURIComponent(jobId), 400);
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
def process_interpolate(job_id: str, request: Request):
    if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    vehicle = db_get_vehicle_by_job_id(job_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Job not found")
    _require_vehicle_owner(request, vehicle["vin"])

    vin = vehicle["vin"]
    exterior_dir = _ensure_safe_vehicle_subdir(vin, "exterior")
    vehicle_dir = _safe_vehicle_directory(vin)
    manifest_path = vehicle_dir / "manifest.json"

    if not exterior_dir.exists():
        raise HTTPException(status_code=400, detail="No ordered exterior frames found")

    manifest = None
    if manifest_path.is_file() and not manifest_path.is_symlink():
        try:
            if manifest_path.stat().st_size <= 1_000_000:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            manifest = None

    frame_count = _bounded_frame_count(exterior_dir, "frame_", 72)
    if frame_count < 4:
        raise HTTPException(status_code=400, detail="At least 4 ordered exterior frames are required")
    submission = _queue_vehicle_spin_job(vin, manifest)
    if submission == "busy":
        raise HTTPException(
            status_code=429, detail="Background processing capacity is full",
            headers={"Retry-After": "30"},
        )
    return {
        "status": submission,
        "job_id": job_id,
        "vin": vin,
        "original_frames": frame_count,
        "message": "The 360 build runs in the background. Poll /api/finalize-status/{vin}.",
    }


@app.post("/api/process/{job_id}/order")
def process_order(job_id: str, request: Request):
    if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    vehicle = db_get_vehicle_by_job_id(job_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Job not found")
    _require_vehicle_owner(request, vehicle["vin"])

    import tempfile

    vin = vehicle["vin"]
    vehicle_dir = _safe_vehicle_directory(vin)
    exterior_dir = _ensure_safe_vehicle_subdir(vin, "exterior", create=True)
    manifest_path = vehicle_dir / "manifest.json"
    raw_dir = vehicle_dir / "raw"
    if raw_dir.is_symlink():
        raise HTTPException(status_code=404, detail="Raw photos not found")
    raw_dir_exists = raw_dir.is_dir()
    raw_frames = sorted(raw_dir.glob("frame_*.jpg")) if raw_dir_exists else []
    pre_extracted = sorted(
        (path for path in exterior_dir.glob("frame_*.jpg")
         if path.is_file() and not path.is_symlink()),
        key=lambda path: path.name,
    )[:72]

    # Video extraction already writes clean source frames to exterior/. Do not
    # rewrite those frames; only create/update the ordering manifest here.
    if not raw_frames and pre_extracted:
        ordered_frames = [
            {"file": frame.name, "estimated_angle": round(i * 360.0 / len(pre_extracted), 2),
             "confidence": 0.1}
            for i, frame in enumerate(pre_extracted)
        ]
        manifest = build_manifest(job_id, vin, ordered_frames)
        _atomic_write_json(manifest_path, manifest)
        return {"status": "ordered", "job_id": job_id, "vin": vin,
                "confidence": 0.1, "source": "video", "frames": len(pre_extracted)}

    if not raw_frames:
        raise HTTPException(status_code=400, detail="No raw photos found")
    if len(raw_frames) < 4 or len(raw_frames) > 72:
        raise HTTPException(status_code=400, detail="A photo spin requires 4–72 raw photos")

    ordered_frames, confidence = estimate_angles_orb(raw_dir)
    if confidence < 0.25 or len(ordered_frames) < 4:
        n = len(raw_frames)
        step = 360.0 / max(n, 1)
        ordered_frames = [
            {"file": frame.name, "estimated_angle": round(i * step, 2), "confidence": 0.1}
            for i, frame in enumerate(raw_frames)
        ]
    ordered_frames = ordered_frames[:72]
    manifest = build_manifest(job_id, vin, ordered_frames)

    exterior_stage = Path(tempfile.mkdtemp(prefix=".exterior-stage-", dir=vehicle_dir))
    try:
        for index, entry in enumerate(ordered_frames):
            filename = str(entry.get("file", ""))
            if not re.fullmatch(r"frame_[A-Za-z0-9_-]{1,80}\.jpg", filename):
                raise ValueError("Frame manifest contains an invalid filename")
            source = raw_dir / filename
            if source.is_symlink() or not source.is_file() or source.resolve().parent != raw_dir:
                raise ValueError("An ordered source frame is unavailable")
            shutil.copyfile(source, exterior_stage / f"frame_{index + 1:04d}.jpg")
        _replace_directory(exterior_stage, exterior_dir)
        _atomic_write_json(manifest_path, manifest)
    finally:
        if exterior_stage.exists():
            shutil.rmtree(exterior_stage, ignore_errors=True)

    return {"status": "ordered", "job_id": job_id, "vin": vin,
            "confidence": confidence, "frames": len(ordered_frames)}


# =============================================================================
# API — CREATE VEHICLE (for photo-only mode, no video)
# =============================================================================
@app.post("/api/create-vehicle")
def create_vehicle(
    request: Request,
    vin: str = Form(...),
    year: str = Form(""),
    make: str = Form(""),
    model: str = Form(""),
    color: str = Form(""),
    price: str = Form(""),
    mileage: str = Form(""),
):
    vin = validate_vin(vin)
    user_email, _existing_vehicle = _require_vehicle_access(request, vin, allow_create=True)
    if any(len(value or "") > 120 for value in (year, make, model, color)) or len(price) > 32 or len(mileage) > 32:
        raise HTTPException(status_code=400, detail="Vehicle metadata is too long")

    vehicle_dir = STORAGE_DIR / vin
    if _existing_vehicle is None and vehicle_dir.exists():
        raise HTTPException(status_code=409, detail="Vehicle storage already exists; contact support")
    exterior_dir = vehicle_dir / "exterior"
    interior_dir = vehicle_dir / "interior"
    vehicle_dir.mkdir(parents=True, exist_ok=True)
    exterior_dir.mkdir(exist_ok=True)
    interior_dir.mkdir(exist_ok=True)

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
        user_email=user_email,
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
    request: Request,
    vin: str = Form(""),
    photo: UploadFile = File(...),
    index: str = Form("0"),
):
    # Auto-generate VIN if not provided
    if not vin or len(vin) < 17:
        import uuid
        vin = str(uuid.uuid4())[:17].upper().replace("-", "0")
    
    vin = validate_vin(vin)
    user_email, existing_vehicle = await run_in_threadpool(_require_vehicle_access, request, vin, allow_create=True)
    vehicle_dir = await run_in_threadpool(
        _prepare_vehicle_upload, vin, user_email, existing_vehicle, ("exterior", "interior")
    )
    exterior_dir = vehicle_dir / "exterior"
    
    raw = await _read_upload_limited(photo, MAX_PHOTO_MB * 1024 * 1024)
    try:
        requested_index = int(index)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid frame index") from exc
    if requested_index < 0 or requested_index >= 72:
        raise HTTPException(status_code=400, detail="Frame index must be between 0 and 71")
    # Use index for ordering — frame_0001.jpg, frame_0002.jpg, etc.
    idx = requested_index + 1
    photo_path = exterior_dir / f"frame_{idx:04d}.jpg"
    
    # Keep CPU-heavy decode/enhancement off the async event loop.
    showroom = await run_in_threadpool(get_dealer_showroom, user_email)
    await run_in_threadpool(process_exterior_photo, raw, photo_path, showroom)
    
    # Update thumbnail from first frame
    if idx == 1:
        await run_in_threadpool(generate_thumbnail, exterior_dir, vehicle_dir / "thumbnail.jpg")
    
    # Update counts
    await run_in_threadpool(db_update_counts, vin)
    
    return {"status": "ok", "vin": vin, "filename": photo_path.name, "index": idx}


# =============================================================================
# VIEWER — STATIC FRAME SERVING
# =============================================================================
def _public_job_spin_dir(job_id: str) -> tuple[str, Path]:
    """Resolve only an active vehicle's real spin directory under storage."""
    if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", job_id or ""):
        raise HTTPException(status_code=404, detail="Viewer not found")
    vehicle = db_get_vehicle_by_job_id(job_id)
    if not vehicle or vehicle.get("status") != "active":
        raise HTTPException(status_code=404, detail="Viewer not found")
    try:
        vin = validate_vehicle_id(vehicle.get("vin", ""))
        storage_root = Path(STORAGE_DIR).resolve(strict=True)
        vehicle_path = Path(STORAGE_DIR) / vin
        if vehicle_path.is_symlink():
            raise HTTPException(status_code=404, detail="Viewer not found")
        vehicle_dir = vehicle_path.resolve(strict=True)
        if (vehicle_dir.parent != storage_root or vehicle_dir.name != vin
                or not vehicle_dir.is_dir()):
            raise HTTPException(status_code=404, detail="Viewer not found")
        spin_path = vehicle_dir / "spin"
        if spin_path.is_symlink():
            raise HTTPException(status_code=404, detail="Frames not found")
        spin_dir = spin_path.resolve(strict=True)
        if (spin_dir.parent != vehicle_dir or spin_dir.name != "spin"
                or not spin_dir.is_dir()):
            raise HTTPException(status_code=404, detail="Frames not found")
        return vin, spin_dir
    except HTTPException:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Frames not found") from exc


@app.get("/static/{job_id}/frames/{filename}")
def serve_frame(job_id: str, filename: str):
    if not re.fullmatch(r"spin_[A-Za-z0-9_-]{1,80}\.jpg", filename or ""):
        raise HTTPException(status_code=404, detail="Frame not found")
    try:
        _, spin_dir = _public_job_spin_dir(job_id)
        frame_path = spin_dir / filename
        if frame_path.is_symlink():
            raise HTTPException(status_code=404, detail="Frame not found")
        frame_path = frame_path.resolve(strict=True)
    except HTTPException:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Frame not found") from exc
    if frame_path.parent != spin_dir or not frame_path.is_file():
        raise HTTPException(status_code=404, detail="Frame not found")
    return FileResponse(frame_path)


# =============================================================================
# VIEWER — INTERACTIVE 360 PAGE
# =============================================================================
@app.get("/viewer/{job_id}")
def viewer_page(job_id: str, request: Request):
    try:
        _, spin_dir = _public_job_spin_dir(job_id)
    except HTTPException as exc:
        return HTMLResponse("<h1>Viewer not found</h1>", status_code=exc.status_code)

    try:
        with os.scandir(spin_dir) as entries:
            frames = heapq.nsmallest(
                120,
                (entry.name for entry in entries
                 if not entry.is_symlink()
                 and entry.is_file(follow_symlinks=False)
                 and re.fullmatch(r"spin_[A-Za-z0-9_-]{1,80}\.jpg", entry.name)),
            )
    except OSError:
        return HTMLResponse("<h1>Frames not ready</h1>", status_code=404)
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
    request: Request,
    vin: str = Form(""),
    year: str = Form(""),
    make: str = Form(""),
    model: str = Form(""),
    color: str = Form(""),
    force: str = Form(""),  # "1" bypasses the capture quality gate (logged)
    video: Optional[UploadFile] = File(None),
    photos: Optional[List[UploadFile]] = File(None),
):
    import uuid
    import tempfile
    from validate_video import validate_dir, validate_video_file, gate_summary
    job_id = str(uuid.uuid4())

    # Auto-generate VIN if not provided
    if not vin or len(vin) < 17:
        vin = f"LV360{job_id.replace('-','')[:12].upper()}"

    vin = validate_vin(vin)
    user_email, existing_vehicle = await run_in_threadpool(_require_vehicle_access, request, vin, allow_create=True)

    vehicle_dir = await run_in_threadpool(
        _prepare_vehicle_upload,
        vin,
        user_email,
        existing_vehicle,
        ("raw", "exterior", "interior"),
        year=year,
        make=make,
        model=model,
        job_id=job_id,
    )
    raw_dir = vehicle_dir / "raw"
    exterior_dir = vehicle_dir / "exterior"

    # ── Photo batch branch ───────────────────────────────────────────────────
    if photos and len(photos) > 0:
        if len(photos) < 4:
            raise HTTPException(status_code=400, detail="Upload at least 4 photos")
        if len(photos) > 72:
            raise HTTPException(status_code=400, detail="Max 72 photos allowed")

        valid_exts = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
        stage_dir = Path(await run_in_threadpool(
            tempfile.mkdtemp, prefix=".raw-upload-", dir=vehicle_dir
        ))
        photo_count = 0
        batch_bytes = 0
        batch_limit = MAX_PHOTO_BATCH_MB * 1024 * 1024
        try:
            for i, photo in enumerate(photos):
                filename = Path(photo.filename or "upload").name
                ext = Path(filename).suffix.lower()
                if ext not in valid_exts:
                    raise HTTPException(status_code=400, detail=f"Invalid file type: {filename}")

                raw_bytes = await _read_upload_limited(photo, MAX_PHOTO_MB * 1024 * 1024)
                batch_bytes += len(raw_bytes)
                if batch_bytes > batch_limit:
                    raise HTTPException(status_code=413,
                                        detail=f"Photo batch too large (max {MAX_PHOTO_BATCH_MB}MB total)")

                save_path = stage_dir / f"frame_{i+1:04d}.jpg"
                await run_in_threadpool(_process_batch_photo, raw_bytes, filename, save_path)
                photo_count += 1

            # Validate before replacing a previous upload. Failed retries never
            # destroy the dealer's last good batch.
            gate = await run_in_threadpool(validate_dir, stage_dir)
            if gate["verdict"] == "FAIL" and force != "1":
                raise HTTPException(status_code=422, detail={
                    "error": "capture_rejected", "gate": gate_summary(gate)})
            if gate["verdict"] == "FAIL":
                print(f"[gate] FORCED past FAIL for {vin}: {gate['reasons']}")

            await run_in_threadpool(_replace_directory, stage_dir, raw_dir)
            await run_in_threadpool(db_upsert_vehicle, vin, year, make, model, photo_count, 0, user_email=user_email, job_id=job_id)
            return {"job_id": job_id, "photo_count": photo_count, "status": "uploaded",
                    "vin": vin, "gate": gate_summary(gate)}
        finally:
            await run_in_threadpool(_remove_directory_if_exists, stage_dir)

    # ── Video branch ─────────────────────────────────────────────────────────
    if not video:
        raise HTTPException(status_code=400, detail="No video or photos provided")

    allowed_video_exts = {".mp4", ".mov", ".m4v", ".webm", ".avi"}
    video_name = Path(video.filename or "").name
    if Path(video_name).suffix.lower() not in allowed_video_exts:
        raise HTTPException(status_code=400, detail="Upload an MP4, MOV, M4V, WebM, or AVI video")

    video_path = vehicle_dir / f".video-{job_id}.mp4"
    await _save_upload_limited(video, video_path, MAX_VIDEO_MB * 1024 * 1024)

    # ── P0 QUALITY GATE — runs BEFORE the expensive extract+matting pipeline ─
    try:
        gate = await run_in_threadpool(validate_video_file, video_path)
    except Exception as e:
        gate = {"verdict": "FAIL", "reasons": [f"unreadable video: {e}"],
                "frames": 0, "checks": {}}
    if gate["verdict"] == "FAIL" and force != "1":
        await run_in_threadpool(_unlink_file, video_path)
        raise HTTPException(status_code=422, detail={
            "error": "capture_rejected", "gate": gate_summary(gate)})
    if gate["verdict"] == "FAIL":
        print(f"[gate] FORCED past FAIL for {vin}: {gate['reasons']}")

    exterior_stage = Path(await run_in_threadpool(
        tempfile.mkdtemp, prefix=".video-frames-", dir=vehicle_dir
    ))
    try:
        num_extracted = await run_in_threadpool(
            extract_frames, video_path, exterior_stage, NUM_EXTERIOR_FRAMES,
            vehicle_dir / "source",
        )
        if num_extracted < 4:
            raise ValueError("Video extraction produced fewer than four usable frames")
        await run_in_threadpool(_replace_directory, exterior_stage, exterior_dir)
    except Exception as e:
        await run_in_threadpool(_unlink_file, video_path)
        raise HTTPException(status_code=500, detail="Frame extraction failed; existing media was preserved") from e
    finally:
        await run_in_threadpool(_remove_directory_if_exists, exterior_stage)

    await run_in_threadpool(_unlink_file, video_path)

    await run_in_threadpool(generate_thumbnail, exterior_dir, vehicle_dir / "thumbnail.jpg")
    await run_in_threadpool(db_upsert_vehicle, vin, year, make, model, num_extracted, 0, user_email=user_email, job_id=job_id)

    return {"job_id": job_id, "vin": vin, "frames_extracted": num_extracted,
            "status": "uploaded", "gate": gate_summary(gate)}


# =============================================================================
# API — UPLOAD INTERIOR PHOTO
# =============================================================================
@app.post("/api/upload/interior")
async def upload_interior(
    request: Request,
    vin: str = Form(""),
    photo: UploadFile = File(...)
):
    # Auto-generate VIN if not provided
    if not vin or len(vin) < 17:
        import uuid
        vin = str(uuid.uuid4())[:17].upper().replace("-", "0")
    
    vin = validate_vin(vin)
    user_email, existing_vehicle = await run_in_threadpool(_require_vehicle_access, request, vin, allow_create=True)
    vehicle_dir = await run_in_threadpool(
        _prepare_vehicle_upload, vin, user_email, existing_vehicle, ("exterior", "interior")
    )
    interior_dir = vehicle_dir / "interior"
    raw = await _read_upload_limited(photo, MAX_PHOTO_MB * 1024 * 1024)
    existing = await run_in_threadpool(_count_matching_files, interior_dir, (".jpg",))
    photo_path = interior_dir / f"interior_{existing+1:03d}.jpg"
    try:
        await run_in_threadpool(process_interior_photo, raw, photo_path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid interior image") from exc
    
    # Update counts
    await run_in_threadpool(db_update_counts, vin)
    
    return {"status": "ok", "filename": photo_path.name}


# =============================================================================
# PHASE 2A: UPLOAD ENDPOINTS — PANORAMA, GALLERY, VIDEO
# =============================================================================

@app.post("/api/upload/panorama")
async def upload_panorama(
    request: Request,
    vin: str = Form(""),
    scene_name: str = Form("Front Seat"),
    photo: UploadFile = File(...)
):
    """Upload an equirectangular interior panorama for Pannellum viewer."""
    # Accept a VIN or internal vehicle ID, but reject rather than silently rewrite it.
    vin = validate_vehicle_id(vin)
    await run_in_threadpool(_require_vehicle_owner, request, vin)
    pano_dir = await run_in_threadpool(
        _ensure_safe_vehicle_subdir, vin, "panoramas", create=True
    )

    if len(scene_name.strip()) > 100:
        raise HTTPException(status_code=400, detail="Scene name must be 100 characters or fewer")
    raw = await _read_upload_limited(photo, MAX_PHOTO_MB * 1024 * 1024)

    existing = await run_in_threadpool(
        _count_matching_files, pano_dir, (".jpg", ".jpeg", ".png")
    )
    pano_path = pano_dir / f"pano_{existing+1:03d}.jpg"

    try:
        await run_in_threadpool(
            _process_panorama_image,
            raw,
            Path(photo.filename or "panorama").name,
            pano_path,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid panorama image") from e

    await run_in_threadpool(_register_panorama_upload, vin, pano_path, scene_name, existing + 1)

    return {"status": "ok", "filename": pano_path.name, "vin": vin, "scene_name": scene_name}


@app.post("/api/upload/gallery")
async def upload_gallery_photo(
    request: Request,
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
    if category not in {"exterior", "interior", "engine", "trunk", "damage", "other"}:
        raise HTTPException(status_code=400, detail="Unsupported gallery photo category")
    if len(caption) > 255:
        raise HTTPException(status_code=400, detail="Caption must be 255 characters or fewer")
    user_email, existing_vehicle = await run_in_threadpool(_require_vehicle_access, request, vin, allow_create=True)
    vehicle_dir = await run_in_threadpool(
        _prepare_vehicle_upload, vin, user_email, existing_vehicle, ("gallery",)
    )
    gallery_dir = vehicle_dir / "gallery"

    raw = await _read_upload_limited(photo, MAX_PHOTO_MB * 1024 * 1024)
    existing = await run_in_threadpool(_count_matching_files, gallery_dir, (".jpg",))
    photo_path = gallery_dir / f"gallery_{existing+1:03d}.jpg"

    try:
        await run_in_threadpool(
            _process_gallery_image,
            raw,
            Path(photo.filename or "gallery photo").name,
            photo_path,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid gallery image") from e

    await run_in_threadpool(
        _register_gallery_upload, vin, photo_path, category, caption, existing + 1
    )

    return {"status": "ok", "filename": photo_path.name, "vin": vin, "category": category}


@app.post("/api/upload/video-url")
def set_vehicle_video_url(
    request: Request,
    vin: str = Form(""),
    video_url: str = Form(""),
    video_type: str = Form(""),
):
    """Set an allowlisted YouTube/Vimeo embed or direct HTTPS video URL."""
    if not vin:
        raise HTTPException(status_code=400, detail="VIN required")
    vin = validate_vin(vin)
    _require_vehicle_owner(request, vin)
    safe_url, safe_type = normalize_video_source(video_url, video_type, vin)
    with sqlite3.connect(str(DB_PATH), timeout=10) as conn:
        updated = conn.execute(
            "UPDATE vehicles SET video_url=?, video_type=?, show_video=? "
            "WHERE vin=? AND status='active'",
            (safe_url, safe_type, int(bool(safe_url)), vin),
        )
        if not updated.rowcount:
            raise HTTPException(status_code=404, detail="Vehicle not found")
    return {"status": "ok", "vin": vin, "video_url": safe_url, "video_type": safe_type}


# =============================================================================
# API — FINALIZE (run interpolation + background removal)
# =============================================================================
@app.post("/api/finalize/{vin}")
def finalize_vehicle(request: Request, vin: str, engine: str = ""):
    """Run the canonical, staged 360 renderer; ``engine=pro`` requests optional splats."""
    vin = validate_vehicle_id(vin)
    _require_vehicle_owner(request, vin)
    vehicle = db_get_vehicle(vin)
    if not vehicle or vehicle.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")
    vehicle_dir = _safe_vehicle_directory(vin)
    exterior_dir = _ensure_safe_vehicle_subdir(vin, "exterior")
    frame_count = _bounded_frame_count(exterior_dir, "frame_", 72)
    if frame_count < 4:
        raise HTTPException(status_code=400, detail="At least 4 exterior photos are required")

    if engine == "pro":
        if _background_job_running(f"gaussian3d:{vin}"):
            return {"status": "already_running", "vin": vin}
        _ensure_safe_vehicle_subdir(vin, "splat", create=True)
        _write_gaussian_status(vin, {
            "vin": vin, "status": "running", "step": "queued", "progress": 1,
        })
        submission = _submit_background_job(
            f"gaussian3d:{vin}", _run_gaussian_3d_job,
            vin, ["--images", str(exterior_dir)], 7000, "natural", "light",
        )
        if submission != "started":
            _write_gaussian_status(vin, {
                "status": "error", "step": "queued", "progress": 0, "vin": vin,
                "error": "Background processing capacity is full",
            })
            raise HTTPException(status_code=429, detail="Background processing capacity is full",
                                headers={"Retry-After": "30"})
        return {"status": "started", "engine": "pro", "original_frames": frame_count,
                "message": "Optional Gaussian-splat build running. Poll /api/vehicle/{vin}/3d-status."}

    manifest = None
    manifest_path = vehicle_dir / "manifest.json"
    if manifest_path.is_file() and not manifest_path.is_symlink():
        try:
            if manifest_path.stat().st_size <= 1_000_000:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            manifest = None
    submission = _queue_vehicle_spin_job(vin, manifest)
    if submission == "busy":
        raise HTTPException(status_code=429, detail="Background processing capacity is full",
                            headers={"Retry-After": "30"})
    return {
        "status": submission, "vin": vin,
        "original_frames": frame_count, "processing_bg": submission == "started",
        "status_url": f"/api/finalize-status/{vin}",
        "message": "360 media build queued. Poll the status URL before opening the viewer.",
    }


@app.get("/api/finalize-status/{vin}")
def finalize_status(vin: str, request: Request):
    """Return the latest owner-scoped spin-job state and whether frames exist."""
    vin = validate_vehicle_id(vin)
    _require_vehicle_owner(request, vin)
    vehicle = db_get_vehicle(vin)
    if not vehicle or vehicle.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")
    _safe_vehicle_directory(vin)
    try:
        spin_dir = _ensure_safe_vehicle_subdir(vin, "spin")
        frame_count = _bounded_frame_count(spin_dir, "spin_", 120)
        marker_processing = (spin_dir / ".processing").is_file()
    except HTTPException:
        frame_count = 0
        marker_processing = False
    jobs = _read_pipeline_jobs(vin)
    job = next((item for item in jobs if item["kind"] == "spin"), None)
    job_status = job["status"] if job else ("running" if marker_processing else ("completed" if frame_count else "not_started"))
    processing = job_status in {"queued", "running"} or marker_processing
    return {
        "vin": vin,
        "status": job_status,
        "step": job.get("step", "") if job else "",
        "progress": job.get("progress", 100 if frame_count else 0) if job else (100 if frame_count else 0),
        "spin_frames": frame_count,
        "processing": processing,
        "ready": frame_count > 0,
        "message": job.get("message", "") if job else "",
        "updated_at": job.get("updated_at", "") if job else "",
    }


# =============================================================================
# API — UPLOAD DEALER SHOWROOM BACKGROUND
# =============================================================================
@app.post("/api/showroom")
async def upload_showroom(
    request: Request,
    dealer_name: str = Form("default"),
    photo: UploadFile = File(...),
):
    """Upload a tenant-private showroom background."""
    email = await run_in_threadpool(_authenticated_user_email, request)
    save_path = _showroom_file(email, dealer_name)
    await run_in_threadpool(save_path.parent.mkdir, parents=True, exist_ok=True)
    raw = await _read_upload_limited(photo, MAX_PHOTO_MB * 1024 * 1024)
    await run_in_threadpool(
        _process_gallery_image, raw, Path(photo.filename or "showroom").name, save_path
    )
    return {"status": "ok", "showroom": save_path.name}


@app.get("/api/showroom")
def get_showroom(request: Request, dealer_name: str = "default"):
    """Report whether the authenticated tenant has uploaded a showroom background."""
    email = _authenticated_user_email(request)
    showroom_path = _showroom_file(email, dealer_name)
    if showroom_path.is_file():
        return {"exists": True, "showroom": showroom_path.name}
    return {"exists": False}


# =============================================================================
# API — HOTSPOTS
# =============================================================================
@app.post("/api/hotspot")
async def add_hotspot(
    request: Request,
    vin: str = Form(...),
    photo_filename: str = Form(...),
    tag: str = Form(...),
    x: str = Form("0.5"),
    y: str = Form("0.5"),
    note: str = Form(""),
    condition_type: str = Form(""),
    severity: str = Form(""),
    photo: Optional[UploadFile] = File(None),
):
    """Add a bounded hotspot to a frame for a vehicle owned by this tenant."""
    vin = validate_vehicle_id(vin)
    await run_in_threadpool(_require_vehicle_owner, request, vin)
    photo_filename = (photo_filename or "").strip()
    if (Path(photo_filename).name != photo_filename
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", photo_filename)
            or Path(photo_filename).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}):
        raise HTTPException(status_code=400, detail="Invalid photo filename")
    image_exists = False
    for subdir in ("exterior", "spin"):
        if await run_in_threadpool(_vehicle_media_file_exists, vin, subdir, photo_filename):
            image_exists = True
            break
    if not image_exists:
        raise HTTPException(status_code=404, detail="Frame not found")
    tag = "".join(ch for ch in (tag or "") if ord(ch) >= 32 and ord(ch) != 127).strip()[:80]
    note = "".join(ch for ch in (note or "") if ch in "\t\n\r" or (ord(ch) >= 32 and ord(ch) != 127)).strip()[:500]
    if not tag:
        raise HTTPException(status_code=400, detail="Tag is required")
    condition_type = (condition_type or "").strip().lower()
    severity = (severity or "").strip().lower()
    if condition_type not in {"", "scratch", "dent", "chip", "tear", "stain", "crack", "rust", "other"}:
        raise HTTPException(status_code=400, detail="Invalid condition type")
    if severity and severity not in {"minor", "moderate", "major"}:
        raise HTTPException(status_code=400, detail="Invalid severity")
    if condition_type and not severity:
        raise HTTPException(status_code=400, detail="Damage hotspot needs a severity")
    if not condition_type:
        severity = ""
    try:
        hotspot_x, hotspot_y = float(x), float(y)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Hotspot coordinates must be numbers") from exc
    if not (0.0 <= hotspot_x <= 1.0 and 0.0 <= hotspot_y <= 1.0):
        raise HTTPException(status_code=400, detail="Hotspot coordinates must be between 0 and 1")

    close_up = ""
    if photo is not None:
        raw = await _read_upload_limited(photo, MAX_PHOTO_MB * 1024 * 1024)
        hs_dir = await run_in_threadpool(
            _ensure_safe_vehicle_subdir, vin, "hotspot_photos", create=True
        )
        close_up = f"hs_{uuid.uuid4().hex[:10]}.jpg"
        await run_in_threadpool(
            _process_gallery_image,
            raw,
            Path(photo.filename or "close-up").name,
            hs_dir / close_up,
        )
    hotspot_id = await run_in_threadpool(
        _insert_hotspot, vin, photo_filename, tag, hotspot_x, hotspot_y,
        condition_type, severity, note, close_up,
    )
    return {"status": "ok", "id": hotspot_id, "close_up": close_up}


@app.get("/api/hotspots/{vin}")
def get_hotspots(vin: str):
    """Read viewer hotspots only for active inventory."""
    vin = validate_vehicle_id(vin)
    vehicle = db_get_vehicle(vin)
    if not vehicle or vehicle.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cursor = conn.execute(
        "SELECT id, photo_filename, tag, hotspot_x, hotspot_y, "
        "condition_type, severity, note, close_up FROM hotspots WHERE vin = ?",
        (vin,)
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {"id": r["id"], "photo": r["photo_filename"], "tag": r["tag"],
         "x": r["hotspot_x"], "y": r["hotspot_y"],
         "condition_type": r["condition_type"] or "", "severity": r["severity"] or "",
         "note": r["note"] or "", "close_up": r["close_up"] or ""}
        for r in rows
    ]


@app.delete("/api/hotspot/{hotspot_id}")
def delete_hotspot(hotspot_id: int, request: Request):
    """Delete a hotspot only when its vehicle belongs to the current tenant."""
    conn = sqlite3.connect(str(DB_PATH))
    row = conn.execute("SELECT vin FROM hotspots WHERE id=?", (hotspot_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Hotspot not found")
    _require_vehicle_owner(request, row[0])
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("DELETE FROM hotspots WHERE id = ?", (hotspot_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}


# ── Interior-panorama hotspots (Pannellum yaw/pitch feature pins) ─────────────
@app.post("/api/pano-hotspot")
def add_pano_hotspot(request: Request, vin: str = Form(...), pano_file: str = Form(""),
                           yaw: str = Form("0"), pitch: str = Form("0"),
                           label: str = Form(...), description: str = Form("")):
    """Add a bounded interior feature pin to a vehicle owned by this tenant."""
    vin = validate_vehicle_id(vin)
    _require_vehicle_owner(request, vin)
    label = "".join(ch for ch in (label or "") if ord(ch) >= 32 and ord(ch) != 127).strip()[:80]
    description = "".join(ch for ch in (description or "") if ord(ch) in (9, 10, 13) or (ord(ch) >= 32 and ord(ch) != 127)).strip()[:500]
    if not label:
        raise HTTPException(status_code=400, detail="label is required")
    try:
        y, p = float(yaw), float(pitch)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Yaw and pitch must be numbers") from exc
    if not (-180.0 <= y <= 180.0 and -90.0 <= p <= 90.0):
        raise HTTPException(status_code=400, detail="Yaw or pitch is outside its valid range")
    pano_file = (pano_file or "").strip()
    if pano_file:
        if (Path(pano_file).name != pano_file
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", pano_file)
                or Path(pano_file).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}):
            raise HTTPException(status_code=400, detail="Invalid panorama filename")
        pano_dir = (STORAGE_DIR / vin / "panoramas").resolve()
        try:
            pano_path = (pano_dir / pano_file).resolve(strict=True)
        except OSError as exc:
            raise HTTPException(status_code=404, detail="Panorama not found") from exc
        if pano_path.parent != pano_dir or not pano_path.is_file():
            raise HTTPException(status_code=404, detail="Panorama not found")
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.execute(
        "INSERT INTO pano_hotspots (vin, pano_file, yaw, pitch, label, description, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (vin, pano_file, y, p, label, description, datetime.now().isoformat()))
    conn.commit(); hid = cur.lastrowid; conn.close()
    return {"status": "ok", "id": hid}


@app.get("/api/pano-hotspots/{vin}")
def get_pano_hotspots(vin: str):
    """List interior-pano hotspots only for active public inventory."""
    vin = validate_vehicle_id(vin)
    vehicle = db_get_vehicle(vin)
    if not vehicle or vehicle.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")
    conn = sqlite3.connect(str(DB_PATH)); conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, pano_file, yaw, pitch, label, description FROM pano_hotspots WHERE vin=? ORDER BY id",
        (vin,)).fetchall()
    conn.close()
    return [{"id": r["id"], "pano_file": r["pano_file"], "yaw": r["yaw"], "pitch": r["pitch"],
             "label": r["label"], "description": r["description"]} for r in rows]


@app.delete("/api/pano-hotspot/{hotspot_id}")
def delete_pano_hotspot(hotspot_id: int, request: Request):
    """Delete a panorama hotspot only for its owning tenant."""
    conn = sqlite3.connect(str(DB_PATH))
    row = conn.execute("SELECT vin FROM pano_hotspots WHERE id=?", (hotspot_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Hotspot not found")
    _require_vehicle_owner(request, row[0])
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("DELETE FROM pano_hotspots WHERE id = ?", (hotspot_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}


# =============================================================================
# ROUTES — TAGGING PAGE
# =============================================================================
@app.get("/tag/{vin}")
def tag_page(vin: str, request: Request):
    """Tag hotspots on vehicle photos owned by the current tenant."""
    vin = validate_vehicle_id(vin)
    _require_vehicle_owner(request, vin)
    vehicle_dir = STORAGE_DIR / vin
    
    if not vehicle_dir.exists():
        return HTMLResponse("<h1>Vehicle not found</h1>", status_code=404)
    
    exterior_dir = vehicle_dir / "exterior"
    frames = sorted(
        f.name for f in exterior_dir.glob("frame_*.jpg")
        if f.is_file() and re.fullmatch(r"frame_[A-Za-z0-9_-]{1,80}\.jpg", f.name)
    ) if exterior_dir.exists() else []
    
    if not frames:
        return HTMLResponse("<h1>No exterior photos found</h1>", status_code=404)
    
    html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Tag Features - LotVue Pro</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Segoe UI', sans-serif; background: #e8edf2; color: #1a2332; min-height: 100vh; }
        .header { background: #dde3ea; padding: 1rem 2rem; border-bottom: 1px solid #c5cdd6; display: flex; justify-content: space-between; align-items: center; }
        .logo { font-size: 1.2rem; font-weight: 700; color: #58a6ff; }
        .container { max-width: 1200px; margin: 0 auto; padding: 1.5rem; }
        .card { background: #dde3ea; border: 1px solid #c5cdd6; border-radius: 8px; padding: 1.5rem; margin-bottom: 1.5rem; }
        .card h2 { color: #1a2332; margin-bottom: 1rem; }
        .frames-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 0.8rem; }
        .frame-thumb { aspect-ratio: 16/9; border-radius: 6px; overflow: hidden; cursor: pointer; border: 2px solid transparent; transition: all 0.2s; }
        .frame-thumb:hover { border-color: #58a6ff; }
        .frame-thumb.selected { border-color: #58a6ff; box-shadow: 0 0 12px rgba(88,166,255,0.4); }
        .frame-thumb img { width: 100%; height: 100%; object-fit: cover; }
        .frame-num { position: absolute; bottom: 4px; left: 4px; background: rgba(0,0,0,0.7); padding: 2px 6px; border-radius: 4px; font-size: 0.75rem; }
        .selected-viewer { position: relative; margin-top: 1rem; }
        .selected-viewer img { width: 100%; max-height: 400px; object-fit: contain; border-radius: 8px; background: #000; cursor: crosshair; }
        .tag-form { display: flex; gap: 1rem; align-items: center; margin-top: 1rem; flex-wrap: wrap; }
        .tag-form select { padding: 0.6rem; background: #010409; border: 1px solid #c5cdd6; border-radius: 6px; color: #1a2332; font-size: 1rem; }
        .btn { background: #238636; border: none; padding: 0.6rem 1.2rem; color: white; border-radius: 6px; cursor: pointer; font-weight: 600; }
        .btn:hover { background: #2ea043; }
        .btn-outline { background: transparent; border: 1px solid #b0bac5; color: #8b949e; }
        .hotspots-list { margin-top: 1.5rem; }
        .hotspot-item { display: flex; justify-content: space-between; align-items: center; background: #c5cdd6; padding: 0.8rem 1rem; border-radius: 6px; margin-bottom: 0.5rem; }
        .hotspot-item .tag-badge { background: #58a6ff; color: #e8edf2; padding: 2px 8px; border-radius: 4px; font-size: 0.85rem; font-weight: 600; }
        .nav-btns { display: flex; gap: 1rem; justify-content: center; margin-top: 2rem; }
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
            list.replaceChildren();
            if (!Array.isArray(hotspots) || hotspots.length === 0) {{
                const empty = document.createElement('p');
                empty.textContent = 'No hotspots tagged yet.';
                list.appendChild(empty);
                return;
            }}
            hotspots.forEach(h => {{
                const item = document.createElement('div');
                item.className = 'hotspot-item';
                const label = document.createElement('span');
                label.textContent = '📷 ' + String(h.photo || '') + ' → ';
                const badge = document.createElement('span');
                badge.className = 'tag-badge';
                badge.textContent = String(h.tag || '').replace(/_/g, ' ');
                label.appendChild(badge);
                const button = document.createElement('button');
                button.className = 'btn-outline';
                button.style.padding = '4px 8px';
                button.textContent = 'Delete';
                button.addEventListener('click', () => deleteHotspot(Number(h.id)));
                item.append(label, button);
                list.appendChild(item);
            }});
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
def view_vehicle(vin: str, request: Request):
    try:
        vin = validate_vehicle_id(vin)
        info = db_get_vehicle(vin)
        if not info or info.get("status", "active") != "active":
            return HTMLResponse("<h1>Vehicle not found</h1>", status_code=404)
        try:
            vehicle_dir = _safe_vehicle_directory(vin)
        except HTTPException:
            return HTMLResponse("<h1>Vehicle not found</h1>", status_code=404)

        def safe_media_names(subdir: str, suffixes: set[str], prefix: str = "") -> list[str]:
            directory = vehicle_dir / subdir
            if directory.is_symlink():
                return []
            try:
                resolved_dir = directory.resolve(strict=True)
                if (resolved_dir.parent != vehicle_dir or resolved_dir.name != subdir
                        or not resolved_dir.is_dir()):
                    return []
                with os.scandir(resolved_dir) as entries:
                    return heapq.nsmallest(
                        300,
                        (entry.name for entry in entries
                         if not entry.is_symlink()
                         and entry.is_file(follow_symlinks=False)
                         and Path(entry.name).suffix.lower() in suffixes
                         and (not prefix or entry.name.startswith(prefix))
                         and not entry.name.startswith(".")
                         and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", entry.name)),
                    )
            except OSError:
                return []

        # ── Exterior frames ──────────────────────────────────────────────────
        ext_frames = safe_media_names("spin", {".jpg"}, "spin_")
        if ext_frames:
            frame_path_prefix = f"/storage/{vin}/spin"
        else:
            ext_frames = safe_media_names("exterior", {".jpg", ".jpeg", ".png", ".webp"})
            frame_path_prefix = f"/storage/{vin}/exterior"

        # ── Interior photos ──────────────────────────────────────────────────
        int_photos = safe_media_names("interior", {".jpg", ".jpeg", ".png", ".webp"})

        # ── Interior panoramas (equirectangular) ─────────────────────────────
        pano_files = safe_media_names("panoramas", {".jpg", ".jpeg", ".png", ".webp"})

        # ── Gallery photos ───────────────────────────────────────────────────
        gallery_files = safe_media_names("gallery", {".jpg", ".jpeg", ".png", ".webp"})
        # Fallback: use exterior frames as gallery if no dedicated gallery
        if not gallery_files:
            gallery_files = ext_frames
            gallery_prefix = frame_path_prefix
        else:
            gallery_prefix = f"/storage/{vin}/gallery"

        # ── Public listing details ───────────────────────────────────────────
        title = f"{info.get('year','')} {info.get('make','')} {info.get('model','')}".strip() or vin
        price = str(info.get("price", "") or "")
        mileage = str(info.get("mileage", "") or "")
        color = str(info.get("color", "") or "")
        vehicle = info
        try:
            video_url, video_type = normalize_video_source(
                info.get("video_url", "") or "", info.get("video_type", "") or "", vin)
        except HTTPException:
            # Never reflect a legacy/untrusted URL into public page markup.
            video_url, video_type = "", ""
        dealer_logo = str(info.get("dealer_logo_url", "") or "").strip()
        try:
            logo_parts = urlsplit(dealer_logo)
            logo_port = logo_parts.port
            if logo_parts.scheme:
                safe_logo = (logo_parts.scheme.lower() == "https" and bool(logo_parts.hostname)
                             and not logo_parts.username and not logo_parts.password
                             and logo_port in {None, 443})
            elif not logo_parts.netloc:
                logo_path = unquote(logo_parts.path, errors="strict")
                logo_segments = logo_path.split("/")
                safe_logo = (not logo_parts.query and not logo_parts.fragment
                             and "%" not in logo_parts.path and chr(92) not in logo_path
                             and all(part not in {".", ".."} for part in logo_segments)
                             and all(not part or re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in logo_segments)
                             and logo_path.startswith(("/static/", "/storage/_brand/", f"/storage/{vin}/")))
            else:
                safe_logo = False
            if not safe_logo:
                dealer_logo = ""
        except (ValueError, UnicodeDecodeError):
            dealer_logo = ""

        # ── Build JS arrays ──────────────────────────────────────────────────
        ext_urls_js = _json_for_script([f"{frame_path_prefix}/{f}" for f in ext_frames])
        int_urls_js = _json_for_script([f"/storage/{vin}/interior/{f}" for f in int_photos])
        pano_scene_names = {}
        try:
            conn = sqlite3.connect(str(DB_PATH))
            pano_rows = conn.execute(
                "SELECT file_path, scene_name FROM interior_panoramas WHERE vin=? ORDER BY sort_order, id",
                (vin,)
            ).fetchall()
            conn.close()
            pano_scene_names = {Path(row[0]).name: row[1] for row in pano_rows}
        except Exception:
            pass
        pano_urls_js = _json_for_script([
            {"src": f"/storage/{vin}/panoramas/{f}",
             "name": str(pano_scene_names.get(f) or f"Scene {i+1}"), "file": f}
            for i, f in enumerate(pano_files)
        ])
        gallery_urls_js = _json_for_script([f"{gallery_prefix}/{f}" for f in gallery_files])
        video_url_js = _json_for_script(video_url)
        video_type_js = _json_for_script(video_type)
        vin_js = _json_for_script(vin)
        active_bg_slug_js = _json_for_script(str(info.get("background_id", "") or ""))

        # ── Tab visibility ───────────────────────────────────────────────────
        has_interior = len(int_photos) > 0 or len(pano_files) > 0
        has_gallery = len(gallery_files) > 0
        has_video = bool(video_url)
        base_url = (os.getenv("LOTVUE_PUBLIC_URL", "").strip().rstrip("/")
                    or str(request.base_url).rstrip("/"))
        share_url = f"{base_url}/view/{vin}"
        embed_code = (f'&lt;iframe src="{html_lib.escape(base_url, quote=True)}/embed/{vin}" '
                      'width="100%" height="600" frameborder="0" allowfullscreen&gt;&lt;/iframe&gt;')
        # Social sharing metadata (og: / twitter:)
        og_image = f"{base_url}{frame_path_prefix}/{ext_frames[0]}" if ext_frames else ""
        og_desc = " · ".join(x for x in [
            f"${price}" if price else "",
            f"{mileage} miles" if mileage else "",
            color if color else "",
        ] if x) or f"360° interactive viewer for {title}"
        title_html = html_lib.escape(str(title), quote=True)
        price_html = html_lib.escape(price, quote=True)
        mileage_html = html_lib.escape(mileage, quote=True)
        color_html = html_lib.escape(color, quote=True)
        vin_html = html_lib.escape(vin, quote=True)
        og_desc_html = html_lib.escape(og_desc, quote=True)
        og_image_html = html_lib.escape(og_image, quote=True)
        share_url_html = html_lib.escape(share_url, quote=True)
        trim_html = html_lib.escape(str(vehicle.get("trim_level", "") or ""), quote=True)
        engine_html = html_lib.escape(str(vehicle.get("engine", "") or ""), quote=True)
        transmission_html = html_lib.escape(str(vehicle.get("transmission", "") or ""), quote=True)
        drivetrain_html = html_lib.escape(str(vehicle.get("drivetrain", "") or ""), quote=True)
        fuel_type_html = html_lib.escape(str(vehicle.get("fuel_type", "") or ""), quote=True)
        body_style_html = html_lib.escape(str(vehicle.get("body_style", "") or ""), quote=True)
    except HTTPException:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        return HTMLResponse("<h1>Unable to load this vehicle viewer</h1>", status_code=500)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title_html} — LotVue Pro</title>
<meta property="og:type" content="website">
<meta property="og:title" content="{title_html} — LotVue360">
<meta property="og:description" content="{og_desc_html}">
<meta property="og:image" content="{og_image_html}">
<meta property="og:url" content="{share_url_html}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{title_html} — LotVue360">
<meta name="twitter:description" content="{og_desc_html}">
<meta name="twitter:image" content="{og_image_html}">
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
#spin-container img.spin-image{{width:100%;height:100%;object-fit:contain;position:absolute;inset:0;pointer-events:none;display:block;opacity:0;transition:opacity 120ms linear;}}
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
.pano-hs-card{{display:none;position:fixed;left:50%;bottom:2rem;transform:translateX(-50%);z-index:9999;background:#0f2040;color:#f5f0e8;border:1px solid #1a2e50;border-radius:12px;padding:1rem 2.4rem 1rem 1.2rem;max-width:340px;box-shadow:0 10px 30px rgba(0,0,0,0.45);}}
.pano-hs-card h4{{margin:0 0 0.3rem;color:#C8A55C;font-size:1rem;}}
.pano-hs-card p{{margin:0;font-size:0.86rem;line-height:1.45;color:#cdd9ea;}}
.pano-hs-x{{position:absolute;top:0.5rem;right:0.75rem;cursor:pointer;font-size:1.3rem;line-height:1;color:#8b9bb4;}}
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
    {'<img class="dealer-logo" src="' + html_lib.escape(dealer_logo, quote=True) + '" alt="Dealer">' if dealer_logo else '<div class="dealer-logo-placeholder">LotVue Pro</div>'}
    <div class="vehicle-title">
      <h1>{title_html}</h1>
      <div class="vin-badge">VIN: {vin_html}</div>
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
          <input type="text" id="shareUrlInput" value="{share_url_html}" readonly>
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
      {'<div class="spec-row"><div class="spec-label">Price</div><div class="price-display">' + price_html + '</div></div>' if price else ''}
      {'<div class="spec-row"><div class="spec-label">Mileage</div><div class="spec-value">' + mileage_html + ' mi</div></div>' if mileage else ''}
      {'<div class="spec-row"><div class="spec-label">Color</div><div class="spec-value">' + color_html + '</div></div>' if color else ''}
      {'<div class="spec-row"><div class="spec-label">Trim</div><div class="spec-value">' + trim_html + '</div></div>' if vehicle.get("trim_level") else ''}
      {'<div class="spec-row"><div class="spec-label">Engine</div><div class="spec-value">' + engine_html + '</div></div>' if vehicle.get("engine") else ''}
      {'<div class="spec-row"><div class="spec-label">Trans.</div><div class="spec-value">' + transmission_html + '</div></div>' if vehicle.get("transmission") else ''}
      {'<div class="spec-row"><div class="spec-label">Drive</div><div class="spec-value">' + drivetrain_html + '</div></div>' if vehicle.get("drivetrain") else ''}
      {'<div class="spec-row"><div class="spec-label">Fuel</div><div class="spec-value">' + fuel_type_html + '</div></div>' if vehicle.get("fuel_type") else ''}
      {'<div class="spec-row"><div class="spec-label">Body</div><div class="spec-value">' + body_style_html + '</div></div>' if vehicle.get("body_style") else ''}
      <div class="spec-row"><div class="spec-label">VIN</div><div class="spec-value" style="font-size:0.75rem;font-family:monospace;">{vin_html}</div></div>
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
<div id="lv-glo3d-root" data-vin="{vin_html}" data-title="{title_html}"></div>
<script src="/static/glo3d_tools.js?v=20260612b" defer></script>

<script>
// ── Data from server ────────────────────────────────────────────────────────
const EXT_URLS = {ext_urls_js};
const INT_URLS = {int_urls_js};
const PANO_URLS = {pano_urls_js};
const GALLERY_URLS = {gallery_urls_js};
const VIDEO_URL = {video_url_js};
const VIDEO_TYPE = {video_type_js};
const VIN = {vin_js};

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
let spinImage = null, spinCur = 0, spinDragging = false, spinStartX = 0, spinStartFrame = 0;
let autoInterval = null, autoRunning = false;
const spinImageCache = new Map();
const SPIN_CACHE_RADIUS = 2;

function preloadSpinFrame(idx) {{
  if (!EXT_URLS.length) return;
  idx = ((idx % EXT_URLS.length) + EXT_URLS.length) % EXT_URLS.length;
  if (idx === spinCur || spinImageCache.has(idx)) return;
  const img = new Image();
  img.decoding = 'async';
  img.loading = 'eager';
  img.src = EXT_URLS[idx];
  spinImageCache.set(idx, img);
}}

function preloadSpinNeighborhood(center) {{
  if (!EXT_URLS.length) return;
  for (let offset = -SPIN_CACHE_RADIUS; offset <= SPIN_CACHE_RADIUS; offset++) {{
    if (offset) preloadSpinFrame(center + offset);
  }}
  for (const [idx] of spinImageCache) {{
    const distance = Math.min((idx - center + EXT_URLS.length) % EXT_URLS.length,
                              (center - idx + EXT_URLS.length) % EXT_URLS.length);
    if (distance > SPIN_CACHE_RADIUS) spinImageCache.delete(idx);
  }}
}}

function initSpin() {{
  const container = document.getElementById('spin-container');
  if (!EXT_URLS.length) {{
    container.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#8b949e;font-size:1rem;">No exterior frames available</div>';
    return;
  }}
  // Only one full-resolution image is retained in the DOM. Nearby frames are
  // decoded on demand in a small rolling cache instead of loading every frame.
  spinImage = document.createElement('img');
  spinImage.className = 'spin-image';
  spinImage.alt = 'Interactive 360 degree vehicle view';
  spinImage.decoding = 'async';
  spinImage.fetchPriority = 'high';
  spinImage.draggable = false;
  spinImage.onload = () => {{ spinImage.style.opacity = '1'; }};
  spinImage.onerror = () => showToast('A spin frame could not be loaded.');
  container.insertBefore(spinImage, container.querySelector('.frame-counter'));
  showSpinFrame(0);
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
  container.addEventListener('wheel', spinWheel, {{passive: false}});
  container.addEventListener('dblclick', () => {{
    zoomLevel = zoomLevel >= 4 ? 1 : zoomLevel * 2;
    if (zoomLevel === 1) {{ panX = 0; panY = 0; }}
    applyZoom();
  }});
  // Per the capture spec, autoplay starts on desktop hover (not on initial page
  // load), and stops immediately when the pointer leaves or the page is hidden.
  container.addEventListener('pointerenter', () => {{
    if (matchMedia('(hover: hover) and (prefers-reduced-motion: no-preference)').matches) startAuto();
  }});
  container.addEventListener('pointerleave', stopAuto);
  document.addEventListener('visibilitychange', () => {{ if (document.hidden) stopAuto(); }});
  document.addEventListener('keydown', e => {{
    if (activeTab !== 'exterior') return;
    if (e.key === 'ArrowLeft') spinStep(-1);
    if (e.key === 'ArrowRight') spinStep(1);
  }});
}}

function showSpinFrame(idx) {{
  if (!EXT_URLS.length || !spinImage) return;
  spinCur = ((idx % EXT_URLS.length) + EXT_URLS.length) % EXT_URLS.length;
  const nextUrl = EXT_URLS[spinCur];
  if (spinImage.getAttribute('src') !== nextUrl) {{
    spinImage.style.opacity = '0.35';
    spinImage.src = nextUrl;
  }}
  preloadSpinNeighborhood(spinCur);
  updateFrameCounter();
  updateStripHighlight(spinCur);
  if (window.hotspotManager) window.hotspotManager.update(spinCur);
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
  if (spinImage) {{
    spinImage.style.transform = z > 1 ? `translate(${{panX}}px, ${{panY}}px) scale(${{z}})` : '';
  }}
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
  if (!EXT_URLS.length || document.hidden || autoRunning) return;
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
    const image = document.createElement('img');
    image.src = url;
    image.loading = 'lazy';
    image.decoding = 'async';
    image.alt = `frame ${{i + 1}}`;
    thumb.appendChild(image);
    thumb.onclick = () => {{ stopAuto(); showSpinFrame(i); }};
    strip.appendChild(thumb);
  }});
}}
function updateStripHighlight(idx) {{
  document.querySelectorAll('.frame-thumb').forEach((t, i) => {{
    t.classList.toggle('active', i === idx);
  }});
}}

// ── Interior tab ────────────────────────────────────────────────────────────
let panoViewer = null, panoInited = false;
function showPanoHotspot(evt, args) {{
  // GLO3D-style feature card when a buyer taps an interior pin
  let c = document.getElementById('panoHsCard');
  if (!c) {{ c = document.createElement('div'); c.id = 'panoHsCard'; c.className = 'pano-hs-card'; document.body.appendChild(c); }}
  c.textContent = '';
  const x = document.createElement('span'); x.className = 'pano-hs-x'; x.textContent = '×';
  x.onclick = function() {{ c.style.display = 'none'; }};
  const h = document.createElement('h4'); h.textContent = (args && args.label) || '';
  const p = document.createElement('p'); p.textContent = (args && args.description) || '';
  c.appendChild(x); c.appendChild(h); c.appendChild(p);
  c.style.display = 'block';
}}
async function initInterior() {{
  if (panoInited) return;
  panoInited = true;
  if (PANO_URLS.length > 0) {{
    // Equirectangular panorama with Pannellum
    document.getElementById('pano-container').style.display = 'block';
    document.getElementById('intGrid').style.display = 'none';
    // feature pins (yaw/pitch) — best-effort; pano still renders if this fails
    let _hs = [];
    try {{ const r = await fetch('/api/pano-hotspots/{vin}'); if (r.ok) _hs = await r.json(); }} catch (e) {{}}
    const scenes = {{}};
    PANO_URLS.forEach((p, i) => {{
      const mine = _hs.filter(h => !h.pano_file || h.pano_file === p.file);
      scenes['scene' + i] = {{
        type: 'equirectangular',
        panorama: p.src,
        title: p.name,
        autoLoad: i === 0,
        hfov: 120,
        minHfov: 50,
        maxHfov: 150,
        hotSpots: mine.map(h => ({{
          pitch: h.pitch, yaw: h.yaw, type: 'info', text: h.label,
          clickHandlerFunc: showPanoHotspot,
          clickHandlerArgs: {{ label: h.label, description: h.description }}
        }}))
      }};
    }});
    panoViewer = pannellum.viewer('pano-container', {{
      escapeHTML: true,
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
      const image = document.createElement('img');
      image.src = url;
      image.loading = 'lazy';
      image.decoding = 'async';
      image.alt = 'Interior photo';
      div.appendChild(image);
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
    const image = document.createElement('img');
    image.src = url;
    image.loading = 'lazy';
    image.decoding = 'async';
    image.alt = `Photo ${{i + 1}}`;
    item.appendChild(image);
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

  let parsed;
  try {{ parsed = new URL(VIDEO_URL, window.location.origin); }} catch (e) {{ return; }}
  let embedUrl = '';
  if (VIDEO_TYPE === 'youtube' && parsed.protocol === 'https:' &&
      ['www.youtube.com', 'youtube.com'].includes(parsed.hostname)) {{
    const id = parsed.searchParams.get('v') || '';
    if (/^[A-Za-z0-9_-]{{11}}$/.test(id)) embedUrl = `https://www.youtube-nocookie.com/embed/${{id}}?autoplay=0&rel=0`;
  }} else if (VIDEO_TYPE === 'vimeo' && parsed.protocol === 'https:' && parsed.hostname === 'vimeo.com') {{
    const id = parsed.pathname.split('/').filter(Boolean).pop() || '';
    if (/^[0-9]{{1,20}}$/.test(id)) embedUrl = `https://player.vimeo.com/video/${{id}}`;
  }}

  if (embedUrl) {{
    const frame = document.createElement('iframe');
    frame.src = embedUrl;
    frame.title = 'Vehicle walkaround video';
    frame.loading = 'lazy';
    frame.referrerPolicy = 'strict-origin-when-cross-origin';
    frame.allow = 'autoplay; encrypted-media; picture-in-picture; web-share';
    frame.setAttribute('allowfullscreen', '');
    container.replaceChildren(frame);
    return;
  }}

  // Direct video sources are HTTPS URLs or allowlisted, same-vehicle storage
  // paths. Assign through the DOM API; never interpolate a URL into HTML.
  const hasVideoSuffix = ['.mp4', '.m4v', '.webm'].some(ext =>
    parsed.pathname.toLowerCase().endsWith(ext));
  const isLocalVehicleVideo = parsed.origin === window.location.origin &&
    parsed.pathname.startsWith(`/storage/${{VIN}}/`) && hasVideoSuffix;
  const isRemoteVideo = parsed.protocol === 'https:' && Boolean(parsed.hostname) && hasVideoSuffix;
  if ((VIDEO_TYPE === 'upload' || VIDEO_TYPE === 'mp4' || VIDEO_TYPE === 'video') &&
      (isLocalVehicleVideo || isRemoteVideo)) {{
    const video = document.createElement('video');
    video.controls = true;
    video.playsInline = true;
    video.preload = 'metadata';
    video.style.maxWidth = '100%';
    video.style.maxHeight = '100%';
    video.src = parsed.href;
    video.addEventListener('error', () => showToast('The video could not be loaded.'));
    container.replaceChildren(video);
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
      const success = document.createElement('div');
      success.className = 'form-success';
      const icon = document.createElement('div');
      icon.className = 'success-icon';
      icon.textContent = '✅';
      const heading = document.createElement('h3');
      heading.textContent = 'Inquiry Sent!';
      const message = document.createElement('p');
      message.textContent = `Thank you, ${{String(data.name || '')}}. We'll contact you at ${{String(data.email || '')}} shortly.`;
      success.append(icon, heading, message);
      document.getElementById('leadFormInner').replaceChildren(success);
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
// deep-link: open the Interior tab when arriving with #interior (e.g. from the turntable viewer)
(function(){{ const ib=document.getElementById('tab-interior');
  if(location.hash==='#interior' && ib && !ib.classList.contains('hidden')){{ switchTab('interior'); }} }})();

// ── Background Selector ─────────────────────────────────────────────────────
let activeBgSlug = {active_bg_slug_js};

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
    const response = await fetch('/api/vehicle/' + encodeURIComponent(VIN) + '/background', {{method:'POST', body:fd}});
    if (!response.ok) throw new Error('Background save failed');
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
def embed_vehicle(vin: str):
    """Minimal, safe iframe wrapper around the shared 360 viewer component."""
    vin = validate_vehicle_id(vin)
    vehicle = db_get_vehicle(vin)
    try:
        _safe_vehicle_directory(vin)
    except HTTPException:
        return HTMLResponse("<h1>Vehicle not found</h1>", status_code=404)
    if not vehicle or vehicle.get("status", "active") != "active":
        return HTMLResponse("<h1>Vehicle not found</h1>", status_code=404)
    safe_vin = html_lib.escape(vin, quote=True)
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LotVue Pro vehicle viewer</title>
<style>html,body{{margin:0;width:100%;height:100%;background:#0a1226}}
lotvue-spin{{display:block;width:100%;height:100%}}</style></head>
<body><lotvue-spin vin="{safe_vin}"></lotvue-spin>
<script type="module">import "/static/lotvue_spin.js";</script></body></html>"""
    return HTMLResponse(html)


# =============================================================================
# ROUTES — DEALER SETTINGS
# =============================================================================
@app.get("/settings")
def dealer_settings(request: Request):
    email = _authenticated_user_email(request)
    dealer = _account_dealer(email) or {"name": "", "display_name": "", "phone": "", "email": "", "address": "", "website": "", "logo_url": "", "brand_color": "#58a6ff"}
    dealer = {key: html_lib.escape(str(value or ""), quote=True) for key, value in dealer.items()}
    dealer.setdefault("brand_color", "#58a6ff")
    
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
    body = await _read_json_object_limited(request, 4096)
    vin = validate_vehicle_id(vin)
    new_status = str(body.get("status", "")).strip().lower()
    if new_status not in VALID_WORKFLOW_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {VALID_WORKFLOW_STATUSES}")
    await run_in_threadpool(_require_vehicle_owner, request, vin)
    vehicle = await run_in_threadpool(db_get_vehicle, vin)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    await run_in_threadpool(_write_workflow_status, vin, new_status)
    return {"status": "ok", "vin": vin, "workflow_status": new_status}


# =============================================================================
# API — DEALER
# =============================================================================
@app.post("/api/dealer")
def save_dealer(
    request: Request,
    display_name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    address: str = Form(""),
    website: str = Form(""),
    brand_color: str = Form("#58a6ff")
):
    actor_email = _authenticated_user_email(request)
    for field_name, value, maximum in (
        ("display name", display_name, 120), ("phone", phone, 40), ("email", email, 254),
        ("address", address, 300), ("website", website, 300),
    ):
        if len(value or "") > maximum:
            raise HTTPException(status_code=400, detail=f"{field_name} is too long")
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.strip()):
        raise HTTPException(status_code=400, detail="Enter a valid dealer email")
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", brand_color):
        raise HTTPException(status_code=400, detail="Brand color must be a six-digit hex color")
    dealer = _account_dealer(actor_email, create=True)
    if not dealer:
        raise HTTPException(status_code=500, detail="Dealer profile could not be created")
    db_upsert_dealer(
        dealer["name"], display_name=display_name.strip(), phone=phone.strip(),
        email=email.strip().lower() or actor_email, address=address.strip(),
        website=website.strip(), brand_color=brand_color, user_email=actor_email,
    )
    return {"status": "ok"}


@app.get("/api/dealer")
def get_dealer_api(request: Request):
    email = _authenticated_user_email(request)
    dealer = _account_dealer(email)
    return dealer or {"name": "", "display_name": "", "brand_color": "#58a6ff"}


@app.get("/api/vehicles")
def get_vehicles_api(request: Request):
    actor_email = _authenticated_user_email(request)
    owner_email = os.getenv("LOTVUE_OWNER_EMAIL", "").strip().lower()
    return db_get_vehicles() if owner_email and actor_email == owner_email else db_get_vehicles_by_email(actor_email)


# =============================================================================
# API — DELETE
# =============================================================================
@app.delete("/api/vehicle/{vin}")
def delete_vehicle(vin: str, request: Request):
    vin = validate_vehicle_id(vin)
    _require_vehicle_owner(request, vin)
    vehicle = db_get_vehicle(vin)
    if not vehicle or vehicle.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")
    vehicle_dir = (STORAGE_DIR / vin).resolve()
    if vehicle_dir.parent != STORAGE_DIR.resolve():
        raise HTTPException(status_code=400, detail="Invalid vehicle path")
    if vehicle_dir.exists():
        shutil.rmtree(vehicle_dir)
    db_delete_vehicle(vin)
    return {"status": "ok"}


# =============================================================================
# API — UPDATE VEHICLE METADATA (price, mileage, color)
# =============================================================================
@app.post("/api/vehicle/{vin}/meta")
def update_vehicle_meta(
    vin: str,
    request: Request,
    price: str = Form(""),
    mileage: str = Form(""),
    color: str = Form(""),
):
    vin = validate_vehicle_id(vin)
    _require_vehicle_owner(request, vin)
    if len(price) > 32 or len(mileage) > 32 or len(color) > 80:
        raise HTTPException(status_code=400, detail="Vehicle metadata is too long")
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
def decode_vin(vin: str, request: Request):
    """Decode an 11–17 character VIN; an existing vehicle may only be updated by its owner."""
    vin = (vin or "").strip().upper()
    if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{11,17}", vin):
        raise HTTPException(status_code=400, detail="VIN must contain 11–17 valid VIN characters")
    _authenticated_user_email(request)
    existing = db_get_vehicle(vin)
    if existing:
        _require_vehicle_owner(request, vin)

    try:
        import urllib.request
        url = f"https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{quote(vin, safe='')}?format=json"
        with urllib.request.urlopen(url, timeout=8) as resp:
            raw_response = resp.read(512 * 1024 + 1)
        if len(raw_response) > 512 * 1024:
            raise HTTPException(status_code=502, detail="VIN decoder returned an oversized response")
        data = json.loads(raw_response)

        if not data.get("Results"):
            return {"status": "error", "message": "No results from NHTSA"}

        r = data["Results"][0]

        def clean(val):
            text = str(val or "").strip()
            return text if text not in {"", "0", "Not Applicable"} else ""

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

        # Auto-save only the record checked above against this tenant.
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

    except HTTPException:
        raise
    except Exception as e:
        print(f"[VIN Decode] {type(e).__name__} for {vin}")
        return {"status": "error", "message": "VIN decoder is temporarily unavailable", "decoded": {}}


@app.get("/api/backgrounds")
def get_backgrounds(category: str = None):
    """List packaged public system backgrounds from the canonical asset catalog."""
    if category is not None and (len(category) > 40 or not re.fullmatch(r"[A-Za-z0-9_-]+", category)):
        raise HTTPException(status_code=400, detail="Invalid background category")
    with closing(sqlite3.connect(str(DB_PATH), timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id,slug FROM backgrounds WHERE is_active=1 AND is_system=1 "
            "ORDER BY category,name"
        ).fetchall()
    assets = {asset["slug"]: asset for asset in list_background_assets(include_none=True)}
    result = []
    for row in rows:
        asset = assets.get(row["slug"])
        if not asset or (category and asset["category"] != category):
            continue
        result.append({"id": row["id"], **asset})
    return result


@app.post("/api/vehicle/{vin}/background")
def set_vehicle_background(
    vin: str,
    request: Request,
    background_slug: str = Form(""),
    turntable_style: str = Form("studio_shadow"),
):
    """Set the background and turntable style for a vehicle owned by this tenant."""
    vin = validate_vehicle_id(vin)
    _require_vehicle_owner(request, vin)
    background_slug = background_slug.strip()
    if len(background_slug) > 80 or turntable_style not in {"studio_shadow", "concrete", "glossy_black", "none"}:
        raise HTTPException(status_code=400, detail="Invalid background selection")
    conn = sqlite3.connect(str(DB_PATH))
    background = conn.execute(
        "SELECT 1 FROM backgrounds WHERE slug=? AND is_active=1", (background_slug,)
    ).fetchone()
    if not background:
        conn.close()
        raise HTTPException(status_code=400, detail="Unknown background")
    cur = conn.execute(
        "UPDATE vehicles SET background_id=?, turntable_style=? WHERE vin=?",
        (background_slug, turntable_style, vin)
    )
    if cur.rowcount == 0:
        conn.close()
        raise HTTPException(status_code=404, detail="Vehicle not found")
    conn.commit()
    conn.close()
    return {"status": "ok", "vin": vin, "background_slug": background_slug, "turntable_style": turntable_style}


# =============================================================================
# ROUTES — PHASE 2A: VIEWER DATA, LEADS, ANALYTICS
# =============================================================================
# =============================================================================

@app.get("/api/vehicles/{vin}/viewer")
def get_vehicle_viewer_data(vin: str):
    """Public, read-only viewer manifest for active inventory; never expose tenant fields."""
    vin = validate_vehicle_id(vin)
    info = db_get_vehicle(vin)
    if not info or info.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")
    vehicle_dir = _safe_vehicle_directory(vin)

    def image_names(subdir: str, suffixes: set[str], prefix: str = "") -> list[str]:
        directory = vehicle_dir / subdir
        if directory.is_symlink():
            return []
        try:
            resolved_dir = directory.resolve(strict=True)
            if (resolved_dir.parent != vehicle_dir or resolved_dir.name != subdir
                    or not resolved_dir.is_dir()):
                return []
            with os.scandir(resolved_dir) as entries:
                return heapq.nsmallest(
                    300,
                    (entry.name for entry in entries
                     if not entry.is_symlink()
                     and entry.is_file(follow_symlinks=False)
                     and Path(entry.name).suffix.lower() in suffixes
                     and (not prefix or entry.name.startswith(prefix))
                     and not entry.name.startswith(".")
                     and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", entry.name)),
                )
        except OSError:
            return []

    spin_frames = image_names("spin", {".jpg"}, "spin_")
    if spin_frames:
        frame_subdir = "spin"
        ext_frames = spin_frames
    else:
        frame_subdir = "exterior"
        ext_frames = image_names("exterior", {".jpg", ".jpeg", ".png", ".webp"})
    ext_urls = [f"/storage/{vin}/{frame_subdir}/{name}" for name in ext_frames]
    int_urls = [f"/storage/{vin}/interior/{name}" for name in image_names("interior", {".jpg", ".jpeg", ".png", ".webp"})]
    pano_files = image_names("panoramas", {".jpg", ".jpeg", ".png", ".webp"})
    pano_urls = [{"src": f"/storage/{vin}/panoramas/{name}", "name": f"Scene {i+1}"}
                 for i, name in enumerate(pano_files)]
    gallery_urls = [f"/storage/{vin}/gallery/{name}"
                    for name in image_names("gallery", {".jpg", ".jpeg", ".png", ".webp"})]
    if not gallery_urls:
        gallery_urls = ext_urls

    video_url, video_type = "", ""
    try:
        video_url, video_type = normalize_video_source(
            info.get("video_url", ""), info.get("video_type", ""), vin
        )
    except HTTPException:
        pass
    logo_url = str(info.get("dealer_logo_url", "") or "")[:2048]
    if logo_url and not (
        (logo_url.startswith(("/static/", f"/storage/{vin}/", "/storage/_brand/"))
         and ".." not in urlsplit(logo_url).path.split("/"))
        or (urlsplit(logo_url).scheme == "https" and bool(urlsplit(logo_url).hostname)
            and not urlsplit(logo_url).username and not urlsplit(logo_url).password)
    ):
        logo_url = ""

    public_info = {
        key: str(info.get(key, "") or "")[:500]
        for key in ("year", "make", "model", "price", "mileage", "color")
    }
    return {
        "vin": vin,
        "title": f"{public_info['year']} {public_info['make']} {public_info['model']}".strip() or vin,
        **public_info,
        "video_url": video_url,
        "video_type": video_type,
        "dealer_logo_url": logo_url,
        "exterior_frames": ext_urls,
        "interior_photos": int_urls,
        "interior_panoramas": pano_urls,
        "gallery_photos": gallery_urls,
        "has_interior": bool(int_urls or pano_urls),
        "has_gallery": bool(gallery_urls),
        "has_video": bool(video_url),
        "frame_count": len(ext_urls),
    }


# =============================================================================
# ROUTE — CINEMATIC 3D VIEWER (Module 3)
# =============================================================================
@app.get("/view3d/{vin}")
def view3d(vin: str):
    """Honest 3D routing: TRUE gaussian-splat viewer when scene.ply exists,
    else the real-photo 360 spin. A customer must only ever see the REAL
    vehicle — never the procedural placeholder car (viewer3d.js legacy)."""
    from fastapi.responses import RedirectResponse
    vin_safe = validate_vehicle_id(vin)
    vehicle = db_get_vehicle(vin_safe)
    if not vehicle or vehicle.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")
    if (STORAGE_DIR / vin_safe / "splat" / "scene.ply").is_file():
        return RedirectResponse(f"/splat3d/{vin_safe}", status_code=302)
    if (STORAGE_DIR / vin_safe).exists() or db_get_vehicle(vin_safe):
        return RedirectResponse(f"/view/{vin_safe}", status_code=302)
    raise HTTPException(status_code=404, detail="Vehicle not found")
    # ── legacy cinematic viewer below is intentionally unreachable: it rendered
    # a procedural box-car (viewer3d.js) instead of the real vehicle. ──
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


from pydantic import BaseModel as _BaseModel, Field as _Field

class LeadSubmission(_BaseModel):
    vin: str = _Field(min_length=1, max_length=32)
    name: str = _Field(min_length=1, max_length=120)
    email: str = _Field(min_length=3, max_length=254)
    phone: str = _Field(default="", max_length=40)
    message: str = _Field(default="", max_length=2000)
    wants_video: int = _Field(default=0, ge=0, le=1)
    wants_test_drive: int = _Field(default=0, ge=0, le=1)
    source: str = _Field(default="viewer", max_length=32)


@app.post("/api/leads")
def submit_buyer_lead(lead: LeadSubmission, request: Request):
    """Receive a bounded buyer inquiry for an existing published vehicle."""
    _check_public_lead_rate(request)
    vin = validate_vehicle_id(lead.vin)
    name = re.sub(r"[\x00-\x1f\x7f]", "", lead.name).strip()
    email = lead.email.strip().lower()
    phone = re.sub(r"[\x00-\x1f\x7f]", "", lead.phone or "").strip()
    message = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", lead.message or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=400, detail="Enter a valid email address")
    vehicle = db_get_vehicle(vin)
    if not vehicle or vehicle.get("status") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")

    source = lead.source.strip().lower()
    if source not in {"viewer", "vdp", "embed", "widget", "website"}:
        source = "viewer"
    dealer_id = 0
    tenant_email = str(vehicle.get("user_email", "") or "").strip().lower()
    dealer = db_get_dealer_by_email(tenant_email) if tenant_email else None
    if dealer:
        dealer_id = int(dealer["id"])
    with sqlite3.connect(str(DB_PATH), timeout=5) as conn:
        cur = conn.execute("""
            INSERT INTO leads (dealer_id, vin, name, email, phone, message, wants_video,
                               wants_test_drive, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (dealer_id, vin, name, email, phone, message, lead.wants_video,
              lead.wants_test_drive, source, datetime.now().isoformat()))
        lead_id = cur.lastrowid
    print(f"[Lead] New inquiry for VIN {vin}")
    try:
        from a2_extensions import notify_lead
        notify_lead({"id": lead_id, "vin": vin, "name": name,
                     "email": email, "phone": phone, "message": message,
                     "wants_video": lead.wants_video, "wants_test_drive": lead.wants_test_drive})
    except Exception as e:
        print(f"[Lead-Notify] hook error (lead still stored): {e}")
    return {"status": "ok", "message": "Inquiry received", "lead_id": lead_id}


@app.get("/api/leads")
def get_leads(
    request: Request,
    vin: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
):
    """List only leads attached to the signed-in dealer's vehicles."""
    email = _authenticated_user_email(request)
    owner_email = os.getenv("LOTVUE_OWNER_EMAIL", "").strip().lower()
    limit = max(1, min(limit, 200))
    conditions = []
    params = []
    if vin:
        conditions.append("l.vin=?")
        params.append(validate_vehicle_id(vin))
    if status:
        if len(status) > 40:
            raise HTTPException(status_code=400, detail="Status filter is too long")
        conditions.append("l.status=?")
        params.append(status)
    if not owner_email or email != owner_email:
        conditions.append("EXISTS (SELECT 1 FROM vehicles v WHERE v.vin=l.vin AND LOWER(v.user_email)=?)")
        params.append(email)
    where = " AND ".join(conditions) or "1=1"
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT l.* FROM leads l WHERE {where} ORDER BY l.created_at DESC, l.id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    conn.close()
    return {"status": "ok", "leads": [dict(row) for row in rows], "count": len(rows)}


@app.post("/api/analytics/event")
async def track_analytics_event(request: Request):
    """Accept only small, bounded viewer events for active vehicles."""
    _check_public_analytics_rate(request)
    body = await _read_json_object_limited(request, 8192)
    vin = validate_vehicle_id(str(body.get("vin", "")))
    vehicle = await run_in_threadpool(db_get_vehicle, vin)
    if not vehicle or vehicle.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")
    event_type = str(body.get("event_type", "")).strip().lower()
    if event_type not in {"viewer_open", "tab_switch", "lead_submitted"}:
        raise HTTPException(status_code=400, detail="Unsupported viewer event")
    supplied = body.get("data", {})
    if not isinstance(supplied, dict) or len(supplied) > 12:
        raise HTTPException(status_code=400, detail="Event data must be a small object")
    safe_data = {}
    for key, value in supplied.items():
        if (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", key)
                or not isinstance(value, (str, int, float, bool, type(None)))):
            continue
        if isinstance(value, str):
            safe_data[key] = "".join(ch for ch in value if ord(ch) >= 32 and ord(ch) != 127)[:120]
        else:
            safe_data[key] = value
    if len(json.dumps(safe_data, ensure_ascii=True)) > 2048:
        raise HTTPException(status_code=400, detail="Event data is too large")
    print(f"[Analytics] {event_type} | VIN={vin} | {json.dumps(safe_data, ensure_ascii=True)}")
    return {"status": "ok"}


# =============================================================================
# API — AI VIDEO WALKAROUND (Module 4)
# =============================================================================
@app.post("/api/vehicle/{vin}/walkaround")
def generate_vehicle_walkaround(vin: str, request: Request):
    """Start a walkaround job for an active vehicle the requester owns."""
    vin = validate_vehicle_id(vin)
    _require_vehicle_owner(request, vin)
    info = db_get_vehicle(vin)
    if not info or info.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")

    from ai_studio.video_walkaround import (
        MAX_WALKAROUND_VALIDATION_BYTES,
        generate_walkaround,
        get_walkaround_status,
        mark_walkaround_failed,
    )

    status = get_walkaround_status(vin)
    job_key = f"walkaround:{vin}"
    active_states = {"starting", "queued", "processing"}
    if status.get("status") in active_states or _background_job_running(job_key):
        return {"status": "already_running", "progress": status.get("progress", 0)}

    def _run():
        try:
            generate_walkaround(
                vin,
                info,
                max_photo_bytes=MAX_PHOTO_MB * 1024 * 1024,
                max_image_pixels=MAX_UPLOAD_IMAGE_PIXELS,
                validation_bytes_limit=min(
                    MAX_PHOTO_BATCH_MB * 1024 * 1024,
                    MAX_WALKAROUND_VALIDATION_BYTES,
                ),
            )
        except Exception:
            mark_walkaround_failed(vin)
            raise

    submission = _submit_background_job(job_key, _run)
    if submission == "already_running":
        return {"status": "already_running", "progress": status.get("progress", 0)}
    if submission == "busy":
        raise HTTPException(status_code=429, detail="Background processing capacity is full",
                            headers={"Retry-After": "30"})
    return {"status": "started", "vin": vin}


@app.get("/api/vehicle/{vin}/walkaround-status")
def walkaround_status(vin: str, request: Request):
    """Get walkaround status for an active vehicle the requester owns."""
    vin = validate_vehicle_id(vin)
    _require_vehicle_owner(request, vin)
    vehicle = db_get_vehicle(vin)
    if not vehicle or vehicle.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")
    from ai_studio.video_walkaround import get_walkaround_status
    status = get_walkaround_status(vin)
    status_name = status.get("status")
    active_states = {"starting", "queued", "processing"}
    job_running = _background_job_running(f"walkaround:{vin}")
    if job_running and status_name not in active_states and status_name != "error":
        return {"status": "queued", "progress": 0, "step": "Waiting for a background worker"}

    # A previous public output must not hide an active job or a newer failure.
    video_path = STORAGE_DIR / vin / "walkaround" / "walkaround.mp4"
    if video_path.exists() and status_name not in active_states and status_name != "error":
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
def get_market_price(vin: str, request: Request):
    """Estimate market value for an active vehicle the requester owns."""
    from ai_studio.market_pricing import estimate_market_price

    vin = validate_vehicle_id(vin)
    _require_vehicle_owner(request, vin)
    vehicle = db_get_vehicle(vin)
    if not vehicle or vehicle.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")

    pricing = estimate_market_price(vehicle)
    return {"status": "ok", "vin": vin, "pricing": pricing}


@app.post("/api/vehicle/{vin}/set-price")
async def set_vehicle_price(vin: str, request: Request):
    """Set asking price and get updated market comparison."""
    from ai_studio.market_pricing import estimate_market_price

    body = await _read_json_object_limited(request, 4096)
    vin = validate_vehicle_id(vin)
    await run_in_threadpool(_require_vehicle_owner, request, vin)
    raw_price = body.get("price", "")
    price = str(raw_price if raw_price is not None else "").replace("$", "").replace(",", "").strip()
    if price and (len(price) > 16 or not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", price)
                  or not 0 <= float(price) <= 100_000_000):
        raise HTTPException(status_code=400, detail="Price must be a non-negative amount up to $100,000,000")
    vehicle = await run_in_threadpool(db_get_vehicle, vin)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")

    # Update price in DB, then compute local comps outside the event loop.
    await run_in_threadpool(_write_vehicle_price, vin, price)
    vehicle = await run_in_threadpool(db_get_vehicle, vin)
    pricing = await run_in_threadpool(estimate_market_price, vehicle)
    return {"status": "ok", "vin": vin, "price": price, "pricing": pricing}


# =============================================================================
# ROUTES — LISTING SYNDICATION
# =============================================================================

@app.get("/api/syndication/facebook-csv")
def syndication_facebook(request: Request):
    """Export only the authenticated tenant's active inventory as a catalog CSV."""
    from ai_studio.listing_syndication import export_facebook_csv

    actor_email = _authenticated_user_email(request)
    vehicles = db_get_vehicles_by_email(actor_email)
    csv_data = export_facebook_csv(vehicles)
    return Response(content=csv_data, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=facebook_catalog.csv"})


@app.get("/api/syndication/xml-feed")
def syndication_xml(request: Request):
    """Export only the authenticated tenant's active inventory as XML."""
    from ai_studio.listing_syndication import export_xml_feed

    actor_email = _authenticated_user_email(request)
    vehicles = db_get_vehicles_by_email(actor_email)
    xml_data = export_xml_feed(vehicles)
    return Response(content=xml_data, media_type="application/xml",
                    headers={"Content-Disposition": "attachment; filename=inventory_feed.xml"})


@app.get("/api/syndication/json")
def syndication_json(request: Request):
    """Export only the authenticated tenant's active inventory as JSON."""
    from ai_studio.listing_syndication import export_json_api

    actor_email = _authenticated_user_email(request)
    vehicles = db_get_vehicles_by_email(actor_email)
    return export_json_api(vehicles)


@app.get("/api/vehicle/{vin}/craigslist")
def syndication_craigslist(vin: str, request: Request):
    """Generate Craigslist-ready HTML for an active vehicle the caller owns."""
    from ai_studio.listing_syndication import export_craigslist_html

    vin = validate_vehicle_id(vin)
    _require_vehicle_owner(request, vin)
    vehicle = db_get_vehicle(vin)
    if not vehicle or vehicle.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")

    html = export_craigslist_html(vehicle)
    return {"status": "ok", "vin": vin, "html": html}


@app.get("/api/vehicle/{vin}/embed")
def syndication_embed(vin: str, request: Request):
    """Generate an embeddable widget for an active vehicle the caller owns."""
    from ai_studio.listing_syndication import export_embed_html

    vin = validate_vehicle_id(vin)
    _require_vehicle_owner(request, vin)
    vehicle = db_get_vehicle(vin)
    if not vehicle or vehicle.get("status", "active") != "active":
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
_COMFYUI_PYTHON = os.environ.get("COMFYUI_PYTHON") or sys.executable
_COMMERCIAL_SCRIPT = str(BASE_DIR / "commercial_pipeline.py")
_COMMERCIAL_JOB_LOCK = _threading.Lock()


def _write_commercial_status(status_file: Path, payload: dict) -> None:
    """Atomically replace job status so pollers never read a partial JSON file."""
    temp_file = status_file.with_suffix(".json.tmp")
    temp_file.write_text(_json_mod.dumps(payload), encoding="utf-8")
    os.replace(temp_file, status_file)


def _run_commercial_pipeline_bg(
    vin: str, vehicle: dict, image_path: str, script: str, style: str,
    job_token: str, lock_file: Path,
):
    """Run commercial pipeline without retaining potentially large logs in RAM."""
    commercial_dir = Path(STORAGE_DIR) / vin / "commercial"
    commercial_dir.mkdir(parents=True, exist_ok=True)
    status_file = commercial_dir / "status.json"
    log_file = commercial_dir / "pipeline.log"
    try:
        _write_commercial_status(status_file, {
            "status": "running", "step": "starting", "progress": 0, "vin": vin
        })
        command = [
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
            "--dealer", str(vehicle.get("dealer_name") or vehicle.get("display_name") or "ABUZ8 Auto Demo"),
            "--style", style,
        ]
        if script:
            command.extend(["--script", script])
        with log_file.open("ab") as log:
            result = _subprocess.run(
                command, stdout=log, stderr=_subprocess.STDOUT,
                timeout=900, check=False
            )
        video_file = commercial_dir / "commercial.mp4"
        if result.returncode != 0 or not video_file.is_file() or video_file.stat().st_size <= 0:
            try:
                with log_file.open("rb") as log:
                    log.seek(max(0, log.seek(0, os.SEEK_END) - 8192))
                    error_text = log.read().decode("utf-8", errors="replace")[-500:]
            except OSError:
                error_text = "Commercial pipeline failed without a readable log"
            _write_commercial_status(status_file, {
                "status": "error", "vin": vin,
                "error": error_text or f"Pipeline exited with status {result.returncode}"
            })
        else:
            # The pipeline writes its own completed progress state. Ensure the
            # success artifact actually exists before considering the job done.
            print(f"[Commercial] Pipeline completed for VIN {vin}")
    except Exception as exc:
        try:
            _write_commercial_status(status_file, {"status": "error", "vin": vin, "error": str(exc)[:500]})
        except OSError:
            pass
        print(f"[Commercial] Error for VIN {vin}: {exc}")
    finally:
        try:
            if lock_file.read_text(encoding="utf-8").strip() == job_token:
                lock_file.unlink(missing_ok=True)
        except OSError:
            pass


def _validate_commercial_options(body: dict) -> tuple[str, str]:
    script = body.get("script", "")
    style = body.get("style", "cinematic")
    if not isinstance(script, str) or len(script) > 600:
        raise HTTPException(status_code=422, detail="Script must be text with at most 600 characters")
    if any(ord(char) < 32 and char not in "\\n\\r\\t" for char in script):
        raise HTTPException(status_code=422, detail="Script contains unsupported control characters")
    if not isinstance(style, str) or style not in {"cinematic", "social", "showroom"}:
        raise HTTPException(status_code=422, detail="Unsupported commercial style")
    return script.strip(), style


def _start_commercial_generation(vin: str, request: Request, script: str = "", style: str = "cinematic"):
    """Start a commercial job; callers run this blocking DB/network/filesystem work in a thread."""
    vin = validate_vehicle_id(vin)
    _require_vehicle_owner(request, vin)
    vehicle = db_get_vehicle(vin)
    if not vehicle or vehicle.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")
    interpreter = Path(_COMFYUI_PYTHON).expanduser()
    if not interpreter.is_file() and not shutil.which(_COMFYUI_PYTHON):
        raise HTTPException(status_code=503, detail="Commercial generation interpreter is not configured")
    if not Path(_COMMERCIAL_SCRIPT).is_file():
        raise HTTPException(status_code=503, detail="Commercial generation pipeline is not installed")
    if not _comfyui_is_available():
        raise HTTPException(status_code=503, detail="ComfyUI is unavailable; try again after it starts")

    storage_root = STORAGE_DIR.resolve()
    vehicle_dir = (storage_root / vin).resolve()
    if vehicle_dir.parent != storage_root or not vehicle_dir.is_dir():
        raise HTTPException(status_code=404, detail="Vehicle storage not found")
    commercial_path = vehicle_dir / "commercial"
    commercial_path.mkdir(parents=True, exist_ok=True)
    status_file = commercial_path / "status.json"

    def _find_image() -> Path | None:
        for subdir in ("spin", "exterior"):
            directory = (vehicle_dir / subdir).resolve()
            if directory.parent != vehicle_dir or not directory.is_dir():
                continue
            frames = sorted(
                path for path in directory.iterdir()
                if path.is_file()
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,180}\.(?:jpg|jpeg|png)", path.name, re.IGNORECASE)
                and path.resolve().parent == directory
            )
            if frames:
                return frames[max(0, len(frames) // 4)]
        thumbnail = vehicle_dir / "thumbnail.jpg"
        if thumbnail.is_file() and thumbnail.resolve().parent == vehicle_dir:
            return thumbnail.resolve()
        return None

    image_path = _find_image()
    if not image_path:
        raise HTTPException(status_code=400, detail="No vehicle images found. Upload exterior photos first.")

    # Coordinate duplicate requests across both threads and app worker processes.
    lock_file = commercial_path / ".job.lock"
    with _COMMERCIAL_JOB_LOCK:
        if lock_file.is_file():
            try:
                stale = time.time() - lock_file.stat().st_mtime >= 1800
            except OSError:
                stale = False
            if not stale:
                return {"status": "already_running", "message": "Commercial generation already in progress"}
            lock_file.unlink(missing_ok=True)

        job_token = uuid.uuid4().hex
        try:
            descriptor = os.open(str(lock_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return {"status": "already_running", "message": "Commercial generation already in progress"}
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as lock_handle:
                lock_handle.write(job_token)
                lock_handle.flush()
                os.fsync(lock_handle.fileno())
            _write_commercial_status(status_file, {
                "status": "running", "step": "starting", "progress": 0, "vin": vin
            })
            submission = _submit_background_job(
                f"commercial:{vin}",
                _run_commercial_pipeline_bg,
                vin, vehicle, str(image_path), script, style, job_token, lock_file,
            )
            if submission != "started":
                try:
                    if lock_file.read_text(encoding="utf-8").strip() == job_token:
                        lock_file.unlink(missing_ok=True)
                except OSError:
                    pass
                if submission == "busy":
                    raise HTTPException(
                        status_code=429,
                        detail="Background processing capacity is full",
                        headers={"Retry-After": "30"},
                    )
                return {"status": "already_running", "message": "Commercial generation already in progress"}
        except HTTPException:
            try:
                if lock_file.read_text(encoding="utf-8").strip() == job_token:
                    lock_file.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        except Exception as exc:
            try:
                if lock_file.read_text(encoding="utf-8").strip() == job_token:
                    lock_file.unlink(missing_ok=True)
            except OSError:
                pass
            raise HTTPException(status_code=500, detail="Could not start commercial generation") from exc

    return {
        "status": "started",
        "vin": vin,
        "message": "Commercial generation started. Poll /api/vehicle/{vin}/commercial-status for progress."
    }


@app.post("/api/vehicle/{vin}/generate-commercial")
async def generate_commercial_endpoint(vin: str, request: Request):
    """Backward-compatible vehicle-scoped commercial job endpoint."""
    body = await _read_json_object_limited(request, 8192)
    script, style = _validate_commercial_options(body)
    return await run_in_threadpool(
        _start_commercial_generation, vin, request, script, style
    )


@app.get("/api/vehicle/{vin}/commercial-status")
def commercial_status_endpoint(vin: str, request: Request):
    """Poll private generation status for an active vehicle owned by the requester."""
    vin = validate_vehicle_id(vin)
    _require_vehicle_owner(request, vin)
    vehicle = db_get_vehicle(vin)
    if not vehicle or vehicle.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")
    commercial_dir = (STORAGE_DIR / vin / "commercial").resolve()
    expected_parent = (STORAGE_DIR / vin).resolve()
    if commercial_dir.parent != expected_parent:
        raise HTTPException(status_code=400, detail="Invalid vehicle path")
    status_file = commercial_dir / "status.json"

    if not status_file.is_file():
        return {"status": "not_started", "vin": vin}
    try:
        if status_file.stat().st_size > 64 * 1024:
            raise ValueError("Status file too large")
        data = _json_mod.loads(status_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Invalid status format")
    except (OSError, ValueError, TypeError):
        return {"status": "error", "vin": vin, "error": "Generation status is unavailable"}

    if data.get("status") == "done":
        mp4_path = commercial_dir / "commercial.mp4"
        if mp4_path.is_file() and mp4_path.stat().st_size > 0 and mp4_path.resolve().parent == commercial_dir:
            data["url"] = f"/storage/{vin}/commercial/commercial.mp4"
            data["size_mb"] = round(mp4_path.stat().st_size / (1024 * 1024), 1)
        else:
            return {"status": "error", "vin": vin, "error": "Generated video is missing"}
    return data


@app.delete("/api/vehicle/{vin}/commercial")
def delete_commercial_endpoint(vin: str, request: Request):
    """Delete a generated commercial only when the requester owns the vehicle."""
    vin = validate_vehicle_id(vin)
    _require_vehicle_owner(request, vin)
    vehicle = db_get_vehicle(vin)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    commercial_dir = (STORAGE_DIR / vin / "commercial").resolve()
    if commercial_dir.parent != (STORAGE_DIR / vin).resolve():
        raise HTTPException(status_code=400, detail="Invalid vehicle path")
    if commercial_dir.exists():
        import shutil as _shutil
        _shutil.rmtree(commercial_dir)
    return {"status": "deleted", "vin": vin}


# =============================================================================
# ROUTES — PUBLIC URL
# =============================================================================
@app.get("/public-url")
def public_url_page():
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
def _run_photo_studio_job(
    vin: str, background: str, upscale: bool, kind: str, max_photos: int,
    require_background_removal: bool = False,
) -> None:
    """Run the shared PhotoProcessor into a staged directory and publish atomically."""
    stage_dir = None
    try:
        import tempfile
        from ai_studio import PhotoProcessor

        vehicle_dir = _safe_vehicle_directory(vin)
        exterior_dir = _ensure_safe_vehicle_subdir(vin, "exterior")
        studio_dir = _ensure_safe_vehicle_subdir(vin, "studio", create=True)
        photo_count = _count_matching_files(exterior_dir, (".jpg", ".jpeg", ".png", ".webp"))
        total = min(photo_count, max_photos)
        _write_pipeline_job(
            vin, kind, "running", step="processing", progress=5, total=total,
        )
        stage_dir = Path(tempfile.mkdtemp(prefix=".studio-stage-", dir=vehicle_dir))
        results = PhotoProcessor(
            background_name=background,
            upscale=upscale,
            require_background_removal=require_background_removal,
        ).process_batch(str(exterior_dir), str(stage_dir), max_photos=max_photos)
        succeeded = sum(result.get("status") == "ok" for result in results)
        degraded = sum(result.get("status") == "degraded" for result in results)
        failed = sum(result.get("status") == "error" for result in results)
        produced = succeeded + degraded
        if produced:
            _replace_directory(stage_dir, studio_dir)
            stage_dir = None
        if produced == 0:
            _write_pipeline_job(
                vin, kind, "error", step="failed", progress=100, total=total,
                failed=failed or total,
                message="No photos were processed; previous studio outputs were preserved.",
            )
            return

        final_status = "degraded" if degraded or failed else "completed"
        _write_pipeline_job(
            vin, kind, final_status, step="ready", progress=100, total=len(results),
            succeeded=succeeded, degraded=degraded, failed=failed,
            message=f"Processed {produced} photo(s); {failed} failed. Review all outputs before publishing.",
        )
    except Exception as exc:
        print(f"[Photo Studio] job failed for {vin}: {type(exc).__name__}: {exc}")
        try:
            _write_pipeline_job(
                vin, kind, "error", step="failed", progress=0,
                message="Photo processing failed; previous studio outputs were preserved.",
                failed=1,
            )
        except sqlite3.Error:
            pass
    finally:
        if stage_dir is not None and stage_dir.exists():
            shutil.rmtree(stage_dir, ignore_errors=True)


def _queue_photo_studio_job(
    vin: str,
    *,
    background: str,
    upscale: bool = False,
    kind: str = "photo_studio",
    max_photos: int = 72,
    require_rembg: bool = False,
) -> dict:
    """Validate a shared-engine job request and submit it to the bounded worker pool."""
    if kind not in {"photo_studio", "background_studio"}:
        raise ValueError("Unknown photo pipeline kind")
    if require_rembg and not REMBG_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="Background replacement requires an explicitly selected, operator-approved model; no photos were changed.",
        )
    if background != "none" and background_asset_path(background) is None:
        raise HTTPException(status_code=400, detail="Unknown packaged background")
    exterior_dir = _ensure_safe_vehicle_subdir(vin, "exterior")
    photo_count = _count_matching_files(exterior_dir, (".jpg", ".jpeg", ".png", ".webp"))
    if not photo_count:
        raise HTTPException(status_code=400, detail="No supported exterior photos found")
    if photo_count > 72:
        raise HTTPException(status_code=413, detail="Photo Studio is limited to 72 photos per vehicle job")
    total = min(photo_count, max_photos)
    _write_pipeline_job(
        vin, kind, "queued", step="queued", progress=0, total=total,
        message="Photo processing is queued.",
    )
    submission = _submit_background_job(
        f"{kind}:{vin}", _run_photo_studio_job,
        vin, background, upscale, kind, max_photos, require_rembg,
    )
    if submission == "busy":
        _write_pipeline_job(
            vin, kind, "error", step="queued", progress=0, total=total,
            message="Background processing capacity is full; retry in a moment.",
        )
        raise HTTPException(
            status_code=429, detail="Background processing capacity is full",
            headers={"Retry-After": "30"},
        )
    return {
        "status": submission,
        "vin": vin,
        "job_kind": kind,
        "photo_count": total,
        "background": background,
        "upscale": upscale,
        "status_url": f"/api/studio/vehicles/{vin}/jobs",
    }


@app.post("/api/vehicle/{vin}/process-photos")
async def process_vehicle_photos(vin: str, request: Request):
    """
    Run the shared PhotoProcessor: optional cut-out, packaged backdrop, enhancement,
    heuristic plate-blur attempt, and optional ComfyUI upscale. Missing rembg is
    explicitly reported as a degraded result, not successful background removal.
    """
    vin = validate_vehicle_id(vin)
    await run_in_threadpool(_require_vehicle_owner, request, vin)
    vehicle = await run_in_threadpool(db_get_vehicle, vin)
    if not vehicle or vehicle.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")

    body = await _read_json_object_limited(request, 4096)
    background = str(body.get("background", "studio_white")).strip()
    do_upscale = body.get("upscale", False)
    if not isinstance(do_upscale, bool):
        raise HTTPException(status_code=400, detail="upscale must be a boolean")
    if not re.fullmatch(r"[a-z0-9_-]{1,64}", background):
        raise HTTPException(status_code=400, detail="Invalid background name")

    return await run_in_threadpool(
        _queue_photo_studio_job, vin,
        background=background, upscale=do_upscale,
        kind="photo_studio", max_photos=72, require_rembg=False,
    )


@app.get("/api/vehicle/{vin}/studio-photos")
def get_studio_photos(vin: str, request: Request):
    """Get studio derivatives only for an active vehicle the requester owns."""
    vin = validate_vehicle_id(vin)
    _require_vehicle_owner(request, vin)
    vehicle = db_get_vehicle(vin)
    if not vehicle or vehicle.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")
    try:
        studio_dir = _ensure_safe_vehicle_subdir(vin, "studio")
    except HTTPException:
        return {"photos": [], "count": 0}
    try:
        with os.scandir(studio_dir) as entries:
            photos = heapq.nsmallest(
                72,
                (entry.name for entry in entries
                 if not entry.is_symlink() and entry.is_file(follow_symlinks=False)
                 and entry.name.startswith("studio_") and entry.name.endswith(".jpg")),
            )
    except OSError:
        photos = []
    return {
        "photos": [f"/storage/{vin}/studio/{filename}" for filename in photos],
        "count": len(photos),
    }


@app.get("/api/ai-studio/backgrounds")
def list_studio_backgrounds():
    """Expose the same packaged asset catalog used by the 360 renderer."""
    backgrounds = [
        {**asset, "path": asset["url"] or asset["thumbnail_url"]}
        for asset in list_background_assets()
    ]
    return {"backgrounds": backgrounds, "count": len(backgrounds)}


# Serve studio photos
app.mount("/storage", StaticFiles(directory=str(STORAGE_DIR)), name="vehicle-storage")

# Serve AI studio backgrounds
_AI_STUDIO_BG_DIR = BASE_DIR / "ai_studio" / "backgrounds"
if _AI_STUDIO_BG_DIR.exists():
    app.mount("/ai-studio-bg", StaticFiles(directory=str(_AI_STUDIO_BG_DIR)), name="ai-studio-bg")


# =============================================================================
# ROUTES — AI COMMERCIAL
# =============================================================================
COMFY_HOST = (os.getenv("COMFYUI_URL") or "http://127.0.0.1:8188").rstrip("/")


def _comfyui_is_available() -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"{COMFY_HOST}/system_stats", timeout=2) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


@app.get("/api/comfyui/status")
async def comfyui_status(request: Request):
    """Probe ComfyUI server-side and report owner-configured scene workflow state."""
    from ai_studio.background_studio import comfyui_scene_workflow_configured

    await run_in_threadpool(_authenticated_user_email, request)
    available = await run_in_threadpool(_comfyui_is_available)
    workflow_configured = await run_in_threadpool(comfyui_scene_workflow_configured)
    return {
        "available": available,
        "scene_workflow_configured": workflow_configured,
        "scene_generation_available": available and workflow_configured,
    }


@app.get("/ai-commercial")
def ai_commercial_page():
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
            <p style="color:#8b949e;margin-bottom:1rem;">Video generation runs through the server-configured ComfyUI service; this page never connects to localhost directly.</p>
            <pre>ComfyUI Status: <span id="comfyStatus">Checking...</span></pre>
            <p style="color:#8b949e;margin-top:1rem;font-size:0.9rem;">
                Configure the server-side <code>COMFYUI_URL</code> and <code>COMFYUI_PYTHON</code> settings.
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
        
        // Probe ComfyUI through the authenticated backend proxy (never browser localhost).
        fetch('/api/comfyui/status')
            .then(r => r.ok ? r.json() : Promise.reject())
            .then(data => document.getElementById('comfyStatus').textContent = data.available ? '✅ Connected' : '❌ Not running')
            .catch(() => document.getElementById('comfyStatus').textContent = '❌ Not configured');
        
        // Generate a tenant-owned job and poll its real server-side status.
        document.getElementById('generateForm').onsubmit = async e => {
            e.preventDefault();
            const btn = document.getElementById('generateBtn');
            const status = document.getElementById('status');
            const preview = document.getElementById('videoPreview');
            const vin = document.getElementById('vehicleSelect').value;
            if (!vin) {
                status.style.display = 'block';
                status.className = 'status error';
                status.textContent = 'Select a vehicle first.';
                return;
            }
            btn.disabled = true;
            btn.textContent = 'Starting...';
            status.style.display = 'block';
            status.className = 'status pending';
            status.textContent = 'Starting commercial generation...';
            preview.style.display = 'none';
            try {
                const response = await fetch('/api/generate-commercial', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        vin,
                        script: document.getElementById('scriptInput').value,
                        style: document.getElementById('styleSelect').value
                    })
                });
                const result = await response.json();
                if (!response.ok) throw new Error(result.detail || result.error || 'Request failed (' + response.status + ')');
                if (result.status !== 'started' && result.status !== 'already_running') {
                    throw new Error(result.detail || result.error || 'Commercial job did not start');
                }
                await pollCommercial(vin, btn, status, preview);
            } catch (err) {
                status.className = 'status error';
                status.textContent = err.message || 'Commercial request failed.';
                btn.disabled = false;
                btn.textContent = 'Generate Commercial';
            }
        };

        async function pollCommercial(vin, btn, status, preview) {
            const startedAt = Date.now();
            while (Date.now() - startedAt < 16 * 60 * 1000) {
                await new Promise(resolve => setTimeout(resolve, 2500));
                try {
                    const response = await fetch('/api/vehicle/' + encodeURIComponent(vin) + '/commercial-status');
                    const data = await response.json();
                    if (!response.ok) throw new Error(data.detail || 'Could not read job status');
                    if (data.status === 'done') {
                        if (typeof data.url !== 'string' || data.url !== '/storage/' + vin + '/commercial/commercial.mp4') {
                            throw new Error('Generated video URL is invalid.');
                        }
                        status.className = 'status ready';
                        status.textContent = 'Video generated successfully!' + (data.size_mb ? ' (' + data.size_mb + ' MB)' : '');
                        document.getElementById('videoPlayer').src = data.url;
                        document.getElementById('downloadLink').href = data.url;
                        preview.style.display = 'block';
                        btn.disabled = false;
                        btn.textContent = 'Generate Commercial';
                        return;
                    }
                    if (data.status === 'error') throw new Error(data.error || 'Commercial generation failed.');
                    const progress = Number.isFinite(Number(data.progress)) ? Number(data.progress) : 0;
                    status.className = 'status pending';
                    status.textContent = (data.step || 'Generating commercial...') + ' (' + progress + '%)';
                    btn.textContent = 'Generating ' + progress + '%';
                } catch (err) {
                    status.className = 'status error';
                    status.textContent = err.message || 'Could not read generation status.';
                    btn.disabled = false;
                    btn.textContent = 'Generate Commercial';
                    return;
                }
            }
            status.className = 'status error';
            status.textContent = 'Generation is taking longer than expected. Check the dashboard for status.';
            btn.disabled = false;
            btn.textContent = 'Generate Commercial';
        }
    </script>
</body>
</html>"""
    return HTMLResponse(html)


# =============================================================================
# API — AI COMMERCIAL
# =============================================================================
@app.post("/api/generate-commercial")
async def generate_commercial(request: Request):
    """Start an authenticated commercial job using the legacy page's form."""
    body = await _read_json_object_limited(request, 16 * 1024)
    vin_value = body.get("vin", "")
    if not isinstance(vin_value, str) or not vin_value.strip():
        raise HTTPException(status_code=422, detail="Select a vehicle")
    vin = validate_vehicle_id(vin_value)
    script, style = _validate_commercial_options(body)
    return await run_in_threadpool(
        _start_commercial_generation, vin, request, script, style
    )


# =============================================================================
# ROUTES — MOBILE CAPTURE (Phase 8)
# =============================================================================
@app.get("/capture-mobile")
def capture_mobile():
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
            if (photoFiles.length < 4) {{ alert('Please select at least 4 photos first.'); return; }}
            let usedVin = document.getElementById('vinInput').value.trim();
            const btn = document.getElementById('uploadBtn');
            btn.disabled = true;
            btn.textContent = 'Uploading...';
            try {{
                for (let i = 0; i < photoFiles.length; i++) {{
                    const pf = new FormData();
                    pf.append('vin', usedVin);
                    pf.append('photo', photoFiles[i]);
                    pf.append('index', i.toString());
                    const response = await fetch('/api/upload/exterior', {{ method: 'POST', body: pf, credentials: 'same-origin' }});
                    const data = await response.json().catch(() => ({{}}));
                    if (!response.ok) throw new Error(data.detail || ('Photo upload failed (' + response.status + ')'));
                    if (data.vin) usedVin = data.vin;
                    btn.textContent = 'Uploaded ' + (i + 1) + ' of ' + photoFiles.length;
                }}
                if (!usedVin) throw new Error('No vehicle identifier was returned by the upload service.');
                const start = await fetch('/api/finalize/' + encodeURIComponent(usedVin), {{ method: 'POST', credentials: 'same-origin' }});
                const startData = await start.json().catch(() => ({{}}));
                if (!start.ok) throw new Error(startData.detail || 'Could not start the 360 renderer');
                btn.textContent = 'Building 360...';
                let ready = false;
                for (let attempt = 0; attempt < 360; attempt++) {{
                    await new Promise(resolve => setTimeout(resolve, 1500));
                    const response = await fetch('/api/finalize-status/' + encodeURIComponent(usedVin), {{ credentials: 'same-origin' }});
                    const status = await response.json().catch(() => ({{}}));
                    if (!response.ok) throw new Error(status.detail || 'Could not read 360 job status');
                    if (status.status === 'error') throw new Error(status.message || '360 rendering failed');
                    if ((status.status === 'completed' || status.status === 'degraded') && status.ready) {{ ready = true; break; }}
                    btn.textContent = 'Building 360 · ' + (status.progress || 0) + '%';
                }}
                if (!ready) throw new Error('The render is still processing. Continue from Dealer Studio.');
                btn.textContent = 'Ready — review vehicle';
                setTimeout(() => window.location.href = '/view/' + encodeURIComponent(usedVin), 400);
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
def pricing_page():
    if not _pricing_is_approved():
        return HTMLResponse(
            "<!doctype html><html lang='en'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>LotVue360 pricing</title><main style='font:16px system-ui;max-width:42rem;margin:12vh auto;padding:2rem'>"
            "<h1>Pricing is being finalized</h1><p>Plans will be published after the offer is confirmed.</p>"
            "<a href='/'>Return to LotVue360</a></main></html>",
            status_code=200,
        )
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
                <div class="plan-price">__SINGLE_PRICE__</div>
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
                <div class="plan-price">__STARTER_PRICE__</div>
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
                <div class="plan-price">__DEALERSHIP_PRICE__<span>/mo</span></div>
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
    for placeholder, plan_id in (
        ("__SINGLE_PRICE__", "single"),
        ("__STARTER_PRICE__", "starter"),
        ("__DEALERSHIP_PRICE__", "dealership"),
    ):
        amount = float(PLANS[plan_id]["price"])
        html = html.replace(placeholder, f"${amount:,.2f}")
    return HTMLResponse(html)


# =============================================================================
# ROUTES — AUTH PAGES (Phase 7)
# =============================================================================
@app.get("/register")
def register_page():
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
                <input type="password" name="password" required minlength="12" maxlength="1024" autocomplete="new-password" placeholder="At least 12 characters">
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
def login_page():
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
async def submit_public_lead(request: Request):
    """Submit a bounded public inquiry; only its vehicle's dealer receives dealer data."""
    from ai_studio.lead_engine import score_lead, generate_lead_response, schedule_follow_up_sequence, build_digital_dossier
    _check_public_lead_rate(request)
    raw_body = await _read_limited_body(request, 8192)
    try:
        body = json.loads(raw_body or b"{}")
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")
    name = re.sub(r"[\x00-\x1f\x7f]", "", str(body.get("name", "")))[:120].strip()
    email = re.sub(r"[\x00-\x1f\x7f]", "", str(body.get("email", "")))[:254].strip().lower()
    phone = re.sub(r"[\x00-\x1f\x7f]", "", str(body.get("phone", "")))[:40].strip()
    vin = validate_vehicle_id(str(body.get("vin", ""))) if body.get("vin") else ""
    message = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(body.get("message", "")))[:2000].strip()
    source = str(body.get("source", "website"))[:32].strip().lower()
    wants_test_drive = int(bool(body.get("wants_test_drive", 0)))
    wants_video = int(bool(body.get("wants_video", 0)))
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    if not email and not phone:
        raise HTTPException(status_code=400, detail="Email or phone required")
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=400, detail="Enter a valid email address")
    vehicle = await run_in_threadpool(db_get_vehicle, vin) if vin else None
    if vin and (not vehicle or vehicle.get("status") != "active"):
        raise HTTPException(status_code=404, detail="Vehicle not found")

    source = source if source in {"website", "viewer", "vdp", "embed", "widget"} else "website"
    dealer_id = 0
    tenant_email = str((vehicle or {}).get("user_email", "") or "").strip().lower()
    dealer = await run_in_threadpool(db_get_dealer_by_email, tenant_email) if tenant_email else None
    if dealer:
        dealer_id = int(dealer["id"])
    lead_id = await run_in_threadpool(
        _insert_public_lead,
        dealer_id,
        vin,
        name,
        email,
        phone,
        message,
        source,
        wants_test_drive,
        wants_video,
    )

    lead = {
        "id": lead_id, "name": name, "email": email, "phone": phone,
        "vin": vin, "message": message, "source": source,
        "wants_test_drive": wants_test_drive, "wants_video": wants_video,
    }

    # Auto-score
    vehicle = await run_in_threadpool(db_get_vehicle, vin) if vin else None
    scoring = score_lead(lead, vehicle)

    # Local LLM / SMTP model calls are synchronous; keep them off the ASGI loop.
    response_msg = await run_in_threadpool(generate_lead_response, lead, vehicle)

    # Schedule follow-up sequence
    follow_ups = await run_in_threadpool(
        schedule_follow_up_sequence, lead_id, scoring["grade"]
    )

    # Build digital dossier if vehicle exists
    dossier = None
    if vehicle:
        dossier = await run_in_threadpool(build_digital_dossier, lead, vehicle)

    # Dealer-only intelligence goes back only to the actual vehicle tenant.
    is_dealer = False
    if vin:
        try:
            await run_in_threadpool(_require_vehicle_owner, request, vin)
            is_dealer = True
        except HTTPException:
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


@app.get("/api/leads/{lead_id}")
def get_lead_detail(lead_id: int, request: Request):
    """Get full lead detail with scoring and follow-up history."""
    from ai_studio.lead_engine import score_lead, build_digital_dossier
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    lead = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not lead:
        conn.close()
        raise HTTPException(status_code=404, detail="Lead not found")
    lead = dict(lead)
    try:
        _require_lead_owner(request, lead)
    except Exception:
        conn.close()
        raise

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
    except sqlite3.OperationalError:
        pass

    # Get appointments
    appointments = []
    try:
        rows = conn.execute("SELECT * FROM appointments WHERE lead_id=? ORDER BY scheduled_at", (lead_id,)).fetchall()
        appointments = [dict(r) for r in rows]
    except sqlite3.OperationalError:
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
def generate_lead_response_endpoint(lead_id: int, request: Request, response_type: str = "initial"):
    """Generate AI response for a lead (SMS + email)."""
    from ai_studio.lead_engine import generate_lead_response
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    lead = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    conn.close()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    lead = dict(lead)
    _require_lead_owner(request, lead)
    vehicle = db_get_vehicle(lead.get("vin", "")) if lead.get("vin") else None
    response = generate_lead_response(lead, vehicle, response_type=response_type)
    return {"status": "ok", "response": response}


@app.get("/api/lead-analytics")
def lead_analytics(request: Request):
    """Get lead funnel analytics for the authenticated tenant."""
    from ai_studio.lead_engine import get_lead_analytics
    email = _authenticated_user_email(request)
    owner_email = os.getenv("LOTVUE_OWNER_EMAIL", "").strip().lower()
    analytics = get_lead_analytics(None if owner_email and email == owner_email else email)
    return {"status": "ok", "analytics": analytics}


@app.get("/api/appointments/slots")
def available_appointment_slots(request: Request, days: int = 7):
    """Get available appointment time slots for the authenticated tenant."""
    from ai_studio.lead_engine import get_available_slots
    email = _authenticated_user_email(request)
    owner_email = os.getenv("LOTVUE_OWNER_EMAIL", "").strip().lower()
    days = max(1, min(days, 30))
    slots = get_available_slots(days_ahead=days, user_email=None if owner_email and email == owner_email else email)
    return {"status": "ok", "slots": slots, "count": len(slots)}


# C0 control characters (keeping TAB/LF/CR) plus DEL; stripped from free-text notes.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@app.post("/api/appointments/book")
async def book_appointment_endpoint(request: Request):
    """Book an appointment only for a lead owned by the signed-in dealer."""
    from ai_studio.lead_engine import book_appointment
    raw = await _read_limited_body(request, 4096)
    try:
        body = json.loads(raw or b"{}")
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")
    try:
        lead_id = int(body.get("lead_id", 0))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Valid lead_id required") from exc
    slot = str(body.get("datetime", ""))[:64]
    apt_type = str(body.get("type", "test_drive"))
    notes = _CONTROL_CHARS_RE.sub("", str(body.get("notes", "")))[:500]
    if lead_id <= 0 or not slot:
        raise HTTPException(status_code=400, detail="lead_id and datetime required")
    if apt_type not in {"test_drive", "viewing", "trade_eval", "financing"}:
        raise HTTPException(status_code=400, detail="Unsupported appointment type")
    lead = await run_in_threadpool(_read_lead_record, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    actor_email = await run_in_threadpool(_require_lead_owner, request, lead)
    result = await run_in_threadpool(
        book_appointment, lead_id, slot, apt_type, notes, user_email=actor_email
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {"status": "ok", "appointment": result}


@app.get("/api/followup-queue")
def follow_up_queue(request: Request):
    """Get pending follow-ups for the authenticated tenant."""
    from ai_studio.lead_engine import get_follow_up_queue
    email = _authenticated_user_email(request)
    owner_email = os.getenv("LOTVUE_OWNER_EMAIL", "").strip().lower()
    queue = get_follow_up_queue(None if owner_email and email == owner_email else email)
    return {"status": "ok", "queue": queue, "count": len(queue)}


@app.get("/api/dossier/{vin}")
def get_dossier(vin: str, request: Request, lead_id: int = None):
    """Get an owned vehicle's dossier, optionally personalized for its own lead."""
    from ai_studio.lead_engine import build_digital_dossier
    vin = validate_vehicle_id(vin)
    _require_vehicle_owner(request, vin)
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
            if str(lead.get("vin", "") or "").upper() != vin:
                raise HTTPException(status_code=404, detail="Lead not found")
            _require_lead_owner(request, lead)
    dossier = build_digital_dossier(lead, vehicle)
    return {"status": "ok", "dossier": dossier}


# =============================================================================
# MODULE 10 — AI VIDEO ADS + PERSONALIZED LEAD RESPONSE
# Cloning best of Impel AI + AIVO360
# =============================================================================

@app.post("/api/vehicle/{vin}/video-ad")
async def generate_video_ad(vin: str, request: Request):
    """Generate a bounded video ad script for an active vehicle the caller owns."""
    from ai_studio.video_ads import generate_video_ad_script
    vin = validate_vehicle_id(vin)
    await run_in_threadpool(_require_vehicle_owner, request, vin)
    vehicle = await run_in_threadpool(db_get_vehicle, vin)
    if not vehicle or vehicle.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")
    body = await _read_json_object_limited(request, 8192)
    ad_type = body.get("type", "listing")
    if not isinstance(ad_type, str) or ad_type not in {"listing", "social", "personalized"}:
        raise HTTPException(status_code=422, detail="Unsupported video ad type")
    script = await run_in_threadpool(generate_video_ad_script, vehicle, ad_type=ad_type)
    return {"status": "ok", "script": script}


@app.post("/api/leads/{lead_id}/personalized-video")
def personalized_video_response(lead_id: int, request: Request):
    """Generate personalized video response for a lead (Impel AI style)."""
    from ai_studio.video_ads import generate_personalized_response
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    lead = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    conn.close()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    lead = dict(lead)
    _require_lead_owner(request, lead)
    vehicle = db_get_vehicle(lead.get("vin", "")) if lead.get("vin") else None
    if not vehicle:
        raise HTTPException(status_code=404, detail="No vehicle linked to this lead")
    result = generate_personalized_response(lead, vehicle)
    return {"status": "ok", **result}


@app.get("/api/vehicle/{vin}/video-ads")
def list_video_ads(vin: str, request: Request):
    """List generated ads for an active vehicle the caller owns."""
    from ai_studio.video_ads import get_video_ad_status
    vin = validate_vehicle_id(vin)
    _require_vehicle_owner(request, vin)
    vehicle = db_get_vehicle(vin)
    if not vehicle or vehicle.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")
    result = get_video_ad_status(vin)
    return {"status": "ok", **result}


# =============================================================================
# MODULE 11 — AI BACKGROUND STUDIO
# Cloning best of autofox + AIVO360
# =============================================================================

@app.get("/api/backgrounds/scenes")
def list_background_scenes():
    """List all available background scenes."""
    from ai_studio.background_studio import list_scenes
    scenes = list_scenes()
    return {"status": "ok", "scenes": scenes}


@app.post("/api/vehicle/{vin}/background/process")
async def apply_background(vin: str, request: Request):
    """Queue strict scene replacement through the shared PhotoProcessor engine."""
    from ai_studio.background_studio import PREMIUM_SCENES

    vin = validate_vehicle_id(vin)
    await run_in_threadpool(_require_vehicle_owner, request, vin)
    vehicle = await run_in_threadpool(db_get_vehicle, vin)
    if not vehicle or vehicle.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")
    body = await _read_json_object_limited(request, 8192)
    scene_id = body.get("scene", "clean-studio")
    if not isinstance(scene_id, str) or scene_id not in PREMIUM_SCENES:
        raise HTTPException(status_code=400, detail="Unknown background scene")
    background_slug = scene_asset_slug(scene_id)
    if not background_slug:
        raise HTTPException(status_code=400, detail="Scene has no packaged background asset")
    return await run_in_threadpool(
        _queue_photo_studio_job, vin,
        background=background_slug, upscale=False,
        kind="background_studio", max_photos=20, require_rembg=True,
    )


@app.post("/api/photo/remove-background")
async def remove_bg_endpoint(file: UploadFile = File(...)):
    """Remove a background from a bounded upload without trusting its filename."""
    from ai_studio.background_studio import remove_background
    from rembg_runtime import ModelNotConfiguredError
    from starlette.background import BackgroundTask
    import tempfile

    raw = await _read_upload_limited(file, MAX_PHOTO_MB * 1024 * 1024)
    await run_in_threadpool(_verify_uploaded_image, raw, Path(file.filename or "image").name)

    temp_dir = Path(await run_in_threadpool(
        tempfile.mkdtemp, prefix="lotvue-background-"
    ))
    input_path = temp_dir / "input-image"
    try:
        await run_in_threadpool(input_path.write_bytes, raw)
        del raw
        output = await run_in_threadpool(remove_background, str(input_path))
    except (ModuleNotFoundError, ModelNotConfiguredError) as exc:
        await run_in_threadpool(shutil.rmtree, temp_dir, ignore_errors=True)
        raise HTTPException(
            status_code=503,
            detail="No explicitly approved background-removal model is configured",
        ) from exc
    except Exception as exc:
        await run_in_threadpool(shutil.rmtree, temp_dir, ignore_errors=True)
        raise HTTPException(status_code=422, detail="Could not remove this image background") from exc
    return FileResponse(
        output,
        media_type="image/png",
        filename="no_background.png",
        background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True),
    )


@app.post("/api/photo/composite")
async def composite_endpoint(request: Request):
    """Run an explicit local composite or configured ComfyUI scene workflow."""
    from ai_studio.background_studio import (
        PREMIUM_SCENES,
        ComfyUIWorkflowConfigurationError,
        composite_on_scene,
        generate_scene_with_comfyui,
    )

    body = await _read_json_object_limited(request, 8192)
    image_ref = body.get("image_path")
    scene_id = body.get("scene", "clean-studio")
    engine = body.get("engine", "local")
    if not isinstance(image_ref, str) or not image_ref or len(image_ref) > 2048:
        raise HTTPException(status_code=400, detail="image_path required")
    if not isinstance(scene_id, str) or scene_id not in PREMIUM_SCENES:
        raise HTTPException(status_code=400, detail="Unknown background scene")
    if not isinstance(engine, str) or engine not in {"local", "comfyui"}:
        raise HTTPException(status_code=400, detail="engine must be 'local' or 'comfyui'")

    vin, image_path = await run_in_threadpool(_resolve_vehicle_image_ref, image_ref)
    await run_in_threadpool(_require_vehicle_owner, request, vin)
    output_dir = await run_in_threadpool(
        _ensure_safe_vehicle_subdir, vin, "studio", create=True
    )
    output_suffix = "_comfyui" if engine == "comfyui" else ""
    output_path = output_dir / f"{image_path.stem}_{scene_id}{output_suffix}.jpg"
    renderer = generate_scene_with_comfyui if engine == "comfyui" else composite_on_scene
    scene_slot_acquired = False
    if engine == "comfyui":
        scene_slot_acquired = _COMFYUI_SCENE_JOB_SLOTS.acquire(blocking=False)
        if not scene_slot_acquired:
            raise HTTPException(
                status_code=429,
                detail="ComfyUI scene-generation capacity is full; retry later.",
                headers={"Retry-After": "30"},
            )
    try:
        await run_in_threadpool(renderer, str(image_path), scene_id, str(output_path))
    except ComfyUIWorkflowConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail="ComfyUI scene generation timed out; no generated output was published.",
        ) from exc
    except (RuntimeError, OSError) as exc:
        if engine == "comfyui":
            raise HTTPException(
                status_code=503,
                detail="ComfyUI scene generation failed; no generated output was published.",
            ) from exc
        raise HTTPException(status_code=422, detail="Could not composite this image") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Could not process this image") from exc
    finally:
        if scene_slot_acquired:
            _COMFYUI_SCENE_JOB_SLOTS.release()

    return {
        "status": "ok",
        "engine": "comfyui_workflow" if engine == "comfyui" else "local_image_composite",
        "ai_generated": engine == "comfyui",
        "review_required": engine == "comfyui",
        "output": f"/storage/{vin}/studio/{output_path.name}",
    }


# =============================================================================
# MODULE 12 — REPUTATION MANAGER
# Cloning 360autoai reputation management
# =============================================================================

@app.get("/api/reviews")
def list_reviews(request: Request, platform: str = None, status: str = None, limit: int = 50):
    """List the platform-wide reputation record for its configured owner."""
    _require_platform_owner(request)
    if platform is not None and len(platform) > 40:
        raise HTTPException(status_code=400, detail="Platform filter is too long")
    if status is not None and len(status) > 40:
        raise HTTPException(status_code=400, detail="Status filter is too long")
    limit = max(1, min(int(limit), 200))
    from ai_studio.reputation_manager import get_reviews
    reviews = get_reviews(platform, status, limit)
    return {"status": "ok", "reviews": reviews, "count": len(reviews)}


@app.post("/api/reviews")
async def add_review_endpoint(request: Request):
    """Add a bounded review and get an auto-generated response."""
    await run_in_threadpool(_require_platform_owner, request)
    raw = await _read_limited_body(request, 16 * 1024)
    try:
        body = json.loads(raw or b"{}")
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")
    from ai_studio.reputation_manager import add_review
    platform = str(body.get("platform", "google")).strip().lower()
    if platform not in {"google", "facebook", "yelp", "dealer_rater", "cars.com", "edmunds", "other"}:
        raise HTTPException(status_code=400, detail="Unsupported review platform")
    reviewer = "".join(ch for ch in str(body.get("reviewer_name", "Customer")) if ord(ch) >= 32 and ord(ch) != 127)[:120].strip()
    try:
        rating = int(body.get("rating", 5))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Rating must be an integer from 1 to 5") from exc
    text = "".join(ch for ch in str(body.get("review_text", "")) if ord(ch) in (9, 10, 13) or ord(ch) >= 32 and ord(ch) != 127)[:5000]
    if not reviewer or not 1 <= rating <= 5 or len(text) > 5000:
        raise HTTPException(status_code=400, detail="Invalid reviewer, rating, or review length")
    result = await run_in_threadpool(add_review, platform, reviewer, rating, text)
    return {"status": "ok", **result}


@app.post("/api/reviews/{review_id}/respond")
async def respond_review(review_id: int, request: Request):
    """Save a bounded response to a review."""
    await run_in_threadpool(_require_platform_owner, request)
    raw = await _read_limited_body(request, 8192)
    try:
        body = json.loads(raw or b"{}")
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")
    from ai_studio.reputation_manager import respond_to_review
    response_text = "".join(ch for ch in str(body.get("response", "")) if ord(ch) in (9, 10, 13) or ord(ch) >= 32 and ord(ch) != 127).strip()[:4000]
    if not response_text or len(response_text) > 4000 or review_id < 1:
        raise HTTPException(status_code=400, detail="Response text is required and must be at most 4,000 characters")
    result = await run_in_threadpool(respond_to_review, review_id, response_text)
    if result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])
    return {"status": "ok", **result}


@app.get("/api/reviews/{review_id}/generate-response")
def generate_review_response(review_id: int, request: Request):
    """Auto-generate an AI response for a review."""
    _require_platform_owner(request)
    if review_id < 1:
        raise HTTPException(status_code=400, detail="Invalid review id")
    from ai_studio.reputation_manager import generate_review_response as gen_resp
    # reviews live in lotvue.db (reputation_manager's DB), not vehicles.db
    from ai_studio.reputation_manager import DB_PATH as REVIEW_DB_PATH
    conn = sqlite3.connect(str(REVIEW_DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Review not found")
    review = dict(row)
    response = gen_resp(review)
    return {"status": "ok", "suggested_response": response}


@app.get("/api/reputation/dashboard")
def reputation_dashboard(request: Request):
    """Get platform-wide reputation analytics for the configured owner."""
    _require_platform_owner(request)
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
def splat3d_viewer(vin: str):
    from fastapi.responses import RedirectResponse
    vin = validate_vehicle_id(vin)
    vehicle = db_get_vehicle(vin)
    if not vehicle or vehicle.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")
    return RedirectResponse(f"/static/splat_viewer.html?vin={quote(vin, safe='')}")


@app.get("/3d-studio")
def studio_3d_page():
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
<div style="margin-top:1.2rem;padding-top:1rem;border-top:1px solid #E3EAF2">
  <button type="button" class="ghost" onclick="goInterior()">🛋 Interior 360 + hotspots →</button>
  <span class="hint" style="display:block;margin-top:.4rem">Add a tappable interior 360 for this vehicle (separate from the exterior turntable).</span>
</div>
<div class="tips"><b>You'll always get a 360° spin.</b> For full <b>orbit-anywhere 3D</b>: capture a slow, complete circle around the car — a 20–40s video, or 30+ overlapping photos — keep the whole car + some ground in frame, move smoothly, even lighting. ~5–15 min on the GPU.</div>
</div><script>
let vin='';
function goInterior(){
  const v=(document.getElementById('vin').value||'').trim();
  if(!v){ alert('Enter the VIN/ID first'); return; }
  location.href='/interior-studio/'+encodeURIComponent(v);
}
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
def vehicle_3d_status(vin: str, request: Request):
    vin = validate_vehicle_id(vin)
    _require_vehicle_owner(request, vin)
    vehicle = db_get_vehicle(vin)
    if not vehicle or vehicle.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")
    sp = STORAGE_DIR / vin / "splat"
    data = {}
    st = sp / "status.json"
    if st.is_file():
        try:
            if st.stat().st_size > 64 * 1024:
                raise ValueError("Status file too large")
            parsed = json.loads(st.read_text(encoding="utf-8"))
            data = parsed if isinstance(parsed, dict) else {}
        except (OSError, ValueError, TypeError):
            data = {"status": "error", "error": "3D job status is unavailable"}
    data["ready"] = (sp / "scene.ply").exists()
    if data["ready"]:
        data.setdefault("url", f"/storage/{vin}/splat/scene.ply")
        data.setdefault("viewer", f"/splat3d/{vin}")
    if (STORAGE_DIR / vin / "spin" / "spin.mp4").exists():
        data["spin_mp4"] = f"/storage/{vin}/spin/spin.mp4"
    return data


@app.post("/api/vehicle/{vin}/generate-3d")
async def generate_3d(request: Request, vin: str, video: Optional[UploadFile] = File(None),
                      photos: Optional[List[UploadFile]] = File(None),
                      steps: str = Form("7000"),
                      style: str = Form("natural"),
                      backdrop: str = Form("light"),
                      force: str = Form("")):  # "1" bypasses the capture quality gate
    from validate_video import validate_dir as _gate_dir, \
        validate_video_file as _gate_video, gate_summary as _gate_sum
    vin = validate_vehicle_id(vin)
    await run_in_threadpool(_require_vehicle_owner, request, vin)
    vehicle = await run_in_threadpool(db_get_vehicle, vin)
    if not vehicle or vehicle.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")
    style = (style or "natural").strip().lower()
    if style not in {"natural", "studio", "branded"}:
        raise HTTPException(status_code=422, detail="Unsupported 3D render style")
    backdrop = (backdrop or "light").strip().lower()
    if backdrop not in ("light", "white", "silver", "warm", "blue"):
        backdrop = "light"
    vdir = await run_in_threadpool(_safe_vehicle_directory, vin)
    await run_in_threadpool(_ensure_safe_vehicle_subdir, vin, "splat", create=True)
    if _background_job_running(f"gaussian3d:{vin}"):
        return JSONResponse({"error": "A 3D reconstruction is already running for this vehicle"}, status_code=409)
    try:
        steps_i = max(2000, min(int(steps), 30000))
    except ValueError:
        steps_i = 7000

    # Either a walkaround VIDEO or a SET OF PHOTOS — both always produce a 360 spin,
    # and auto-upgrade to true 3D when the capture supports it.
    real_photos = [p for p in (photos or []) if getattr(p, "filename", "")]
    if real_photos:
        if len(real_photos) < 4:
            raise HTTPException(status_code=400, detail="Upload at least 4 photos.")
        if len(real_photos) > 72:
            raise HTTPException(status_code=413, detail="Upload no more than 72 photos per 3D job")
        import tempfile
        pdir = await run_in_threadpool(
            _ensure_safe_vehicle_subdir, vin, "_3dphotos", create=True
        )
        # Build and validate beside the live directory, then atomically swap it.
        # A rejected retry must not erase the last usable capture set.
        stage_dir = Path(await run_in_threadpool(
            tempfile.mkdtemp, prefix=".3dphotos-stage-", dir=vdir
        ))
        saved = 0
        batch_bytes = 0
        try:
            for i, photo in enumerate(real_photos):
                data = await _read_upload_limited(photo, MAX_PHOTO_MB * 1024 * 1024)
                batch_bytes += len(data)
                if batch_bytes > MAX_PHOTO_BATCH_MB * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="Photo batch exceeds the total size limit")
                filename = Path(photo.filename or "photo").name
                saved_photo = await run_in_threadpool(
                    _process_3d_photo, data, filename, stage_dir / f"p_{i+1:04d}.jpg"
                )
                if saved_photo:
                    saved += 1
                del data
            if saved < 4:
                raise HTTPException(status_code=400, detail="Upload at least 4 readable photos with a 256px minimum dimension.")
            # ── P0 QUALITY GATE — the exact input class that caused COLMAP 2/N ────
            gate = await run_in_threadpool(_gate_dir, stage_dir)
            if gate["verdict"] == "FAIL" and force != "1":
                raise HTTPException(status_code=422, detail={
                    "error": "capture_rejected", "gate": _gate_sum(gate)})
            if gate["verdict"] == "FAIL":
                print(f"[gate] FORCED past FAIL for {vin} (photos): {gate['reasons']}")
            await run_in_threadpool(_replace_directory, stage_dir, pdir)
        finally:
            await run_in_threadpool(_remove_directory_if_exists, stage_dir)
        mode_args = ["--images", str(pdir)]
    elif video is not None and getattr(video, "filename", ""):
        walk = vdir / "walkaround.mp4"
        await _save_upload_limited(video, walk, MAX_VIDEO_MB * 1024 * 1024)
        # ── P0 QUALITY GATE — reject before COLMAP/Brush burn GPU-hours ───────
        try:
            gate = await run_in_threadpool(_gate_video, walk)
        except Exception as e:
            gate = {"verdict": "FAIL", "reasons": [f"unreadable video: {e}"],
                    "frames": 0, "checks": {}}
        if gate["verdict"] == "FAIL" and force != "1":
            await run_in_threadpool(_unlink_file, walk)
            raise HTTPException(status_code=422, detail={
                "error": "capture_rejected", "gate": _gate_sum(gate)})
        if gate["verdict"] == "FAIL":
            print(f"[gate] FORCED past FAIL for {vin} (video): {gate['reasons']}")
        mode_args = ["--video", str(walk)]
    else:
        raise HTTPException(status_code=400, detail="Upload a walkaround video or 4+ photos.")

    await run_in_threadpool(
        _write_gaussian_status, vin,
        {"status": "running", "step": "queued", "progress": 1, "vin": vin},
    )
    submission = _submit_background_job(
        f"gaussian3d:{vin}", _run_gaussian_3d_job, vin, mode_args, steps_i, style, backdrop
    )
    if submission != "started":
        await run_in_threadpool(_write_gaussian_status, vin, {
            "status": "error", "step": "queued", "progress": 0, "vin": vin,
            "error": "A 3D reconstruction is already running" if submission == "already_running"
                     else "Background processing capacity is full; retry shortly",
        })
        if submission == "already_running":
            return JSONResponse({"error": "A 3D reconstruction is already running for this vehicle"}, status_code=409)
        raise HTTPException(status_code=429, detail="Background processing capacity is full",
                            headers={"Retry-After": "30"})
    return {"status": "started", "vin": vin, "style": style, "backdrop": backdrop,
            "status_url": f"/api/vehicle/{vin}/3d-status", "viewer": f"/splat3d/{vin}",
            "gate": _gate_sum(gate)}


# ── Dealer branding assets (for the "branded" turntable style) ───────────────
@app.get("/api/brand/assets")
def brand_assets_status(request: Request):
    """Report platform-wide brand assets to the configured owner only."""
    _require_platform_owner(request)
    bdir = STORAGE_DIR / "_brand"
    bg = None
    for n in ("background.jpg", "background.jpeg", "background.png"):
        if (bdir / n).exists():
            bg = f"/storage/_brand/{n}"; break
    logo = "/storage/_brand/logo.png" if (bdir / "logo.png").exists() else None
    return {"background": bg, "logo": logo}


@app.post("/api/brand/assets")
async def brand_assets_upload(request: Request,
                              background: Optional[UploadFile] = File(None),
                              logo: Optional[UploadFile] = File(None)):
    """Save platform-wide branded turntable assets; only the configured owner may change them."""
    await run_in_threadpool(_require_platform_owner, request)
    bdir = await run_in_threadpool(_ensure_brand_storage_dir)
    saved = {}
    if background is not None and getattr(background, "filename", ""):
        data = await _read_upload_limited(background, MAX_PHOTO_MB * 1024 * 1024)
        await run_in_threadpool(
            _process_brand_background, data, Path(background.filename or "background").name,
            bdir / "background.jpg",
        )
        for name in ("background.jpeg", "background.png"):
            await run_in_threadpool(_unlink_file, bdir / name)
        del data
        saved["background"] = "/storage/_brand/background.jpg"
    if logo is not None and getattr(logo, "filename", ""):
        data = await _read_upload_limited(logo, MAX_PHOTO_MB * 1024 * 1024)
        await run_in_threadpool(
            _process_brand_logo, data, Path(logo.filename or "logo").name,
            bdir / "logo.png",
        )
        del data
        saved["logo"] = "/storage/_brand/logo.png"
    if not saved:
        raise HTTPException(status_code=400, detail="Upload a background and/or a logo image")
    return {"status": "ok", "saved": saved}


@app.post("/api/brand/clear")
def brand_assets_clear(request: Request, target: str = Form("all")):
    """Remove platform-wide branding assets; only the configured owner may change them."""
    _require_platform_owner(request)
    if target not in {"all", "background", "logo"}:
        raise HTTPException(status_code=400, detail="target must be all, background, or logo")
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


# ── Interior 360 studio (dealer): upload interior pano + place feature hotspots ──
_INTERIOR_STUDIO_HTML = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LotVue360 — Interior 360 Studio</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/pannellum@2.5.6/build/pannellum.css"/>
<script src="https://cdn.jsdelivr.net/npm/pannellum@2.5.6/build/pannellum.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box;font-family:-apple-system,'Segoe UI',Roboto,sans-serif}
body{background:#F8FAFC;color:#2C3E50;padding:1.5rem 1rem}
.wrap{max-width:900px;margin:0 auto;background:#fff;border:1px solid #E3EAF2;border-radius:16px;padding:1.6rem}
h1{font-size:1.4rem;color:#2E75B6}h1 span{color:#C8A55C}
p.sub{color:#7A8CA0;margin:.3rem 0 1.2rem;font-size:.92rem}
label{display:block;font-weight:600;margin:.8rem 0 .35rem;font-size:.92rem}
input[type=text],input[type=file]{width:100%;padding:.6rem;border:1.5px solid #DDE5EE;border-radius:10px;font-size:.95rem}
button{background:#2E75B6;color:#fff;border:0;padding:.7rem 1.1rem;border-radius:10px;font-size:.95rem;font-weight:700;cursor:pointer}
button:disabled{background:#9bb6d0;cursor:wait}
button.ghost{background:#fff;color:#2E75B6;border:1.5px solid #2E75B6}
button.gold{background:#C8A55C}
.row{display:flex;gap:.6rem;align-items:flex-end;flex-wrap:wrap}
.row>div{flex:1;min-width:160px}
#pano{width:100%;height:460px;border-radius:12px;overflow:hidden;background:#0f2040;margin-top:1rem;display:none}
.bar{display:flex;gap:.6rem;align-items:center;margin-top:.8rem;flex-wrap:wrap}
.hint{font-size:.82rem;color:#7A8CA0}
.addform{display:none;margin-top:.8rem;padding:1rem;border:1.5px dashed #C8A55C;border-radius:12px;background:#FFFDF7}
.pins{margin-top:1rem}
.pin{display:flex;align-items:center;gap:.6rem;padding:.55rem .7rem;border:1px solid #E3EAF2;border-radius:10px;margin-bottom:.5rem}
.pin b{color:#2E75B6}.pin .d{color:#7A8CA0;font-size:.85rem;flex:1}
.pin button{background:#fff;color:#c0392b;border:1.5px solid #c0392b;padding:.35rem .7rem;font-size:.8rem}
.empty{color:#7A8CA0;padding:1rem;text-align:center}
a.preview{display:inline-block;margin-left:auto;color:#2E75B6;font-weight:700;text-decoration:none}
.addmode{outline:3px solid #C8A55C;outline-offset:-3px}
</style></head><body><div class="wrap">
<h1>LotVue<span>360</span> — Interior 360 Studio</h1>
<p class="sub">Upload an <b>equirectangular</b> interior 360 (from a 360 camera or a phone pano app), then click on it to drop <b>feature hotspots</b> buyers can tap — just like GLO3D/CloudPano. VIN: <b id="vinlabel"></b></p>

<div id="uploadBox">
  <div class="row">
    <div><label>Interior 360 photo (equirectangular, ~2:1)</label><input id="panoFile" type="file" accept="image/*"></div>
    <div style="flex:0 0 200px"><label>Scene name</label><input id="sceneName" type="text" value="Front Seat"></div>
    <div style="flex:0 0 auto"><button id="upBtn" onclick="uploadPano()">Upload interior 360</button></div>
  </div>
  <p class="hint" style="margin-top:.4rem">Tip: a 360 camera (Insta360/Ricoh Theta) or the Google Street View app exports a proper equirectangular photo. Non-2:1 images are auto-padded.</p>
</div>

<div id="pano"></div>
<div class="bar" id="editbar" style="display:none">
  <button id="addBtn" class="gold" onclick="toggleAdd()">＋ Add hotspot</button>
  <select id="sceneSel" onchange="switchScene()" style="display:none;padding:.5rem;border-radius:8px;border:1.5px solid #DDE5EE"></select>
  <span class="hint" id="addHint"></span>
  <a class="preview" id="previewLink" target="_blank">Preview on buyer page →</a>
</div>

<div class="addform" id="addform">
  <label>Feature label (e.g. "Heated leather seats")</label>
  <input id="hsLabel" type="text" placeholder="Short feature name">
  <label>Description (optional)</label>
  <input id="hsDesc" type="text" placeholder="A sentence buyers see when they tap the pin">
  <div class="bar"><button class="gold" onclick="saveHotspot()">Save hotspot</button><button class="ghost" onclick="cancelAdd()">Cancel</button></div>
</div>

<div class="pins" id="pins"></div>
</div>
<script>
const VIN="__VIN__", PANOS=__PANOS__;
let viewer=null, addMode=false, curFile=(PANOS[0]||{}).file||'', curPinIds=[], pendingPitch=0, pendingYaw=0, downX=0, downY=0;
document.getElementById('vinlabel').textContent=VIN;
document.getElementById('previewLink').href='/view/'+VIN;

function mountViewer(){
  if(!PANOS.length){ return; }
  document.getElementById('pano').style.display='block';
  document.getElementById('editbar').style.display='flex';
  const scenes={};
  PANOS.forEach((p,i)=>{ scenes['s'+i]={type:'equirectangular',panorama:p.src,title:p.name,autoLoad:i===0,hfov:110}; });
  viewer=pannellum.viewer('pano',{escapeHTML:true,default:{firstScene:'s0',autoLoad:true},scenes:scenes});
  if(PANOS.length>1){
    const sel=document.getElementById('sceneSel'); sel.style.display='inline-block';
    PANOS.forEach((p,i)=>{ const o=document.createElement('option'); o.value='s'+i; o.textContent=p.name; sel.appendChild(o); });
  }
  viewer.on('load', loadPins);
  const panoEl=document.getElementById('pano');
  panoEl.addEventListener('mousedown', function(e){ downX=e.clientX; downY=e.clientY; });
  panoEl.addEventListener('click', function(e){
    if(!addMode) return;
    if(Math.hypot(e.clientX-downX, e.clientY-downY)>5) return;   // a look-around drag, not a placement tap
    try{ const c=viewer.mouseEventToCoords(e); pendingPitch=c[0]; pendingYaw=c[1]; openAddForm(); }catch(err){}
  });
}
function switchScene(){
  const v=document.getElementById('sceneSel').value; const idx=parseInt(v.slice(1));
  curFile=(PANOS[idx]||{}).file||''; viewer.loadScene(v);
}
async function loadPins(){
  curPinIds.forEach(id=>{ try{viewer.removeHotSpot(id);}catch(e){} }); curPinIds=[];
  let all=[]; try{ all=await (await fetch('/api/pano-hotspots/'+VIN)).json(); }catch(e){}
  all.filter(h=>!h.pano_file||h.pano_file===curFile).forEach(h=>{
    const hid='pin'+h.id;
    try{ viewer.addHotSpot({id:hid,pitch:h.pitch,yaw:h.yaw,type:'info',text:h.label}); curPinIds.push(hid); }catch(e){}
  });
  renderList(all);
}
function renderList(all){
  const box=document.getElementById('pins'); box.textContent='';
  if(!all.length){ box.innerHTML='<div class="empty">No hotspots yet. Click ＋ Add hotspot, then click a spot on the 360.</div>'; return; }
  all.forEach(h=>{
    const d=document.createElement('div'); d.className='pin';
    const b=document.createElement('b'); b.textContent=h.label;
    const s=document.createElement('span'); s.className='d'; s.textContent=h.description||'';
    const x=document.createElement('button'); x.textContent='Delete'; x.onclick=function(){ delHotspot(h.id); };
    d.appendChild(b); d.appendChild(s); d.appendChild(x); box.appendChild(d);
  });
}
function toggleAdd(){
  addMode=!addMode;
  document.getElementById('pano').classList.toggle('addmode', addMode);
  document.getElementById('addBtn').textContent = addMode ? '✓ Click a spot on the 360…' : '＋ Add hotspot';
  document.getElementById('addHint').textContent = addMode ? 'Click the feature on the panorama to place a pin.' : '';
  if(!addMode) cancelAdd();
}
function openAddForm(){ document.getElementById('addform').style.display='block'; document.getElementById('hsLabel').focus(); }
function cancelAdd(){ document.getElementById('addform').style.display='none'; document.getElementById('hsLabel').value=''; document.getElementById('hsDesc').value=''; }
async function saveHotspot(){
  const label=document.getElementById('hsLabel').value.trim();
  if(!label){ alert('Enter a feature label'); return; }
  const fd=new FormData();
  fd.append('vin',VIN); fd.append('pano_file',curFile);
  fd.append('yaw',pendingYaw); fd.append('pitch',pendingPitch);
  fd.append('label',label); fd.append('description',document.getElementById('hsDesc').value.trim());
  const r=await fetch('/api/pano-hotspot',{method:'POST',body:fd,credentials:'same-origin'});
  if(r.ok){ cancelAdd(); toggleAdd(); loadPins(); } else { alert('Save failed — are you signed in?'); }
}
async function delHotspot(id){
  if(!confirm('Delete this hotspot?')) return;
  await fetch('/api/pano-hotspot/'+id,{method:'DELETE',credentials:'same-origin'});
  loadPins();
}
async function uploadPano(){
  const f=document.getElementById('panoFile').files[0];
  if(!f){ alert('Choose an interior 360 photo'); return; }
  const fd=new FormData();
  fd.append('vin',VIN); fd.append('scene_name',document.getElementById('sceneName').value||'Interior'); fd.append('photo',f);
  const btn=document.getElementById('upBtn'); btn.disabled=true; btn.textContent='Uploading…';
  try{
    const r=await fetch('/api/upload/panorama',{method:'POST',body:fd,credentials:'same-origin'});
    if(r.ok){ location.reload(); } else { const j=await r.json().catch(()=>({})); alert(j.detail||'Upload failed'); btn.disabled=false; btn.textContent='Upload interior 360'; }
  }catch(e){ alert('Error: '+e); btn.disabled=false; btn.textContent='Upload interior 360'; }
}
mountViewer();
if(!PANOS.length){ document.getElementById('pins').innerHTML='<div class="empty">Upload an interior 360 above to start placing hotspots.</div>'; }
</script></body></html>"""


@app.get("/interior-studio/{vin}")
def interior_studio_page(vin: str, request: Request):
    from fastapi.responses import HTMLResponse
    vin = validate_vehicle_id(vin)
    _require_vehicle_owner(request, vin)
    pano_dir = STORAGE_DIR / vin / "panoramas"
    pano_files = sorted(
        f.name for f in pano_dir.iterdir()
        if f.is_file() and f.suffix.lower() in {".jpg", ".jpeg", ".png"}
        and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", f.name)
    ) if pano_dir.exists() else []
    import json as _json
    panos_js = _json.dumps([{"src": f"/storage/{vin}/panoramas/{f}", "file": f, "name": f"Scene {i+1}"}
                            for i, f in enumerate(pano_files)])
    html = _INTERIOR_STUDIO_HTML.replace("__VIN__", vin).replace("__PANOS__", panos_js)
    return HTMLResponse(html)


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

from viewer_api import register_viewer
register_viewer(app)

from crm_api import register_crm
register_crm(app)

try:
    from h_dealer_import import register_dealer_import
    register_dealer_import(app)
except Exception as _h_err:
    print(f"[H-Import] dealer importer not loaded: {_h_err}")

from studio_agent import register_studio_agent
register_studio_agent(app)


# =============================================================================
# LEAD FOLLOW-UP — one-click unsubscribe (public) + opt-in autonomous dispatcher
# =============================================================================
@app.get("/api/unsubscribe")
def lead_unsubscribe(e: str = "", p: str = ""):
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


def _start_followup_dispatcher() -> threading.Event | None:
    """Start the optional autonomous BDC sender and return its shutdown signal."""
    if os.getenv("LOTVUE_AUTO_FOLLOWUP", "0") != "1":
        print("[Follow-up] autonomous dispatcher OFF (set LOTVUE_AUTO_FOLLOWUP=1 to enable)")
        return None

    stop_event = threading.Event()
    interval = _bounded_env_int("LOTVUE_FOLLOWUP_INTERVAL_S", 300, 30, 86_400)

    def _loop():
        from ai_studio.lead_engine import send_due_follow_ups
        while not stop_event.is_set():
            try:
                summary = send_due_follow_ups()
                if summary.get("sent") or summary.get("dealer_tasks"):
                    print(f"[Follow-up] dispatch: {summary}")
            except Exception as exc:
                print(f"[Follow-up] dispatcher error: {type(exc).__name__}: {exc}")
            if stop_event.wait(interval):
                break

    threading.Thread(target=_loop, name="lotvue-followups", daemon=True).start()
    print("[Follow-up] autonomous dispatcher ON")
    return stop_event


from auth_gate import install_auth_gate
install_auth_gate(app)


# =============================================================================
# RUN
# =============================================================================
if __name__ == "__main__":
    import uvicorn
    PORT = int(os.getenv("LOTVUE_PORT", 8001))
    uvicorn.run(app, host="0.0.0.0", port=PORT)
