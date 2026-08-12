"""
validate.py — the INPUT GATE. Modeled on Spyne's /360/v1/validate-video, which
checks a submitted video for Cropping, Angle, and Car Present BEFORE processing.

Why this file exists: on 2026-08-11 we generated 68 GLB meshes before looking at
one, and every one was a blob. The cause was bad INPUT (dealer 3/4 marketing
shots fed to a model needing 4 orthogonal views). A gate at the front would have
cost 30 seconds and saved the whole batch. Spyne runs exactly such a gate. So do we.

CHECKS (all measured, none inferred):
  CAR_PRESENT  segmentation finds a subject covering a plausible area fraction
  CROPPING     subject does not touch frame edges (a cut-off car ruins a spin)
  ANGLE        across the set, the subject's aspect ratio must VARY — a real
               walkaround changes the car's silhouette; a static/synthetic
               turntable of the same angle does not. This is the check that
               would have caught the synthetic-turntable COLMAP failure.
  EXPOSURE     frame is not blown out or crushed (mean luma sanity)

  python validate.py --vin WDDEJ7EB9CA029569
  python validate.py --dir path\to\frames --json
"""
import argparse, json, sys
from pathlib import Path
import numpy as np
from PIL import Image

BASE = Path(__file__).parent
STORAGE = BASE / "storage"

MIN_AREA = 0.06      # subject smaller than 6% of frame = car too far / absent
MAX_AREA = 0.92      # larger than 92% = extreme crop, no room for a spin box
EDGE_TOL = 2         # px from frame edge counted as "touching"
AR_SPREAD_MIN = 0.10 # min spread in subject aspect ratio across the set


def _subject_mask(path):
    """Alpha matte of the main subject, reusing studio.py's tuned cutter so the
    gate and the renderer agree on what 'the car' is."""
    from studio import cut_car
    img = Image.open(path).convert("RGB")
    rgba = cut_car(img)
    a = np.array(rgba.getchannel("A")) > 24
    return a, img


def check_frame(path):
    """One frame -> dict of measured facts + per-frame verdict."""
    m, img = _subject_mask(path)
    h, w = m.shape
    r = {"file": Path(path).name, "size": [w, h]}
    area = float(m.mean())
    r["area_fraction"] = round(area, 4)
    if not m.any():
        r.update(car_present=False, verdict="FAIL", reason="no subject found")
        return r
    ys, xs = np.where(m)
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    r["bbox"] = [x0, y0, x1, y1]
    r["car_present"] = MIN_AREA <= area <= MAX_AREA
    touch = [n for n, c in (("left", x0 <= EDGE_TOL), ("right", x1 >= w - 1 - EDGE_TOL),
                            ("top", y0 <= EDGE_TOL), ("bottom", y1 >= h - 1 - EDGE_TOL))
             if c]
    # bottom contact is normal (car sits on ground); only sides/top are crops
    r["cropped_edges"] = [t for t in touch if t != "bottom"]
    r["aspect"] = round((x1 - x0) / max(1, (y1 - y0)), 3)
    luma = float(np.array(img.convert("L")).mean())
    r["luma"] = round(luma, 1)
    fails = []
    if not r["car_present"]:
        fails.append(f"area {area:.1%} outside {MIN_AREA:.0%}-{MAX_AREA:.0%}")
    if r["cropped_edges"]:
        fails.append("cropped: " + ",".join(r["cropped_edges"]))
    if luma < 28 or luma > 232:
        fails.append(f"exposure {luma:.0f}")
    r["verdict"] = "PASS" if not fails else "FAIL"
    r["reason"] = "; ".join(fails)
    return r


def validate_set(frames, sample=8):
    """Set-level gate. Samples evenly across the set (full-set matting is slow
    and unnecessary — the failures we care about are systemic, not per-frame)."""
    frames = sorted(frames)
    n = len(frames)
    if n < 2:
        return {"verdict": "FAIL", "reason": "need >= 2 frames", "frames": n}
    idx = sorted(set(int(i * n / min(sample, n)) for i in range(min(sample, n))))
    per = [check_frame(frames[i]) for i in idx]
    aspects = [p["aspect"] for p in per if "aspect" in p]
    spread = round(max(aspects) - min(aspects), 3) if aspects else 0.0
    fails = [p for p in per if p["verdict"] == "FAIL"]
    out = {"frames": n, "sampled": len(per), "aspect_spread": spread,
           "failed_samples": len(fails), "per_frame": per}
    reasons = []
    if fails:
        reasons.append(f"{len(fails)}/{len(per)} sampled frames failed")
    if spread < AR_SPREAD_MIN:
        reasons.append(
            f"angle spread {spread} < {AR_SPREAD_MIN} — the silhouette barely "
            "changes across the set. This is the synthetic-turntable signature "
            "(car rotates, camera static). Photo-spin is fine; do NOT send this "
            "to COLMAP/splat — it is geometrically unsolvable.")
    out["angle_ok"] = spread >= AR_SPREAD_MIN
    out["splat_eligible"] = spread >= AR_SPREAD_MIN and not fails
    out["verdict"] = "PASS" if not reasons else "FAIL"
    out["reason"] = " | ".join(reasons)
    return out


def run(vin=None, dirpath=None, as_json=False, sample=8):
    if dirpath:
        src = Path(dirpath)
    else:
        veh = STORAGE / vin
        src = next((veh / d for d in ("spin_hd", "spin", "photos") if (veh / d).exists()), None)
    if not src or not src.exists():
        print("FAIL no source frames"); return 2
    files = [p for p in src.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png")]
    if not files:
        print(f"FAIL empty: {src}"); return 2
    res = validate_set(files, sample)
    res["source"] = str(src)
    if as_json:
        print(json.dumps(res, indent=2))
    else:
        print(f"{res['verdict']}  {res['frames']} frames  "
              f"aspect_spread={res['aspect_spread']}  "
              f"splat_eligible={res['splat_eligible']}")
        if res["reason"]:
            print("  " + res["reason"])
        for p in res["per_frame"]:
            if p["verdict"] == "FAIL":
                print(f"  FAIL {p['file']}: {p['reason']}")
    return 0 if res["verdict"] == "PASS" else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--vin")
    ap.add_argument("--dir")
    ap.add_argument("--sample", type=int, default=8)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if not a.vin and not a.dir:
        ap.error("need --vin or --dir")
    sys.exit(run(a.vin, a.dir, a.json, a.sample))
