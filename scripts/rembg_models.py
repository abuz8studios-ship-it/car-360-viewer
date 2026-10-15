"""Background-removal checkpoints bundled in desktop releases.

Owner-approved 2026-10-15: rembg `isnet-general-use` as the primary model with
`u2net` as the fallback, shipped inside the installer so a fresh install never
falls back to "background kept". The build downloads each file from rembg's own
GitHub release and refuses it unless the SHA-256 matches; the artifact verifier
accepts these two files (and no other model weights) at MODEL_DIR.

rembg 2.0.85 pins the same files by MD5 (isnet fc16ebd8…, u2net 60024c5c…);
both downloads matched those values when these SHA-256 pins were recorded.
"""
from __future__ import annotations

MODEL_DIR = "models/rembg"  # relative to the packaged app/ directory

BUNDLED_REMBG_MODELS = {
    "isnet-general-use.onnx": {
        "url": "https://github.com/danielgatis/rembg/releases/download/v0.0.0/isnet-general-use.onnx",
        "sha256": "60920e99c45464f2ba57bee2ad08c919a52bbf852739e96947fbb4358c0d964a",
        "bytes": 178_648_008,
        "role": "primary",
        "license": "Apache-2.0 (github.com/xuebinqin/DIS); trained on DIS5K, whose dataset "
                   "terms restrict commercial use without the authors' permission",
    },
    "u2net.onnx": {
        "url": "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx",
        "sha256": "8d10d2f3bb75ae3b6d527c77944fc5e7dcd94b29809d47a739a7a728a912b491",
        "bytes": 175_997_641,
        "role": "fallback",
        "license": "Apache-2.0 (github.com/xuebinqin/U-2-Net)",
    },
}
