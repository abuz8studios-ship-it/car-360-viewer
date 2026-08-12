"""
LotVue360 Mobile — installable PWA for Android + iOS.
Sign in → scan/enter VIN → guided 360 capture (uploads each shot live) →
interior/gallery → finalize → share. Same backend as the dashboard, so every
car captured on the phone appears in the home app instantly.

Served routes:  GET /mobile · /mobile/manifest.json · /mobile/sw.js
Icons are generated once into static/mobile/ at registration.

Wired from main.py:  from mobile_app import register_mobile; register_mobile(app)
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse, Response

STATIC_MOBILE = Path(__file__).resolve().parent / "static" / "mobile"

router = APIRouter()

ANGLES = [
    "Front", "Front-Right 45°", "Right Side", "Rear-Right 45°",
    "Rear", "Rear-Left 45°", "Left Side", "Front-Left 45°",
    "Front Low", "Rear Low", "Front-Right Wide", "Front-Left Wide",
]

MANIFEST = {
    "name": "LotVue360 Capture",
    "short_name": "LotVue360",
    "description": "Capture a 360 interactive car tour from your phone",
    "start_url": "/mobile",
    "scope": "/mobile",
    "display": "standalone",
    "background_color": "#F8FAFC",
    "theme_color": "#2E75B6",
    "icons": [
        {"src": "/static/mobile/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/static/mobile/icon-512.png", "sizes": "512x512", "type": "image/png"},
    ],
}

SW_JS = """
const SHELL = 'lotvue-mobile-v1';
self.addEventListener('install', e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(['/mobile'])));
  self.skipWaiting();
});
self.addEventListener('activate', e => { e.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  // network-first for APIs, cache-first for the shell
  if (url.pathname === '/mobile') {
    e.respondWith(fetch(e.request).catch(() => caches.match('/mobile')));
  }
});
"""

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover, user-scalable=no">
<title>LotVue360 Capture</title>
<link rel="manifest" href="/mobile/manifest.json">
<meta name="theme-color" content="#2E75B6">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="LotVue360">
<link rel="apple-touch-icon" href="/static/mobile/icon-192.png">
<script src="https://cdn.jsdelivr.net/npm/@zxing/library@0.21.3/umd/index.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent;}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#F8FAFC;color:#2C3E50;min-height:100vh;min-height:100dvh;}
.screen{display:none;flex-direction:column;min-height:100vh;min-height:100dvh;padding:1.2rem;padding-bottom:calc(1.2rem + env(safe-area-inset-bottom));}
.screen.active{display:flex;}
.brand{font-size:1.5rem;font-weight:800;color:#2E75B6;text-align:center;margin:2rem 0 0.3rem;}
.brand small{display:block;font-size:0.8rem;font-weight:500;color:#7A8CA0;margin-top:0.2rem;}
.card{background:#FFFFFF;border-radius:14px;box-shadow:0 2px 10px rgba(44,62,80,0.08);padding:1.3rem;margin:0.8rem 0;}
label{font-size:0.8rem;font-weight:600;color:#7A8CA0;display:block;margin:0.7rem 0 0.25rem;}
input{width:100%;padding:0.85rem;border:1.5px solid #DDE5EE;border-radius:10px;font-size:1rem;background:#FFFFFF;color:#2C3E50;}
input:focus{outline:none;border-color:#2E75B6;}
.btn{width:100%;padding:1rem;border:none;border-radius:12px;background:#2E75B6;color:#fff;font-size:1.05rem;font-weight:700;margin-top:1rem;cursor:pointer;}
.btn:active{transform:scale(0.98);}
.btn.secondary{background:#FFFFFF;color:#2E75B6;border:1.5px solid #2E75B6;}
.btn.ghost{background:transparent;color:#7A8CA0;border:none;box-shadow:none;font-weight:600;}
.btn:disabled{opacity:0.5;}
.err{color:#C0392B;font-size:0.85rem;margin-top:0.6rem;text-align:center;min-height:1.1em;}
.ok{color:#1E8449;}
/* capture screen */
#camWrap{position:fixed;inset:0;background:#000;display:none;z-index:50;}
#camWrap.active{display:block;}
#cam{width:100%;height:100%;object-fit:cover;}
.cam-overlay{position:absolute;inset:0;pointer-events:none;display:flex;flex-direction:column;justify-content:space-between;}
.cam-top{background:linear-gradient(rgba(0,0,0,0.55),transparent);padding:max(1rem, env(safe-area-inset-top)) 1rem 1.6rem;color:#fff;text-align:center;}
.cam-top .angle{font-size:1.25rem;font-weight:800;}
.cam-top .count{font-size:0.85rem;opacity:0.85;margin-top:0.15rem;}
.silhouette{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:86%;opacity:0.55;pointer-events:none;transition:opacity 0.25s;}
.silhouette.flip{transform:translate(-50%,-50%) scaleX(-1);}
.silhouette.narrow{width:58%;}
.silhouette.wide{width:96%;}
.levelbar{position:absolute;top:calc(max(1rem, env(safe-area-inset-top)) + 4.2rem);left:50%;transform:translateX(-50%);width:120px;height:26px;pointer-events:none;}
.levelbar .bubble-track{position:absolute;inset:0;border:2px solid rgba(255,255,255,0.55);border-radius:13px;}
.levelbar .bubble{position:absolute;top:3px;left:50%;width:16px;height:16px;border-radius:50%;background:#E74C3C;transition:left 0.12s,background 0.2s;}
.levelbar .bubble.good{background:#2ECC71;}
.levelbar .tick{position:absolute;left:50%;top:-4px;width:2px;height:34px;background:rgba(255,255,255,0.55);}
.leveltxt{position:absolute;top:calc(max(1rem, env(safe-area-inset-top)) + 6.2rem);width:100%;text-align:center;color:#fff;font-size:0.72rem;opacity:0.8;pointer-events:none;}
.cam-bottom{background:linear-gradient(transparent,rgba(0,0,0,0.6));padding:1.4rem 1rem calc(1.4rem + env(safe-area-inset-bottom));display:flex;align-items:center;justify-content:space-between;pointer-events:auto;}
.shutter{width:74px;height:74px;border-radius:50%;background:#fff;border:5px solid #2E75B6;cursor:pointer;}
.shutter:active{transform:scale(0.92);}
.cam-side{color:#fff;font-weight:700;background:rgba(0,0,0,0.35);border:none;border-radius:10px;padding:0.7rem 1rem;cursor:pointer;}
.thumbs{display:flex;gap:6px;overflow-x:auto;padding:0.5rem 0;}
.thumbs img{height:54px;border-radius:6px;border:2px solid #DDE5EE;}
.progressbar{height:10px;background:#DDE5EE;border-radius:5px;overflow:hidden;margin-top:0.8rem;}
.progressbar div{height:100%;background:#2E75B6;width:0%;transition:width 0.4s;}
.steps{display:flex;justify-content:center;gap:0.4rem;margin:0.8rem 0;}
.dot{width:9px;height:9px;border-radius:50%;background:#DDE5EE;}
.dot.on{background:#2E75B6;}
.big{font-size:2.4rem;text-align:center;margin:1rem 0;}
.hint{font-size:0.85rem;color:#7A8CA0;text-align:center;margin-top:0.5rem;line-height:1.45;}
a.link{color:#2E75B6;font-weight:700;text-decoration:none;}
#scanWrap{position:fixed;inset:0;background:#000;display:none;z-index:60;}
#scanWrap.active{display:block;}
#scanVid{width:100%;height:100%;object-fit:cover;}
.scan-frame{position:absolute;left:50%;top:45%;transform:translate(-50%,-50%);width:84%;height:120px;border:3px solid #2E75B6;border-radius:10px;box-shadow:0 0 0 2000px rgba(0,0,0,0.45);}
.scan-close{position:absolute;top:max(1rem, env(safe-area-inset-top));right:1rem;background:rgba(0,0,0,0.5);color:#fff;border:none;border-radius:10px;padding:0.7rem 1rem;font-weight:700;}
.scan-hint{position:absolute;bottom:calc(3rem + env(safe-area-inset-bottom));width:100%;text-align:center;color:#fff;font-size:0.9rem;}
</style>
</head>
<body>

<!-- SIGN IN -->
<div class="screen active" id="s-login">
  <div class="brand">LotVue360 <small>360° capture — synced to your dashboard</small></div>
  <div class="card">
    <label>Email</label><input id="email" type="email" autocomplete="username" placeholder="you@dealership.com">
    <label>Password</label><input id="password" type="password" autocomplete="current-password" placeholder="••••••••">
    <button class="btn" onclick="doLogin()">Sign In</button>
    <div class="err" id="loginErr"></div>
    <div class="hint">New here? <a class="link" href="/register">Create an account</a></div>
  </div>
</div>

<!-- VIN -->
<div class="screen" id="s-vin">
  <div class="brand">New Vehicle</div>
  <div class="card">
    <button class="btn secondary" onclick="openScanner()">📷 Scan VIN barcode</button>
    <div class="hint">Door-jamb sticker or windshield plate</div>
    <label>VIN</label><input id="vin" maxlength="17" autocapitalize="characters" placeholder="17-character VIN">
    <div id="decoded" class="hint"></div>
    <label>Price (optional)</label><input id="price" inputmode="numeric" placeholder="15995">
    <label>Mileage (optional)</label><input id="mileage" inputmode="numeric" placeholder="61500">
    <button class="btn" onclick="createVehicle()">Continue → Capture</button>
    <div class="err" id="vinErr"></div>
  </div>
  <button class="btn ghost" onclick="logout()">Sign out</button>
</div>

<!-- CAPTURE HUB -->
<div class="screen" id="s-hub">
  <div class="brand" id="hubTitle">Capture</div>
  <div class="card">
    <b>1 · Exterior 360</b>
    <div class="hint">Walk around the car. We guide every angle — each shot uploads instantly.</div>
    <div class="thumbs" id="extThumbs"></div>
    <button class="btn" id="extBtn" onclick="startCapture()">Start 360 capture (0/12)</button>
  </div>
  <div class="card">
    <b>Or · Walkaround video</b>
    <div class="hint">One slow circle around the car (30–90s). We extract the frames and build the spin — no photo positioning needed.</div>
    <input type="file" id="vidFile" accept="video/*" capture="environment" style="display:none" onchange="uploadVideo(this)">
    <button class="btn secondary" onclick="document.getElementById('vidFile').click()" id="vidBtn">🎥 Record / upload walkaround</button>
    <div class="progressbar"><div id="vidProg"></div></div>
    <div class="err" id="vidMsg"></div>
  </div>
  <div class="card">
    <b>2 · Interior + extras (optional)</b>
    <div class="hint">Dash, seats, trunk, engine bay.</div>
    <input type="file" id="intFile" accept="image/*" capture="environment" style="display:none" onchange="uploadInterior(this)">
    <button class="btn secondary" onclick="document.getElementById('intFile').click()" id="intBtn">Add interior photo (0)</button>
  </div>
  <div class="card">
    <b>3 · Build the 360</b>
    <div class="hint">Background removal, turntable shadow, smooth spin — built on our servers.</div>
    <button class="btn" id="buildBtn" onclick="finalize()" disabled>Build my 360</button>
    <div class="progressbar"><div id="buildProg"></div></div>
    <div class="err" id="buildMsg"></div>
  </div>
</div>

<!-- DONE -->
<div class="screen" id="s-done">
  <div class="big">🎉</div>
  <div class="brand">360 is live!</div>
  <div class="card" style="text-align:center">
    <div class="hint" id="doneTitle"></div>
    <button class="btn" onclick="window.open('/view/'+VIN,'_blank')">Open interactive 360</button>
    <button class="btn secondary" onclick="copyShare()">Copy share link</button>
    <div class="err ok" id="copyMsg"></div>
    <div class="hint">Already synced to your dashboard — manage photos, leads and exports there.</div>
  </div>
  <button class="btn ghost" onclick="resetFlow()">+ Capture another vehicle</button>
</div>

<!-- CAMERA -->
<div id="camWrap">
  <video id="cam" autoplay playsinline muted></video>
  <div id="ghost" class="silhouette"></div>
  <div class="levelbar"><div class="bubble-track"></div><div class="tick"></div><div class="bubble" id="lvlBubble"></div></div>
  <div class="leveltxt" id="lvlTxt">Hold the phone level</div>
  <div class="cam-overlay">
    <div class="cam-top">
      <div class="angle" id="angleName">Front</div>
      <div class="count" id="angleCount">Photo 1 of 12</div>
    </div>
    <div class="cam-bottom">
      <button class="cam-side" onclick="closeCapture()">Done</button>
      <div class="shutter" onclick="snap()"></div>
      <button class="cam-side" onclick="skipAngle()">Skip</button>
    </div>
  </div>
</div>

<!-- VIN SCANNER -->
<div id="scanWrap">
  <video id="scanVid" autoplay playsinline muted></video>
  <div class="scan-frame"></div>
  <button class="scan-close" onclick="closeScanner()">Close</button>
  <div class="scan-hint">Align the VIN barcode inside the frame</div>
</div>

<script>
const ANGLES = __ANGLES__;
let TOKEN = localStorage.getItem('lv_token') || '';
let VIN = localStorage.getItem('lv_vin') || '';
let extCount = 0, realShots = 0, intCount = 0, camStream = null, scanReader = null;

// ── per-angle wireframe ghosts (the replicable-positioning guides) ──────────
// Every car gets shot at the SAME 12 stops, each with its own outline: match
// the car to the ghost and the capture is exactly repeatable across inventory.
const S = 'fill="none" stroke="#FFFFFF" stroke-width="3" stroke-dasharray="10 7"';
const GHOSTS = {
  side: `<svg viewBox="0 0 400 160" xmlns="http://www.w3.org/2000/svg">
    <path d="M30 110 Q35 85 80 78 Q120 45 175 42 Q240 40 275 70 Q330 76 360 92 Q372 96 370 110 L355 112 A22 22 0 1 0 311 114 L130 114 A22 22 0 1 0 86 114 L32 112 Z" ${S}/>
    <path d="M120 76 Q150 52 185 50 L230 52 Q255 58 268 72" ${S} stroke-width="2" stroke-dasharray="6 5"/></svg>`,
  front: `<svg viewBox="0 0 240 170" xmlns="http://www.w3.org/2000/svg">
    <path d="M45 130 L45 95 Q47 70 62 62 L70 40 Q75 30 95 28 L145 28 Q165 30 170 40 L178 62 Q193 70 195 95 L195 130 Q195 140 185 140 L55 140 Q45 140 45 130 Z" ${S}/>
    <path d="M74 44 L166 44 L172 62 L68 62 Z" ${S} stroke-width="2" stroke-dasharray="6 5"/>
    <rect x="58" y="98" width="34" height="16" rx="8" ${S} stroke-width="2"/>
    <rect x="148" y="98" width="34" height="16" rx="8" ${S} stroke-width="2"/>
    <path d="M62 122 L178 122" ${S} stroke-width="2" stroke-dasharray="5 5"/>
    <path d="M40 78 L30 82 L30 92 L42 90 M200 78 L210 82 L210 92 L198 90" ${S} stroke-width="2"/></svg>`,
  rear: `<svg viewBox="0 0 240 170" xmlns="http://www.w3.org/2000/svg">
    <path d="M45 130 L45 95 Q47 72 60 64 L68 42 Q74 32 95 30 L145 30 Q166 32 172 42 L180 64 Q193 72 195 95 L195 130 Q195 140 185 140 L55 140 Q45 140 45 130 Z" ${S}/>
    <path d="M76 46 L164 46 L170 62 L70 62 Z" ${S} stroke-width="2" stroke-dasharray="6 5"/>
    <path d="M56 96 L92 96 L92 112 L56 112 Z M148 96 L184 96 L184 112 L148 112 Z" ${S} stroke-width="2"/>
    <path d="M96 120 L144 120" ${S} stroke-width="2"/></svg>`,
  q34: `<svg viewBox="0 0 400 170" xmlns="http://www.w3.org/2000/svg">
    <path d="M40 118 Q42 92 78 84 Q112 50 170 46 Q235 44 268 72 Q322 80 352 98 Q366 102 364 118 L348 121 A21 21 0 1 0 306 123 L146 123 A23 23 0 1 0 100 124 L42 120 Z" ${S}/>
    <path d="M118 82 Q148 56 182 54 L228 56 Q252 62 264 74" ${S} stroke-width="2" stroke-dasharray="6 5"/>
    <path d="M40 118 Q60 108 92 106 M40 106 L52 96" ${S} stroke-width="2"/></svg>`,
};
// angle index -> which ghost + mirror/size  (matches the ANGLES list order)
const GUIDES = [
  {g:'front'},               {g:'q34'},                {g:'side'},               {g:'q34',flip:1},
  {g:'rear'},                {g:'q34'},                {g:'side',flip:1},        {g:'q34',flip:1},
  {g:'front',cls:'narrow'},  {g:'rear',cls:'narrow'},  {g:'q34',cls:'wide'},     {g:'q34',flip:1,cls:'wide'},
];

// ── live level bubble (DeviceOrientation; iOS needs a permission tap) ───────
let lvlOn = false;
function startLevel(){
  const go = () => { window.addEventListener('deviceorientation', onTilt); lvlOn = true; };
  try{
    if (typeof DeviceOrientationEvent !== 'undefined' && DeviceOrientationEvent.requestPermission){
      DeviceOrientationEvent.requestPermission().then(p => { if(p==='granted') go(); }).catch(()=>{});
    } else { go(); }
  }catch(_){}
}
function onTilt(e){
  if (e.gamma === null) return;
  // portrait hold: gamma = roll (left/right tilt). Level = |gamma| small.
  const roll = Math.max(-30, Math.min(30, e.gamma));
  const bubble = document.getElementById('lvlBubble');
  bubble.style.left = `calc(50% + ${roll*1.6}px - 8px)`;
  const good = Math.abs(roll) < 6;
  bubble.classList.toggle('good', good);
  document.getElementById('lvlTxt').textContent = good ? 'Level ✓' : 'Hold the phone level';
}

function show(id){document.querySelectorAll('.screen').forEach(s=>s.classList.remove('active'));document.getElementById(id).classList.add('active');}
function authHeaders(extra){const h = extra || {};if (TOKEN) h['Authorization'] = 'Bearer ' + TOKEN;return h;}

async function api(path, opts){
  opts = opts || {};
  opts.headers = authHeaders(opts.headers);
  const r = await fetch(path, opts);
  if (r.status === 401){ show('s-login'); throw new Error('Sign in required'); }
  return r;
}

// ── auth ────────────────────────────────────────────────────────────────────
async function doLogin(){
  const err = document.getElementById('loginErr'); err.textContent='';
  try{
    const r = await fetch('/api/auth/token',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({email:document.getElementById('email').value.trim(),password:document.getElementById('password').value})});
    if(!r.ok){err.textContent='Wrong email or password';return;}
    const t = await r.json();
    TOKEN = t.access_token; localStorage.setItem('lv_token', TOKEN);
    show('s-vin');
  }catch(e){err.textContent='Network error — try again';}
}
function logout(){TOKEN='';localStorage.removeItem('lv_token');fetch('/api/auth/logout',{method:'POST'});show('s-login');}

// ── VIN ─────────────────────────────────────────────────────────────────────
document.getElementById('vin').addEventListener('input', async (e)=>{
  const v = e.target.value.trim().toUpperCase(); e.target.value = v;
  if(v.length===17){
    document.getElementById('decoded').textContent='Decoding…';
    try{
      const r = await api('/api/vin/'+v+'/decode'); const d = await r.json();
      const dd = d.decoded||{};
      document.getElementById('decoded').textContent = dd.year? `✓ ${dd.year} ${dd.make} ${dd.model} ${dd.trim_level||''}` : 'VIN not recognized — you can still continue';
    }catch(_){document.getElementById('decoded').textContent='';}
  }
});

async function createVehicle(){
  const err=document.getElementById('vinErr');err.textContent='';
  const v=document.getElementById('vin').value.trim().toUpperCase();
  if(v.length<11){err.textContent='Enter the full VIN (or scan it)';return;}
  const fd=new FormData();
  fd.append('vin',v);
  fd.append('price',document.getElementById('price').value||'');
  fd.append('mileage',document.getElementById('mileage').value||'');
  try{
    const r=await api('/api/create-vehicle',{method:'POST',body:fd});
    const d=await r.json();
    if(d.status!=='ok'&&!d.vin){err.textContent=d.detail||d.error||'Could not create vehicle';return;}
    VIN=v;localStorage.setItem('lv_vin',VIN);
    extCount=0;realShots=0;intCount=0;updateHub();
    document.getElementById('hubTitle').textContent='Capture · '+VIN.slice(-6);
    show('s-hub');
  }catch(e){err.textContent='Failed: '+e.message;}
}

// ── VIN barcode scanner (ZXing — works iOS Safari + Android Chrome) ────────
async function openScanner(){
  document.getElementById('scanWrap').classList.add('active');
  try{
    scanReader = new ZXing.BrowserMultiFormatReader();
    const devices = await ZXing.BrowserMultiFormatReader.listVideoInputDevices?.() || [];
    await scanReader.decodeFromConstraints(
      {video:{facingMode:'environment'}}, 'scanVid',
      (result)=>{ if(result){
        let txt = result.getText().trim().toUpperCase();
        if(txt.length===18 && (txt[0]==='I'||txt[0]==='*')) txt = txt.slice(1); // some VIN barcodes prefix a char
        txt = txt.replace(/[^A-Z0-9]/g,'');
        if(txt.length>=11 && txt.length<=17){
          document.getElementById('vin').value = txt;
          document.getElementById('vin').dispatchEvent(new Event('input'));
          closeScanner();
        }
      }});
  }catch(e){
    closeScanner();
    alert('Camera blocked — allow camera access in your browser settings, or type the VIN.');
  }
}
function closeScanner(){
  document.getElementById('scanWrap').classList.remove('active');
  try{scanReader && scanReader.reset();}catch(_){}
}

// ── guided 360 capture ──────────────────────────────────────────────────────
function updateHub(){
  document.getElementById('extBtn').textContent =
    extCount >= ANGLES.length ? `✓ Exterior complete (${extCount})` : `Start 360 capture (${extCount}/${ANGLES.length})`;
  document.getElementById('intBtn').textContent = `Add interior photo (${intCount})`;
  // 4 real WALKAROUND shots = the minimum for a true 360 (skips + beauty shots don't count)
  document.getElementById('buildBtn').disabled = realShots < 4;
}

async function startCapture(){
  if(!lvlOn) startLevel();   // BEFORE any await — iOS grants sensor permission only inside the tap
  try{
    camStream = await navigator.mediaDevices.getUserMedia({video:{facingMode:'environment',width:{ideal:1920},height:{ideal:1080}}});
  }catch(e){alert('Camera blocked — allow camera access for this site.');return;}
  document.getElementById('cam').srcObject = camStream;
  document.getElementById('camWrap').classList.add('active');
  updateAngleUI();
}
function updateAngleUI(){
  const i = Math.min(extCount, ANGLES.length-1);
  document.getElementById('angleName').textContent = extCount>=ANGLES.length ? 'Extra shots' : ANGLES[i];
  document.getElementById('angleCount').textContent = `Photo ${extCount+1}${extCount>=ANGLES.length?'':' of '+ANGLES.length}`;
  const gd = GUIDES[i] || {g:'side'};
  const ghost = document.getElementById('ghost');
  ghost.innerHTML = GHOSTS[gd.g];
  ghost.className = 'silhouette' + (gd.flip ? ' flip' : '') + (gd.cls ? ' ' + gd.cls : '');
}
function skipAngle(){extCount++;updateAngleUI();updateHub();}
function closeCapture(){
  document.getElementById('camWrap').classList.remove('active');
  if(camStream){camStream.getTracks().forEach(t=>t.stop());camStream=null;}
  updateHub();
}

async function snap(){
  const video=document.getElementById('cam');
  const c=document.createElement('canvas');
  c.width=video.videoWidth;c.height=video.videoHeight;
  c.getContext('2d').drawImage(video,0,0);
  const blob=await new Promise(res=>c.toBlob(res,'image/jpeg',0.92));
  const idx=extCount;
  extCount++;
  // Only the 8 walkaround stops feed the 360 loop. The low/wide beauty shots
  // (stops 9-12) go to the GALLERY — mixing camera heights into the spin makes
  // the rotation whip. realShots gates the build on true spin shots only.
  const isSpinShot = idx < 8;
  if(isSpinShot) realShots++;
  updateAngleUI();updateHub();
  const t=document.createElement('img');t.src=URL.createObjectURL(blob);
  document.getElementById('extThumbs').appendChild(t);
  if(isSpinShot){ uploadWithRetry('/api/upload/exterior', blob, idx); }
  else{ uploadGallery(blob, idx); }
}

async function uploadGallery(blob, idx){
  for(let attempt=0;attempt<5;attempt++){
    try{
      const fd=new FormData();
      fd.append('vin',VIN);
      fd.append('category','beauty');
      fd.append('photo',blob,`${VIN}_beauty_${String(idx).padStart(2,'0')}.jpg`);
      const r=await api('/api/upload/gallery',{method:'POST',body:fd});
      if(r.ok) return true;
    }catch(_){/* offline — retry */}
    await new Promise(res=>setTimeout(res,1500*(attempt+1)));
  }
  return false;
}

async function uploadWithRetry(path, blob, idx, kind){
  for(let attempt=0;attempt<5;attempt++){
    try{
      const fd=new FormData();
      fd.append('vin',VIN);
      fd.append('index',String(idx));
      fd.append('photo',blob,`${VIN}_${kind||'ext'}_${String(idx).padStart(2,'0')}.jpg`);
      const r=await api(path,{method:'POST',body:fd});
      if(r.ok) return true;
    }catch(_){/* offline — retry */}
    await new Promise(res=>setTimeout(res,1500*(attempt+1)));
  }
  document.getElementById('buildMsg').textContent='Some photos failed to upload — check connection.';
  return false;
}

async function uploadInterior(inp){
  if(!inp.files.length)return;
  const ok = await uploadWithRetry('/api/upload/interior', inp.files[0], intCount, 'int');
  if(ok){intCount++;updateHub();}
  inp.value='';
}

// ── walkaround video -> premium engine ─────────────────────────────────────
async function jsonDetail(r){
  // Cloudflare-fronted errors (413 etc.) are HTML, not JSON — never blow up on them
  try{ return (await r.json()).detail || ('server said '+r.status); }
  catch(_){ return r.status===413 ? 'file too large for the connection' : ('server said '+r.status); }
}

// Render the quality gate's structured reasons + an explicit override path.
// The gate is the front door: bad captures get told WHY, not just "failed".
function showGateReasons(el, gate, retryFn){
  const reasons = (gate && gate.reasons) || [];
  el.classList.remove('ok');
  el.innerHTML = '<b>Capture check failed:</b><ul style="margin:6px 0 8px;padding-left:18px">' +
    reasons.map(x=>'<li>'+String(x).replace(/</g,'&lt;')+'</li>').join('') +
    '</ul><button class="btn secondary" style="margin-top:4px" id="gateForceBtn">Upload anyway</button>';
  document.getElementById('gateForceBtn').onclick = ()=>{ el.textContent=''; retryFn(); };
}

async function uploadVideo(inp, fileOverride, force){
  if(!fileOverride && !inp.files.length) return;
  const f = fileOverride || inp.files[0]; if(inp) inp.value='';
  const msg=document.getElementById('vidMsg'); msg.textContent=''; msg.classList.remove('ok');
  const bar=document.getElementById('vidProg');
  // the public URL rides a Cloudflare tunnel that caps uploads at ~100MB
  if(f.size > 95*1024*1024){ msg.textContent='Video over 95MB — trim to ~60s or lower the resolution and retry.'; return; }
  document.getElementById('vidBtn').disabled=true;
  msg.textContent= force ? 'Uploading video (quality check overridden)…' : 'Uploading video…';
  try{
    const fd=new FormData();
    fd.append('video', f, 'walkaround.mp4');
    fd.append('style','natural');
    if(force) fd.append('force','1');
    const r=await api('/api/vehicle/'+VIN+'/generate-3d',{method:'POST',body:fd});
    if(r.status===422){
      let d=null; try{ d=(await r.json()).detail; }catch(_){}
      if(d && d.gate){
        showGateReasons(msg, d.gate, ()=>uploadVideo(null, f, true));
        document.getElementById('vidBtn').disabled=false;
        return;
      }
      throw new Error((d && d.error) || 'capture rejected');
    }
    if(!r.ok){ throw new Error(await jsonDetail(r)); }
    msg.textContent='Building the 360 from your video…';
    const done = await pollBuild(bar);
    if(done==='ok'){ document.getElementById('doneTitle').textContent='VIN '+VIN; show('s-done'); return; }
    msg.textContent = done==='timeout' ? 'Still processing — check the dashboard in a few minutes.' : done;
  }catch(e){ msg.textContent='Video build failed: '+e.message; }
  document.getElementById('vidBtn').disabled=false;
}

// shared premium-build poller: 45 min window (WAN arcs are minutes each),
// resolves 'ok' | 'timeout' | error text
async function pollBuild(bar){
  for(let i=0;i<675;i++){
    await new Promise(res=>setTimeout(res,4000));
    let s;
    try{ s=await (await api('/api/vehicle/'+VIN+'/3d-status')).json(); }
    catch(_){ continue; }   // network blip — keep polling
    bar.style.width=Math.min(98, s.progress||0)+'%';
    if(s.status==='error') return 'Build failed: '+(s.error||'unknown error');
    if(s.status==='done' && !s.spin_ready) return 'Build finished without a spin — check the photos and retry.';
    if(s.spin_ready && (s.status==='done'||s.progress>=100)){ bar.style.width='100%'; return 'ok'; }
  }
  return 'timeout';
}

// ── finalize (photos -> premium engine: stabilized + AI-densified 360) ──────
async function finalize(){
  const msg=document.getElementById('buildMsg');msg.textContent='';msg.classList.remove('ok');
  const btn=document.getElementById('buildBtn');
  btn.disabled=true;
  try{
    const r=await api('/api/finalize/'+VIN+'?engine=pro',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    if(!r.ok){ throw new Error(await jsonDetail(r)); }
    const mode=(await r.json()).engine||'classic';
    const bar=document.getElementById('buildProg');
    if(mode==='pro'){
      const done = await pollBuild(bar);
      if(done==='ok'){ document.getElementById('doneTitle').textContent='VIN '+VIN; show('s-done'); return; }
      msg.textContent = done==='timeout' ? 'Still processing — check the dashboard in a few minutes.' : done;
    }else{
      for(let i=0;i<220;i++){
        await new Promise(res=>setTimeout(res,4000));
        const s=await (await api('/api/finalize-status/'+VIN)).json();
        const pct=Math.min(95, Math.round((s.spin_frames||0)/36*100));
        bar.style.width=(s.ready?100:pct)+'%';
        if(s.ready){
          document.getElementById('doneTitle').textContent='VIN '+VIN;
          show('s-done');return;
        }
      }
      msg.textContent='Still processing — check the dashboard in a minute.';
    }
  }catch(e){msg.textContent='Build failed: '+e.message;}
  btn.disabled=false;
}

function copyShare(){
  navigator.clipboard.writeText(location.origin+'/view/'+VIN)
    .then(()=>{document.getElementById('copyMsg').textContent='Link copied ✓';});
}
function resetFlow(){
  VIN='';localStorage.removeItem('lv_vin');
  extCount=0;realShots=0;intCount=0;
  document.getElementById('vin').value='';document.getElementById('decoded').textContent='';
  document.getElementById('extThumbs').innerHTML='';
  document.getElementById('buildProg').style.width='0%';
  document.getElementById('vidProg').style.width='0%';
  updateHub();
  show('s-vin');
}

// ── boot ────────────────────────────────────────────────────────────────────
if('serviceWorker' in navigator){navigator.serviceWorker.register('/mobile/sw.js');}
(async()=>{
  if(TOKEN){
    try{
      const r=await fetch('/api/auth/me',{headers:authHeaders()});
      if(r.ok){show(VIN?'s-hub':'s-vin');if(VIN){document.getElementById('hubTitle').textContent='Capture · '+VIN.slice(-6);}return;}
    }catch(_){}
    TOKEN='';localStorage.removeItem('lv_token');
  }
  show('s-login');
})();
</script>
</body>
</html>"""


def _make_icons():
    """Generate PWA icons once (Pillow, no downloads)."""
    from PIL import Image, ImageDraw
    STATIC_MOBILE.mkdir(parents=True, exist_ok=True)
    for size in (192, 512):
        f = STATIC_MOBILE / f"icon-{size}.png"
        if f.exists():
            continue
        img = Image.new("RGB", (size, size), "#2E75B6")
        d = ImageDraw.Draw(img)
        m = size // 8
        # stylized 360 ring + car dot
        d.ellipse([m, size * 0.32, size - m, size * 0.78], outline="#FFFFFF", width=max(4, size // 40))
        d.ellipse([size * 0.42, size * 0.47, size * 0.58, size * 0.63], fill="#F8FAFC")
        d.polygon([(size - m, size * 0.55), (size - m - size * 0.08, size * 0.48),
                   (size - m - size * 0.08, size * 0.62)], fill="#FFFFFF")
        img.save(f, "PNG")


@router.get("/mobile", response_class=HTMLResponse)
async def mobile_app():
    import json
    return HTML.replace("__ANGLES__", json.dumps(ANGLES))


@router.get("/mobile/manifest.json")
async def mobile_manifest():
    return JSONResponse(MANIFEST, media_type="application/manifest+json")


@router.get("/mobile/sw.js")
async def mobile_sw():
    return Response(SW_JS, media_type="application/javascript")


def register_mobile(app):
    try:
        _make_icons()
    except Exception as e:
        print(f"[Mobile] icon generation failed (non-fatal): {e}")
    app.include_router(router)
    print("[Mobile] PWA registered at /mobile (installable on iOS + Android, "
          "guided 360 capture, VIN barcode scan, live sync)")
