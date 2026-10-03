"""
LotVue Pro — AI Background Studio (Module 11)
Background removal and premium showroom scene replacement.

All paths are portable and rooted at the app directory by default. Operators may
set LOTVUE_STORAGE_DIR / LOTVUE_SCENES_DIR to use external storage. Uploaded and
stored images are bounded before inference/compositing to keep memory predictable.
"""
from __future__ import annotations

import io
import os
import re
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageOps

from app_paths import BASE_DIR, STORAGE_DIR, configured_path
from background_catalog import (
    STATIC_BACKGROUND_DIR,
    background_asset_path,
    scene_asset_slug,
)


def _bounded_int(env_key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(env_key, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


_PACKAGED_SCENES_DIR = configured_path(
    "LOTVUE_BACKGROUNDS_DIR",
    configured_path("LOTVUE_SCENES_DIR", STATIC_BACKGROUND_DIR),
)
SCENES_DIR = configured_path("LOTVUE_SCENES_DIR", _PACKAGED_SCENES_DIR)
MAX_IMAGE_PIXELS = _bounded_int("LOTVUE_MAX_IMAGE_PIXELS", 40_000_000, 1_000_000, 100_000_000)
MAX_PROCESSING_EDGE = _bounded_int("LOTVUE_BACKGROUND_MAX_EDGE", 1920, 640, 4096)
MAX_PHOTOS_PER_JOB = _bounded_int("LOTVUE_BACKGROUND_MAX_PHOTOS", 20, 1, 100)

# Compatibility IDs map to the same packaged asset library used by the main
# 360 renderer and Photo Studio. These are deterministic local composites, not
# generated/inpainted scenes.
PREMIUM_SCENES = {
    "luxury-showroom": {
        "name": "Luxury Showroom", "description": "Warm glass showroom setting",
        "color": "#1a1a2e", "asset_slug": "showroom_lux",
    },
    "outdoor-mountain": {
        "name": "Mountain Vista", "description": "Mountain road at golden hour",
        "color": "#2d4a3e", "asset_slug": "outdoor_mountain",
    },
    "urban-night": {
        "name": "Urban Night", "description": "City street after dark",
        "color": "#0a1628", "asset_slug": "outdoor_night_city",
    },
    "clean-studio": {
        "name": "Clean Studio", "description": "Neutral studio backdrop",
        "color": "#e8ecf0", "asset_slug": "studio_white",
    },
    "desert-sunset": {
        "name": "Desert Sunset", "description": "Desert landscape at sunset",
        "color": "#8b4513", "asset_slug": "outdoor_desert",
    },
    "beach-coast": {
        "name": "Coastal Drive", "description": "Coastal road and ocean backdrop",
        "color": "#1a5276", "asset_slug": "outdoor_coast",
    },
    "dealer-lot": {
        "name": "Dealer Lot", "description": "Clean dealership showroom",
        "color": "#34495e", "asset_slug": "showroom_classic",
    },
    "garage-dark": {
        "name": "Dark Studio", "description": "Dark neutral studio backdrop",
        "color": "#0d1117", "asset_slug": "studio_dark",
    },
}


def list_scenes() -> list[dict]:
    """List compatibility scenes backed by the canonical packaged background set."""
    scenes = []
    for scene_id, info in PREMIUM_SCENES.items():
        slug = scene_asset_slug(scene_id)
        asset = background_asset_path(slug, SCENES_DIR) if slug else None
        packaged = False
        if asset is not None:
            try:
                packaged = asset.is_relative_to(STATIC_BACKGROUND_DIR.resolve())
            except (OSError, RuntimeError, ValueError):
                packaged = False
        scenes.append({
            "id": scene_id,
            "name": info["name"],
            "description": info["description"],
            "background_slug": slug,
            "has_preview": asset is not None,
            "preview_url": f"/static/backgrounds/thumbs/{slug}.jpg" if packaged and slug else None,
            "renderer": "local_image_composite" if asset else "local_gradient_fallback",
            "ai_generated": False,
            "requires_background_removal": True,
        })
    return scenes


def _open_bounded_image(image_path: str | Path, mode: str) -> Image.Image:
    """Decode one image only after applying pixel and long-edge bounds."""
    with Image.open(image_path) as source:
        width, height = source.size
        if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
            raise ValueError(f"Image exceeds the {MAX_IMAGE_PIXELS:,}-pixel processing limit")
        source.draft("RGB", (MAX_PROCESSING_EDGE, MAX_PROCESSING_EDGE))
        source.thumbnail(
            (MAX_PROCESSING_EDGE, MAX_PROCESSING_EDGE),
            Image.Resampling.LANCZOS,
        )
        source.load()
        oriented = ImageOps.exif_transpose(source)
        try:
            return oriented.convert(mode)
        finally:
            if oriented is not source:
                oriented.close()


def _image_from_result(result) -> Image.Image:
    if isinstance(result, Image.Image):
        return result
    if isinstance(result, (bytes, bytearray, memoryview)):
        with Image.open(io.BytesIO(result)) as image:
            image.load()
            return image.copy()
    raise TypeError("Background-removal engine returned an unsupported image type")


def remove_background(image_path: str, output_path: Optional[str] = None) -> str:
    """Remove a photo background only with an explicitly approved local model."""
    from rembg_runtime import remove as rembg_remove

    image = _open_bounded_image(image_path, "RGB")
    result = None
    try:
        result = _image_from_result(rembg_remove(image))
        output = Path(output_path) if output_path else Path(image_path).with_name(
            f"{Path(image_path).stem}_nobg.png"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        result.save(output, "PNG", optimize=True)
        return str(output)
    finally:
        image.close()
        if result is not None:
            result.close()


def _make_gradient(size: tuple[int, int], color: str) -> Image.Image:
    width, height = size
    rgb = tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))
    scene = Image.new("RGBA", size)
    draw = ImageDraw.Draw(scene)
    # One bounded gradient image is generated per vehicle frame; avoid storing rows.
    denominator = max(height - 1, 1)
    for y in range(height):
        factor = 1.0 - 0.3 * (y / denominator)
        draw.line(
            (0, y, width, y),
            fill=tuple(round(channel * factor) for channel in rgb) + (255,),
        )
    return scene


def composite_on_scene(
    vehicle_png: str,
    scene_id: str,
    output_path: Optional[str] = None,
) -> str:
    """Composite a transparent vehicle cutout onto a bounded scene image."""
    scene_info = PREMIUM_SCENES.get(scene_id)
    if not scene_info:
        raise ValueError(f"Unknown scene: {scene_id}")

    vehicle = _open_bounded_image(vehicle_png, "RGBA")
    scene = None
    final = None
    try:
        size = vehicle.size
        scene_slug = scene_asset_slug(scene_id)
        scene_path = background_asset_path(scene_slug, SCENES_DIR) if scene_slug else None
        if scene_path is not None:
            scene = _open_bounded_image(scene_path, "RGBA")
            if scene.size != size:
                resized_scene = scene.resize(size, Image.Resampling.LANCZOS)
                scene.close()
                scene = resized_scene
        else:
            # Degraded local fallback only; this is not an AI-generated scene.
            scene = _make_gradient(size, scene_info["color"])

        x = max(0, (scene.width - vehicle.width) // 2)
        y = max(0, scene.height - vehicle.height)
        scene.alpha_composite(vehicle, (x, y))

        output = Path(output_path) if output_path else Path(vehicle_png).with_name(
            f"{Path(vehicle_png).stem}_{scene_id}.jpg"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        final = scene.convert("RGB")
        final.save(output, "JPEG", quality=92, optimize=True, progressive=True)
        return str(output)
    finally:
        vehicle.close()
        if scene is not None:
            scene.close()
        if final is not None:
            final.close()


def _safe_storage_path(*parts: str) -> Path:
    root = STORAGE_DIR.resolve()
    path = root.joinpath(*parts).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Vehicle storage path is invalid")
    return path


def process_vehicle_photos(
    vin: str,
    scene_id: str = "clean-studio",
    max_photos: int = 20,
) -> dict:
    """Compatibility wrapper over the canonical PhotoProcessor engine."""
    vehicle_id = (vin or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{1,32}", vehicle_id):
        raise ValueError("Invalid vehicle identifier")
    if scene_id not in PREMIUM_SCENES:
        raise ValueError(f"Unknown scene: {scene_id}")
    from rembg_runtime import is_enabled as rembg_enabled
    if not rembg_enabled():
        return {
            "vin": vehicle_id, "scene": scene_id, "status": "unavailable",
            "processed": 0, "errors": 0,
            "error": "No explicitly approved background-removal model is configured",
        }

    scene_slug = scene_asset_slug(scene_id)
    if not scene_slug:
        raise ValueError("Scene is not mapped to the packaged background catalog")
    photo_limit = max(1, min(int(max_photos), MAX_PHOTOS_PER_JOB))
    exterior_dir = _safe_storage_path(vehicle_id, "exterior")
    studio_dir = _safe_storage_path(vehicle_id, "studio")
    if not exterior_dir.is_dir():
        return {"vin": vehicle_id, "scene": scene_id, "processed": 0,
                "errors": 0, "error": "No exterior photos found"}

    from .photo_processor import PhotoProcessor
    results = PhotoProcessor(background_name=scene_slug).process_batch(
        str(exterior_dir), str(studio_dir), max_photos=photo_limit
    )
    succeeded = sum(result.get("status") == "ok" for result in results)
    degraded = sum(result.get("status") == "degraded" for result in results)
    errors = sum(result.get("status") == "error" for result in results)
    return {
        "vin": vehicle_id,
        "scene": scene_id,
        "background_slug": scene_slug,
        "status": "error" if results and errors == len(results) else (
            "degraded" if degraded or errors else "completed"
        ),
        "processed": succeeded + degraded,
        "degraded": degraded,
        "errors": errors,
        "results": results,
    }


def generate_scene_with_comfyui(
    vehicle_png: str,
    scene_id: str,
    output_path: Optional[str] = None,
) -> str:
    """Probe ComfyUI and use the bounded local compositor as the current fallback."""
    if scene_id not in PREMIUM_SCENES:
        raise ValueError(f"Unknown scene: {scene_id}")
    import requests

    comfy_url = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/")
    try:
        response = requests.get(f"{comfy_url}/system_stats", timeout=3)
        response.raise_for_status()
    except requests.RequestException:
        return composite_on_scene(vehicle_png, scene_id, output_path)

    # TODO: Wire inpainting into a real ComfyUI workflow; current successful probe
    # still uses the deterministic local composite until that workflow is configured.
    return composite_on_scene(vehicle_png, scene_id, output_path)
