"""Optional ComfyUI 4x image upscaling with an explicit 2x local fallback.

The current graph expects a ComfyUI installation with RealESRGAN_x4plus and
shared input/output folders. The service, model weights, and GPU are operator
provided; the source package does not verify model availability or image quality.
"""
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path, PurePosixPath

from PIL import Image

from app_paths import BASE_DIR, configured_path

COMFY = (os.getenv("COMFYUI_URL") or "http://127.0.0.1:8188").rstrip("/")
_COMFYUI_ROOT = configured_path("COMFYUI_ROOT", BASE_DIR / "ComfyUI")
COMFY_INPUT_DIR = str(configured_path("COMFYUI_INPUT", _COMFYUI_ROOT / "input"))
COMFY_OUTPUT_DIR = str(configured_path("COMFYUI_OUTPUT", _COMFYUI_ROOT / "output"))


def stage_image(image_path: str) -> str:
    """Atomically stage a regular image under ComfyUI's configured input root."""
    source_path = Path(image_path).expanduser()
    if source_path.is_symlink():
        raise ValueError("ComfyUI input must not be a symlink")
    source = source_path.resolve(strict=True)
    if not source.is_file():
        raise ValueError("ComfyUI input must be a regular file")
    name = source.name
    if (
        name in {".", ".."}
        or len(name) > 255
        or re.fullmatch(r"[A-Za-z0-9_.-]+", name) is None
    ):
        raise ValueError("ComfyUI input filename contains unsupported characters")

    input_root = Path(COMFY_INPUT_DIR).expanduser().resolve()
    input_root.mkdir(parents=True, exist_ok=True)
    destination = input_root / name
    if source == destination:
        return name

    temporary = input_root / f".lotvue-stage-{uuid.uuid4().hex}.tmp"
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return name


def upscale_workflow(image_name: str) -> dict:
    """Build ComfyUI workflow JSON for RealESRGAN 4x upscale."""
    return {
        "1": {
            "class_type": "LoadImage",
            "inputs": {"image": image_name}
        },
        "2": {
            "class_type": "UpscaleModelLoader",
            "inputs": {"model_name": "RealESRGAN_x4plus.pth"}
        },
        "3": {
            "class_type": "ImageUpscaleWithModel",
            "inputs": {
                "upscale_model": ["2", 0],
                "image": ["1", 0]
            }
        },
        "4": {
            "class_type": "SaveImage",
            "inputs": {
                "images": ["3", 0],
                "filename_prefix": "UPSCALE_LOTVUE"
            }
        }
    }


_COMFY_MAX_JSON_BYTES = 4 * 1024 * 1024
_COMFY_MAX_PROMPT_BYTES = 2 * 1024 * 1024


def _read_comfy_json(url: str, *, data: bytes | None = None, timeout: float = 30) -> dict:
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(_COMFY_MAX_JSON_BYTES + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise RuntimeError("ComfyUI request failed") from exc
    if len(payload) > _COMFY_MAX_JSON_BYTES:
        raise RuntimeError("ComfyUI response exceeded the configured limit")
    try:
        result = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("ComfyUI returned an invalid JSON response") from exc
    if not isinstance(result, dict):
        raise RuntimeError("ComfyUI returned an invalid JSON object")
    return result


def _comfy_output_path(image: dict) -> Path:
    """Resolve one ComfyUI history image beneath its configured output root."""
    filename = image.get("filename")
    subfolder = image.get("subfolder", "")
    if (
        not isinstance(filename, str)
        or not filename
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or ":" in filename
        or any(ord(char) < 32 for char in filename)
        or not isinstance(subfolder, str)
        or "\\" in subfolder
        or ":" in subfolder
    ):
        raise RuntimeError("ComfyUI returned an invalid output path")
    subpath = PurePosixPath(subfolder) if subfolder else PurePosixPath()
    if subpath.is_absolute() or any(part in {".", ".."} for part in subpath.parts):
        raise RuntimeError("ComfyUI returned an invalid output folder")

    root = Path(COMFY_OUTPUT_DIR).expanduser().resolve()
    candidate = root.joinpath(*subpath.parts, filename)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError("ComfyUI output file was not found") from exc
    if not resolved.is_relative_to(root) or not resolved.is_file() or candidate.is_symlink():
        raise RuntimeError("ComfyUI output path is outside the configured output folder")
    return resolved


def submit_and_wait(
    workflow: dict,
    timeout: int = 120,
    *,
    poll_interval: float = 2.0,
) -> str:
    """Submit a ComfyUI API graph and return its single safe output image path."""
    try:
        timeout_seconds = float(timeout)
        poll_seconds = float(poll_interval)
    except (TypeError, ValueError) as exc:
        raise ValueError("ComfyUI timeout values must be numbers") from exc
    if not 1 <= timeout_seconds <= 3600:
        raise ValueError("ComfyUI timeout must be between 1 and 3600 seconds")
    if not 0.05 <= poll_seconds <= 30:
        raise ValueError("ComfyUI poll interval must be between 0.05 and 30 seconds")

    data = json.dumps({"prompt": workflow}, separators=(",", ":")).encode("utf-8")
    if len(data) > _COMFY_MAX_PROMPT_BYTES:
        raise ValueError("ComfyUI workflow exceeds the configured size limit")
    deadline = time.monotonic() + timeout_seconds
    response = _read_comfy_json(
        f"{COMFY}/prompt", data=data, timeout=min(30, timeout_seconds)
    )
    prompt_id = response.get("prompt_id")
    if "error" in response or not isinstance(prompt_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,128}", prompt_id
    ):
        raise RuntimeError("ComfyUI rejected the workflow")

    history_url = f"{COMFY}/history/{urllib.parse.quote(prompt_id, safe='')}"
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"ComfyUI workflow timed out after {timeout_seconds:g}s")
        time.sleep(min(poll_seconds, remaining))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"ComfyUI workflow timed out after {timeout_seconds:g}s")
        history = _read_comfy_json(history_url, timeout=min(30, remaining))
        entry = history.get(prompt_id)
        if not isinstance(entry, dict):
            continue

        status = entry.get("status")
        status = status if isinstance(status, dict) else {}
        status_text = status.get("status_str")
        if status_text in {"error", "interrupted"}:
            raise RuntimeError("ComfyUI workflow failed")
        outputs = entry.get("outputs")
        images = []
        if isinstance(outputs, dict):
            for node_output in outputs.values():
                if not isinstance(node_output, dict):
                    continue
                node_images = node_output.get("images", [])
                if isinstance(node_images, list):
                    images.extend(
                        image for image in node_images
                        if isinstance(image, dict) and image.get("type", "output") == "output"
                    )
        completed = status.get("completed") is True or status_text in {"success", "completed"}
        if not completed:
            continue
        if len(images) != 1:
            raise RuntimeError("ComfyUI workflow must produce exactly one output image")
        return str(_comfy_output_path(images[0]))


def upscale_image_with_status(img: Image.Image) -> tuple[Image.Image, dict]:
    """Return a 4x ComfyUI result or an explicit 2x Lanczos fallback."""
    os.makedirs(COMFY_INPUT_DIR, exist_ok=True)
    temp_path = Path(COMFY_INPUT_DIR) / f"_lotvue_upscale_{uuid.uuid4().hex}.png"
    output_path = None
    try:
        img.save(temp_path, "PNG")
        workflow = upscale_workflow(stage_image(str(temp_path)))
        output_path = Path(submit_and_wait(workflow))
        with Image.open(output_path) as source:
            result = source.copy()
        expected_size = (img.width * 4, img.height * 4)
        if result.size != expected_size:
            result.close()
            raise RuntimeError("ComfyUI result dimensions did not match the requested 4x scale")
        return result, {"status": "comfyui_4x", "scale": 4}
    except Exception as exc:
        print(f"[Upscale] ComfyUI 4x unavailable ({type(exc).__name__}); using Lanczos 2x fallback")
        return img.resize((img.width * 2, img.height * 2), Image.Resampling.LANCZOS), {
            "status": "lanczos_2x_fallback", "scale": 2,
        }
    finally:
        temp_path.unlink(missing_ok=True)
        if output_path is not None:
            try:
                output_root = Path(COMFY_OUTPUT_DIR).resolve()
                resolved_output = output_path.resolve(strict=True)
                if resolved_output.is_relative_to(output_root) and resolved_output.is_file():
                    resolved_output.unlink(missing_ok=True)
            except (OSError, RuntimeError, ValueError):
                pass


def upscale_image(img: Image.Image) -> Image.Image:
    """Backwards-compatible image-only API; inspect ``upscale_image_with_status`` for fallbacks."""
    return upscale_image_with_status(img)[0]
