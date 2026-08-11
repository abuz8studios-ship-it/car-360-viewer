"""
spin_polish.py — turn raw dealer frames into a GLO3D-grade spin sequence.

Per frame:  strip white letterbox -> strip dealer banner -> find the car ->
Per set:    ONE shared crop box (union of all car boxes, aspect-locked) so the
            car never jumps between frames -> resize -> save.

That shared box is the whole trick: cropping each frame independently makes the
car swim around during the spin. One box for the set = a locked-off turntable.

  python spin_polish.py --vin WDDEJ7EB9CA029569 [--out spin_hd] [--ar 1.6]
"""
import argparse, sys
from pathlib import Path
import numpy as np
from PIL import Image

BASE = Path(__file__).parent
STORAGE = BASE / "storage"


def _strip_white(a):
    """Drop near-white rows/cols at the edges (letterbox baked into the JPEG)."""
    g = a.mean(2)
    colw = (g > 244).mean(0)          # fraction of pixels white per column
    roww = (g > 244).mean(1)
    def edge(v):
        n = len(v)
        lo = 0
        while lo < n and v[lo] > 0.94: lo += 1
        hi = n - 1
        while hi > lo and v[hi] > 0.94: hi -= 1
        return lo, hi + 1
    x0, x1 = edge(colw)
    y0, y1 = edge(roww)
    return x0, y0, x1, y1


def _banner_y(samples):
    """A dealer ad bar is BURNED IN: identical pixels in every frame. The photo
    above it changes as the car turns. So: rows that are ~identical across
    sample frames, contiguous from the bottom, are the banner.
    `samples` = list of same-shaped RGB arrays. Returns y where photo ends."""
    if len(samples) < 2:
        return samples[0].shape[0]
    h = samples[0].shape[0]
    ref = samples[0].astype(np.int16)
    diff = np.zeros(h, dtype=np.float32)
    for s in samples[1:]:
        diff = np.maximum(diff, np.abs(s.astype(np.int16) - ref).mean(axis=(1, 2)))
    static = diff < 2.0                      # row unchanged between frames
    y = h
    i = h - 1
    while i >= 0 and static[i]:
        i -= 1
    run = h - 1 - i
    if run > h * 0.04:                       # a real band, not one dark row
        y = i + 1
    return int(y)


def _car_box(a):
    """Rough vehicle bbox: saturation+darkness beats grass/sky/asphalt."""
    f = a.astype(np.float32) / 255.0
    mx, mn = f.max(2), f.min(2)
    sat = (mx - mn) / (mx + 1e-6)
    val = f.mean(2)
    # cars: dark bodies OR strongly saturated paint. background: bright, low-sat.
    m = (val < 0.42) | (sat > 0.42)
    # kill thin noise
    ys, xs = np.where(m)
    if len(xs) < 500:
        h, w = m.shape
        return 0, 0, w, h
    # use robust percentiles so a stray dark tree doesn't blow the box out
    x0, x1 = np.percentile(xs, [1.0, 99.0])
    y0, y1 = np.percentile(ys, [1.5, 99.0])
    return int(x0), int(y0), int(x1), int(y1)


def run(vin, out_name, ar, pad, width):
    vdir = STORAGE / vin
    src = vdir / "spin"
    if not src.exists():
        sys.exit(f"[polish] no spin dir for {vin}")
    files = sorted(p for p in src.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    if not files:
        sys.exit(f"[polish] no frames for {vin}")
    print(f"[polish] {vin}: {len(files)} frames")

    # ---- pass 0: white-strip geometry + burned-in banner (needs >1 frame) ----
    a0 = np.asarray(Image.open(files[0]).convert("RGB"))
    WX0, WY0, WX1, WY1 = _strip_white(a0)
    idx = [0, len(files) // 3, (2 * len(files)) // 3, len(files) - 1]
    samples = []
    for k in dict.fromkeys(idx):
        s = np.asarray(Image.open(files[k]).convert("RGB"))[WY0:WY1, WX0:WX1]
        samples.append(s)
    BY = _banner_y(samples)
    stripped = (WY1 - WY0) - BY
    print(f"[polish] white-strip -> {WX1-WX0}x{WY1-WY0}; burned-in banner: "
          f"{stripped}px {'removed' if stripped else '(none found)'}")

    # ---- pass 1: measure every frame, collect car boxes in a common space ----
    boxes, metas = [], []
    for p in files:
        a = np.asarray(Image.open(p).convert("RGB"))
        a3 = a[WY0:WY1, WX0:WX1][:BY]
        cx0, cy0, cx1, cy1 = _car_box(a3)
        metas.append((p, WX0, WY0, WX1, WY1, BY, a3.shape[1], a3.shape[0]))
        boxes.append((cx0, cy0, cx1, cy1))

    # ---- pass 1b: drop non-orbit frames -------------------------------------
    # A dealer gallery mixes walkaround angles with detail shots (door open,
    # interior, engine bay, wheel close-up). Those wreck a spin. Orbit frames
    # share a consistent car-box footprint; detail shots don't. Judge on the
    # box's area + aspect vs the set median and drop the outliers.
    B0 = np.array(boxes, dtype=np.float32)
    area = (B0[:, 2] - B0[:, 0]) * (B0[:, 3] - B0[:, 1])
    asp = (B0[:, 2] - B0[:, 0]) / np.maximum(1, B0[:, 3] - B0[:, 1])
    ma, mp = np.median(area), np.median(asp)
    keep = (area > ma * 0.55) & (area < ma * 1.5) & (asp > mp * 0.6) & (asp < mp * 1.55)
    if keep.sum() >= max(8, 0.45 * len(files)):
        dropped = int((~keep).sum())
        if dropped:
            print(f"[polish] dropped {dropped} non-orbit frames "
                  f"(detail/interior/door-open) -> {int(keep.sum())} orbit frames")
        metas = [m for m, k in zip(metas, keep) if k]
        boxes = [b for b, k in zip(boxes, keep) if k]
    else:
        print(f"[polish] orbit filter skipped (would drop too many)")

    B = np.array(boxes)
    # union of the middle 90% of boxes -> ignores one bad frame, keeps the car in
    ux0 = int(np.percentile(B[:, 0], 5))
    uy0 = int(np.percentile(B[:, 1], 5))
    ux1 = int(np.percentile(B[:, 2], 95))
    uy1 = int(np.percentile(B[:, 3], 95))
    cw, ch = ux1 - ux0, uy1 - uy0
    cxm, cym = (ux0 + ux1) / 2, (uy0 + uy1) / 2
    # pad, then force the target aspect
    cw *= (1 + pad); ch *= (1 + pad)
    if cw / ch < ar: cw = ch * ar
    else:            ch = cw / ar
    print(f"[polish] locked box {int(cw)}x{int(ch)} centred ({int(cxm)},{int(cym)})")

    out = vdir / out_name
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.jpg"):
        old.unlink()

    n = 0
    for (p, wx0, wy0, wx1, wy1, by, pw, ph) in metas:
        im = Image.open(p).convert("RGB")
        a = np.asarray(im)[wy0:wy1, wx0:wx1][:by]
        # clamp the shared box to this frame
        x0 = int(round(cxm - cw / 2)); x1 = int(round(cxm + cw / 2))
        y0 = int(round(cym - ch / 2)); y1 = int(round(cym + ch / 2))
        dx = max(0, -x0) - max(0, x1 - pw)
        dy = max(0, -y0) - max(0, y1 - ph)
        x0 += dx; x1 += dx; y0 += dy; y1 += dy
        x0 = max(0, x0); y0 = max(0, y0); x1 = min(pw, x1); y1 = min(ph, y1)
        crop = Image.fromarray(a[y0:y1, x0:x1])
        h2 = int(round(width / ar))
        crop = crop.resize((width, h2), Image.LANCZOS)
        n += 1
        crop.save(out / f"spin_{n:04d}.jpg", quality=90, optimize=True, progressive=True)

    total = sum(f.stat().st_size for f in out.glob("*.jpg")) / 1e6
    print(f"[polish] wrote {n} frames -> {out}  ({total:.1f}MB, {width}x{h2})")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--vin", required=True)
    ap.add_argument("--out", default="spin_hd")
    ap.add_argument("--ar", type=float, default=1.6)
    ap.add_argument("--pad", type=float, default=0.16)
    ap.add_argument("--width", type=int, default=1400)
    a = ap.parse_args()
    run(a.vin.upper(), a.out, a.ar, a.pad, a.width)
