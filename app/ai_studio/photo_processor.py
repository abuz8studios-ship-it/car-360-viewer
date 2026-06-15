"""
LotVue Pro — AI Photo Processor (Orchestrator)
Takes a raw photo from a dealer's lot, runs the full AI pipeline:
  1. Background removal (rembg)
  2. Background replacement (showroom composite)
  3. Enhancement (color correction, white balance, shadow)
  4. License plate blur
  5. Upscale 4x (RealESRGAN via ComfyUI)

All processing uses local GPU. Zero cloud cost.
"""
import os
import io
import shutil
import traceback
from pathlib import Path
from PIL import Image
from typing import Optional

from .background_swap import remove_background, composite_on_showroom
from .enhance import auto_enhance, add_floor_shadow
from .plate_blur import blur_license_plate

STORAGE_DIR = Path(r".\360Car\app\storage")
BACKGROUNDS_DIR = Path(r".\360Car\app\ai_studio\backgrounds")


class PhotoProcessor:
    """Orchestrates the full AI photo pipeline for a single vehicle photo."""

    def __init__(self, background_name: str = "studio_white", upscale: bool = False):
        self.background_name = background_name
        self.upscale = upscale

    def process(self, input_path: str, output_path: str) -> dict:
        """
        Run the full pipeline on a single photo.
        Returns dict with status and metadata.
        """
        result = {
            "input": input_path,
            "output": output_path,
            "steps": [],
            "status": "ok",
        }

        try:
            img = Image.open(input_path).convert("RGBA")
            original_size = img.size
            result["original_size"] = original_size

            # Step 1: Background removal
            try:
                car_isolated, mask = remove_background(img)
                result["steps"].append("bg_removal:ok")
            except Exception as e:
                print(f"[AI Studio] BG removal failed: {e}")
                car_isolated = img
                mask = None
                result["steps"].append(f"bg_removal:fallback ({e})")

            # Step 2: Background replacement
            try:
                bg_path = BACKGROUNDS_DIR / f"{self.background_name}.jpg"
                if not bg_path.exists():
                    bg_path = BACKGROUNDS_DIR / "studio_white.jpg"
                if bg_path.exists():
                    composited = composite_on_showroom(car_isolated, str(bg_path))
                    result["steps"].append("bg_swap:ok")
                else:
                    # Fallback: white background
                    bg = Image.new("RGBA", car_isolated.size, (248, 250, 252, 255))
                    bg.paste(car_isolated, (0, 0), car_isolated if car_isolated.mode == "RGBA" else None)
                    composited = bg.convert("RGB")
                    result["steps"].append("bg_swap:white_fallback")
            except Exception as e:
                print(f"[AI Studio] BG swap failed: {e}")
                composited = car_isolated.convert("RGB")
                result["steps"].append(f"bg_swap:error ({e})")

            # Step 3: Enhancement
            try:
                enhanced = auto_enhance(composited)
                enhanced = add_floor_shadow(enhanced)
                result["steps"].append("enhance:ok")
            except Exception as e:
                print(f"[AI Studio] Enhancement failed: {e}")
                enhanced = composited
                result["steps"].append(f"enhance:fallback ({e})")

            # Step 4: License plate blur
            try:
                blurred = blur_license_plate(enhanced)
                result["steps"].append("plate_blur:ok")
            except Exception as e:
                print(f"[AI Studio] Plate blur failed: {e}")
                blurred = enhanced
                result["steps"].append(f"plate_blur:skip ({e})")

            # Step 5: Upscale (optional — uses ComfyUI RealESRGAN)
            if self.upscale:
                try:
                    from .upscale import upscale_image
                    final = upscale_image(blurred)
                    result["steps"].append("upscale:ok")
                except Exception as e:
                    print(f"[AI Studio] Upscale failed: {e}")
                    final = blurred
                    result["steps"].append(f"upscale:skip ({e})")
            else:
                final = blurred
                result["steps"].append("upscale:skipped")

            # Save output
            final_rgb = final.convert("RGB") if final.mode != "RGB" else final
            final_rgb.save(output_path, "JPEG", quality=95)
            result["output_size"] = final_rgb.size
            result["status"] = "ok"

        except Exception as e:
            result["status"] = "error"
            result["error"] = str(e)
            traceback.print_exc()
            # Save original as fallback
            try:
                Image.open(input_path).convert("RGB").save(output_path, "JPEG", quality=90)
            except:
                pass

        return result

    def process_batch(self, input_dir: str, output_dir: str) -> list:
        """Process all photos in a directory."""
        results = []
        input_path = Path(input_dir)
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        for img_file in sorted(input_path.glob("*.jpg")):
            out_file = output_path / f"studio_{img_file.name}"
            r = self.process(str(img_file), str(out_file))
            results.append(r)

        return results
