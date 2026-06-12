"""
F-Control — full dealer control over vehicle media.
Fixes "I couldn't even delete the old photos": per-photo delete across every
category, clear-and-reshoot, re-finalize, plus a /manage/{vin} management page.

Wired from main.py:  from f_control import register_control; register_control(app)
"""

import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / "storage"

router = APIRouter()

# storage subdir → friendly category
_CATEGORIES = {
    "exterior": "exterior",   # raw uploaded exterior frames
    "spin": "spin",           # processed 360 spin frames
    "gallery": "gallery",
    "interior": "interior",
    "panoramas": "panoramas",
}
_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def _vehicle_dir(vin: str) -> Path:
    d = STORAGE_DIR / vin.upper()
    if not d.exists():
        raise HTTPException(404, "Vehicle not found")
    return d


def _list_photos(vin: str):
    vdir = _vehicle_dir(vin)
    out = {}
    for cat, sub in _CATEGORIES.items():
        d = vdir / sub
        files = []
        if d.exists():
            for f in sorted(d.iterdir()):
                if f.suffix.lower() in _EXTS:
                    files.append({"filename": f.name,
                                  "url": f"/storage/{vin.upper()}/{sub}/{f.name}",
                                  "size": f.stat().st_size})
        out[cat] = files
    return out


@router.get("/api/vehicle/{vin}/photos")
async def list_photos(vin: str):
    photos = _list_photos(vin)
    return {"vin": vin.upper(), "photos": photos,
            "counts": {k: len(v) for k, v in photos.items()}}


@router.delete("/api/vehicle/{vin}/photo")
async def delete_photo(vin: str, category: str, filename: str):
    """Delete ONE photo. category in {exterior,spin,gallery,interior,panoramas}."""
    sub = _CATEGORIES.get(category)
    if not sub:
        raise HTTPException(400, f"category must be one of {list(_CATEGORIES)}")
    vdir = _vehicle_dir(vin)
    # filename is a basename only — never allow path traversal
    fn = Path(filename).name
    target = (vdir / sub / fn)
    if not target.exists() or target.parent != (vdir / sub):
        raise HTTPException(404, "Photo not found")
    target.unlink()
    return {"status": "deleted", "vin": vin.upper(), "category": category, "filename": fn}


@router.post("/api/vehicle/{vin}/photos/clear")
async def clear_category(vin: str, category: str):
    """Clear a whole category (e.g. wipe exterior to re-shoot the 360)."""
    sub = _CATEGORIES.get(category)
    if not sub:
        raise HTTPException(400, f"category must be one of {list(_CATEGORIES)}")
    d = _vehicle_dir(vin) / sub
    n = 0
    if d.exists():
        for f in d.iterdir():
            if f.suffix.lower() in _EXTS:
                f.unlink(); n += 1
    return {"status": "cleared", "vin": vin.upper(), "category": category, "deleted": n}


@router.get("/manage/{vin}", response_class=HTMLResponse)
async def manage_page(vin: str, request: Request):
    vin = vin.upper()
    _vehicle_dir(vin)  # 404 if missing
    return HTMLResponse(_MANAGE_HTML.replace("__VIN__", vin))


_MANAGE_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Manage Photos — __VIN__ — LotVue360</title>
<style>
*{margin:0;padding:0;box-sizing:border-box;}
body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:#F8FAFC;color:#2C3E50;padding:1.5rem;}
.top{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:1rem;margin-bottom:1.5rem;}
h1{font-size:1.3rem;color:#2E75B6;}
.vin{font-family:monospace;color:#7A8CA0;font-size:.85rem;}
.btn{background:#2E75B6;color:#fff;border:none;border-radius:8px;padding:.6rem 1rem;font-weight:600;cursor:pointer;text-decoration:none;display:inline-block;font-size:.9rem;}
.btn.secondary{background:#fff;color:#2E75B6;border:1.5px solid #2E75B6;}
.btn.danger{background:#C0392B;}
.btn:active{transform:scale(.98);}
.cat{background:#fff;border-radius:12px;box-shadow:0 2px 10px rgba(44,62,80,.07);padding:1.2rem;margin-bottom:1.2rem;}
.cat h2{font-size:1rem;text-transform:capitalize;display:flex;justify-content:space-between;align-items:center;margin-bottom:.8rem;}
.cat h2 .count{font-size:.8rem;color:#7A8CA0;font-weight:500;}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:.7rem;}
.cell{position:relative;border-radius:8px;overflow:hidden;border:1px solid #DDE5EE;background:#EEF3F8;aspect-ratio:4/3;}
.cell img{width:100%;height:100%;object-fit:cover;display:block;}
.cell .del{position:absolute;top:5px;right:5px;background:rgba(192,57,43,.92);color:#fff;border:none;border-radius:6px;width:30px;height:30px;font-size:1rem;cursor:pointer;line-height:1;}
.cell .fn{position:absolute;bottom:0;left:0;right:0;background:rgba(0,0,0,.55);color:#fff;font-size:.65rem;padding:2px 4px;font-family:monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.empty{color:#9AA8B6;font-size:.85rem;padding:.6rem 0;}
.bar{display:flex;gap:.5rem;flex-wrap:wrap;margin-top:.8rem;}
#toast{position:fixed;bottom:1.5rem;left:50%;transform:translateX(-50%);background:#2C3E50;color:#fff;padding:.7rem 1.2rem;border-radius:8px;opacity:0;transition:opacity .25s;pointer-events:none;z-index:50;}
#toast.show{opacity:1;}
.hint{color:#7A8CA0;font-size:.82rem;margin-top:.3rem;}
</style></head><body>
<div class="top">
  <div><h1>Manage Photos</h1><div class="vin">VIN __VIN__</div></div>
  <div class="bar">
    <a class="btn secondary" href="/view/__VIN__" target="_blank">Preview 360</a>
    <a class="btn secondary" href="/dashboard">← Dashboard</a>
    <button class="btn" onclick="refinalize()">Rebuild 360 spin</button>
  </div>
</div>
<div id="cats"></div>
<div id="toast"></div>
<script>
const VIN="__VIN__";
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2200);}
async function load(){
  const r=await fetch(`/api/vehicle/${VIN}/photos`);
  if(r.status===401){location.href='/login?next=/manage/'+VIN;return;}
  const d=await r.json();
  const order=['spin','exterior','gallery','interior','panoramas'];
  const labels={spin:'360 Spin (processed)',exterior:'Exterior (raw uploads)',gallery:'Gallery',interior:'Interior',panoramas:'Panoramas'};
  const root=document.getElementById('cats');root.innerHTML='';
  for(const cat of order){
    const files=d.photos[cat]||[];
    const sec=document.createElement('div');sec.className='cat';
    let cells=files.map(f=>`<div class="cell"><img src="${f.url}" loading="lazy"><button class="del" title="Delete" onclick="del('${cat}','${f.filename}')">&times;</button><div class="fn">${f.filename}</div></div>`).join('');
    if(!files.length) cells='<div class="empty">No photos in this category.</div>';
    sec.innerHTML=`<h2><span>${labels[cat]}</span><span class="count">${files.length} photo(s)</span></h2><div class="grid">${cells}</div>`+
      (files.length?`<div class="bar"><button class="btn danger" onclick="clearCat('${cat}','${labels[cat]}')">Delete all ${cat}</button></div>`:'');
    root.appendChild(sec);
  }
}
async function del(cat,fn){
  if(!confirm(`Delete ${fn}?`))return;
  const r=await fetch(`/api/vehicle/${VIN}/photo?category=${cat}&filename=${encodeURIComponent(fn)}`,{method:'DELETE'});
  if(r.ok){toast('Deleted '+fn);load();}else{toast('Delete failed');}
}
async function clearCat(cat,label){
  if(!confirm(`Delete ALL ${label} photos? This cannot be undone.`))return;
  const r=await fetch(`/api/vehicle/${VIN}/photos/clear?category=${cat}`,{method:'POST'});
  const d=await r.json();if(r.ok){toast(`Deleted ${d.deleted} ${cat} photos`);load();}else{toast('Failed');}
}
async function refinalize(){
  toast('Rebuilding 360 spin…');
  const r=await fetch(`/api/finalize/${VIN}`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  if(r.ok){toast('Rebuild started — refresh in ~30s');}else{toast('Rebuild failed (need exterior photos)');}
}
load();
</script>
</body></html>"""


def register_control(app):
    app.include_router(router)
    print("[F-Control] photo management registered: per-photo delete, clear category, "
          "re-finalize, /manage/{vin} page")
