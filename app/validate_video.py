"""
validate_video.py — P0 UPLOAD QUALITY GATE (Spyne validate-video parity).

Runs at the FRONT DOOR of /api/upload, before any expensive processing.
Modeled on Spyne's /360/v1/validate-video (cropping / angle / car-present).

Why: 2026-08-11 we generated 68 blob GLBs from bad input before looking at one.
2026-08-12 measurement showed validate.py's aspect-spread check does NOT catch
the synthetic-turntable set (DS8097 spread=0.822 — a turntable DOES change the
silhouette). What discriminates a real walkaround from a static-camera
turntable is BACKGROUND PARALLAX: when the camera orbits the car, the
background changes completely between distant frames; when the car spins on a
turntable in front of a static camera, the background is frozen. That frozen
background is exactly why COLMAP registered 2/N frames — no camera motion to
solve. So this gate measures background motion directly.

CHECKS (all measured, none inferred):
  CAR_PRESENT   subject matte covers a plausible area fraction in >=80% samples
  CROPPING      tolerant set-level check — occasional edge-touch is normal in
                real handheld footage (DSREAL01 real walkaround touches edges
                in 4/8 samples); only SYSTEMIC crop (>70% of samples) fails
  ANGLE         silhouette aspect must swing wide<->narrow across the set
                (side views are wide, front/rear narrow; a full walkaround
                transitions between them repeatedly)
  CAMERA_MOTION background mean-abs-diff between consecutive samples; below
                threshold = static-camera turntable signature -> FAIL
  EXPOSURE      mean luma sanity per frame

SEGMENTATION MODES
  CAR_PRESENT, CROPPING and ANGLE need a subject matte, which comes only from
  the operator-approved matting model (rembg_runtime). The default install has
  none, so those three are reported in checks["not_evaluated"] and the verdict
  rests on CONTINUITY, CAMERA_MOTION (measured outside a centred subject prior)
  and EXPOSURE. checks["segmentation"] is "model", "unavailable" or
  "model_failed" (configured model could not run; same model-free checks).

USAGE
  python validate_video.py --video path\to\walkaround.mp4 [--json]
  python validate_video.py --dir path\to\frames [--json]
  python validate_video.py --vin WDDEJ7EB9CA029569 [--json]
Importable: validate_dir(path), validate_video_file(path) -> verdict dict.
"""
import argparse, json, os, shutil, subprocess, sys, tempfile
from pathlib import Path
import numpy as np
from PIL import Image

BASE = Path(__file__).parent
STORAGE = BASE / "storage"

# ── Thresholds ───────────────────────────────────────────────────────────────
# Provisional values marked MEASURE are tuned against DSREAL01 (real
# walkaround, COLMAP 150/150) and DS8097 (synthetic turntable, COLMAP 2/N)
# in the same session they were written. See LOTVUE_CANONICAL_LEDGER row 11.
MIN_AREA = 0.06        # subject < 6% of frame = car absent / too far
MAX_AREA = 0.92        # subject > 92% = extreme crop
EDGE_TOL = 2           # px tolerance for "touching frame edge"
LUMA_LO, LUMA_HI = 28, 232
SAMPLE = 10            # frames sampled per set
PRESENT_RATIO_MIN = 0.80
CROP_RATIO_MAX = 0.70  # systemic-crop threshold (fraction of samples)
ASPECT_RANGE_MIN = 0.35     # wide-vs-narrow silhouette swing for coverage
COVERAGE_TRANSITIONS_MIN = 2  # wide<->narrow state changes across the set
BG_MOTION_MIN = 0.040  # below = true static-camera turntable (frozen background)
# Continuity (MEASURED 2026-08-12, ORB 1500 feats @640x360, adjacent pairs):
#   DSREAL01 real video: inliers med 471 min 243 -> all pairs strong
#   DS8097 scraped spin: 44/162/192/199 on 4 of 8 pairs -> weak
ORB_INLIER_MIN = 200      # adjacent-pair RANSAC inliers below this = weak pair
WEAK_PAIR_RATIO_MAX = 0.30  # >30% weak pairs = not a continuous walkaround
PAIR_SPACING_S = 0.4      # video continuity pairs sampled this far apart


# ── ffmpeg resolution (mirrors main.py; no import of main — circular) ────────
def _resolve_tool(env_key, name):
    """env override -> bundled app/bin -> legacy workstation -> PATH.

    ``name`` is the bare tool name; the platform executable suffix is added
    here so Linux/macOS source runs do not look for ``ffprobe.exe``."""
    exe = name + (".exe" if os.name == "nt" and not name.endswith(".exe") else "")
    for cand in (os.environ.get(env_key, "").strip(),
                 str(BASE / "bin" / exe),
                 str(BASE.parent.parent / "AI" / "ffmpeg" / exe)):
        if cand and os.path.exists(cand):
            return cand
    return shutil.which(exe) or exe

FFMPEG = _resolve_tool("FFMPEG_PATH", "ffmpeg")
FFPROBE = _resolve_tool("FFPROBE_PATH", "ffprobe")


def _video_duration(video_path):
    out = subprocess.run([FFPROBE, "-v", "error", "-show_entries",
                          "format=duration", "-of",
                          "default=noprint_wrappers=1:nokey=1", str(video_path)],
                         capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {out.stderr[:300]}")
    return float(out.stdout.strip())


def sample_video_frames(video_path, outdir, n=SAMPLE):
    """Extract n evenly spaced probe frames. Cheap: runs BEFORE the full
    36-frame extraction + background pipeline."""
    outdir = Path(outdir); outdir.mkdir(parents=True, exist_ok=True)
    dur = _video_duration(video_path)
    if dur <= 0:
        raise RuntimeError("video has zero duration")
    fps = n / dur
    out = subprocess.run([FFMPEG, "-i", str(video_path), "-vf", f"fps={fps}",
                          "-frames:v", str(n), "-q:v", "2", "-y",
                          str(outdir / "probe_%03d.jpg")],
                         capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {out.stderr[:300]}")
    return sorted(outdir.glob("probe_*.jpg"))


def _orb_inliers(path_a, path_b):
    """RANSAC-inlier count of ORB matches between two frames @640x360 — the
    same adjacent-view overlap COLMAP's sequential matcher depends on."""
    import cv2
    a = cv2.resize(cv2.imread(str(path_a), cv2.IMREAD_GRAYSCALE), (640, 360))
    b = cv2.resize(cv2.imread(str(path_b), cv2.IMREAD_GRAYSCALE), (640, 360))
    orb = cv2.ORB_create(1500)
    ka, da = orb.detectAndCompute(a, None)
    kb, db = orb.detectAndCompute(b, None)
    if da is None or db is None:
        return 0
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(da, db, k=2)
    good = [m for p in pairs if len(p) == 2
            for m, n_ in [p] if m.distance < 0.75 * n_.distance]
    if len(good) < 12:
        return len(good)
    pa = np.float32([ka[m.queryIdx].pt for m in good])
    pb = np.float32([kb[m.trainIdx].pt for m in good])
    try:
        _, mask = cv2.findFundamentalMat(pa, pb, cv2.FM_RANSAC, 3.0, 0.99)
        return int(mask.sum()) if mask is not None else 0
    except cv2.error:
        # degenerate geometry (few/collinear matches) — count raw good matches
        return len(good)


def continuity_check(pair_list):
    """pair_list: [(pathA, pathB), ...] of temporally adjacent frames."""
    inliers = [_orb_inliers(a, b) for a, b in pair_list]
    weak = sum(1 for i in inliers if i < ORB_INLIER_MIN)
    ratio = weak / len(inliers) if inliers else 1.0
    return {"pair_inliers": inliers, "weak_pairs": weak,
            "weak_ratio": round(ratio, 2), "ok": ratio <= WEAK_PAIR_RATIO_MAX}


# Without an approved matting model the gate cannot measure the car's
# silhouette, so background motion is measured outside a centred subject prior
# (a walkaround keeps the car centred; a turntable leaves this area frozen).
_YY, _XX = np.mgrid[0:64, 0:64]
_CENTRE_PRIOR = (((_XX - 31.5) / 22.4) ** 2 + ((_YY - 35.0) / 19.2) ** 2) <= 1.0
MODEL_FREE_SKIPPED = ["car_present", "cropping", "angle_coverage"]


def _model_masker():
    """Subject-mask callable backed by the operator-approved matting model, or
    None when no model is selected/acknowledged (the default install)."""
    try:
        from rembg_runtime import is_enabled
    except ImportError:
        return None
    if not is_enabled():
        return None

    def mask(img):
        from studio import cut_car  # lazy: pulls in the matting model once
        return np.array(cut_car(img).getchannel("A")) > 24
    return mask


def _frame_facts(path, masker=None):
    """One frame -> (facts dict, small grayscale, small subject mask).

    ``masker=None`` records model-free facts only (exposure + a centred
    subject prior for the background-motion check)."""
    img = Image.open(path).convert("RGB")
    f = {"file": Path(path).name}
    g = np.asarray(img.convert("L").resize((64, 64)), dtype=np.float32) / 255.0
    f["luma"] = round(float(np.asarray(img.convert("L")).mean()), 1)
    if masker is None:
        f.update(area=None, present=None, cropped=None, aspect=None)
        return f, g, _CENTRE_PRIOR
    m = masker(img)
    h, w = m.shape
    f["area"] = round(float(m.mean()), 4)
    ms = np.asarray(Image.fromarray((m * 255).astype(np.uint8)).resize((64, 64))) > 64
    if not m.any():
        f.update(present=False, cropped=[], aspect=None)
        return f, g, ms
    ys, xs = np.where(m)
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    f["present"] = MIN_AREA <= f["area"] <= MAX_AREA
    touch = [n_ for n_, c in (("left", x0 <= EDGE_TOL), ("right", x1 >= w - 1 - EDGE_TOL),
                              ("top", y0 <= EDGE_TOL)) if c]  # bottom contact is normal
    f["cropped"] = touch
    f["aspect"] = round((x1 - x0) / max(1, (y1 - y0)), 3)
    return f, g, ms


def analyze_set(files, sample=SAMPLE, pair_list=None):
    """Set-level gate. Returns verdict dict with measured checks + reasons a
    capture UI can show verbatim. pair_list overrides continuity pairs (used
    by video mode); default = true adjacent neighbors at sample positions."""
    files = sorted(files)
    n = len(files)
    if n < 4:
        return {"verdict": "FAIL", "reasons": [f"only {n} frames — need at least 4"],
                "frames": n, "checks": {}}
    # Guided PHOTO batches (Glo3D-style, <=24 discrete shots) are a legitimate
    # spin input — adjacent shots are ~15-45deg apart, so the video-continuity
    # bar cannot apply. Dense sets (>24 frames) claim walkaround density and
    # must pass it (DS8097's 72 frames is exactly the case that must FAIL).
    guided_photo_set = pair_list is None and n <= 24
    if guided_photo_set:
        cont = {"pair_inliers": [], "weak_pairs": 0, "weak_ratio": 0.0,
                "ok": True, "skipped": "guided photo set (<=24 shots)"}
    else:
        if pair_list is None:
            pidx = sorted(set(min(n - 2, int(i * n / 8)) for i in range(8)))
            pair_list = [(files[i], files[i + 1]) for i in pidx]
        cont = continuity_check(pair_list)
    idx = sorted(set(min(n - 1, int(i * n / min(sample, n))) for i in range(min(sample, n))))
    masker = _model_masker()
    segmentation = "model" if masker else "unavailable"

    def sample_facts(mask_fn):
        rows = [_frame_facts(files[i], mask_fn) for i in idx]
        return [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows]

    try:
        per, grays, masks = sample_facts(masker)
    except Exception as exc:
        if masker is None:
            raise
        # A configured model that cannot load/run must not turn every capture
        # into a rejection; fall back to the model-free checks and say so.
        print(f"[gate] matting model unavailable, using model-free checks: {type(exc).__name__}: {exc}")
        segmentation = "model_failed"
        per, grays, masks = sample_facts(None)
    segmented = segmentation == "model"

    k = len(per)
    expo_bad = sum(1 for p in per if not (LUMA_LO <= p["luma"] <= LUMA_HI)) / k
    if segmented:
        present_ratio = sum(1 for p in per if p.get("present")) / k
        crop_ratio = sum(1 for p in per if p["cropped"]) / k
        aspects = [p["aspect"] for p in per if p["aspect"]]
        a_range = round(max(aspects) - min(aspects), 3) if len(aspects) > 1 else 0.0
        mid = (max(aspects) + min(aspects)) / 2 if aspects else 0
        states = ["w" if a > mid else "n" for a in aspects]
        transitions = sum(1 for a, b in zip(states, states[1:]) if a != b)
    else:
        present_ratio = crop_ratio = a_range = transitions = None
        aspects = []

    # background parallax: mean-abs-diff of non-subject pixels, consecutive pairs
    diffs = []
    for j in range(len(grays) - 1):
        bg = ~(masks[j] | masks[j + 1])
        if bg.mean() < 0.05:
            continue  # subject fills frame; no background to compare
        diffs.append(float(np.abs(grays[j] - grays[j + 1])[bg].mean()))
    bg_motion = round(float(np.median(diffs)), 4) if diffs else 0.0

    checks = {"present_ratio": None if present_ratio is None else round(present_ratio, 2),
              "crop_ratio": None if crop_ratio is None else round(crop_ratio, 2),
              "exposure_bad_ratio": round(expo_bad, 2), "aspect_range": a_range,
              "coverage_transitions": transitions, "bg_motion": bg_motion,
              "pair_inliers": cont["pair_inliers"], "weak_pair_ratio": cont["weak_ratio"],
              "sampled": k, "segmentation": segmentation}
    if not segmented:
        checks["not_evaluated"] = list(MODEL_FREE_SKIPPED)
    reasons = []
    if segmented and present_ratio < PRESENT_RATIO_MIN:
        reasons.append(f"car not clearly visible in {int((1-present_ratio)*k)}/{k} "
                       "sampled frames — keep the whole car in frame")
    if segmented and crop_ratio > CROP_RATIO_MAX:
        reasons.append("car is cut off by the frame edge in most of the capture — "
                       "step back so the full car fits")
    if expo_bad > 0.5:
        reasons.append("footage too dark or blown out — recapture in even light")
    if aspects and (a_range < ASPECT_RANGE_MIN or transitions < COVERAGE_TRANSITIONS_MIN):
        reasons.append("missing angle coverage — walk a full circle: front, both "
                       "sides and rear must all appear")
    if bg_motion < BG_MOTION_MIN:
        reasons.append("static camera / turntable signature: the background does not "
                       "move across the capture. Walk around the car with your phone "
                       "instead of rotating the car in front of a fixed camera.")
    if not cont["ok"]:
        reasons.append(f"not a smooth continuous walkaround: {cont['weak_pairs']}/"
                       f"{len(cont['pair_inliers'])} adjacent views barely overlap "
                       "(discrete or synthetic spin frames, or camera moving too "
                       "fast). This is the input signature that broke 3D "
                       "reconstruction (COLMAP registered 2/N frames) — recapture "
                       "as one slow continuous video.")
    if guided_photo_set:
        checks["continuity"] = cont["skipped"]
    return {"verdict": "PASS" if not reasons else "FAIL", "reasons": reasons,
            "frames": n, "checks": checks,
            "splat_eligible": (not reasons) and bg_motion >= BG_MOTION_MIN
                              and cont["ok"] and not guided_photo_set,
            "per_frame": per}


# ── Public API (imported by main.py /api/upload) ─────────────────────────────
def validate_dir(dirpath, sample=SAMPLE):
    src = Path(dirpath)
    files = [p for p in src.iterdir()
             if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")]
    if not files:
        return {"verdict": "FAIL", "reasons": [f"no frames found in {src.name}"],
                "frames": 0, "checks": {}}
    res = analyze_set(files, sample)
    res["source"] = str(src)
    return res


def _video_pairs(video_path, outdir, dur, n_pairs=8):
    """n_pairs of frames PAIR_SPACING_S apart at even timestamps — same
    temporal regime the continuity thresholds were measured at."""
    pairs = []
    for i in range(n_pairs):
        t = dur * (i + 0.5) / n_pairs
        base = Path(outdir) / f"pair_{i:02d}"
        got = []
        for j, tt in enumerate((t, min(t + PAIR_SPACING_S, max(0.0, dur - 0.05)))):
            f = Path(f"{base}_{j}.jpg")
            r = subprocess.run([FFMPEG, "-ss", f"{tt:.3f}", "-i", str(video_path),
                                "-frames:v", "1", "-q:v", "2", "-y", str(f)],
                               capture_output=True, text=True, timeout=30)
            if r.returncode == 0 and f.exists():
                got.append(f)
        if len(got) == 2:
            pairs.append((got[0], got[1]))
    return pairs


def validate_video_file(video_path, sample=SAMPLE):
    tmp = Path(tempfile.mkdtemp(prefix="lv_gate_"))
    try:
        frames = sample_video_frames(video_path, tmp, sample)
        if not frames:
            return {"verdict": "FAIL", "reasons": ["could not decode any frames"],
                    "frames": 0, "checks": {}}
        dur = _video_duration(video_path)
        pair_list = _video_pairs(video_path, tmp, dur) or None
        res = analyze_set(frames, sample, pair_list=pair_list)
        res["source"] = str(video_path)
        return res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def gate_summary(res):
    """Slim JSON-safe dict for HTTP responses (drops per_frame detail)."""
    return {k: res.get(k) for k in
            ("verdict", "reasons", "frames", "checks", "splat_eligible", "source")}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--video"); ap.add_argument("--dir"); ap.add_argument("--vin")
    ap.add_argument("--sample", type=int, default=SAMPLE)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.video:
        res = validate_video_file(a.video, a.sample)
    elif a.dir:
        res = validate_dir(a.dir, a.sample)
    elif a.vin:
        veh = STORAGE / a.vin
        src = next((veh / d for d in ("raw", "exterior", "spin_hd", "spin", "photos")
                    if (veh / d).exists()), None)
        if not src:
            print("FAIL no source frames"); sys.exit(2)
        res = validate_dir(src, a.sample)
    else:
        ap.error("need --video, --dir or --vin")
    if a.json:
        print(json.dumps(gate_summary(res), indent=2))
    else:
        c = res.get("checks", {})
        print(f"{res['verdict']}  frames={res.get('frames')}  "
              f"bg_motion={c.get('bg_motion')}  aspect_range={c.get('aspect_range')}  "
              f"transitions={c.get('coverage_transitions')}  "
              f"crop_ratio={c.get('crop_ratio')}  present={c.get('present_ratio')}")
        for r in res.get("reasons", []):
            print("  REASON: " + r)
    sys.exit(0 if res["verdict"] == "PASS" else 1)
