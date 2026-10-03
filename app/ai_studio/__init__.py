"""Photo-processing components shared by Dealer Studio workflows.

The pipeline combines local image operations with optional operator-configured
rembg and ComfyUI services. Model rights, hardware, credentials, and visual
quality are installation-specific and are not certified by this package.
"""
from .photo_processor import PhotoProcessor
