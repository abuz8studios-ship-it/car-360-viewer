// LOTVUE PRO — Interior 360° Panorama Viewer
// Equirectangular projection + Pannellum integration
// GLO3D Competitor Feature

class LotVueInteriorPanorama {
    constructor(containerId) {
        this.container = document.getElementById(containerId);
        this.panorama = null;
        this.currentScene = null;
        this.scenes = [];
        this.hotspots = [];
        
        this.init();
    }
    
    init() {
        this.setupContainer();
        this.setupControls();
    }
    
    setupContainer() {
        this.container.classList.add('lotvue-panorama-container');
        this.container.innerHTML = `
            <div class="panorama-wrapper">
                <canvas id="panorama-canvas"></canvas>
                <div class="panorama-overlay"></div>
                <div class="controls">
                    <button class="controls-btn" data-action="reset" title="Reset View">⟳</button>
                    <button class="controls-btn" data-action="fullscreen" title="Fullscreen">⛶</button>
                    <button class="controls-btn" data-action="info" title="Info">ℹ</button>
                </div>
            </div>
            <div class="scene-selector"></div>
        `;
    }
    
    setupControls() {
        const buttons = this.container.querySelectorAll('.controls-btn');
        buttons.forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                const action = btn.dataset.action;
                this.handleControl(action);
            });
        });
    }
    
    handleControl(action) {
        switch(action) {
            case 'reset':
                this.resetView();
                break;
            case 'fullscreen':
                this.toggleFullscreen();
                break;
            case 'info':
                this.showInfo();
                break;
        }
    }
    
    loadPanorama(imageUrl, options = {}) {
        // Using Pannellum for equirectangular 360° rendering
        if (typeof pannellum === 'undefined') {
            console.warn('Pannellum library not loaded. Loading from CDN...');
            this.loadPannellumFromCDN();
            return;
        }
        
        this.panorama = pannellum.viewer(this.container.querySelector('.panorama-wrapper'), {
            default: {
                firstScene: options.firstScene || 'scene1',
                sceneFadeDuration: 1000,
                autoLoad: true,
                hfov: options.hfov || 110,
                vfov: options.vfov || 70,
                horizonPitch: options.horizonPitch || 0,
                horizonYaw: options.horizonYaw || 0
            },
            scenes: options.scenes || {
                scene1: {
                    title: options.title || 'Interior 360°',
                    author: options.author || 'LOTVUE Pro',
                    type: 'equirectangular',
                    panorama: imageUrl,
                    hotSpots: options.hotspots || []
                }
            }
        });
        
        this.currentScene = options.firstScene || 'scene1';
        this.setupHotSpotListeners();
    }
    
    loadPannellumFromCDN() {
        const script = document.createElement('script');
        script.src = 'https://cdn.pannellum.org/pannellum.2.5.6/pannellum.js';
        script.onload = () => {
            console.log('Pannellum loaded from CDN');
        };
        document.head.appendChild(script);
        
        const link = document.createElement('link');
        link.rel = 'stylesheet';
        link.href = 'https://cdn.pannellum.org/pannellum.2.5.6/pannellum.css';
        document.head.appendChild(link);
    }
    
    addScene(sceneId, sceneData) {
        if (!this.panorama) return;
        
        this.panorama.addScene(sceneId, sceneData);
        this.scenes.push(sceneId);
        
        // Add to scene selector
        const selector = this.container.querySelector('.scene-selector');
        const btn = document.createElement('button');
        btn.className = 'scene-btn';
        btn.textContent = sceneData.title || `Scene ${sceneId}`;
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            this.switchScene(sceneId);
        });
        selector.appendChild(btn);
    }
    
    switchScene(sceneId) {
        if (!this.panorama || !this.scenes.includes(sceneId)) return;
        
        this.panorama.destroy();
        this.panorama = null;
        
        const scene = this.panorama.config.scenes[sceneId];
        this.loadPanorama(scene.panorama, {
            firstScene: sceneId,
            scenes: { [sceneId]: scene }
        });
        
        this.currentScene = sceneId;
    }
    
    addHotSpot(hotspot) {
        if (!this.panorama) return;
        
        const hotspotConfig = {
            type: 'info',
            pitch: hotspot.pitch,
            yaw: hotspot.yaw,
            text: hotspot.text,
            targetScene: hotspot.targetScene,
            targetPitch: hotspot.targetPitch,
            targetYaw: hotspot.targetYaw
        };
        
        this.panorama.addHotSpot(hotspotConfig);
        this.hotspots.push(hotspot);
    }
    
    resetView() {
        if (!this.panorama) return;
        this.panorama.setView({ pitch: 0, yaw: 0, hfov: 110 });
    }
    
    toggleFullscreen() {
        if (!document.fullscreenElement) {
            this.container.requestFullscreen();
        } else {
            document.exitFullscreen();
        }
    }
    
    showInfo() {
        alert('LOTVUE Pro Interior 360° Panorama Viewer\n\n' +
              '• Drag to look around\n' +
              '• Scroll to zoom in/out\n' +
              '• Double-click to reset view\n' +
              '• Use scene selector to change rooms\n' +
              '• Click hotspots for more info');
    }
    
    setupHotSpotListeners() {
        // Add click handlers to hotspots
        this.container.querySelectorAll('.pan-hotspot').forEach(hotspot => {
            hotspot.addEventListener('click', (e) => {
                e.preventDefault();
                const targetScene = hotspot.dataset.targetScene;
                if (targetScene) {
                    this.switchScene(targetScene);
                }
            });
        });
    }
    
    destroy() {
        if (this.panorama) {
            this.panorama.destroy();
        }
        this.container.innerHTML = '';
    }
}

// Pannellum API shim (in case CDN fails)
if (typeof pannellum === 'undefined') {
    window.pannellum = {
        viewer: (container, config) => ({
            loadScene: () => {},
            addScene: () => {},
            addHotSpot: () => {},
            setView: () => {},
            destroy: () => {}
        })
    };
}

// Auto-init if DOM loaded
document.addEventListener('DOMContentLoaded', () => {
    const panorama = document.querySelector('[data-lotvue-panorama]');
    if (panorama) {
        const viewer = new LotVueInteriorPanorama(panorama.id);
        if (panorama.dataset.image) {
            viewer.loadPanorama(panorama.dataset.image);
        }
    }
});
