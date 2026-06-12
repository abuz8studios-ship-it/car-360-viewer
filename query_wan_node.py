import json, urllib.request

COMFYUI_URL = "http://127.0.0.1:8188"

for node_name in ["WanImageToVideo", "Wan22ImageToVideoLatent", "WanCameraImageToVideo"]:
    try:
        req = urllib.request.Request(f"{COMFYUI_URL}/object_info/{node_name}")
        resp = urllib.request.urlopen(req, timeout=10)
        info = json.loads(resp.read())
        node = info.get(node_name, {})
        inputs = node.get("input", {})
        print(f"\n=== {node_name} ===")
        print(f"  REQUIRED: {list(inputs.get('required', {}).keys())}")
        print(f"  OPTIONAL: {list(inputs.get('optional', {}).keys())[:15]}")
    except Exception as e:
        print(f"\n=== {node_name} === ERROR: {e}")

# Also check video saver nodes
for node_name in ["VHSVideoCombine", "SaveAnimatedWEBP", "DecodeAndSaveVideo"]:
    try:
        req = urllib.request.Request(f"{COMFYUI_URL}/object_info/{node_name}")
        resp = urllib.request.urlopen(req, timeout=10)
        info = json.loads(resp.read())
        node = info.get(node_name, {})
        inputs = node.get("input", {})
        print(f"\n=== {node_name} ===")
        print(f"  REQUIRED: {list(inputs.get('required', {}).keys())}")
    except Exception as e:
        print(f"\n=== {node_name} === ERROR: {e}")

# Check text encoder loader
for node_name in ["CLIPLoader", "UnetLoaderGGUF", "UNETLoader", "WanVideoModelLoader"]:
    try:
        req = urllib.request.Request(f"{COMFYUI_URL}/object_info/{node_name}")
        resp = urllib.request.urlopen(req, timeout=10)
        info = json.loads(resp.read())
        node = info.get(node_name, {})
        inputs = node.get("input", {})
        print(f"\n=== {node_name} ===")
        print(f"  REQUIRED: {list(inputs.get('required', {}).keys())}")
    except Exception as e:
        print(f"\n=== {node_name} === ERROR: {e}")
