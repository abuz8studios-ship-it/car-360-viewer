"""
WAN 2.2 I2V via ComfyUI API - v4 FINAL
Uses ADE_AnimateDiffCombine for video output (confirmed available)
WanImageToVideo outputs: [0]=latent(LATENT), [1]=positive(CONDITIONING), [2]=negative(CONDITIONING)
"""
import json, time, urllib.request, urllib.error, os, shutil, glob

COMFYUI_URL = "http://127.0.0.1:8188"
INPUT_IMAGE = r".\360Car\app\storage\B4C0E2900191F04ED\spin\spin_0001.jpg"
OUTPUT_DIR = r"C:\Temp\lotvue_commercial_test"
COMFYUI_OUTPUT = r".\ComfyUI\output"
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

# ── Step 2: Build workflow ────────────────────────────────────────────────────
print("Step 2: Build WAN 2.2 I2V workflow (v4)...")

workflow = {
    # Load UNET
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
    # Load CLIP vision
    "3": {
        "class_type": "CLIPVisionLoader",
        "inputs": {"clip_name": "clip_vision_h.safetensors"}
    },
    # Load VAE
    "4": {
        "class_type": "VAELoader",
        "inputs": {"vae_name": "wan2.2_vae.safetensors"}
    },
    # Load image
    "5": {
        "class_type": "LoadImage",
        "inputs": {"image": uploaded_name}
    },
    # Positive prompt
    "6": {
        "class_type": "CLIPTextEncode",
        "inputs": {
            "text": "A luxury car slowly rotating on a dark studio floor, cinematic lighting, professional automotive photography, smooth camera movement, high quality 4K",
            "clip": ["2", 0]
        }
    },
    # Negative prompt
    "7": {
        "class_type": "CLIPTextEncode",
        "inputs": {
            "text": "blurry, low quality, distorted, watermark, text, people",
            "clip": ["2", 0]
        }
    },
    # CLIP vision encode
    "8": {
        "class_type": "CLIPVisionEncode",
        "inputs": {
            "clip_vision": ["3", 0],
            "image": ["5", 0],
            "crop": "center"
        }
    },
    # WAN I2V: outputs [0]=LATENT, [1]=CONDITIONING(pos), [2]=CONDITIONING(neg)
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
    # KSampler
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
    # Save as animated video using ADE_AnimateDiffCombine
    "12": {
        "class_type": "ADE_AnimateDiffCombine",
        "inputs": {
            "images": ["11", 0],
            "frame_rate": 16,
            "loop_count": 0,
            "filename_prefix": "lotvue_commercial",
            "format": "video/h264-mp4",
            "pingpong": False,
            "save_image": True
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
    if result.get("node_errors"):
        print(f"  Node errors: {json.dumps(result['node_errors'], indent=2)[:500]}")
    print(f"  Prompt ID: {prompt_id}")
except urllib.error.HTTPError as e:
    body = e.read().decode()
    print(f"  HTTP {e.code}: {body[:800]}")
    exit(1)

if not prompt_id:
    print(f"  ERROR: {result}")
    exit(1)

# ── Step 4: Poll ──────────────────────────────────────────────────────────────
print("Step 4: Polling for completion (2-5 min)...")
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
                print(f"  Completed! Output keys: {list(outputs.keys())}")
                for node_id, node_output in outputs.items():
                    print(f"  Node {node_id} output keys: {list(node_output.keys())}")
                    for key in ["gifs", "videos", "images"]:
                        if key in node_output:
                            for item in node_output[key]:
                                fn = item.get("filename", "")
                                if fn:
                                    output_video = fn
                                    output_subfolder = item.get("subfolder", "")
                                    print(f"  Found output: {fn}")
                                    break
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
    src = os.path.join(COMFYUI_OUTPUT, output_subfolder, output_video)
    dst = os.path.join(OUTPUT_DIR, "test_commercial.mp4")
    if os.path.exists(src):
        shutil.copy2(src, dst)
        size = os.path.getsize(dst)
        print(f"\n  Saved: {dst} ({size:,} bytes)")
        print("=== WAN 2.2 I2V: PASS ===")
    else:
        # Try to find any new mp4 in output dir
        mp4s = sorted(glob.glob(os.path.join(COMFYUI_OUTPUT, "lotvue_commercial*.mp4")))
        if mp4s:
            shutil.copy2(mp4s[-1], dst)
            print(f"\n  Found via glob: {mp4s[-1]}")
            print("=== WAN 2.2 I2V: PASS ===")
        else:
            print(f"  File not found: {src}")
            print("=== WAN 2.2 I2V: CHECK COMFYUI OUTPUT DIR ===")
else:
    print("  No video output found")
    print("=== WAN 2.2 I2V: FAIL ===")
