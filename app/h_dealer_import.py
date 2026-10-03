"""
H-Phase — Dealer inventory importer for LotVue360.
Pulls live inventory from Ahmad's OWN dealership websites (fully authorized)
and feeds each vehicle straight into the LotVue 3D pipeline.

Supported dealers (both DealerCenter/Nowcom WordPress sites behind Cloudflare —
plain requests gets a 403 interstitial, so we use curl_cffi chrome impersonation):

  • drivesmartwc  — https://www.drivesmartwc.com   (Drive Smart Auto Sales, 2 lots)
  • unlimitedag   — https://www.unlimitedag.com    (Unlimited Auto Group)

Endpoints:
  GET  /api/import/dealers               → supported dealers + reachability
  GET  /api/import/{dealer}/inventory    → live inventory list (10-min in-memory cache)
  POST /api/import/{dealer}/{vin}        → scrape detail page, download ALL full-size
                                           photos to STORAGE_DIR/<VIN>/_import/,
                                           CLASSIFY each photo (exterior / interior /
                                           detail), route exterior→_3dphotos (3D build
                                           input), interior→interior/, detail→gallery/,
                                           upsert the vehicle row, queue the 3D build
                                           from EXTERIOR shots only.
  POST /api/import/reclassify/{vin}      → re-run classification + routing on an
                                           already-downloaded _import/ set (no
                                           re-download), re-queue the spin build.

Wired from main.py:  from h_dealer_import import register_dealer_import; register_dealer_import(app)
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from app_paths import BASE_DIR, DB_PATH, STORAGE_DIR
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

router = APIRouter()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TIMEOUT = 20          # seconds, every HTTP call
CACHE_TTL = 600       # 10 minutes
IMPORT_USER_EMAIL = os.getenv("LOTVUE_IMPORT_USER", "").strip().lower()
_MIB = 1024 * 1024


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return min(max(value, minimum), maximum)


MAX_IMPORT_PHOTOS = _bounded_env_int("LOTVUE_MAX_IMPORT_PHOTOS", 100, 4, 500)
MAX_IMPORT_PHOTO_BYTES = _bounded_env_int("LOTVUE_MAX_PHOTO_MB", 30, 1, 128) * _MIB
MAX_IMPORT_BATCH_BYTES = _bounded_env_int("LOTVUE_MAX_PHOTO_BATCH_MB", 400, 1, 1024) * _MIB
MAX_IMPORT_IMAGE_PIXELS = _bounded_env_int(
    "LOTVUE_MAX_IMPORT_IMAGE_PIXELS", 16_000_000, 1_000_000, 40_000_000
)
MAX_SCRAPE_PAGE_BYTES = 8 * _MIB
IMPORT_CONCURRENCY = threading.BoundedSemaphore(2)

DEALERS = {
    "drivesmartwc": {
        "name": "Drive Smart Auto Sales (West Chester + Fairfield)",
        "site": "https://www.drivesmartwc.com",
        "inventory_url": "https://www.drivesmartwc.com/inventory/",
        "platform": "DealerCenter Websites (WordPress)",
    },
    "unlimitedag": {
        "name": "Unlimited Auto Group of West Chester",
        "site": "https://www.unlimitedag.com",
        "inventory_url": "https://www.unlimitedag.com/inventory/",
        "platform": "DealerCenter Websites (WordPress)",
    },
}


# ── HTTP session (Cloudflare-capable) ───────────────────────────────────────
def _session():
    """curl_cffi chrome-impersonated session, with a requests fallback."""
    try:
        from curl_cffi import requests as cr
        session = cr.Session(impersonate="chrome")
    except ImportError:
        import requests
        session = requests.Session()
    session.headers.update({"User-Agent": UA})
    return session


def _safe_session_get(session, url: str, *, headers=None):
    """Stream HTTPS responses while refusing cross-origin redirects."""
    from urllib.parse import urljoin, urlsplit

    origin = urlsplit(str(url))
    if (origin.scheme != "https" or not origin.hostname or origin.username is not None
            or origin.password is not None or origin.port not in (None, 443)):
        raise ValueError("upstream URL must be an absolute HTTPS URL")
    current = str(url)
    for _ in range(4):
        response = session.get(
            current, headers=headers, timeout=TIMEOUT, stream=True, allow_redirects=False
        )
        status = getattr(response, "status_code", 0)
        if status not in {301, 302, 303, 307, 308}:
            return response
        location = (getattr(response, "headers", {}) or {}).get("location", "")
        try:
            response.close()
        except Exception:
            pass
        if not location:
            raise ValueError("upstream returned a redirect without a location")
        target = urlsplit(urljoin(current, location))
        if (target.scheme != "https" or target.hostname != origin.hostname
                or target.port not in (None, 443) or target.username is not None
                or target.password is not None):
            raise ValueError("upstream redirect changed origin")
        current = target.geturl()
    raise ValueError("upstream returned too many redirects")


def _read_response_bytes(response, max_bytes: int) -> bytes:
    """Consume a streaming HTTP response with a hard byte ceiling."""
    try:
        declared = int(response.headers.get("content-length") or 0)
    except (AttributeError, TypeError, ValueError):
        declared = 0
    if declared > max_bytes:
        raise ValueError("upstream response exceeds the size limit")
    chunks = []
    total = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("upstream response exceeds the size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _fetch_text(session, url: str, *, headers=None, max_bytes: int = MAX_SCRAPE_PAGE_BYTES):
    response = _safe_session_get(session, url, headers=headers)
    try:
        status = getattr(response, "status_code", 0)
        if status != 200:
            return status, ""
        raw = _read_response_bytes(response, max_bytes)
        return status, raw.decode("utf-8", errors="replace")
    finally:
        try:
            response.close()
        except Exception:
            pass


def _download_jpeg(session, url: str, destination: Path, max_bytes: int) -> int:
    """Stream, validate, normalize, and atomically save one bounded remote JPEG."""
    response = _safe_session_get(session, url)
    download_path = destination.with_suffix(".download")
    normalized_path = destination.with_suffix(".normalized")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        if getattr(response, "status_code", 0) != 200:
            raise ValueError("image server returned a non-200 status")
        try:
            declared = int(response.headers.get("content-length") or 0)
        except (AttributeError, TypeError, ValueError):
            declared = 0
        if declared > max_bytes:
            raise ValueError("remote image exceeds the per-file or batch size limit")
        total = 0
        with download_path.open("wb") as output:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("remote image exceeds the per-file or batch size limit")
                output.write(chunk)
        if total < 1001:
            raise ValueError("remote image is empty or incomplete")
        with download_path.open("rb") as source:
            if source.read(3) != b"\xff\xd8\xff":
                raise ValueError("remote image is not a JPEG")
        from PIL import Image, ImageOps

        with Image.open(download_path) as image:
            if image.format != "JPEG":
                raise ValueError("remote image is not a JPEG")
            oriented = ImageOps.exif_transpose(image)
            if oriented.width * oriented.height > MAX_IMPORT_IMAGE_PIXELS:
                raise ValueError("remote image exceeds the pixel limit")
            normalized = oriented.convert("RGB")
            normalized.thumbnail((6000, 4000), Image.Resampling.LANCZOS)
            normalized.save(normalized_path, format="JPEG", quality=92, optimize=True)
        if normalized_path.stat().st_size > max_bytes:
            raise ValueError("normalized image exceeds the per-file or batch size limit")
        normalized_path.replace(destination)
        return total
    finally:
        try:
            response.close()
        except Exception:
            pass
        download_path.unlink(missing_ok=True)
        normalized_path.unlink(missing_ok=True)


def _slug(value: str) -> str:
    """DealerCenter URL slug: lowercase, runs of non-alphanumerics → one hyphen."""
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")


def _clean_vin(vin: str) -> str:
    # same folder-safety rule as main.py generate-3d
    return re.sub(r"[^A-Za-z0-9_-]", "", vin or "").upper()[:32]


def _fmt_price(p) -> str:
    try:
        f = float(p)
        return str(int(f)) if f else ""
    except (TypeError, ValueError):
        return re.sub(r"[^0-9.]", "", str(p or "")) or ""


# ── inventory cache ─────────────────────────────────────────────────────────
_cache_lock = threading.Lock()
_inventory_locks = {dealer: threading.Lock() for dealer in DEALERS}
_inv_cache: dict = {}   # dealer → {"ts": epoch, "items": [...], "by_vin": {...}}


def _cached(dealer):
    with _cache_lock:
        e = _inv_cache.get(dealer)
        if e and time.time() - e["ts"] < CACHE_TTL:
            return e
    return None


def _store_cache(dealer, items):
    entry = {"ts": time.time(), "items": items,
             "by_vin": {i["vin"].upper(): i for i in items if i.get("vin")}}
    with _cache_lock:
        _inv_cache[dealer] = entry
    return entry


# ── drivesmartwc scraper ────────────────────────────────────────────────────
def _scrape_drivesmartwc(s):
    site = DEALERS["drivesmartwc"]["site"]
    headers = {
        "Referer": site + "/inventory/",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }
    vehicles, seen_ids, total, page_no = [], set(), None, 1
    while page_no <= 40:
        status, inventory_html = _fetch_text(
            s, f"{site}/inventory/?pager=12&page_no={page_no}"
        )
        if status != 200:
            raise RuntimeError(f"drivesmartwc: inventory page HTTP {status} (page {page_no})")
        settings_match = re.search(
            r'id="DWS_Async_Vehicle_Listing_Settings[^"]*">\s*(\{.*?\})\s*</script>',
            inventory_html, re.S,
        )
        if not settings_match:
            raise RuntimeError("drivesmartwc: DWS settings block not found (site changed again?)")
        settings = json.loads(settings_match.group(1))
        service_url = settings["serviceUrl"]
        service_status, body = _fetch_text(s, site + service_url, headers=headers)
        if service_status != 200:
            raise RuntimeError(f"drivesmartwc: inv service HTTP {service_status} (page {page_no})")
        jsonp_match = re.match(r"^\s*[\w$.]+\((.*)\)\s*;?\s*$", body, re.S)
        try:
            data = json.loads(jsonp_match.group(1) if jsonp_match else body)
        except ValueError as exc:
            raise RuntimeError(f"drivesmartwc: page {page_no} returned non-JSON") from exc
        batch = data.get("Vehicles") or []
        total = data.get("TotalRecordCount", total)
        fresh = [
            item for item in batch
            if (item.get("Vin") or item.get("VehicleId"))
            and str(item.get("Vin") or item.get("VehicleId")).upper() not in seen_ids
        ]
        for item in fresh:
            seen_ids.add(str(item.get("Vin") or item.get("VehicleId")).upper())
        fresh = fresh[:max(0, 5000 - len(vehicles))]
        if not fresh:
            break
        vehicles.extend(fresh)
        page_no += 1
        if len(vehicles) >= 5000 or (total is not None and len(vehicles) >= total):
            break
        time.sleep(0.3)

    items = []
    for vehicle in vehicles:
        vin = str(vehicle.get("Vin") or "").upper()
        make = vehicle.get("Make") or ""
        model = vehicle.get("Model") or ""
        stock = vehicle.get("StockNumber") or ""
        title = " ".join(
            str(value) for value in (vehicle.get("Year"), make, model, vehicle.get("Trim")) if value
        ).strip()
        items.append({
            "vin": vin,
            "title": title,
            "price": _fmt_price(vehicle.get("VehiclePrice") or vehicle.get("AskingPrice")),
            "detail_url": f"{site}/inventory/{_slug(make)}/{_slug(model)}/{_slug(stock)}/",
            "photo_count": vehicle.get("VehicleImageCount"),
            "_meta": {
                "year": str(vehicle.get("Year") or ""), "make": make, "model": model,
                "trim_level": vehicle.get("Trim") or "", "engine": vehicle.get("Engine") or "",
                "transmission": vehicle.get("Transmission") or "",
                "drivetrain": vehicle.get("Drivetrain") or "", "fuel_type": vehicle.get("FuelType") or "",
                "body_style": vehicle.get("BodyType") or "", "stock_number": stock,
                "color": vehicle.get("ExteriorColor") or "",
                "mileage": _fmt_price(vehicle.get("Odometer")),
            },
        })
    return items


def _photos_drivesmartwc(detail_html):
    docs, seen = [], set()
    for doc in re.findall(r"imagescf\.dealercenter\.net/\d+/\d+/([0-9a-f/\-]+?)\.jpg", detail_html):
        if doc not in seen:
            seen.add(doc)
            docs.append(doc)
    return [f"https://imagescf.dealercenter.net/0/0/{d}.jpg" for d in docs]


# ── unlimitedag scraper ─────────────────────────────────────────────────────
def _scrape_unlimitedag(s):
    site = DEALERS["unlimitedag"]["site"]
    items, seen_vins, total, page = [], set(), None, 1
    while page <= 30 and len(items) < 5000:
        status, html = _fetch_text(s, f"{site}/inventory/?perpage=48&page_no={page}")
        if status != 200:
            raise RuntimeError(f"unlimitedag: inventory page HTTP {status} (page {page})")
        if total is None:
            match_total = re.search(r"\((\d+) vehicles\)", html)
            total = int(match_total.group(1)) if match_total else None
        cards = list(re.finditer(r'data-vehicle-id="vehicle-id-([A-HJ-NPR-Z0-9]{17})"', html))
        new_count = 0
        for index, match in enumerate(cards):
            vin = match.group(1).upper()
            if vin in seen_vins:
                continue
            segment_end = cards[index + 1].start() if index + 1 < len(cards) else match.start() + 9000
            segment = html[match.start():segment_end]
            url_match = re.search(
                r'href="(https://www\.unlimitedag\.com/inventory/[a-z0-9\-]+/[a-z0-9\-]+/[a-z0-9\-]+/)"',
                segment,
            )
            title_match = re.search(r'data-title="([^"]+)"', segment)
            price_match = re.search(r'vehicle-price-value[^>]*>\s*\$?([0-9,]+)', segment)
            seen_vins.add(vin)
            new_count += 1
            items.append({
                "vin": vin,
                "title": (title_match.group(1) if title_match else "").strip(),
                "price": price_match.group(1).replace(",", "") if price_match else "",
                "detail_url": url_match.group(1) if url_match else "",
                "photo_count": None,
                "_meta": {},
            })
            if len(items) >= 5000:
                break
        if new_count == 0 or (total is not None and len(items) >= total):
            break
        page += 1
        time.sleep(0.4)
    return items


def _photos_unlimitedag(detail_html):
    """Gallery photos only: keep hashes that appear at the 1366/1025 lightbox size
    (smaller-only sizes are the 'similar vehicles' carousel of OTHER cars)."""
    first_pos, gallery = {}, set()
    for m in re.finditer(r"imagescf\.dealercenter\.net/(\d+)/(\d+)/(\d{6}-[0-9a-f]{32})\.jpg", detail_html):
        w, h, doc = m.group(1), m.group(2), m.group(3)
        if doc not in first_pos:
            first_pos[doc] = m.start()
        if (w, h) == ("1366", "1025"):
            gallery.add(doc)
    docs = sorted(gallery, key=lambda d: first_pos[d])
    return [f"https://imagescf.dealercenter.net/0/0/{d}.jpg" for d in docs]


def _detail_meta_unlimitedag(detail_html):
    m = re.search(r'"vehicleDetails"\s*:\s*(\{[^{}]*\})', detail_html)
    if not m:
        return {}
    try:
        d = json.loads(m.group(1))
    except ValueError:
        return {}
    return {
        "year": str(d.get("year") or ""), "make": d.get("make") or "",
        "model": d.get("model") or "", "trim_level": d.get("trim") or "",
        "body_style": d.get("bodyStyle") or "", "fuel_type": d.get("fuelType") or "",
        "color": d.get("exteriorColor") or "", "stock_number": str(d.get("stockNumber") or ""),
        "engine": "", "transmission": "", "drivetrain": "", "mileage": "",
        "price": _fmt_price(d.get("price")),
    }


_SCRAPERS = {"drivesmartwc": _scrape_drivesmartwc, "unlimitedag": _scrape_unlimitedag}
_PHOTO_EXTRACTORS = {"drivesmartwc": _photos_drivesmartwc, "unlimitedag": _photos_unlimitedag}


def _get_inventory(dealer, force=False):
    if dealer not in DEALERS:
        raise ValueError(f"Unknown dealer: {dealer}")
    lock = _inventory_locks[dealer]
    with lock:
        entry = None if force else _cached(dealer)
        if entry:
            return entry
        session = _session()
        try:
            items = _SCRAPERS[dealer](session)
        finally:
            try:
                session.close()
            except Exception:
                pass
        return _store_cache(dealer, items)


# ── DB registration (same columns/idiom as main.py db_upsert_vehicle) ──────
def _db_register_vehicle(vin, meta, title="", user_email=IMPORT_USER_EMAIL):
    user_email = str(user_email or "").strip().lower()
    if not user_email:
        raise ValueError("authenticated tenant email is required")
    conn = sqlite3.connect(str(DB_PATH), timeout=20)
    try:
        existing = conn.execute("SELECT user_email FROM vehicles WHERE vin=?", (vin,)).fetchone()
        existing_email = str(existing[0] or "").strip().lower() if existing else ""
        if existing_email and existing_email != user_email:
            raise ValueError("VIN belongs to a different tenant; refusing to overwrite it")
        conn.execute("""
            INSERT INTO vehicles (vin, year, make, model, exterior_frames, interior_count, created_at, watermark_mode, user_email,
                                  trim_level, engine, transmission, drivetrain, fuel_type, body_style, stock_number, turntable_style, job_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(vin) DO UPDATE SET
                year=excluded.year, make=excluded.make, model=excluded.model,
                watermark_mode=excluded.watermark_mode,
                user_email=CASE WHEN COALESCE(user_email,'')='' THEN excluded.user_email ELSE user_email END,
                trim_level=CASE WHEN excluded.trim_level != '' THEN excluded.trim_level ELSE trim_level END,
                engine=CASE WHEN excluded.engine != '' THEN excluded.engine ELSE engine END,
                transmission=CASE WHEN excluded.transmission != '' THEN excluded.transmission ELSE transmission END,
                drivetrain=CASE WHEN excluded.drivetrain != '' THEN excluded.drivetrain ELSE drivetrain END,
                fuel_type=CASE WHEN excluded.fuel_type != '' THEN excluded.fuel_type ELSE fuel_type END,
                body_style=CASE WHEN excluded.body_style != '' THEN excluded.body_style ELSE body_style END,
                stock_number=CASE WHEN excluded.stock_number != '' THEN excluded.stock_number ELSE stock_number END
        """, (vin, meta.get("year", ""), meta.get("make", ""), meta.get("model", ""), 0, 0,
              datetime.now().isoformat(), "preview", user_email,
              meta.get("trim_level", ""), meta.get("engine", ""), meta.get("transmission", ""),
              meta.get("drivetrain", ""), meta.get("fuel_type", ""), meta.get("body_style", ""),
              meta.get("stock_number", ""), "studio_shadow", ""))
        sets, vals = [], []
        for col in ("color", "price", "mileage"):
            if meta.get(col):
                sets.append(f"{col}=?")
                vals.append(str(meta[col]))
        if meta.get("description") or title:
            sets.append("description=CASE WHEN description='' THEN ? ELSE description END")
            vals.append(meta.get("description") or title)
        if sets:
            conn.execute(f"UPDATE vehicles SET {', '.join(sets)} WHERE vin=?", (*vals, vin))
        conn.commit()
    finally:
        conn.close()


# ── PHOTO CLASSIFIER (exterior-orbit / interior / detail) ───────────────────
# rembg session is cached module-wide (same fallback ladder as gaussian_3d.py).
_rembg_lock = threading.Lock()
_rembg_sess = None
_rembg_tried = False


def _rembg_session():
    global _rembg_sess, _rembg_tried
    with _rembg_lock:
        if _rembg_tried:
            return _rembg_sess
        _rembg_tried = True
        try:
            from rembg_runtime import is_enabled, require_session
            if is_enabled():
                _rembg_sess = require_session()
                print("[H-Import] owner-selected segmentation model is configured")
            else:
                print("[H-Import] no explicitly approved segmentation model; classifier uses geometry+position")
        except Exception as e:
            print(f"[H-Import] rembg unavailable — classifier degrades to geometry+position: {type(e).__name__}")
        return _rembg_sess


def _photo_features(path: Path) -> dict:
    """Measure one bounded image after downscaling to at most 512 pixels."""
    import numpy as np
    from PIL import Image, ImageOps

    with Image.open(path) as source:
        if source.width * source.height > MAX_IMPORT_IMAGE_PIXELS:
            raise ValueError("Image exceeds the safe pixel limit")
        oriented = ImageOps.exif_transpose(source)
        try:
            image = oriented.convert("RGB")
        finally:
            if oriented is not source:
                oriented.close()
    width, height = image.size
    scale = min(1.0, 512.0 / max(width, height))
    if scale < 1:
        small = image.resize(
            (max(1, int(width * scale)), max(1, int(height * scale))),
            Image.Resampling.BILINEAR,
        )
    else:
        small = image
    try:
        small_width, small_height = small.size
        gray = small.convert("L")
        try:
            luma = np.asarray(gray, dtype=np.float32)
        finally:
            gray.close()
        features = {
            "img_aspect": round(width / max(height, 1), 3),
            "dark_frac": round(float((luma < 60).mean()), 3),
            "luma_std": round(float(luma.std()), 1),
            "mask_frac": None,
            "bbox_aspect": None,
            "edges_touched": None,
        }
        session = _rembg_session()
        if session is not None:
            from rembg_runtime import remove
            rgba = remove(small, session=session)
            try:
                alpha_image = rgba.getchannel("A")
                try:
                    alpha = np.asarray(alpha_image, dtype=np.uint8)
                finally:
                    alpha_image.close()
            finally:
                rgba.close()
            mask = alpha > 40
            features["mask_frac"] = round(float(mask.mean()), 4)
            ys, xs = np.where(mask)
            if len(xs) > 50:
                box_width = int(xs.max() - xs.min()) + 1
                box_height = int(ys.max() - ys.min()) + 1
                features["bbox_aspect"] = round(box_width / max(box_height, 1), 3)
                edge = 3
                features["edges_touched"] = sum((
                    int(xs.min()) <= edge,
                    int(ys.min()) <= edge,
                    int(xs.max()) >= small_width - 1 - edge,
                    int(ys.max()) >= small_height - 1 - edge,
                ))
            else:
                features["bbox_aspect"] = 0.0
                features["edges_touched"] = 0
        return features
    finally:
        if small is not image:
            small.close()
        image.close()


_VLM_BACKOFF_LOCK = threading.Lock()
_VLM_BACKOFF_UNTIL = 0.0
OLLAMA_URL = os.getenv("LOTVUE_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
VLM_MODEL = os.getenv("LOTVUE_VLM_MODEL", "qwen2.5vl:3b")
_VLM_PROMPT = (
    "You are sorting used-car dealership listing photos. Reply with exactly one word and nothing else.\n"
    "exterior = the whole car photographed from outside (front, side, rear, or three-quarter view; most of the car body visible).\n"
    "interior = taken inside the cabin or of the cabin/cargo space (seats, dashboard, steering wheel, console, headliner, door panel, trunk interior).\n"
    "detail = closeup of a single part, wheel, badge, engine, tire, or document.\n"
    "Choose exterior, interior, or detail."
)


def _disable_vlm_for(seconds: float) -> None:
    global _VLM_BACKOFF_UNTIL
    with _VLM_BACKOFF_LOCK:
        _VLM_BACKOFF_UNTIL = max(_VLM_BACKOFF_UNTIL, time.monotonic() + max(0.0, seconds))


def _vlm_classify(path: Path):
    """Optionally classify one bounded image with the local Ollama endpoint."""
    with _VLM_BACKOFF_LOCK:
        if time.monotonic() < _VLM_BACKOFF_UNTIL:
            return None
    try:
        import base64
        import urllib.request
        from PIL import Image, ImageOps

        with Image.open(path) as source:
            if source.width * source.height > MAX_IMPORT_IMAGE_PIXELS:
                return None
            oriented = ImageOps.exif_transpose(source)
            try:
                image = oriented.convert("RGB")
            finally:
                if oriented is not source:
                    oriented.close()
        try:
            image.thumbnail((512, 512), Image.Resampling.LANCZOS)
            import io
            with io.BytesIO() as buffer:
                image.save(buffer, format="JPEG", quality=85, optimize=True)
                encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        finally:
            image.close()

        body = json.dumps({
            "model": VLM_MODEL,
            "prompt": _VLM_PROMPT,
            "images": [encoded],
            "stream": False,
            "keep_alive": "15m",
            "options": {"temperature": 0, "num_predict": 8},
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status != 200:
                _disable_vlm_for(60)
                return None
            raw = response.read(65_537)
        if len(raw) > 65_536:
            _disable_vlm_for(60)
            return None
        result = json.loads(raw.decode("utf-8"))
        label = str(result.get("response", "")).strip().lower().strip(".,:;!? ")
        if label in {"exterior", "interior", "detail"}:
            return label, 0.85
    except Exception as exc:
        print(f"[H-Import] local VLM unavailable; using fallback classifier: {type(exc).__name__}")
        _disable_vlm_for(60)
    return None


def classify_photo(feats: dict, index: int, total: int):
    """PURE decision function → (category, confidence).
    Categories: 'exterior' (orbit shot, trains the spin) | 'interior' | 'detail'.

    EXTERIOR-ORBIT: coherent car mask 8-70% of frame, bbox w/h 1.15-4.0,
                    bbox touching ≤1 frame edge.
    INTERIOR:       mask <6% or >80% (rembg can't isolate a car from inside the
                    cabin), OR near-square/portrait frame with dominant
                    dark/uniform regions.
    DETAIL/OTHER:   everything else (wheels, badges, stickers).
    Tiebreak (deterministic): dealer feeds are ordered exterior-walk first —
    ambiguous photos in the first third of the set lean EXTERIOR, later ones
    lean INTERIOR/DETAIL."""
    frac = feats.get("mask_frac")
    aspect = feats.get("bbox_aspect") or 0.0
    edges = feats.get("edges_touched")
    img_aspect = feats.get("img_aspect") or 1.0
    darkish = (feats.get("dark_frac") or 0.0) >= 0.35 or (feats.get("luma_std") or 999.0) <= 40.0
    squarish = img_aspect <= 1.05                     # near-square or portrait frame
    early = total > 0 and index < max(1.0, total / 3.0)

    if frac is None:                                  # rembg unavailable → geometry + position
        if squarish and darkish:
            return ("interior", 0.55)
        return ("exterior", 0.40) if early else ("detail", 0.40)

    if frac < 0.06 or frac > 0.80:                    # no isolatable car → cabin shot
        return ("interior", 0.90 if (darkish or frac > 0.80) else 0.70)
    if squarish and darkish:                          # portrait + dark/uniform → cabin
        return ("interior", 0.75)
    if 0.08 <= frac <= 0.70 and 1.15 <= aspect <= 4.0 and edges is not None and edges <= 1:
        return ("exterior", 0.90)                     # clean orbit shot

    # ambiguous band → deterministic position tiebreak
    near_exterior = 0.06 <= frac <= 0.80 and 1.0 <= aspect <= 4.5 and edges is not None and edges <= 2
    if early and near_exterior:
        return ("exterior", 0.55)
    if squarish or darkish:
        return ("interior", 0.50)
    return ("detail", 0.55)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def _atomic_replace_directory(source: Path, destination: Path) -> None:
    """Swap a staged directory into place, restoring the old one on failure."""
    source = Path(source)
    destination = Path(destination)
    if source.is_symlink() or destination.is_symlink():
        raise ValueError("Refusing to replace a symlinked media directory")
    backup = destination.with_name(f".backup-{uuid.uuid4().hex}")
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
            _remove_path(backup)


def _replace_owned_images(stage: Path, destination: Path, prefix: str) -> None:
    """Replace importer-owned JPEGs while preserving dealer-uploaded files."""
    stage = Path(stage)
    destination = Path(destination)
    if stage.is_symlink() or destination.is_symlink():
        raise ValueError("Refusing to write into a symlinked media directory")
    destination.mkdir(parents=True, exist_ok=True)
    backup = destination / f".{prefix}-backup-{uuid.uuid4().hex}"
    backup.mkdir(mode=0o700)
    old_files = list(destination.glob(f"{prefix}*.jpg"))
    installed = []
    try:
        for old in old_files:
            if old.is_symlink() or not old.is_file():
                raise ValueError("Refusing to replace a non-regular importer image")
            os.replace(old, backup / old.name)
        for new in sorted(stage.glob("*.jpg")):
            if new.is_symlink() or not new.is_file() or not new.name.startswith(prefix):
                raise ValueError("Invalid staged importer image")
            target = destination / new.name
            os.replace(new, target)
            installed.append(target)
    except Exception:
        for target in installed:
            target.unlink(missing_ok=True)
        for old in backup.iterdir():
            os.replace(old, destination / old.name)
        raise
    finally:
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)


def _load_clip_results(import_dir: Path) -> dict:
    """Run the optional isolated CLIP model, accepting only bounded JSON output."""
    script = BASE_DIR / "clip_classify.py"
    if not script.is_file():
        return {}
    try:
        result = subprocess.run(
            [sys.executable, "-s", str(script), str(import_dir)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=600,
            check=False,
        )
        if result.returncode != 0 or len(result.stdout) > 2 * _MIB:
            return {}
        payload = json.loads(result.stdout.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            return {}
        photos = payload.get("photos")
        if not isinstance(photos, list) or len(photos) > MAX_IMPORT_PHOTOS:
            return {}
        output = {}
        for item in photos:
            if not isinstance(item, dict):
                continue
            name = item.get("file")
            category = item.get("category")
            try:
                confidence = float(item.get("confidence", 0.0))
            except (TypeError, ValueError):
                continue
            if (isinstance(name, str) and Path(name).name == name
                    and category in {"exterior", "interior", "detail"}
                    and 0.0 <= confidence <= 1.0):
                output[name] = {"category": category, "confidence": confidence}
        return output
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return {}


def _classify_and_route(vin: str) -> dict:
    """Classify downloaded photos and atomically route them by intended use."""
    vin = _clean_vin(vin)
    if not vin or any(char in vin for char in "/\\"):
        raise ValueError("Invalid VIN")
    storage_root = STORAGE_DIR.resolve()
    vdir = STORAGE_DIR / vin
    if vdir.is_symlink() or not vdir.resolve().is_relative_to(storage_root):
        raise ValueError("Vehicle media directory escapes configured storage")
    import_dir = vdir / "_import"
    if import_dir.is_symlink():
        raise ValueError("Refusing to classify a symlinked import directory")
    photos = sorted(import_dir.glob("p_*.jpg"))
    if len(photos) > MAX_IMPORT_PHOTOS:
        raise ValueError(f"Imported photo set exceeds the {MAX_IMPORT_PHOTOS}-photo limit")
    photos = [photo for photo in photos if photo.is_file() and not photo.is_symlink()]
    if not photos:
        raise RuntimeError(f"No downloaded photos in {import_dir}")

    clip_results = _load_clip_results(import_dir)
    manifest = []
    for index, photo in enumerate(photos):
        features = {}
        clip_result = clip_results.get(photo.name)
        if clip_result:
            category = clip_result["category"]
            confidence = clip_result["confidence"]
            method = "clip"
        else:
            try:
                features = _photo_features(photo)
            except Exception as exc:
                features = {"error": f"{type(exc).__name__}: {exc}"}
            vlm_result = _vlm_classify(photo)
            if vlm_result:
                category, confidence = vlm_result
                method = "vlm"
            else:
                category, confidence = classify_photo(features, index, len(photos))
                method = "heuristic"
        if category not in {"exterior", "interior", "detail"}:
            category, confidence, method = "detail", 0.0, "heuristic"
        manifest.append({
            "file": photo.name,
            "index": index,
            "category": category,
            "confidence": round(max(0.0, min(float(confidence), 1.0)), 3),
            "method": method,
            "features": features,
        })

    manifest_payload = {
        "vin": vin,
        "classified_at": datetime.now().isoformat(),
        "total": len(photos),
        "photos": manifest,
    }
    manifest_tmp = import_dir / f".classify-{uuid.uuid4().hex}.tmp"
    try:
        with manifest_tmp.open("w", encoding="utf-8") as output:
            json.dump(manifest_payload, output, ensure_ascii=False, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(manifest_tmp, import_dir / "classify.json")
    finally:
        manifest_tmp.unlink(missing_ok=True)

    ext_dir = vdir / "_3dphotos"
    int_dir = vdir / "interior"
    gal_dir = vdir / "gallery"
    stages = {
        ext_dir: vdir / f".3dphotos-stage-{uuid.uuid4().hex}",
        int_dir: vdir / f".interior-stage-{uuid.uuid4().hex}",
        gal_dir: vdir / f".gallery-stage-{uuid.uuid4().hex}",
    }
    for stage in stages.values():
        stage.mkdir(mode=0o700)
    ne = ni = ng = 0
    try:
        for entry in manifest:
            source = import_dir / entry["file"]
            if source.is_symlink() or not source.is_file():
                raise ValueError("Imported image changed during classification")
            if entry["category"] == "exterior":
                ne += 1
                target = stages[ext_dir] / f"p_{ne:04d}.jpg"
            elif entry["category"] == "interior":
                ni += 1
                target = stages[int_dir] / f"int_{ni:03d}.jpg"
            else:
                ng += 1
                target = stages[gal_dir] / f"imp_{ng:03d}.jpg"
            shutil.copyfile(source, target)

        _atomic_replace_directory(stages[ext_dir], ext_dir)
        _replace_owned_images(stages[int_dir], int_dir, "int_")
        _replace_owned_images(stages[gal_dir], gal_dir, "imp_")
    finally:
        for stage in stages.values():
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)

    try:
        with sqlite3.connect(str(DB_PATH), timeout=10) as conn:
            conn.execute("""UPDATE vehicles SET interior_count=?,
                            show_interior=CASE WHEN ?>0 THEN 1 ELSE show_interior END,
                            show_gallery=CASE WHEN ?>0 THEN 1 ELSE show_gallery END
                            WHERE vin=?""", (ni, ni, ng, vin))
    except sqlite3.Error:
        pass

    return {
        "total": len(photos),
        "exterior": ne,
        "interior": ni,
        "detail": ng,
        "exterior_dir": str(ext_dir),
        "manifest": f"/storage/{vin}/_import/classify.json",
    }


# ── 3D pipeline hand-off (same subprocess call as main.py generate-3d) ─────
def _queue_3d_build(vin, images_dir):
    vdir = STORAGE_DIR / vin
    (vdir / "splat").mkdir(parents=True, exist_ok=True)
    (vdir / "splat" / "status.json").write_text(
        json.dumps({"status": "running", "step": "queued", "progress": 1, "vin": vin}),
        encoding="utf-8")
    subprocess.Popen(
        [sys.executable, str(BASE_DIR / "gaussian_3d.py"), "--vin", vin,
         "--images", str(images_dir), "--steps", "7000",
         "--style", "natural", "--backdrop", "light"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(BASE_DIR))


_vehicle_locks_guard = threading.Lock()
_vehicle_locks = {}
_CLASSIFICATION_SEMAPHORE = threading.BoundedSemaphore(1)


@contextmanager
def _vehicle_import_lock(vin: str):
    """Serialize import/reclassify operations for this VIN without leaking locks."""
    with _vehicle_locks_guard:
        state = _vehicle_locks.get(vin)
        if state is None:
            state = {"lock": threading.Lock(), "users": 0}
            _vehicle_locks[vin] = state
        state["users"] += 1
    state["lock"].acquire()
    try:
        yield
    finally:
        state["lock"].release()
        with _vehicle_locks_guard:
            state["users"] -= 1
            if state["users"] == 0 and _vehicle_locks.get(vin) is state:
                _vehicle_locks.pop(vin, None)


def _existing_vehicle_owner(vin: str):
    """Look up the tenant owning a VIN; missing/uninitialized DB means no row."""
    try:
        with sqlite3.connect(str(DB_PATH), timeout=10) as conn:
            row = conn.execute("SELECT user_email FROM vehicles WHERE vin=?", (vin,)).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None
        raise
    return str(row[0] or "").strip().lower() if row else None


def _commit_import_stage(stage: Path, destination: Path, vin: str, meta: dict,
                         title: str, actor_email: str) -> None:
    """Replace photos and upsert metadata together, restoring the old files on failure."""
    if destination.is_symlink() or stage.is_symlink():
        raise ValueError("Refusing to replace a symlinked import directory")
    backup = destination.with_name(f".import-backup-{uuid.uuid4().hex}")
    moved_old = False
    try:
        if destination.exists():
            os.replace(destination, backup)
            moved_old = True
        os.replace(stage, destination)
        _db_register_vehicle(vin, meta, title=title, user_email=actor_email)
    except Exception:
        if destination.exists():
            _remove_path(destination)
        if moved_old and backup.exists():
            os.replace(backup, destination)
        raise
    else:
        if backup.exists():
            _remove_path(backup)


def _import_vehicle_work(dealer: str, vin: str, actor_email: str):
    if dealer not in DEALERS:
        return JSONResponse({"error": f"Unknown dealer '{dealer}'. Supported: {list(DEALERS)}"}, status_code=404)
    if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
        return JSONResponse({"error": "Provide a valid 17-character VIN"}, status_code=400)
    actor_email = str(actor_email or "").strip().lower()
    if not actor_email:
        return JSONResponse({"error": "Authenticated tenant email is required"}, status_code=401)
    try:
        current_owner = _existing_vehicle_owner(vin)
        if current_owner and current_owner != actor_email:
            return JSONResponse(
                {"error": "VIN belongs to a different tenant; import was not allowed"},
                status_code=409,
            )
        entry = _get_inventory(dealer)
        rec = entry["by_vin"].get(vin)
        if not rec:
            entry = _get_inventory(dealer, force=True)
            rec = entry["by_vin"].get(vin)
        if not rec:
            return JSONResponse(
                {"error": f"VIN {vin} not found in {dealer} live inventory ({len(entry['items'])} vehicles)"},
                status_code=404,
            )
        if not rec.get("detail_url"):
            return JSONResponse({"error": f"No detail URL for VIN {vin}"}, status_code=502)

        session = _session()
        try:
            status, detail_html = _fetch_text(
                session,
                rec["detail_url"],
                headers={"Referer": DEALERS[dealer]["inventory_url"]},
            )
            if status != 200:
                return JSONResponse(
                    {"error": f"Detail page HTTP {status}: {rec['detail_url']}"},
                    status_code=502,
                )
            photo_urls = list(dict.fromkeys(_PHOTO_EXTRACTORS[dealer](detail_html)))
            if not photo_urls:
                return JSONResponse(
                    {"error": "No photos found on detail page", "detail_url": rec["detail_url"]},
                    status_code=502,
                )
            if len(photo_urls) > MAX_IMPORT_PHOTOS:
                return JSONResponse(
                    {"error": "Dealer photo set exceeds the import photo-count limit",
                     "photos_found": len(photo_urls), "max_photos": MAX_IMPORT_PHOTOS},
                    status_code=413,
                )

            meta = dict(rec.get("_meta") or {})
            if dealer == "unlimitedag":
                meta = {**_detail_meta_unlimitedag(detail_html),
                        **{key: value for key, value in meta.items() if value}}
            if not meta.get("price") and rec.get("price"):
                meta["price"] = rec["price"]

            vehicle_dir = STORAGE_DIR / vin
            storage_root = STORAGE_DIR.resolve()
            if vehicle_dir.is_symlink() or not vehicle_dir.resolve().is_relative_to(storage_root):
                return JSONResponse({"error": "Vehicle storage path is unsafe"}, status_code=400)
            vehicle_dir.mkdir(parents=True, exist_ok=True)
            import_dir = vehicle_dir / "_import"
            if import_dir.is_symlink():
                return JSONResponse({"error": "Vehicle import directory is unsafe"}, status_code=400)
            stage = vehicle_dir / f".import-stage-{uuid.uuid4().hex}"
            stage.mkdir(mode=0o700)
            saved = failed = total_bytes = 0
            try:
                for index, url in enumerate(photo_urls, start=1):
                    remaining = MAX_IMPORT_BATCH_BYTES - total_bytes
                    if remaining <= 0:
                        failed += len(photo_urls) - index + 1
                        break
                    per_file_limit = min(MAX_IMPORT_PHOTO_BYTES, remaining)
                    try:
                        destination = stage / f"p_{index:04d}.jpg"
                        written = _download_jpeg(session, url, destination, per_file_limit)
                        stored_bytes = destination.stat().st_size
                        used_bytes = max(written, stored_bytes)
                        if used_bytes <= 0 or used_bytes > per_file_limit:
                            raise ValueError("normalized photo exceeded its byte budget")
                        total_bytes += used_bytes
                        saved += 1
                    except Exception as exc:
                        failed += 1
                        print(f"[H-Import] skipped remote photo {index} for {vin}: {type(exc).__name__}")
                    time.sleep(0.15)

                if saved == 0:
                    return JSONResponse(
                        {"error": "All photo downloads failed", "photos_found": len(photo_urls)},
                        status_code=502,
                    )
                _commit_import_stage(
                    stage, import_dir, vin, meta, rec.get("title", ""), actor_email
                )
            finally:
                if stage.exists():
                    shutil.rmtree(stage, ignore_errors=True)
        finally:
            try:
                session.close()
            except Exception:
                pass

        classification, build_queued, skip_reason = None, False, ""
        try:
            with _CLASSIFICATION_SEMAPHORE:
                classification = _classify_and_route(vin)
        except Exception as exc:
            classification = {"error": f"{type(exc).__name__}: {exc}"}
        exterior_count = classification.get("exterior", 0)
        if exterior_count >= 4:
            _queue_3d_build(vin, STORAGE_DIR / vin / "_3dphotos")
            build_queued = True
        else:
            skip_reason = (
                f"only {exterior_count} exterior-orbit photos classified (need 4+) — "
                f"review /storage/{vin}/_import/classify.json or POST /api/import/reclassify/{vin}"
            )
        return {
            "vin": vin,
            "dealer": dealer,
            "title": rec.get("title", ""),
            "photos_found": len(photo_urls),
            "photos_saved": saved,
            "photos_failed": failed,
            "import_dir": f"/storage/{vin}/_import/",
            "classification": classification,
            "build_queued": build_queued,
            "build_skip_reason": skip_reason,
            "status_url": f"/api/vehicle/{vin}/3d-status",
            "viewer": f"/splat3d/{vin}",
        }
    except Exception as exc:
        return JSONResponse(
            {"error": f"Import failed: {type(exc).__name__}: {exc}"}, status_code=500
        )


def _import_vehicle_sync(dealer: str, vin: str, actor_email: str):
    with _vehicle_import_lock(vin):
        if not IMPORT_CONCURRENCY.acquire(blocking=False):
            return JSONResponse(
                {"error": "Another dealer inventory import is already running"},
                status_code=429,
                headers={"Retry-After": "10"},
            )
        try:
            return _import_vehicle_work(dealer, vin, actor_email)
        finally:
            IMPORT_CONCURRENCY.release()


def _reclassify_vehicle_sync(vin: str):
    with _vehicle_import_lock(vin):
        import_dir = STORAGE_DIR / vin / "_import"
        if import_dir.is_symlink() or not import_dir.exists() or not any(import_dir.glob("p_*.jpg")):
            return JSONResponse(
                {"error": f"No imported photo set at storage/{vin}/_import/ — run POST /api/import/{{dealer}}/{vin} first"},
                status_code=404,
            )
        try:
            with _CLASSIFICATION_SEMAPHORE:
                classification = _classify_and_route(vin)
            build_queued, skip_reason = False, ""
            if classification.get("exterior", 0) >= 4:
                _queue_3d_build(vin, STORAGE_DIR / vin / "_3dphotos")
                build_queued = True
            else:
                skip_reason = f"only {classification.get('exterior', 0)} exterior-orbit photos classified (need 4+)"
            return {
                "vin": vin,
                "classification": classification,
                "build_queued": build_queued,
                "build_skip_reason": skip_reason,
                "status_url": f"/api/vehicle/{vin}/3d-status",
                "viewer": f"/splat3d/{vin}",
            }
        except Exception as exc:
            return JSONResponse(
                {"error": f"Reclassify failed: {type(exc).__name__}: {exc}"}, status_code=500
            )


def _list_dealers_sync():
    output = []
    session = _session()
    try:
        for key, config in DEALERS.items():
            reachable = False
            try:
                status, _ = _fetch_text(
                    session, config["site"] + "/", max_bytes=1 * _MIB
                )
                reachable = 200 <= status < 400
            except Exception:
                reachable = False
            output.append({
                "dealer": key,
                "name": config["name"],
                "site": config["site"],
                "inventory_url": config["inventory_url"],
                "platform": config["platform"],
                "reachable": reachable,
            })
    finally:
        try:
            session.close()
        except Exception:
            pass
    return {"dealers": output}


# ── API ─────────────────────────────────────────────────────────────────────
@router.get("/api/import/dealers")
async def import_dealers():
    return await run_in_threadpool(_list_dealers_sync)


@router.get("/api/import/{dealer}/inventory")
async def import_inventory(dealer: str, refresh: int = 0):
    dealer = (dealer or "").lower().strip()
    if dealer not in DEALERS:
        return JSONResponse(
            {"error": f"Unknown dealer '{dealer}'. Supported: {list(DEALERS)}"},
            status_code=404,
        )
    try:
        entry = await run_in_threadpool(_get_inventory, dealer, bool(refresh))
    except Exception as exc:
        return JSONResponse(
            {"error": f"Inventory scrape failed: {type(exc).__name__}: {exc}"},
            status_code=502,
        )
    public = [
        {key: value for key, value in item.items() if not key.startswith("_")}
        for item in entry["items"]
    ]
    return {
        "dealer": dealer,
        "count": len(public),
        "cached_at": datetime.fromtimestamp(entry["ts"]).isoformat(),
        "cache_ttl_seconds": CACHE_TTL,
        "inventory": public,
    }


@router.post("/api/import/reclassify/{vin}")
async def import_reclassify(vin: str, request: Request):
    """Reclassify and rebuild an already-imported photo set for an owned vehicle."""
    vin = _clean_vin(vin)
    if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
        return JSONResponse({"error": "Provide a valid 17-character VIN"}, status_code=400)
    from main import _require_vehicle_owner
    await run_in_threadpool(_require_vehicle_owner, request, vin)
    return await run_in_threadpool(_reclassify_vehicle_sync, vin)


@router.post("/api/import/{dealer}/{vin}")
async def import_vehicle(dealer: str, vin: str, request: Request):
    from main import _authenticated_user_email

    actor_email = str(await run_in_threadpool(_authenticated_user_email, request) or "").strip().lower()
    owner_email = os.getenv("LOTVUE_OWNER_EMAIL", "").strip().lower()
    if not owner_email:
        raise HTTPException(
            status_code=503,
            detail="Dealer inventory importer requires LOTVUE_OWNER_EMAIL configuration",
        )
    if actor_email != owner_email:
        raise HTTPException(
            status_code=403,
            detail="Only the configured account owner can import external dealer inventory",
        )
    dealer = (dealer or "").lower().strip()
    if dealer not in DEALERS:
        return JSONResponse(
            {"error": f"Unknown dealer '{dealer}'. Supported: {list(DEALERS)}"},
            status_code=404,
        )
    vin = _clean_vin(vin)
    if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
        return JSONResponse({"error": "Provide a valid 17-character VIN"}, status_code=400)
    return await run_in_threadpool(_import_vehicle_sync, dealer, vin, actor_email)


def register_dealer_import(app):
    app.include_router(router)
    print(f"[H-Import] registered: dealer inventory importer ({', '.join(DEALERS)}) "
          "-> scrape + photo download + 3D pipeline hand-off")
