"""
viewer_api.py — LotVue Pro unified viewer backend (P2)
Serves the per-vehicle render manifest that <lotvue-viewer> consumes, and the
embeddable VDP page. One manifest, three tiers: spin / splat / glb.
"""
import json
from pathlib import Path
from fastapi import HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

STORAGE_ROOTS = (Path(__file__).parent / "storage", Path(__file__).parent / "vehicles")

def _vehicle_dir(vin: str) -> Path:
    for root in STORAGE_ROOTS:
        d = root / vin
        if d.exists():
            return d
    raise HTTPException(404, f"unknown vin {vin}")

def _spin_frames(d: Path, vin: str):
    spin = d / "spin"
    if not spin.exists():
        return []
    frames = sorted(p.name for p in spin.iterdir()
                    if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
    return [f"/storage/{vin}/spin/{n}" for n in frames]

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
