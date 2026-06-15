// AUTO 360 - Enhanced 360 Viewer: Momentum, Touch, Mobile, Hotspots

function init360Viewer(viewerId, innerId, frameSelector, loaderId, hintId, zoomHintId) {
    let scale = 1.0, panX = 0, panY = 0, isZoomed = false;
    let velocity = 0, momentumInt = null, lastX = 0, lastTime = 0;
    let lastTap = 0;
    let cur = 0, dragging = false, sx = 0, sy = 0, sf = 0;
    let autoInt = null;
    let loaded = 0;
    let touchCount = 0;
    let pinchScale = 1.0;
    
    const viewer = document.getElementById(viewerId);
    const inner = document.getElementById(innerId);
    const frames = document.querySelectorAll(frameSelector);
    const loader = document.getElementById(loaderId);
    const hint = document.getElementById(hintId);
    const zoomHint = zoomHintId ? document.getElementById(zoomHintId) : null;
    
    function updateTransform() {
        if (inner) inner.style.transform = 'translate(' + panX + 'px, ' + panY + 'px) scale(' + (scale * pinchScale) + ')';
        if (zoomHint) zoomHint.classList.toggle('visible', scale > 1 || pinchScale > 1);
    }
    
    window['resetZoom_' + viewerId] = function() {
        scale = 1; pinchScale = 1; panX = 0; panY = 0; isZoomed = false;
        updateTransform();
    };
    
    // Preload
    frames.forEach(img => {
        const pre = new Image();
        pre.onload = () => {
            loaded++;
            if (loader) loader.textContent = 'Loading ' + loaded + '/' + frames.length + '...';
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
        }, 35);
    }
    
    function stopAuto() { if (autoInt) { clearInterval(autoInt); autoInt = null; } }
    
    function show() {
        frames.forEach(f => f.style.display = 'none');
        if (frames[cur]) frames[cur].style.display = 'block';
    }
    
    if (viewer) {
        // Wheel zoom
        viewer.onwheel = e => {
            e.preventDefault();
            const delta = e.deltaY > 0 ? -0.15 : 0.15;
            const newScale = Math.min(4, Math.max(1, scale + delta));
            if (newScale === 1) window['resetZoom_' + viewerId]();
            else { scale = newScale; isZoomed = scale > 1; updateTransform(); }
        };
        
        viewer.ondblclick = () => window['resetZoom_' + viewerId]();
        
        // Mouse drag with momentum
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
                velocity = (e.clientX - lastX) / (Date.now() - lastTime || 1) * 16;
                lastX = e.clientX; lastTime = Date.now();
                const fd = Math.round(dx / 12);
                if (fd !== 0) { cur = ((sf - fd) % frames.length + frames.length) % frames.length; show(); sf = cur; sx = e.clientX; }
            }
        };
        
        viewer.onmouseup = () => {
            dragging = false;
            if (!isZoomed && Math.abs(velocity) > 0.3) {
                momentumInt = setInterval(() => {
                    velocity *= 0.93;
                    if (Math.abs(velocity) < 0.1) { clearInterval(momentumInt); momentumInt = null; return; }
                    cur = ((cur - Math.sign(velocity)) % frames.length + frames.length) % frames.length;
                    show();
                }, 16);
            }
        };
        viewer.onmouseleave = () => { dragging = false; };
        
        // Touch events
        let lastTouchDist = 0;
        
        viewer.ontouchstart = e => {
            stopAuto();
            if (momentumInt) { clearInterval(momentumInt); momentumInt = null; }
            dragging = true;
            touchCount = e.touches.length;
            sx = e.touches[0].clientX; sy = e.touches[0].clientY; sf = cur;
            if (hint) hint.style.opacity = '0';
            velocity = 0; lastX = e.touches[0].clientX; lastTime = Date.now();
            
            if (touchCount === 2) {
                lastTouchDist = Math.hypot(
                    e.touches[0].clientX - e.touches[1].clientX,
                    e.touches[0].clientY - e.touches[1].clientY
                );
            }
            
            const now = Date.now();
            if (now - lastTap < 300) window['resetZoom_' + viewerId]();
            lastTap = now;
        };
        
        viewer.ontouchmove = e => {
            if (!dragging) return;
            e.preventDefault();
            
            if (touchCount === 2 && e.touches.length === 2) {
                const dist = Math.hypot(
                    e.touches[0].clientX - e.touches[1].clientX,
                    e.touches[0].clientY - e.touches[1].clientY
                );
                const scaleDelta = dist / lastTouchDist;
                pinchScale = Math.min(4, Math.max(1, pinchScale * scaleDelta));
                lastTouchDist = dist;
                if (pinchScale <= 1.01) { pinchScale = 1; isZoomed = false; } else isZoomed = true;
                updateTransform();
            } else if (touchCount === 1) {
                if (isZoomed || pinchScale > 1.1) {
                    panX += e.touches[0].clientX - sx; panY += e.touches[0].clientY - sy;
                    sx = e.touches[0].clientX; sy = e.touches[0].clientY;
                    updateTransform();
                } else {
                    const dx = e.touches[0].clientX - sx;
                    velocity = (e.touches[0].clientX - lastX) / (Date.now() - lastTime || 1) * 16;
                    lastX = e.touches[0].clientX; lastTime = Date.now();
                    const fd = Math.round(dx / 12);
                    if (fd !== 0) { cur = ((sf - fd) % frames.length + frames.length) % frames.length; show(); sf = cur; sx = e.touches[0].clientX; }
                }
            }
        };
        
        viewer.ontouchend = e => {
            dragging = false;
            touchCount = e.touches.length;
            if (touchCount === 0 && !isZoomed && Math.abs(velocity) > 0.3) {
                momentumInt = setInterval(() => {
                    velocity *= 0.93;
                    if (Math.abs(velocity) < 0.1) { clearInterval(momentumInt); momentumInt = null; return; }
                    cur = ((cur - Math.sign(velocity)) % frames.length + frames.length) % frames.length;
                    show();
                }, 16);
            }
        };
        
        // Pinch zoom reset on double tap
        viewer.addEventListener('gesturestart', e => e.preventDefault());
    }
    
    return { show, cur: () => cur };
}
