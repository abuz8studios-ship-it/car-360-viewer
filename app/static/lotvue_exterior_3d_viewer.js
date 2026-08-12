// LOTVUE PRO — Exterior 360° Interactive 3D Viewer
// Frame interpolation + momentum + touch + zoom + pan
// GLO3D Competitor Feature

class LotVueExteriorViewer {
    constructor(containerId, framesSelector) {
        this.container = document.getElementById(containerId);
        this.frames = document.querySelectorAll(framesSelector);
        this.currentFrame = 0;
        this.totalFrames = this.frames.length;
        
        // State
        this.isDragging = false;
        this.startX = 0;
        this.currentX = 0;
        this.velocity = 0;
        this.lastX = 0;
        this.lastTime = 0;
        
        // Zoom state
        this.scale = 1.0;
        this.minScale = 1.0;
        this.maxScale = 3.0;
        this.isZoomed = false;
        
        // Auto-rotate
        this.autoRotate = true;
        this.autoRotateSpeed = 0.5; // degrees per frame
        this.autoRotateInterval = null;
        
        this.init();
    }
    
    init() {
        this.setupEventListeners();
        this.showFrame(0);
        this.startAutoRotate();
        this.animate();
    }
    
    setupEventListeners() {
        // Mouse events
        this.container.addEventListener('mousedown', (e) => this.onMouseDown(e));
        document.addEventListener('mousemove', (e) => this.onMouseMove(e));
        document.addEventListener('mouseup', () => this.onMouseUp());
        
        // Touch events
        this.container.addEventListener('touchstart', (e) => this.onTouchStart(e), { passive: false });
        this.container.addEventListener('touchmove', (e) => this.onTouchMove(e), { passive: false });
        this.container.addEventListener('touchend', () => this.onTouchEnd());
        
        // Wheel zoom
        this.container.addEventListener('wheel', (e) => this.onWheel(e), { passive: false });
        
        // Double click zoom
        this.container.addEventListener('dblclick', (e) => this.onDoubleClick(e));
        
        // Keyboard
        document.addEventListener('keydown', (e) => this.onKeyDown(e));
    }
    
    onMouseDown(e) {
        this.isDragging = true;
        this.startX = e.clientX;
        this.lastX = e.clientX;
        this.lastTime = Date.now();
        this.velocity = 0;
        this.stopAutoRotate();
    }
    
    onMouseMove(e) {
        if (!this.isDragging) return;
        
        const deltaX = e.clientX - this.lastX;
        const currentTime = Date.now();
        const deltaTime = currentTime - this.lastTime;
        
        if (deltaTime > 0) {
            this.velocity = deltaX / deltaTime;
        }
        
        this.currentX = this.startX + deltaX;
        const angle = (this.currentX / this.container.offsetWidth) * 360;
        this.showFrameAtAngle(angle);
        
        this.lastX = e.clientX;
        this.lastTime = currentTime;
    }
    
    onMouseUp() {
        this.isDragging = false;
        this.startAutoRotate();
    }
    
    onTouchStart(e) {
        e.preventDefault();
        if (e.touches.length === 1) {
            this.isDragging = true;
            this.startX = e.touches[0].clientX;
            this.lastX = e.touches[0].clientX;
            this.lastTime = Date.now();
            this.velocity = 0;
            this.stopAutoRotate();
        }
    }
    
    onTouchMove(e) {
        e.preventDefault();
        if (!this.isDragging || e.touches.length !== 1) return;
        
        const deltaX = e.touches[0].clientX - this.lastX;
        const currentTime = Date.now();
        const deltaTime = currentTime - this.lastTime;
        
        if (deltaTime > 0) {
            this.velocity = deltaX / deltaTime;
        }
        
        this.currentX = this.startX + deltaX;
        const angle = (this.currentX / this.container.offsetWidth) * 360;
        this.showFrameAtAngle(angle);
        
        this.lastX = e.touches[0].clientX;
        this.lastTime = currentTime;
    }
    
    onTouchEnd() {
        this.isDragging = false;
        this.startAutoRotate();
    }
    
    onWheel(e) {
        e.preventDefault();
        const delta = e.deltaY > 0 ? 0.9 : 1.1;
        this.scale = Math.max(this.minScale, Math.min(this.maxScale, this.scale * delta));
        this.updateTransform();
    }
    
    onDoubleClick(e) {
        e.preventDefault();
        this.isZoomed = !this.isZoomed;
        if (this.isZoomed) {
            this.scale = 2.0;
        } else {
            this.scale = 1.0;
        }
        this.updateTransform();
    }
    
    onKeyDown(e) {
        switch(e.key) {
            case 'ArrowLeft':
                this.previousFrame();
                break;
            case 'ArrowRight':
                this.nextFrame();
                break;
            case ' ':
                e.preventDefault();
                this.toggleAutoRotate();
                break;
            case '+':
            case '=':
                this.scale = Math.min(this.maxScale, this.scale * 1.2);
                this.updateTransform();
                break;
            case '-':
                this.scale = Math.max(this.minScale, this.scale / 1.2);
                this.updateTransform();
                break;
        }
    }
    
    showFrame(index) {
        if (index < 0) index = this.totalFrames - 1;
        if (index >= this.totalFrames) index = 0;
        
        this.currentFrame = index;
        this.frames.forEach((frame, i) => {
            frame.style.display = i === index ? 'block' : 'none';
        });
    }
    
    showFrameAtAngle(angle) {
        const normalizedAngle = ((angle % 360) + 360) % 360;
        const frameIndex = Math.floor((normalizedAngle / 360) * this.totalFrames) % this.totalFrames;
        this.showFrame(frameIndex);
    }
    
    nextFrame() {
        this.showFrame(this.currentFrame + 1);
    }
    
    previousFrame() {
        this.showFrame(this.currentFrame - 1);
    }
    
    startAutoRotate() {
        this.autoRotate = true;
    }
    
    stopAutoRotate() {
        this.autoRotate = false;
    }
    
    toggleAutoRotate() {
        this.autoRotate = !this.autoRotate;
        if (this.autoRotate) {
            this.startAutoRotate();
        } else {
            this.stopAutoRotate();
        }
    }
    
    updateTransform() {
        this.container.style.transform = `scale(${this.scale})`;
    }
    
    animate() {
        if (this.autoRotate && !this.isDragging) {
            const currentTime = Date.now();
            const deltaTime = currentTime - this.lastTime;
            
            if (deltaTime > 16) { // ~60fps
                const angle = (this.currentX / this.container.offsetWidth) * 360;
                const newAngle = (angle + this.autoRotateSpeed * (deltaTime / 16)) % 360;
                this.currentX = (newAngle / 360) * this.container.offsetWidth;
                this.showFrameAtAngle(newAngle);
                this.lastTime = currentTime;
            }
        } else if (!this.isDragging && this.velocity !== 0) {
            // Momentum
            this.velocity *= 0.95; // Decay
            if (Math.abs(this.velocity) > 0.01) {
                this.currentX += this.velocity * 16;
                const angle = (this.currentX / this.container.offsetWidth) * 360;
                this.showFrameAtAngle(angle);
            } else {
                this.velocity = 0;
            }
        }
        
        requestAnimationFrame(() => this.animate());
    }
    
    loadFrames(frames) {
        this.frames = frames;
        this.totalFrames = this.frames.length;
        this.showFrame(0);
    }
    
    setAutoRotateSpeed(speed) {
        this.autoRotateSpeed = speed;
    }
    
    destroy() {
        this.stopAutoRotate();
        this.container.innerHTML = '';
    }
}

// Export for use
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { LotVueExteriorViewer };
}

// Auto-init if DOM loaded
document.addEventListener('DOMContentLoaded', () => {
    const viewer = document.querySelector('[data-lotvue-exterior]');
    if (viewer) {
        new LotVueExteriorViewer(
            viewer.id,
            viewer.querySelector('[data-frame]') ? '[data-frame]' : 'img'
        );
    }
});
