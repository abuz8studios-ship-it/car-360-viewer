"""clip_classify.py — batch dealer-photo classifier (exterior / interior / detail).

Zero-shot CLIP-L/14-336 (openai/clip-vit-large-patch14-336, HF cache).
Proven 5/5 on the eye-audited truth set 2026-07-06 — replaces the geometric
heuristic (22% error) and qwen2.5vl:3b (2/5 on closeups) as PRIMARY.

Run ISOLATED from the server process (user-site shadows break transformers):
    E:\\Miniconda3\\envs\\lotvue\\python.exe -s clip_classify.py <photo_dir>

Prints one JSON object to stdout:
    {"ok": true, "photos": [{"file": "p_0001.jpg", "category": "exterior",
                             "confidence": 0.91, "margin": 0.045}, ...]}
Exit code 0 even on per-photo errors (they get category "detail", the safe
bucket — never pollutes the 360 spin, never hides a cabin shot in exteriors).
"""
import json
import math
import os
import sys
from pathlib import Path

MODEL_ID = "openai/clip-vit-large-patch14-336"

PROMPTS = {
    "exterior": [
        "a photo of a whole car seen from outside",
        "a used car for sale photographed outdoors, full body visible",
        "side view of a parked car",
        "front three-quarter view of a vehicle on a dealership lot",
        "rear view of a car outdoors",
    ],
    "interior": [
        "the interior cabin of a car",
        "car dashboard and steering wheel",
        "car seats photographed inside the cabin",
        "a view from inside a vehicle",
        "burl wood trim and round air vents on a car dashboard",
        "the open trunk or cargo area of a car",
        "a car with its door open showing the interior cabin",
        "the front seats and steering wheel seen through an open car door",
        "rear leather seats photographed through an open car door",
        "car instrument cluster showing speedometer and gauges",
        "closeup of a car infotainment touchscreen display",
        "an open car door photographed to show the door panel",
        "an open tailgate showing the cargo area of an SUV",
        "a car backup camera screen showing a parking view",
        "a dashboard display screen photographed up close",
    ],
    "detail": [
        "a closeup of a car wheel and tire",
        "a closeup macro photo of a car badge or emblem",
        "a car engine bay",
        "a vehicle window sticker with text",
        "a closeup of a single car part",
    ],
}


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return min(max(value, minimum), maximum)


def main(photo_dir: str) -> dict:
    import torch
    from PIL import Image, ImageOps
    from transformers import CLIPModel, CLIPProcessor

    root = Path(photo_dir)
    if root.is_symlink() or not root.is_dir():
        return {"ok": False, "error": "photo_dir must be a regular directory"}
    max_photos = _bounded_env_int("LOTVUE_MAX_IMPORT_PHOTOS", 100, 4, 500)
    max_pixels = _bounded_env_int("LOTVUE_MAX_IMPORT_IMAGE_PIXELS", 16_000_000, 1_000_000, 40_000_000)
    photos = sorted(
        photo for photo in root.glob("p_*.jpg")
        if photo.is_file() and not photo.is_symlink()
    )
    if not photos:
        return {"ok": False, "error": f"no regular p_*.jpg files in {photo_dir}"}
    if len(photos) > max_photos:
        return {"ok": False, "error": f"photo set exceeds {max_photos} files"}

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = CLIPModel.from_pretrained(MODEL_ID).to(device)
    processor = CLIPProcessor.from_pretrained(MODEL_ID)
    model.eval()

    out = []
    with torch.inference_mode():
        class_embeds = {}
        for class_name, prompt_list in PROMPTS.items():
            inputs = processor(text=prompt_list, return_tensors="pt", padding=True)
            inputs = {key: value.to(device) for key, value in inputs.items()}
            text_features = model.get_text_features(**inputs)
            normalized = text_features / text_features.norm(dim=-1, keepdim=True).clamp_min(1e-12)
            class_embeds[class_name] = normalized
            del inputs, text_features, normalized

        for photo in photos:
            image = None
            try:
                with Image.open(photo) as source:
                    if source.width * source.height > max_pixels:
                        raise ValueError("image exceeds the configured pixel limit")
                    oriented = ImageOps.exif_transpose(source)
                    try:
                        image = oriented.convert("RGB")
                    finally:
                        if oriented is not source:
                            oriented.close()
                # CLIP will rescale to its input size; a bounded pre-resize avoids
                # allocating tensors proportional to full-resolution upload size.
                image.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
                inputs = processor(images=image, return_tensors="pt")
                inputs = {key: value.to(device) for key, value in inputs.items()}
                image_features = model.get_image_features(**inputs)
                image_features = image_features / image_features.norm(dim=-1, keepdim=True).clamp_min(1e-12)
                image_features = image_features.squeeze(0)
                per_class = {}
                for class_name, embeds in class_embeds.items():
                    similarities = embeds @ image_features
                    best_index = int(similarities.argmax().item())
                    best_score = float(similarities[best_index].item())
                    per_class[class_name] = (best_score, PROMPTS[class_name][best_index])
                ordered = sorted(per_class.items(), key=lambda item: -item[1][0])
                margin = ordered[0][1][0] - ordered[1][1][0]
                confidence = min(0.99, max(0.30, 0.70 + margin * 5.0))
                if not math.isfinite(confidence) or not math.isfinite(margin):
                    raise ValueError("classifier returned a non-finite score")
                category = ordered[0][0]
                if category not in PROMPTS:
                    raise ValueError("classifier returned an unsupported category")
                out.append({
                    "file": photo.name,
                    "category": category,
                    "confidence": round(confidence, 2),
                    "margin": round(margin, 4),
                    "won_by": per_class[category][1],
                })
                del inputs, image_features, per_class
            except Exception as exc:
                out.append({
                    "file": photo.name,
                    "category": "detail",
                    "confidence": 0.30,
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                })
            finally:
                if image is not None:
                    image.close()
    return {"ok": True, "model": MODEL_ID, "photos": out}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(json.dumps({"ok": False, "error": "usage: clip_classify.py <photo_dir>"}))
        sys.exit(0)
    try:
        print(json.dumps(main(sys.argv[1])))
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
    sys.exit(0)
