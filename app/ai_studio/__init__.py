"""
LotVue Pro AI Photo Studio
Transforms raw dealer lot photos into studio-quality automotive imagery.
Pipeline: Background Removal → Background Swap → Enhancement → Plate Blur → Upscale
Uses local GPU (RTX 5090) via rembg + ComfyUI. Zero cloud cost.
"""
from .photo_processor import PhotoProcessor
