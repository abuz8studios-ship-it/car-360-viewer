"""
LotVue Pro — Photo Enhancement
Auto color correction, white balance, contrast, and floor shadow generation.
All CPU-based (Pillow + numpy) — no GPU needed for this step.
"""
import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageDraw


def auto_enhance(img: Image.Image) -> Image.Image:
    """
    Apply auto enhancement pipeline:
    1. Auto white balance
    2. Contrast boost
    3. Saturation boost
    4. Sharpening
    """
    # Convert to RGB if needed
    if img.mode != "RGB":
        img = img.convert("RGB")

    # 1. Auto white balance (gray world assumption)
    img = auto_white_balance(img)

    # 2. Contrast: subtle boost
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(1.08)

    # 3. Saturation: subtle boost for car paint pop
    enhancer = ImageEnhance.Color(img)
    img = enhancer.enhance(1.12)

    # 4. Brightness: slight increase for studio look
    enhancer = ImageEnhance.Brightness(img)
    img = enhancer.enhance(1.03)

    # 5. Sharpen: gentle pass
    enhancer = ImageEnhance.Sharpness(img)
    img = enhancer.enhance(1.15)

    return img


def auto_white_balance(img: Image.Image) -> Image.Image:
    """
    Gray world white balance correction.
    Adjusts color channels so their averages converge.
    """
    arr = np.array(img, dtype=np.float32)
    avg_r = arr[:, :, 0].mean()
    avg_g = arr[:, :, 1].mean()
    avg_b = arr[:, :, 2].mean()
    avg_all = (avg_r + avg_g + avg_b) / 3.0

    if avg_r > 0:
        arr[:, :, 0] *= avg_all / avg_r
    if avg_g > 0:
        arr[:, :, 1] *= avg_all / avg_g
    if avg_b > 0:
        arr[:, :, 2] *= avg_all / avg_b

    arr = np.clip(arr, 0, 255).astype(np.uint8)
    return Image.fromarray(arr)


def add_floor_shadow(img: Image.Image, shadow_height_pct: float = 0.08,
                     shadow_opacity: int = 40) -> Image.Image:
    """
    Add a subtle floor shadow at the bottom of the image.
    Creates a gradient shadow that fades upward from the bottom.
    """
    w, h = img.size
    shadow_h = int(h * shadow_height_pct)

    # Create shadow overlay
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    for y in range(shadow_h):
        # Gradient from bottom (darkest) to top (transparent)
        progress = y / shadow_h  # 0 at top of shadow, 1 at bottom
        alpha = int(shadow_opacity * progress)
        draw.line([(0, h - shadow_h + y), (w, h - shadow_h + y)], fill=(0, 0, 0, alpha))

    # Composite shadow onto image
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    result = Image.alpha_composite(img, overlay)
    return result.convert("RGB")
