import json, urllib.request

COMFYUI_URL = "http://127.0.0.1:8188"

req = urllib.request.Request(f"{COMFYUI_URL}/object_info")
resp = urllib.request.urlopen(req, timeout=15)
nodes = json.loads(resp.read())

wan_nodes = sorted([k for k in nodes.keys() if "wan" in k.lower()])
video_nodes = sorted([k for k in nodes.keys() if "video" in k.lower()])
umt5_nodes = sorted([k for k in nodes.keys() if "umt5" in k.lower() or "t5" in k.lower()])
vae_nodes = sorted([k for k in nodes.keys() if "vae" in k.lower()])
sampler_nodes = sorted([k for k in nodes.keys() if "sampler" in k.lower() or "ksampler" in k.lower()])

print("=== WAN NODES ===")
for n in wan_nodes:
    print(f"  {n}")

print("\n=== VIDEO NODES ===")
for n in video_nodes[:20]:
    print(f"  {n}")

print("\n=== T5/UMT5 NODES ===")
for n in umt5_nodes[:10]:
    print(f"  {n}")

print("\n=== VAE NODES ===")
for n in vae_nodes[:10]:
    print(f"  {n}")

print("\n=== SAMPLER NODES ===")
for n in sampler_nodes[:10]:
    print(f"  {n}")

# Save full node list
with open(r"C:\Temp\comfyui_nodes.json", "w") as f:
    json.dump(list(nodes.keys()), f, indent=2)
print(f"\nFull node list saved to C:\\Temp\\comfyui_nodes.json ({len(nodes)} nodes)")
