// LOTVUE 360 Viewer - Zoom, Pan, Momentum, Double-tap Reset

function init360Viewer(viewerId, innerId, frameSelector, loaderId, hintId, zoomHintId) {
    // State - start at normal zoom
    let scale = 1.0, panX = 0, panY = 0, isZoomed = false;
    let velocity = 0, momentumInt = null, lastX = 0, lastTime = 0;
    let lastTap = 0;
    let cur = 0, dragging = false, sx = 0, sy = 0, sf = 0;
    let autoInt = null;
    let loaded = 0;
    
    const viewer = document.getElementById(viewerId);
    const inner = document.getElementById(innerId);
    const frames = document.querySelectorAll(frameSelector);
    const loader = document.getElementById(loaderId);
    const hint = document.getElementById(hintId);
    const zoomHint = zoomHintId ? document.getElementById(zoomHintId) : null;
    
    function updateTransform() {
        if (inner) inner.style.transform = 'translate(' + panX + 'px, ' + panY + 'px) scale(' + scale + ')';
        if (zoomHint) zoomHint.classList.toggle('visible', scale > 1);
    }
    
    window['resetZoom_' + viewerId] = function() {
        scale = 1; panX = 0; panY = 0; isZoomed = false;
        updateTransform();
    };
    
    // Preload frames
    frames.forEach(img => {
        const pre = new Image();
        pre.onload = () => {
            loaded++;
            if (loader) loader.textContent = 'Loading ' + loaded + '/...';
            if (loaded === frames.length) {
                if (loader) loader.style.display = 'none';
                if (frames.length > 0) frames[0].style.display = 'block';
                if (hint) hint.style.opacity = '1';
                autoRotate();
            }
        };
        pre.onerror = () => { loaded++; };
        pre.src = img.src;
    });
    
    function autoRotate() {
        let count = 0;
        autoInt = setInterval(() => {
            cur = (cur + 1) % frames.length;
            show();
            count++;
            if (count >= frames.length) { clearInterval(autoInt); autoInt = null; }
        }, 35); // ~28 FPS for smooth video-game feel
    }
    
    function stopAuto() { if (autoInt) { clearInterval(autoInt); autoInt = null; } }
    
    function show() {
        frames.forEach(f => f.style.display = 'none');
        if (frames[cur]) frames[cur].style.display = 'block';
    }
    
    // Zoom (wheel)
    if (viewer) {
        viewer.onwheel = e => {
            e.preventDefault();
            const delta = e.deltaY > 0 ? -0.2 : 0.2;
            const newScale = Math.min(4, Math.max(1, scale + delta));
            if (newScale === 1) window['resetZoom_' + viewerId]();
            else { scale = newScale; isZoomed = scale > 1; updateTransform(); }
        };
        
        // Double-tap
        viewer.ondblclick = () => window['resetZoom_' + viewerId]();
        
        // Mouse drag
        viewer.onmousedown = e => {
            stopAuto();
            if (momentumInt) { clearInterval(momentumInt); momentumInt = null; }
            dragging = true;
            sx = e.clientX; sy = e.clientY; sf = cur;
            if (hint) hint.style.opacity = '0';
            velocity = 0; lastX = e.clientX; lastTime = Date.now();
        };
        
        viewer.onmousemove = e => {
            if (!dragging) return;
            if (isZoomed) {
                panX += e.clientX - sx; panY += e.clientY - sy;
                sx = e.clientX; sy = e.clientY;
                updateTransform();
            } else {
                const dx = e.clientX - sx;
                velocity = (e.clientX - lastX) / (Date.now() - lastTime) * 16;
                lastX = e.clientX; lastTime = Date.now();
                const fd = Math.round(dx / 15);
                if (fd !== 0) { cur = ((sf - fd) % frames.length + frames.length) % frames.length; show(); sf = cur; sx = e.clientX; }
            }
        };
        
        viewer.onmouseup = () => {
            dragging = false;
            if (!isZoomed && Math.abs(velocity) > 0.5) {
                momentumInt = setInterval(() => {
                    velocity *= 0.95;
                    if (Math.abs(velocity) < 0.1) { clearInterval(momentumInt); momentumInt = null; return; }
                    cur = ((cur - Math.sign(velocity)) % frames.length + frames.length) % frames.length;
                    show();
                }, 16);
            }
        };
        viewer.onmouseleave = () => dragging = false;
        
        // Touch drag
        viewer.ontouchstart = e => {
            stopAuto();
            if (momentumInt) { clearInterval(momentumInt); momentumInt = null; }
            dragging = true;
            sx = e.touches[0].clientX; sy = e.touches[0].clientY; sf = cur;
            if (hint) hint.style.opacity = '0';
            velocity = 0; lastX = e.touches[0].clientX; lastTime = Date.now();
            const now = Date.now();
            if (now - lastTap < 300) window['resetZoom_' + viewerId]();
            lastTap = now;
        };
        
        viewer.ontouchmove = e => {
            if (!dragging) return;
            e.preventDefault();
            if (isZoomed) {
                panX += e.touches[0].clientX - sx; panY += e.touches[0].clientY - sy;
                sx = e.touches[0].clientX; sy = e.touches[0].clientY;
                updateTransform();
            } else {
                const dx = e.touches[0].clientX - sx;
                velocity = (e.touches[0].clientX - lastX) / (Date.now() - lastTime) * 16;
                lastX = e.touches[0].clientX; lastTime = Date.now();
                const fd = Math.round(dx / 15);
                if (fd !== 0) { cur = ((sf - fd) % frames.length + frames.length) % frames.length; show(); sf = cur; sx = e.touches[0].clientX; }
            }
        };
        
        viewer.ontouchend = () => {
            dragging = false;
            if (!isZoomed && Math.abs(velocity) > 0.5) {
                momentumInt = setInterval(() => {
                    velocity *= 0.95;
                    if (Math.abs(velocity) < 0.1) { clearInterval(momentumInt); momentumInt = null; return; }
                    cur = ((cur - Math.sign(velocity)) % frames.length + frames.length) % frames.length;
                    show();
                }, 16);
            }
        };
    }
    
    return { show, cur: () => cur };
}
