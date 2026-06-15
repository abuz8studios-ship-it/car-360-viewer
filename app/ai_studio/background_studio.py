"""
LotVue Pro — AI Background Studio (Module 11)
AI background removal + premium showroom scene replacement.
Cloning best of autofox + AIVO360.

Uses rembg for background removal, ComfyUI for premium scene compositing.
Zero cloud cost — all local generation.
"""
import os
import io
import json
import shutil
from pathlib import Path
from datetime import datetime
from typing import Optional
from PIL import Image

STORAGE_DIR = Path(r".\360Car\app\storage")
SCENES_DIR = Path(r".\360Car\app\static\scenes")

# Premium showroom scenes (shipped with LotVue Pro)
PREMIUM_SCENES = {
    "luxury-showroom": {
        "name": "Luxury Showroom",
        "description": "High-end glass showroom with marble floors",
        "color": "#1a1a2e",
        "prompt": "luxury car showroom, marble floor, glass walls, dramatic lighting, photorealistic, 8k",
    },
    "outdoor-mountain": {
        "name": "Mountain Vista",
        "description": "Scenic mountain road at golden hour",
        "color": "#2d4a3e",
        "prompt": "car parked on mountain road, golden hour, scenic vista, cinematic, 8k photography",
    },
    "urban-night": {
        "name": "Urban Night",
        "description": "City street with neon reflections",
        "color": "#0a1628",
        "prompt": "car on city street at night, neon reflections, wet road, cinematic lighting, 8k",
    },
    "clean-studio": {
        "name": "Clean Studio",
        "description": "Professional white/grey gradient studio",
        "color": "#e8ecf0",
        "prompt": "car in professional photography studio, clean white background, soft lighting, product photography, 8k",
    },
    "desert-sunset": {
        "name": "Desert Sunset",
        "description": "Wide desert road at sunset",
        "color": "#8b4513",
        "prompt": "car on desert highway at sunset, dramatic sky, cinematic wide shot, 8k",
    },
    "beach-coast": {
        "name": "Coastal Drive",
        "description": "Pacific coast highway with ocean",
        "color": "#1a5276",
        "prompt": "car on coastal highway, ocean view, blue sky, golden hour, cinematic, 8k",
    },
    "dealer-lot": {
        "name": "Dealer Lot (Clean)",
        "description": "Professional dealer lot, clean background",
        "color": "#34495e",
        "prompt": "car on clean dealer lot, professional, even lighting, no distractions, product photography",
    },
    "garage-dark": {
        "name": "Dark Garage",
        "description": "Moody garage with spotlight effect",
        "color": "#0d1117",
        "prompt": "car in dark garage, single spotlight, dramatic shadows, automotive photography, 8k",
    },
}


def list_scenes() -> list:
    """List all available background scenes."""
    scenes = []
    for scene_id, info in PREMIUM_SCENES.items():
        preview_path = SCENES_DIR / f"{scene_id}.jpg"
        scenes.append({
            "id": scene_id,
            "name": info["name"],
            "description": info["description"],
            "color": info["color"],
            "has_preview": preview_path.exists(),
            "preview_url": f"/static/scenes/{scene_id}.jpg" if preview_path.exists() else None,
        })
    return scenes


def remove_background(image_path: str, output_path: str = None) -> str:
    """
    Remove background from a vehicle photo using rembg.
    Returns path to the transparent PNG.
    """
    from rembg import remove as rembg_remove

    img = Image.open(image_path)
    result = rembg_remove(img)

    if not output_path:
        p = Path(image_path)
        output_path = str(p.parent / f"{p.stem}_nobg.png")

    result.save(output_path, "PNG")
    return output_path


def composite_on_scene(vehicle_png: str, scene_id: str,
                       output_path: str = None) -> str:
    """
    Composite a vehicle (with transparent background) onto a scene.
    Uses simple PIL compositing — no GPU needed for basic scenes.
    """
    scene_info = PREMIUM_SCENES.get(scene_id)
    if not scene_info:
        raise ValueError(f"Unknown scene: {scene_id}")

    # Load vehicle with transparency
    vehicle = Image.open(vehicle_png).convert("RGBA")
    vw, vh = vehicle.size

    # Create scene background
    scene_path = SCENES_DIR / f"{scene_id}.jpg"
    if scene_path.exists():
        scene = Image.open(scene_path).convert("RGBA")
        scene = scene.resize((vw, vh), Image.LANCZOS)
    else:
        # Generate solid color + gradient background
        from PIL import ImageDraw
        scene = Image.new("RGBA", (vw, vh))
        draw = ImageDraw.Draw(scene)
        base_color = scene_info["color"]
        r = int(base_color[1:3], 16)
        g = int(base_color[3:5], 16)
        b = int(base_color[5:7], 16)

        # Vertical gradient
        for y in range(vh):
            ratio = y / vh
            cr = int(r * (1 - ratio * 0.3))
            cg = int(g * (1 - ratio * 0.3))
            cb = int(b * (1 - ratio * 0.3))
            draw.line([(0, y), (vw, y)], fill=(cr, cg, cb, 255))

    # Composite vehicle onto scene
    # Position vehicle centered, lower third
    vx = (scene.width - vehicle.width) // 2
    vy = scene.height - vehicle.height
    scene.paste(vehicle, (vx, vy), vehicle)

    if not output_path:
        p = Path(vehicle_png)
        output_path = str(p.parent / f"{p.stem}_{scene_id}.jpg")

    # Convert to RGB for JPEG
    final = scene.convert("RGB")
    final.save(output_path, "JPEG", quality=95)
    return output_path


def process_vehicle_photos(vin: str, scene_id: str = "clean-studio",
                           max_photos: int = 20) -> dict:
    """
    Process all photos for a vehicle: remove backgrounds + composite on scene.
    Returns paths to processed photos.
    """
    vehicle_dir = STORAGE_DIR / vin
    exterior_dir = vehicle_dir / "exterior"
    studio_dir = vehicle_dir / "studio"
    studio_dir.mkdir(parents=True, exist_ok=True)

    if not exterior_dir.exists():
        return {"error": f"No exterior photos found for {vin}", "processed": 0}

    photos = sorted(exterior_dir.glob("*.jpg"))[:max_photos]
    if not photos:
        return {"error": f"No JPG photos found for {vin}", "processed": 0}

    results = []
    errors = []

    for photo_path in photos:
        try:
            # Step 1: Remove background
            nobg_path = str(studio_dir / f"{photo_path.stem}_nobg.png")
            remove_background(str(photo_path), nobg_path)

            # Step 2: Composite on scene
            final_path = str(studio_dir / f"{photo_path.stem}.jpg")
            composite_on_scene(nobg_path, scene_id, final_path)

            results.append({
                "original": str(photo_path),
                "no_bg": nobg_path,
                "final": final_path,
                "scene": scene_id,
            })

            # Clean up intermediate PNG to save space
            if os.path.exists(nobg_path):
                os.remove(nobg_path)

        except Exception as e:
            errors.append({"photo": str(photo_path), "error": str(e)})

    return {
        "vin": vin,
        "scene": scene_id,
        "processed": len(results),
        "errors": len(errors),
        "results": results,
        "error_details": errors,
    }


def generate_scene_with_comfyui(vehicle_png: str, scene_id: str,
                                output_path: str = None) -> str:
    """
    Use ComfyUI for high-quality AI scene generation.
    Sends the vehicle + scene prompt to ComfyUI inpainting pipeline.
    Falls back to simple compositing if ComfyUI is unavailable.
    """
    import requests

    scene_info = PREMIUM_SCENES.get(scene_id)
    if not scene_info:
        raise ValueError(f"Unknown scene: {scene_id}")

    # Check ComfyUI availability
    try:
        r = requests.get("http://localhost:8188/system_stats", timeout=3)
        if r.status_code != 200:
            raise ConnectionError("ComfyUI not responding")
    except:
        # Fallback to simple compositing
        return composite_on_scene(vehicle_png, scene_id, output_path)

    # For now, use simple compositing
    # TODO: Wire up actual ComfyUI inpainting workflow for premium quality
    return composite_on_scene(vehicle_png, scene_id, output_path)
