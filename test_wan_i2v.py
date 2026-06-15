"""
Test WAN 2.2 I2V via ComfyUI API
Sends a vehicle photo and generates a 3-second video clip
Run with: .\ComfyUI\.venv\Scripts\python.exe test_wan_i2v.py
"""
import json, time, urllib.request, urllib.error, base64, os, shutil

COMFYUI_URL = "http://127.0.0.1:8188"
INPUT_IMAGE = r".\360Car\app\storage\B4C0E2900191F04ED\spin\spin_0001.jpg"
OUTPUT_DIR = r"C:\Temp\lotvue_commercial_test"
COMFYUI_INPUT_DIR = r".\ComfyUI\input"

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── Step 1: Upload image to ComfyUI ──────────────────────────────────────────
print("Step 1: Upload input image to ComfyUI...")
with open(INPUT_IMAGE, 'rb') as f:
    img_data = f.read()

import urllib.parse
boundary = "----FormBoundary7MA4YWxkTrZu0gW"
body = (
    f"--{boundary}\r\n"
    f'Content-Disposition: form-data; name="image"; filename="vehicle_input.jpg"\r\n'
    f"Content-Type: image/jpeg\r\n\r\n"
).encode() + img_data + f"\r\n--{boundary}--\r\n".encode()

req = urllib.request.Request(
    f"{COMFYUI_URL}/upload/image",
    data=body,
    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    method="POST"
)
resp = urllib.request.urlopen(req, timeout=30)
upload_result = json.loads(resp.read())
uploaded_name = upload_result.get("name", "vehicle_input.jpg")
print(f"  Uploaded as: {uploaded_name}")

# ── Step 2: Build WAN 2.2 I2V workflow ───────────────────────────────────────
print("Step 2: Build WAN 2.2 I2V workflow...")

workflow = {
    "1": {
        "class_type": "LoadImage",
        "inputs": {"image": uploaded_name}
    },
    "2": {
        "class_type": "CLIPVisionLoader",
        "inputs": {"clip_name": "clip_vision_h.safetensors"}
    },
    "3": {
        "class_type": "UMT5TextEncoderLoader",
        "inputs": {
            "model_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
        }
    },
    "4": {
        "class_type": "WanVideoVAELoader",
        "inputs": {"model_name": "wan2.2_vae.safetensors"}
    },
    "5": {
        "class_type": "UnetLoaderGGUF",
        "inputs": {
            "unet_name": "wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors"
        }
    },
    "6": {
        "class_type": "CLIPTextEncode",
        "inputs": {
            "text": "A luxury car slowly rotating on a dark studio floor, cinematic lighting, professional automotive photography, smooth camera movement",
            "clip": ["3", 0]
        }
    },
    "7": {
        "class_type": "CLIPTextEncode",
        "inputs": {
            "text": "blurry, low quality, distorted, watermark, text overlay",
            "clip": ["3", 0]
        }
    },
    "8": {
        "class_type": "CLIPVisionEncode",
        "inputs": {
            "clip_vision": ["2", 0],
            "image": ["1", 0]
        }
    },
    "9": {
        "class_type": "WanImageToVideo",
        "inputs": {
            "model": ["5", 0],
            "positive": ["6", 0],
            "negative": ["7", 0],
            "image_embeds": ["8", 0],
            "vae": ["4", 0],
            "width": 720,
            "height": 480,
            "num_frames": 49,
            "steps": 20,
            "cfg": 6.0,
            "seed": 42,
            "sampler_name": "euler",
            "scheduler": "linear_quadratic"
        }
    },
    "10": {
        "class_type": "VHSVideoCombine",
        "inputs": {
            "images": ["9", 0],
            "frame_rate": 16,
            "loop_count": 0,
            "filename_prefix": "lotvue_commercial",
            "format": "video/h264-mp4",
            "save_output": True
        }
    }
}

print("  Workflow built with 10 nodes")

# ── Step 3: Submit to ComfyUI ─────────────────────────────────────────────────
print("Step 3: Submit workflow to ComfyUI...")
payload = json.dumps({"prompt": workflow}).encode()
req = urllib.request.Request(
    f"{COMFYUI_URL}/prompt",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST"
)
resp = urllib.request.urlopen(req, timeout=30)
result = json.loads(resp.read())
prompt_id = result.get("prompt_id")
print(f"  Prompt ID: {prompt_id}")

if not prompt_id:
    print(f"  ERROR: {result}")
    exit(1)

# ── Step 4: Poll until complete ───────────────────────────────────────────────
print("Step 4: Polling for completion (this takes 2-5 minutes on RTX 5090)...")
start = time.time()
output_video = None

while True:
    time.sleep(3)
    elapsed = time.time() - start
    
    try:
        req = urllib.request.Request(f"{COMFYUI_URL}/history/{prompt_id}")
        resp = urllib.request.urlopen(req, timeout=10)
        history = json.loads(resp.read())
        
        if prompt_id in history:
            entry = history[prompt_id]
            status = entry.get("status", {})
            
            if status.get("completed"):
                outputs = entry.get("outputs", {})
                for node_id, node_output in outputs.items():
                    if "gifs" in node_output:
                        for gif in node_output["gifs"]:
                            output_video = gif.get("filename")
                            output_subfolder = gif.get("subfolder", "")
                            print(f"  Video generated: {output_video}")
                            break
                    if "videos" in node_output:
                        for vid in node_output["videos"]:
                            output_video = vid.get("filename")
                            output_subfolder = vid.get("subfolder", "")
                            print(f"  Video generated: {output_video}")
                            break
                break
            
            # Check queue position
            req2 = urllib.request.Request(f"{COMFYUI_URL}/queue")
            resp2 = urllib.request.urlopen(req2, timeout=10)
            queue = json.loads(resp2.read())
            running = queue.get("queue_running", [])
            pending = queue.get("queue_pending", [])
            print(f"  [{elapsed:.0f}s] Running: {len(running)}, Pending: {len(pending)}")
    except Exception as e:
        print(f"  [{elapsed:.0f}s] Poll error: {e}")
    
    if elapsed > 600:
        print("  TIMEOUT after 10 minutes")
        break

# ── Step 5: Copy output ───────────────────────────────────────────────────────
if output_video:
    src = os.path.join(r".\ComfyUI\output", output_subfolder, output_video)
    dst = os.path.join(OUTPUT_DIR, "test_commercial.mp4")
    if os.path.exists(src):
        shutil.copy2(src, dst)
        size = os.path.getsize(dst)
        print(f"  Copied to: {dst} ({size:,} bytes)")
        print()
        print("=== WAN 2.2 I2V: PASS ===")
    else:
        print(f"  File not found at: {src}")
        print("  Check E:\\ABU\\ComfyUI\\output\\ manually")
else:
    print("  No output video found in history")
    print("=== WAN 2.2 I2V: CHECK COMFYUI LOGS ===")
