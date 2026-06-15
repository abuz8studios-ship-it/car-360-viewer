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
from pathlib import Path
from PIL import Image

COMFY = "http://127.0.0.1:8188"
COMFY_INPUT_DIR = r".\ComfyUI\input"
COMFY_OUTPUT_DIR = r".\ComfyUI\output"


def stage_image(image_path: str) -> str:
    """Copy image to ComfyUI input directory (LoadImage only reads from input/)."""
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


def upscale_image(img: Image.Image) -> Image.Image:
    """
    Upscale an image 4x using RealESRGAN via ComfyUI.
    Falls back to Lanczos resize if ComfyUI is unreachable.
    """
    try:
        # Save temp image
        temp_path = os.path.join(COMFY_INPUT_DIR, "_lotvue_upscale_temp.png")
        img.save(temp_path, "PNG")

        # Stage and submit
        name = stage_image(temp_path)
        workflow = upscale_workflow(name)
        output_path = submit_and_wait(workflow)

        # Load result
        result = Image.open(output_path)
        return result

    except Exception as e:
        print(f"[Upscale] ComfyUI upscale failed ({e}), using Lanczos 2x fallback")
        w, h = img.size
        return img.resize((w * 2, h * 2), Image.LANCZOS)
