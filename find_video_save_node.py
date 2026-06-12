import json, urllib.request

COMFYUI_URL = "http://127.0.0.1:8188"

req = urllib.request.Request(f"{COMFYUI_URL}/object_info")
resp = urllib.request.urlopen(req, timeout=15)
nodes = json.loads(resp.read())

# Find video save nodes
save_nodes = sorted([k for k in nodes.keys() if any(x in k.lower() for x in ["save", "combine", "export"]) and any(x in k.lower() for x in ["video", "anim", "gif", "mp4"])])
print("VIDEO SAVE NODES:")
for n in save_nodes:
    inp = nodes[n].get("input", {})
    req_keys = list(inp.get("required", {}).keys())
    print(f"  {n}: required={req_keys}")

# Also check image-based savers that could work for video frames
print("\nIMAGE SAVE NODES:")
img_save = sorted([k for k in nodes.keys() if "save" in k.lower() and "image" in k.lower()])
for n in img_save[:10]:
    inp = nodes[n].get("input", {})
    req_keys = list(inp.get("required", {}).keys())
    print(f"  {n}: required={req_keys}")

# Check if PreviewAnimation exists
for n in ["PreviewAnimation", "SaveAnimatedWEBP", "GifSave", "CreateVideo", "EncodeVideoComponents"]:
    if n in nodes:
        inp = nodes[n].get("input", {})
        req_keys = list(inp.get("required", {}).keys())
        opt_keys = list(inp.get("optional", {}).keys())[:5]
        print(f"\n{n}: required={req_keys}, optional={opt_keys}")
