"""Check the latest ComfyUI prompt history for actual execution errors"""
import json, urllib.request

COMFYUI_URL = "http://127.0.0.1:8188"
PROMPT_ID = "29eb307a-7292-4399-ae99-1b4f7e697c2d"

# Check this specific prompt
req = urllib.request.Request(f"{COMFYUI_URL}/history/{PROMPT_ID}")
resp = urllib.request.urlopen(req, timeout=10)
history = json.loads(resp.read())

if PROMPT_ID in history:
    entry = history[PROMPT_ID]
    status = entry.get("status", {})
    print("=== STATUS ===")
    print(json.dumps(status, indent=2))
    
    # Check for messages/errors in status
    messages = status.get("messages", [])
    if messages:
        print("\n=== MESSAGES ===")
        for msg in messages:
            print(json.dumps(msg, indent=2)[:500])
    
    outputs = entry.get("outputs", {})
    print(f"\n=== OUTPUTS (keys): {list(outputs.keys())} ===")
    
    meta = entry.get("meta", {})
    print(f"\n=== META ===")
    print(json.dumps(meta, indent=2)[:1000])
else:
    print(f"Prompt {PROMPT_ID} not in history yet")
    
# Also check ComfyUI logs via system_stats
req2 = urllib.request.Request(f"{COMFYUI_URL}/system_stats")
resp2 = urllib.request.urlopen(req2, timeout=10)
stats = json.loads(resp2.read())
devices = stats.get("devices", [])
for d in devices:
    print(f"\nGPU: {d['name']}")
    print(f"  VRAM Total: {d['vram_total']/1e9:.1f} GB")
    print(f"  VRAM Free: {d['vram_free']/1e9:.1f} GB")
    print(f"  Torch VRAM Total: {d['torch_vram_total']/1e9:.1f} GB")
    print(f"  Torch VRAM Free: {d['torch_vram_free']/1e9:.1f} GB")
