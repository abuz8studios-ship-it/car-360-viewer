"""
gaussian_3d.py — LotVue360 vehicle 3D / 360 engine.

Accepts ANY walkaround video OR ANY set of photos and ALWAYS delivers a
draggable 360 spin (works from anything, like GLO3D/CloudPano/Spyne) — then
AUTO-UPGRADES to true orbit-anywhere 3D (Gaussian Splatting on the GPU) whenever
the capture has enough camera motion/overlap. It never hard-fails to the user:
if true 3D can't be solved, the 360 spin is still delivered.

Outputs (under storage/<VIN>/):
  spin/spin_NNNN.jpg + spin/manifest.json   <- the always-on 360 spin
  splat/scene.ply                            <- true 3D, when capture supports it
  splat/status.json                          <- {spin_ready, splat_ready, step, progress, note}

Usage:
  python gaussian_3d.py --vin VIN --video PATH   [--steps 7000] [--max-frames 150]
  python gaussian_3d.py --vin VIN --images DIR   [--steps 7000]
"""
import os, sys, json, time, shutil, struct, argparse, subprocess
from pathlib import Path

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / "storage"
FFMPEG = os.environ.get("FFMPEG_PATH", r"E:\ABU\AI\ffmpeg\ffmpeg.exe")
FFPROBE = os.environ.get("FFPROBE_PATH", r"E:\ABU\AI\ffmpeg\ffprobe.exe")
COLMAP = os.environ.get("COLMAP_EXE", r"E:\ABU\colmap\bin\colmap.exe")
BRUSH = os.environ.get("BRUSH_EXE", r"E:\ABU\brush\brush_app.exe")

_VIN = "UNKNOWN"
_STATUS = None
_STATE = {}


def _write(extra):
    if _STATUS:
        _STATUS.write_text(json.dumps({**_STATE, **extra, "vin": _VIN}), encoding="utf-8")


def set_status(step, progress, **extra):
    _STATE.update({"status": "running", "step": step, "progress": progress})
    _STATE.update(extra)
    _write({})
    print(f"[3D] [{progress}%] {step}"); sys.stdout.flush()


def finish():
    _STATE["status"] = "done"; _STATE["progress"] = 100; _STATE["step"] = "complete"
    _write({})
    print(f"[3D] DONE  spin_ready={_STATE.get('spin_ready')} splat_ready={_STATE.get('splat_ready')}")


def set_error(msg):
    _STATE.update({"status": "error", "step": "error", "error": msg})
    _write({})
    print(f"[3D] ERROR: {msg}", file=sys.stderr)


def _run(cmd, timeout, label):
    print(f"[3D] $ {label}"); sys.stdout.flush()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{label} failed (exit {r.returncode}): {(r.stderr or r.stdout or '')[-500:]}")
    return r


# ── frame gathering ──────────────────────────────────────────────────────────
def extract_frames(video, out_dir, max_frames):
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        pr = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
                            capture_output=True, text=True, timeout=60)
        dur = float(pr.stdout.strip() or "0") or 30.0
    except Exception:
        dur = 30.0
    fps = max(0.5, max_frames / dur)
    _run([FFMPEG, "-i", str(video), "-vf", f"fps={fps:.4f},scale='min(1600,iw)':-2",
          "-q:v", "2", "-y", str(out_dir / "frame_%04d.jpg")], 600, "ffmpeg extract")
    return sorted(out_dir.glob("frame_*.jpg"))


def copy_photos(src_dir, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    srcs = sorted(Path(src_dir).glob("*.jpg")) + sorted(Path(src_dir).glob("*.jpeg")) \
        + sorted(Path(src_dir).glob("*.png"))
    srcs = [f for f in srcs if "_isolated" not in f.name and "_mask" not in f.name]
    for i, f in enumerate(srcs):
        shutil.copy(f, out_dir / f"frame_{i+1:04d}.jpg")
    return sorted(out_dir.glob("frame_*.jpg"))


# ── photo ordering + optical-flow interpolation (the CarCutter 4-photo wire) ─
# Sparse photo sets (a dealer's 4-8 standard shots) must become a SMOOTH closed
# 360. Three stages: (1) order the photos around the car (ORB feature-shift,
# same math as the proven /api/process order route), (2) stabilize as before,
# (3) interpolate the stabilized frames up to SPIN_DENSE frames with Farneback
# optical flow, wrap-around included, weighted by the estimated per-gap degrees.
SPIN_DENSE = 36           # output frame count for interpolated photo spins (10°/frame)
ORDER_MIN_CONFIDENCE = 0.25

def order_photos_by_angle(frames):
    """Order sparse photos into walkaround sequence via ORB feature shifts.
    Returns (ordered_frames, gap_degrees_or_None). Falls back to the incoming
    (filename/upload) order — which for dealer listings IS the walkaround order —
    whenever matching confidence is too low to trust."""
    try:
        import cv2
    except Exception:
        return frames, None
    n = len(frames)
    if n < 3:
        return frames, None
    try:
        orb = cv2.ORB_create(nfeatures=2000)
        bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
        kps_desc, widths = [], []
        for f in frames:
            img = cv2.imread(str(f), cv2.IMREAD_GRAYSCALE)
            if img is None:
                kp, des = [], None
                widths.append(1)
            else:
                kp, des = orb.detectAndCompute(img, None)
                widths.append(img.shape[1])
            kps_desc.append((kp, des))
        relative_shifts, match_counts, failed = [], [], []
        for i in range(n):
            kp1, des1 = kps_desc[i]
            kp2, des2 = kps_desc[(i + 1) % n]
            if des1 is None or des2 is None or len(kp1) < 10 or len(kp2) < 10:
                relative_shifts.append(0.0); match_counts.append(0); failed.append(i); continue
            good = []
            for m_n in bf.knnMatch(des1, des2, k=2):
                if len(m_n) == 2 and m_n[0].distance < 0.75 * m_n[1].distance:
                    good.append(m_n[0])
            if len(good) < 10:
                relative_shifts.append(0.0); match_counts.append(len(good)); failed.append(i); continue
            dxs = sorted(kp2[m.trainIdx].pt[0] - kp1[m.queryIdx].pt[0] for m in good)
            relative_shifts.append(dxs[len(dxs) // 2] / max(1, widths[i]))
            match_counts.append(len(good))
        confidences = [min(1.0, c / 50.0) for c in match_counts]
        avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
        if avg_conf < ORDER_MIN_CONFIDENCE or len(failed) > n // 2:
            print(f"[3D] angle-order confidence {avg_conf:.2f} (failed pairs {len(failed)}/{n}) — keeping upload order")
            return frames, None
        # A failed pair contributes a 0.0 shift; left as-is it collapses the angle
        # integration (a failed WRAP pair pins the last photo onto photo 0 and the
        # sort scrambles a correct walkaround). Substitute the median good shift.
        good_shifts = [s for i, s in enumerate(relative_shifts) if i not in failed]
        if failed and good_shifts:
            med = sorted(good_shifts)[len(good_shifts) // 2]
            for i in failed:
                relative_shifts[i] = med
        abs_angles = [0.0]
        for shift in relative_shifts[:-1]:
            abs_angles.append(abs_angles[-1] + shift)
        total_span = abs_angles[-1] + relative_shifts[-1]
        if abs(total_span) < 1e-6:
            return frames, None
        normalized = [(a / total_span) * 360.0 % 360.0 for a in abs_angles]
        order = sorted(range(n), key=lambda i: normalized[i])
        ordered = [frames[i] for i in order]
        angles = [normalized[i] for i in order]
        gaps = []
        for i in range(n):
            a1, a2 = angles[i], angles[(i + 1) % n]
            deg = ((360.0 - a1) + a2) if i == n - 1 else (a2 - a1)
            gaps.append(max(1.0, deg))
        print(f"[3D] angle-ordered {n} photos (confidence {avg_conf:.2f})")
        return ordered, gaps
    except Exception as e:
        print(f"[3D] angle ordering failed, keeping upload order: {e}")
        return frames, None


def _interp_pair(img_a, img_b, t):
    """Farneback optical-flow interpolation between two frames (t in 0..1)."""
    import cv2
    import numpy as np
    gray_a = cv2.cvtColor(img_a, cv2.COLOR_BGR2GRAY)
    gray_b = cv2.cvtColor(img_b, cv2.COLOR_BGR2GRAY)
    flow = cv2.calcOpticalFlowFarneback(gray_a, gray_b, None,
                                        pyr_scale=0.5, levels=3, winsize=15,
                                        iterations=3, poly_n=5, poly_sigma=1.2, flags=0)
    h, w = flow.shape[:2]
    grid_x, grid_y = np.meshgrid(np.arange(w, dtype=np.float32),
                                 np.arange(h, dtype=np.float32))
    map_x = grid_x + flow[..., 0] * t
    map_y = grid_y + flow[..., 1] * t
    map_x_inv = grid_x - flow[..., 0] * (1.0 - t)
    map_y_inv = grid_y - flow[..., 1] * (1.0 - t)
    warped_a = cv2.remap(img_a, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT_101)
    warped_b = cv2.remap(img_b, map_x_inv, map_y_inv, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT_101)
    return cv2.addWeighted(warped_a, 1.0 - t, warped_b, t, 0)


def interpolate_spin_frames(spin_dir, target=SPIN_DENSE, gap_degrees=None):
    """Densify the stabilized spin_NNNN.jpg sequence in place to `target` frames.
    Runs AFTER stabilization on purpose: locked/normalized frames (static backdrop
    in studio/branded) give the flow a pure car-rotation morph instead of warping
    a moving background (the old finalize path's ghosting). Wrap-around (last->first)
    keeps the loop closed. Per-gap frame counts follow gap_degrees when known."""
    import cv2
    import numpy as np
    frames_list = sorted(Path(spin_dir).glob("spin_*.jpg"))
    n = len(frames_list)
    if n < 2 or n >= target:
        return n
    images = []
    ref_shape = None
    for f in frames_list:
        img = cv2.imread(str(f))
        if img is None:
            continue
        if ref_shape is None:
            ref_shape = img.shape[:2]
        elif img.shape[:2] != ref_shape:
            img = cv2.resize(img, (ref_shape[1], ref_shape[0]), interpolation=cv2.INTER_LANCZOS4)
        images.append(img)
    n = len(images)
    if n < 2:
        return n
    if gap_degrees and len(gap_degrees) == n:
        total_deg = sum(gap_degrees)
        gap_frames = [max(1, round(target * d / total_deg)) for d in gap_degrees]
        while sum(gap_frames) < target:
            gap_frames[gap_frames.index(min(gap_frames))] += 1
        while sum(gap_frames) > target:
            gap_frames[gap_frames.index(max(gap_frames))] -= 1
    else:
        base, extra = target // n, target % n
        gap_frames = [base + (1 if i < extra else 0) for i in range(n)]
    # Write the dense sequence to a temp dir first, then swap frame-by-frame with
    # os.replace — the live server may be streaming the current frames, and a
    # delete-everything-first swap would leave the public spin broken on failure.
    tmp_dir = Path(spin_dir) / f"_densify_tmp_{os.getpid()}"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    out_idx = 1
    for i in range(n):
        img_a, img_b = images[i], images[(i + 1) % n]
        for j in range(gap_frames[i]):
            t = j / gap_frames[i]
            if 0 < t < 1:
                try:
                    blended = _interp_pair(img_a, img_b, t)
                except Exception:
                    blended = cv2.addWeighted(img_a, 1.0 - t, img_b, t, 0)
            else:
                blended = img_a
            cv2.imwrite(str(tmp_dir / f"spin_{out_idx:04d}.jpg"), blended,
                        [cv2.IMWRITE_JPEG_QUALITY, 92])
            out_idx += 1
    made = out_idx - 1
    made = _swap_spin_frames(Path(spin_dir), tmp_dir) or made
    print(f"[3D] optical-flow densified {n} -> {made} frames (closed 360 loop)")
    return made


def _swap_spin_frames(spin_dir, tmp_dir, retries=3):
    """Swap a freshly-built spin sequence into place with per-file os.replace
    (retry on Windows share violations while the live server streams a frame),
    then drop leftover old frames beyond the new count. Returns the new count."""
    import time as _time
    new_frames = sorted(tmp_dir.glob("spin_*.jpg"))
    for f in new_frames:
        dst = spin_dir / f.name
        for attempt in range(retries):
            try:
                os.replace(str(f), str(dst))
                break
            except PermissionError:
                if attempt == retries - 1:
                    raise
                _time.sleep(0.3)
    n_new = len(new_frames)
    for old in sorted(spin_dir.glob("spin_*.jpg")):
        try:
            if int(old.stem.split("_")[1]) > n_new:
                old.unlink()
        except (ValueError, OSError):
            pass
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return n_new


# ── STEP A: the always-on 360 spin (works from ANY input) ────────────────────
def build_spin(frames, vin, target=72, style=None, backdrop=None, ordered=False):
    from PIL import Image, ImageOps
    spin_dir = STORAGE_DIR / vin / "spin"
    spin_dir.mkdir(parents=True, exist_ok=True)
    for old in spin_dir.glob("spin_*.jpg"):
        old.unlink()
    # ALWAYS clear stale keyframes: a dense re-run (or video run) must never leave
    # last build's _keys behind, or the generative upgrade would rebuild this VIN's
    # spin from OLD photos.
    shutil.rmtree(spin_dir / "_keys", ignore_errors=True)
    n = len(frames)
    if n == 0:
        return 0
    # sample evenly down to ~target for a smooth, web-light spin
    if n > target:
        idx = [round(i * (n - 1) / (target - 1)) for i in range(target)]
        sel = [frames[i] for i in idx]
    else:
        sel = frames
    # Sparse PHOTO sets (video frames are already time-ordered): put the shots in
    # walkaround order BEFORE stabilizing — the stabilizer smooths the bbox track
    # across the sequence, so a scrambled order corrupts the center/size lock.
    gap_degrees = None
    if not ordered and len(sel) < SPIN_DENSE:
        sel, gap_degrees = order_photos_by_angle(sel)
    # STABILIZE into a smooth GLO3D-style turntable: lock the car centered at a
    # constant size so it rotates in place (raw handheld frames jitter/drift).
    if _stabilize_frames(sel, spin_dir, style=style, backdrop=backdrop):
        cnt = len(list(spin_dir.glob("spin_*.jpg")))
    else:
        # fallback (no rembg, e.g. the light bundle): letterbox every frame onto one
        # fixed canvas — mixed portrait/landscape uploads must not get stretched by
        # the densify pass's uniform resize.
        FB_W, FB_H = 1280, 854
        for i, f in enumerate(sel):
            try:
                im = ImageOps.exif_transpose(Image.open(f)).convert("RGB")
                s = min(FB_W / im.width, FB_H / im.height)
                im = im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))),
                               Image.LANCZOS)
                canvas = Image.new("RGB", (FB_W, FB_H), (246, 247, 249))
                canvas.paste(im, ((FB_W - im.width) // 2, (FB_H - im.height) // 2))
                canvas.save(spin_dir / f"spin_{i+1:04d}.jpg", "JPEG", quality=90)
            except Exception as e:
                print(f"[3D] spin frame fail {f.name}: {e}")
        cnt = len(list(spin_dir.glob("spin_*.jpg")))
    # Sparse set -> densify to a smooth CLOSED 360 (CarCutter Next-Gen parity):
    # optical-flow interpolate the stabilized frames up to SPIN_DENSE, wrap included.
    engine = "raw"
    if 2 <= cnt < SPIN_DENSE:
        # keep the stabilized keyframes + gap map so the generative upgrade
        # (WAN first-last-frame arcs) can rebuild a premium spin afterwards
        keys_dir = spin_dir / "_keys"
        shutil.rmtree(keys_dir, ignore_errors=True)
        keys_dir.mkdir(parents=True, exist_ok=True)
        for i, f in enumerate(sorted(spin_dir.glob("spin_*.jpg"))):
            shutil.copy(f, keys_dir / f"key_{i+1:04d}.jpg")
        eff_style = (style or os.environ.get("LOTVUE_SPIN_STYLE", "natural")).lower()
        (keys_dir / "gaps.json").write_text(json.dumps({
            "gap_degrees": gap_degrees, "style": eff_style}), encoding="utf-8")
        try:
            cnt = interpolate_spin_frames(spin_dir, target=SPIN_DENSE, gap_degrees=gap_degrees)
            engine = "optical-flow"
        except Exception as e:
            print(f"[3D] densify failed: {e}")
    # manifest count must reflect what is actually on disk, whatever happened above
    cnt = len(list(spin_dir.glob("spin_*.jpg")))
    (spin_dir / "manifest.json").write_text(
        json.dumps({"count": cnt, "engine": engine}), encoding="utf-8")
    return cnt


def generative_upgrade(vin, target=72):
    """PREMIUM tier: replace the optical-flow spin with WAN 2.2 Fun-InP generative
    arcs — true novel-view synthesis between each pair of real photos (the real
    photos stay in as anchor frames). Requires the local ComfyUI (:8188); any
    failure leaves the already-delivered flow spin untouched. Returns final count
    or 0 if the upgrade didn't happen."""
    spin_dir = STORAGE_DIR / vin / "spin"
    keys_dir = spin_dir / "_keys"
    keys = sorted(keys_dir.glob("key_*.jpg"))
    n = len(keys)
    if n < 2:
        return 0
    if os.environ.get("LOTVUE_SPIN_GEN", "1") != "1":
        print("[3D] generative upgrade disabled (LOTVUE_SPIN_GEN=0)")
        return 0
    try:
        sys.path.insert(0, str(BASE_DIR))
        from wan_comfy_api import generate_wan_flf_frames, comfyui_up, FLF_PROMPT
    except Exception as e:
        print(f"[3D] generative upgrade unavailable: {e}")
        return 0
    if not comfyui_up():
        print("[3D] generative upgrade skipped: ComfyUI :8188 not reachable")
        return 0
    try:
        import cv2
        meta = json.loads((keys_dir / "gaps.json").read_text(encoding="utf-8"))
    except Exception:
        meta = {}
    gap_degrees = meta.get("gap_degrees")
    style = meta.get("style", "natural")
    if not (gap_degrees and len(gap_degrees) == n):
        gap_degrees = [360.0 / n] * n
    scene = ("a clean bright studio floor" if style in ("studio", "branded")
             else "the dealership lot")
    prompt = ("Camera smoothly orbiting around a parked car on " + scene +
              ", the car stays perfectly still, seamless rotation, professional "
              "automotive 360 photography, consistent lighting, high quality")
    total_deg = sum(gap_degrees)
    tween_budget = max(target - n, n)
    ref = cv2.imread(str(keys[0]))
    ref_h, ref_w = ref.shape[:2]
    gen_dir = spin_dir / f"_gen_tmp_{os.getpid()}"
    shutil.rmtree(gen_dir, ignore_errors=True)
    gen_dir.mkdir(parents=True, exist_ok=True)
    out_idx = 1

    def _emit(img):
        nonlocal out_idx
        if img.shape[:2] != (ref_h, ref_w):
            img = cv2.resize(img, (ref_w, ref_h), interpolation=cv2.INTER_LANCZOS4)
        cv2.imwrite(str(gen_dir / f"spin_{out_idx:04d}.jpg"), img,
                    [cv2.IMWRITE_JPEG_QUALITY, 92])
        out_idx += 1

    for i in range(n):
        need = max(2, round(tween_budget * gap_degrees[i] / total_deg))
        length = 4 * ((need + 2 + 3) // 4) + 1          # 4k+1, >= need+2 endpoints
        set_status(f"AI-synthesizing arc {i+1}/{n} (WAN first-last-frame)",
                   36 + round(34 * i / n), spin_ready=True)
        arc_dir = gen_dir / f"_arc{i}"
        frames = generate_wan_flf_frames(str(keys[i]), str(keys[(i + 1) % n]),
                                         str(arc_dir), prompt=prompt,
                                         num_frames=length, seed=1000 + i)
        if len(frames) < need + 2:
            print(f"[3D] generative arc {i+1}/{n} failed ({len(frames)} frames) — keeping flow spin")
            shutil.rmtree(gen_dir, ignore_errors=True)
            return 0
        _emit(cv2.imread(str(keys[i])))                  # real photo anchor
        tweens = frames[1:-1]                            # drop endpoint dupes
        idxs = [round(j * (len(tweens) - 1) / (need - 1)) for j in range(need)] if need > 1 else [0]
        for j in idxs:
            img = cv2.imread(tweens[j])
            if img is not None:
                _emit(img)
        shutil.rmtree(arc_dir, ignore_errors=True)
    # swap the premium sequence in frame-by-frame (os.replace, retry on share
    # violations) — never leaves the public spin dir empty mid-swap
    _swap_spin_frames(spin_dir, gen_dir)
    cnt = len(list(spin_dir.glob("spin_*.jpg")))
    (spin_dir / "manifest.json").write_text(
        json.dumps({"count": cnt, "engine": "wan-flf", "keyframes": n}), encoding="utf-8")
    print(f"[3D] generative upgrade complete: {n} photos -> {cnt}-frame WAN 360")
    return cnt


def export_mp4(vin):
    """Render the finished turntable to a web-friendly looping MP4 (download / social /
    marketplace). Best-effort: returns the path on success, None if ffmpeg is unavailable
    or there are no frames — never raises, so it can't fail the spin delivery."""
    spin_dir = STORAGE_DIR / vin / "spin"
    frames = sorted(spin_dir.glob("spin_*.jpg"))
    if not frames:
        return None
    out = spin_dir / "spin.mp4"
    fps = max(8, round(len(frames) / 6.0))   # ~6s smooth loop regardless of frame count
    try:
        _run([FFMPEG, "-y", "-framerate", str(fps), "-start_number", "1",
              "-i", str(spin_dir / "spin_%04d.jpg"),
              "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
              "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
              "-movflags", "+faststart", str(out)], 600, "ffmpeg spin.mp4")
        return out if out.exists() else None
    except Exception as e:
        print(f"[3D] mp4 export skipped: {e}")
        return None


def _stabilize_frames(sel, out_dir, OUT_W=1280, OUT_H=854, TARGET_W=0.80, style=None, backdrop=None):
    """Turn raw walkaround frames into a GLO3D-grade turntable. Per frame: segment the
    car (rembg), lock it centered at constant size, then composite per style —
      natural : keep the real lot, softly blurred at the edges (Ahmad's default);
      studio  : drop the cutout on a clean gradient floor + grounding shadow + reflection;
      branded : same cutout, but on the dealer's own backdrop (storage/_brand/background.*)
                with the dealer logo (storage/_brand/logo.png) — falls back to studio look.
    A cross-set exposure/white-balance pass makes EVERY frame match so the spin never
    flickers (the single biggest 'looks professional' lever). Per-call `style` wins; else
    env LOTVUE_SPIN_STYLE; else natural. Returns True on success, False if rembg is absent."""
    try:
        import numpy as np
        from PIL import Image, ImageOps, ImageFilter, ImageDraw
        from rembg import remove, new_session
    except Exception as e:
        print(f"[3D] stabilize unavailable (rembg): {e}")
        return False
    # Per-call style wins; else the install default (env); else natural (Ahmad's pick).
    style = (style or os.environ.get("LOTVUE_SPIN_STYLE", "natural")).lower()
    if style not in ("natural", "studio", "branded"):
        style = "natural"
    cutout = style in ("studio", "branded")   # both cut the car out onto a backdrop
    try:
        sess = None
        for m in ("isnet-general-use", "u2net", "u2netp"):
            try:
                sess = new_session(m); print(f"[3D] segmentation model: {m}"); break
            except Exception:
                continue
        # ── Pass A: segment every frame, collect car box + per-frame car color means ──
        origs, alphas, bboxes, means = [], [], [], []
        last = None
        for f in sel:
            im = ImageOps.exif_transpose(Image.open(f)).convert("RGB")
            a = np.array(remove(im, session=sess))[:, :, 3]
            ys, xs = np.where(a > 40)
            b = (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())) if len(xs) > 200 else last
            last = b or (0, 0, im.width, im.height); b = last
            origs.append(im); alphas.append(a); bboxes.append(b)
            arr = np.asarray(im, dtype=np.float32); mask = a > 40
            means.append([arr[:, :, c][mask].mean() if mask.sum() > 500 else arr[:, :, c].mean() for c in range(3)])
        means = np.array(means)
        target = np.median(means, axis=0)           # shared color/brightness target

        cxs = [(b[0]+b[2])/2 for b in bboxes]; cys = [(b[1]+b[3])/2 for b in bboxes]; cws = [b[2]-b[0] for b in bboxes]
        med_w = float(np.median(cws)) if cws else OUT_W*0.5
        def smooth(v, k=2):
            return [sum(v[max(0,i-k):min(len(v),i+k+1)])/len(v[max(0,i-k):min(len(v),i+k+1)]) for i in range(len(v))]
        cxs, cys, cws = smooth(cxs), smooth(cys), smooth([max(w, med_w*0.6) for w in cws])

        # studio backdrop presets (procedural top->bottom gradients — no assets to download).
        # All keep a darker floor band so the grounding shadow + reflection always read.
        BACKDROPS = {
            "light":  ((249, 250, 252), (210, 215, 222)),   # default cool-light
            "white":  ((255, 255, 255), (238, 240, 243)),   # bright catalog
            "silver": ((228, 230, 234), (176, 180, 188)),   # neutral mid-gray studio
            "warm":   ((250, 247, 240), (225, 216, 201)),   # warm cream
            "blue":   ((234, 241, 250), (186, 205, 228)),   # cool showroom
        }
        bd = (backdrop or "light").lower()
        if bd not in BACKDROPS:
            bd = "light"

        def studio_bg():
            top = np.array(BACKDROPS[bd][0], np.float32); bot = np.array(BACKDROPS[bd][1], np.float32)
            g = np.linspace(0, 1, OUT_H)[:, None, None]
            arr = np.repeat((top*(1-g)+bot*g).astype(np.uint8), OUT_W, axis=1)
            return Image.fromarray(arr, "RGB").convert("RGBA")

        def blur_plate(im):
            s = max(OUT_W/im.width, OUT_H/im.height)
            p = im.resize((int(im.width*s)+2, int(im.height*s)+2), Image.LANCZOS)
            l = (p.width-OUT_W)//2; t = (p.height-OUT_H)//2
            return p.crop((l, t, l+OUT_W, t+OUT_H)).filter(ImageFilter.GaussianBlur(34))

        # optional dealer branding (branded style only) — cover-fit backdrop + corner logo.
        # Missing assets degrade gracefully to the plain studio look, so it never hard-fails.
        _brand_bg = _brand_logo = None
        if style == "branded":
            bdir = STORAGE_DIR / "_brand"
            for name in ("background.jpg", "background.jpeg", "background.png"):
                bp = bdir / name
                if bp.exists():
                    try:
                        bi = ImageOps.exif_transpose(Image.open(bp)).convert("RGB")
                        s = max(OUT_W/bi.width, OUT_H/bi.height)
                        bi = bi.resize((int(bi.width*s)+2, int(bi.height*s)+2), Image.LANCZOS)
                        l = (bi.width-OUT_W)//2; t = (bi.height-OUT_H)//2
                        _brand_bg = bi.crop((l, t, l+OUT_W, t+OUT_H)).convert("RGBA")
                    except Exception:
                        _brand_bg = None
                    break
            lp = bdir / "logo.png"
            if lp.exists():
                try:
                    lg = Image.open(lp).convert("RGBA")
                    lw = max(1, int(OUT_W*0.18)); lh = max(1, int(lg.height*lw/lg.width))
                    _brand_logo = lg.resize((lw, lh), Image.LANCZOS)
                except Exception:
                    _brand_logo = None

        # ── Pass B: normalize colour to the shared target, then composite ──
        for i, (im, a, cx, cy, cw) in enumerate(zip(origs, alphas, cxs, cys, cws)):
            gain = np.clip(target / np.maximum(means[i], 1e-3), 0.75, 1.35)
            scale = (OUT_W*TARGET_W) / max(cw, 1)
            if cutout:
                rgba = np.dstack([np.asarray(im, dtype=np.float32), a.astype(np.float32)])
                rgba[:, :, :3] = np.clip(rgba[:, :, :3] * gain, 0, 255)
                car = Image.fromarray(rgba.astype(np.uint8), "RGBA").resize(
                    (max(1, int(im.width*scale)), max(1, int(im.height*scale))), Image.LANCZOS)
                px = int(OUT_W/2 - cx*scale); py = int(OUT_H*0.44 - cy*scale)
                canvas = _brand_bg.copy() if _brand_bg is not None else studio_bg()
                ca = np.array(car)[:, :, 3]; cyy, cxx = np.where(ca > 40)
                if len(cxx) > 100:
                    foot_y = py+int(cyy.max()); foot_cx = px+int((cxx.min()+cxx.max())/2); foot_w = int(cxx.max()-cxx.min())
                else:
                    foot_y, foot_cx, foot_w = int(OUT_H*0.8), OUT_W//2, int(OUT_W*0.6)
                # grounding shadow (angle-aware: follows the car footprint)
                sh = Image.new("RGBA", (OUT_W, OUT_H), (0, 0, 0, 0)); d = ImageDraw.Draw(sh)
                ew, eh = int(foot_w*0.96), max(10, int(foot_w*0.15))
                d.ellipse([foot_cx-ew//2, foot_y-eh//2, foot_cx+ew//2, foot_y+eh//2], fill=(0, 0, 0, 130))
                canvas = Image.alpha_composite(canvas, sh.filter(ImageFilter.GaussianBlur(20)))
                # floor reflection (flip lower car, fade out)
                refl = np.array(car.transpose(Image.FLIP_TOP_BOTTOM), dtype=np.float32)
                refl[:, :, 3] *= np.linspace(0.28, 0.0, refl.shape[0])[:, None]
                canvas.alpha_composite(Image.fromarray(refl.astype(np.uint8), "RGBA"), (px, foot_y))
                canvas.alpha_composite(car, (px, py))
                if _brand_logo is not None:   # branded: dealer logo, bottom-right
                    canvas.alpha_composite(_brand_logo,
                                           (OUT_W - _brand_logo.width - 24, OUT_H - _brand_logo.height - 20))
                canvas.convert("RGB").save(out_dir / f"spin_{i+1:04d}.jpg", "JPEG", quality=92)
            else:
                arr = np.clip(np.asarray(im, dtype=np.float32) * gain, 0, 255).astype(np.uint8)
                imn = Image.fromarray(arr, "RGB")
                scaled = imn.resize((max(1, int(im.width*scale)), max(1, int(im.height*scale))), Image.LANCZOS)
                canvas = blur_plate(imn)
                canvas.paste(scaled, (int(OUT_W/2 - cx*scale), int(OUT_H*0.5 - cy*scale)))
                canvas.save(out_dir / f"spin_{i+1:04d}.jpg", "JPEG", quality=92)
        print(f"[3D] stabilized+normalized {len(sel)} frames (style={style}) into a GLO3D-grade turntable")
        return True
    except Exception as e:
        print(f"[3D] stabilize failed, using plain frames: {e}")
        import traceback; traceback.print_exc()
        return False


# ── STEP B: true 3D (Gaussian Splatting), best-effort ────────────────────────
def colmap_poses(work, images_dir, ordered):
    db = work / "database.db"
    sparse = work / "sparse"; sparse.mkdir(parents=True, exist_ok=True)
    set_status("Solving camera positions (COLMAP)", 45, spin_ready=True)
    # Grab far more features per frame (16k vs ~8k default) — helps glossy cars /
    # low-texture asphalt where default SIFT finds too few stable points to chain.
    # (affine-shape/DSP are CPU-only in COLMAP and ~10x slower, so we stay on GPU.)
    _run([COLMAP, "feature_extractor", "--database_path", str(db), "--image_path", str(images_dir),
          "--ImageReader.single_camera", "1", "--ImageReader.camera_model", "OPENCV",
          "--FeatureExtraction.use_gpu", "1",
          "--SiftExtraction.max_num_features", "16384"], 2400, "colmap feature_extractor")
    set_status("Matching frames (COLMAP)", 55, spin_ready=True)
    if ordered:
        # walkaround video frames are time-ordered -> sequential matching is far more
        # robust + faster than exhaustive. Wider overlap + quadratic chaining links
        # frames that are several steps apart (helps when the background changes fast).
        _run([COLMAP, "sequential_matcher", "--database_path", str(db),
              "--FeatureMatching.use_gpu", "1",
              "--SequentialMatching.overlap", "20",
              "--SequentialMatching.quadratic_overlap", "1",
              "--FeatureMatching.guided_matching", "1"], 3600, "colmap sequential_matcher")
    else:
        _run([COLMAP, "exhaustive_matcher", "--database_path", str(db),
              "--FeatureMatching.use_gpu", "1"], 3600, "colmap exhaustive_matcher")
    set_status("Reconstructing 3D camera path (COLMAP mapper)", 62, spin_ready=True)
    # lenient mapper thresholds -> register more frames on reflective / lower-texture scenes
    try:
        _run([COLMAP, "mapper", "--database_path", str(db), "--image_path", str(images_dir),
              "--output_path", str(sparse),
              "--Mapper.init_min_num_inliers", "30",
              "--Mapper.abs_pose_min_num_inliers", "8",
              "--Mapper.min_num_matches", "5",
              "--Mapper.multiple_models", "0"], 5400, "colmap mapper")
    except Exception:
        raise RuntimeError("scene couldn't be reconstructed — not enough camera motion/overlap")
    model0 = sparse / "0"
    if not (model0 / "cameras.bin").exists():
        raise RuntimeError("scene could not be reconstructed (not enough camera motion/overlap)")
    reg = 0
    try:
        with open(model0 / "images.bin", "rb") as fh:
            reg = struct.unpack("<Q", fh.read(8))[0]
    except Exception:
        pass
    total = len(list(images_dir.glob("frame_*.jpg")))
    print(f"[3D] COLMAP registered {reg}/{total} frames")
    if reg < max(20, int(0.30 * total)):
        raise RuntimeError(f"only {reg} of {total} frames aligned — capture needs a slower, fuller circle")
    return model0


def train_brush(work, out_ply, steps):
    set_status("Training true 3D model on GPU (Brush)", 75, spin_ready=True)
    out_ply.parent.mkdir(parents=True, exist_ok=True)
    cmd = [BRUSH, str(work), "--total-steps", str(steps), "--export-every", str(steps),
           "--export-path", str(out_ply.parent), "--export-name", out_ply.name,
           "--max-resolution", "1400", "--sh-degree", "1", "--max-splats", "400000"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    plys = sorted(out_ply.parent.glob("*.ply"), key=lambda p: p.stat().st_mtime)
    if not plys:
        raise RuntimeError(f"Brush produced no .ply (exit {r.returncode}): {(r.stderr or '')[-300:]}")
    if plys[-1] != out_ply:
        shutil.copy(plys[-1], out_ply)
    return out_ply


def _acquire_vin_lock(vdir):
    """One engine run per VIN: concurrent runs share _3dwork/spin/_keys and would
    wipe each other mid-build. Returns the lock Path, or None if another LIVE run
    holds it (a dead PID's stale lock is taken over)."""
    lock = vdir / ".engine.lock"
    try:
        if lock.exists():
            try:
                other = int(lock.read_text(encoding="utf-8").strip() or "0")
            except (ValueError, OSError):
                other = 0
            alive = False
            if other and other != os.getpid():
                try:
                    os.kill(other, 0)
                    alive = True
                except OSError:
                    alive = False
            if alive:
                return None
        lock.write_text(str(os.getpid()), encoding="utf-8")
        return lock
    except OSError:
        return lock  # lock bookkeeping must never block a build


def run(args):
    global _VIN, _STATUS
    _VIN = args.vin.upper()
    vdir = STORAGE_DIR / _VIN
    vdir.mkdir(parents=True, exist_ok=True)
    lock = _acquire_vin_lock(vdir)
    if lock is None:
        # do NOT touch status.json — it belongs to the run that holds the lock
        print(f"[3D] another build is already running for {_VIN} — exiting")
        return False
    work = vdir / "_3dwork"
    images = work / "images"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    images.mkdir(parents=True, exist_ok=True)
    (vdir / "splat").mkdir(parents=True, exist_ok=True)
    _STATUS = vdir / "splat" / "status.json"

    set_status("Preparing frames", 10)
    ordered = bool(args.video)
    if args.video:
        frames = extract_frames(Path(args.video), images, args.max_frames)
    elif args.images:
        frames = copy_photos(args.images, images)
    else:
        set_error("No video or photos provided")
        lock.unlink(missing_ok=True)
        return False
    print(f"[3D] {len(frames)} frames gathered")
    if len(frames) < 4:
        set_error("Need a walkaround video or at least 4 photos.")
        lock.unlink(missing_ok=True)
        return False

    # STEP A — always deliver a 360 spin (works from anything)
    set_status("Building 360 spin", 25)
    nspin = build_spin(frames, _VIN, style=(getattr(args, "style", "") or None),
                       backdrop=(getattr(args, "backdrop", "") or None),
                       ordered=ordered)
    _STATE["spin_ready"] = nspin > 0
    _STATE["spin_frames"] = nspin
    _STATE["spin_url"] = f"/storage/{_VIN}/spin/"
    # web-friendly MP4 of the turntable (download / social) — best-effort
    if nspin > 0 and export_mp4(_VIN):
        _STATE["spin_mp4"] = f"/storage/{_VIN}/spin/spin.mp4"
    set_status("360 spin ready", 35, spin_ready=True, spin_frames=nspin)

    # PREMIUM upgrade for sparse photo sets: WAN first-last-frame arcs replace the
    # optical-flow tweens with true generative novel views (real photos stay as
    # anchors). The flow spin above is already live, so failure costs nothing.
    if not ordered and (STORAGE_DIR / _VIN / "spin" / "_keys").exists():
        try:
            ngen = generative_upgrade(_VIN)
            if ngen:
                nspin = ngen
                _STATE["spin_frames"] = ngen
                _STATE["spin_engine"] = "wan-flf"
                if export_mp4(_VIN):
                    _STATE["spin_mp4"] = f"/storage/{_VIN}/spin/spin.mp4"
        except Exception as e:
            print(f"[3D] generative upgrade error (flow spin kept): {e}")
    set_status("Spin complete — now attempting true 3D", 70, spin_ready=True, spin_frames=nspin)

    # STEP B — best-effort true 3D upgrade
    splat_ready = False
    note = ""
    try:
        # COLMAP's acceptance gate requires >=20 registered frames — a <20-frame
        # capture can NEVER pass it (proven across 16 solver strategies in rnd/).
        # Skip the guaranteed failure instead of burning minutes of GPU on it.
        if len(frames) < 20:
            raise RuntimeError("too few frames for 3D reconstruction — "
                               "not enough camera motion/overlap")
        model0 = colmap_poses(work, images, ordered)
        out_ply = vdir / "splat" / "scene.ply"
        train_brush(work, out_ply, args.steps)
        splat_ready = out_ply.exists()
        _STATE["splat_url"] = f"/storage/{_VIN}/splat/scene.ply"
    except Exception as e:
        reason = str(e)
        detail = reason if ("aligned" in reason or "reconstruct" in reason) else "not enough camera motion/overlap"
        note = (f"Your draggable 360° is ready. Full orbit-anywhere 3D wasn't possible this time "
                f"({detail}) — for 3D, reshoot a slow complete circle (or add 30+ overlapping photos) and re-run.")
        print(f"[3D] true-3D skipped: {reason}")

    _STATE["splat_ready"] = splat_ready
    _STATE["viewer"] = f"/splat3d/{_VIN}"
    _STATE["note"] = note
    finish()
    lock.unlink(missing_ok=True)
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--vin", required=True)
    ap.add_argument("--video", default="")
    ap.add_argument("--images", default="")
    ap.add_argument("--steps", type=int, default=7000)
    ap.add_argument("--max-frames", type=int, default=150)
    ap.add_argument("--style", default="", help="natural | studio | branded (else env default)")
    ap.add_argument("--backdrop", default="", help="light|white|silver|warm|blue (studio backdrop)")
    a = ap.parse_args()
    sys.exit(0 if run(a) else 1)
