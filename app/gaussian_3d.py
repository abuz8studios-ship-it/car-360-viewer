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


# ── STEP A: the always-on 360 spin (works from ANY input) ────────────────────
def build_spin(frames, vin, target=72):
    from PIL import Image, ImageOps
    spin_dir = STORAGE_DIR / vin / "spin"
    spin_dir.mkdir(parents=True, exist_ok=True)
    for old in spin_dir.glob("spin_*.jpg"):
        old.unlink()
    n = len(frames)
    if n == 0:
        return 0
    # sample evenly down to ~target for a smooth, web-light spin
    if n > target:
        idx = [round(i * (n - 1) / (target - 1)) for i in range(target)]
        sel = [frames[i] for i in idx]
    else:
        sel = frames
    for i, f in enumerate(sel):
        try:
            im = ImageOps.exif_transpose(Image.open(f)).convert("RGB")
            if im.width > 1280:
                im = im.resize((1280, round(im.height * 1280 / im.width)), Image.LANCZOS)
            im.save(spin_dir / f"spin_{i+1:04d}.jpg", "JPEG", quality=90)
        except Exception as e:
            print(f"[3D] spin frame fail {f.name}: {e}")
    cnt = len(list(spin_dir.glob("spin_*.jpg")))
    (spin_dir / "manifest.json").write_text(json.dumps({"count": cnt}), encoding="utf-8")
    return cnt


# ── STEP B: true 3D (Gaussian Splatting), best-effort ────────────────────────
def colmap_poses(work, images_dir, ordered):
    db = work / "database.db"
    sparse = work / "sparse"; sparse.mkdir(parents=True, exist_ok=True)
    set_status("Solving camera positions (COLMAP)", 45, spin_ready=True)
    _run([COLMAP, "feature_extractor", "--database_path", str(db), "--image_path", str(images_dir),
          "--ImageReader.single_camera", "1", "--ImageReader.camera_model", "OPENCV",
          "--FeatureExtraction.use_gpu", "1"], 1800, "colmap feature_extractor")
    set_status("Matching frames (COLMAP)", 55, spin_ready=True)
    if ordered:
        # walkaround video frames are time-ordered -> sequential matching is far more
        # robust + faster than exhaustive (matches neighbours along the path).
        _run([COLMAP, "sequential_matcher", "--database_path", str(db),
              "--FeatureMatching.use_gpu", "1"], 2400, "colmap sequential_matcher")
    else:
        _run([COLMAP, "exhaustive_matcher", "--database_path", str(db),
              "--FeatureMatching.use_gpu", "1"], 3600, "colmap exhaustive_matcher")
    set_status("Reconstructing 3D camera path (COLMAP mapper)", 62, spin_ready=True)
    # lenient mapper thresholds -> register more frames on reflective / lower-texture scenes
    try:
        _run([COLMAP, "mapper", "--database_path", str(db), "--image_path", str(images_dir),
              "--output_path", str(sparse),
              "--Mapper.init_min_num_inliers", "50",
              "--Mapper.abs_pose_min_num_inliers", "12",
              "--Mapper.min_num_matches", "8",
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


def run(args):
    global _VIN, _STATUS
    _VIN = args.vin.upper()
    vdir = STORAGE_DIR / _VIN
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
        set_error("No video or photos provided"); return False
    print(f"[3D] {len(frames)} frames gathered")
    if len(frames) < 4:
        set_error("Need a walkaround video or at least 4 photos."); return False

    # STEP A — always deliver a 360 spin (works from anything)
    set_status("Building 360 spin", 25)
    nspin = build_spin(frames, _VIN)
    _STATE["spin_ready"] = nspin > 0
    _STATE["spin_frames"] = nspin
    _STATE["spin_url"] = f"/storage/{_VIN}/spin/"
    set_status("360 spin ready — now attempting true 3D", 35, spin_ready=True, spin_frames=nspin)

    # STEP B — best-effort true 3D upgrade
    splat_ready = False
    note = ""
    try:
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
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--vin", required=True)
    ap.add_argument("--video", default="")
    ap.add_argument("--images", default="")
    ap.add_argument("--steps", type=int, default=7000)
    ap.add_argument("--max-frames", type=int, default=150)
    a = ap.parse_args()
    sys.exit(0 if run(a) else 1)
