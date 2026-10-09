"""Background removal and scene rendering for Dealer Studio.

Packaged backdrops use deterministic local compositing. AI scene generation is
available only through an owner-supplied ComfyUI API-format workflow; it is never
silently substituted for the local renderer. Paths are portable by default and
images are bounded before inference/compositing.
"""
from __future__ import annotations

import io
import json
import os
import re
import uuid
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
        alpha = vehicle.getchannel("A")
        try:
            if alpha.getextrema() in {(0, 0), (255, 255)}:
                raise ValueError("A transparent vehicle cut-out is required for local compositing")
        finally:
            alpha.close()
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


LOTVUE_COMFYUI_SCENE_WORKFLOW_ENV = "LOTVUE_COMFYUI_SCENE_WORKFLOW"
LOTVUE_COMFYUI_SCENE_TIMEOUT_ENV = "LOTVUE_COMFYUI_SCENE_TIMEOUT"
_COMFY_SCENE_MAX_WORKFLOW_BYTES = 1024 * 1024
_COMFY_SCENE_MAX_NODES = 500
_COMFY_SCENE_MAX_FILE_BYTES = 30 * 1024 * 1024
_COMFY_SCENE_IMAGE_TOKEN = "__LOTVUE_INPUT_IMAGE__"
_COMFY_SCENE_PROMPT_TOKEN = "__LOTVUE_SCENE_PROMPT__"
_COMFY_SCENE_NEGATIVE_TOKEN = "__LOTVUE_NEGATIVE_PROMPT__"
_COMFY_SCENE_PREFIX_TOKEN = "__LOTVUE_OUTPUT_PREFIX__"


class ComfyUIWorkflowConfigurationError(RuntimeError):
    """The owner has not supplied a valid ComfyUI API-format scene workflow."""


def _count_workflow_tokens(value, counts: dict[str, int]) -> None:
    if isinstance(value, str):
        for token in counts:
            counts[token] += value.count(token)
    elif isinstance(value, dict):
        for child in value.values():
            _count_workflow_tokens(child, counts)
    elif isinstance(value, list):
        for child in value:
            _count_workflow_tokens(child, counts)


def _replace_workflow_tokens(value, replacements: dict[str, str]):
    if isinstance(value, str):
        for token, replacement in replacements.items():
            value = value.replace(token, replacement)
        return value
    if isinstance(value, dict):
        return {key: _replace_workflow_tokens(child, replacements) for key, child in value.items()}
    if isinstance(value, list):
        return [_replace_workflow_tokens(child, replacements) for child in value]
    return value


def _load_comfyui_scene_workflow() -> tuple[dict, int]:
    configured = os.environ.get(LOTVUE_COMFYUI_SCENE_WORKFLOW_ENV, "").strip()
    if not configured:
        raise ComfyUIWorkflowConfigurationError(
            f"Set {LOTVUE_COMFYUI_SCENE_WORKFLOW_ENV} to an owner-supplied ComfyUI API workflow"
        )
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    try:
        if not path.is_file():
            raise ComfyUIWorkflowConfigurationError("Configured ComfyUI workflow is missing or too large")
        with path.open("rb") as stream:
            raw = stream.read(_COMFY_SCENE_MAX_WORKFLOW_BYTES + 1)
        if not raw or len(raw) > _COMFY_SCENE_MAX_WORKFLOW_BYTES:
            raise ComfyUIWorkflowConfigurationError("Configured ComfyUI workflow is missing or too large")
        workflow = json.loads(raw)
    except ComfyUIWorkflowConfigurationError:
        raise
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ComfyUIWorkflowConfigurationError("Configured ComfyUI workflow could not be read") from exc

    if not isinstance(workflow, dict) or not 1 <= len(workflow) <= _COMFY_SCENE_MAX_NODES:
        raise ComfyUIWorkflowConfigurationError("ComfyUI workflow must be an API-format node graph")
    for node_id, node in workflow.items():
        if (
            not isinstance(node_id, str)
            or not node_id
            or not isinstance(node, dict)
            or not isinstance(node.get("class_type"), str)
            or not isinstance(node.get("inputs"), dict)
        ):
            raise ComfyUIWorkflowConfigurationError("ComfyUI workflow must be an API-format node graph")

    tokens = {
        _COMFY_SCENE_IMAGE_TOKEN: 0,
        _COMFY_SCENE_PROMPT_TOKEN: 0,
        _COMFY_SCENE_NEGATIVE_TOKEN: 0,
        _COMFY_SCENE_PREFIX_TOKEN: 0,
    }
    _count_workflow_tokens(workflow, tokens)
    if (
        tokens[_COMFY_SCENE_IMAGE_TOKEN] != 1
        or tokens[_COMFY_SCENE_PROMPT_TOKEN] != 1
        or tokens[_COMFY_SCENE_PREFIX_TOKEN] != 1
        or tokens[_COMFY_SCENE_NEGATIVE_TOKEN] > 1
    ):
        raise ComfyUIWorkflowConfigurationError(
            "Workflow must contain one input-image, scene-prompt, and output-prefix token; negative prompt is optional"
        )
    return workflow, tokens[_COMFY_SCENE_NEGATIVE_TOKEN]


def comfyui_scene_workflow_configured() -> bool:
    """Report valid local workflow configuration without contacting ComfyUI."""
    try:
        _load_comfyui_scene_workflow()
    except ComfyUIWorkflowConfigurationError:
        return False
    return True


def _scene_generation_prompt(scene_id: str) -> tuple[str, str]:
    description = PREMIUM_SCENES[scene_id]["description"]
    positive = (
        "Create a photorealistic automotive listing environment for the scene: "
        f"{description}. Replace or generate only the setting around the supplied vehicle. "
        "Keep the vehicle recognizable and do not add people, extra cars, logos, signage, or text."
    )
    negative = "extra vehicles, people, text, watermark, changed body shape, distorted wheels, altered badges"
    return positive, negative


def _remove_comfy_temp(path: Path | None, root: Path | None) -> None:
    if path is None or root is None:
        return
    try:
        if path.is_symlink() or not path.is_file():
            return
        resolved = path.resolve(strict=True)
        if resolved.is_relative_to(root.resolve()):
            resolved.unlink(missing_ok=True)
    except (OSError, RuntimeError, ValueError):
        pass


def generate_scene_with_comfyui(
    vehicle_png: str,
    scene_id: str,
    output_path: Optional[str] = None,
) -> str:
    """Run an explicitly configured ComfyUI API workflow; never fake an AI fallback.

    The owner supplies the graph through ``LOTVUE_COMFYUI_SCENE_WORKFLOW``. It
    must contain one each of the input-image, scene-prompt, and output-prefix
    tokens documented in the installation guide. A local deterministic
    composite remains a separate explicit engine choice.
    """
    if scene_id not in PREMIUM_SCENES:
        raise ValueError(f"Unknown scene: {scene_id}")
    workflow_template, has_negative = _load_comfyui_scene_workflow()

    from . import upscale as comfyui

    source_path = Path(vehicle_png).expanduser()
    source_resolved = source_path.resolve(strict=True)
    if not source_resolved.is_file() or source_path.is_symlink():
        raise ValueError("Vehicle image must be a regular, non-symlink file")
    target = Path(output_path) if output_path else source_path.with_name(
        f"{source_path.stem}_{scene_id}.jpg"
    )
    if target.resolve(strict=False) == source_resolved:
        raise ValueError("ComfyUI output must not overwrite the vehicle image")

    input_root = Path(comfyui.COMFY_INPUT_DIR).expanduser().resolve()
    output_root = Path(comfyui.COMFY_OUTPUT_DIR).expanduser().resolve()
    target_resolved = target.resolve(strict=False)
    if target_resolved.is_relative_to(input_root) or target_resolved.is_relative_to(output_root):
        raise ValueError("ComfyUI scene output must remain outside its managed input/output folders")
    input_root.mkdir(parents=True, exist_ok=True)
    upload_source = input_root / f"_lotvue_scene_source_{uuid.uuid4().hex}.png"
    staged_input = None
    generated_output = None
    output_prefix = f"lotvue_scene_{uuid.uuid4().hex}"
    temporary_output = None
    try:
        bounded_vehicle = _open_bounded_image(source_resolved, "RGBA")
        try:
            bounded_vehicle.save(upload_source, "PNG", optimize=True)
        finally:
            bounded_vehicle.close()
        if upload_source.stat().st_size > _COMFY_SCENE_MAX_FILE_BYTES:
            raise ValueError("Vehicle image exceeds the ComfyUI scene input byte limit")

        staged_name = comfyui.stage_image(str(upload_source))
        if (
            not isinstance(staged_name, str)
            or staged_name in {".", ".."}
            or len(staged_name) > 255
            or re.fullmatch(r"[A-Za-z0-9_.-]+", staged_name) is None
        ):
            raise RuntimeError("ComfyUI input staging returned an invalid filename")
        staged_input = input_root / staged_name
        if (
            not staged_input.is_file()
            or staged_input.is_symlink()
            or not staged_input.resolve(strict=True).is_relative_to(input_root)
        ):
            raise RuntimeError("ComfyUI input image was not staged")

        positive_prompt, negative_prompt = _scene_generation_prompt(scene_id)
        replacements = {
            _COMFY_SCENE_IMAGE_TOKEN: staged_name,
            _COMFY_SCENE_PROMPT_TOKEN: positive_prompt,
            _COMFY_SCENE_PREFIX_TOKEN: output_prefix,
        }
        if has_negative:
            replacements[_COMFY_SCENE_NEGATIVE_TOKEN] = negative_prompt
        workflow = _replace_workflow_tokens(workflow_template, replacements)
        if any(
            token in json.dumps(workflow, ensure_ascii=False)
            for token in (
                _COMFY_SCENE_IMAGE_TOKEN,
                _COMFY_SCENE_PROMPT_TOKEN,
                _COMFY_SCENE_NEGATIVE_TOKEN,
                _COMFY_SCENE_PREFIX_TOKEN,
            )
        ):
            raise ComfyUIWorkflowConfigurationError("ComfyUI workflow contains an unresolved LotVue token")

        timeout = _bounded_int(LOTVUE_COMFYUI_SCENE_TIMEOUT_ENV, 300, 10, 1800)
        generated_output = Path(comfyui.submit_and_wait(workflow, timeout=timeout))
        resolved_generated = generated_output.resolve(strict=True)
        if (
            not resolved_generated.is_relative_to(output_root)
            or not resolved_generated.is_file()
            or not resolved_generated.name.startswith(output_prefix)
            or generated_output.is_symlink()
        ):
            raise RuntimeError("ComfyUI returned an unexpected output file")
        if resolved_generated.stat().st_size > _COMFY_SCENE_MAX_FILE_BYTES:
            raise ValueError("ComfyUI scene output exceeds the configured byte limit")

        with Image.open(resolved_generated) as generated:
            width, height = generated.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise ValueError("ComfyUI scene output exceeds the configured pixel limit")
            generated.thumbnail(
                (MAX_PROCESSING_EDGE, MAX_PROCESSING_EDGE), Image.Resampling.LANCZOS
            )
            oriented = ImageOps.exif_transpose(generated)
            try:
                final = oriented.convert("RGB")
            finally:
                if oriented is not generated:
                    oriented.close()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary_output = target.with_name(f".{target.stem}.{uuid.uuid4().hex}.tmp.jpg")
            final.save(temporary_output, "JPEG", quality=92, optimize=True, progressive=True)
            os.replace(temporary_output, target)
        finally:
            final.close()
        return str(target)
    finally:
        _remove_comfy_temp(upload_source, input_root)
        if staged_input != upload_source:
            _remove_comfy_temp(staged_input, input_root)
        _remove_comfy_temp(generated_output, output_root)
        if generated_output is None and output_root.is_dir():
            for possible_output in output_root.glob(f"{output_prefix}*"):
                _remove_comfy_temp(possible_output, output_root)
        if temporary_output is not None:
            try:
                temporary_output.unlink(missing_ok=True)
            except OSError:
                pass
