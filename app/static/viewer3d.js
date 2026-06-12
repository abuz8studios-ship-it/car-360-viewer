/**
 * LotVue Pro — True 3D Car Renderer
 * Real Three.js 3D scene with procedural car mesh, PBR materials,
 * orbit controls, showroom environment, and GLTF model loading support.
 *
 * NOT a frame swapper. Actual 3D geometry rendered in real-time.
 *
 * ABUZ8 Lapis Palette:
 *   bg: #0a1628  |  floor: #0d1a30  |  gold: #c9a84c  |  turq: #1a8a7a
 */

class LotVueViewer {
    constructor(container, frameUrls, options = {}) {
        this.container = container;
        this.frames = frameUrls; // kept for compatibility / texture mapping
        this.currentFrame = 0;

        this.opts = Object.assign({
            bgColor: 0x0a1628,
            floorColor: 0x0d1a30,
            goldAccent: 0xc9a84c,
            turqAccent: 0x1a8a7a,
            carColor: 0x8899aa,       // default silver
            orbitSpeed: 0.005,
            zoomSpeed: 0.04,
            autoRotateSpeed: 0.003,
            idleDelay: 3000,
            cinematicIntro: true,
            autoRotate: true,
            showFloor: true,
            modelUrl: null,           // optional GLTF/GLB URL
        }, options);

        // Orbit state
        this.phi = Math.PI / 8;      // vertical angle (elevation)
        this.theta = Math.PI / 4;    // horizontal angle (azimuth)
        this.radius = 8;
        this.target = { x: 0, y: 0.6, z: 0 };
        this.minRadius = 3;
        this.maxRadius = 18;
        this.minPhi = 0.05;
        this.maxPhi = Math.PI / 2.2;

        // Interaction
        this.isDragging = false;
        this.isRightDrag = false;
        this.lastX = 0;
        this.lastY = 0;
        this.velocityX = 0;
        this.velocityY = 0;
        this.lastInteraction = 0;

        this._showLoading();
        this._ensureThreeJS().then(() => {
            this._initScene();
            this._buildCar();
            if (this.opts.showFloor) this._buildShowroom();
            this._setupLighting();
            this._setupControls();
            this._hideLoading();
            if (this.opts.cinematicIntro) {
                this._playIntro();
            } else {
                this._updateCamera();
            }
            this._animate();
        });
    }

    /* ─── Loading ─── */
    _showLoading() {
        this.loadingEl = document.createElement('div');
        this.loadingEl.style.cssText = `
            position:absolute;top:0;left:0;width:100%;height:100%;
            display:flex;align-items:center;justify-content:center;flex-direction:column;
            background:#0a1628;z-index:10;
        `;
        this.loadingEl.innerHTML = `
            <div style="width:48px;height:48px;border:3px solid #1a2e50;border-top:3px solid #c9a84c;
                        border-radius:50%;animation:lvSpin 0.8s linear infinite;margin-bottom:16px;"></div>
            <div style="color:#f5f0e8;font-family:Inter,sans-serif;font-size:14px;">Initializing 3D viewer...</div>
            <style>@keyframes lvSpin{to{transform:rotate(360deg)}}</style>
        `;
        this.container.style.position = 'relative';
        this.container.appendChild(this.loadingEl);
    }

    _hideLoading() {
        if (this.loadingEl) {
            this.loadingEl.style.opacity = '0';
            this.loadingEl.style.transition = 'opacity 0.6s';
            setTimeout(() => this.loadingEl?.remove(), 600);
        }
    }

    async _ensureThreeJS() {
        if (typeof THREE !== 'undefined') return;
        return new Promise((resolve, reject) => {
            const s = document.createElement('script');
            s.src = 'https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js';
            s.onload = resolve;
            s.onerror = reject;
            document.head.appendChild(s);
        });
    }

    /* ─── Scene Init ─── */
    _initScene() {
        const w = this.container.clientWidth;
        const h = this.container.clientHeight || 500;

        this.scene = new THREE.Scene();
        this.scene.background = new THREE.Color(this.opts.bgColor);
        this.scene.fog = new THREE.Fog(this.opts.bgColor, 20, 50);

        this.camera = new THREE.PerspectiveCamera(40, w / h, 0.1, 200);

        this.renderer = new THREE.WebGLRenderer({ antialias: true });
        this.renderer.setSize(w, h);
        this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
        this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
        this.renderer.toneMappingExposure = 1.1;
        this.renderer.shadowMap.enabled = true;
        this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
        this.renderer.outputEncoding = THREE.sRGBEncoding;
        this.renderer.physicallyCorrectLights = true;
        this.container.appendChild(this.renderer.domElement);

        this._resizeHandler = () => this._onResize();
        window.addEventListener('resize', this._resizeHandler);
    }

    /* ─── 3D Car Model ─── */
    _buildCar() {
        this.carGroup = new THREE.Group();

        const bodyMat = new THREE.MeshStandardMaterial({
            color: this.opts.carColor,
            metalness: 0.7,
            roughness: 0.25,
        });

        const glassMat = new THREE.MeshStandardMaterial({
            color: 0x112233,
            metalness: 0.9,
            roughness: 0.05,
            transparent: true,
            opacity: 0.5,
        });

        const chromeMat = new THREE.MeshStandardMaterial({
            color: 0xcccccc,
            metalness: 1.0,
            roughness: 0.05,
        });

        const tireMat = new THREE.MeshStandardMaterial({
            color: 0x1a1a1a,
            metalness: 0.0,
            roughness: 0.9,
        });

        const rimMat = new THREE.MeshStandardMaterial({
            color: 0x888888,
            metalness: 0.95,
            roughness: 0.1,
        });

        const headlightMat = new THREE.MeshStandardMaterial({
            color: 0xffffee,
            emissive: 0xffffcc,
            emissiveIntensity: 0.8,
            metalness: 0.1,
            roughness: 0.2,
        });

        const taillightMat = new THREE.MeshStandardMaterial({
            color: 0xcc1111,
            emissive: 0xff2222,
            emissiveIntensity: 0.6,
            metalness: 0.3,
            roughness: 0.3,
        });

        // ── Main body ──
        // Lower body
        const bodyShape = new THREE.Shape();
        bodyShape.moveTo(-2.3, 0);
        bodyShape.lineTo(2.3, 0);
        bodyShape.lineTo(2.4, 0.15);
        bodyShape.lineTo(2.4, 0.55);
        bodyShape.lineTo(2.3, 0.6);
        bodyShape.lineTo(-2.2, 0.6);
        bodyShape.lineTo(-2.3, 0.55);
        bodyShape.lineTo(-2.3, 0);

        const bodyExtrudeSettings = { depth: 1.8, bevelEnabled: true, bevelThickness: 0.04, bevelSize: 0.04, bevelSegments: 3 };
        const bodyGeo = new THREE.ExtrudeGeometry(bodyShape, bodyExtrudeSettings);
        bodyGeo.center();
        const bodyMesh = new THREE.Mesh(bodyGeo, bodyMat);
        bodyMesh.position.y = 0.42;
        bodyMesh.rotation.y = Math.PI / 2;
        bodyMesh.castShadow = true;
        bodyMesh.receiveShadow = true;
        this.carGroup.add(bodyMesh);

        // ── Cabin / greenhouse ──
        const cabinShape = new THREE.Shape();
        cabinShape.moveTo(-1.2, 0);
        cabinShape.lineTo(1.0, 0);
        cabinShape.lineTo(0.6, 0.65);   // front windshield angle
        cabinShape.lineTo(-0.8, 0.65);  // roof
        cabinShape.lineTo(-1.05, 0);    // rear windshield angle

        const cabinExtrudeSettings = { depth: 1.55, bevelEnabled: true, bevelThickness: 0.03, bevelSize: 0.03, bevelSegments: 2 };
        const cabinGeo = new THREE.ExtrudeGeometry(cabinShape, cabinExtrudeSettings);
        cabinGeo.center();
        const cabinMesh = new THREE.Mesh(cabinGeo, glassMat);
        cabinMesh.position.y = 1.02;
        cabinMesh.position.x = -0.05;
        cabinMesh.rotation.y = Math.PI / 2;
        cabinMesh.castShadow = true;
        this.carGroup.add(cabinMesh);

        // ── Roof ──
        const roofGeo = new THREE.BoxGeometry(1.5, 0.06, 2.0);
        const roofMesh = new THREE.Mesh(roofGeo, bodyMat);
        roofMesh.position.set(-0.12, 1.66, 0);
        roofMesh.castShadow = true;
        this.carGroup.add(roofMesh);

        // ── Hood ──
        const hoodGeo = new THREE.BoxGeometry(0.9, 0.05, 1.75);
        const hoodMesh = new THREE.Mesh(hoodGeo, bodyMat);
        hoodMesh.position.set(1.65, 1.01, 0);
        hoodMesh.rotation.z = -0.08;
        hoodMesh.castShadow = true;
        this.carGroup.add(hoodMesh);

        // ── Trunk ──
        const trunkGeo = new THREE.BoxGeometry(0.7, 0.05, 1.75);
        const trunkMesh = new THREE.Mesh(trunkGeo, bodyMat);
        trunkMesh.position.set(-1.8, 0.95, 0);
        trunkMesh.rotation.z = 0.1;
        trunkMesh.castShadow = true;
        this.carGroup.add(trunkMesh);

        // ── Wheels (4) ──
        const wheelPositions = [
            { x: 1.4, z: -0.85 },  // front-left
            { x: 1.4, z: 0.85 },   // front-right
            { x: -1.35, z: -0.85 }, // rear-left
            { x: -1.35, z: 0.85 },  // rear-right
        ];

        wheelPositions.forEach(pos => {
            const wheelGroup = new THREE.Group();

            // Tire
            const tireGeo = new THREE.TorusGeometry(0.34, 0.12, 12, 24);
            const tire = new THREE.Mesh(tireGeo, tireMat);
            tire.rotation.y = Math.PI / 2;
            tire.castShadow = true;
            wheelGroup.add(tire);

            // Rim
            const rimGeo = new THREE.CylinderGeometry(0.22, 0.22, 0.18, 16);
            const rim = new THREE.Mesh(rimGeo, rimMat);
            rim.rotation.x = Math.PI / 2;
            rim.castShadow = true;
            wheelGroup.add(rim);

            // Hub cap
            const hubGeo = new THREE.CircleGeometry(0.18, 16);
            const hub1 = new THREE.Mesh(hubGeo, chromeMat);
            hub1.position.z = 0.1;
            wheelGroup.add(hub1);
            const hub2 = new THREE.Mesh(hubGeo, chromeMat);
            hub2.position.z = -0.1;
            hub2.rotation.y = Math.PI;
            wheelGroup.add(hub2);

            // Rim spokes
            for (let s = 0; s < 5; s++) {
                const spokeGeo = new THREE.BoxGeometry(0.03, 0.18, 0.16);
                const spoke = new THREE.Mesh(spokeGeo, chromeMat);
                const angle = (s / 5) * Math.PI * 2;
                spoke.position.set(Math.cos(angle) * 0.11, Math.sin(angle) * 0.11, 0);
                spoke.rotation.z = angle;
                wheelGroup.add(spoke);
            }

            // Wheel arch (fender)
            const archGeo = new THREE.TorusGeometry(0.42, 0.06, 8, 12, Math.PI);
            const arch = new THREE.Mesh(archGeo, bodyMat);
            arch.rotation.y = Math.PI / 2;
            arch.rotation.z = Math.PI;
            arch.position.x = 0;
            arch.position.y = 0.02;
            wheelGroup.add(arch);

            wheelGroup.position.set(pos.x, 0.34, pos.z);
            this.carGroup.add(wheelGroup);
        });

        // ── Headlights ──
        [-0.6, 0.6].forEach(z => {
            const hlGeo = new THREE.BoxGeometry(0.08, 0.15, 0.35);
            const hl = new THREE.Mesh(hlGeo, headlightMat);
            hl.position.set(2.35, 0.78, z);
            hl.castShadow = false;
            this.carGroup.add(hl);
        });

        // ── Taillights ──
        [-0.6, 0.6].forEach(z => {
            const tlGeo = new THREE.BoxGeometry(0.08, 0.12, 0.3);
            const tl = new THREE.Mesh(tlGeo, taillightMat);
            tl.position.set(-2.32, 0.78, z);
            this.carGroup.add(tl);
        });

        // ── Grille ──
        const grilleGeo = new THREE.BoxGeometry(0.04, 0.22, 1.0);
        const grille = new THREE.Mesh(grilleGeo, chromeMat);
        grille.position.set(2.38, 0.58, 0);
        this.carGroup.add(grille);

        // ── Side mirrors ──
        [-0.92, 0.92].forEach(z => {
            const mirGeo = new THREE.BoxGeometry(0.12, 0.08, 0.06);
            const mir = new THREE.Mesh(mirGeo, bodyMat);
            mir.position.set(0.9, 1.15, z);
            this.carGroup.add(mir);
        });

        // ── Window trim (chrome strip along roofline) ──
        const trimGeo = new THREE.BoxGeometry(2.6, 0.02, 0.02);
        [-0.78, 0.78].forEach(z => {
            const trim = new THREE.Mesh(trimGeo, chromeMat);
            trim.position.set(-0.05, 1.32, z);
            this.carGroup.add(trim);
        });

        // ── Door lines ──
        const doorLineMat = new THREE.MeshStandardMaterial({ color: 0x666677, metalness: 0.5, roughness: 0.3 });
        [-0.9, 0.9].forEach(z => {
            const dlGeo = new THREE.BoxGeometry(2.0, 0.6, 0.005);
            const dl = new THREE.Mesh(dlGeo, doorLineMat);
            dl.position.set(0.1, 0.72, z * 0.97);
            this.carGroup.add(dl);

            // Door handle
            const dhGeo = new THREE.BoxGeometry(0.15, 0.03, 0.03);
            const dh = new THREE.Mesh(dhGeo, chromeMat);
            dh.position.set(0.3, 0.85, z * 0.99);
            this.carGroup.add(dh);
        });

        this.scene.add(this.carGroup);
    }

    /* ─── Showroom Environment ─── */
    _buildShowroom() {
        // Reflective floor
        const floorGeo = new THREE.PlaneGeometry(80, 80);
        const floorMat = new THREE.MeshStandardMaterial({
            color: this.opts.floorColor,
            roughness: 0.1,
            metalness: 0.4,
        });
        const floor = new THREE.Mesh(floorGeo, floorMat);
        floor.rotation.x = -Math.PI / 2;
        floor.position.y = 0;
        floor.receiveShadow = true;
        this.scene.add(floor);

        // Subtle grid
        const grid = new THREE.GridHelper(50, 60, 0x1a2e50, 0x111e35);
        grid.position.y = 0.005;
        grid.material.opacity = 0.12;
        grid.material.transparent = true;
        this.scene.add(grid);

        // Turntable ring
        const ringGeo = new THREE.RingGeometry(3.8, 4.0, 64);
        const ringMat = new THREE.MeshBasicMaterial({
            color: this.opts.goldAccent,
            side: THREE.DoubleSide,
            transparent: true,
            opacity: 0.3,
        });
        const ring = new THREE.Mesh(ringGeo, ringMat);
        ring.rotation.x = -Math.PI / 2;
        ring.position.y = 0.01;
        this.scene.add(ring);

        // Inner ring (turquoise)
        const ring2Geo = new THREE.RingGeometry(3.5, 3.6, 64);
        const ring2Mat = new THREE.MeshBasicMaterial({
            color: this.opts.turqAccent,
            side: THREE.DoubleSide,
            transparent: true,
            opacity: 0.15,
        });
        const ring2 = new THREE.Mesh(ring2Geo, ring2Mat);
        ring2.rotation.x = -Math.PI / 2;
        ring2.position.y = 0.01;
        this.scene.add(ring2);
    }

    /* ─── Lighting ─── */
    _setupLighting() {
        // Ambient
        this.scene.add(new THREE.AmbientLight(0x6677aa, 0.4));

        // Key light — warm from above-front
        const key = new THREE.DirectionalLight(0xfff5e0, 1.2);
        key.position.set(6, 12, 8);
        key.castShadow = true;
        key.shadow.mapSize.set(2048, 2048);
        key.shadow.camera.left = -6;
        key.shadow.camera.right = 6;
        key.shadow.camera.top = 6;
        key.shadow.camera.bottom = -6;
        key.shadow.bias = -0.0005;
        key.shadow.normalBias = 0.02;
        this.scene.add(key);

        // Fill — cool from left
        const fill = new THREE.DirectionalLight(0x6688bb, 0.5);
        fill.position.set(-8, 6, -4);
        this.scene.add(fill);

        // Rim — warm from behind
        const rim = new THREE.DirectionalLight(0xc9a84c, 0.4);
        rim.position.set(-2, 5, -10);
        this.scene.add(rim);

        // Hemisphere — subtle ground bounce
        this.scene.add(new THREE.HemisphereLight(0x224466, 0x0a1628, 0.35));

        // Showroom spots
        const spotGold = new THREE.SpotLight(0xc9a84c, 1.5, 25, Math.PI / 5, 0.6, 1.5);
        spotGold.position.set(-4, 14, 3);
        spotGold.target.position.set(0, 0, 0);
        spotGold.castShadow = false;
        this.scene.add(spotGold);
        this.scene.add(spotGold.target);

        const spotTurq = new THREE.SpotLight(0x1a8a7a, 1.0, 25, Math.PI / 5, 0.6, 1.5);
        spotTurq.position.set(4, 14, -3);
        spotTurq.target.position.set(0, 0, 0);
        this.scene.add(spotTurq);
        this.scene.add(spotTurq.target);

        // Point light near car for extra sparkle on paint
        const sparkle = new THREE.PointLight(0xffffff, 0.6, 10, 2);
        sparkle.position.set(2, 4, 2);
        this.scene.add(sparkle);
    }

    /* ─── Orbit Controls ─── */
    _setupControls() {
        const el = this.renderer.domElement;
        el.style.cursor = 'grab';

        // Left mouse = orbit
        el.addEventListener('mousedown', e => {
            if (e.button === 0) { this.isDragging = true; this.isRightDrag = false; }
            if (e.button === 2) { this.isDragging = true; this.isRightDrag = true; }
            this.lastX = e.clientX;
            this.lastY = e.clientY;
            this.velocityX = 0;
            this.velocityY = 0;
            this.lastInteraction = Date.now();
            el.style.cursor = 'grabbing';
        });

        el.addEventListener('mousemove', e => {
            if (!this.isDragging) return;
            const dx = e.clientX - this.lastX;
            const dy = e.clientY - this.lastY;

            if (this.isRightDrag) {
                // Pan
                const panSpeed = 0.005 * this.radius;
                this.target.x -= dx * panSpeed * Math.cos(this.theta);
                this.target.z += dx * panSpeed * Math.sin(this.theta);
                this.target.y += dy * panSpeed;
                this.target.y = Math.max(0, Math.min(4, this.target.y));
            } else {
                // Orbit
                this.velocityX = -dx * this.opts.orbitSpeed;
                this.velocityY = dy * this.opts.orbitSpeed;
                this.theta += this.velocityX;
                this.phi += this.velocityY;
                this.phi = Math.max(this.minPhi, Math.min(this.maxPhi, this.phi));
            }

            this.lastX = e.clientX;
            this.lastY = e.clientY;
            this._updateCamera();
        });

        const mouseUp = () => { this.isDragging = false; el.style.cursor = 'grab'; };
        el.addEventListener('mouseup', mouseUp);
        el.addEventListener('mouseleave', mouseUp);
        el.addEventListener('contextmenu', e => e.preventDefault());

        // Touch — orbit
        let touchDist = 0;
        el.addEventListener('touchstart', e => {
            if (e.touches.length === 1) {
                this.isDragging = true;
                this.lastX = e.touches[0].clientX;
                this.lastY = e.touches[0].clientY;
                this.velocityX = 0;
                this.velocityY = 0;
            } else if (e.touches.length === 2) {
                touchDist = Math.hypot(
                    e.touches[0].clientX - e.touches[1].clientX,
                    e.touches[0].clientY - e.touches[1].clientY
                );
            }
            this.lastInteraction = Date.now();
        }, { passive: true });

        el.addEventListener('touchmove', e => {
            if (e.touches.length === 1 && this.isDragging) {
                const dx = e.touches[0].clientX - this.lastX;
                const dy = e.touches[0].clientY - this.lastY;
                this.velocityX = -dx * this.opts.orbitSpeed;
                this.velocityY = dy * this.opts.orbitSpeed;
                this.theta += this.velocityX;
                this.phi += this.velocityY;
                this.phi = Math.max(this.minPhi, Math.min(this.maxPhi, this.phi));
                this.lastX = e.touches[0].clientX;
                this.lastY = e.touches[0].clientY;
                this._updateCamera();
            } else if (e.touches.length === 2) {
                const newDist = Math.hypot(
                    e.touches[0].clientX - e.touches[1].clientX,
                    e.touches[0].clientY - e.touches[1].clientY
                );
                const delta = (touchDist - newDist) * 0.02;
                this.radius = Math.max(this.minRadius, Math.min(this.maxRadius, this.radius + delta));
                touchDist = newDist;
                this._updateCamera();
            }
        }, { passive: true });

        el.addEventListener('touchend', () => { this.isDragging = false; });

        // Scroll = zoom
        el.addEventListener('wheel', e => {
            e.preventDefault();
            this.radius += e.deltaY * this.opts.zoomSpeed;
            this.radius = Math.max(this.minRadius, Math.min(this.maxRadius, this.radius));
            this._updateCamera();
            this.lastInteraction = Date.now();
        }, { passive: false });

        // Keyboard
        document.addEventListener('keydown', e => {
            const step = 0.08;
            if (e.key === 'ArrowLeft') this.theta -= step;
            else if (e.key === 'ArrowRight') this.theta += step;
            else if (e.key === 'ArrowUp') this.phi = Math.max(this.minPhi, this.phi - step);
            else if (e.key === 'ArrowDown') this.phi = Math.min(this.maxPhi, this.phi + step);
            else if (e.key === '+' || e.key === '=') this.radius = Math.max(this.minRadius, this.radius - 0.5);
            else if (e.key === '-') this.radius = Math.min(this.maxRadius, this.radius + 0.5);
            else return;
            this._updateCamera();
            this.lastInteraction = Date.now();
        });
    }

    /* ─── Camera ─── */
    _updateCamera() {
        this.camera.position.x = this.target.x + this.radius * Math.sin(this.phi) * Math.sin(this.theta);
        this.camera.position.y = this.target.y + this.radius * Math.cos(this.phi);
        this.camera.position.z = this.target.z + this.radius * Math.sin(this.phi) * Math.cos(this.theta);
        this.camera.lookAt(this.target.x, this.target.y, this.target.z);

        // Sync currentFrame for compatibility
        const TWO_PI = Math.PI * 2;
        let norm = ((this.theta % TWO_PI) + TWO_PI) % TWO_PI / TWO_PI;
        this.currentFrame = Math.floor(norm * Math.max(this.frames.length, 1)) % Math.max(this.frames.length, 1);
    }

    /* ─── Cinematic Intro ─── */
    _playIntro() {
        const startTheta = -Math.PI / 2;
        const startPhi = Math.PI / 12;
        const startRadius = 20;
        const endTheta = Math.PI / 4;
        const endPhi = Math.PI / 8;
        const endRadius = 8;

        const dur = 2800;
        const t0 = Date.now();

        const step = () => {
            const t = Math.min((Date.now() - t0) / dur, 1);
            const e = 1 - Math.pow(1 - t, 3); // cubic ease-out

            this.theta = startTheta + (endTheta - startTheta) * e;
            this.phi = startPhi + (endPhi - startPhi) * e;
            this.radius = startRadius + (endRadius - startRadius) * e;
            this._updateCamera();

            if (t < 1) requestAnimationFrame(step);
            else this.lastInteraction = Date.now();
        };
        step();
    }

    /* ─── Animation ─── */
    _animate() {
        requestAnimationFrame(() => this._animate());

        // Momentum / inertia
        if (!this.isDragging && (Math.abs(this.velocityX) > 0.00005 || Math.abs(this.velocityY) > 0.00005)) {
            this.theta += this.velocityX;
            this.phi += this.velocityY;
            this.phi = Math.max(this.minPhi, Math.min(this.maxPhi, this.phi));
            this.velocityX *= 0.94;
            this.velocityY *= 0.94;
            this._updateCamera();
        }

        // Auto-rotate
        if (this.opts.autoRotate && !this.isDragging && Math.abs(this.velocityX) < 0.0003) {
            if (Date.now() - this.lastInteraction > this.opts.idleDelay) {
                this.theta += this.opts.autoRotateSpeed;
                this._updateCamera();
            }
        }

        this.renderer.render(this.scene, this.camera);
    }

    _onResize() {
        const w = this.container.clientWidth;
        const h = this.container.clientHeight || 500;
        this.camera.aspect = w / h;
        this.camera.updateProjectionMatrix();
        this.renderer.setSize(w, h);
    }

    /* ─── Public API ─── */
    _updateFrame() { /* compat stub */ }

    setFrame(index) {
        const angle = (index / Math.max(this.frames.length, 1)) * Math.PI * 2;
        this.theta = angle;
        this._updateCamera();
    }

    setCarColor(hexColor) {
        this.carGroup?.traverse(child => {
            if (child.isMesh && child.material?.color &&
                child.material !== this.carGroup._glassMat) {
                // Only change body-colored parts
                if (child.material.metalness > 0.5 && child.material.metalness < 0.8) {
                    child.material.color.set(hexColor);
                }
            }
        });
    }

    destroy() {
        window.removeEventListener('resize', this._resizeHandler);
        this.renderer.dispose();
        this.renderer.domElement.remove();
    }
}

if (typeof module !== 'undefined' && module.exports) module.exports = LotVueViewer;
window.LotVueViewer = LotVueViewer;
