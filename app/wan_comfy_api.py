"""
WAN 2.2 I2V via ComfyUI HTTP API (:8188) — uses the LOCAL model files ComfyUI
already serves (no HuggingFace download). Decodes to PNG frames via SaveImage,
then assembles MP4 with FFmpeg.

Importable:  from wan_comfy_api import generate_wan_video_via_api
Standalone:  python wan_comfy_api.py --image <jpg> --out <mp4> [--prompt "..."]
"""

import os
import sys
import json
import glob
import time
import shutil
import argparse
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188")
COMFYUI_OUTPUT = os.environ.get("COMFYUI_OUTPUT", r".\ComfyUI\output")
FFMPEG = os.environ.get("FFMPEG_PATH", r".\AI\ffmpeg\ffmpeg.exe")

UNET = "wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors"
CLIP = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
CLIP_VISION = "clip_vision_h.safetensors"
VAE = os.environ.get("WAN_VAE_NAME", "wan_2.1_vae_official.safetensors")  # WAN 2.2 14B I2V pairs with the 2.1 VAE (16-ch)

DEFAULT_PROMPT = ("A car slowly rotating on a polished studio floor, cinematic "
                  "lighting, professional automotive commercial, smooth camera "
                  "motion, high quality")
NEGATIVE = "blurry, low quality, distorted, watermark, text, people, hands"


def _post(path, payload, timeout=30):
    req = urllib.request.Request(f"{COMFYUI_URL}{path}", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def _get(path, timeout=15):
    return json.loads(urllib.request.urlopen(f"{COMFYUI_URL}{path}", timeout=timeout).read())


def _upload_image(image_path: str) -> str:
    with open(image_path, "rb") as f:
        img = f.read()
    boundary = "FormBoundaryLotVue360"
    name = os.path.basename(image_path)
    body = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="{name}"\r\n'
            f"Content-Type: image/jpeg\r\n\r\n").encode() + img + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{COMFYUI_URL}/upload/image", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                                 method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=30).read()).get("name", name)


def _build_workflow(uploaded: str, prompt: str, width: int, height: int,
                    num_frames: int, steps: int, cfg: float, seed: int, prefix: str):
    return {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": UNET, "weight_dtype": "fp8_e4m3fn"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP, "type": "wan"}},
        "3": {"class_type": "CLIPVisionLoader", "inputs": {"clip_name": CLIP_VISION}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "5": {"class_type": "LoadImage", "inputs": {"image": uploaded}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": NEGATIVE, "clip": ["2", 0]}},
        "8": {"class_type": "CLIPVisionEncode",
              "inputs": {"clip_vision": ["3", 0], "image": ["5", 0], "crop": "center"}},
        "9": {"class_type": "WanImageToVideo",
              "inputs": {"positive": ["6", 0], "negative": ["7", 0], "vae": ["4", 0],
                         "width": width, "height": height, "length": num_frames,
                         "batch_size": 1, "clip_vision_output": ["8", 0],
                         "start_image": ["5", 0]}},
        "10": {"class_type": "KSampler",
               "inputs": {"model": ["1", 0], "seed": seed, "steps": steps, "cfg": cfg,
                          "sampler_name": "euler", "scheduler": "linear_quadratic",
                          "positive": ["9", 0], "negative": ["9", 1],
                          "latent_image": ["9", 2], "denoise": 1.0}},
        "11": {"class_type": "VAEDecode", "inputs": {"samples": ["10", 0], "vae": ["4", 0]}},
        "12": {"class_type": "SaveImage",
               "inputs": {"images": ["11", 0], "filename_prefix": prefix}},
    }


def generate_wan_video_via_api(image_path: str, output_path: str, prompt: str = DEFAULT_PROMPT,
                               width: int = 720, height: int = 480, num_frames: int = 49,
                               steps: int = 20, cfg: float = 6.0, seed: int = 42,
                               fps: int = 16, timeout_s: int = 900,
                               log=print) -> bool:
    """Generate an I2V clip through the running ComfyUI. Returns True on success."""
    try:
        _get("/system_stats", timeout=8)
    except Exception as e:
        log(f"[WAN-API] ComfyUI not reachable at {COMFYUI_URL}: {e}")
        return False

    prefix = f"lotvue_wan_{int(time.time())}"
    uploaded = _upload_image(image_path)
    log(f"[WAN-API] uploaded {uploaded}; submitting workflow (frames={num_frames}, steps={steps})")
    wf = _build_workflow(uploaded, prompt, width, height, num_frames, steps, cfg, seed, prefix)
    try:
        result = _post("/prompt", {"prompt": wf})
    except urllib.error.HTTPError as e:
        log(f"[WAN-API] submit failed HTTP {e.code}: {e.read().decode()[:500]}")
        return False
    prompt_id = result.get("prompt_id")
    if not prompt_id:
        log(f"[WAN-API] no prompt_id: {result}")
        return False

    start = time.time()
    while time.time() - start < timeout_s:
        time.sleep(5)
        try:
            hist = _get(f"/history/{prompt_id}")
            if prompt_id in hist:
                entry = hist[prompt_id]
                st = entry.get("status", {})
                if st.get("status_str") == "error":
                    msgs = [m for m in st.get("messages", []) if m[0] == "execution_error"]
                    log(f"[WAN-API] execution error: {str(msgs)[:400]}")
                    return False
                if st.get("completed"):
                    break
        except Exception as e:
            log(f"[WAN-API] poll error: {e}")
    else:
        log(f"[WAN-API] timeout after {timeout_s}s")
        return False

    elapsed = time.time() - start
    frames = sorted(glob.glob(os.path.join(COMFYUI_OUTPUT, f"{prefix}_*.png")))
    log(f"[WAN-API] completed in {elapsed:.0f}s — {len(frames)} frames")
    if not frames:
        return False

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    pattern = os.path.join(COMFYUI_OUTPUT, f"{prefix}_%05d_.png")
    cmd = [FFMPEG, "-y", "-framerate", str(fps), "-i", pattern,
           "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
           "-c:v", "libx264", "-preset", "fast", "-crf", "20", output_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not os.path.exists(output_path):
        # fallback: concat demuxer (handles any numbering)
        lst = Path(output_path).with_suffix(".frames.txt")
        with open(lst, "w", encoding="utf-8") as fh:
            for f in frames:
                fh.write(f"file '{Path(f).as_posix()}'\nduration {1/fps:.4f}\n")
            fh.write(f"file '{Path(frames[-1]).as_posix()}'\n")
        cmd = [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
               "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
               "-c:v", "libx264", "-preset", "fast", "-crf", "20", output_path]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        try:
            lst.unlink()
        except OSError:
            pass
        if r.returncode != 0 or not os.path.exists(output_path):
            log(f"[WAN-API] ffmpeg assembly failed: {r.stderr[-300:]}")
            return False
    # tidy ComfyUI output frames
    for f in frames:
        try:
            os.remove(f)
        except OSError:
            pass
    log(f"[WAN-API] SUCCESS -> {output_path} ({os.path.getsize(output_path):,} bytes)")
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--out", default=r"C:\Temp\lotvue_wan_test.mp4")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--frames", type=int, default=49)
    ap.add_argument("--steps", type=int, default=20)
    a = ap.parse_args()
    ok = generate_wan_video_via_api(a.image, a.out, a.prompt, num_frames=a.frames, steps=a.steps)
    print("=== WAN VIA COMFYUI API:", "PASS ===" if ok else "FAIL ===")
    sys.exit(0 if ok else 1)
