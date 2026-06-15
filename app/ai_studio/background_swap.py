"""
LotVue Pro — Background Removal & Swap
Uses rembg (already in lotvue conda env) for background removal.
Composites car on showroom background with proper positioning.
"""
import io
import numpy as np
from PIL import Image, ImageFilter
from typing import Tuple, Optional


def remove_background(img: Image.Image) -> Tuple[Image.Image, Optional[Image.Image]]:
    """
    Remove background from car photo using rembg.
    Returns (car_isolated_RGBA, mask_L).
    """
    try:
        from rembg import remove
        # rembg returns RGBA with transparent background
        result = remove(img)
        if result.mode != "RGBA":
            result = result.convert("RGBA")
        # Extract mask from alpha channel
        mask = result.split()[3]
        return result, mask
    except ImportError:
        print("[BG Swap] rembg not available, returning original with white bg")
        return img.convert("RGBA"), None


def composite_on_showroom(car_rgba: Image.Image, background_path: str,
                          position: str = "center-bottom",
                          car_scale: float = 0.85) -> Image.Image:
    """
    Composite the isolated car onto a showroom background.

    Args:
        car_rgba: Car image with transparent background (RGBA)
        background_path: Path to background image
        position: Where to place car — "center-bottom" (default), "center"
        car_scale: Scale car to this fraction of background width

    Returns:
        Composited image (RGB)
    """
    bg = Image.open(background_path).convert("RGBA")

    # Scale car to fit nicely on background
    bg_w, bg_h = bg.size
    car_w, car_h = car_rgba.size

    # Calculate target width
    target_w = int(bg_w * car_scale)
    scale_factor = target_w / car_w
    target_h = int(car_h * scale_factor)

    # Resize car
    car_resized = car_rgba.resize((target_w, target_h), Image.LANCZOS)

    # Position car on background
    if position == "center-bottom":
        x = (bg_w - target_w) // 2
        y = bg_h - target_h - int(bg_h * 0.05)  # 5% padding from bottom
    elif position == "center":
        x = (bg_w - target_w) // 2
        y = (bg_h - target_h) // 2
    else:
        x = (bg_w - target_w) // 2
        y = bg_h - target_h - int(bg_h * 0.05)

    # Composite
    bg.paste(car_resized, (x, y), car_resized)

    return bg.convert("RGB")


def create_gradient_background(width: int = 1920, height: int = 1080,
                                top_color: tuple = (248, 250, 252),
                                bottom_color: tuple = (220, 225, 230)) -> Image.Image:
    """Create a clean gradient background for when no background image is available."""
    img = Image.new("RGB", (width, height))
    pixels = img.load()
    for y in range(height):
        r = int(top_color[0] + (bottom_color[0] - top_color[0]) * y / height)
        g = int(top_color[1] + (bottom_color[1] - top_color[1]) * y / height)
        b = int(top_color[2] + (bottom_color[2] - top_color[2]) * y / height)
        for x in range(width):
            pixels[x, y] = (r, g, b)
    return img
