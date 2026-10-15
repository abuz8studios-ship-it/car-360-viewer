"""Studio 360 spin built cut-out first (photo sets and walkaround frames).

Older pipeline: blend raw photos into in-between frames, then cut the car out
of each blend. Blends of photos taken 30-90 degrees apart carry two cars and
two backgrounds, so the cut-outs ghosted, their bounding boxes included ghost
parts, and one global scale with a fixed top margin let the car change size
between angles and float above its shadow.

This module:
  1. cuts the car out of each real source frame once and drops detached specks,
  2. scales every car to one height and puts its tyres on one ground line,
  3. builds in-between frames from those aligned cut-outs with optical flow and
     a short cross-fade (each output shows one car, not a double exposure),
  4. draws the contact shadow at each frame's own tyre line.

Pure functions; the caller passes the background-removal callable, so tests
can run without model weights.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Callable

import cv2
import numpy as np
from PIL import Image, ImageOps

CANVAS_W, CANVAS_H = 1920, 1080
GROUND_Y = int(CANVAS_H * 0.86)       # tyre line on every output frame
MAX_CAR_H = int(CANVAS_H * 0.60)      # car height cap (fits a wide side view too)
MAX_CAR_W = int(CANVAS_W * 0.90)
ALPHA_SOLID = 24                      # alpha above this counts as car
FADE_HALF_WIDTH = 0.18                # cross-fade only for |t - 0.5| < this
# Below this silhouette overlap, neighbouring views (e.g. the car facing the
# other way) cannot be morphed without a double exposure: cut at mid-gap.
# Measured 2026-10-15 on 11 dealer photos: bad pairs 0.49-0.61, good 0.67-0.82.
MORPH_MIN_IOU = 0.65
MAX_SOURCE_EDGE = 1600


def _clean_alpha(rgba: np.ndarray) -> np.ndarray:
    """Zero alpha outside the car's main connected region (specks, hair lines)."""
    alpha = rgba[..., 3]
    mask = (alpha > ALPHA_SOLID).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if count <= 1:
        return rgba
    areas = stats[1:, cv2.CC_STAT_AREA]
    main = 1 + int(np.argmax(areas))
    x, y = stats[main, cv2.CC_STAT_LEFT], stats[main, cv2.CC_STAT_TOP]
    w, h = stats[main, cv2.CC_STAT_WIDTH], stats[main, cv2.CC_STAT_HEIGHT]
    pad_x, pad_y = 0.05 * w, 0.05 * h
    _, _, _, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    # keep the car and its detached parts (mirrors, antenna) inside its outline;
    # drop other parked cars and speckle elsewhere in the photo
    keep = [label for label in range(1, count)
            if areas[label - 1] >= 64
            and x - pad_x <= centroids[label][0] <= x + w + pad_x
            and y - pad_y <= centroids[label][1] <= y + h + pad_y]
    keep_mask = np.isin(labels, keep)
    # keep soft edges that touch the kept region, drop the rest
    keep_mask = cv2.dilate(keep_mask.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    out = rgba.copy()
    out[..., 3] = np.where(keep_mask, alpha, 0)
    return out


def tight_bbox(rgba: np.ndarray) -> tuple[int, int, int, int] | None:
    """Bounding box of solid car pixels (x0, y0, x1, y1), exclusive ends."""
    ys, xs = np.nonzero(rgba[..., 3] > ALPHA_SOLID)
    if not len(xs):
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def cut_out(path: Path, remove_fn: Callable[[bytes], bytes]) -> np.ndarray | None:
    """Source frame -> cleaned RGBA cut-out (bounded size) or None."""
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
    image.thumbnail((MAX_SOURCE_EDGE, MAX_SOURCE_EDGE), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    image.close()
    with Image.open(io.BytesIO(remove_fn(buffer.getvalue()))) as result:
        rgba = np.array(result.convert("RGBA"))
    rgba = _clean_alpha(rgba)
    return rgba if tight_bbox(rgba) else None


def target_height(boxes: list[tuple[int, int, int, int]]) -> int:
    """One car height for the whole spin, small enough for the widest view."""
    widest = max((x1 - x0) / max(1, y1 - y0) for x0, y0, x1, y1 in boxes)
    return int(min(MAX_CAR_H, MAX_CAR_W / max(widest, 0.1)))


def normalize(rgba: np.ndarray, car_height: int) -> np.ndarray:
    """Place the car on a transparent canvas: fixed height, centred, tyres on GROUND_Y."""
    x0, y0, x1, y1 = tight_bbox(rgba)
    crop = rgba[y0:y1, x0:x1]
    scale = car_height / max(1, y1 - y0)
    width = max(1, min(MAX_CAR_W, int(round((x1 - x0) * scale))))
    resized = cv2.resize(crop, (width, car_height),
                         interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
    canvas = np.zeros((CANVAS_H, CANVAS_W, 4), np.uint8)
    left = (CANVAS_W - width) // 2
    top = GROUND_Y - car_height
    canvas[top:top + car_height, left:left + width] = resized
    return canvas


def _flow(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Dense flow a->b computed at half resolution on a neutral backdrop."""
    def gray(rgba):
        alpha = rgba[..., 3:4].astype(np.float32) / 255.0
        rgb = rgba[..., :3].astype(np.float32) * alpha + 128.0 * (1.0 - alpha)
        small = cv2.resize(rgb.astype(np.uint8), (CANVAS_W // 2, CANVAS_H // 2),
                           interpolation=cv2.INTER_AREA)
        return cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    flow = cv2.calcOpticalFlowFarneback(gray(a), gray(b), None, 0.5, 4, 31, 5, 7, 1.5, 0)
    return cv2.resize(flow, (CANVAS_W, CANVAS_H)) * 2.0


def blend_weight(t: float) -> float:
    """Share of the later frame at time t: 0 then a short ramp then 1."""
    return float(np.clip((t - (0.5 - FADE_HALF_WIDTH)) / (2 * FADE_HALF_WIDTH), 0.0, 1.0))


def in_between(a: np.ndarray, b: np.ndarray, t: float, flow: np.ndarray,
               grid: tuple[np.ndarray, np.ndarray]) -> np.ndarray:
    """Warp both aligned cut-outs toward time t and mix with a short cross-fade."""
    gx, gy = grid
    warped_a = cv2.remap(a, gx - flow[..., 0] * t, gy - flow[..., 1] * t,
                         cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
    warped_b = cv2.remap(b, gx + flow[..., 0] * (1 - t), gy + flow[..., 1] * (1 - t),
                         cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
    w = blend_weight(t)
    if w <= 0.0:
        return warped_a
    if w >= 1.0:
        return warped_b
    # premultiplied mix so transparent pixels never darken the car's edge
    pa = warped_a.astype(np.float32)
    pb = warped_b.astype(np.float32)
    alpha = pa[..., 3:4] * (1 - w) + pb[..., 3:4] * w
    rgb = (pa[..., :3] * pa[..., 3:4] * (1 - w) + pb[..., :3] * pb[..., 3:4] * w) / np.maximum(alpha, 1e-3)
    return np.dstack([np.clip(rgb, 0, 255), np.clip(alpha, 0, 255)]).astype(np.uint8)


def gap_counts(n: int, target_frames: int, manifest: dict | None = None) -> list[int]:
    """Output frames per source gap (cyclic), following measured angles when known."""
    gaps = manifest.get("gaps") if isinstance(manifest, dict) else None
    if isinstance(gaps, list) and len(gaps) == n:
        try:
            degrees = [float(g.get("degrees", 0)) for g in gaps]
            total = sum(degrees)
            if total > 0 and all(np.isfinite(d) and d > 0 for d in degrees):
                counts = [max(1, round(target_frames * d / total)) for d in degrees]
                while sum(counts) < target_frames:
                    counts[counts.index(min(counts))] += 1
                while sum(counts) > target_frames and max(counts) > 1:
                    counts[counts.index(max(counts))] -= 1
                return counts
        except (TypeError, ValueError, AttributeError):
            pass
    base, extra = divmod(target_frames, n)
    return [base + (1 if i < extra else 0) for i in range(n)]


def turn_frame(a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
    """Cut gap rendered as a turn: the car's width moves from view a's toward
    view b's, showing a until mid-gap and b after. One car per frame, height
    and tyre line unchanged."""
    src = a if t < 0.5 else b
    ax0, _, ax1, _ = tight_bbox(a)
    bx0, _, bx1, _ = tight_bbox(b)
    x0, y0, x1, y1 = tight_bbox(src)
    width = max(1, int(round((ax1 - ax0) + ((bx1 - bx0) - (ax1 - ax0)) * t)))
    car = cv2.resize(src[y0:y1, x0:x1], (width, y1 - y0), interpolation=cv2.INTER_AREA)
    out = np.zeros_like(src)
    left = (CANVAS_W - width) // 2
    out[y0:y1, left:left + width] = car
    return out


def silhouette_iou(a: np.ndarray, b: np.ndarray) -> float:
    ma, mb = a[..., 3] > ALPHA_SOLID, b[..., 3] > ALPHA_SOLID
    return float((ma & mb).sum() / max(1, (ma | mb).sum()))


def spin_frames(sources: list[np.ndarray], target_frames: int,
                manifest: dict | None = None):
    """Yield target_frames aligned RGBA frames that go once around the car."""
    n = len(sources)
    if n >= target_frames:
        for i in range(target_frames):
            yield sources[round(i * n / target_frames) % n]
        return
    gy, gx = np.mgrid[0:CANVAS_H, 0:CANVAS_W].astype(np.float32)
    for i, count in enumerate(gap_counts(n, target_frames, manifest)):
        a, b = sources[i], sources[(i + 1) % n]
        morph = count > 1 and silhouette_iou(a, b) >= MORPH_MIN_IOU
        flow = _flow(a, b) if morph else None
        for j in range(count):
            t = j / count
            if j == 0:
                yield a
            elif flow is None:
                yield turn_frame(a, b, t)   # never a double exposure
            else:
                yield in_between(a, b, t, flow, (gx, gy))


def composite(frame: np.ndarray, background: Image.Image,
              shadow_fn: Callable[[Image.Image, Image.Image, int, int], Image.Image]) -> Image.Image:
    """Car over background with the shadow at this frame's own tyre line."""
    car = Image.fromarray(frame, "RGBA")
    box = tight_bbox(frame)
    bg = background.copy()
    if box:
        x0, y0, x1, y1 = box
        shadowed = shadow_fn(bg, car.crop(box), x0, y0)
        bg.close()
        bg = shadowed
    out = bg.convert("RGBA")
    out.alpha_composite(car)
    return out.convert("RGB")


def jitter_report(frames: list[np.ndarray]) -> dict:
    """Measured stability of a spin: tyre line, height, and horizontal centre."""
    boxes = [b for b in (tight_bbox(f) for f in frames) if b]
    bottoms = [b[3] for b in boxes]
    heights = [b[3] - b[1] for b in boxes]
    centres = [(b[0] + b[2]) / 2 for b in boxes]
    return {
        "frames": len(frames),
        "ground_line_spread_px": int(max(bottoms) - min(bottoms)) if bottoms else None,
        "height_spread_pct": round(100 * (max(heights) - min(heights)) / max(heights), 1) if heights else None,
        "centre_spread_px": round(max(centres) - min(centres), 1) if centres else None,
    }
