import json, urllib.request

COMFYUI_URL = "http://127.0.0.1:8188"

# Get all nodes
req = urllib.request.Request(f"{COMFYUI_URL}/object_info")
resp = urllib.request.urlopen(req, timeout=15)
all_nodes = json.loads(resp.read())

# Find text encode nodes
text_nodes = sorted([k for k in all_nodes.keys() if "text" in k.lower() and "encode" in k.lower()])
print("=== TEXT ENCODE NODES ===")
for n in text_nodes[:15]:
    print(f"  {n}")

# Find clip loader nodes
clip_nodes = sorted([k for k in all_nodes.keys() if "clip" in k.lower() and "load" in k.lower()])
print("\n=== CLIP LOADER NODES ===")
for n in clip_nodes[:10]:
    print(f"  {n}")

# Find unet/model loader nodes
model_nodes = sorted([k for k in all_nodes.keys() if ("unet" in k.lower() or "model" in k.lower()) and "load" in k.lower()])
print("\n=== MODEL LOADER NODES ===")
for n in model_nodes[:15]:
    print(f"  {n}")

# Get WanImageToVideo full schema
node = all_nodes.get("WanImageToVideo", {})
inputs = node.get("input", {})
print("\n=== WanImageToVideo FULL SCHEMA ===")
print("REQUIRED:", json.dumps(list(inputs.get("required", {}).keys()), indent=2))
print("OPTIONAL:", json.dumps(list(inputs.get("optional", {}).keys()), indent=2))

# Get DecodeAndSaveVideo full schema
node2 = all_nodes.get("DecodeAndSaveVideo", {})
inputs2 = node2.get("input", {})
print("\n=== DecodeAndSaveVideo FULL SCHEMA ===")
print("REQUIRED:", json.dumps(list(inputs2.get("required", {}).keys()), indent=2))

# Check KSampler
node3 = all_nodes.get("KSampler", {})
inputs3 = node3.get("input", {})
print("\n=== KSampler REQUIRED ===")
print(list(inputs3.get("required", {}).keys()))
