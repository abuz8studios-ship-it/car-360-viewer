"""
LotVue Pro — Image Upscale via ComfyUI RealESRGAN
Uses ComfyUI API to upscale images 4x using RealESRGAN_x4plus.
Zero cloud cost — runs on local RTX 5090.
"""
import os
import json
import time
import shutil
import urllib.request
import uuid
from pathlib import Path
from PIL import Image

from app_paths import BASE_DIR, configured_path

COMFY = (os.getenv("COMFYUI_URL") or "http://127.0.0.1:8188").rstrip("/")
_COMFYUI_ROOT = configured_path("COMFYUI_ROOT", BASE_DIR / "ComfyUI")
COMFY_INPUT_DIR = str(configured_path("COMFYUI_INPUT", _COMFYUI_ROOT / "input"))
COMFY_OUTPUT_DIR = str(configured_path("COMFYUI_OUTPUT", _COMFYUI_ROOT / "output"))


def stage_image(image_path: str) -> str:
    """Copy image to ComfyUI input directory (LoadImage only reads from input/)."""
    os.makedirs(COMFY_INPUT_DIR, exist_ok=True)
    name = os.path.basename(image_path)
    dest = os.path.join(COMFY_INPUT_DIR, name)
    if os.path.abspath(image_path) != os.path.abspath(dest):
        shutil.copy2(image_path, dest)
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


def submit_and_wait(workflow: dict, timeout: int = 120) -> str:
    """Submit workflow to ComfyUI and wait for output."""
    data = json.dumps({"prompt": workflow}).encode()
    req = urllib.request.Request(
        f"{COMFY}/prompt",
        data=data,
        headers={"Content-Type": "application/json"}
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=30).read())

    if "error" in resp:
        raise RuntimeError(f"ComfyUI rejected: {resp}")

    prompt_id = resp.get("prompt_id")
    t0 = time.time()

    while True:
        time.sleep(2)
        elapsed = int(time.time() - t0)
        history = json.loads(
            urllib.request.urlopen(f"{COMFY}/history/{prompt_id}", timeout=30).read()
        )

        if prompt_id in history:
            status = history[prompt_id].get("status", {})
            if status.get("completed"):
                for _, outputs in history[prompt_id].get("outputs", {}).items():
                    for img in outputs.get("images", []):
                        filename = img.get("filename", "")
                        if filename:
                            return os.path.join(COMFY_OUTPUT_DIR, filename)
                raise RuntimeError("ComfyUI completed but no output found")
            if status.get("status_str") == "error":
                raise RuntimeError(f"ComfyUI error: {status}")

        if elapsed > timeout:
            raise TimeoutError(f"ComfyUI upscale timed out after {timeout}s")


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
