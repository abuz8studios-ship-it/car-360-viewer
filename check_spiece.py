"""Check if spiece_model is embedded in the umt5 safetensors file"""
import sys
sys.path.insert(0, r".\ComfyUI")
import safetensors.torch
import os

model_path = r".\ComfyUI\models\text_encoders\umt5_xxl_fp8_e4m3fn_scaled.safetensors"
print(f"Loading keys from: {model_path}")

# Just read metadata without loading tensors
from safetensors import safe_open
with safe_open(model_path, framework="pt", device="cpu") as f:
    keys = list(f.keys())
    metadata = f.metadata()

spiece_keys = [k for k in keys if "spiece" in k.lower() or "tokenizer" in k.lower()]
print(f"Total keys: {len(keys)}")
print(f"Tokenizer keys: {spiece_keys}")
print(f"Metadata: {metadata}")
print(f"First 5 keys: {keys[:5]}")

# Check if spiece.model file exists in the right place
# ComfyUI sd1_clip.py line 490: tokenizer_class.from_pretrained(tokenizer_path)
# tokenizer_path comes from tokenizer_data["spiece_model"] in wan.py
# If not in tokenizer_data, it will be None -> invalid tokenizer
# Solution: The spiece.model needs to be EMBEDDED in the safetensors OR
# we need to use wan_t5_umt5_xxl_enc_bf16.pth which has it embedded

# Check the .pth file
pth_path = r".\ComfyUI\models\text_encoders\wan_t5_umt5_xxl_enc_bf16.pth"
if os.path.exists(pth_path):
    import torch
    print(f"\nLoading .pth file: {pth_path}")
    data = torch.load(pth_path, map_location="cpu", weights_only=False)
    if isinstance(data, dict):
        pth_keys = list(data.keys())
        spiece_pth = [k for k in pth_keys if "spiece" in k.lower() or "tokenizer" in k.lower()]
        print(f"PTH keys: {pth_keys[:10]}")
        print(f"PTH tokenizer keys: {spiece_pth}")
    else:
        print(f"PTH type: {type(data)}")
