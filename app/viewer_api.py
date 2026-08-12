"""
viewer_api.py — LotVue Pro unified viewer backend (P2)
Serves the per-vehicle render manifest that <lotvue-viewer> consumes, and the
embeddable VDP page. One manifest, three tiers: spin / splat / glb.
"""
import json
import sqlite3
from pathlib import Path
from fastapi import HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

STORAGE_ROOTS = (Path(__file__).parent / "storage", Path(__file__).parent / "vehicles")
DB_PATH = Path(__file__).parent / "vehicles.db"

SEV_ORDER = {"major": 0, "moderate": 1, "minor": 2}


def _db_hotspots(vin: str):
    """Dealer-placed hotspots (feature + damage) from the DB — the same rows
    /api/hotspot writes. Returns [] on any error so the viewer never breaks."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, photo_filename, tag, hotspot_x, hotspot_y, condition_type,"
            " severity, note, close_up FROM hotspots WHERE vin=?", (vin.upper(),)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []

def _vehicle_dir(vin: str) -> Path:
    for root in STORAGE_ROOTS:
        d = root / vin
        if d.exists():
            return d
    raise HTTPException(404, f"unknown vin {vin}")

def _spin_frames(d: Path, vin: str):
    # polished set wins when present (borders + dealer banner stripped,
    # locked-off framing, non-orbit frames removed)
    spin = d / "spin_hd"
    if not spin.exists():
        spin = d / "spin"
    if not spin.exists():
        return []
    frames = sorted(p.name for p in spin.iterdir()
                    if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
    return [f"/storage/{vin}/{spin.name}/{n}" for n in frames]

def build_manifest(vin: str) -> dict:
    d = _vehicle_dir(vin)
    man = {"vin": vin, "spin_frames": _spin_frames(d, vin)}
    for cand in (d / "splat" / "scene.ply", d / "model.ply", d / "3d" / "model.ply"):
        if cand.exists() and cand.stat().st_size > 100_000:
            man["splat_url"] = f"/storage/{vin}/{cand.relative_to(d).as_posix()}"
            break
    glb = d / "model.glb"
    if glb.exists():
        man["glb_url"] = f"/storage/{vin}/model.glb"
        man["paint"] = ["#0b1f3a", "#7a0d16", "#101010", "#c9ccd2", "#f2f3f5"]
    interior = d / "interior.jpg"
    if interior.exists():
        man["interior_url"] = f"/storage/{vin}/interior.jpg"
    hs = d / "hotspots.json"
    if hs.exists():
        try:
            man["hotspots"] = json.loads(hs.read_text("utf-8"))
        except Exception:
            pass
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
    meta = d / "meta.json"
    if meta.exists():
        try:
            man["vehicle"] = json.loads(meta.read_text("utf-8"))
        except Exception:
            pass
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
  log.insertAdjacentHTML('beforeend', '<div class="q">You: ' + q.replace(/</g,'&lt;') + '</div>');
  try {{
    const r = await fetch('/api/chat/' + vin, {{method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{message: q}})}});
    const j = await r.json();
    log.insertAdjacentHTML('beforeend', '<div>' + (j.reply || j.response || j.answer || 'One moment…').replace(/</g,'&lt;') + '</div>');
  }} catch (err) {{ log.insertAdjacentHTML('beforeend', '<div>Connection hiccup — try again.</div>'); }}
  log.scrollTop = log.scrollHeight;
}});
</script></div></body></html>"""

def register_viewer(app):
    @app.get("/api/viewer/{vin}/manifest")
    async def viewer_manifest(vin: str):
        return JSONResponse(build_manifest(vin))

    @app.get("/vdp/{vin}", response_class=HTMLResponse)
    async def vdp_page(vin: str):
        man = build_manifest(vin)
        v = man.get("vehicle", {})
        title = " ".join(str(v.get(k, "")) for k in ("year", "make", "model")).strip() or f"VIN {vin}"
        bits = [str(v[k]) for k in ("trim", "mileage", "price") if v.get(k)]
        return VDP_HTML.format(vin=vin, vin_js=json.dumps(vin), title=title,
                               sub=" · ".join(bits) or "LotVue Pro 3D listing")

    @app.get("/vdp/{vin}/damage-report", response_class=HTMLResponse)
    async def damage_report(vin: str):
        """Printable condition/damage summary — damage-tagged hotspots with
        severity, notes, close-ups, and dot-marked frames. Public read."""
        man = build_manifest(vin)
        v = man.get("vehicle", {})
        title = " ".join(str(v.get(k, "")) for k in ("year", "make", "model")).strip() or f"VIN {vin}"
        rows = [h for h in _db_hotspots(vin) if h["condition_type"]]
        rows.sort(key=lambda h: SEV_ORDER.get(h["severity"], 3))
        frames = man.get("spin_frames", [])
        counts = {s: sum(1 for h in rows if h["severity"] == s)
                  for s in ("major", "moderate", "minor")}
        chips = " ".join(f'<span class="chip {s}">{counts[s]} {s}</span>'
                         for s in ("major", "moderate", "minor") if counts[s])
        items = []
        for i, h in enumerate(rows, 1):
            frame_url = ""
            for u in frames:
                if u.endswith("/" + h["photo_filename"]):
                    frame_url = u
                    break
            img = (f'<div class="shot"><img src="{frame_url}" alt="frame">'
                   f'<span class="dot" style="left:{h["hotspot_x"]*100:.1f}%;'
                   f'top:{h["hotspot_y"]*100:.1f}%"></span></div>') if frame_url else ""
            close = (f'<img class="close" src="/storage/{vin}/hotspot_photos/{h["close_up"]}" '
                     'alt="close-up">') if h["close_up"] else ""
            note = (h["note"] or "").replace("<", "&lt;")
            items.append(
                f'<div class="item"><div class="head"><b>#{i} {h["tag"]}</b>'
                f'<span class="chip {h["severity"]}">{h["severity"]} {h["condition_type"]}</span></div>'
                f'{img}{close}<p>{note}</p></div>')
        body = "".join(items) or ('<p class="clean">No damage recorded. Every hotspot on '
                                  'this vehicle is a feature callout — nothing tagged as '
                                  'scratch, dent, chip, rust or crack.</p>')
        return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Condition report — {title}</title><style>
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
<h1>Condition report — {title}</h1>
<div class="sub">VIN {vin} · generated by LotVue Pro {chips}</div>
<button class="print" onclick="window.print()">Print / save as PDF</button>
{body}
</div></body></html>"""

    @app.get("/embed/{vin}", response_class=HTMLResponse)
    async def embed_page(vin: str):
        return ('<!DOCTYPE html><html><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<style>html,body{margin:0;height:100%;background:#0a1226}'
                'lotvue-spin{display:block;height:100%}</style></head><body>'
                f'<lotvue-spin vin="{vin}"></lotvue-spin>'
                '<script type="module">import "/static/lotvue_spin.js";</script>'
                '</body></html>')

    print("[Viewer-API] registered: /api/viewer/{vin}/manifest, /vdp/{vin}, /embed/{vin} (3-tier unified viewer)")
