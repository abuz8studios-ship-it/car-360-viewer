"""
WAN 2.2 I2V via ComfyUI API - v3 with correct node connections
Fixes:
- CLIPVisionEncode needs 'crop' parameter
- KSampler gets latent from WanImageToVideo node 0 output (latent)
- WanImageToVideo outputs: [0]=latent, [1]=positive, [2]=negative
- Use VHSVideoCombine for output (more reliable than DecodeAndSaveVideo)
"""
import json, time, urllib.request, urllib.error, os, shutil

COMFYUI_URL = "http://127.0.0.1:8188"
INPUT_IMAGE = r".\360Car\app\storage\B4C0E2900191F04ED\spin\spin_0001.jpg"
OUTPUT_DIR = r"C:\Temp\lotvue_commercial_test"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── Step 1: Upload image ──────────────────────────────────────────────────────
print("Step 1: Upload input image to ComfyUI...")
with open(INPUT_IMAGE, "rb") as f:
    img_data = f.read()

boundary = "FormBoundaryLotVue360"
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

# ── Check actual CLIPVisionEncode params ──────────────────────────────────────
req = urllib.request.Request(f"{COMFYUI_URL}/object_info/CLIPVisionEncode")
resp = urllib.request.urlopen(req, timeout=10)
info = json.loads(resp.read())
node_info = info.get("CLIPVisionEncode", {})
inputs = node_info.get("input", {})
print(f"  CLIPVisionEncode required: {list(inputs.get('required', {}).keys())}")
print(f"  CLIPVisionEncode optional: {list(inputs.get('optional', {}).keys())}")

# Check crop options
crop_options = inputs.get("required", {}).get("crop", [None])[0]
print(f"  crop options: {crop_options}")

# Check WanImageToVideo outputs
req2 = urllib.request.Request(f"{COMFYUI_URL}/object_info/WanImageToVideo")
resp2 = urllib.request.urlopen(req2, timeout=10)
info2 = json.loads(resp2.read())
node2 = info2.get("WanImageToVideo", {})
print(f"  WanImageToVideo output_node: {node2.get('output_node')}")
print(f"  WanImageToVideo output: {node2.get('output')}")
print(f"  WanImageToVideo output_name: {node2.get('output_name')}")

# Check VHSVideoCombine
req3 = urllib.request.Request(f"{COMFYUI_URL}/object_info/VHSVideoCombine")
resp3 = urllib.request.urlopen(req3, timeout=10)
info3 = json.loads(resp3.read())
node3 = info3.get("VHSVideoCombine", {})
inputs3 = node3.get("input", {})
print(f"\n  VHSVideoCombine required: {list(inputs3.get('required', {}).keys())}")
print(f"  VHSVideoCombine optional: {list(inputs3.get('optional', {}).keys())[:10]}")

# ── Step 2: Build workflow ────────────────────────────────────────────────────
print("\nStep 2: Build corrected WAN 2.2 I2V workflow...")

workflow = {
    # Load UNET model
    "1": {
        "class_type": "UNETLoader",
        "inputs": {
            "unet_name": "wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors",
            "weight_dtype": "fp8_e4m3fn"
        }
    },
    # Load CLIP text encoder (UMT5 for WAN)
    "2": {
        "class_type": "CLIPLoader",
        "inputs": {
            "clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
            "type": "wan"
        }
    },
    # Load CLIP vision encoder
    "3": {
        "class_type": "CLIPVisionLoader",
        "inputs": {"clip_name": "clip_vision_h.safetensors"}
    },
    # Load VAE
    "4": {
        "class_type": "VAELoader",
        "inputs": {"vae_name": "wan2.2_vae.safetensors"}
    },
    # Load input image
    "5": {
        "class_type": "LoadImage",
        "inputs": {"image": uploaded_name}
    },
    # Positive text prompt
    "6": {
        "class_type": "CLIPTextEncode",
        "inputs": {
            "text": "A luxury car slowly rotating on a dark studio floor, cinematic lighting, professional automotive photography, smooth camera movement, high quality",
            "clip": ["2", 0]
        }
    },
    # Negative text prompt
    "7": {
        "class_type": "CLIPTextEncode",
        "inputs": {
            "text": "blurry, low quality, distorted, watermark, text, people",
            "clip": ["2", 0]
        }
    },
    # CLIP vision encode (with crop parameter)
    "8": {
        "class_type": "CLIPVisionEncode",
        "inputs": {
            "clip_vision": ["3", 0],
            "image": ["5", 0],
            "crop": "center"
        }
    },
    # WAN I2V - generates latent + conditionings
    "9": {
        "class_type": "WanImageToVideo",
        "inputs": {
            "positive": ["6", 0],
            "negative": ["7", 0],
            "vae": ["4", 0],
            "width": 720,
            "height": 480,
            "length": 49,
            "batch_size": 1,
            "clip_vision_output": ["8", 0],
            "start_image": ["5", 0]
        }
    },
    # KSampler - WanImageToVideo outputs: 0=latent, 1=positive_cond, 2=negative_cond
    "10": {
        "class_type": "KSampler",
        "inputs": {
            "model": ["1", 0],
            "seed": 42,
            "steps": 20,
            "cfg": 6.0,
            "sampler_name": "euler",
            "scheduler": "linear_quadratic",
            "positive": ["9", 1],
            "negative": ["9", 2],
            "latent_image": ["9", 0],
            "denoise": 1.0
        }
    },
    # VAE Decode
    "11": {
        "class_type": "VAEDecode",
        "inputs": {
            "samples": ["10", 0],
            "vae": ["4", 0]
        }
    },
    # Save video using VHS
    "12": {
        "class_type": "VHSVideoCombine",
        "inputs": {
            "images": ["11", 0],
            "frame_rate": 16,
            "loop_count": 0,
            "filename_prefix": "lotvue_commercial",
            "format": "video/h264-mp4",
            "save_output": True,
            "pingpong": False
        }
    }
}

print(f"  Workflow built with {len(workflow)} nodes")

# ── Step 3: Submit ────────────────────────────────────────────────────────────
print("Step 3: Submit to ComfyUI...")
payload = json.dumps({"prompt": workflow}).encode()
req = urllib.request.Request(
    f"{COMFYUI_URL}/prompt",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST"
)
try:
    resp = urllib.request.urlopen(req, timeout=30)
    result = json.loads(resp.read())
    prompt_id = result.get("prompt_id")
    print(f"  Prompt ID: {prompt_id}")
    if result.get("node_errors"):
        print(f"  Node errors: {json.dumps(result['node_errors'], indent=2)}")
except urllib.error.HTTPError as e:
    body = e.read().decode()
    print(f"  HTTP {e.code}: {body[:800]}")
    exit(1)

if not prompt_id:
    print(f"  ERROR: {result}")
    exit(1)

# ── Step 4: Poll ──────────────────────────────────────────────────────────────
print("Step 4: Polling (2-5 min on RTX 5090)...")
start = time.time()
output_video = None
output_subfolder = ""

while True:
    time.sleep(4)
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
                    for key in ["gifs", "videos"]:
                        if key in node_output:
                            for item in node_output[key]:
                                fn = item.get("filename", "")
                                if fn:
                                    output_video = fn
                                    output_subfolder = item.get("subfolder", "")
                                    print(f"  Output: {output_video}")
                                    break
                print(f"  Status: {status}")
                break

            req2 = urllib.request.Request(f"{COMFYUI_URL}/queue")
            resp2 = urllib.request.urlopen(req2, timeout=10)
            queue = json.loads(resp2.read())
            running = len(queue.get("queue_running", []))
            pending = len(queue.get("queue_pending", []))
            print(f"  [{elapsed:.0f}s] Running: {running}, Pending: {pending}")

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
        print(f"\n  Saved: {dst} ({size:,} bytes)")
        print("=== WAN 2.2 I2V: PASS ===")
    else:
        print(f"  File not found: {src}")
        print("  Check E:\\ABU\\ComfyUI\\output\\ for output files")
        print("=== WAN 2.2 I2V: CHECK OUTPUT DIR ===")
else:
    print("  No video output found in history")
    print("=== WAN 2.2 I2V: FAIL ===")
