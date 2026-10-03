"""Bounded photo-processing pipeline shared by Dealer Studio image workflows.

The pipeline applies a background cut-out when rembg is installed, then a
packaged-scene composite, enhancement, a heuristic plate-blur attempt, and an
optional ComfyUI upscale. Missing optional engines produce explicit degraded
results; an unsegmented source is never reported as a successful background
replacement. Strict background jobs also refuse to publish a source photo when
cut-out or scene compositing fails.
"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from PIL import Image, ImageOps

from app_paths import BASE_DIR, configured_path
from .background_swap import composite_on_showroom, remove_background
from .enhance import add_floor_shadow, auto_enhance
from .plate_blur import blur_license_plate_with_status


BACKGROUNDS_DIR = configured_path(
    "LOTVUE_BACKGROUNDS_DIR",
    configured_path("LOTVUE_SCENES_DIR", BASE_DIR / "static" / "backgrounds"),
)


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


MAX_IMAGE_PIXELS = _bounded_env_int("LOTVUE_MAX_IMAGE_PIXELS", 40_000_000, 1_000_000, 100_000_000)
MAX_PROCESSING_EDGE = _bounded_env_int("LOTVUE_PHOTO_MAX_EDGE", 1920, 640, 4096)
MAX_PHOTOS_PER_JOB = _bounded_env_int("LOTVUE_STUDIO_MAX_PHOTOS", 72, 1, 72)
_BACKGROUND_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class PhotoProcessor:
    """Process one photo or a bounded directory batch using one image at a time."""

    def __init__(
        self,
        background_name: str = "studio_white",
        upscale: bool = False,
        require_background_removal: bool = False,
    ):
        if not isinstance(background_name, str) or not _BACKGROUND_RE.fullmatch(background_name):
            raise ValueError("Invalid background name")
        self.background_name = background_name
        self.upscale = bool(upscale)
        self.require_background_removal = bool(require_background_removal)
        if self.require_background_removal and background_name == "none":
            raise ValueError("Required background replacement cannot keep the source background")

    def process(self, input_path: str, output_path: str) -> dict:
        result = {
            "input": Path(input_path).name,
            "output": Path(output_path).name,
            "steps": [],
            "warnings": [],
            "status": "error",
        }
        images: list[Image.Image] = []
        temporary_path: Path | None = None
        degraded = False

        def keep(image: Image.Image | None) -> Image.Image | None:
            if image is not None and all(id(image) != id(known) for known in images):
                images.append(image)
            return image

        try:
            with Image.open(input_path) as source:
                width, height = source.size
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    raise ValueError("Image exceeds the configured processing pixel limit")
                original_size = (width, height)
                # Let JPEG decoders reduce at decode time, then orient/convert the
                # bounded working image instead of retaining a full 40 MP copy.
                source.draft("RGB", (MAX_PROCESSING_EDGE, MAX_PROCESSING_EDGE))
                source.thumbnail(
                    (MAX_PROCESSING_EDGE, MAX_PROCESSING_EDGE), Image.Resampling.LANCZOS
                )
                oriented = ImageOps.exif_transpose(source)
                try:
                    img = keep(oriented.convert("RGB"))
                finally:
                    if oriented is not source:
                        oriented.close()
            assert img is not None
            result["original_size"] = original_size
            result["processing_size"] = img.size

            car_isolated = None
            mask = None
            cutout_ready = False
            if self.background_name == "none":
                result["steps"].append("bg_removal:skipped_keep_background")
                result["steps"].append("bg_swap:skipped_keep_background")
            else:
                try:
                    car_isolated, mask = remove_background(img)
                    keep(car_isolated)
                    keep(mask)
                    cutout_ready = mask is not None
                    if cutout_ready:
                        result["steps"].append("bg_removal:ok")
                    else:
                        degraded = True
                        result["steps"].append("bg_removal:unavailable_or_no_alpha")
                        result["warnings"].append(
                            "Background removal was unavailable; the photographed background was retained."
                        )
                except Exception as exc:
                    degraded = True
                    result["steps"].append(f"bg_removal:error:{type(exc).__name__}")
                    result["warnings"].append(
                        "Background removal failed; the photographed background was retained."
                    )
                    car_isolated = None
                    mask = None

            if self.require_background_removal and not cutout_ready:
                result["error_type"] = "BackgroundRemovalRequiredError"
                result["steps"].append("bg_swap:blocked_without_cutout")
                result["warnings"].append(
                    "Required background removal did not complete; no replacement output was written."
                )
                return result

            composited = None
            if cutout_ready and car_isolated is not None:
                bg_path = BACKGROUNDS_DIR / f"{self.background_name}.jpg"
                if not bg_path.is_file():
                    fallback_path = BACKGROUNDS_DIR / "studio_white.jpg"
                    if not self.require_background_removal and fallback_path.is_file():
                        bg_path = fallback_path
                        degraded = True
                        result["warnings"].append(
                            "The requested backdrop was unavailable; Studio White was used instead."
                        )
                    else:
                        bg_path = None
                if bg_path is not None:
                    try:
                        composited = keep(composite_on_showroom(car_isolated, str(bg_path)))
                        result["steps"].append("bg_swap:ok")
                    except Exception as exc:
                        degraded = True
                        result["steps"].append(f"bg_swap:error:{type(exc).__name__}")
                        result["warnings"].append(
                            "The cut-out was not composited; the source photo was retained."
                        )
                else:
                    degraded = True
                    result["steps"].append("bg_swap:unavailable")
                    result["warnings"].append("No packaged backdrop was available.")
            elif self.background_name != "none":
                result["steps"].append("bg_swap:skipped_without_cutout")

            if self.require_background_removal and composited is None:
                result["error_type"] = "BackgroundReplacementRequiredError"
                result["steps"].append("bg_swap:blocked_without_replacement")
                result["warnings"].append(
                    "Required background replacement did not complete; no output was written."
                )
                return result

            if composited is None:
                composited = keep(img.copy())

            try:
                enhanced = keep(auto_enhance(composited))
                if cutout_ready:
                    shadowed = keep(add_floor_shadow(enhanced))
                    if shadowed is not enhanced:
                        enhanced = shadowed
                result["steps"].append("enhance:ok")
            except Exception as exc:
                enhanced = composited
                degraded = True
                result["steps"].append(f"enhance:error:{type(exc).__name__}")
                result["warnings"].append("Automatic image enhancement did not complete.")

            try:
                blurred, blur_info = blur_license_plate_with_status(enhanced)
                blurred = keep(blurred)
                result["plate_blur"] = blur_info
                if blur_info["status"] in {"unavailable", "error"}:
                    degraded = True
                    result["warnings"].append(
                        "Plate blur did not complete; review this image manually."
                    )
                elif blur_info["status"] == "no_candidate_detected":
                    result["warnings"].append(
                        "The heuristic found no plate-like region; manual privacy review is still required."
                    )
                result["steps"].append(f"plate_blur:{blur_info['status']}")
            except Exception as exc:
                blurred = enhanced
                degraded = True
                result["plate_blur"] = {"status": "error", "regions_blurred": 0}
                result["steps"].append(f"plate_blur:error:{type(exc).__name__}")
                result["warnings"].append("Plate blur did not complete; review this image manually.")

            final = blurred
            if self.upscale:
                try:
                    from .upscale import upscale_image_with_status
                    final, upscale_status = upscale_image_with_status(blurred)
                    keep(final)
                    result["upscale"] = upscale_status
                    result["steps"].append(f"upscale:{upscale_status['status']}")
                    if upscale_status["status"] != "comfyui_4x":
                        degraded = True
                        result["warnings"].append(
                            "ComfyUI 4x upscale was unavailable; the reported fallback was used."
                        )
                except Exception as exc:
                    final = blurred
                    degraded = True
                    result["steps"].append(f"upscale:error:{type(exc).__name__}")
                    result["warnings"].append("The requested upscale did not complete.")
            else:
                result["steps"].append("upscale:skipped")

            output = Path(output_path)
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = output.with_name(f".{output.stem}.{uuid.uuid4().hex}.tmp")
            final_rgb = final.convert("RGB")
            keep(final_rgb)
            final_rgb.save(temporary_path, "JPEG", quality=93, optimize=True, progressive=True)
            os.replace(temporary_path, output)
            result["output_size"] = final_rgb.size
            result["status"] = "degraded" if degraded else "ok"
        except Exception as exc:
            result["status"] = "error"
            result["error_type"] = type(exc).__name__
            result["warnings"].append("This photo could not be processed.")
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            for image in reversed(images):
                try:
                    image.close()
                except Exception:
                    pass

        return result

    def process_batch(
        self,
        input_dir: str,
        output_dir: str,
        max_photos: int | None = None,
    ) -> list[dict]:
        """Process up to the configured photo cap without retaining batch pixels."""
        input_path = Path(input_dir)
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        try:
            limit = MAX_PHOTOS_PER_JOB if max_photos is None else max(
                1, min(int(max_photos), MAX_PHOTOS_PER_JOB)
            )
        except (TypeError, ValueError):
            limit = MAX_PHOTOS_PER_JOB
        supported = {".jpg", ".jpeg", ".png", ".webp"}
        photos = sorted(
            (path for path in input_path.iterdir()
             if path.is_file() and not path.is_symlink() and path.suffix.lower() in supported),
            key=lambda path: path.name.casefold(),
        )[:limit]
        results = []
        for image_path in photos:
            output_file = output_path / f"studio_{image_path.stem}.jpg"
            results.append(self.process(str(image_path), str(output_file)))
        return results
