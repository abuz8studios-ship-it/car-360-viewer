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


def main(photo_dir: str) -> dict:
    import torch
    from PIL import Image, ImageOps
    from transformers import CLIPModel, CLIPProcessor

    photos = sorted(Path(photo_dir).glob("p_*.jpg"))
    if not photos:
        return {"ok": False, "error": f"no p_*.jpg in {photo_dir}"}

    model = CLIPModel.from_pretrained(MODEL_ID)
    proc = CLIPProcessor.from_pretrained(MODEL_ID)
    model.eval()

    out = []
    with torch.no_grad():
        class_embeds = {}
        for cls, plist in PROMPTS.items():
            ti = proc(text=plist, return_tensors="pt", padding=True)
            te = model.get_text_features(**ti)
            # keep every prompt vector: class score = BEST prompt match, not the
            # averaged embedding — multi-modal classes (gauges vs seats vs door
            # panels) lose their specificity when averaged.
            class_embeds[cls] = te / te.norm(dim=-1, keepdim=True)

        for p in photos:
            try:
                im = ImageOps.exif_transpose(Image.open(p)).convert("RGB")
                ii = proc(images=im, return_tensors="pt")
                ie = model.get_image_features(**ii)
                ie = (ie / ie.norm(dim=-1, keepdim=True)).squeeze(0)
                per_class = {}
                for c, e in class_embeds.items():
                    v = e @ ie
                    j = int(v.argmax())
                    per_class[c] = (float(v[j]), PROMPTS[c][j])
                sims = {c: s for c, (s, _) in per_class.items()}
                ordered = sorted(sims.items(), key=lambda kv: -kv[1])
                margin = ordered[0][1] - ordered[1][1]
                out.append({"file": p.name, "category": ordered[0][0],
                            "confidence": round(min(0.99, 0.70 + margin * 5.0), 2),
                            "margin": round(margin, 4),
                            "won_by": per_class[ordered[0][0]][1]})
            except Exception as e:
                out.append({"file": p.name, "category": "detail",
                            "confidence": 0.30, "error": f"{type(e).__name__}: {e}"})
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
