import os
import shutil
import subprocess
import json
import io
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional
from PIL import Image
import pillow_heif

pillow_heif.register_heif_opener()

app = FastAPI(title="Dreams360ai - Car Viewer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STORAGE_DIR = Path("E:/ABU/360Car/app/storage")
STATIC_DIR = Path("E:/ABU/360Car/app/static")
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

def get_vehicle_dir(vin: str) -> Path:
    return STORAGE_DIR / vin

def extract_frames(video_path: Path, output_dir: Path, num_frames: int = 24):
    output_dir.mkdir(parents=True, exist_ok=True)
    output_pattern = str(output_dir / "frame_%04d.jpg")
    
    cmd = [
        "ffmpeg", "-i", str(video_path),
        "-vf", f"fps={num_frames}",
        "-q:v", "2",
        "-y", output_pattern
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        raise Exception(f"FFmpeg failed: {result.stderr}")
    
    frames = sorted(output_dir.glob("frame_*.jpg"))
    return len(frames)

@app.get("/")
async def dashboard():
    vehicles = []
    if STORAGE_DIR.exists():
        for vin_dir in STORAGE_DIR.iterdir():
            if vin_dir.is_dir():
                info_file = vin_dir / "info.json"
                if info_file.exists():
                    with open(info_file) as f:
                        info = json.load(f)
                else:
                    info = {"vin": vin_dir.name, "year": "", "make": "", "model": ""}
                
                exterior_count = len(list((vin_dir / "exterior").glob("*.jpg"))) if (vin_dir / "exterior").exists() else 0
                interior_count = len(list((vin_dir / "interior").glob("*.jpg"))) if (vin_dir / "interior").exists() else 0
                
                vehicles.append({
                    "vin": vin_dir.name,
                    "year": info.get("year", ""),
                    "make": info.get("make", ""),
                    "model": info.get("model", ""),
                    "exterior_frames": exterior_count,
                    "interior_count": interior_count
                })
    
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Dreams360ai - Dashboard</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { font-family: 'Segoe UI', sans-serif; background: #0a0a0f; color: #fff; min-height: 100vh; }
            .header { background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); padding: 2rem; border-bottom: 1px solid #333; }
            .header h1 { font-size: 2rem; background: linear-gradient(90deg, #00d4ff, #7c3aed); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
            .container { max-width: 1200px; margin: 0 auto; padding: 2rem; }
            .btn { background: linear-gradient(135deg, #00d4ff, #7c3aed); border: none; padding: 0.8rem 1.5rem; color: white; border-radius: 8px; cursor: pointer; font-weight: 600; text-decoration: none; display: inline-block; }
            .btn:hover { transform: translateY(-2px); box-shadow: 0 4px 20px rgba(0,212,255,0.3); }
            .vehicle-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 1.5rem; margin-top: 2rem; }
            .vehicle-card { background: #1a1a2e; border-radius: 12px; padding: 1.5rem; border: 1px solid #333; }
            .vehicle-card h3 { color: #00d4ff; margin-bottom: 0.5rem; }
            .vehicle-card .meta { color: #888; font-size: 0.9rem; margin-bottom: 1rem; }
            .vehicle-card .actions { display: flex; gap: 0.5rem; margin-top: 1rem; }
            .vehicle-card .btn-sm { padding: 0.5rem 1rem; font-size: 0.85rem; }
            .empty { text-align: center; padding: 4rem; color: #666; }
            .stats { display: flex; gap: 2rem; margin-top: 1rem; }
            .stat { background: #16213e; padding: 1rem; border-radius: 8px; text-align: center; }
            .stat-value { font-size: 2rem; font-weight: bold; color: #00d4ff; }
            .stat-label { color: #888; font-size: 0.85rem; }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>🚗 Dreams360ai</h1>
            <p>Car 360 Viewer - Dashboard</p>
        </div>
        <div class="container">
            <a href="/capture" class="btn">+ New Vehicle</a>
            
            <div class="stats">
                <div class="stat">
                    <div class="stat-value">""" + str(len(vehicles)) + """</div>
                    <div class="stat-label">Vehicles</div>
                </div>
            </div>
            
            <div class="vehicle-grid">
    """
    
    if not vehicles:
        html += '<div class="empty">No vehicles yet. Click "+ New Vehicle" to get started.</div>'
    else:
        for v in vehicles:
            html += f"""
                <div class="vehicle-card">
                    <h3>{v['year']} {v['make']} {v['model']}</h3>
                    <div class="meta">VIN: {v['vin']}</div>
                    <div class="meta">{v['exterior_frames']} exterior frames • {v['interior_count']} interior photos</div>
                    <div class="actions">
                        <a href="/view/{v['vin']}" class="btn btn-sm">View 360</a>
                        <a href="/embed/{v['vin']}" target="_blank" class="btn btn-sm">Embed</a>
                        <button onclick="deleteVehicle('{v['vin']}')" class="btn btn-sm" style="background: #dc2626;">Delete</button>
                    </div>
                </div>
            """
    
    html += """
            </div>
        </div>
        <script>
            async function deleteVehicle(vin) {
                if(!confirm('Delete this vehicle?')) return;
                const res = await fetch('/api/vehicle/' + vin, { method: 'DELETE' });
                if(res.ok) location.reload();
            }
        </script>
    </body>
    </html>
    """
    return HTMLResponse(html)

@app.get("/capture")
async def capture_page():
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Dreams360ai - Capture</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { font-family: 'Segoe UI', sans-serif; background: #0a0a0f; color: #fff; min-height: 100vh; }
            .header { background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); padding: 1.5rem 2rem; border-bottom: 1px solid #333; display: flex; justify-content: space-between; align-items: center; }
            .header h1 { font-size: 1.5rem; background: linear-gradient(90deg, #00d4ff, #7c3aed); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
            .container { max-width: 800px; margin: 0 auto; padding: 2rem; }
            .card { background: #1a1a2e; border-radius: 12px; padding: 2rem; margin-bottom: 1.5rem; border: 1px solid #333; }
            .card h2 { color: #00d4ff; margin-bottom: 1rem; font-size: 1.3rem; }
            .form-group { margin-bottom: 1.5rem; }
            .form-group label { display: block; margin-bottom: 0.5rem; color: #ccc; }
            .form-group input { width: 100%; padding: 0.8rem; background: #0a0a0f; border: 1px solid #333; border-radius: 8px; color: #fff; font-size: 1rem; }
            .form-group input:focus { outline: none; border-color: #00d4ff; }
            .dropzone { border: 2px dashed #333; border-radius: 12px; padding: 3rem; text-align: center; cursor: pointer; transition: all 0.3s; }
            .dropzone:hover { border-color: #00d4ff; background: rgba(0,212,255,0.05); }
            .dropzone.dragover { border-color: #00d4ff; background: rgba(0,212,255,0.1); }
            .dropzone p { color: #888; }
            .dropzone .icon { font-size: 3rem; margin-bottom: 1rem; }
            .btn { background: linear-gradient(135deg, #00d4ff, #7c3aed); border: none; padding: 1rem 2rem; color: white; border-radius: 8px; cursor: pointer; font-weight: 600; font-size: 1rem; width: 100%; }
            .btn:hover { transform: translateY(-2px); box-shadow: 0 4px 20px rgba(0,212,255,0.3); }
            .btn:disabled { opacity: 0.5; cursor: not-allowed; }
            .progress { margin-top: 1rem; display: none; }
            .progress-bar { height: 8px; background: #333; border-radius: 4px; overflow: hidden; }
            .progress-bar-fill { height: 100%; background: linear-gradient(90deg, #00d4ff, #7c3aed); width: 0%; transition: width 0.3s; }
            .progress-text { margin-top: 0.5rem; color: #888; font-size: 0.9rem; }
            .file-list { margin-top: 1rem; }
            .file-item { background: #16213e; padding: 0.5rem 1rem; border-radius: 6px; margin-bottom: 0.5rem; display: flex; justify-content: space-between; }
            .back-link { color: #00d4ff; text-decoration: none; }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>🚗 Dreams360ai</h1>
            <a href="/" class="back-link">← Dashboard</a>
        </div>
        <div class="container">
            <div class="card">
                <h2>📋 Vehicle Info</h2>
                <div class="form-group">
                    <label>VIN (17 characters)</label>
                    <input type="text" id="vin" placeholder="Enter VIN" maxlength="17">
                </div>
                <div class="form-group">
                    <label>Year</label>
                    <input type="text" id="year" placeholder="2024">
                </div>
                <div class="form-group">
                    <label>Make</label>
                    <input type="text" id="make" placeholder="Toyota">
                </div>
                <div class="form-group">
                    <label>Model</label>
                    <input type="text" id="model" placeholder="Camry">
                </div>
            </div>
            
            <div class="card">
                <h2>🎥 Exterior Video (360)</h2>
                <div class="dropzone" id="videoDropzone">
                    <div class="icon">📹</div>
                    <p>Drag & drop walkaround video here<br>or click to browse</p>
                    <input type="file" id="videoInput" accept="video/*" style="display:none">
                </div>
                <div class="file-list" id="videoList"></div>
            </div>
            
            <div class="card">
                <h2>📷 Interior Photos</h2>
                <div class="dropzone" id="photoDropzone">
                    <div class="icon">📷</div>
                    <p>Drag & drop 12-20 interior photos here<br>or click to browse</p>
                    <input type="file" id="photoInput" accept="image/*" multiple style="display:none">
                </div>
                <div class="file-list" id="photoList"></div>
            </div>
            
            <button class="btn" id="submitBtn" onclick="uploadAll()">Create 360 Viewer</button>
            
            <div class="progress" id="progress">
                <div class="progress-bar"><div class="progress-bar-fill" id="progressBar"></div></div>
                <div class="progress-text" id="progressText">Uploading...</div>
            </div>
        </div>
        
        <script>
            let videoFile = null;
            let photoFiles = [];
            
            // Video handling
            const videoDropzone = document.getElementById('videoDropzone');
            const videoInput = document.getElementById('videoInput');
            
            videoDropzone.onclick = () => videoInput.click();
            videoInput.onchange = (e) => { if(e.target.files[0]) handleVideo(e.target.files[0]); };
            
            videoDropzone.ondragover = (e) => { e.preventDefault(); videoDropzone.classList.add('dragover'); };
            videoDropzone.ondragleave = () => videoDropzone.classList.remove('dragover');
            videoDropzone.ondrop = (e) => {
                e.preventDefault();
                videoDropzone.classList.remove('dragover');
                if(e.dataTransfer.files[0]) handleVideo(e.dataTransfer.files[0]);
            };
            
            function handleVideo(file) {
                videoFile = file;
                document.getElementById('videoList').innerHTML = '<div class="file-item"><span>' + file.name + '</span><span>' + (file.size/1024/1024).toFixed(1) + ' MB</span></div>';
            }
            
            // Photo handling
            const photoDropzone = document.getElementById('photoDropzone');
            const photoInput = document.getElementById('photoInput');
            
            photoDropzone.onclick = () => photoInput.click();
            photoInput.onchange = (e) => { handlePhotos(e.target.files); };
            
            photoDropzone.ondragover = (e) => { e.preventDefault(); photoDropzone.classList.add('dragover'); };
            photoDropzone.ondragleave = () => photoDropzone.classList.remove('dragover');
            photoDropzone.ondrop = (e) => {
                e.preventDefault();
                photoDropzone.classList.remove('dragover');
                handlePhotos(e.dataTransfer.files);
            };
            
            function handlePhotos(files) {
                photoFiles = [...photoFiles, ...Array.from(files)];
                renderPhotoList();
            }
            
            function renderPhotoList() {
                document.getElementById('photoList').innerHTML = photoFiles.map((f, i) => 
                    '<div class="file-item"><span>' + f.name + '</span><span style="cursor:pointer;color:#ff6b6b" onclick="removePhoto(' + i + ')">✕</span></div>'
                ).join('') + '<div style="color:#666;margin-top:0.5rem">' + photoFiles.length + ' photos selected</div>';
            }
            
            function removePhoto(i) {
                photoFiles.splice(i, 1);
                renderPhotoList();
            }
            
            async function uploadAll() {
                const vin = document.getElementById('vin').value.trim();
                if(!vin || vin.length < 17) { alert('Please enter a valid VIN'); return; }
                if(!videoFile) { alert('Please upload an exterior video'); return; }
                if(photoFiles.length < 12) { alert('Please upload at least 12 interior photos'); return; }
                
                const btn = document.getElementById('submitBtn');
                const progress = document.getElementById('progress');
                const progressBar = document.getElementById('progressBar');
                const progressText = document.getElementById('progressText');
                
                btn.disabled = true;
                progress.style.display = 'block';
                
                try {
                    // Create vehicle folder
                    progressText.textContent = 'Creating vehicle...';
                    
                    const formData = new FormData();
                    formData.append('vin', vin);
                    formData.append('year', document.getElementById('year').value);
                    formData.append('make', document.getElementById('make').value);
                    formData.append('model', document.getElementById('model').value);
                    formData.append('video', videoFile);
                    
                    progressBar.style.width = '20%';
                    
                    const res = await fetch('/api/upload', {
                        method: 'POST',
                        body: formData
                    });
                    
                    if(!res.ok) throw new Error('Upload failed');
                    
                    progressText.textContent = 'Extracting frames from video...';
                    progressBar.style.width = '40%';
                    
                    // Upload interior photos
                    for(let i = 0; i < photoFiles.length; i++) {
                        progressText.textContent = 'Uploading interior photo ' + (i+1) + '/' + photoFiles.length;
                        progressBar.style.width = (40 + (i / photoFiles.length) * 50) + '%';
                        
                        const photoForm = new FormData();
                        photoForm.append('vin', vin);
                        photoForm.append('photo', photoFiles[i]);
                        await fetch('/api/upload/interior', {
                            method: 'POST',
                            body: photoForm
                        });
                    }
                    
                    progressBar.style.width = '100%';
                    progressText.textContent = 'Complete!';
                    
                    setTimeout(() => window.location.href = '/view/' + vin, 1000);
                    
                } catch(e) {
                    alert('Error: ' + e.message);
                    btn.disabled = false;
                }
            }
        </script>
    </body>
    </html>
    """
    return HTMLResponse(html)

@app.post("/api/upload")
async def upload_video(vin: str, year: str, make: str, model: str, video: UploadFile = File(...)):
    vin = vin.upper().strip()
    if len(vin) != 17:
        raise HTTPException(status_code=400, detail="VIN must be 17 characters")
    
    vehicle_dir = get_vehicle_dir(vin)
    vehicle_dir.mkdir(parents=True, exist_ok=True)
    exterior_dir = vehicle_dir / "exterior"
    interior_dir = vehicle_dir / "interior"
    exterior_dir.mkdir(exist_ok=True)
    interior_dir.mkdir(exist_ok=True)
    
    info = {"vin": vin, "year": year, "make": make, "model": model}
    with open(vehicle_dir / "info.json", "w") as f:
        json.dump(info, f)
    
    video_path = vehicle_dir / "video_temp.mp4"
    with open(video_path, "wb") as f:
        shutil.copyfileobj(video.file, f)
    
    try:
        num_extracted = extract_frames(video_path, exterior_dir, 24)
        os.remove(video_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Frame extraction failed: {str(e)}")
    
    return {"status": "ok", "vin": vin, "frames_extracted": num_extracted}

@app.post("/api/upload/interior")
async def upload_interior(vin: str, photo: UploadFile = File(...)):
    vin = vin.upper().strip()
    vehicle_dir = get_vehicle_dir(vin)
    
    if not vehicle_dir.exists():
        raise HTTPException(status_code=404, detail="Vehicle not found")
    
    interior_dir = vehicle_dir / "interior"
    interior_dir.mkdir(exist_ok=True)
    
    existing = len(list(interior_dir.glob("*.jpg")))
    photo_path = interior_dir / f"interior_{existing+1:02d}.jpg"
    
    raw = await photo.read()
    ext = Path(photo.filename).suffix.lower()
    if ext in [".heic", ".heif"]:
        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")
        img.save(photo_path, "JPEG", quality=90)
    else:
        with open(photo_path, "wb") as f:
            f.write(raw)
    
    return {"status": "ok", "filename": photo_path.name}

@app.get("/view/{vin}")
async def view_vehicle(vin: str):
    vin = vin.upper()
    vehicle_dir = get_vehicle_dir(vin)
    
    if not vehicle_dir.exists():
        return HTMLResponse("<h1>Vehicle not found</h1>", status_code=404)
    
    exterior_frames = sorted([f.name for f in (vehicle_dir / "exterior").glob("*.jpg")]) if (vehicle_dir / "exterior").exists() else []
    interior_photos = sorted([f.name for f in (vehicle_dir / "interior").glob("*.jpg")]) if (vehicle_dir / "interior").exists() else []
    
    info_file = vehicle_dir / "info.json"
    if info_file.exists():
        with open(info_file) as f:
            info = json.load(f)
    else:
        info = {"vin": vin, "year": "", "make": "", "model": ""}
    
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>""" + f"{info.get('year','')} {info.get('make','')} {info.get('model','')} - 360 View" + """</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { font-family: 'Segoe UI', sans-serif; background: #0a0a0f; color: #fff; min-height: 100vh; }
            .header { background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); padding: 1rem 2rem; border-bottom: 1px solid #333; display: flex; justify-content: space-between; align-items: center; }
            .header h1 { font-size: 1.3rem; background: linear-gradient(90deg, #00d4ff, #7c3aed); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
            .container { max-width: 1200px; margin: 0 auto; padding: 1rem; }
            .tabs { display: flex; gap: 0.5rem; margin-bottom: 1rem; }
            .tab { padding: 0.8rem 1.5rem; background: #1a1a2e; border: none; color: #888; border-radius: 8px 8px 0 0; cursor: pointer; }
            .tab.active { background: #1a1a2e; color: #00d4ff; border-bottom: 2px solid #00d4ff; }
            .tab-content { display: none; }
            .tab-content.active { display: block; }
            .viewer-container { background: #1a1a2e; border-radius: 12px; padding: 1rem; position: relative; overflow: hidden; }
            #exteriorViewer { width: 100%; height: 500px; cursor: grab; position: relative; overflow: hidden; border-radius: 8px; background: #000; }
            #exteriorViewer:active { cursor: grabbing; }
            #exteriorViewer img { width: 100%; height: 100%; object-fit: contain; position: absolute; left: 0; top: 0; pointer-events: none; }
            .viewer-hint { position: absolute; bottom: 1rem; left: 50%; transform: translateX(-50%); background: rgba(0,0,0,0.7); padding: 0.5rem 1rem; border-radius: 20px; font-size: 0.85rem; }
            .interior-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 1rem; }
            .interior-photo { aspect-ratio: 16/9; border-radius: 8px; overflow: hidden; cursor: pointer; transition: transform 0.2s; }
            .interior-photo:hover { transform: scale(1.02); }
            .interior-photo img { width: 100%; height: 100%; object-fit: cover; }
            .embed-section { background: #1a1a2e; border-radius: 12px; padding: 1.5rem; margin-top: 1rem; }
            .embed-section h3 { color: #00d4ff; margin-bottom: 1rem; }
            .embed-code { background: #0a0a0f; padding: 1rem; border-radius: 8px; font-family: monospace; font-size: 0.85rem; word-break: break-all; }
            .copy-btn { background: #00d4ff; border: none; padding: 0.5rem 1rem; border-radius: 6px; cursor: pointer; margin-left: 1rem; }
            .lightbox { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.95); z-index: 1000; justify-content: center; align-items: center; }
            .lightbox.active { display: flex; }
            .lightbox img { max-width: 90%; max-height: 90%; object-fit: contain; }
            .lightbox-close { position: absolute; top: 1rem; right: 1rem; font-size: 2rem; cursor: pointer; }
            .share-link { background: #16213e; padding: 0.8rem 1rem; border-radius: 8px; margin-bottom: 1rem; word-break: break-all; }
            .back-link { color: #00d4ff; text-decoration: none; }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>🚗 """ + f"{info.get('year','')} {info.get('make','')} {info.get('model','')}" + """</h1>
            <a href="/" class="back-link">← Dashboard</a>
        </div>
        <div class="container">
            <div class="tabs">
                <button class="tab active" onclick="showTab('exterior')">Exterior 360°</button>
                <button class="tab" onclick="showTab('interior')">Interior Gallery</button>
            </div>
            
            <div id="exterior" class="tab-content active">
                <div class="viewer-container">
                    <div id="exteriorViewer">
    """
    
    for i, frame in enumerate(exterior_frames):
        html += f'<img src="/storage/{vin}/exterior/{frame}" style="display:none" data-index="{i}">'
    
    html += """
                        <div class="viewer-hint">← Drag to spin →</div>
                    </div>
                </div>
            </div>
            
            <div id="interior" class="tab-content">
                <div class="interior-grid">
    """
    
    for photo in interior_photos:
        html += f'<div class="interior-photo" onclick="openLightbox(\'/storage/{vin}/interior/{photo}\')"><img src="/storage/{vin}/interior/{photo}" loading="lazy"></div>'
    
    html += """
                </div>
            </div>
            
            <div class="embed-section">
                <h3>🔗 Share Link</h3>
                <div class="share-link" id="shareLink"></div>
                <h3>� Embed Code</h3>
                <div class="embed-code" id="embedCode"></div>
                <button class="copy-btn" onclick="copyEmbed()">Copy</button>
            </div>
        </div>
        
        <div class="lightbox" id="lightbox" onclick="closeLightbox()">
            <span class="lightbox-close">×</span>
            <img id="lightboxImg" src="">
        </div>
        
        <script>
            const frames = document.querySelectorAll('#exteriorViewer img');
            let currentFrame = 0;
            let isDragging = false;
            let startX = 0;
            let startFrame = 0;
            
            if(frames.length > 0) {
                frames[0].style.display = 'block';
            }
            
            const viewer = document.getElementById('exteriorViewer');
            
            viewer.onmousedown = (e) => { isDragging = true; startX = e.clientX; startFrame = currentFrame; };
            viewer.onmousemove = (e) => {
                if(!isDragging) return;
                const dx = e.clientX - startX;
                const frameDelta = Math.floor(Math.abs(dx) / 20);
                if(frameDelta > 0) {
                    if(dx > 0) currentFrame = (startFrame - frameDelta + frames.length) % frames.length;
                    else currentFrame = (startFrame + frameDelta) % frames.length;
                    updateFrame();
                }
            };
            viewer.onmouseup = () => isDragging = false;
            viewer.onmouseleave = () => isDragging = false;
            
            // Touch support
            viewer.ontouchstart = (e) => { isDragging = true; startX = e.touches[0].clientX; startFrame = currentFrame; };
            viewer.ontouchmove = (e) => {
                if(!isDragging) return;
                const dx = e.touches[0].clientX - startX;
                const frameDelta = Math.floor(Math.abs(dx) / 30);
                if(frameDelta > 0) {
                    if(dx > 0) currentFrame = (startFrame - frameDelta + frames.length) % frames.length;
                    else currentFrame = (startFrame + frameDelta) % frames.length;
                    updateFrame();
                }
            };
            viewer.ontouchend = () => isDragging = false;
            
            function updateFrame() {
                frames.forEach(f => f.style.display = 'none');
                frames[currentFrame].style.display = 'block';
            }
            
            function showTab(tab) {
                document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
                document.querySelector('.tab:nth-child(' + (tab === 'exterior' ? 1 : 2) + ')').classList.add('active');
                document.getElementById(tab).classList.add('active');
            }
            
            function openLightbox(src) {
                document.getElementById('lightboxImg').src = src;
                document.getElementById('lightbox').classList.add('active');
            }
            function closeLightbox() {
                document.getElementById('lightbox').classList.remove('active');
            }
            
            const vin = '""" + vin + """';
            const baseUrl = window.location.origin;
            document.getElementById('shareLink').textContent = baseUrl + '/view/' + vin;
            document.getElementById('embedCode').textContent = '<iframe src="' + baseUrl + '/embed/' + vin + '" width="100%" height="600" frameborder="0"></iframe>';
            
            function copyEmbed() {
                navigator.clipboard.writeText(document.getElementById('embedCode').textContent);
                alert('Copied!');
            }
        </script>
    </body>
    </html>
    """
    return HTMLResponse(html)

@app.get("/embed/{vin}")
async def embed_vehicle(vin: str):
    vin = vin.upper()
    vehicle_dir = get_vehicle_dir(vin)
    
    if not vehicle_dir.exists():
        return HTMLResponse("<h1>Vehicle not found</h1>", status_code=404)
    
    exterior_frames = sorted([f.name for f in (vehicle_dir / "exterior").glob("*.jpg")]) if (vehicle_dir / "exterior").exists() else []
    interior_photos = sorted([f.name for f in (vehicle_dir / "interior").glob("*.jpg")]) if (vehicle_dir / "interior").exists() else []
    
    info_file = vehicle_dir / "info.json"
    if info_file.exists():
        with open(info_file) as f:
            info = json.load(f)
    else:
        info = {"vin": vin, "year": "", "make": "", "model": ""}
    
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>""" + f"{info.get('year','')} {info.get('make','')} {info.get('model','')}" + """</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { font-family: 'Segoe UI', sans-serif; background: #0a0a0f; color: #fff; }
            .tabs { display: flex; background: #1a1a2e; }
            .tab { flex: 1; padding: 0.8rem; background: #1a1a2e; border: none; color: #888; cursor: pointer; text-align: center; }
            .tab.active { background: #16213e; color: #00d4ff; }
            .tab-content { display: none; }
            .tab-content.active { display: block; }
            #exteriorViewer { width: 100%; height: 400px; cursor: grab; position: relative; background: #000; }
            #exteriorViewer:active { cursor: grabbing; }
            #exteriorViewer img { width: 100%; height: 100%; object-fit: contain; position: absolute; left: 0; top: 0; pointer-events: none; }
            .interior-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 0.5rem; padding: 0.5rem; }
            .interior-photo { aspect-ratio: 16/9; border-radius: 4px; overflow: hidden; }
            .interior-photo img { width: 100%; height: 100%; object-fit: cover; }
        </style>
    </head>
    <body>
        <div class="tabs">
            <button class="tab active" onclick="showTab('exterior')">Exterior</button>
            <button class="tab" onclick="showTab('interior')">Interior</button>
        </div>
        
        <div id="exterior" class="tab-content active">
            <div id="exteriorViewer">
    """
    
    for i, frame in enumerate(exterior_frames):
        html += f'<img src="/storage/{vin}/exterior/{frame}" style="display:none" data-index="{i}">'
    
    html += """
            </div>
        </div>
        
        <div id="interior" class="tab-content">
            <div class="interior-grid">
    """
    
    for photo in interior_photos:
        html += f'<div class="interior-photo"><img src="/storage/{vin}/interior/{photo}" loading="lazy"></div>'
    
    html += """
            </div>
        </div>
        
        <script>
            const frames = document.querySelectorAll('#exteriorViewer img');
            let currentFrame = 0;
            let isDragging = false;
            let startX = 0;
            let startFrame = 0;
            
            if(frames.length > 0) frames[0].style.display = 'block';
            
            const viewer = document.getElementById('exteriorViewer');
            
            viewer.onmousedown = (e) => { isDragging = true; startX = e.clientX; startFrame = currentFrame; };
            viewer.onmousemove = (e) => {
                if(!isDragging) return;
                const dx = e.clientX - startX;
                const frameDelta = Math.floor(Math.abs(dx) / 20);
                if(frameDelta > 0) {
                    currentFrame = (startFrame + (dx > 0 ? -frameDelta : frameDelta) + frames.length) % frames.length;
                    updateFrame();
                }
            };
            viewer.onmouseup = () => isDragging = false;
            viewer.onmouseleave = () => isDragging = false;
            
            viewer.ontouchstart = (e) => { isDragging = true; startX = e.touches[0].clientX; startFrame = currentFrame; };
            viewer.ontouchmove = (e) => {
                if(!isDragging) return;
                const dx = e.touches[0].clientX - startX;
                const frameDelta = Math.floor(Math.abs(dx) / 30);
                if(frameDelta > 0) {
                    currentFrame = (startFrame + (dx > 0 ? -frameDelta : frameDelta) + frames.length) % frames.length;
                    updateFrame();
                }
            };
            viewer.ontouchend = () => isDragging = false;
            
            function updateFrame() {
                frames.forEach(f => f.style.display = 'none');
                frames[currentFrame].style.display = 'block';
            }
            
            function showTab(tab) {
                document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.tab')[tab === 'exterior' ? 0 : 1].classList.add('active');
                document.getElementById(tab).classList.add('active');
            }
        </script>
    </body>
    </html>
    """
    return HTMLResponse(html)

@app.delete("/api/vehicle/{vin}")
async def delete_vehicle(vin: str):
    vin = vin.upper()
    vehicle_dir = get_vehicle_dir(vin)
    
    if vehicle_dir.exists():
        shutil.rmtree(vehicle_dir)
        return {"status": "ok"}
    
    raise HTTPException(status_code=404, detail="Vehicle not found")

app.mount("/storage", StaticFiles(directory=str(STORAGE_DIR)), name="storage")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
