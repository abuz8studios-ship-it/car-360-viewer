"""clip_fullcar.py — second-stage CLIP filter for studio_spin: among frames
already classified 'exterior', keep only WHOLE-CAR shots (drop close-ups,
doors-open, part shots, matting ghosts). Same zero-shot CLIP-L/14-336 as
clip_classify.py, same isolated invocation:

    E:\\Miniconda3\\envs\\lotvue\\python.exe -s clip_fullcar.py <photo_dir>

Prints {"ok": true, "photos": [{"file": ..., "full_car": true, "score": 0.93}]}
Why it exists (measured 2026-08-12): geometric set-statistics pruning removed
real side views (aspect 2.7-3.4) and KEPT headlight close-ups (aspect ~1.9) —
whole-vs-part is a semantic question, not a shape-statistics one.
"""
import json
import sys
from pathlib import Path

MODEL_ID = "openai/clip-vit-large-patch14-336"

FULL = [
    "a photo of an entire car, the whole vehicle fits in the frame",
    "a complete car photographed from a distance, all doors closed",
    "front view of a whole parked car",
    "rear view of a whole parked car",
    "side profile of a whole car, full length visible",
    "three-quarter view of a complete vehicle on a car lot",
]
PART = [
    "a close-up of one part of a car",
    "a car headlight photographed up close",
    "a car grille or air vent close-up",
    "a close-up of a car wheel and tire",
    "a car with its doors wide open",
    "a close-up of a car roof or window",
    "the rear badge and taillight photographed up close",
    "a blurry pale smeared image with no clear subject",
]


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
    prompts = FULL + PART
    # pairwise doors test — solidity measurably fails (doors-open 0.96-0.97
    # vs clean 0.85-0.97, 2026-08-12); CLIP answers the binary directly
    doors = ["a parked car with all of its doors and trunk closed",
             "a car with one or more doors or trunk wide open"]
    out = []
    with torch.no_grad():
        for p in photos:
            try:
                img = ImageOps.exif_transpose(Image.open(p).convert("RGB"))
                inp = proc(text=prompts, images=img, return_tensors="pt", padding=True)
                probs = model(**inp).logits_per_image.softmax(dim=1)[0]
                full_score = float(probs[:len(FULL)].sum())
                inp2 = proc(text=doors, images=img, return_tensors="pt", padding=True)
                d = model(**inp2).logits_per_image.softmax(dim=1)[0]
                doors_open = float(d[1])
                out.append({"file": p.name, "full_car": full_score >= 0.5,
                            "score": round(full_score, 3),
                            "doors_open": round(doors_open, 3)})
            except Exception as e:
                out.append({"file": p.name, "full_car": False, "score": 0.0,
                            "doors_open": 1.0, "error": str(e)[:120]})
    return {"ok": True, "photos": out}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(json.dumps({"ok": False, "error": "usage: clip_fullcar.py <dir>"}))
        sys.exit(0)
    print(json.dumps(main(sys.argv[1])))
    sys.exit(0)
