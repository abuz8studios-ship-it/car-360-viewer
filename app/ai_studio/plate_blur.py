"""
LotVue360 — license-plate detection + blur.

Classic-CV detector (OpenCV only, no model download, runs on CPU in ms):

  1. CANDIDATES  black-hat morphology highlights dark strokes on a light
                 background (plate characters); a horizontal Sobel + closing
                 merges a row of characters into one blob. Also the Haar plate
                 cascades that ship with OpenCV, as a second proposal source.
  2. VERIFY      each candidate must look like a plate:
                   - plate-like aspect ratio (US 2:1 ... EU 4.7:1, perspective-tolerant)
                   - light plate background (bright median luma vs. its surroundings)
                   - 3..10 character-sized dark blobs, roughly in a row
  3. BLUR        strong Gaussian blur + pixelation over the padded box, so the
                 characters are unrecoverable (a light blur can be reversed).

detect_plates(img) -> list of (x1, y1, x2, y2)
blur_license_plate(img) -> PIL image with every detected plate blurred
blur_vehicle(storage_dir, vin) -> blurs a vehicle's published frames in place,
                                  keeping originals in <dir>/_orig_unblurred/

QA rule (project doctrine): a new step is never batched before ONE output is
inspected — run `python -m ai_studio.plate_blur --image x.jpg --debug out.jpg`
and look at the boxes before running it across a vehicle.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import numpy as np
from PIL import Image

MIN_ASPECT, MAX_ASPECT = 1.4, 6.5
MIN_CHARS, MAX_CHARS = 3, 10
MAX_TILT_DEG = 25          # plates in dealer photos are near-horizontal
MAX_AREA_FRAC = 0.015      # a plate is a small part of a vehicle photo


def _cv2():
    import cv2
    return cv2


def _char_blobs(cv2, gray_roi):
    """Count character-like dark blobs inside a candidate plate region."""
    h, w = gray_roi.shape
    if h < 8 or w < 16:
        return 0, 0.0
    # Otsu inside the region: characters are the dark class
    _, bw = cv2.threshold(gray_roi, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    n, _, stats, cents = cv2.connectedComponentsWithStats(bw, 8)
    chars = []
    for i in range(1, n):
        x, y, cw, ch, area = stats[i]
        if ch < 0.30 * h or ch > 0.95 * h:      # characters fill a good part of the plate height
            continue
        if cw > 0.35 * w or cw < 1:
            continue
        if cw / max(ch, 1) > 1.2:                # characters are taller than wide
            continue
        if area < 0.15 * cw * ch:
            continue
        chars.append((cents[i][0], cents[i][1], ch))
    if len(chars) < MIN_CHARS:
        return len(chars), 0.0
    ys = np.array([c[1] for c in chars])
    hs = np.array([c[2] for c in chars])
    # in a row: centroid y spread small relative to char height, similar heights
    rowness = 1.0 - min(1.0, (ys.std() / max(hs.mean(), 1)) + (hs.std() / max(hs.mean(), 1)))
    return len(chars), float(rowness)


def _bright_rect_candidates(cv2, rgb, gray):
    """Light, low-saturation rotated rectangles (plate background). Returns
    rotated rects ((cx, cy), (w, h), angle) — handles perspective tilt."""
    H, W = gray.shape
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    out = []
    for vmin in (150, 120):
        mask = ((hsv[..., 2] >= vmin) & (hsv[..., 1] <= 90)).astype("uint8") * 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in cnts:
            area = cv2.contourArea(c)
            if area < 0.00004 * W * H or area > MAX_AREA_FRAC * W * H:
                continue
            (cx, cy), (rw, rh), ang = cv2.minAreaRect(c)
            if rw < rh:
                rw, rh, ang = rh, rw, ang + 90
            if rh < 6 or not (MIN_ASPECT <= rw / max(rh, 1) <= MAX_ASPECT):
                continue
            if abs(((ang + 90) % 180) - 90) > MAX_TILT_DEG:
                continue
            if area / max(rw * rh, 1) < 0.55:           # rectangular
                continue
            out.append(((cx, cy), (rw, rh), ang))
    return out


def _warp(cv2, gray, rrect, pad=0.0):
    (cx, cy), (rw, rh), ang = rrect
    rw, rh = rw * (1 + pad), rh * (1 + pad)
    M = cv2.getRotationMatrix2D((cx, cy), ang, 1.0)
    rot = cv2.warpAffine(gray, M, (gray.shape[1], gray.shape[0]), flags=cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_REPLICATE)
    x1, y1 = int(max(0, cx - rw / 2)), int(max(0, cy - rh / 2))
    x2, y2 = int(min(gray.shape[1], cx + rw / 2)), int(min(gray.shape[0], cy + rh / 2))
    return rot[y1:y2, x1:x2]


def _rrect_box(cv2, rrect, W, H):
    pts = cv2.boxPoints(rrect)
    x1, y1 = pts.min(0)
    x2, y2 = pts.max(0)
    return (int(max(0, x1)), int(max(0, y1)), int(min(W, x2)), int(min(H, y2)))


def _verify_roi(cv2, roi):
    h, w = roi.shape[:2]
    if w < 18 or h < 7:
        return 0.0
    # trim the border (plate frame / bodywork) before counting characters
    my, mx = int(h * 0.08), int(w * 0.03)
    inner = roi[my:h - my, mx:w - mx] if h - 2 * my > 6 and w - 2 * mx > 12 else roi
    n_chars, rowness = _char_blobs(cv2, inner)
    if not (MIN_CHARS <= n_chars <= MAX_CHARS) or rowness < 0.45:
        return 0.0
    if np.percentile(roi, 75) < 110:
        return 0.0
    contrast = (np.percentile(roi, 90) - np.percentile(roi, 10)) / 255.0
    if contrast < 0.25:
        return 0.0
    return rowness * min(1.0, contrast * 1.5) * min(1.0, n_chars / 5)


def _candidates(cv2, gray):
    H, W = gray.shape
    boxes = []
    for kw in (max(9, W // 60), max(13, W // 40)):
        kh = max(3, kw // 3)
        rect = cv2.getStructuringElement(cv2.MORPH_RECT, (kw, kh))
        blackhat = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, rect)
        gx = cv2.Sobel(blackhat, cv2.CV_32F, 1, 0, ksize=3)
        gx = np.absolute(gx)
        gx = (255 * (gx - gx.min()) / max(gx.max() - gx.min(), 1e-6)).astype("uint8")
        gx = cv2.GaussianBlur(gx, (5, 5), 0)
        gx = cv2.morphologyEx(gx, cv2.MORPH_CLOSE, rect)
        _, th = cv2.threshold(gx, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        th = cv2.erode(th, None, iterations=1)
        th = cv2.dilate(th, None, iterations=2)
        cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in cnts:
            x, y, w, h = cv2.boundingRect(c)
            if w * h < 0.00005 * W * H or w * h > MAX_AREA_FRAC * W * H:
                continue
            # pad a little so the light plate border is included
            px, py = int(w * 0.08) + 2, int(h * 0.25) + 2
            boxes.append((max(0, x - px), max(0, y - py), min(W, x + w + px), min(H, y + h + py)))
    # Haar cascades shipped with OpenCV (second proposal source)
    for name in ("haarcascade_russian_plate_number.xml", "haarcascade_license_plate_rus_16stages.xml"):
        try:
            casc = cv2.CascadeClassifier(cv2.data.haarcascades + name)
            if casc.empty():
                continue
            for (x, y, w, h) in casc.detectMultiScale(gray, 1.08, 3, minSize=(24, 8)):
                boxes.append((x, y, x + w, y + h))
        except Exception:
            pass
    return boxes


def _iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua else 0.0


def detect_plates(img: Image.Image, max_plates: int = 2, min_score: float = 0.30):
    """Return plate boxes [(x1,y1,x2,y2), ...] in image coordinates."""
    cv2 = _cv2()
    rgb = np.asarray(img.convert("RGB"))
    H, W = rgb.shape[:2]
    scale = 1.0
    if max(H, W) > 1600:      # detect at <=1600px; boxes scaled back
        scale = 1600 / max(H, W)
    elif max(H, W) < 1000:    # small web images: upscale so characters are resolvable
        scale = 2.0
    if scale != 1.0:
        rgb = cv2.resize(rgb, (int(W * scale), int(H * scale)),
                         interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
    h, w = rgb.shape[:2]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    scored = []
    for rr in _bright_rect_candidates(cv2, rgb, gray):
        sc = _verify_roi(cv2, _warp(cv2, gray, rr))
        if sc >= min_score:
            scored.append((sc + 0.05, _rrect_box(cv2, rr, w, h)))  # deskewed = more reliable
    for b in _candidates(cv2, gray):
        x1, y1, x2, y2 = b
        if (x2 - x1) < 18 or (y2 - y1) < 7 or not (MIN_ASPECT <= (x2 - x1) / (y2 - y1) <= MAX_ASPECT):
            continue
        sc = _verify_roi(cv2, gray[y1:y2, x1:x2])
        if sc >= min_score:
            scored.append((sc, b))
    scored.sort(key=lambda t: -t[0])
    keep = []
    for sc, b in scored:
        if all(_iou(b, k) < 0.2 for _, k in keep):
            keep.append((sc, b))
        if len(keep) >= max_plates:
            break
    inv = 1 / scale
    return [tuple(int(round(v * inv)) for v in b) for _, b in keep]


def blur_boxes(img: Image.Image, boxes, pad_frac: float = 0.06) -> Image.Image:
    cv2 = _cv2()
    arr = np.asarray(img.convert("RGB")).copy()
    H, W = arr.shape[:2]
    for (x1, y1, x2, y2) in boxes:
        pw, ph = int((x2 - x1) * pad_frac) + 2, int((y2 - y1) * pad_frac) + 2
        x1, y1 = max(0, x1 - pw), max(0, y1 - ph)
        x2, y2 = min(W, x2 + pw), min(H, y2 + ph)
        region = arr[y1:y2, x1:x2]
        if region.size == 0:
            continue
        h, w = region.shape[:2]
        # pixelate (irreversible) then soften so it reads as a clean blur
        small = cv2.resize(region, (max(1, w // 10), max(1, h // 10)), interpolation=cv2.INTER_AREA)
        region = cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)
        k = max(3, (min(w, h) // 3) | 1)
        arr[y1:y2, x1:x2] = cv2.GaussianBlur(region, (k, k), 0)
    return Image.fromarray(arr)


def blur_license_plate(img: Image.Image) -> Image.Image:
    """Detect and blur plates. Returns the input unchanged if none are found
    or OpenCV is unavailable (never fails the photo pipeline)."""
    try:
        boxes = detect_plates(img)
        return blur_boxes(img, boxes) if boxes else img
    except ImportError:
        print("[Plate Blur] OpenCV not available, skipping plate blur")
        return img
    except Exception as e:
        print(f"[Plate Blur] Error: {e}")
        return img


FRAME_DIRS = ("spin", "studio", "studio_spin", "exterior", "spin_hd", "gallery", "studio_photos")


def blur_vehicle(storage_dir: Path, vin: str, dirs=FRAME_DIRS) -> dict:
    """Blur plates across a vehicle's published frames, in place.

    Originals are preserved under <dir>/_orig_unblurred/. Re-runs are
    idempotent: a frame we blurred earlier (mtime matches our record) is
    re-detected from its preserved original; a frame regenerated since (e.g.
    by a new finalize) replaces the stale original first."""
    vdir = Path(storage_dir) / vin
    rec_path = vdir / "plate_blur.json"
    try:
        prev = json.loads(rec_path.read_text()).get("written", {})
    except Exception:
        prev = {}
    report = {"vin": vin, "frames": 0, "frames_with_plate": 0, "boxes": {}, "written": {}}
    for d in dirs:
        fdir = vdir / d
        if not fdir.is_dir():
            continue
        orig = fdir / "_orig_unblurred"
        for f in sorted(fdir.glob("*.jpg")) + sorted(fdir.glob("*.png")):
            key = f"{d}/{f.name}"
            src = orig / f.name
            ours = key in prev and abs(prev[key] - f.stat().st_mtime) < 1e-3
            if not ours or not src.exists():
                orig.mkdir(exist_ok=True)
                shutil.copy2(f, src)          # current file is the pristine original
            im = Image.open(src)
            im.load()
            boxes = detect_plates(im)
            report["frames"] += 1
            if boxes:
                report["frames_with_plate"] += 1
                report["boxes"][key] = boxes
                out = blur_boxes(im, boxes)
                if im.mode == "RGBA":          # keep transparency of studio cut-outs
                    out.putalpha(im.getchannel("A"))
                    out.save(f)
                else:
                    out.convert("RGB").save(f, quality=92)
                report["written"][key] = f.stat().st_mtime
            elif ours:
                shutil.copy2(src, f)           # earlier false positive -> restore original
    rec_path.write_text(json.dumps(report, indent=2))
    return report


def _debug(path: str, out: str):
    cv2 = _cv2()
    im = Image.open(path).convert("RGB")
    boxes = detect_plates(im)
    arr = np.asarray(blur_boxes(im, boxes)).copy()
    for (x1, y1, x2, y2) in boxes:
        cv2.rectangle(arr, (x1, y1), (x2, y2), (0, 255, 0), 2)
    Image.fromarray(arr).save(out)
    print(json.dumps({"image": path, "boxes": boxes}))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image")
    ap.add_argument("--debug", help="write blurred+boxed preview here")
    ap.add_argument("--vin")
    ap.add_argument("--storage", default=str(Path(__file__).resolve().parents[1] / "storage"))
    a = ap.parse_args()
    if a.image:
        _debug(a.image, a.debug or "plate_debug.jpg")
    elif a.vin:
        print(json.dumps(blur_vehicle(Path(a.storage), a.vin.upper()), indent=2))
    else:
        ap.print_help()
