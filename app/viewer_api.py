"""
viewer_api.py — LotVue Pro unified viewer backend (P2)
Serves the per-vehicle render manifest that <lotvue-viewer> consumes, and the
embeddable VDP page. One manifest, three tiers: spin / splat / glb.
"""
import heapq
import html
import json
import math
import os
import re
import sqlite3
from pathlib import Path, PurePosixPath

from app_paths import BASE_DIR, DB_PATH, STORAGE_DIR
from fastapi import HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

STORAGE_ROOTS = (STORAGE_DIR, BASE_DIR / "vehicles")

SEV_ORDER = {"major": 0, "moderate": 1, "minor": 2}


def _db_hotspots(vin: str):
    """Dealer-placed hotspots (feature + damage) from the DB — the same rows
    /api/hotspot writes. Returns [] on any error so the viewer never breaks."""
    conn = None
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=2)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, photo_filename, tag, hotspot_x, hotspot_y, condition_type,"
            " severity, note, close_up FROM hotspots WHERE vin=? ORDER BY id DESC LIMIT 500",
            (vin.upper(),)
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()

def validate_vehicle_id(vin: str) -> str:
    """Constrain public viewer identifiers before using them in paths/markup."""
    value = str(vin or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{1,32}", value):
        raise HTTPException(400, "Invalid vehicle identifier")
    return value


def _read_json_limited(path: Path, max_bytes: int):
    """Read bounded JSON without trusting a prior stat check."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
        if len(raw) > max_bytes:
            return None
        return json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None


def _safe_vehicle_path(vehicle_dir: Path, relative: str | Path) -> Path | None:
    """Resolve a child without following symlinks within or out of a vehicle tree."""
    try:
        relative_path = PurePosixPath(str(relative).replace("\\", "/"))
        if relative_path.is_absolute():
            return None
        root = vehicle_dir.resolve(strict=True)
        candidate = root
        for part in relative_path.parts:
            if part in {"", ".", ".."}:
                return None
            candidate = candidate / part
            if candidate.is_symlink():
                return None
        resolved = candidate.resolve(strict=True)
        return resolved if resolved.is_relative_to(root) else None
    except (OSError, RuntimeError, ValueError):
        return None


def _vehicle_dir(vin: str) -> Path:
    vin = validate_vehicle_id(vin)
    try:
        with sqlite3.connect(str(DB_PATH), timeout=2) as conn:
            row = conn.execute("SELECT status FROM vehicles WHERE vin=?", (vin,)).fetchone()
    except sqlite3.Error as exc:
        raise HTTPException(503, "Vehicle listing is unavailable") from exc
    if not row or row[0] != "active":
        raise HTTPException(404, "Vehicle not found")
    for root in STORAGE_ROOTS:
        resolved_root = root.resolve()
        candidate = resolved_root / vin
        try:
            if candidate.is_symlink():
                continue
            directory = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if directory.parent == resolved_root and directory.name == vin and directory.is_dir():
            return directory
    raise HTTPException(404, "Vehicle not found")

def _spin_frames(d: Path, vin: str):
    # polished set wins when present (borders + dealer banner stripped,
    # locked-off framing, non-orbit frames removed). Bound the public manifest
    # to 120 assets to match the viewer's client-side memory limit.
    spin = _safe_vehicle_path(d, "spin_hd")
    if spin is None or not spin.is_dir():
        spin = _safe_vehicle_path(d, "spin")
    if spin is None or not spin.is_dir():
        return []
    allowed = (".jpg", ".jpeg", ".png", ".webp")
    try:
        with os.scandir(spin) as entries:
            names = heapq.nsmallest(
                120,
                (entry.name for entry in entries
                 if not entry.is_symlink()
                 and entry.is_file(follow_symlinks=False)
                 and entry.name.rsplit(".", 1)[-1].lower() in {"jpg", "jpeg", "png", "webp"}
                 and not entry.name.startswith(".")
                 and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", entry.name)
                 and "." in entry.name
                 and Path(entry.name).suffix.lower() in allowed),
            )
    except OSError:
        return []
    return [f"/storage/{vin}/{spin.name}/{name}" for name in names]

def build_manifest(vin: str) -> dict:
    vin = validate_vehicle_id(vin)
    d = _vehicle_dir(vin)
    man = {"vin": vin, "spin_frames": _spin_frames(d, vin)}
    for relative in ("splat/scene.ply", "model.ply", "3d/model.ply"):
        candidate = _safe_vehicle_path(d, relative)
        if candidate and candidate.is_file() and candidate.stat().st_size > 100_000:
            man["splat_url"] = f"/storage/{vin}/{relative}"
            break
    glb = _safe_vehicle_path(d, "model.glb")
    if glb and glb.is_file():
        man["glb_url"] = f"/storage/{vin}/model.glb"
        man["paint"] = ["#0b1f3a", "#7a0d16", "#101010", "#c9ccd2", "#f2f3f5"]
    interior = _safe_vehicle_path(d, "interior.jpg")
    if interior and interior.is_file():
        man["interior_url"] = f"/storage/{vin}/interior.jpg"
    hs = _safe_vehicle_path(d, "hotspots.json")
    if hs:
        payload = _read_json_limited(hs, 1_000_000)
        if isinstance(payload, list):
            man["hotspots"] = payload[:500]
    # merge dealer-placed DB hotspots (feature pins + damage layer)
    db_hs = _db_hotspots(vin)
    if db_hs:
        frame_names = [u.rsplit("/", 1)[-1] for u in man["spin_frames"]]
        merged = list(man.get("hotspots", []))
        for h in db_hs:
            try:
                frame = frame_names.index(h["photo_filename"])
            except ValueError:
                frame = 0
            body = h["note"] or ""
            if h["condition_type"]:
                body = f"[{h['severity'] or 'noted'} {h['condition_type']}] {body}".strip()
            merged.append({"frame": frame, "u": h["hotspot_x"], "v": h["hotspot_y"],
                           "label": h["tag"], "body": body,
                           "damage": bool(h["condition_type"]),
                           "severity": h["severity"] or ""})
        man["hotspots"] = merged
    meta = _safe_vehicle_path(d, "meta.json")
    if meta:
        payload = _read_json_limited(meta, 1_000_000)
        vehicle = payload.get("vehicle", payload) if isinstance(payload, dict) else {}
        public_fields = {
            "vin", "year", "make", "model", "trim", "trim_level", "price", "mileage",
            "color", "description", "engine", "transmission", "drivetrain", "fuel_type",
            "body_style", "stock_number", "video_url", "video_type", "show_video",
            "show_interior", "show_gallery", "turntable_style", "background_id",
        }
        if isinstance(vehicle, dict):
            man["vehicle"] = {
                key: value[:4000] if isinstance(value, str) else value
                for key, value in vehicle.items()
                if key in public_fields and isinstance(value, (str, int, float, bool))
            }
    if not man.get("vehicle"):
        # upload flow writes the DB row, not meta.json — fall back so a fresh
        # dealer's first vehicle gets a real title on /vdp, not a bare VIN
        try:
            with sqlite3.connect(str(DB_PATH), timeout=2) as conn:
                conn.row_factory = sqlite3.Row
                r = conn.execute("SELECT year, make, model, price, mileage "
                                 "FROM vehicles WHERE vin=?", (vin.upper(),)).fetchone()
                if r:
                    fields = ("year", "make", "model", "price", "mileage")
                    man["vehicle"] = {key: r[key] for key in fields if r[key]}
        except sqlite3.Error as exc:
            print(f"[Viewer-API] vehicle metadata unavailable for {vin}: {exc}")
    return man

VDP_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — LotVue Pro</title>
<style>
 body{{margin:0;background:#0a1226;color:#e2eaf8;font-family:-apple-system,'Segoe UI',Roboto,sans-serif}}
 .wrap{{max-width:1100px;margin:0 auto;padding:18px}}
 lotvue-spin{{display:block;height:64vh;min-height:400px}}
 h1{{font-size:22px;margin:14px 0 4px}} .sub{{color:#8ea6d4;font-size:13px}}
 .row{{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap}}
 .row a{{background:rgba(90,140,255,.2);border:1px solid rgba(120,165,255,.45);color:#dfe8ff;
   text-decoration:none;border-radius:9px;padding:9px 16px;font-size:13.5px}}
 #bdc{{margin-top:18px;background:rgba(255,255,255,.05);border:1px solid rgba(150,180,235,.16);
   border-radius:12px;padding:14px}}
 #bdc input{{width:100%;box-sizing:border-box;background:rgba(10,18,40,.8);border:1px solid rgba(120,165,255,.35);
   color:#e6eeff;border-radius:8px;padding:10px 12px;font-size:14px}}
 #log div{{margin:8px 0;font-size:13.5px;line-height:1.5}} #log .q{{color:#9fb8e8}}
</style></head><body><div class="wrap">
<lotvue-spin vin="{vin}"></lotvue-spin>
<h1>{title}</h1><div class="sub">{sub}</div>
<div class="row">
 <a href="/api/leadmagnet/book-test-drive?vin={vin}">Book a test drive</a>
 <a href="/api/leadmagnet/{vin}/price-advisor">Price advisor</a>
 <a href="/api/leadmagnet/request-history?vin={vin}">Vehicle history</a>
 <a href="/vdp/{vin}/damage-report">Condition report</a>
</div>
<div id="bdc"><div id="log"><div>Ask me anything about this vehicle — I only answer from its real specs.</div></div>
 <input id="q" placeholder="e.g. Does it have AWD? What's the mileage?" autocomplete="off"></div>
<script type="module">import '/static/lotvue_spin.js';</script>
<script>
const vin={vin_js};
document.getElementById('q').addEventListener('keydown', async e => {{
  if (e.key !== 'Enter' || !e.target.value.trim()) return;
  const q = e.target.value.trim(); e.target.value = '';
  const log = document.getElementById('log');
  const questionLine = document.createElement('div');
  questionLine.className = 'q';
  questionLine.textContent = 'You: ' + q;
  log.appendChild(questionLine);
  try {{
    const r = await fetch('/api/chat/' + vin, {{method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{message: q}})}});
    const j = await r.json();
    const answerLine = document.createElement('div');
    answerLine.textContent = String(j.reply || j.response || j.answer || 'One moment…');
    log.appendChild(answerLine);
  }} catch (err) {{
    const errorLine = document.createElement('div');
    errorLine.textContent = 'Connection hiccup — try again.';
    log.appendChild(errorLine);
  }}
  log.scrollTop = log.scrollHeight;
}});
</script></div></body></html>"""

def register_viewer(app):
    @app.get("/api/viewer/{vin}/manifest")
    def viewer_manifest(vin: str):
        return JSONResponse(build_manifest(validate_vehicle_id(vin)))

    @app.get("/vdp/{vin}", response_class=HTMLResponse)
    def vdp_page(vin: str):
        vin = validate_vehicle_id(vin)
        man = build_manifest(vin)
        v = man.get("vehicle", {})
        title = " ".join(str(v.get(k, "") or "") for k in ("year", "make", "model")).strip() or f"VIN {vin}"
        bits = [str(v[k]) for k in ("trim", "trim_level", "mileage", "price") if v.get(k)]
        return VDP_HTML.format(vin=html.escape(vin, quote=True), vin_js=json.dumps(vin),
                               title=html.escape(title, quote=True),
                               sub=html.escape(" · ".join(bits) or "LotVue Pro 3D listing", quote=True))

    @app.get("/vdp/{vin}/damage-report", response_class=HTMLResponse)
    def damage_report(vin: str):
        """Printable condition/damage summary — damage-tagged hotspots with
        severity, notes, close-ups, and dot-marked frames. Public read."""
        vin = validate_vehicle_id(vin)
        man = build_manifest(vin)
        v = man.get("vehicle", {})
        title = " ".join(str(v.get(k, "") or "") for k in ("year", "make", "model")).strip() or f"VIN {vin}"
        rows = [h for h in _db_hotspots(vin) if h.get("condition_type")]
        rows.sort(key=lambda h: SEV_ORDER.get(h.get("severity"), 3))
        frames = man.get("spin_frames", [])
        frame_by_name = {PurePosixPath(u).name: u for u in frames}
        counts = {s: sum(1 for h in rows if h.get("severity") == s)
                  for s in ("major", "moderate", "minor")}
        chips = " ".join(f'<span class="chip {s}">{counts[s]} {s}</span>'
                         for s in ("major", "moderate", "minor") if counts[s])

        def safe_image_name(value):
            name = str(value or "")
            if (PurePosixPath(name).name != name
                    or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name)
                    or name.startswith(".")
                    or PurePosixPath(name).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}):
                return ""
            return name

        def safe_percent(value):
            try:
                number = float(value)
            except (TypeError, ValueError):
                return 50.0
            if not math.isfinite(number):
                return 50.0
            return min(100.0, max(0.0, number * 100))

        items = []
        for i, h in enumerate(rows, 1):
            photo_name = safe_image_name(h.get("photo_filename"))
            frame_url = frame_by_name.get(photo_name, "")
            if frame_url:
                x = safe_percent(h.get("hotspot_x", 0.5))
                y = safe_percent(h.get("hotspot_y", 0.5))
                img = (f'<div class="shot"><img src="{html.escape(frame_url, quote=True)}" alt="frame">'
                       f'<span class="dot" style="left:{x:.1f}%;top:{y:.1f}%"></span></div>')
            else:
                img = ""
            close_name = safe_image_name(h.get("close_up"))
            close = (f'<img class="close" src="/storage/{vin}/hotspot_photos/{close_name}" '
                     'alt="close-up">') if close_name else ""
            tag = html.escape(str(h.get("tag") or ""), quote=True)
            severity = h.get("severity") if h.get("severity") in SEV_ORDER else "minor"
            condition = html.escape(str(h.get("condition_type") or ""), quote=True)
            note = html.escape(str(h.get("note") or ""), quote=False)
            items.append(
                f'<div class="item"><div class="head"><b>#{i} {tag}</b>'
                f'<span class="chip {severity}">{severity} {condition}</span></div>'
                f'{img}{close}<p>{note}</p></div>')
        body = "".join(items) or ('<p class="clean">No damage recorded. Every hotspot on '
                                  'this vehicle is a feature callout — nothing tagged as '
                                  'scratch, dent, chip, rust or crack.</p>')
        title_html = html.escape(title, quote=True)
        return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Condition report — {title_html}</title><style>
 body{{margin:0;background:#fff;color:#16202e;font-family:-apple-system,'Segoe UI',Roboto,sans-serif}}
 .wrap{{max-width:820px;margin:0 auto;padding:28px 22px}}
 h1{{font-size:22px;margin:0 0 2px}} .sub{{color:#5c6c82;font-size:13px;margin-bottom:14px}}
 .chip{{display:inline-block;border-radius:20px;padding:2px 10px;font-size:12px;font-weight:600;
   margin-left:8px;color:#fff;background:#8a97a8;text-transform:uppercase}}
 .chip.major{{background:#c0392b}} .chip.moderate{{background:#d68910}} .chip.minor{{background:#7d8ea5}}
 .item{{border:1px solid #dde4ee;border-radius:10px;padding:14px;margin:14px 0;page-break-inside:avoid}}
 .head{{display:flex;align-items:center;justify-content:space-between}}
 .shot{{position:relative;margin-top:10px}} .shot img{{width:100%;border-radius:8px;display:block}}
 .dot{{position:absolute;width:22px;height:22px;border-radius:50%;border:3px solid #e74c3c;
   background:rgba(231,76,60,.25);transform:translate(-50%,-50%)}}
 img.close{{max-width:280px;border-radius:8px;margin-top:10px;display:block}}
 .clean{{color:#3f9142;font-size:15px;border:1px solid #cfe6d0;background:#f2faf2;
   border-radius:10px;padding:16px}}
 .print{{margin:16px 0}} @media print{{.print{{display:none}}}}
 p{{font-size:14px;line-height:1.5;color:#39485c}}
</style></head><body><div class="wrap">
<h1>Condition report — {title_html}</h1>
<div class="sub">VIN {vin} · generated by LotVue Pro {chips}</div>
<button class="print" onclick="window.print()">Print / save as PDF</button>
{body}
</div></body></html>"""

    print("[Viewer-API] registered: /api/viewer/{vin}/manifest, /vdp/{vin}, /embed/{vin} (3-tier unified viewer)")
