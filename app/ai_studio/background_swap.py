"""Photo background cut-out and compositing primitives for the Photo Studio."""
from __future__ import annotations

from typing import Optional, Tuple

from PIL import Image


def remove_background(img: Image.Image) -> Tuple[Image.Image, Optional[Image.Image]]:
    """Return a rembg cutout and alpha mask, or the original plus ``None``.

    A missing optional dependency is deliberately represented by ``mask=None``.
    Callers must treat that as an unavailable cut-out, never as a successful
    transparent image.
    """
    from rembg_runtime import is_enabled, remove
    if not is_enabled():
        print("[BG Swap] no explicitly approved rembg model is configured")
        return img.convert("RGBA"), None

    try:
        result = remove(img)
    except Exception as exc:
        print(f"[BG Swap] background removal unavailable: {type(exc).__name__}")
        return img.convert("RGBA"), None
    if result.mode != "RGBA":
        result = result.convert("RGBA")
    alpha = result.getchannel("A")
    # rembg should make at least some pixels transparent. An all-opaque result is
    # not a valid cut-out and must not be composited as though segmentation worked.
    if alpha.getextrema() == (255, 255):
        alpha.close()
        result.close()
        return img.convert("RGBA"), None
    return result, alpha


def composite_on_showroom(
    car_rgba: Image.Image,
    background_path: str,
    position: str = "center-bottom",
    car_scale: float = 0.85,
) -> Image.Image:
    """Place a transparent vehicle cutout on a packaged showroom background."""
    if not 0.1 <= float(car_scale) <= 1.0:
        raise ValueError("car_scale must be between 0.1 and 1.0")

    with Image.open(background_path) as source:
        background = source.convert("RGBA")
    try:
        bg_w, bg_h = background.size
        car_w, car_h = car_rgba.size
        target_w = max(1, int(bg_w * car_scale))
        target_h = max(1, int(bg_h * 0.82))
        scale = min(target_w / max(car_w, 1), target_h / max(car_h, 1))
        resized = car_rgba.resize(
            (max(1, int(car_w * scale)), max(1, int(car_h * scale))),
            Image.Resampling.LANCZOS,
        )
        try:
            x = max(0, (bg_w - resized.width) // 2)
            if position == "center":
                y = max(0, (bg_h - resized.height) // 2)
            else:
                y = max(0, bg_h - resized.height - int(bg_h * 0.05))
            background.alpha_composite(resized, (x, y))
            return background.convert("RGB")
        finally:
            resized.close()
    finally:
        background.close()


def create_gradient_background(
    width: int = 1920,
    height: int = 1080,
    top_color: tuple = (248, 250, 252),
    bottom_color: tuple = (220, 225, 230),
) -> Image.Image:
    """Create a simple fallback gradient when no scene asset is selected."""
    img = Image.new("RGB", (width, height))
    pixels = img.load()
    for y in range(height):
        ratio = y / max(height - 1, 1)
        color = tuple(
            int(top_color[channel] + (bottom_color[channel] - top_color[channel]) * ratio)
            for channel in range(3)
        )
        for x in range(width):
            pixels[x, y] = color
    return img
