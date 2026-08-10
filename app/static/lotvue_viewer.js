/* ============================================================================
   <lotvue-viewer> — LotVue Pro unified 3D vehicle viewer (P2 deliverable)
   One component, three render tiers behind one API:
     tier "spin"   — photo-360 frame scrubber (every car, zero GPU)
     tier "splat"  — Gaussian splat orbit (hero cars, true 3D)
     tier "glb"    — GLB/PBR mesh w/ paint swap + HDRI env (AR-ready tier)
   Plus: interior equirect panorama, hotspot layer, AI-BDC dock hook.

   Usage:
     <lotvue-viewer vin="1C4RJFBG..." tier="auto"></lotvue-viewer>
   Data contract (all optional, resolved from /api/viewer/{vin}/manifest):
     { spin_frames:[urls], splat_url:"...ply", glb_url:"...glb",
       interior_url:"...jpg", hotspots:[{pos:[x,y,z]|frame,u,v,label,body}],
       paint:["#0b1f3a", ...], dealer:{name,logo} }
   ========================================================================== */
import * as THREE from 'https://cdn.jsdelivr.net/npm/three@0.164.1/build/three.module.js';
import { OrbitControls }   from 'https://cdn.jsdelivr.net/npm/three@0.164.1/examples/jsm/controls/OrbitControls.js';
import { GLTFLoader }      from 'https://cdn.jsdelivr.net/npm/three@0.164.1/examples/jsm/loaders/GLTFLoader.js';
import { DRACOLoader }     from 'https://cdn.jsdelivr.net/npm/three@0.164.1/examples/jsm/loaders/DRACOLoader.js';
import { RoomEnvironment } from 'https://cdn.jsdelivr.net/npm/three@0.164.1/examples/jsm/environments/RoomEnvironment.js';

const SPLAT_LIB = 'https://cdn.jsdelivr.net/npm/@mkkellogg/gaussian-splats-3d@0.4.7/build/gaussian-splats-3d.module.js';

const TPL = `
<style>
  :host{display:block;position:relative;width:100%;height:100%;min-height:320px;
        background:radial-gradient(120% 90% at 50% 0%,#16264d 0%,#0b152e 70%,#081024 100%);
        border-radius:12px;overflow:hidden;font-family:-apple-system,Segoe UI,Roboto,sans-serif;
        touch-action:none;user-select:none}
  canvas,img.spin{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;display:block}
  .hud{position:absolute;left:0;right:0;bottom:0;display:flex;gap:8px;align-items:center;
       padding:10px 14px;background:linear-gradient(transparent,rgba(5,10,25,.82));z-index:5}
  .hud button{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.18);color:#dfe8f8;
       border-radius:8px;padding:6px 12px;font-size:12px;cursor:pointer;letter-spacing:.3px}
  .hud button.on{background:rgba(90,140,255,.28);border-color:rgba(120,165,255,.55)}
  .hud .sp{flex:1}
  .paint{display:flex;gap:6px}
  .paint b{width:20px;height:20px;border-radius:50%;border:2px solid rgba(255,255,255,.35);cursor:pointer;display:block}
  .hs{position:absolute;width:26px;height:26px;margin:-13px 0 0 -13px;border-radius:50%;z-index:4;
      background:rgba(90,140,255,.25);border:2px solid rgba(140,180,255,.9);cursor:pointer;
      animation:pulse 2.2s infinite}
  @keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(120,165,255,.5)}50%{box-shadow:0 0 0 9px rgba(120,165,255,0)}}
  .tip{position:absolute;z-index:6;max-width:240px;background:rgba(10,18,40,.95);border:1px solid rgba(120,165,255,.4);
      color:#e6eeff;border-radius:10px;padding:10px 12px;font-size:12.5px;line-height:1.5;pointer-events:none;display:none}
  .load{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#8fa8d8;
      font-size:13px;letter-spacing:.5px;z-index:3}
  .badge{position:absolute;top:10px;right:12px;z-index:5;font-size:10px;letter-spacing:1.2px;color:#7d95c9;
      background:rgba(8,14,32,.6);padding:3px 9px;border-radius:99px;border:1px solid rgba(125,150,200,.25)}
</style>
<div class="load">LOADING VEHICLE…</div>
<div class="badge"></div>
<div class="hud" style="display:none">
  <button data-m="exterior">EXTERIOR</button>
  <button data-m="interior">INTERIOR</button>
  <span class="sp"></span>
  <div class="paint"></div>
</div>
<div class="tip"></div>`;

export class LotVueViewer extends HTMLElement {
  static observedAttributes = ['vin', 'tier'];
  #three = null; #spin = null; #raf = 0; #mode = 'exterior';

  connectedCallback() {
    this.attachShadow({ mode: 'open' }).innerHTML = TPL;
    this.#boot();
  }
  disconnectedCallback() { cancelAnimationFrame(this.#raf); this.#three?.renderer?.dispose(); }

  async #boot() {
    const vin = this.getAttribute('vin');
    let man = {};
    try { man = await (await fetch(`/api/viewer/${vin}/manifest`)).json(); } catch (e) {}
    this.manifest = man;
    const want = this.getAttribute('tier') || 'auto';
    const tier = want !== 'auto' ? want
      : man.splat_url ? 'splat' : man.glb_url ? 'glb' : 'spin';
    this.shadowRoot.querySelector('.badge').textContent =
      { splat: 'TRUE 3D', glb: '3D · AR READY', spin: '360°' }[tier] || '360°';
    try {
      if (tier === 'splat' && man.splat_url)      await this.#bootSplat(man.splat_url);
      else if (tier === 'glb' && man.glb_url)     await this.#bootGLB(man.glb_url, man.paint);
      else                                        await this.#bootSpin(man.spin_frames || []);
    } catch (e) {
      console.warn('[lotvue-viewer] tier failed, falling back to spin:', e);
      await this.#bootSpin(man.spin_frames || []);
    }
    this.#hud(man);
    this.shadowRoot.querySelector('.load').style.display = 'none';
    this.dispatchEvent(new CustomEvent('lotvue:ready', { detail: { vin, tier } }));
  }

  /* ---------- shared three.js scaffolding (glb + interior) ---------- */
  #scene() {
    const r = this.shadowRoot;
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    r.appendChild(renderer.domElement);
    const scene = new THREE.Scene();
    const cam = new THREE.PerspectiveCamera(45, 1, 0.05, 200);
    cam.position.set(4.2, 1.6, 5.2);
    const controls = new OrbitControls(cam, renderer.domElement);
    controls.enableDamping = true; controls.dampingFactor = 0.06;
    controls.minDistance = 2.2; controls.maxDistance = 12;
    controls.maxPolarAngle = Math.PI * 0.52;
    const pm = new THREE.PMREMGenerator(renderer);
    scene.environment = pm.fromScene(new RoomEnvironment(renderer), 0.04).texture;
    const key = new THREE.DirectionalLight(0xffffff, 1.4); key.position.set(5, 8, 4); scene.add(key);
    scene.add(new THREE.AmbientLight(0x8899bb, 0.5));
    // ground: soft contact shadow disc
    const g = new THREE.Mesh(
      new THREE.CircleGeometry(3.4, 48),
      new THREE.MeshBasicMaterial({ color: 0x000000, transparent: true, opacity: 0.32 }));
    g.rotation.x = -Math.PI / 2; g.position.y = 0.001; scene.add(g);
    const resize = () => {
      const w = this.clientWidth || 640, h = this.clientHeight || 400;
      renderer.setSize(w, h); cam.aspect = w / h; cam.updateProjectionMatrix();
    };
    new ResizeObserver(resize).observe(this); resize();
    this.#three = { renderer, scene, cam, controls };
    const loop = () => { this.#raf = requestAnimationFrame(loop); controls.update(); renderer.render(scene, cam); };
    loop();
    return this.#three;
  }

  /* ---------- tier: GLB / PBR ---------- */
  async #bootGLB(url, paints) {
    const { scene, cam, controls } = this.#scene();
    const dl = new DRACOLoader().setDecoderPath('https://cdn.jsdelivr.net/npm/three@0.164.1/examples/jsm/libs/draco/');
    const gltf = await new GLTFLoader().setDRACOLoader(dl).loadAsync(url);
    const car = gltf.scene;
    // normalize: center + scale to ~4.6m footprint
    const box = new THREE.Box3().setFromObject(car);
    const size = box.getSize(new THREE.Vector3()), ctr = box.getCenter(new THREE.Vector3());
    const s = 4.6 / Math.max(size.x, size.z);
    car.scale.setScalar(s);
    car.position.sub(ctr.multiplyScalar(s)); car.position.y = (size.y * s) / 2;
    scene.add(car);
    controls.target.set(0, size.y * s * 0.42, 0);
    cam.lookAt(controls.target);
    // collect body-paint materials (largest metallic surfaces heuristic + name match)
    this.paintTargets = [];
    car.traverse(o => {
      if (o.isMesh && o.material) {
        const m = o.material, n = (m.name + o.name).toLowerCase();
        if (/body|paint|carpaint|exterior|shell/.test(n) || (m.metalness > 0.4 && !m.map)) {
          m.metalness = Math.max(m.metalness ?? 0, 0.75);
          m.roughness = Math.min(m.roughness ?? 1, 0.28);
          m.envMapIntensity = 1.15;
          this.paintTargets.push(m);
        }
      }
    });
    this.setPaint = hex => this.paintTargets.forEach(m => m.color.set(hex));
    this.#hotspots3D();
  }

  /* ---------- tier: Gaussian splat ---------- */
  async #bootSplat(url) {
    const GS = await import(SPLAT_LIB);
    const w = this.clientWidth || 640, h = this.clientHeight || 400;
    const viewer = new GS.Viewer({
      rootElement: this.shadowRoot, useBuiltInControls: true,
      cameraUp: [0, 1, 0], initialCameraPosition: [3.6, 1.8, 4.4],
      initialCameraLookAt: [0, 0.7, 0], sharedMemoryForWorkers: false,
    });
    await viewer.addSplatScene(url, { showLoadingUI: false, splatAlphaRemovalThreshold: 5 });
    viewer.start();
    this.splat = viewer;
  }

  /* ---------- tier: photo 360 spin ---------- */
  async #bootSpin(frames) {
    if (!frames.length) {
      const vin = this.getAttribute('vin');
      try {
        const j = await (await fetch(`/api/vehicles/${vin}/spin`)).json();
        frames = j.frames || j.spin_frames || [];
      } catch (e) {}
    }
    const img = document.createElement('img');
    img.className = 'spin'; img.draggable = false;
    this.shadowRoot.appendChild(img);
    let i = 0; const n = frames.length || 1;
    const show = k => { i = ((k % n) + n) % n; img.src = frames[i]; };
    frames.slice(0, 6).forEach(u => { new Image().src = u; });   // prime
    show(0);
    let down = null;
    const px = e => (e.touches ? e.touches[0].clientX : e.clientX);
    this.addEventListener('pointerdown', e => { down = { x: px(e), i }; });
    addEventListener('pointermove', e => {
      if (!down) return;
      show(down.i + Math.round((px(e) - down.x) / -(this.clientWidth / n)));
    });
    addEventListener('pointerup', () => down = null);
    // idle auto-rotate
    let idle = setInterval(() => { if (!down && n > 1) show(i + 1); }, 130);
    this.addEventListener('pointerdown', () => { clearInterval(idle); idle = null; }, { once: true });
    this.#spin = { show, get frame() { return i; }, count: n };
    this.#hotspots2D();
  }

  /* ---------- interior panorama (any tier) ---------- */
  async #interior(url) {
    cancelAnimationFrame(this.#raf);
    this.shadowRoot.querySelectorAll('canvas, img.spin, .hs').forEach(e => e.remove());
    const { renderer, scene, cam, controls } = this.#scene();
    controls.minDistance = 0.01; controls.maxDistance = 0.02; controls.rotateSpeed = -0.4;
    const tex = await new THREE.TextureLoader().loadAsync(url);
    tex.colorSpace = THREE.SRGBColorSpace; tex.mapping = THREE.EquirectangularReflectionMapping;
    const sph = new THREE.Mesh(
      new THREE.SphereGeometry(8, 60, 40),
      new THREE.MeshBasicMaterial({ map: tex, side: THREE.BackSide }));
    scene.add(sph);
    cam.position.set(0, 0, 0.015); controls.target.set(0, 0, 0);
  }

  /* ---------- hotspots ---------- */
  #hotspots3D() {
    const hs = (this.manifest.hotspots || []).filter(h => h.pos);
    const tip = this.shadowRoot.querySelector('.tip');
    const els = hs.map(h => {
      const d = document.createElement('div'); d.className = 'hs';
      d.onmouseenter = () => { tip.textContent = h.label + (h.body ? ' — ' + h.body : ''); tip.style.display = 'block'; };
      d.onmouseleave = () => tip.style.display = 'none';
      this.shadowRoot.appendChild(d); return d;
    });
    const v = new THREE.Vector3();
    const tick = () => {
      if (!this.#three) return;
      const { cam } = this.#three;
      hs.forEach((h, k) => {
        v.set(...h.pos).project(cam);
        const el = els[k];
        el.style.left = ((v.x * 0.5 + 0.5) * this.clientWidth) + 'px';
        el.style.top = ((-v.y * 0.5 + 0.5) * this.clientHeight) + 'px';
        el.style.display = v.z < 1 ? 'block' : 'none';
        tip.style.left = el.style.left; tip.style.top = (parseFloat(el.style.top) - 60) + 'px';
      });
      requestAnimationFrame(tick);
    };
    tick();
  }
  #hotspots2D() {
    const hs = (this.manifest.hotspots || []).filter(h => h.frame != null);
    const tip = this.shadowRoot.querySelector('.tip');
    hs.forEach(h => {
      const d = document.createElement('div'); d.className = 'hs';
      d.style.left = (h.u * 100) + '%'; d.style.top = (h.v * 100) + '%';
      d.onmouseenter = () => { tip.textContent = h.label + (h.body ? ' — ' + h.body : ''); tip.style.display = 'block'; tip.style.left = d.style.left; tip.style.top = `calc(${d.style.top} - 60px)`; };
      d.onmouseleave = () => tip.style.display = 'none';
      this.shadowRoot.appendChild(d);
      const vis = () => d.style.display = Math.abs(this.#spin.frame - h.frame) <= 2 ? 'block' : 'none';
      setInterval(vis, 120); vis();
    });
  }

  /* ---------- HUD ---------- */
  #hud(man) {
    const hud = this.shadowRoot.querySelector('.hud');
    hud.style.display = 'flex';
    const [bx, bi] = hud.querySelectorAll('button');
    bx.classList.add('on');
    bi.style.display = man.interior_url ? '' : 'none';
    bi.onclick = () => { bi.classList.add('on'); bx.classList.remove('on'); this.#interior(man.interior_url); };
    bx.onclick = () => location.reload ? this.#reboot() : null;
    const pd = hud.querySelector('.paint');
    (man.paint || []).forEach(hex => {
      const b = document.createElement('b'); b.style.background = hex;
      b.onclick = () => this.setPaint && this.setPaint(hex);
      pd.appendChild(b);
    });
  }
  #reboot() {
    cancelAnimationFrame(this.#raf);
    this.shadowRoot.innerHTML = TPL; this.#three = null; this.#boot();
  }
}
customElements.define('lotvue-viewer', LotVueViewer);
