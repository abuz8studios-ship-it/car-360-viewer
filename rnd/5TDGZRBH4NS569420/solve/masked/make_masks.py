"""Config 3 prep: rembg-isolate each curated photo onto white, keeping car + ground shadow."""
import sys, numpy as np
from pathlib import Path
from PIL import Image, ImageOps
from rembg import remove, new_session
from scipy import ndimage

SRC = Path(r"E:\ABU\360Car\rnd\5TDGZRBH4NS569420\solve\seq-loop\images")
OUT = Path(r"E:\ABU\360Car\rnd\5TDGZRBH4NS569420\solve\masked\images")
OUT.mkdir(parents=True, exist_ok=True)

sess = None
for m in ("isnet-general-use", "u2net", "u2netp"):
    try:
        sess = new_session(m); print("model:", m); break
    except Exception as e:
        print("model fail", m, e)

for f in sorted(SRC.glob("frame_*.jpg")):
    im = ImageOps.exif_transpose(Image.open(f)).convert("RGB")
    arr = np.asarray(im)
    a = np.array(remove(im, session=sess))[:, :, 3]
    keep = a > 15
    ys, xs = np.where(keep)
    if len(xs) < 200:
        im.save(OUT / f.name, "JPEG", quality=97); print(f.name, "no mask, kept raw"); continue
    x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
    # dilate mask ~25px so car silhouette edges + contact shadow fringe survive
    keep = ndimage.binary_dilation(keep, iterations=25)
    # ground/shadow band: strip under the car footprint (bbox bottom -> +9% img height)
    h, w = keep.shape
    band_top = max(0, y1 - int(0.02 * h)); band_bot = min(h, y1 + int(0.09 * h))
    bx0 = max(0, x0 - int(0.03 * w)); bx1 = min(w, x1 + int(0.03 * w))
    keep[band_top:band_bot, bx0:bx1] = True
    out = np.full_like(arr, 255)
    out[keep] = arr[keep]
    Image.fromarray(out).save(OUT / f.name, "JPEG", quality=97)
    print(f.name, "masked, car bbox", (int(x0), int(y0), int(x1), int(y1)))
print("done")
