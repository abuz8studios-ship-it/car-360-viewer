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
import tempfile
import argparse
import subprocess
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

from app_paths import BASE_DIR, configured_path

COMFYUI_URL = (os.getenv("COMFYUI_URL") or "http://127.0.0.1:8188").rstrip("/")
_COMFYUI_ROOT = configured_path("COMFYUI_ROOT", BASE_DIR / "ComfyUI")
COMFYUI_OUTPUT = str(configured_path("COMFYUI_OUTPUT", _COMFYUI_ROOT / "output"))
FFMPEG = os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg") or "ffmpeg"
POLL_SECONDS = 5  # how often a running ComfyUI job is checked

UNET = "wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors"
CLIP = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
CLIP_VISION = "clip_vision_h.safetensors"
# WAN 2.2 14B pairs with the 2.1 VAE (16-ch); this is its file name in
# Comfy-Org/Wan_2.2_ComfyUI_Repackaged (split_files/vae/).
VAE = os.environ.get("WAN_VAE_NAME", "wan_2.1_vae.safetensors")
# First-last-frame gap synthesis (the 360-spin arc filler): WAN 2.2 Fun-InP 14B,
# conditioned on BOTH endpoint photos, sampled in the canonical two-stage
# high-noise -> low-noise chain.
FLF_UNET_HIGH = "wan2.2_fun_inpaint_high_noise_14B_fp8_scaled.safetensors"
FLF_UNET_LOW = "wan2.2_fun_inpaint_low_noise_14B_fp8_scaled.safetensors"
FLF_PROMPT = ("Camera smoothly orbiting around a parked car on a clean studio floor, "
              "the car stays perfectly still, seamless rotation, professional automotive "
              "360 photography, consistent lighting, high quality")

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


def _collect_output_frames(entry: dict, prefix: str, out_dir: str, node: str = "12") -> list:
    """Copy a finished job's SaveImage frames into out_dir as flf_NNNN.png.
    Frames are read over HTTP (/view), so ComfyUI may run from its own folder
    (e.g. the portable Windows build) rather than <app>/ComfyUI. Copies ComfyUI
    left in COMFYUI_OUTPUT on this machine are removed afterwards."""
    images = sorted(
        (img for img in ((entry.get("outputs") or {}).get(node) or {}).get("images", [])
         if img.get("filename", "").startswith(prefix)),
        key=lambda img: img["filename"],
    )
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    moved = []
    if images:
        for i, img in enumerate(images):
            query = urllib.parse.urlencode({"filename": img["filename"],
                                            "subfolder": img.get("subfolder", ""),
                                            "type": img.get("type", "output")})
            dst = os.path.join(out_dir, f"flf_{i:04d}.png")
            with urllib.request.urlopen(f"{COMFYUI_URL}/view?{query}", timeout=60) as resp, \
                    open(dst, "wb") as fh:
                shutil.copyfileobj(resp, fh)
            moved.append(dst)
            local = os.path.join(COMFYUI_OUTPUT, img.get("subfolder", ""), img["filename"])
            try:
                os.remove(local)
            except OSError:
                pass
        return moved
    # older ComfyUI builds without outputs in /history: read its output folder
    for i, f in enumerate(sorted(glob.glob(os.path.join(COMFYUI_OUTPUT, f"{prefix}_*.png")))):
        dst = os.path.join(out_dir, f"flf_{i:04d}.png")
        shutil.move(f, dst)
        moved.append(dst)
    return moved


def comfyui_up(timeout=6) -> bool:
    try:
        _get("/system_stats", timeout=timeout)
        return True
    except Exception:
        return False


def _build_flf_workflow(start_name: str, end_name: str, prompt: str, width: int, height: int,
                        num_frames: int, steps: int, cfg: float, seed: int, prefix: str):
    """WanFunInpaintToVideo conditioned on start+end frames; two-stage sampling:
    high-noise model for the first half of the steps (leftover noise passed on),
    low-noise model finishes — the canonical WAN 2.2 14B recipe."""
    mid = steps // 2
    return {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": FLF_UNET_HIGH, "weight_dtype": "fp8_e4m3fn"}},
        "1b": {"class_type": "UNETLoader",
               "inputs": {"unet_name": FLF_UNET_LOW, "weight_dtype": "fp8_e4m3fn"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP, "type": "wan"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "5": {"class_type": "LoadImage", "inputs": {"image": start_name}},
        "5b": {"class_type": "LoadImage", "inputs": {"image": end_name}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": NEGATIVE, "clip": ["2", 0]}},
        "9": {"class_type": "WanFunInpaintToVideo",
              "inputs": {"positive": ["6", 0], "negative": ["7", 0], "vae": ["4", 0],
                         "width": width, "height": height, "length": num_frames,
                         "batch_size": 1,
                         "start_image": ["5", 0], "end_image": ["5b", 0]}},
        "10": {"class_type": "KSamplerAdvanced",
               "inputs": {"model": ["1", 0], "add_noise": "enable", "noise_seed": seed,
                          "steps": steps, "cfg": cfg, "sampler_name": "euler",
                          "scheduler": "simple", "positive": ["9", 0], "negative": ["9", 1],
                          "latent_image": ["9", 2], "start_at_step": 0, "end_at_step": mid,
                          "return_with_leftover_noise": "enable"}},
        "10b": {"class_type": "KSamplerAdvanced",
                "inputs": {"model": ["1b", 0], "add_noise": "disable", "noise_seed": seed,
                           "steps": steps, "cfg": cfg, "sampler_name": "euler",
                           "scheduler": "simple", "positive": ["9", 0], "negative": ["9", 1],
                           "latent_image": ["10", 0], "start_at_step": mid, "end_at_step": steps,
                           "return_with_leftover_noise": "disable"}},
        "11": {"class_type": "VAEDecode", "inputs": {"samples": ["10b", 0], "vae": ["4", 0]}},
        "12": {"class_type": "SaveImage",
               "inputs": {"images": ["11", 0], "filename_prefix": prefix}},
    }


def generate_wan_flf_frames(start_image: str, end_image: str, out_dir: str,
                            prompt: str = FLF_PROMPT, width: int = 0, height: int = 0,
                            num_frames: int = 21, steps: int = 20, cfg: float = 5.0,
                            seed: int = 42, timeout_s: int = 900, log=print) -> list:
    """Generate the camera-orbit arc BETWEEN two photos of the same car (first/last-frame
    conditioned). Returns the list of extracted frame paths (moved into out_dir), or []
    on any failure — callers fall back to optical flow. num_frames must be 4k+1."""
    if not comfyui_up():
        log("[WAN-FLF] ComfyUI not reachable")
        return []
    # render size tunable per install: bigger = sharper tweens (less pulse vs the
    # native-res anchor photos) at ~linear GPU cost. 720x480 = speed default.
    width = width or int(os.environ.get("LOTVUE_FLF_W", "720"))
    height = height or int(os.environ.get("LOTVUE_FLF_H", "480"))
    try:
        prefix = f"lotvue_flf_{os.getpid()}_{int(time.time()*1000)}"
        up_start = _upload_image(start_image)
        up_end = _upload_image(end_image)
        wf = _build_flf_workflow(up_start, up_end, prompt, width, height,
                                 num_frames, steps, cfg, seed, prefix)
        result = _post("/prompt", {"prompt": wf})
        prompt_id = result.get("prompt_id")
        if not prompt_id:
            log(f"[WAN-FLF] no prompt_id: {str(result)[:300]}")
            return []

        def _in_queue():
            """Is our job still queued/running? (empty history + gone from queue =
            ComfyUI restarted and lost it — no point waiting out the timeout)"""
            try:
                q = _get("/queue")
                for sec in ("queue_running", "queue_pending"):
                    for item in q.get(sec, []):
                        if len(item) > 1 and item[1] == prompt_id:
                            return True
            except Exception:
                return True  # can't tell -> keep waiting
            return False

        def _cancel_and_clean():
            """Best-effort: pull the abandoned job off the queue (interrupt it if
            it is the one running) and remove any frames it already produced —
            otherwise a timed-out arc keeps burning the GPU and litters output/."""
            try:
                q = _get("/queue")
                running = [i[1] for i in q.get("queue_running", []) if len(i) > 1]
                if prompt_id in running:
                    _post("/interrupt", {})
                else:
                    _post("/queue", {"delete": [prompt_id]})
            except Exception:
                pass
            for f in glob.glob(os.path.join(COMFYUI_OUTPUT, f"{prefix}_*.png")):
                try:
                    os.remove(f)
                except OSError:
                    pass

        start_t = time.time()
        missing_polls = 0
        entry = {}
        while time.time() - start_t < timeout_s:
            time.sleep(POLL_SECONDS)
            try:
                hist = _get(f"/history/{prompt_id}")
                if prompt_id in hist:
                    missing_polls = 0
                    entry = hist[prompt_id]
                    st = entry.get("status", {})
                    if st.get("status_str") == "error":
                        msgs = [m for m in st.get("messages", []) if m[0] == "execution_error"]
                        log(f"[WAN-FLF] execution error: {str(msgs)[:400]}")
                        _cancel_and_clean()
                        return []
                    if st.get("completed"):
                        break
                else:
                    missing_polls += 1
                    if missing_polls >= 3 and not _in_queue():
                        log("[WAN-FLF] job vanished from queue+history (ComfyUI restarted?) — aborting early")
                        return []
            except Exception as e:
                log(f"[WAN-FLF] poll error: {e}")
        else:
            log(f"[WAN-FLF] timeout after {timeout_s}s")
            _cancel_and_clean()
            return []
        moved = _collect_output_frames(entry, prefix, out_dir)
        log(f"[WAN-FLF] {len(moved)} frames in {time.time()-start_t:.0f}s")
        return moved
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()[:400]
        except Exception:
            detail = str(e)
        log(f"[WAN-FLF] HTTP {e.code}: {detail}")
        return []
    except Exception as e:
        log(f"[WAN-FLF] failed: {e}")
        return []


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
    ap.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "lotvue_wan_test.mp4"))
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--frames", type=int, default=49)
    ap.add_argument("--steps", type=int, default=20)
    a = ap.parse_args()
    ok = generate_wan_video_via_api(a.image, a.out, a.prompt, num_frames=a.frames, steps=a.steps)
    print("=== WAN VIA COMFYUI API:", "PASS ===" if ok else "FAIL ===")
    sys.exit(0 if ok else 1)
