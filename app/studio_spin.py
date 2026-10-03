"""
studio_spin.py — UNIVERSAL Spyne-clone lane: any dealer photo gallery in,
studio-background 360 spin video out.

Why this exists (measured 2026-08-12): the imported "spin" dirs are NOT orbit
sequences — they are full dealer galleries (exterior angles + interiors +
engine bay + wheel/mirror close-ups, filename-ordered). Compositing them
blindly produces a chaotic slideshow. Spyne's pipeline classifies exterior
full-car shots, orders them by angle, and builds the 360 from those only.
So does this.

STAGES (each measurable, none generative):
  1. SELECT   CLIP zero-shot exterior/interior/detail (clip_classify.py —
              proven 5/5 on the eye-audited truth set; the pure-geometric
              filter measured 22% error and let banner/trunk frames through)
              + matte-geometry backstop on the exteriors
  2. ORDER    greedy ORB-adjacency chain (same overlap metric as the gate)
  3. COMPOSE  studio.studio_frame -> chosen background + contact shadow
  4. RENDER   STEPPED spin (each real angle held) -> ffmpeg mp4. NO optical-
              flow interpolation across gallery angle gaps — flow between
              45-90 degree views ghosts (measured again 2026-08-12; the
              double-exposure contact sheet is in rnd/_gate_probe).

  python studio_spin.py --vin 5LMJJ2KT0LEL00884 --bg showroom_floor
  python studio_spin.py --dir path\to\any\dealer\photos --vin DEMO1 --bg studio_dark
"""
import argparse, json, os, shutil, subprocess, sys, tempfile, time
from pathlib import Path
import numpy as np
from PIL import Image

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
from studio import cut_car, _bbox, studio_frame, STORAGE
from validate_video import _orb_inliers, _resolve_tool

FFMPEG = _resolve_tool("FFMPEG_PATH", "ffmpeg.exe")

# Use an optional isolated interpreter for CLIP; otherwise reuse this runtime.
_isolated_python = os.getenv("LOTVUE_PY_ISOLATED", "").strip()
ISOLATED_PY = _isolated_python if _isolated_python and Path(_isolated_python).is_file() else sys.executable


def clip_exteriors(files):
    """Run the proven CLIP classifier over the gallery; return the exterior
    subset (original paths). Falls back to 'all' only if CLIP itself fails —
    and says so loudly."""
    tmp = Path(tempfile.mkdtemp(prefix="lv_clip_"))
    try:
        mapping = {}
        for i, p in enumerate(files):
            name = f"p_{i+1:04d}.jpg"
            shutil.copyfile(p, tmp / name)
            mapping[name] = p
        r = subprocess.run([ISOLATED_PY, "-s", str(BASE / "clip_classify.py"), str(tmp)],
                           capture_output=True, text=True, timeout=900)
        data = json.loads(r.stdout.strip().splitlines()[-1])
        if not data.get("ok"):
            raise RuntimeError(data.get("error", "classifier failed"))
        keep, cats = [], {}
        for ph in data["photos"]:
            cats[ph["file"]] = ph["category"]
            if ph["category"] == "exterior":
                keep.append(mapping[ph["file"]])
        return keep, cats, None
    except Exception as e:
        return list(files), {}, f"CLIP unavailable ({e}) — geometric filter only"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

# Full-car exterior filter (tuned on Navigator/Accord/Outback/CL63 galleries,
# 2026-08-12 — every threshold checked against a rendered contact sheet)
ASPECT_MIN, ASPECT_MAX = 1.15, 4.2   # wheels/badges ~1.0; full car 1.2-3.6
AREA_MIN, AREA_MAX = 0.08, 0.62     # close-ups fill the frame; distant = tiny
MIN_KEEP = 6                         # fewer usable angles than this = refuse
SAT_MIN = 20.0        # MEASURED: matte-ghost frame sat 8.8, real cars 42-71
FULLCAR_SCORE_MIN = 0.92  # MEASURED: clean orbit >=0.927, partials <=0.905


def frame_facts(path):
    img = Image.open(path).convert("RGB")
    m = np.array(cut_car(img).getchannel("A")) > 24
    if not m.any():
        return {"file": str(path), "keep": False, "why": "no subject"}
    h, w = m.shape
    ys, xs = np.where(m)
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    area = float(m.mean())
    aspect = (x1 - x0) / max(1, (y1 - y0))
    px = np.asarray(img, dtype=np.float32)[m]
    sat = float((px.max(axis=1) - px.min(axis=1)).mean())
    # close-ups / interiors: subject swallows the frame or is not car-shaped
    bbox_frac_w = (x1 - x0) / w
    keep, why = True, "ok"
    if not (ASPECT_MIN <= aspect <= ASPECT_MAX):
        keep, why = False, f"aspect {aspect:.2f}"
    elif not (AREA_MIN <= area <= AREA_MAX):
        keep, why = False, f"area {area:.2f}"
    elif bbox_frac_w > 0.985 and area > 0.5:
        keep, why = False, "fills frame (close-up)"
    return {"file": str(path), "keep": keep, "why": why, "sat": round(sat, 1),
            "aspect": round(aspect, 2), "area": round(area, 3)}


def clip_fullcar_filter(files):
    """Second CLIP pass: whole-car vs part/close-up/doors-open/ghost. This is
    a SEMANTIC call — set-relative shape statistics measurably failed (they
    pruned real side views and kept headlight close-ups, 2026-08-12)."""
    tmp = Path(tempfile.mkdtemp(prefix="lv_full_"))
    try:
        mapping = {}
        for i, p in enumerate(files):
            name = f"p_{i+1:04d}.jpg"
            shutil.copyfile(p, tmp / name)
            mapping[name] = p
        r = subprocess.run([ISOLATED_PY, "-s", str(BASE / "clip_fullcar.py"), str(tmp)],
                           capture_output=True, text=True, timeout=900)
        data = json.loads(r.stdout.strip().splitlines()[-1])
        if not data.get("ok"):
            raise RuntimeError(data.get("error", "fullcar classifier failed"))
        # CLIP absolute scores do NOT transfer across galleries (measured
        # 2026-08-12: CL63 clean cluster >=0.927, Accord clean frames 0.67-0.72;
        # the pairwise doors test misfired on closed-door sedans). So the bar
        # is SET-RELATIVE: 0.72x this gallery's median, floored at 0.5.
        scores = [ph["score"] for ph in data["photos"]]
        med = sorted(scores)[len(scores) // 2] if scores else 0
        bar = max(0.5, 0.72 * med)
        keep, dropped = [], []
        for ph in data["photos"]:
            if ph["score"] < bar:
                dropped.append((mapping[ph["file"]].name,
                                f"part/close-up score {ph['score']} < bar {bar:.2f}"))
            else:
                keep.append(mapping[ph["file"]])
        return keep, dropped, None
    except Exception as e:
        return list(files), [], f"fullcar CLIP unavailable ({e})"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


CHAIN_LINK_MIN = 25  # a frame whose best ORB link is weaker than this is not
                     # part of the same physical orbit (ghosts, stray shots)


def order_by_orb(files):
    """Greedy adjacency chain: start anywhere, repeatedly hop to the unvisited
    frame with the most RANSAC inliers — reconstructs the walk order the
    photographer actually took, whatever the filenames say. Frames that fail
    to link into the chain (best link < CHAIN_LINK_MIN) are dropped."""
    n = len(files)
    if n <= 2:
        return files, []
    sim = np.zeros((n, n), dtype=int)
    for i in range(n):
        for j in range(i + 1, n):
            sim[i, j] = sim[j, i] = _orb_inliers(files[i], files[j])
    best = sim.max(axis=1)
    # Chain-link prune only makes sense on DENSE sets. A sparse guided gallery
    # (8 shots ~45deg apart) has legitimately weak adjacent overlap — measured
    # 2026-08-12: Outback real angles best-linked at 10-20 inliers.
    if n <= 12:
        linked, unlinked = list(range(n)), []
    else:
        linked = [i for i in range(n) if best[i] >= CHAIN_LINK_MIN]
        unlinked = [(files[i].name, f"best link {int(best[i])} inliers") for i in range(n)
                    if best[i] < CHAIN_LINK_MIN]
    if len(linked) < 3:
        return files, []  # too few to judge — keep all, let the human look
    start = linked[int(np.argmax(sim[np.ix_(linked, linked)].sum(axis=1)))]
    order, seen = [start], {start}
    while len(order) < len(linked):
        last = order[-1]
        nxt = max((j for j in linked if j not in seen), key=lambda j: sim[last, j])
        order.append(nxt); seen.add(nxt)
    return [files[i] for i in order], unlinked


def run(vin, src_dir=None, bg="showroom_floor", out_frames=36, fps=4, keep_json=True):
    t0 = time.time()
    if src_dir:
        src = Path(src_dir)
    else:
        veh = STORAGE / vin
        # _import = the ORIGINAL dealer gallery. Derived dirs (spin/spin_hd)
        # may contain July flow/WAN-generated ghost frames — never mine them.
        src = next((veh / d for d in ("_import", "spin_hd", "spin", "photos", "raw")
                    if (veh / d).exists()), None)
    if not src or not src.exists():
        print("FAIL no source dir"); return 2
    files = sorted(p for p in src.iterdir()
                   if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
    if not files:
        print("FAIL empty source"); return 2

    # 1a. CLIP semantic pass (interiors / engine bays / details / banners out)
    ext_files, cats, clip_err = clip_exteriors(files)
    if clip_err:
        print(f"[select] WARNING: {clip_err}")
    else:
        n_cat = {c: list(cats.values()).count(c) for c in set(cats.values())}
        print(f"[select] CLIP: {n_cat} -> {len(ext_files)} exterior")
    # 1b. matte-geometry backstop on the exteriors
    facts = [frame_facts(p) for p in ext_files]
    kept_facts = [f for f in facts if f["keep"]]
    kept = [Path(f["file"]) for f in kept_facts]
    dropped = [(Path(f['file']).name, f['why']) for f in facts if not f['keep']]
    print(f"[select] geometry backstop: {len(kept)}/{len(ext_files)} kept; dropped: {dropped}")
    # 1b2. SET-RELATIVE saturation prune. Absolute sat fails (silver cars 10-20
    # overlap matte ghosts ~9); relative to the set's own median it separates:
    # a ghost sits far below its set (8.8 vs blue-car set ~55), a silver car IS
    # its set's median. Measured 2026-08-12.
    if len(kept) >= 4:
        sats = sorted(f["sat"] for f in kept_facts if f["keep"])
        med_sat = sats[len(sats) // 2]
        sat_bar = 0.35 * med_sat
        ghosts = [(p.name, f"sat {f['sat']} < 0.35x set median {med_sat:.0f}")
                  for p, f in zip(kept, kept_facts) if f["sat"] < sat_bar]
        if ghosts:
            keep_pairs = [(p, f) for p, f in zip(kept, kept_facts) if f["sat"] >= sat_bar]
            kept = [p for p, _ in keep_pairs]
            print(f"[select] saturation prune: dropped {ghosts}")
            dropped += ghosts
    # 1c. semantic whole-car pass (close-ups / doors-open / matte ghosts out)
    kept, out_dropped, fc_err = clip_fullcar_filter(kept)
    if fc_err:
        print(f"[select] WARNING: {fc_err}")
    print(f"[select] whole-car CLIP: {len(kept)} kept; dropped: {out_dropped}")
    dropped += out_dropped
    if len(kept) < MIN_KEEP:
        print(f"FAIL only {len(kept)} usable exterior angles (< {MIN_KEEP})"); return 1

    ordered, unlinked = order_by_orb(kept)
    if unlinked:
        print(f"[order] dropped unlinked frames: {unlinked}")
        dropped += unlinked
    print(f"[order] ORB chain: {[p.name for p in ordered]}")
    if len(ordered) < MIN_KEEP:
        print(f"FAIL only {len(ordered)} chained angles (< {MIN_KEEP})"); return 1

    out = STORAGE / vin / "studio_spin"
    if out.exists():
        for old in out.glob("*.jpg"):
            old.unlink()
    out.mkdir(parents=True, exist_ok=True)
    for i, p in enumerate(ordered):
        studio_frame(p, bg).convert("RGB").save(out / f"{i:03d}.jpg", quality=92)
        print(f"[compose] {i+1}/{len(ordered)} {p.name}", flush=True)

    # STEPPED spin: hold each real angle (no cross-angle flow — it ghosts)
    mp4 = out / f"{bg}.mp4"
    r = subprocess.run([FFMPEG, "-y", "-framerate", str(fps),
                        "-i", str(out / "%03d.jpg"),
                        "-vf", "scale=1280:-2:flags=lanczos,format=yuv420p",
                        "-c:v", "libx264", "-crf", "19", "-loglevel", "error",
                        str(mp4)], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        print("FAIL ffmpeg:", r.stderr[:300]); return 1
    if keep_json:
        (out / "meta.json").write_text(json.dumps(
            {"vin": vin, "bg": bg, "source": str(src), "gallery_frames": len(files),
             "clip_categories": cats and {k: cats[k] for k in sorted(cats)} or "unavailable",
             "kept_exterior": len(kept), "dropped_geometry": dropped, "mp4": mp4.name,
             "generated": time.strftime("%Y-%m-%dT%H:%M:%S")}, indent=2))
    print(f"OK {mp4}  ({mp4.stat().st_size/1e6:.1f}MB, {len(kept)} angles, "
          f"{time.time()-t0:.0f}s)")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--vin", required=True)
    ap.add_argument("--dir", help="any folder of dealer photos (else storage/<vin>)")
    ap.add_argument("--bg", default="showroom_floor")
    ap.add_argument("--frames", type=int, default=36)
    ap.add_argument("--fps", type=int, default=4)  # stepped spin: ~0.25s/angle
    a = ap.parse_args()
    sys.exit(run(a.vin, a.dir, a.bg, a.frames, a.fps))
