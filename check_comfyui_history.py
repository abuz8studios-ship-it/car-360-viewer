"""Check ComfyUI history for the last submitted prompt and get full status/errors"""
import json, urllib.request

COMFYUI_URL = "http://127.0.0.1:8188"
PROMPT_ID = "4fb2153b-c63c-4c58-b93a-a5f03be4de52"

# Check history for this prompt
req = urllib.request.Request(f"{COMFYUI_URL}/history/{PROMPT_ID}")
resp = urllib.request.urlopen(req, timeout=10)
history = json.loads(resp.read())

if PROMPT_ID in history:
    entry = history[PROMPT_ID]
    status = entry.get("status", {})
    print(f"Status: {json.dumps(status, indent=2)}")
    
    outputs = entry.get("outputs", {})
    print(f"\nOutputs: {json.dumps(outputs, indent=2)[:1000]}")
    
    # Check for execution errors
    exec_info = entry.get("execution_cached", {})
    print(f"\nExecution cached: {exec_info}")
    
    # Full entry keys
    print(f"\nEntry keys: {list(entry.keys())}")
else:
    print(f"Prompt {PROMPT_ID} not in history")
    print("Checking all history entries...")
    req2 = urllib.request.Request(f"{COMFYUI_URL}/history")
    resp2 = urllib.request.urlopen(req2, timeout=10)
    all_history = json.loads(resp2.read())
    print(f"Total history entries: {len(all_history)}")
    for pid, entry in list(all_history.items())[-3:]:
        status = entry.get("status", {})
        print(f"\nPrompt {pid}: status={status}")

# Also check system stats
req3 = urllib.request.Request(f"{COMFYUI_URL}/system_stats")
resp3 = urllib.request.urlopen(req3, timeout=10)
stats = json.loads(resp3.read())
print(f"\nSystem stats: {json.dumps(stats, indent=2)}")
