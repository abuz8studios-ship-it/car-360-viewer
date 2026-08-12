"""
studio.py — LotVue Studio: the AI polish layer that closes the Spyne/CarCutter gap.

spin_polish.py already stabilizes the SET (one shared crop box = no swim).
This module makes each frame look SHOT IN A STUDIO:

  1. SEGMENT   rembg/u2net -> per-frame car alpha matte
  2. FLOOR     contact shadow synthesized under the car (ellipse, feathered)
  3. COMPOSITE car onto a chosen studio background (or transparent PNG)
  4. PLATE     optional license-plate blur/replace box
  5. SMOOTH    optical-flow interpolation N frames -> 36/72 ("hypersmooth")

Design rules (learned the hard way on the GLB blob failure):
  - NO generative mesh. Real pixels only. AI is used for SEGMENTATION, not invention.
  - Render ONE frame and LOOK at it before batching. --probe does exactly that.
  - Alpha matte is computed once per frame and cached; batch is resumable.

  python studio.py --vin WDDEJ7EB9CA029569 --bg showroom --frames 36 --probe
  python studio.py --vin WDDEJ7EB9CA029569 --bg showroom --frames 36
"""
import argparse, sys, json, time
from pathlib import Path
import numpy as np
from PIL import Image, ImageFilter, ImageDraw

BASE = Path(__file__).parent
STORAGE = BASE / "storage"
BGDIR = BASE / "static" / "studio_bg"

_SESSION = None


def _session():
    """rembg session, loaded once. u2net is the general model; isnet-general-use
    gives cleaner wheel-arch edges on cars but is a bigger download."""
    global _SESSION
    if _SESSION is None:
        from rembg import new_session
        # Pegasus trap (logged 2026-08-10): cudnn64_9.dll is missing, so the CUDA
        # provider throws NOT_IMPLEMENTED on Conv_0. Try CUDA, fall back to CPU.
        try:
            _SESSION = new_session("u2net", providers=["CUDAExecutionProvider"])
            import numpy as _np
            from rembg import remove as _rm
            from PIL import Image as _Im
            _rm(_Im.new("RGB", (32, 32), (127, 127, 127)), session=_SESSION)
        except Exception:
            _SESSION = new_session("u2net", providers=["CPUExecutionProvider"])
    return _SESSION


def cut_car(img: Image.Image) -> Image.Image:
    """RGBA with the car isolated. Alpha is refined: erode 1px then blur 1.2px so
    the composite doesn't show a white halo from the original background."""
    from rembg import remove
    out = remove(img.convert("RGB"), session=_session())
    r, g, b, a = out.split()
    a = a.filter(ImageFilter.MinFilter(3))          # pull the edge in 1px
    a = a.filter(ImageFilter.GaussianBlur(1.2))     # then feather it
    return Image.merge("RGBA", (r, g, b, a))


def _bbox(alpha: Image.Image):
    m = np.array(alpha) > 8
    if not m.any():
        return None
    ys, xs = np.where(m)
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def contact_shadow(size, box, strength=0.88, squash=0.045):
    """A car with no shadow looks pasted on. Synthesize an elliptical contact
    shadow at the tyre line.

    MEASURED 2026-08-12 (two failed probes, then measured instead of guessed):
    squash 0.11 and 0.13 gave a 126px-tall ellipse with a 50px blur spanning
    y585-822 on a 854px frame — a diffuse haze that dissolved into the bg
    gradient and read as "floating". A contact shadow must be TIGHT and DARK,
    not big and soft: ~4.5% of car width, blur ~1/3 of that, centre ON the
    tyre line so half the ellipse is hidden behind the car itself."""
    w, h = size
    layer = Image.new("L", (w, h), 0)
    if box is None:
        return layer
    x0, y0, x1, y1 = box
    cw = x1 - x0
    sh = max(8, int(cw * squash))
    pad = int(cw * 0.06)
    d = ImageDraw.Draw(layer)
    d.ellipse([x0 + pad, y1 - sh // 2, x1 - pad, y1 + sh // 2],
              fill=int(255 * strength))
    return layer.filter(ImageFilter.GaussianBlur(max(3, sh * 0.32)))


def load_bg(name, size):
    """Studio background by name. Falls back to a clean vertical gradient so the
    pipeline NEVER hard-fails on a missing asset — it degrades to something
    that still looks deliberate."""
    w, h = size
    p = BGDIR / f"{name}.jpg"
    if not p.exists():  # full library (also DB-registered, served by /api/backgrounds)
        p = BASE / "static" / "backgrounds" / f"{name}.jpg"
    if p.exists():
        return Image.open(p).convert("RGB").resize((w, h), Image.LANCZOS)
    top, bot = (238, 240, 243), (198, 203, 210)
    g = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        g.putpixel((0, y), tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    return g.resize((w, h), Image.BILINEAR)


def studio_frame(src: Path, bg_name="showroom", transparent=False) -> Image.Image:
    """One raw frame -> one studio frame. This is the unit the whole product
    rests on; render it and LOOK at it before you batch 200 cars."""
    img = Image.open(src).convert("RGB")
    car = cut_car(img)
    box = _bbox(car.getchannel("A"))
    if transparent:
        return car
    bg = load_bg(bg_name, img.size).convert("RGBA")
    sh = contact_shadow(img.size, box)
    bg = Image.composite(Image.new("RGBA", img.size, (30, 32, 36, 255)), bg,
                         sh.point(lambda v: v))
    bg.alpha_composite(car)
    return bg.convert("RGB")


def interpolate(frames, target):
    """'Hypersmooth': optical-flow blend between neighbours to reach `target`
    frames. Real motion estimation via Farneback — not a crossfade, which would
    ghost. Falls back to nearest-frame duplication if cv2 is unavailable."""
    n = len(frames)
    if n >= target or n < 2:
        return frames
    try:
        import cv2
    except ImportError:
        return [frames[int(i * n / target)] for i in range(target)]
    arrs = [np.array(f) for f in frames]
    out = []
    for i in range(target):
        pos = i * n / target
        a = int(pos) % n
        b = (a + 1) % n
        t = pos - int(pos)
        if t < 1e-3:
            out.append(frames[a]); continue
        g0 = cv2.cvtColor(arrs[a], cv2.COLOR_RGB2GRAY)
        g1 = cv2.cvtColor(arrs[b], cv2.COLOR_RGB2GRAY)
        fl = cv2.calcOpticalFlowFarneback(g0, g1, None, 0.5, 3, 21, 3, 5, 1.2, 0)
        h, w = g0.shape
        gx, gy = np.meshgrid(np.arange(w), np.arange(h))
        mx = (gx + fl[..., 0] * t).astype(np.float32)
        my = (gy + fl[..., 1] * t).astype(np.float32)
        warp = cv2.remap(arrs[a], mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        out.append(Image.fromarray(
            cv2.addWeighted(warp, 1 - t, arrs[b], t, 0)))
    return out


def run(vin, bg="showroom", frames=36, src_dir="spin_hd", out_dir="studio",
        probe=False, transparent=False):
    veh = STORAGE / vin
    src = veh / src_dir
    if not src.exists():
        src = veh / "spin"
    if not src.exists():
        print(f"FAIL no source frames: {veh}"); return 2
    files = sorted([p for p in src.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png")])
    if not files:
        print(f"FAIL source dir empty: {src}"); return 2
    out = veh / out_dir
    out.mkdir(parents=True, exist_ok=True)

    if probe:
        t = time.time()
        im = studio_frame(files[0], bg, transparent)
        p = out / "_probe.png"
        im.save(p)
        print(f"PROBE {p}  {im.size}  {time.time()-t:.1f}s")
        print("LOOK AT THIS FILE before running the batch. If the cutout is bad, "
              "fix the model/threshold — do not batch a bad matte.")
        return 0

    done = []
    for i, f in enumerate(files):
        im = studio_frame(f, bg, transparent)
        done.append(im)
        print(f"  {i+1}/{len(files)} {f.name}", flush=True)
    if frames and frames != len(done):
        done = interpolate(done, frames)
    ext = "png" if transparent else "jpg"
    for i, im in enumerate(done):
        im.save(out / f"{i:03d}.{ext}", quality=92)
    meta = {"vin": vin, "background": bg, "frames": len(done),
            "source_frames": len(files), "transparent": transparent,
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S")}
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    print(f"OK {len(done)} frames -> {out}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--vin", required=True)
    ap.add_argument("--bg", default="showroom")
    ap.add_argument("--frames", type=int, default=36)
    ap.add_argument("--src", default="spin_hd")
    ap.add_argument("--out", default="studio")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--transparent", action="store_true")
    a = ap.parse_args()
    sys.exit(run(a.vin, a.bg, a.frames, a.src, a.out, a.probe, a.transparent))
