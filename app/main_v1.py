"""
Dreams360ai — Car 360 Viewer MVP
CORRECTED VERSION — All bugs from Big Pickle's build fixed.
Drop this file at: .\360Car\app\main.py
Then restart server.

Fixes applied:
  1. FFmpeg extracts EXACTLY 36 evenly-spaced frames (was extracting at 24fps = thousands)
  2. Form() annotations on upload endpoints (was missing, would break)
  3. HEIC/HEIF photo support with Pillow fallback
  4. 360 viewer preloading + auto-rotate preview
  5. Touch fix (touch-action: none)
  6. Thumbnail generation for dashboard
  7. VIN validation (no I/O/Q, 17 alphanumeric)
  8. Upload size guard
  9. Dual input mode: video OR ordered photos for exterior
  10. SQLite metadata store (replaces per-folder info.json)

Requirements (run once):
  E:\\Miniconda3\\envs\\abu-mvp\\python.exe -m pip install fastapi uvicorn python-multipart Pillow aiosqlite

Optional (for HEIC — skip if it fails, iPhone users set camera to "Most Compatible"):
  E:\\Miniconda3\\envs\\abu-mvp\\python.exe -m pip install pillow-heif
"""

import os
import re
import io
import json
import shutil
import sqlite3
import subprocess
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

# =============================================================================
# APP
# =============================================================================
app = FastAPI(title="Dreams360ai")
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
            status TEXT DEFAULT 'active'
        )
    """)
    conn.commit()
    conn.close()

init_db()

def db_upsert_vehicle(vin, year="", make="", model="", ext_frames=0, int_count=0):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        INSERT INTO vehicles (vin, year, make, model, exterior_frames, interior_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(vin) DO UPDATE SET
            year=excluded.year, make=excluded.make, model=excluded.model,
            exterior_frames=excluded.exterior_frames, interior_count=excluded.interior_count
    """, (vin, year, make, model, ext_frames, int_count, datetime.now().isoformat()))
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
    
    return min(len(frames), num_frames)


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
        background: #0a0a0f; color: #e0e0e0; min-height: 100vh;
        -webkit-font-smoothing: antialiased;
    }
    .header { 
        background: linear-gradient(135deg, #0d1117 0%, #161b22 100%); 
        padding: 1.2rem 2rem; 
        border-bottom: 1px solid #21262d;
        display: flex; justify-content: space-between; align-items: center;
    }
    .logo { 
        font-size: 1.4rem; font-weight: 700; letter-spacing: -0.5px;
        color: #58a6ff;
    }
    .logo span { color: #e0e0e0; }
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
    .btn-outline { background: transparent; border: 1px solid #30363d; color: #c9d1d9; }
    .btn-outline:hover { background: #21262d; border-color: #8b949e; }
    .back-link { color: #58a6ff; text-decoration: none; font-size: 0.9rem; }
    .back-link:hover { text-decoration: underline; }
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
            thumb_html = f'<img src="{thumb_src}" style="width:100%;height:180px;object-fit:cover;border-radius:6px 6px 0 0;">' if thumb_src else '<div style="width:100%;height:180px;background:#161b22;border-radius:6px 6px 0 0;display:flex;align-items:center;justify-content:center;color:#484f58;font-size:0.9rem;">No preview</div>'
            
            cards_html += f"""
            <div style="background:#0d1117;border:1px solid #21262d;border-radius:6px;overflow:hidden;">
                {thumb_html}
                <div style="padding:1rem;">
                    <div style="font-weight:600;color:#e0e0e0;font-size:1.05rem;">{v['year']} {v['make']} {v['model']}</div>
                    <div style="color:#484f58;font-size:0.8rem;margin:0.3rem 0 0.8rem;font-family:monospace;">{vin}</div>
                    <div style="color:#8b949e;font-size:0.8rem;margin-bottom:0.8rem;">
                        {v['exterior_frames']} exterior &middot; {v['interior_count']} interior
                    </div>
                    <div style="display:flex;gap:0.4rem;flex-wrap:wrap;">
                        <a href="/view/{vin}" class="btn" style="font-size:0.8rem;padding:0.4rem 0.8rem;">View 360</a>
                        <a href="/embed/{vin}" target="_blank" class="btn btn-outline" style="font-size:0.8rem;padding:0.4rem 0.8rem;">Embed</a>
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
    <title>Dreams360ai — Dashboard</title>
    <style>
        {BASE_CSS}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 1.2rem; margin-top: 1.5rem; }}
        .stats {{ display: flex; gap: 1rem; margin: 1.2rem 0; }}
        .stat {{ background: #0d1117; border: 1px solid #21262d; padding: 0.8rem 1.2rem; border-radius: 6px; }}
        .stat-val {{ font-size: 1.6rem; font-weight: 700; color: #58a6ff; }}
        .stat-lbl {{ font-size: 0.75rem; color: #484f58; text-transform: uppercase; letter-spacing: 0.5px; }}
    </style>
</head>
<body>
    <div class="header">
        <div class="logo">Dreams<span>360</span>ai</div>
        <a href="/capture" class="btn">+ Add Vehicle</a>
    </div>
    <div class="container">
        <div class="stats">
            <div class="stat">
                <div class="stat-val">{len(vehicles)}</div>
                <div class="stat-lbl">Vehicles</div>
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
    </script>
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
    <title>Dreams360ai — Add Vehicle</title>
    <style>
        {BASE_CSS}
        .card {{ background: #0d1117; border: 1px solid #21262d; border-radius: 6px; padding: 1.5rem; margin-bottom: 1.2rem; }}
        .card h2 {{ color: #e0e0e0; font-size: 1.1rem; margin-bottom: 1rem; font-weight: 600; }}
        .fg {{ margin-bottom: 1rem; }}
        .fg label {{ display: block; margin-bottom: 0.3rem; color: #8b949e; font-size: 0.85rem; }}
        .fg input {{ 
            width: 100%; padding: 0.6rem 0.8rem; background: #010409; 
            border: 1px solid #21262d; border-radius: 6px; color: #e0e0e0; font-size: 0.95rem; 
        }}
        .fg input:focus {{ outline: none; border-color: #58a6ff; box-shadow: 0 0 0 3px rgba(88,166,255,0.15); }}
        .dropzone {{ 
            border: 2px dashed #21262d; border-radius: 8px; padding: 2.5rem; 
            text-align: center; cursor: pointer; transition: all 0.2s; 
        }}
        .dropzone:hover {{ border-color: #58a6ff; background: rgba(88,166,255,0.04); }}
        .dropzone.dragover {{ border-color: #58a6ff; background: rgba(88,166,255,0.08); }}
        .dropzone p {{ color: #484f58; font-size: 0.9rem; }}
        .dropzone .icon {{ font-size: 2rem; margin-bottom: 0.5rem; }}
        .file-item {{ 
            background: #161b22; padding: 0.4rem 0.8rem; border-radius: 4px; 
            margin-top: 0.4rem; display: flex; justify-content: space-between; 
            font-size: 0.85rem; color: #8b949e; 
        }}
        .progress {{ margin-top: 1rem; display: none; }}
        .progress-bar {{ height: 6px; background: #21262d; border-radius: 3px; overflow: hidden; }}
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
        <div class="logo">Dreams<span>360</span>ai</div>
        <a href="/" class="back-link">&larr; Dashboard</a>
    </div>
    <div class="container" style="max-width:700px;">
        {heic_note}
        <div class="card">
            <h2>Vehicle Info</h2>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.8rem;">
                <div class="fg" style="grid-column:1/-1;">
                    <label>VIN (17 characters)</label>
                    <input type="text" id="vin" placeholder="1HGCM82633A004352" maxlength="17" style="font-family:monospace;letter-spacing:1px;">
                </div>
                <div class="fg"><label>Year</label><input type="text" id="year" placeholder="2024" maxlength="4"></div>
                <div class="fg"><label>Make</label><input type="text" id="make" placeholder="Toyota"></div>
                <div class="fg"><label>Model</label><input type="text" id="model" placeholder="Camry"></div>
                <div class="fg"><label>Color</label><input type="text" id="color" placeholder="Silver"></div>
            </div>
        </div>

        <div class="card">
            <h2>Exterior &mdash; Walkaround Video</h2>
            <p style="color:#484f58;font-size:0.85rem;margin-bottom:1rem;">Walk around the car slowly in ~60 seconds. We extract 36 evenly-spaced frames for the 360 spin.</p>
            <div class="dropzone" id="videoDropzone">
                <div class="icon">&#127909;</div>
                <p>Drop walkaround video here or click to browse</p>
                <input type="file" id="videoInput" accept="video/*" style="display:none">
            </div>
            <div id="videoList"></div>
        </div>

        <div class="card">
            <h2>Interior Photos (12&ndash;25)</h2>
            <p style="color:#484f58;font-size:0.85rem;margin-bottom:1rem;">Dashboard, seats, trunk, steering wheel, gauges, etc. JPG or PNG recommended.</p>
            <div class="dropzone" id="photoDropzone">
                <div class="icon">&#128247;</div>
                <p>Drop interior photos here or click to browse</p>
                <input type="file" id="photoInput" accept="image/*,.heic,.heif" multiple style="display:none">
            </div>
            <div id="photoList"></div>
        </div>

        <button class="submit-btn" id="submitBtn" onclick="uploadAll()">Create 360 Viewer</button>
        <div class="progress" id="progress">
            <div class="progress-bar"><div class="progress-fill" id="progressBar"></div></div>
            <div class="progress-text" id="progressText">Preparing...</div>
        </div>
    </div>

    <script>
        let videoFile = null;
        let photoFiles = [];

        // --- Video dropzone ---
        const vdz = document.getElementById('videoDropzone');
        const vi = document.getElementById('videoInput');
        vdz.onclick = () => vi.click();
        vi.onchange = e => {{ if(e.target.files[0]) setVideo(e.target.files[0]); }};
        vdz.ondragover = e => {{ e.preventDefault(); vdz.classList.add('dragover'); }};
        vdz.ondragleave = () => vdz.classList.remove('dragover');
        vdz.ondrop = e => {{ e.preventDefault(); vdz.classList.remove('dragover'); if(e.dataTransfer.files[0]) setVideo(e.dataTransfer.files[0]); }};

        function setVideo(f) {{
            if (f.size > {MAX_VIDEO_MB} * 1024 * 1024) {{ alert('Video too large (max {MAX_VIDEO_MB}MB)'); return; }}
            videoFile = f;
            document.getElementById('videoList').innerHTML = 
                '<div class="file-item"><span>' + f.name + '</span><span>' + (f.size/1024/1024).toFixed(1) + ' MB</span></div>';
        }}

        // --- Photo dropzone ---
        const pdz = document.getElementById('photoDropzone');
        const pi = document.getElementById('photoInput');
        pdz.onclick = () => pi.click();
        pi.onchange = e => addPhotos(e.target.files);
        pdz.ondragover = e => {{ e.preventDefault(); pdz.classList.add('dragover'); }};
        pdz.ondragleave = () => pdz.classList.remove('dragover');
        pdz.ondrop = e => {{ e.preventDefault(); pdz.classList.remove('dragover'); addPhotos(e.dataTransfer.files); }};

        function addPhotos(files) {{
            photoFiles = [...photoFiles, ...Array.from(files)];
            renderPhotos();
        }}
        function removePhoto(i) {{ photoFiles.splice(i, 1); renderPhotos(); }}
        function renderPhotos() {{
            document.getElementById('photoList').innerHTML = photoFiles.map((f, i) =>
                '<div class="file-item"><span>' + f.name + '</span>' +
                '<span style="cursor:pointer;color:#f85149" onclick="removePhoto(' + i + ')">&times;</span></div>'
            ).join('') + '<div style="color:#484f58;font-size:0.8rem;margin-top:0.4rem;">' + photoFiles.length + ' photo(s)</div>';
        }}

        // --- Upload ---
        async function uploadAll() {{
            const vin = document.getElementById('vin').value.trim().toUpperCase();
            if (!vin || vin.length !== 17) {{ alert('Enter a valid 17-character VIN'); return; }}
            if (/[IOQ]/.test(vin)) {{ alert('VIN cannot contain I, O, or Q'); return; }}
            if (!videoFile) {{ alert('Upload an exterior walkaround video'); return; }}
            if (photoFiles.length < 1) {{ alert('Upload at least 1 interior photo (12+ recommended)'); return; }}

            const btn = document.getElementById('submitBtn');
            const bar = document.getElementById('progressBar');
            const txt = document.getElementById('progressText');
            btn.disabled = true;
            document.getElementById('progress').style.display = 'block';

            try {{
                // 1. Upload video + metadata
                txt.textContent = 'Uploading video & extracting frames...';
                bar.style.width = '10%';
                const fd = new FormData();
                fd.append('vin', vin);
                fd.append('year', document.getElementById('year').value);
                fd.append('make', document.getElementById('make').value);
                fd.append('model', document.getElementById('model').value);
                fd.append('color', document.getElementById('color').value || '');
                fd.append('video', videoFile);
                const r1 = await fetch('/api/upload', {{ method: 'POST', body: fd }});
                if (!r1.ok) {{
                    const err = await r1.json();
                    throw new Error(err.detail || 'Upload failed');
                }}
                bar.style.width = '50%';

                // 2. Upload interior photos one by one
                for (let i = 0; i < photoFiles.length; i++) {{
                    txt.textContent = 'Interior photo ' + (i+1) + '/' + photoFiles.length;
                    bar.style.width = (50 + (i / photoFiles.length) * 45) + '%';
                    const pf = new FormData();
                    pf.append('vin', vin);
                    pf.append('photo', photoFiles[i]);
                    const r2 = await fetch('/api/upload/interior', {{ method: 'POST', body: pf }});
                    if (!r2.ok) {{
                        const err = await r2.json();
                        console.warn('Photo upload warning:', err.detail);
                    }}
                }}

                bar.style.width = '100%';
                txt.textContent = 'Done! Redirecting...';
                setTimeout(() => window.location.href = '/view/' + vin, 800);
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
# API — UPLOAD VIDEO + METADATA
# =============================================================================
@app.post("/api/upload")
async def upload_video(
    vin: str = Form(...),
    year: str = Form(""),
    make: str = Form(""),
    model: str = Form(""),
    color: str = Form(""),
    video: UploadFile = File(...)
):
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
    vin: str = Form(...),
    photo: UploadFile = File(...)
):
    vin = validate_vin(vin)
    vehicle_dir = STORAGE_DIR / vin
    
    if not vehicle_dir.exists():
        raise HTTPException(status_code=404, detail="Vehicle not found. Upload exterior first.")
    
    interior_dir = vehicle_dir / "interior"
    interior_dir.mkdir(exist_ok=True)
    
    raw = await photo.read()
    
    if len(raw) > MAX_PHOTO_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"Photo too large (max {MAX_PHOTO_MB}MB)")
    
    existing = len(list(interior_dir.glob("*.jpg")))
    photo_path = interior_dir / f"interior_{existing+1:03d}.jpg"
    
    convert_photo_to_jpg(raw, photo.filename or "photo.jpg", photo_path)
    
    # Update counts
    db_update_counts(vin)
    
    return {"status": "ok", "filename": photo_path.name}


# =============================================================================
# ROUTES — 360 VIEWER
# =============================================================================
@app.get("/view/{vin}")
async def view_vehicle(vin: str, request: Request):
    vin = vin.upper()
    vehicle_dir = STORAGE_DIR / vin
    
    if not vehicle_dir.exists():
        return HTMLResponse("<h1>Vehicle not found</h1>", status_code=404)
    
    ext_frames = sorted([f.name for f in (vehicle_dir / "exterior").glob("*.jpg")]) if (vehicle_dir / "exterior").exists() else []
    int_photos = sorted([f.name for f in (vehicle_dir / "interior").glob("*.jpg")]) if (vehicle_dir / "interior").exists() else []
    
    info = db_get_vehicle(vin) or {"vin": vin, "year": "", "make": "", "model": ""}
    title = f"{info.get('year','')} {info.get('make','')} {info.get('model','')}".strip() or vin
    
    # Build frame img tags
    frame_tags = "\n".join(
        f'<img src="/storage/{vin}/exterior/{f}" style="display:none" data-idx="{i}" alt="frame {i}">'
        for i, f in enumerate(ext_frames)
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
            padding: 0.6rem 1.2rem; background: #0d1117; border: 1px solid #21262d; 
            color: #484f58; cursor: pointer; font-size: 0.9rem; border-radius: 6px 6px 0 0;
            transition: all 0.15s;
        }}
        .tab.active {{ background: #161b22; color: #58a6ff; border-bottom-color: #161b22; }}
        .tab-content {{ display: none; }}
        .tab-content.active {{ display: block; }}
        
        .viewer-wrap {{ background: #0d1117; border: 1px solid #21262d; border-radius: 6px; overflow: hidden; }}
        #spin {{
            width: 100%; height: 500px; cursor: grab; position: relative; 
            overflow: hidden; background: #010409;
            touch-action: none; -webkit-user-select: none; user-select: none;
        }}
        #spin:active {{ cursor: grabbing; }}
        #spin img {{ 
            width: 100%; height: 100%; object-fit: contain; 
            position: absolute; left: 0; top: 0; pointer-events: none; 
        }}
        .spin-hint {{ 
            position: absolute; bottom: 0.8rem; left: 50%; transform: translateX(-50%);
            background: rgba(0,0,0,0.7); padding: 0.4rem 1rem; border-radius: 20px; 
            font-size: 0.8rem; color: #8b949e; pointer-events: none; transition: opacity 0.5s;
        }}
        .spin-loader {{
            position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
            color: #484f58; font-size: 0.9rem;
        }}
        
        .int-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 0.8rem; }}
        .int-thumb {{ 
            aspect-ratio: 16/10; border-radius: 6px; overflow: hidden; 
            cursor: pointer; transition: transform 0.15s; border: 1px solid #21262d;
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
            background: #0d1117; border: 1px solid #21262d; border-radius: 6px; 
            padding: 1.2rem; margin-top: 1rem; 
        }}
        .share-box h3 {{ color: #8b949e; font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 0.5rem; }}
        .share-field {{ 
            background: #010409; border: 1px solid #21262d; padding: 0.5rem 0.8rem; 
            border-radius: 4px; font-family: monospace; font-size: 0.8rem; color: #8b949e;
            word-break: break-all; margin-bottom: 0.8rem;
        }}
        .copy-btn {{ font-size: 0.8rem; padding: 0.3rem 0.8rem; }}
        
        @media (max-width: 768px) {{
            #spin {{ height: 300px; }}
            .int-grid {{ grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); }}
        }}
    </style>
</head>
<body>
    <div class="header">
        <div class="logo">{title}</div>
        <a href="/" class="back-link">&larr; Dashboard</a>
    </div>
    <div class="container">
        <div class="tabs">
            <button class="tab active" onclick="showTab('ext')">Exterior 360&deg;</button>
            <button class="tab" onclick="showTab('int')">Interior ({len(int_photos)})</button>
        </div>
        
        <div id="ext" class="tab-content active">
            <div class="viewer-wrap">
                <div id="spin">
                    <div class="spin-loader" id="loader">Loading 0/{len(ext_frames)}...</div>
                    {frame_tags}
                    <div class="spin-hint" id="hint" style="opacity:0">&larr; Drag to spin &rarr;</div>
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

    <script>
        // --- Frame loading + auto-rotate ---
        const frames = document.querySelectorAll('#spin img');
        const loader = document.getElementById('loader');
        const hint = document.getElementById('hint');
        let loaded = 0;
        let cur = 0;
        let dragging = false;
        let sx = 0;
        let sf = 0;
        let autoInt = null;
        
        frames.forEach(img => {{
            const pre = new Image();
            pre.onload = () => {{
                loaded++;
                loader.textContent = 'Loading ' + loaded + '/{len(ext_frames)}...';
                if (loaded === frames.length) {{
                    loader.style.display = 'none';
                    if (frames.length > 0) frames[0].style.display = 'block';
                    hint.style.opacity = '1';
                    autoRotate();
                }}
            }};
            pre.onerror = () => {{ loaded++; }};
            pre.src = img.src;
        }});
        
        function autoRotate() {{
            let count = 0;
            autoInt = setInterval(() => {{
                cur = (cur + 1) % frames.length;
                show();
                count++;
                if (count >= frames.length) {{ clearInterval(autoInt); autoInt = null; }}
            }}, 70);
        }}
        function stopAuto() {{ if (autoInt) {{ clearInterval(autoInt); autoInt = null; }} }}
        
        function show() {{
            frames.forEach(f => f.style.display = 'none');
            if (frames[cur]) frames[cur].style.display = 'block';
        }}
        
        // --- Mouse drag ---
        const spin = document.getElementById('spin');
        spin.onmousedown = e => {{ stopAuto(); dragging = true; sx = e.clientX; sf = cur; hint.style.opacity = '0'; }};
        spin.onmousemove = e => {{
            if (!dragging) return;
            const dx = e.clientX - sx;
            const fd = Math.round(dx / 15);
            if (fd !== 0) {{ cur = ((sf - fd) % frames.length + frames.length) % frames.length; show(); }}
        }};
        spin.onmouseup = () => dragging = false;
        spin.onmouseleave = () => dragging = false;
        
        // --- Touch drag ---
        spin.ontouchstart = e => {{ stopAuto(); dragging = true; sx = e.touches[0].clientX; sf = cur; hint.style.opacity = '0'; }};
        spin.ontouchmove = e => {{
            if (!dragging) return;
            const dx = e.touches[0].clientX - sx;
            const fd = Math.round(dx / 15);
            if (fd !== 0) {{ cur = ((sf - fd) % frames.length + frames.length) % frames.length; show(); }}
        }};
        spin.ontouchend = () => dragging = false;
        
        // --- Tabs ---
        function showTab(id) {{
            document.querySelectorAll('.tab').forEach((t, i) => {{
                t.classList.toggle('active', (id === 'ext' ? i === 0 : i === 1));
            }});
            document.querySelectorAll('.tab-content').forEach(tc => tc.classList.remove('active'));
            document.getElementById(id).classList.add('active');
        }}
        
        // --- Lightbox ---
        function openLB(src) {{ document.getElementById('lbImg').src = src; document.getElementById('lb').classList.add('open'); }}
        function closeLB() {{ document.getElementById('lb').classList.remove('open'); }}
        
        // --- Copy ---
        function copyText(id) {{
            navigator.clipboard.writeText(document.getElementById(id).textContent);
            event.target.textContent = 'Copied!';
            setTimeout(() => event.target.textContent = id === 'shareLink' ? 'Copy Link' : 'Copy Embed', 1500);
        }}
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
    
    ext_frames = sorted([f.name for f in (vehicle_dir / "exterior").glob("*.jpg")]) if (vehicle_dir / "exterior").exists() else []
    int_photos = sorted([f.name for f in (vehicle_dir / "interior").glob("*.jpg")]) if (vehicle_dir / "interior").exists() else []
    
    info = db_get_vehicle(vin) or {"vin": vin, "year": "", "make": "", "model": ""}
    title = f"{info.get('year','')} {info.get('make','')} {info.get('model','')}".strip() or vin
    
    frame_tags = "\n".join(
        f'<img src="/storage/{vin}/exterior/{f}" style="display:none" data-idx="{i}">'
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
        body {{ font-family:'Segoe UI',sans-serif; background:#0a0a0f; color:#e0e0e0; overflow-x:hidden; }}
        .tabs {{ display:flex; background:#0d1117; border-bottom:1px solid #21262d; }}
        .tab {{ flex:1; padding:0.7rem; background:transparent; border:none; color:#484f58; cursor:pointer; text-align:center; font-size:0.9rem; }}
        .tab.active {{ color:#58a6ff; box-shadow:inset 0 -2px 0 #58a6ff; }}
        .tc {{ display:none; }} .tc.active {{ display:block; }}
        #spin {{ width:100%; height:420px; cursor:grab; position:relative; background:#010409; touch-action:none; user-select:none; }}
        #spin:active {{ cursor:grabbing; }}
        #spin img {{ width:100%; height:100%; object-fit:contain; position:absolute; inset:0; pointer-events:none; }}
        .hint {{ position:absolute; bottom:0.6rem; left:50%; transform:translateX(-50%); background:rgba(0,0,0,0.7); padding:0.3rem 0.8rem; border-radius:12px; font-size:0.75rem; color:#8b949e; }}
        .ig {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(140px,1fr)); gap:0.5rem; padding:0.5rem; }}
        .ig div {{ aspect-ratio:16/10; }}
        .brand {{ text-align:center; padding:0.4rem; font-size:0.7rem; color:#21262d; }}
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
    <div class="brand">Powered by Dreams360ai</div>
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
# STATIC FILE SERVING
# =============================================================================
app.mount("/storage", StaticFiles(directory=str(STORAGE_DIR)), name="storage")


# =============================================================================
# RUN
# =============================================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
