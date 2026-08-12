"""
bgpack_qa.py — P1a verification: render ONE real vehicle frame against EVERY
background in static/backgrounds at FULL resolution, measure white-halo
contamination on the matte edge ring, and emit 100%-zoom edge crops for visual
inspection. Never QA from a thumbnail (2026-08-12 lesson).

  python bgpack_qa.py --vin WDDEJ7EB9CA029569 [--src spin_hd]
Outputs -> rnd/_bgpack_qa/: <bg>.jpg (full res), crop_<bg>.jpg (100% roofline),
sheet_*.jpg (contact sheets), report.json
"""
import argparse, json, sys
from pathlib import Path
import numpy as np
from PIL import Image, ImageFilter

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
from studio import cut_car, _bbox, contact_shadow, load_bg, BGDIR, STORAGE

OUT = BASE.parent / "rnd" / "_bgpack_qa"


def composite(car_rgba, box, bg_name, size):
    """EXACT studio_frame composite path, with the matte precomputed."""
    bg = load_bg(bg_name, size).convert("RGBA")
    sh = contact_shadow(size, box)
    bg = Image.composite(Image.new("RGBA", size, (30, 32, 36, 255)), bg,
                         sh.point(lambda v: v))
    bg.alpha_composite(car_rgba)
    return bg.convert("RGB")


def edge_ring(alpha, r=3):
    a = alpha.point(lambda v: 255 if v > 24 else 0)
    grow = a.filter(ImageFilter.MaxFilter(2 * r + 1))
    shrink = a.filter(ImageFilter.MinFilter(2 * r + 1))
    ring = np.array(grow, bool) & ~np.array(shrink, bool)
    return ring


def halo_metric(comp, bg_name, size, ring):
    L = np.asarray(comp.convert("L"), dtype=np.float32)
    bgL = np.asarray(load_bg(bg_name, size).convert("L"), dtype=np.float32)
    bad = ring & (L > 235) & (bgL < 210)
    return float(bad.sum()) / max(1, int(ring.sum()))


def main(vin, src_dir):
    OUT.mkdir(parents=True, exist_ok=True)
    src = sorted((STORAGE / vin / src_dir).glob("*.jpg"))
    if not src:
        print(f"FAIL no frames in {src_dir}"); return 2
    img = Image.open(src[0]).convert("RGB")
    car = cut_car(img)
    box = _bbox(car.getchannel("A"))
    ring = edge_ring(car.getchannel("A"))
    x0, y0, x1, y1 = box
    bgs = sorted({p.stem for d in (BGDIR, BASE / "static" / "backgrounds")
                  if d.exists() for p in d.glob("*.jpg")})
    report = {}
    for name in bgs:
        comp = composite(car, box, name, img.size)
        comp.save(OUT / f"{name}.jpg", quality=92)
        h = halo_metric(comp, name, img.size, ring)
        report[name] = round(h, 5)
        # 100% roofline + side edge crop (top third of bbox, full bbox width)
        cw = min(img.size[0], x1 - x0 + 160)
        crop = comp.crop((max(0, x0 - 80), max(0, y0 - 60),
                          max(0, x0 - 80) + cw, min(img.size[1], y0 + (y1 - y0) // 3)))
        crop.save(OUT / f"crop_{name}.jpg", quality=92)
        print(f"{name}: halo={h:.4%}")
    # contact sheets, 8 per sheet, 640px wide each
    thumbs = [(n, Image.open(OUT / f"{n}.jpg").resize((640, 360))) for n in bgs]
    for s in range(0, len(thumbs), 8):
        chunk = thumbs[s:s + 8]
        sheet = Image.new("RGB", (1280, 360 * ((len(chunk) + 1) // 2)), (12, 12, 14))
        for i, (n, t) in enumerate(chunk):
            sheet.paste(t, ((i % 2) * 640, (i // 2) * 360))
        sheet.save(OUT / f"sheet_{s//8}.jpg", quality=88)
    (OUT / "report.json").write_text(json.dumps(
        {"vin": vin, "frame": src[0].name, "size": list(img.size),
         "halo_by_bg": report}, indent=2))
    worst = sorted(report.items(), key=lambda kv: -kv[1])[:5]
    print("WORST:", worst)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--vin", required=True)
    ap.add_argument("--src", default="spin_hd")
    a = ap.parse_args()
    sys.exit(main(a.vin, a.src))
