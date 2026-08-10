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

import io
import os
import re
import sys
import json
import time
import shutil
import sqlite3
import threading
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / "storage"
DB_PATH = BASE_DIR / "vehicles.db"

router = APIRouter()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TIMEOUT = 20          # seconds, every HTTP call
CACHE_TTL = 600       # 10 minutes
IMPORT_USER_EMAIL = os.getenv("LOTVUE_IMPORT_USER", "ahmad@abuz8ai.com")  # default dealer account

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
    """curl_cffi chrome-impersonated session (required to pass Cloudflare);
    falls back to plain requests if curl_cffi is ever missing."""
    try:
        from curl_cffi import requests as cr
        s = cr.Session(impersonate="chrome")
    except Exception:
        import requests
        s = requests.Session()
    s.headers.update({"User-Agent": UA})
    return s


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
    # DealerCenter "inv-scripts-v2" flow (site migrated ~2026-08): every
    # /inventory/ page load embeds a SIGNED serviceUrl valid only verbatim —
    # any query tampering returns 401. Paging = reload the WP page with
    # ?pager=12&page_no=N and pull that page's fresh signed URL.
    site = DEALERS["drivesmartwc"]["site"]
    headers = {"Referer": site + "/inventory/",
               "Accept": "application/json, text/javascript, */*; q=0.01",
               "X-Requested-With": "XMLHttpRequest"}
    vehicles, seen_ids, total, page_no = [], set(), None, 1
    while page_no <= 40:
        inv_html = s.get(f"{site}/inventory/?pager=12&page_no={page_no}",
                         timeout=TIMEOUT).text
        m = re.search(r'id="DWS_Async_Vehicle_Listing_Settings[^"]*">\s*(\{.*?\})\s*</script>',
                      inv_html, re.S)
        if not m:
            raise RuntimeError("drivesmartwc: DWS settings block not found (site changed again?)")
        service_url = json.loads(m.group(1))["serviceUrl"]
        r = s.get(site + service_url, headers=headers, timeout=TIMEOUT)
        if getattr(r, "status_code", 0) != 200:
            raise RuntimeError(f"drivesmartwc: inv service HTTP {r.status_code} (page {page_no})")
        body = r.text
        mj = re.match(r'^\s*[\w$.]+\((.*)\)\s*;?\s*$', body, re.S)  # optional JSONP wrap
        try:
            data = json.loads(mj.group(1) if mj else body)
        except ValueError:
            raise RuntimeError(f"drivesmartwc: page {page_no} returned non-JSON")
        batch = data.get("Vehicles") or []
        total = data.get("TotalRecordCount", total)
        fresh = [v for v in batch if v.get("Vin") and v["Vin"] not in seen_ids]
        if not fresh:
            break
        for v in fresh:
            seen_ids.add(v["Vin"])
        vehicles.extend(fresh)
        if total is not None and len(vehicles) >= total:
            break
        page_no += 1
        time.sleep(0.3)  # politeness

    items = []
    for v in vehicles:
        vin = (v.get("Vin") or "").upper()
        make, model, stock = v.get("Make") or "", v.get("Model") or "", v.get("StockNumber") or ""
        title = " ".join(str(x) for x in (v.get("Year"), make, model, v.get("Trim")) if x).strip()
        items.append({
            "vin": vin,
            "title": title,
            "price": _fmt_price(v.get("VehiclePrice") or v.get("AskingPrice")),
            "detail_url": f"{site}/inventory/{_slug(make)}/{_slug(model)}/{_slug(stock)}/",
            "photo_count": v.get("VehicleImageCount"),
            "_meta": {   # extra fields kept for import (stripped from API response)
                "year": str(v.get("Year") or ""), "make": make, "model": model,
                "trim_level": v.get("Trim") or "", "engine": v.get("Engine") or "",
                "transmission": v.get("Transmission") or "",
                "drivetrain": v.get("Drivetrain") or "", "fuel_type": v.get("FuelType") or "",
                "body_style": v.get("BodyType") or "", "stock_number": stock,
                "color": v.get("ExteriorColor") or "",
                "mileage": _fmt_price(v.get("Odometer")),
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
    while page <= 30:
        html = s.get(f"{site}/inventory/?perpage=48&page_no={page}", timeout=TIMEOUT).text
        if total is None:
            mt = re.search(r"\((\d+) vehicles\)", html)
            total = int(mt.group(1)) if mt else None
        cards = list(re.finditer(r'data-vehicle-id="vehicle-id-([A-HJ-NPR-Z0-9]{17})"', html))
        new = 0
        for idx, cm in enumerate(cards):
            vin = cm.group(1).upper()
            if vin in seen_vins:
                continue
            seg = html[cm.start(): cards[idx + 1].start() if idx + 1 < len(cards) else cm.start() + 9000]
            mh = re.search(r'href="(https://www\.unlimitedag\.com/inventory/[a-z0-9\-]+/[a-z0-9\-]+/[a-z0-9\-]+/)"', seg)
            mtl = re.search(r'data-title="([^"]+)"', seg)
            mp = re.search(r'vehicle-price-value[^>]*>\s*\$?([0-9,]+)', seg)
            seen_vins.add(vin)
            new += 1
            items.append({
                "vin": vin,
                "title": (mtl.group(1) if mtl else "").strip(),
                "price": (mp.group(1).replace(",", "") if mp else ""),
                "detail_url": mh.group(1) if mh else "",
                "photo_count": None,  # not exposed on listing pages (only on detail pages)
                "_meta": {},          # enriched from the detail page at import time
            })
        if new == 0:
            break
        if total is not None and len(items) >= total:
            break
        page += 1
        time.sleep(0.4)  # politeness
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
    entry = None if force else _cached(dealer)
    if entry:
        return entry
    s = _session()
    try:
        items = _SCRAPERS[dealer](s)
    finally:
        try:
            s.close()
        except Exception:
            pass
    return _store_cache(dealer, items)


# ── DB registration (same columns/idiom as main.py db_upsert_vehicle) ──────
def _db_register_vehicle(vin, meta, title=""):
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("""
            INSERT INTO vehicles (vin, year, make, model, exterior_frames, interior_count, created_at, watermark_mode, user_email,
                                  trim_level, engine, transmission, drivetrain, fuel_type, body_style, stock_number, turntable_style, job_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(vin) DO UPDATE SET
                year=excluded.year, make=excluded.make, model=excluded.model,
                watermark_mode=excluded.watermark_mode, user_email=excluded.user_email,
                trim_level=CASE WHEN excluded.trim_level != '' THEN excluded.trim_level ELSE trim_level END,
                engine=CASE WHEN excluded.engine != '' THEN excluded.engine ELSE engine END,
                transmission=CASE WHEN excluded.transmission != '' THEN excluded.transmission ELSE transmission END,
                drivetrain=CASE WHEN excluded.drivetrain != '' THEN excluded.drivetrain ELSE drivetrain END,
                fuel_type=CASE WHEN excluded.fuel_type != '' THEN excluded.fuel_type ELSE fuel_type END,
                body_style=CASE WHEN excluded.body_style != '' THEN excluded.body_style ELSE body_style END,
                stock_number=CASE WHEN excluded.stock_number != '' THEN excluded.stock_number ELSE stock_number END
        """, (vin, meta.get("year", ""), meta.get("make", ""), meta.get("model", ""), 0, 0,
              datetime.now().isoformat(), "preview", IMPORT_USER_EMAIL,
              meta.get("trim_level", ""), meta.get("engine", ""), meta.get("transmission", ""),
              meta.get("drivetrain", ""), meta.get("fuel_type", ""), meta.get("body_style", ""),
              meta.get("stock_number", ""), "studio_shadow", ""))
        # color/price/mileage live outside the upsert — same as main.py create_vehicle
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
            from rembg import new_session
            for m in ("isnet-general-use", "u2net", "u2netp"):
                try:
                    _rembg_sess = new_session(m)
                    print(f"[H-Import] classifier segmentation model: {m}")
                    break
                except Exception:
                    continue
        except Exception as e:
            print(f"[H-Import] rembg unavailable — classifier degrades to geometry+position: {e}")
        return _rembg_sess


def _photo_features(path: Path) -> dict:
    """Measure one photo: rembg car-mask coverage/bbox + luminance stats.
    Downscaled to ≤512px for speed. Never raises to the caller's caller —
    callers wrap in try/except and classify on partial features."""
    import numpy as np
    from PIL import Image, ImageOps
    im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    W, H = im.size
    scale = 512.0 / max(W, H)
    small = im.resize((max(1, int(W * scale)), max(1, int(H * scale))),
                      Image.BILINEAR) if scale < 1 else im
    sw, sh = small.size
    luma = np.asarray(small.convert("L"), dtype=np.float32)
    feats = {
        "img_aspect": round(W / max(H, 1), 3),          # frame w/h (portrait < 1)
        "dark_frac": round(float((luma < 60).mean()), 3),
        "luma_std": round(float(luma.std()), 1),
        "mask_frac": None, "bbox_aspect": None, "edges_touched": None,
    }
    sess = _rembg_session()
    if sess is not None:
        from rembg import remove
        a = np.array(remove(small, session=sess))[:, :, 3]
        m = a > 40
        feats["mask_frac"] = round(float(m.mean()), 4)
        ys, xs = np.where(m)
        if len(xs) > 50:
            bw = int(xs.max() - xs.min()) + 1
            bh = int(ys.max() - ys.min()) + 1
            feats["bbox_aspect"] = round(bw / max(bh, 1), 3)
            edge = 3
            feats["edges_touched"] = sum([
                int(xs.min()) <= edge, int(ys.min()) <= edge,
                int(xs.max()) >= sw - 1 - edge, int(ys.max()) >= sh - 1 - edge])
        else:
            feats["bbox_aspect"] = 0.0
            feats["edges_touched"] = 0
    return feats


OLLAMA_URL = os.environ.get("LOTVUE_OLLAMA_URL", "http://127.0.0.1:11434")
VLM_MODEL = os.environ.get("LOTVUE_VLM_MODEL", "qwen2.5vl:3b")

_VLM_PROMPT = (
    "You are sorting used-car dealership listing photos. Reply with exactly one "
    "word and nothing else.\n"
    "exterior = the whole car photographed from outside (front, side, rear, or "
    "three-quarter view; most of the car body visible).\n"
    "interior = taken inside the cabin or of the cabin/cargo space (seats, "
    "dashboard, steering wheel, console, headliner, door panel, trunk interior).\n"
    "detail = closeup of a single feature (wheel, tire, badge, grille, mirror, "
    "engine bay, window sticker, key fob, monroney label, document).\n"
    "One word answer: exterior, interior, or detail."
)


def _vlm_classify(path: Path):
    """Ask the local vision model what this photo IS. Returns (category, conf)
    or None when Ollama/model is unavailable — callers fall back to the
    geometric heuristic. Content beats geometry: the heuristic alone routed
    dashboards into 360 spins (22% error on real dealer sets, 2026-07-05)."""
    import base64
    import io as _io

    import requests as _rq
    from PIL import Image, ImageOps
    try:
        im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
        im.thumbnail((512, 512), Image.BILINEAR)
        buf = _io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode()
        r = _rq.post(f"{OLLAMA_URL}/api/generate", json={
            "model": VLM_MODEL, "prompt": _VLM_PROMPT, "images": [b64],
            "stream": False, "keep_alive": "15m",
            "options": {"temperature": 0, "num_predict": 4},
        }, timeout=45)
        if r.status_code != 200:
            return None
        word = (r.json().get("response") or "").strip().lower().split()
        cat = word[0].strip(".,:") if word else ""
        if cat in ("exterior", "interior", "detail"):
            return (cat, 0.85)
    except Exception:
        return None
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


def _classify_and_route(vin: str) -> dict:
    """Classify every downloaded photo in storage/<vin>/_import/ and route:
      exterior (original order) → storage/<vin>/_3dphotos/  (the generate-3d input dir)
      interior                 → storage/<vin>/interior/int_NNN.jpg
      detail/other             → storage/<vin>/gallery/imp_NNN.jpg
    Writes the per-photo manifest to _import/classify.json (manual-override UI story).
    Only files this importer owns (int_*.jpg / imp_*.jpg) are cleared on re-route;
    dealer-app uploads (interior_*.jpg / gallery_*.jpg) are never touched."""
    vdir = STORAGE_DIR / vin
    import_dir = vdir / "_import"
    photos = sorted(import_dir.glob("p_*.jpg"))
    if not photos:
        raise RuntimeError(f"No downloaded photos in {import_dir}")
    total = len(photos)

    manifest = []
    # PRIMARY: CLIP-L/14-336 zero-shot, batch subprocess (5/5 on the eye-audited
    # truth set 2026-07-06; the heuristic alone was 22% wrong, qwen2.5vl 2/5 on
    # closeups). -s isolates from user-site shadow packages that break
    # transformers inside the server process.
    clip_results = {}
    try:
        r = subprocess.run([sys.executable, "-s",
                            str(Path(__file__).parent / "clip_classify.py"),
                            str(import_dir)],
                           capture_output=True, text=True, timeout=600)
        data = json.loads(r.stdout or "{}")
        if data.get("ok"):
            clip_results = {e["file"]: e for e in data["photos"]}
    except Exception:
        clip_results = {}

    for i, p in enumerate(photos):
        try:
            feats = _photo_features(p)
        except Exception as e:
            feats = {"error": f"{type(e).__name__}: {e}"}
        clip_e = clip_results.get(p.name)
        if clip_e:
            cat, conf, method = clip_e["category"], clip_e["confidence"], "clip"
        else:                                  # CLIP unavailable → vlm → heuristic
            h_cat, h_conf = classify_photo(feats, i, total)
            vlm = _vlm_classify(p)
            if vlm is not None:
                cat, conf = vlm
                method = "vlm+heuristic" if cat == h_cat else "vlm"
                conf = 0.97 if cat == h_cat else conf
            else:
                cat, conf, method = h_cat, h_conf, "heuristic"
        manifest.append({"file": p.name, "index": i, "category": cat,
                         "confidence": conf, "method": method, "features": feats})

    (import_dir / "classify.json").write_text(
        json.dumps({"vin": vin, "classified_at": datetime.now().isoformat(),
                    "total": total, "photos": manifest}, indent=1),
        encoding="utf-8")

    ext_dir = vdir / "_3dphotos"                       # same dir generate-3d feeds gaussian_3d
    if ext_dir.exists():
        shutil.rmtree(ext_dir, ignore_errors=True)
    ext_dir.mkdir(parents=True, exist_ok=True)
    int_dir = vdir / "interior"; int_dir.mkdir(parents=True, exist_ok=True)
    gal_dir = vdir / "gallery"; gal_dir.mkdir(parents=True, exist_ok=True)
    for old in list(int_dir.glob("int_*.jpg")) + list(gal_dir.glob("imp_*.jpg")):
        try:
            old.unlink()
        except Exception:
            pass

    ne = ni = ng = 0
    for entry in manifest:
        src = import_dir / entry["file"]
        if entry["category"] == "exterior":
            ne += 1; shutil.copy(src, ext_dir / f"p_{ne:04d}.jpg")
        elif entry["category"] == "interior":
            ni += 1; shutil.copy(src, int_dir / f"int_{ni:03d}.jpg")
        else:
            ng += 1; shutil.copy(src, gal_dir / f"imp_{ng:03d}.jpg")

    try:  # keep dashboard counts + tab flags honest (best-effort)
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("""UPDATE vehicles SET interior_count=?,
                        show_interior=CASE WHEN ?>0 THEN 1 ELSE show_interior END,
                        show_gallery=CASE WHEN ?>0 THEN 1 ELSE show_gallery END
                        WHERE vin=?""", (ni, ni, ng, vin))
        conn.commit(); conn.close()
    except Exception:
        pass

    return {"total": total, "exterior": ne, "interior": ni, "detail": ng,
            "exterior_dir": str(ext_dir),
            "manifest": f"/storage/{vin}/_import/classify.json"}


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


# ── API ─────────────────────────────────────────────────────────────────────
@router.get("/api/import/dealers")
async def import_dealers():
    out = []
    s = _session()
    try:
        for key, cfg in DEALERS.items():
            reachable = False
            try:
                r = s.get(cfg["site"] + "/", timeout=10)
                reachable = 200 <= getattr(r, "status_code", 0) < 400
            except Exception:
                reachable = False
            out.append({"dealer": key, "name": cfg["name"], "site": cfg["site"],
                        "inventory_url": cfg["inventory_url"], "platform": cfg["platform"],
                        "reachable": reachable})
    finally:
        try:
            s.close()
        except Exception:
            pass
    return {"dealers": out}


@router.get("/api/import/{dealer}/inventory")
async def import_inventory(dealer: str, refresh: int = 0):
    dealer = (dealer or "").lower().strip()
    if dealer not in DEALERS:
        return JSONResponse({"error": f"Unknown dealer '{dealer}'. Supported: {list(DEALERS)}"}, status_code=404)
    try:
        entry = _get_inventory(dealer, force=bool(refresh))
    except Exception as e:
        return JSONResponse({"error": f"Inventory scrape failed: {e}"}, status_code=502)
    public = [{k: v for k, v in item.items() if not k.startswith("_")} for item in entry["items"]]
    return {"dealer": dealer, "count": len(public),
            "cached_at": datetime.fromtimestamp(entry["ts"]).isoformat(),
            "cache_ttl_seconds": CACHE_TTL, "inventory": public}


@router.post("/api/import/reclassify/{vin}")
async def import_reclassify(vin: str):
    """Re-run classification + routing on an already-imported car's _import/ set
    WITHOUT re-downloading, then re-queue the spin build from exterior shots only.
    Auth-gated by the app-wide auth middleware (all /api/import/* writes are gated).
    NOTE: must be registered BEFORE /api/import/{dealer}/{vin} or that dynamic
    route swallows 'reclassify' as a dealer name."""
    vin = _clean_vin(vin)
    if not vin:
        return JSONResponse({"error": "Provide a VIN"}, status_code=400)
    import_dir = STORAGE_DIR / vin / "_import"
    if not import_dir.exists() or not any(import_dir.glob("p_*.jpg")):
        return JSONResponse(
            {"error": f"No imported photo set at storage/{vin}/_import/ — "
                      f"run POST /api/import/{{dealer}}/{vin} first"}, status_code=404)
    try:
        classification = _classify_and_route(vin)
        build_queued, skip_reason = False, ""
        if classification.get("exterior", 0) >= 4:
            _queue_3d_build(vin, STORAGE_DIR / vin / "_3dphotos")
            build_queued = True
        else:
            skip_reason = (f"only {classification.get('exterior', 0)} exterior-orbit "
                           f"photos classified (need 4+)")
        return {"vin": vin, "classification": classification,
                "build_queued": build_queued, "build_skip_reason": skip_reason,
                "status_url": f"/api/vehicle/{vin}/3d-status", "viewer": f"/splat3d/{vin}"}
    except Exception as e:
        return JSONResponse({"error": f"Reclassify failed: {type(e).__name__}: {e}"}, status_code=500)


@router.post("/api/import/{dealer}/{vin}")
async def import_vehicle(dealer: str, vin: str):
    dealer = (dealer or "").lower().strip()
    if dealer not in DEALERS:
        return JSONResponse({"error": f"Unknown dealer '{dealer}'. Supported: {list(DEALERS)}"}, status_code=404)
    vin = _clean_vin(vin)
    if not vin:
        return JSONResponse({"error": "Provide a VIN"}, status_code=400)

    try:
        entry = _get_inventory(dealer)
        rec = entry["by_vin"].get(vin)
        if not rec:  # maybe stale cache — refresh once
            entry = _get_inventory(dealer, force=True)
            rec = entry["by_vin"].get(vin)
        if not rec:
            return JSONResponse(
                {"error": f"VIN {vin} not found in {dealer} live inventory ({len(entry['items'])} vehicles)"},
                status_code=404)
        if not rec.get("detail_url"):
            return JSONResponse({"error": f"No detail URL for VIN {vin}"}, status_code=502)

        s = _session()
        try:
            r = s.get(rec["detail_url"], timeout=TIMEOUT,
                      headers={"Referer": DEALERS[dealer]["inventory_url"]})
            if getattr(r, "status_code", 0) != 200:
                return JSONResponse({"error": f"Detail page HTTP {r.status_code}: {rec['detail_url']}"},
                                    status_code=502)
            detail_html = r.text

            photo_urls = _PHOTO_EXTRACTORS[dealer](detail_html)
            if not photo_urls:
                return JSONResponse({"error": "No photos found on detail page", "detail_url": rec["detail_url"]},
                                    status_code=502)

            # vehicle metadata: listing feed (drivesmartwc) or detail-page blob (unlimitedag)
            meta = dict(rec.get("_meta") or {})
            if dealer == "unlimitedag":
                meta = {**_detail_meta_unlimitedag(detail_html), **{k: v for k, v in meta.items() if v}}
            if not meta.get("price") and rec.get("price"):
                meta["price"] = rec["price"]

            # download every full-size photo → STORAGE_DIR/<VIN>/_import/
            import_dir = STORAGE_DIR / vin / "_import"
            import_dir.mkdir(parents=True, exist_ok=True)
            for old in import_dir.glob("*.jpg"):
                try:
                    old.unlink()
                except Exception:
                    pass
            saved, failed = 0, 0
            for i, url in enumerate(photo_urls):
                try:
                    pr = s.get(url, timeout=TIMEOUT)
                    data = pr.content if getattr(pr, "status_code", 0) == 200 else b""
                    if len(data) > 1000 and data[:3] == b"\xff\xd8\xff":
                        (import_dir / f"p_{i + 1:04d}.jpg").write_bytes(data)
                        saved += 1
                    else:
                        failed += 1
                except Exception:
                    failed += 1
                time.sleep(0.15)  # politeness to the image CDN
        finally:
            try:
                s.close()
            except Exception:
                pass

        if saved == 0:
            return JSONResponse({"error": "All photo downloads failed", "photos_found": len(photo_urls)},
                                status_code=502)

        # register/update the vehicle row (default dealer account, like existing uploads)
        _db_register_vehicle(vin, meta, title=rec.get("title", ""))

        # classify + route: exterior→_3dphotos, interior→interior/, detail→gallery/
        classification, build_queued, skip_reason = None, False, ""
        try:
            classification = _classify_and_route(vin)
        except Exception as e:
            classification = {"error": f"{type(e).__name__}: {e}"}
        n_ext = classification.get("exterior", 0)
        if n_ext >= 4:  # generate-3d requires ≥4 photos; spins train ONLY on exterior shots
            _queue_3d_build(vin, STORAGE_DIR / vin / "_3dphotos")
            build_queued = True
        else:
            skip_reason = (f"only {n_ext} exterior-orbit photos classified (need 4+) — "
                           f"review /storage/{vin}/_import/classify.json or POST "
                           f"/api/import/reclassify/{vin}")

        return {"vin": vin, "dealer": dealer, "title": rec.get("title", ""),
                "photos_found": len(photo_urls), "photos_saved": saved, "photos_failed": failed,
                "import_dir": str(import_dir), "classification": classification,
                "build_queued": build_queued, "build_skip_reason": skip_reason,
                "status_url": f"/api/vehicle/{vin}/3d-status", "viewer": f"/splat3d/{vin}"}
    except Exception as e:
        return JSONResponse({"error": f"Import failed: {type(e).__name__}: {e}"}, status_code=500)


def register_dealer_import(app):
    app.include_router(router)
    print(f"[H-Import] registered: dealer inventory importer ({', '.join(DEALERS)}) "
          f"-> scrape + photo download + 3D pipeline hand-off")
