"""
gaussian_3d.py — LotVue360 TRUE 3D pipeline (Gaussian Splatting).

walkaround video (or photos) -> frames -> COLMAP camera poses -> Brush trains a
3D Gaussian Splat on the GPU (WebGPU, no CUDA compile) -> scene.ply -> served to
the in-browser WebGL viewer. Renders the WHOLE scene (car + environment) in real
3D you can orbit freely — beyond the 2D photo-spin competitors sell.

Runs as a subprocess launched by main.py. Writes status.json throughout.

Usage:
  python gaussian_3d.py --vin VIN --video PATH [--steps 12000] [--max-frames 120]
  python gaussian_3d.py --vin VIN --images DIR [--steps 12000]
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
COLMAP = os.environ.get("COLMAP_EXE", r"E:\ABU\colmap\bin\colmap.exe")
BRUSH = os.environ.get("BRUSH_EXE", r"E:\ABU\brush\brush_app.exe")

_VIN = "UNKNOWN"
_STATUS = None


def set_status(step, progress, extra=None):
    if _STATUS:
        data = {"status": "running", "step": step, "progress": progress, "vin": _VIN}
        if extra:
            data.update(extra)
        _STATUS.write_text(json.dumps(data), encoding="utf-8")
    print(f"[3D] [{progress}%] {step}"); sys.stdout.flush()


def set_done(url, n_splats=None):
    if _STATUS:
        _STATUS.write_text(json.dumps({"status": "done", "step": "complete", "progress": 100,
                                       "vin": _VIN, "url": url, "splats": n_splats}), encoding="utf-8")
    print(f"[3D] DONE -> {url}")


def set_error(msg):
    if _STATUS:
        _STATUS.write_text(json.dumps({"status": "error", "step": "error", "progress": 0,
                                       "vin": _VIN, "error": msg}), encoding="utf-8")
    print(f"[3D] ERROR: {msg}", file=sys.stderr)


def _run(cmd, timeout, label):
    print(f"[3D] $ {label}"); sys.stdout.flush()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "")[-600:]
        raise RuntimeError(f"{label} failed (exit {r.returncode}): {tail}")
    return r


def extract_frames(video, out_dir, max_frames):
    out_dir.mkdir(parents=True, exist_ok=True)
    # probe duration
    pr = subprocess.run([os.environ.get("FFPROBE_PATH", r"E:\ABU\AI\ffmpeg\ffprobe.exe"),
                         "-v", "error", "-show_entries", "format=duration",
                         "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
                        capture_output=True, text=True, timeout=60)
    dur = float(pr.stdout.strip() or "0") or 30.0
    fps = max(1.0, max_frames / dur)
    _run([FFMPEG, "-i", str(video), "-vf", f"fps={fps:.4f},scale=1600:-2", "-q:v", "2", "-y",
          str(out_dir / "frame_%04d.jpg")], 300, "ffmpeg extract")
    return sorted(out_dir.glob("frame_*.jpg"))


def colmap_poses(work, images_dir):
    """Run COLMAP SfM -> work/sparse/0/{cameras,images,points3D}.bin"""
    db = work / "database.db"
    sparse = work / "sparse"
    sparse.mkdir(parents=True, exist_ok=True)
    set_status("Solving camera positions (COLMAP feature extraction)", 25)
    _run([COLMAP, "feature_extractor", "--database_path", str(db), "--image_path", str(images_dir),
          "--ImageReader.single_camera", "1", "--ImageReader.camera_model", "OPENCV",
          "--FeatureExtraction.use_gpu", "1"], 1200, "colmap feature_extractor")
    set_status("Matching features across frames (COLMAP)", 40)
    _run([COLMAP, "exhaustive_matcher", "--database_path", str(db),
          "--FeatureMatching.use_gpu", "1"], 1800, "colmap exhaustive_matcher")
    set_status("Reconstructing 3D camera path (COLMAP mapper)", 55)
    _run([COLMAP, "mapper", "--database_path", str(db), "--image_path", str(images_dir),
          "--output_path", str(sparse)], 3600, "colmap mapper")
    model0 = sparse / "0"
    if not (model0 / "cameras.bin").exists():
        raise RuntimeError("COLMAP could not reconstruct the scene at all — the video lacks "
                           "overlap/parallax. Reshoot a SLOW, steady full circle around the car.")
    # How many frames actually aligned? (images.bin starts with a uint64 count.)
    reg = 0
    try:
        with open(model0 / "images.bin", "rb") as fh:
            reg = struct.unpack("<Q", fh.read(8))[0]
    except Exception:
        pass
    total = len(list(images_dir.glob("frame_*.jpg")))
    print(f"[3D] COLMAP registered {reg}/{total} frames")
    if reg < max(25, int(0.40 * total)):
        raise RuntimeError(
            f"Only {reg} of {total} frames aligned — not enough for a 3D model. This happens when the "
            f"walkaround is too fast, doesn't go fully around, the car is very reflective, or there's "
            f"motion blur. Reshoot: walk a SLOW, steady full circle (~30-40s), keep the whole car AND "
            f"some ground/background in frame, move smoothly (about one step per second).")
    return model0


def train_brush(work, out_ply, steps):
    set_status("Training 3D Gaussian Splat on GPU (Brush)", 65)
    out_ply.parent.mkdir(parents=True, exist_ok=True)
    # Brush reads a COLMAP workspace dir (images/ + sparse/0). Export the ply at the end.
    # Web-optimized: SH degree 1 + capped splats keep the .ply light enough to
    # load fast on a buyer's phone (SH-3/uncapped can be 130MB+; this is ~20-30MB).
    cmd = [BRUSH, str(work), "--total-steps", str(steps),
           "--export-every", str(steps), "--export-path", str(out_ply.parent),
           "--export-name", out_ply.name, "--max-resolution", "1400",
           "--sh-degree", "1", "--max-splats", "400000"]
    print(f"[3D] $ brush train ({steps} steps)"); sys.stdout.flush()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    # Brush may name export with iter; find the produced ply
    plys = sorted(out_ply.parent.glob("*.ply"), key=lambda p: p.stat().st_mtime)
    if not plys:
        tail = (r.stderr or r.stdout or "")[-800:]
        raise RuntimeError(f"Brush produced no .ply (exit {r.returncode}): {tail}")
    final = plys[-1]
    if final != out_ply:
        shutil.copy(final, out_ply)
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
    if args.video:
        frames = extract_frames(Path(args.video), images, args.max_frames)
    elif args.images:
        srcs = sorted(Path(args.images).glob("*.jpg"))
        if not srcs:
            srcs = sorted(Path(args.images).glob("*.png"))
        srcs = [f for f in srcs if "_isolated" not in f.name and "_mask" not in f.name]
        for i, f in enumerate(srcs):
            shutil.copy(f, images / f"frame_{i+1:04d}.jpg")
        frames = sorted(images.glob("frame_*.jpg"))
    else:
        set_error("No --video or --images provided"); return False
    print(f"[3D] {len(frames)} frames")
    if len(frames) < 5:
        set_error(f"Only {len(frames)} frames — need a longer walkaround (aim for 40-150)."); return False

    try:
        colmap_poses(work, images)
        out_ply = vdir / "splat" / "scene.ply"
        train_brush(work, out_ply, args.steps)
    except Exception as e:
        set_error(str(e)); return False

    size_mb = out_ply.stat().st_size / (1024 * 1024)
    set_done(f"/storage/{_VIN}/splat/scene.ply", n_splats=None)
    print(f"[3D] SUCCESS: {out_ply} ({size_mb:.1f} MB)")
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--vin", required=True)
    ap.add_argument("--video", default="")
    ap.add_argument("--images", default="")
    ap.add_argument("--steps", type=int, default=12000)
    ap.add_argument("--max-frames", type=int, default=120)
    a = ap.parse_args()
    sys.exit(0 if run(a) else 1)
