"""
glb_gen.py — LotVue AI-3D lane: 4 dealer photos -> Hunyuan3D-2mv (ComfyUI native)
-> interactive GLB with full orbit + fly-over in <lotvue-viewer>.

Usage: python glb_gen.py --vin 4JGFB5KB6NA764948 [--steps 8] [--octree 384]
Requires: ComfyUI running on :8188 with hunyuan3d-dit-v2-mv-turbo_fp16.safetensors.
"""
import argparse, json, shutil, sys, time, urllib.request
from pathlib import Path

BASE = Path(__file__).parent
STORAGE = BASE / "storage"
COMFY = "http://127.0.0.1:8188"
COMFY_INPUT = Path(r"E:\ABU\ComfyUI\input")
COMFY_OUTPUT = Path(r"E:\ABU\ComfyUI\output")
CKPT = "hunyuan3d-dit-v2-mv-turbo_fp16.safetensors"
VIEWS = ["front", "right", "back", "left"]   # walkaround order assumption

def pick_views(vin: str):
    """Choose 4 photos spread around the car from the best available source dir."""
    vdir = STORAGE / vin
    for sub in ("_3dphotos", "exterior", "_import", "photos"):
        d = vdir / sub
        if d.exists():
            imgs = sorted(p for p in d.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
            if len(imgs) >= 4:
                try:  # angle-aware ordering when available
                    sys.path.insert(0, str(BASE))
                    from gaussian_3d import order_photos_by_angle
                    ordered = order_photos_by_angle(imgs) or imgs
                    flat = []
                    for it in ordered:  # may be paths, (path, angle), or nested lists
                        if isinstance(it, (list, tuple)):
                            it = it[0]
                        flat.append(Path(it))
                    imgs = [p for p in flat if p.exists()] or imgs
                except Exception:
                    pass
                n = len(imgs)
                return [imgs[0], imgs[n // 4], imgs[n // 2], imgs[(3 * n) // 4]]
    raise SystemExit(f"[glb] no photo source with >=4 images for {vin}")

def prep(img_path: Path, out_name: str) -> str:
    """rembg -> white composite -> centered square 1024 png in ComfyUI input dir."""
    from PIL import Image
    from rembg import remove, new_session
    if not hasattr(prep, "_s"):
        prep._s = new_session("isnet-general-use", providers=["CPUExecutionProvider"])
    im = Image.open(img_path).convert("RGB")
    cut = remove(im, session=prep._s)
    bbox = cut.getbbox()
    if bbox:
        cut = cut.crop(bbox)
    side = int(max(cut.size) * 1.15)
    canvas = Image.new("RGBA", (side, side), (255, 255, 255, 255))
    canvas.paste(cut, ((side - cut.width) // 2, (side - cut.height) // 2), cut)
    out = COMFY_INPUT / out_name
    canvas.convert("RGB").resize((1024, 1024), Image.LANCZOS).save(out, "PNG")
    return out_name

def build_graph(vin: str, names, steps, octree, seed):
    g = {
        "1": {"class_type": "ImageOnlyCheckpointLoader", "inputs": {"ckpt_name": CKPT}},
        "10": {"class_type": "Hunyuan3Dv2ConditioningMultiView", "inputs": {}},
        "11": {"class_type": "EmptyLatentHunyuan3Dv2",
               "inputs": {"resolution": 3072, "batch_size": 1}},
        "12": {"class_type": "KSampler",
               "inputs": {"model": ["1", 0], "positive": ["10", 0], "negative": ["10", 1],
                          "latent_image": ["11", 0], "seed": seed, "steps": steps, "cfg": 2.0,
                          "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "13": {"class_type": "VAEDecodeHunyuan3D",
               "inputs": {"samples": ["12", 0], "vae": ["1", 2],
                          "num_chunks": 8000, "octree_resolution": octree}},
        "14": {"class_type": "VoxelToMesh",
               "inputs": {"voxel": ["13", 0], "algorithm": "surface net", "threshold": 0.6}},
        "15": {"class_type": "SaveGLB",
               "inputs": {"mesh": ["14", 0], "filename_prefix": f"3d/lv_{vin}"}},
    }
    for k, (view, name) in enumerate(zip(VIEWS, names)):
        img_id, enc_id = str(2 + k), str(6 + k)
        g[img_id] = {"class_type": "LoadImage", "inputs": {"image": name}}
        g[enc_id] = {"class_type": "CLIPVisionEncode",
                     "inputs": {"clip_vision": ["1", 1], "image": [img_id, 0], "crop": "center"}}
        g["10"]["inputs"][view] = [enc_id, 0]
    return g

def run(vin, steps, octree, seed):
    photos = pick_views(vin)
    print(f"[glb] views: {[p.name for p in photos]}")
    names = [prep(p, f"lv_{vin}_{v}.png") for p, v in zip(photos, VIEWS)]
    print("[glb] preprocessed (rembg/white/1024)")
    body = json.dumps({"prompt": build_graph(vin, names, steps, octree, seed)}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request(
        COMFY + "/prompt", data=body, headers={"Content-Type": "application/json"}), timeout=30))
    pid = r["prompt_id"]
    print(f"[glb] queued {pid}")
    t0 = time.time()
    while time.time() - t0 < 1800:
        time.sleep(5)
        h = json.load(urllib.request.urlopen(f"{COMFY}/history/{pid}", timeout=30))
        if pid in h:
            entry = h[pid]
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                msgs = [m for m in status.get("messages", []) if m[0] == "execution_error"]
                raise SystemExit(f"[glb] comfy error: {json.dumps(msgs)[:600]}")
            if entry.get("outputs"):
                break
    else:
        raise SystemExit("[glb] timeout")
    # find the newest glb for this vin
    cands = sorted(COMFY_OUTPUT.glob(f"3d/lv_{vin}*.glb"), key=lambda p: p.stat().st_mtime)
    if not cands:
        cands = sorted(COMFY_OUTPUT.rglob("*.glb"), key=lambda p: p.stat().st_mtime)
    src = cands[-1]
    dst = STORAGE / vin / "model.glb"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    _tint_with_real_color(vin, dst)
    print(f"[glb] DONE {dst} ({dst.stat().st_size/1e6:.1f}MB) in {time.time()-t0:.0f}s")
    return dst

# real-world exterior color names -> hex, so the clay mesh renders as the actual car
_COLORS = {
    "black": "#0d0d0f", "white": "#eef0f2", "silver": "#c7cad0", "gray": "#6f747c",
    "grey": "#6f747c", "red": "#8f1620", "blue": "#12305f", "dark blue": "#0d1f42",
    "green": "#1d3d2a", "brown": "#3c2a1c", "tan": "#b7a184", "gold": "#b79457",
    "beige": "#c8b79b", "orange": "#c2521f", "yellow": "#d9b021", "burgundy": "#4a1220",
    "maroon": "#4a1220", "charcoal": "#2b2e33", "gunmetal": "#3a3f47",
}
def _tint_with_real_color(vin, glb_path):
    try:
        meta_f = STORAGE / vin / "meta.json"
        color = ""
        if meta_f.exists():
            color = (json.loads(meta_f.read_text("utf-8")).get("color") or "").lower()
        hexv = next((v for k, v in _COLORS.items() if k in color), None)
        if not hexv:
            return
        import trimesh, numpy as np
        rgb = tuple(int(hexv[i:i+2], 16) for i in (1, 3, 5))
        scene = trimesh.load(glb_path)
        geoms = scene.geometry.values() if hasattr(scene, "geometry") else [scene]
        for g in geoms:
            g.visual = trimesh.visual.TextureVisuals(
                material=trimesh.visual.material.PBRMaterial(
                    baseColorFactor=[rgb[0]/255, rgb[1]/255, rgb[2]/255, 1.0],
                    metallicFactor=0.85, roughnessFactor=0.35))
        scene.export(glb_path)
        print(f"[glb] tinted with real color '{color}' -> {hexv}")
    except Exception as e:
        print(f"[glb] tint skipped: {e}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--vin", required=True)
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--octree", type=int, default=384)
    ap.add_argument("--seed", type=int, default=809)
    a = ap.parse_args()
    run(a.vin.upper(), a.steps, a.octree, a.seed)
