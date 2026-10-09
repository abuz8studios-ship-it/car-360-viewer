/* ============================================================================
   <lotvue-spin> — LotVue Pro 360° photo spin viewer
   GLO3D-grade interaction on plain dealer photos.

   Renderer : three.js (orthographic + single swappable texture)
              -> GPU-smooth zoom/pan, zero repaint jank, 1 texture on GPU
   Physics  : circlr-style drag with velocity + inertial decay + snap
   Loading  : parallel preload, progress ring, first-frame-fast
   Input    : mouse drag, touch drag, pinch zoom, wheel zoom, dbl-click zoom,
              arrow keys, drag-to-pan when zoomed
   Chrome   : progress ring, drag hint, position dots, zoom/fullscreen/reset

   <lotvue-spin vin="..."></lotvue-spin>
   or  <lotvue-spin frames='["/a.jpg","/b.jpg"]'></lotvue-spin>
   ========================================================================== */
import * as THREE from 'https://cdn.jsdelivr.net/npm/three@0.164.1/build/three.module.js';

const TPL = `
<style>
  :host{display:block;position:relative;width:100%;height:100%;min-height:300px;
    background:
      radial-gradient(120% 85% at 50% 8%, #1b2c55 0%, rgba(27,44,85,0) 62%),
      linear-gradient(172deg,#0d1a3d 0%,#101f47 45%,#0a1430 100%);
    border-radius:14px;overflow:hidden;touch-action:none;user-select:none;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    -webkit-tap-highlight-color:transparent}
  canvas{position:absolute;inset:0;width:100%;height:100%;display:block;
    cursor:grab;opacity:0;transition:opacity .5s ease}
  canvas.ready{opacity:1}
  canvas.grabbing{cursor:grabbing}
  canvas.zoomed{cursor:move}

  /* ---- loading ---- */
  .load{position:absolute;inset:0;display:flex;flex-direction:column;
    align-items:center;justify-content:center;gap:14px;z-index:6;
    transition:opacity .45s ease}
  .load.gone{opacity:0;pointer-events:none}
  .ring{width:54px;height:54px;transform:rotate(-90deg)}
  .ring circle{fill:none;stroke-width:3;stroke-linecap:round}
  .ring .bg{stroke:rgba(140,175,240,.18)}
  .ring .fg{stroke:#7ba4f5;transition:stroke-dashoffset .18s linear}
  .load span{font-size:11px;letter-spacing:2.4px;color:#8fa8d6;text-transform:uppercase}

  /* ---- hint ---- */
  .hint{position:absolute;left:50%;bottom:60px;transform:translateX(-50%);
    z-index:5;display:flex;align-items:center;gap:9px;padding:8px 16px;
    background:rgba(8,15,35,.72);backdrop-filter:blur(10px);border-radius:99px;
    border:1px solid rgba(140,175,240,.22);color:#cfdcf5;font-size:12.5px;
    letter-spacing:.4px;white-space:nowrap;transition:opacity .5s ease}
  .hint.gone{opacity:0;pointer-events:none}
  .hint svg{width:15px;height:15px;stroke:#7ba4f5;fill:none;stroke-width:1.9}
  .hint b{animation:sweep 2.6s ease-in-out infinite;display:inline-block}
  @keyframes sweep{0%,100%{transform:translateX(-3px)}50%{transform:translateX(3px)}}

  /* ---- bottom bar ---- */
  .bar{position:absolute;left:0;right:0;bottom:0;z-index:5;display:flex;
    align-items:center;gap:12px;padding:12px 16px;
    background:linear-gradient(transparent,rgba(6,11,26,.85) 55%);
    opacity:0;transition:opacity .4s ease}
  .bar.on{opacity:1}
  .dots{flex:1;display:flex;align-items:center;gap:3px;height:16px}
  .dots i{flex:1;height:2px;border-radius:2px;background:rgba(150,180,235,.22);
    transition:background .15s,transform .15s}
  .dots i.on{background:#7ba4f5;transform:scaleY(2.6)}
  .btn{width:32px;height:32px;flex:0 0 32px;display:grid;place-items:center;
    border-radius:9px;cursor:pointer;background:rgba(255,255,255,.07);
    border:1px solid rgba(150,180,235,.2);transition:background .15s,border-color .15s}
  .btn:hover{background:rgba(123,164,245,.24);border-color:rgba(123,164,245,.5)}
  .btn svg{width:15px;height:15px;stroke:#d3e0f7;fill:none;stroke-width:1.9;
    stroke-linecap:round;stroke-linejoin:round}
  .btn.hide{display:none}

  /* ---- badges + hotspots ---- */
  .tag{position:absolute;top:12px;left:14px;z-index:5;font-size:10px;
    letter-spacing:1.6px;color:#93aad4;background:rgba(8,14,32,.6);
    padding:4px 11px;border-radius:99px;border:1px solid rgba(140,175,240,.22)}
  .zoomtag{position:absolute;top:12px;right:14px;z-index:5;font-size:10.5px;
    letter-spacing:1px;color:#93aad4;background:rgba(8,14,32,.6);padding:4px 10px;
    border-radius:99px;border:1px solid rgba(140,175,240,.22);opacity:0;
    transition:opacity .25s}
  .zoomtag.on{opacity:1}
  .hs{position:absolute;width:24px;height:24px;margin:-12px 0 0 -12px;z-index:4;
    border-radius:50%;background:rgba(123,164,245,.28);cursor:pointer;
    border:2px solid rgba(160,195,255,.95);animation:pulse 2.4s infinite;display:none}
  @keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(123,164,245,.55)}
    50%{box-shadow:0 0 0 10px rgba(123,164,245,0)}}
  .tip{position:absolute;z-index:7;max-width:230px;padding:9px 12px;
    background:rgba(9,16,36,.96);border:1px solid rgba(123,164,245,.45);
    border-radius:10px;color:#e4ecfb;font-size:12.5px;line-height:1.5;
    pointer-events:none;display:none}
  .err{position:absolute;inset:0;display:grid;place-items:center;z-index:8;
    color:#f0a0a0;font-size:13px;padding:20px;text-align:center}
</style>

<div class="load">
  <svg class="ring" viewBox="0 0 60 60">
    <circle class="bg" cx="30" cy="30" r="26"></circle>
    <circle class="fg" cx="30" cy="30" r="26" stroke-dasharray="163.4" stroke-dashoffset="163.4"></circle>
  </svg>
  <span class="pct">0%</span>
</div>

<div class="tag">360°</div>
<div class="zoomtag">1.0×</div>

<div class="hint">
  <svg viewBox="0 0 24 24"><path d="M4 12h16M8 8l-4 4 4 4M16 8l4 4-4 4"/></svg>
  <b>Drag to rotate</b>
</div>

<div class="bar">
  <div class="btn play" title="Auto-rotate">
    <svg viewBox="0 0 24 24"><path d="M5 3l14 9-14 9z" fill="currentColor" stroke="none"/></svg>
  </div>
  <div class="dots"></div>
  <div class="btn zin" title="Zoom in"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5M11 8v6M8 11h6"/></svg></div>
  <div class="btn zout" title="Zoom out"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5M8 11h6"/></svg></div>
  <div class="btn full" title="Fullscreen"><svg viewBox="0 0 24 24"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg></div>
</div>
<div class="tip"></div>`;

export class LotVueSpin extends HTMLElement {
  static observedAttributes = ['vin', 'frames'];

  connectedCallback() {
    if (this._connected) return;
    this._connected = true;
    this._raf = null;
    this._generation = (this._generation || 0) + 1;
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    this.shadowRoot.innerHTML = TPL;
    this.$ = s => this.shadowRoot.querySelector(s);
    this._loadPromises = new Map();
    this._loadQueue = [];
    this._activeLoads = 0;
    this._failedFrames = new Set();
    this.state = {
      i: 0, n: 0, urls: [], imgs: [], zoom: 1, px: 0, py: 0,
      vel: 0, dragging: false, panning: false, auto: false, ready: false,
    };
    this.#boot(this._generation);
  }

  disconnectedCallback() {
    this._connected = false;
    this._generation = (this._generation || 0) + 1;
    if (this._raf != null) cancelAnimationFrame(this._raf);
    this._raf = null;
    if (this._resizeObserver) this._resizeObserver.disconnect();
    if (this._introTimer) clearTimeout(this._introTimer);
    if (this._hintTimer) clearTimeout(this._hintTimer);
    this._introTimer = this._hintTimer = null;
    this._stopAuto = null;
    if (this._globalListeners) {
      window.removeEventListener('mousemove', this._globalListeners.move);
      window.removeEventListener('touchmove', this._globalListeners.move);
      window.removeEventListener('mouseup', this._globalListeners.up);
      window.removeEventListener('touchend', this._globalListeners.up);
      this._globalListeners = null;
    }
    if (this._hostKeydown) {
      this.removeEventListener('keydown', this._hostKeydown);
      this._hostKeydown = null;
    }
    if (this.state) {
      this.state.auto = false;
      this.state.urls = [];
      this.state.imgs.fill(null);
    }
    for (const task of this._loadQueue || []) task.resolve(null);
    this._loadPromises?.clear();
    this._failedFrames?.clear();
    this._loadQueue = [];
    this._activeLoads = 0;
    this._tex?.dispose();
    this._mesh?.geometry?.dispose();
    this._mesh?.material?.dispose();
    if (this._renderer) {
      this._renderer.renderLists?.dispose();
      this._renderer.dispose();
      this._renderer.forceContextLoss();
      this._renderer = null;
    }
    this._tex = this._mesh = this._scene = this._cam = null;
    this._fit = this._renderLoop = null;
    this._hsEls = this._dotEls = null;
    this._rendererWidth = this._rendererHeight = 0;
    this._renderDirty = false;
    this.manifest = null;
  }

  /* ------------------------------------------------------------------ boot */
  async #boot(generation) {
    let urls = [];
    const attr = this.getAttribute('frames');
    if (attr) { try { urls = JSON.parse(attr); } catch (e) {} }
    if (!Array.isArray(urls) || !urls.length) {
      const vin = (this.getAttribute('vin') || '').trim();
      if (!/^[A-Za-z0-9_-]{1,32}$/.test(vin)) {
        this.#fail('Invalid vehicle identifier.'); return;
      }
      try {
        const response = await fetch(`/api/viewer/${encodeURIComponent(vin)}/manifest`, {
          credentials: 'same-origin',
        });
        if (!response.ok) throw new Error('Manifest unavailable');
        const manifest = await response.json();
        urls = manifest.spin_frames || [];
        this.manifest = manifest;
      } catch (e) {}
    }
    if (!this.isConnected || generation !== this._generation) return;
    if (!Array.isArray(urls) || !urls.length) { this.#fail('No 360° frames for this vehicle.'); return; }
    if (urls.length > 120) { this.#fail('This spin has too many frames to display safely.'); return; }

    this.state.urls = urls.map(source => this.#safeFrameUrl(source));
    this.state.urls.forEach((url, index) => { if (!url) this._failedFrames.add(index); });
    if (!this.state.urls.some(Boolean)) { this.#fail('No valid 360° frame URLs were found.'); return; }
    this.state.n = this.state.urls.length;
    this.state.imgs = new Array(this.state.n).fill(null);
    this.#three();
    this.#dots();
    this.#bind();

    const first = await this.#preload(this.state.urls);
    if (!this.isConnected || generation !== this._generation) return;
    if (first < 0) { this.#fail('Frames failed to load.'); return; }
    this.#show(first);
    this.state.ready = true;
    this.$('canvas').classList.add('ready');
    this.$('.load').classList.add('gone');
    this.$('.bar').classList.add('on');
    this.#intro();
    this.dispatchEvent(new CustomEvent('lotvue:spin-ready', { detail: { frames: this.state.n } }));
  }

  #safeFrameUrl(source) {
    if (typeof source !== 'string' || !source || source.length > 2048) return null;
    try {
      const url = new URL(source, document.baseURI);
      if (!['http:', 'https:'].includes(url.protocol)) return null;
      if (url.origin !== window.location.origin && url.protocol !== 'https:') return null;
      return url.href;
    } catch (e) {
      return null;
    }
  }

  #fail(msg) {
    this.$('.load').classList.add('gone');
    const d = document.createElement('div');
    d.className = 'err'; d.textContent = msg;
    this.shadowRoot.appendChild(d);
  }

  /* -------------------------------------------------------------- preload */
  async #preload(urls) {
    const ring = this.$('.ring .fg'), pct = this.$('.pct'), C = 163.4;
    pct.textContent = 'Loading';
    // Reveal the viewer as soon as one frame is decoded. Adjacent frames are
    // filled by the bounded rolling cache while the buyer interacts.
    const firstCandidates = [...Array(Math.min(urls.length, 4)).keys()];
    for (const index of firstCandidates) {
      if (!urls[index]) continue;
      const image = await this.#loadFrame(index, true);
      if (image) {
        ring.style.strokeDashoffset = '0';
        pct.textContent = '100%';
        return index;
      }
    }
    return -1;
  }

  #loadFrame(index, priority = false) {
    const s = this.state;
    if (index < 0 || index >= s.n || !s.urls[index] || this._failedFrames.has(index)) {
      return Promise.resolve(null);
    }
    if (s.imgs[index]) return Promise.resolve(s.imgs[index]);
    const existing = this._loadPromises.get(index);
    if (existing) return existing.promise;

    let resolveTask;
    const promise = new Promise(resolve => { resolveTask = resolve; });
    const task = { index, generation: this._generation, resolve: resolveTask, started: false };
    this._loadPromises.set(index, { promise, task });
    if (priority) this._loadQueue.unshift(task);
    else this._loadQueue.push(task);
    this.#pumpFrameLoads();
    return promise;
  }

  #pumpFrameLoads() {
    const maxConcurrent = 4;
    while (this._connected && this._activeLoads < maxConcurrent && this._loadQueue.length) {
      const task = this._loadQueue.shift();
      const current = this._loadPromises.get(task.index);
      if (task.generation !== this._generation || !current || current.task !== task) {
        task.resolve(null);
        continue;
      }
      task.started = true;
      this._activeLoads++;
      const image = new Image();
      image.decoding = 'async';
      image.crossOrigin = 'anonymous';
      let settled = false;
      const finish = async loadedImage => {
        if (settled) return;
        settled = true;
        if (task.generation !== this._generation) {
          task.resolve(null);
          return;
        }
        this._activeLoads = Math.max(0, this._activeLoads - 1);
        if (loadedImage && loadedImage.decode) {
          try { await loadedImage.decode(); } catch (e) {}
        }
        const pending = this._loadPromises.get(task.index);
        if (pending?.task === task) this._loadPromises.delete(task.index);
        const distance = Math.min(
          (task.index - this.state.i + this.state.n) % this.state.n,
          (this.state.i - task.index + this.state.n) % this.state.n,
        );
        const useful = task.index === this.state.i || distance <= 2;
        if (loadedImage && useful) this.state.imgs[task.index] = loadedImage;
        else if (!loadedImage) this._failedFrames.add(task.index);
        const result = loadedImage && useful ? loadedImage : null;
        task.resolve(result);
        if (result && task.index === this.state.i) this.#applyFrame(task.index, result);
        else if (!result && task.index === this.state.i) this.#fallbackToNearest(task.index);
        this.#preloadWindow(this.state.i);
        this.#pumpFrameLoads();
      };
      image.onload = () => finish(image);
      image.onerror = () => finish(null);
      image.src = this.state.urls[task.index];
    }
  }

  #preloadWindow(center) {
    const s = this.state;
    if (!s.n || !this._connected) return;
    this.#trimFrameCache(center);
    if (!s.imgs[center] && !this._failedFrames.has(center)) this.#loadFrame(center, true);
    for (const offset of [1, -1, 2, -2]) {
      const index = (center + offset + s.n) % s.n;
      if (!s.imgs[index] && !this._failedFrames.has(index)) this.#loadFrame(index);
    }
  }

  #trimFrameCache(center) {
    const s = this.state;
    for (let index = 0; index < s.imgs.length; index++) {
      if (!s.imgs[index] || index === center) continue;
      const distance = Math.min(
        (index - center + s.n) % s.n,
        (center - index + s.n) % s.n,
      );
      if (distance > 2) s.imgs[index] = null;
    }
    this._loadQueue = this._loadQueue.filter(task => {
      const distance = Math.min(
        (task.index - center + s.n) % s.n,
        (center - task.index + s.n) % s.n,
      );
      if (distance <= 2 || task.index === center) return true;
      const pending = this._loadPromises.get(task.index);
      if (pending?.task === task) this._loadPromises.delete(task.index);
      task.resolve(null);
      return false;
    });
  }

  #fallbackToNearest(index) {
    const s = this.state;
    for (let distance = 1; distance < s.n; distance++) {
      for (const candidate of [(index + distance) % s.n, (index - distance + s.n) % s.n]) {
        if (s.imgs[candidate]) {
          s.i = candidate;
          this.#applyFrame(candidate, s.imgs[candidate]);
          return;
        }
      }
    }
  }

  #applyFrame(index, image) {
    const s = this.state;
    s.i = index;
    this._tex.image = image;
    this._tex.needsUpdate = true;
    this._fit();
    const dots = this._dotEls;
    if (dots) {
      const dotIndex = Math.round((index / s.n) * (dots.length - 1));
      dots.forEach((dot, i) => dot.classList.toggle('on', i === dotIndex));
    }
    this.#hotspots();
    this._requestRender();
  }

  /* ----------------------------------------------------------- three.js */
  #three() {
    const r = new THREE.WebGLRenderer({ antialias: false, alpha: true, powerPreference: 'low-power' });
    r.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
    this.shadowRoot.appendChild(r.domElement);
    const scene = new THREE.Scene();
    const cam = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 10);
    cam.position.z = 1;
    const tex = new THREE.Texture();
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.minFilter = THREE.LinearFilter;
    tex.magFilter = THREE.LinearFilter;
    tex.generateMipmaps = false;
    const mesh = new THREE.Mesh(
      new THREE.PlaneGeometry(1, 1),
      new THREE.MeshBasicMaterial({ map: tex, transparent: true }));
    scene.add(mesh);
    Object.assign(this, { _renderer: r, _scene: scene, _cam: cam, _tex: tex, _mesh: mesh });

    const fit = () => {
      if (!this._connected || !this._renderer) return;
      const w = this.clientWidth || 640, h = this.clientHeight || 400;
      if (w !== this._rendererWidth || h !== this._rendererHeight) {
        r.setSize(w, h, false);
        this._rendererWidth = w;
        this._rendererHeight = h;
      }
      const im = this.state.imgs[this.state.i] || this._tex.image;
      const ar = im ? im.width / im.height : 1.5;
      const va = w / h;
      let pw, ph;
      if (ar > va) { pw = 2; ph = 2 * va / ar; } else { ph = 2; pw = 2 * ar / va; }
      mesh.scale.set(pw, ph, 1);
      cam.left = -1; cam.right = 1; cam.top = 1; cam.bottom = -1;
      cam.updateProjectionMatrix();
      this._renderDirty = true;
    };
    this._fit = fit;
    this._resizeObserver = new ResizeObserver(fit);
    this._resizeObserver.observe(this);
    fit();

    this._renderLoop = () => {
      this._raf = null;
      if (!this._connected || !this._renderer) return;
      this._insideRender = true;
      const s = this.state;
      if (!s.dragging && Math.abs(s.vel) > 0.02) {
        s.acc = (s.acc || 0) + s.vel;
        while (Math.abs(s.acc) >= 1) {
          const step = Math.sign(s.acc);
          this.#show(s.i + step);
          s.acc -= step;
        }
        s.vel *= 0.94;
      } else if (!s.dragging && s.auto) {
        s.acc = (s.acc || 0) + 0.35;
        if (s.acc >= 1) { this.#show(s.i + 1); s.acc = 0; }
      }
      const moving = s.dragging || s.auto || Math.abs(s.vel) > 0.02;
      if (this._renderDirty || moving) {
        cam.zoom = s.zoom;
        cam.position.x = s.px; cam.position.y = s.py;
        cam.updateProjectionMatrix();
        r.render(scene, cam);
        this._renderDirty = false;
      }
      this._insideRender = false;
      if (moving || this._renderDirty) this._raf = requestAnimationFrame(this._renderLoop);
    };
    this._renderDirty = true;
    this._requestRender();
  }

  _requestRender() {
    this._renderDirty = true;
    if (this._connected && !this._insideRender && this._raf == null && this._renderLoop) {
      this._raf = requestAnimationFrame(this._renderLoop);
    }
  }

  /* ------------------------------------------------------------ frame swap */
  #show(k) {
    const s = this.state;
    if (!s.n) return;
    k = ((k % s.n) + s.n) % s.n;
    s.i = k;
    const image = s.imgs[k];
    if (image) this.#applyFrame(k, image);
    else {
      this.#preloadWindow(k);
      if (this._failedFrames.has(k)) this.#fallbackToNearest(k);
    }
  }

  #dots() {
    const wrap = this.$('.dots');
    const count = Math.min(this.state.n, 40);
    wrap.innerHTML = Array.from({ length: count }, () => '<i></i>').join('');
    this._dotEls = [...wrap.querySelectorAll('i')];
  }

  /* ------------------------------------------------------------- intro spin */
  #intro() {
    const s = this.state;
    s.auto = true;
    this.$('.play').style.opacity = .55;
    const stop = () => {
      s.auto = false;
      this.$('.play').style.opacity = 1;
      this._requestRender();
    };
    this._stopAuto = stop;
    this._requestRender();
    // one slow revolution then settle
    this._introTimer = setTimeout(() => { if (s.auto) stop(); }, (s.n / 0.35) * 16.7 + 400);
  }

  /* ------------------------------------------------------------------ input */
  #bind() {
    const cv = this.$('canvas'), s = this.state;
    const hint = this.$('.hint'), zt = this.$('.zoomtag');
    let last = null, moved = 0, pinch = null;
    const X = e => e.touches ? e.touches[0].clientX : e.clientX;
    const Y = e => e.touches ? e.touches[0].clientY : e.clientY;
    const killAuto = () => { this._stopAuto?.(); hint.classList.add('gone'); };
    const setZoom = z => {
      s.zoom = Math.max(1, Math.min(4, z));
      if (s.zoom === 1) { s.px = 0; s.py = 0; }
      zt.textContent = s.zoom.toFixed(1) + '×';
      zt.classList.toggle('on', s.zoom > 1.001);
      cv.classList.toggle('zoomed', s.zoom > 1.001);
      this._requestRender();
    };
    this._setZoom = setZoom;

    const down = e => {
      if (e.touches && e.touches.length === 2) {
        pinch = { d: Math.hypot(e.touches[0].clientX - e.touches[1].clientX,
                                e.touches[0].clientY - e.touches[1].clientY), z: s.zoom };
        return;
      }
      killAuto();
      s.dragging = true; s.vel = 0; moved = 0;
      s.panning = s.zoom > 1.001;
      last = { x: X(e), y: Y(e) };
      cv.classList.add('grabbing');
    };
    const move = e => {
      if (pinch && e.touches && e.touches.length === 2) {
        const d = Math.hypot(e.touches[0].clientX - e.touches[1].clientX,
                             e.touches[0].clientY - e.touches[1].clientY);
        setZoom(pinch.z * (d / pinch.d));
        e.preventDefault(); return;
      }
      if (!s.dragging || !last) return;
      const x = X(e), y = Y(e), dx = x - last.x, dy = y - last.y;
      moved += Math.abs(dx) + Math.abs(dy);
      if (s.panning) {
        const lim = (s.zoom - 1);
        s.px = Math.max(-lim, Math.min(lim, s.px - dx / this.clientWidth * 2));
        s.py = Math.max(-lim, Math.min(lim, s.py + dy / this.clientHeight * 2));
      } else {
        // px-per-frame tuned so ~1.2 viewport widths = full revolution
        const per = (this.clientWidth * 1.2) / s.n;
        const step = -dx / per;
        s.acc = (s.acc || 0) + step;
        while (Math.abs(s.acc) >= 1) { const t = Math.sign(s.acc); this.#show(s.i + t); s.acc -= t; }
        s.vel = step * 0.55;
      }
      last = { x, y };
      this._requestRender();
      e.preventDefault();
    };
    const up = () => {
      if (!s.dragging) { pinch = null; return; }
      s.dragging = false; pinch = null; last = null;
      cv.classList.remove('grabbing');
      this._requestRender();
    };

    cv.addEventListener('mousedown', down);
    cv.addEventListener('touchstart', down, { passive: false });
    window.addEventListener('mousemove', move);
    window.addEventListener('touchmove', move, { passive: false });
    window.addEventListener('mouseup', up);
    window.addEventListener('touchend', up);
    this._globalListeners = { move, up };

    cv.addEventListener('wheel', e => {
      e.preventDefault(); killAuto();
      setZoom(s.zoom * (e.deltaY < 0 ? 1.15 : 1 / 1.15));
    }, { passive: false });

    cv.addEventListener('dblclick', () => { killAuto(); setZoom(s.zoom > 1.5 ? 1 : 2.4); });

    this.tabIndex = 0;
    this._hostKeydown = e => {
      if (e.key === 'ArrowLeft') { killAuto(); this.#show(s.i - 1); }
      if (e.key === 'ArrowRight') { killAuto(); this.#show(s.i + 1); }
      if (e.key === '+' || e.key === '=') setZoom(s.zoom * 1.2);
      if (e.key === '-') setZoom(s.zoom / 1.2);
      if (e.key === '0') setZoom(1);
    };
    this.addEventListener('keydown', this._hostKeydown);

    this.$('.zin').onclick = () => { killAuto(); setZoom(s.zoom * 1.35); };
    this.$('.zout').onclick = () => setZoom(s.zoom / 1.35);
    this.$('.play').onclick = () => {
      hint.classList.add('gone');
      s.auto = !s.auto;
      this.$('.play').style.opacity = s.auto ? .55 : 1;
      this._requestRender();
    };
    const fullBtn = this.$('.full');
    fullBtn.onclick = () => {
      if (document.fullscreenElement) document.exitFullscreen();
      else this.requestFullscreen?.().then(() => this._fit());
    };
    if (!this.requestFullscreen) fullBtn.classList.add('hide');

    this._hintTimer = setTimeout(() => hint.classList.add('gone'), 6000);
  }

  /* --------------------------------------------------------------- hotspots */
  #hotspots() {
    const list = (this.manifest?.hotspots || []).filter(h => h.frame != null);
    if (!list.length) return;
    if (!this._hsEls) {
      const tip = this.$('.tip');
      this._hsEls = list.map(h => {
        const d = document.createElement('div');
        d.className = 'hs';
        d.style.left = (h.u * 100) + '%';
        d.style.top = (h.v * 100) + '%';
        d.onmouseenter = () => {
          tip.textContent = h.label + (h.body ? ' — ' + h.body : '');
          tip.style.display = 'block';
          tip.style.left = d.style.left;
          tip.style.top = `calc(${d.style.top} - 58px)`;
        };
        d.onmouseleave = () => tip.style.display = 'none';
        this.shadowRoot.appendChild(d);
        return d;
      });
    }
    const s = this.state, tol = Math.max(1, Math.round(s.n / 24));
    list.forEach((h, k) => {
      let d = Math.abs(h.frame - s.i);
      d = Math.min(d, s.n - d);
      this._hsEls[k].style.display = d <= tol ? 'block' : 'none';
    });
  }
}
customElements.define('lotvue-spin', LotVueSpin);
