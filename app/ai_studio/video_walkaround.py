"""
LotVue Pro — AI Video Walkaround Generator
Auto-picks 4 best angles from exterior photos, sends to factory.py for
AI video generation, generates Kokoro TTS voiceover, stitches with FFmpeg.
Output: 30-second cinematic walkaround.mp4

Uses:
  - factory.py (Wan2.2 ti2v 5B) for AI video clips
  - Kokoro TTS for voiceover
  - FFmpeg for stitching
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from PIL import Image

from app_paths import BASE_DIR, STORAGE_DIR, configured_path

_COMFYUI_ROOT = configured_path("COMFYUI_ROOT", BASE_DIR / "ComfyUI")
FACTORY_DIR = configured_path("LOTVUE_FACTORY_DIR", _COMFYUI_ROOT / "revenue_workflows")
COMFY_INPUT = configured_path("COMFYUI_INPUT", _COMFYUI_ROOT / "input")
COMFY_OUTPUT = configured_path("COMFYUI_OUTPUT", _COMFYUI_ROOT / "output")
COMFYUI_URL = (os.getenv("COMFYUI_URL") or "http://127.0.0.1:8188").rstrip("/")
FFMPEG = os.getenv("FFMPEG_PATH") or shutil.which("ffmpeg") or "ffmpeg"
LOTVUE_PYTHON = os.getenv("LOTVUE_PYTHON") or sys.executable

MAX_WALKAROUND_FRAMES = 72
MAX_WALKAROUND_ANGLES = 4
MAX_WALKAROUND_SCAN_ENTRIES = 4096
MAX_WALKAROUND_VALIDATION_BYTES = 200 * 1024 * 1024
DEFAULT_MAX_PHOTO_BYTES = 30 * 1024 * 1024
DEFAULT_MAX_IMAGE_PIXELS = 40_000_000

# Motion prompts per angle
ANGLE_PROMPTS = {
    "front": "slow cinematic dolly zoom, studio lighting, automotive commercial, high detail",
    "side": "smooth tracking shot along vehicle profile, depth of field, studio lighting",
    "rear": "slow pan across rear of vehicle, reflective floor, cinematic lighting",
    "quarter": "cinematic orbit around vehicle, shallow depth of field, studio quality",
}

# Status tracking per VIN
_walkaround_status = {}


def _bounded_jpeg_names(
    directory: Path,
    *,
    max_frames: int = MAX_WALKAROUND_FRAMES,
    max_scan_entries: int = MAX_WALKAROUND_SCAN_ENTRIES,
) -> list[str]:
    """Collect sorted JPEG basenames after scanning at most the configured entries."""
    max_frames = min(MAX_WALKAROUND_FRAMES, max(0, int(max_frames)))
    max_scan_entries = min(
        MAX_WALKAROUND_SCAN_ENTRIES,
        max(0, int(max_scan_entries)),
    )
    if (
        max_frames <= 0
        or max_scan_entries <= 0
        or directory.parent.is_symlink()
        or directory.is_symlink()
        or not directory.is_dir()
    ):
        return []
    frame_names = []
    other_names = []
    try:
        with os.scandir(directory) as entries:
            for _ in range(max_scan_entries):
                try:
                    entry = next(entries)
                except StopIteration:
                    break
                try:
                    if (
                        entry.is_symlink()
                        or not entry.is_file(follow_symlinks=False)
                        or entry.name.startswith(".")
                        or not entry.name.lower().endswith(".jpg")
                    ):
                        continue
                except OSError:
                    continue
                if entry.name.startswith("frame_"):
                    frame_names.append(entry.name)
                else:
                    other_names.append(entry.name)
    except OSError:
        return []

    candidates = frame_names or other_names
    return sorted(candidates)[:max_frames]


def _source_photo_size(path: Path, max_photo_bytes: int) -> int | None:
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > max_photo_bytes:
        return None
    return info.st_size


def _source_photo_is_valid(path: Path, max_image_pixels: int) -> bool:
    try:
        with Image.open(path) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > max_image_pixels:
                return False
            image.verify()
    except Exception:
        return False
    return True


def _angles_from_names(
    directory: Path,
    names: list[str],
    *,
    max_photo_bytes: int,
    max_image_pixels: int,
    validation_bytes_limit: int,
) -> dict:
    """Choose representative angles and validate only the at-most-four inputs used."""
    if not names:
        return {}
    frames = [directory / name for name in names]
    count = len(frames)
    if count >= MAX_WALKAROUND_ANGLES:
        selected = {
            "front": frames[0],
            "side": frames[count // 4],
            "rear": frames[count // 2],
            "quarter": frames[3 * count // 4],
        }
    elif count >= 2:
        selected = {"front": frames[0], "side": frames[count // 2]}
    else:
        selected = {"front": frames[0]}

    angles = {}
    validated_bytes = 0
    for angle, path in selected.items():
        file_size = _source_photo_size(path, max_photo_bytes)
        if file_size is None or validated_bytes + file_size > validation_bytes_limit:
            continue
        validated_bytes += file_size
        if _source_photo_is_valid(path, max_image_pixels):
            angles[angle] = path
    return angles


def pick_best_angles(
    exterior_dir: Path,
    *,
    max_frames: int = MAX_WALKAROUND_FRAMES,
    max_scan_entries: int = MAX_WALKAROUND_SCAN_ENTRIES,
    max_photo_bytes: int = DEFAULT_MAX_PHOTO_BYTES,
    max_image_pixels: int = DEFAULT_MAX_IMAGE_PIXELS,
    validation_bytes_limit: int = MAX_WALKAROUND_VALIDATION_BYTES,
) -> dict:
    """
    Pick four representative angles from a bounded set of verified JPEGs.

    Frame names retain clockwise order. At most ``max_frames`` candidates are kept,
    ``max_scan_entries`` directory entries are examined, and only the selected
    (at most four) photos are opened for image verification.
    """
    frame_cap = min(MAX_WALKAROUND_FRAMES, max(0, int(max_frames)))
    scan_cap = min(MAX_WALKAROUND_SCAN_ENTRIES, max(0, int(max_scan_entries)))
    validation_cap = min(
        MAX_WALKAROUND_VALIDATION_BYTES,
        max(0, int(validation_bytes_limit)),
    )
    names = _bounded_jpeg_names(
        exterior_dir,
        max_frames=frame_cap,
        max_scan_entries=scan_cap,
    )
    return _angles_from_names(
        exterior_dir,
        names,
        max_photo_bytes=max_photo_bytes,
        max_image_pixels=max_image_pixels,
        validation_bytes_limit=validation_cap,
    )


def generate_tts_voiceover(text: str, output_path: str) -> bool:
    """
    Generate voiceover using Kokoro TTS (if available in ComfyUI).
    Falls back to silent audio if TTS is not available.
    """
    try:
        # Try Kokoro TTS via ComfyUI custom node
        import urllib.request
        workflow = {
            "1": {
                "class_type": "KokoroTTS",
                "inputs": {
                    "text": text,
                    "voice": "af_heart",
                    "speed": 0.9,
                }
            },
            "2": {
                "class_type": "SaveAudio",
                "inputs": {
                    "audio": ["1", 0],
                    "filename_prefix": "LOTVUE_TTS"
                }
            }
        }
        data = json.dumps({"prompt": workflow}).encode()
        req = urllib.request.Request(
            f"{COMFYUI_URL}/prompt",
            data=data,
            headers={"Content-Type": "application/json"}
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
        prompt_id = resp.get("prompt_id")

        # Wait for completion
        for _ in range(60):
            time.sleep(2)
            h = json.loads(urllib.request.urlopen(
                f"{COMFYUI_URL}/history/{prompt_id}", timeout=30
            ).read())
            if prompt_id in h:
                status = h[prompt_id].get("status", {})
                if status.get("completed"):
                    for _, o in h[prompt_id].get("outputs", {}).items():
                        for a in o.get("audio", []):
                            src = os.path.join(str(COMFY_OUTPUT), a["filename"])
                            shutil.copy2(src, output_path)
                            return True
                if status.get("status_str") == "error":
                    break
    except Exception as e:
        print(f"[Walkaround] TTS failed: {e}")

    # Fallback: generate 5 seconds of silence
    try:
        subprocess.run([
            FFMPEG, "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", "5", "-y", output_path
        ], capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def generate_walkaround(
    vin: str,
    vehicle_info: dict,
    *,
    max_photo_bytes: int = DEFAULT_MAX_PHOTO_BYTES,
    max_image_pixels: int = DEFAULT_MAX_IMAGE_PIXELS,
    validation_bytes_limit: int = MAX_WALKAROUND_VALIDATION_BYTES,
) -> Optional[str]:
    """
    Generate a full AI walkaround video using bounded, verified source photos.

    Returns path to output video or None on failure. The default input limits
    match the standard photo-upload configuration; the application route passes
    its effective photo and pixel limits explicitly.
    """
    _walkaround_status[vin] = {"status": "starting", "progress": 0, "step": "Initializing"}
    validation_bytes_limit = min(
        MAX_WALKAROUND_VALIDATION_BYTES,
        max(0, int(validation_bytes_limit)),
    )

    exterior_dir = STORAGE_DIR / vin / "exterior"
    studio_dir = STORAGE_DIR / vin / "studio"
    walkaround_dir = STORAGE_DIR / vin / "walkaround"
    if walkaround_dir.parent.is_symlink() or walkaround_dir.is_symlink():
        _walkaround_status[vin] = {"status": "error", "error": "Unsafe vehicle media path"}
        return None
    walkaround_dir.mkdir(exist_ok=True)

    # Prefer a bounded studio JPEG set; fall back to exterior only when none exist.
    studio_names = _bounded_jpeg_names(studio_dir)
    if studio_names:
        angles = _angles_from_names(
            studio_dir,
            studio_names,
            max_photo_bytes=max_photo_bytes,
            max_image_pixels=max_image_pixels,
            validation_bytes_limit=validation_bytes_limit,
        )
    else:
        angles = pick_best_angles(
            exterior_dir,
            max_photo_bytes=max_photo_bytes,
            max_image_pixels=max_image_pixels,
            validation_bytes_limit=validation_bytes_limit,
        )
    if not angles:
        _walkaround_status[vin] = {"status": "error", "error": "No usable photos found"}
        return None

    # Generate video clips for each angle
    clips = []
    total_angles = len(angles)

    for i, (angle_name, photo_path) in enumerate(angles.items()):
        _walkaround_status[vin] = {
            "status": "processing",
            "progress": int((i / total_angles) * 70),
            "step": f"Generating {angle_name} clip ({i+1}/{total_angles})"
        }

        prompt = ANGLE_PROMPTS.get(angle_name, ANGLE_PROMPTS["front"])
        clip_path = walkaround_dir / f"clip_{angle_name}.mp4"

        try:
            # Use factory.py render pattern
            import sys
            sys.path.insert(0, str(FACTORY_DIR))
            from factory import stage_image, render_one

            output_filename = render_one(str(photo_path), prompt)
            if output_filename:
                src = COMFY_OUTPUT / output_filename
                if src.exists():
                    shutil.copy2(str(src), str(clip_path))
                    clips.append(str(clip_path))
        except Exception as e:
            print(f"[Walkaround] Clip generation failed for {angle_name}: {e}")

    if not clips:
        _walkaround_status[vin] = {"status": "error", "error": "No clips generated"}
        return None

    # Generate voiceover
    _walkaround_status[vin] = {"status": "processing", "progress": 75, "step": "Generating voiceover"}

    year = vehicle_info.get("year", "")
    make = vehicle_info.get("make", "")
    model = vehicle_info.get("model", "")
    trim = vehicle_info.get("trim_level", "")
    mileage = vehicle_info.get("mileage", "")
    price = vehicle_info.get("price", "")

    tts_text = f"{year} {make} {model}"
    if trim:
        tts_text += f", {trim}"
    if mileage:
        tts_text += f", {mileage} miles"
    if price:
        tts_text += f", ${price}"

    voiceover_path = str(walkaround_dir / "voiceover.wav")
    generate_tts_voiceover(tts_text, voiceover_path)

    # FFmpeg stitch
    _walkaround_status[vin] = {"status": "processing", "progress": 85, "step": "Assembling walkaround video"}

    output_video = str(walkaround_dir / "walkaround.mp4")

    try:
        # Create concat file
        concat_file = str(walkaround_dir / "concat.txt")
        with open(concat_file, "w") as f:
            for clip in clips:
                f.write(f"file '{clip}'\n")

        cmd = [
            FFMPEG,
            "-f", "concat", "-safe", "0", "-i", concat_file,
        ]
        if os.path.exists(voiceover_path):
            cmd += ["-i", voiceover_path, "-map", "0:v", "-map", "1:a", "-shortest"]
        cmd += [
            "-c:v", "libx264", "-preset", "medium", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            "-y", output_video
        ]

        subprocess.run(cmd, capture_output=True, timeout=120)

        if os.path.exists(output_video):
            size_mb = round(os.path.getsize(output_video) / (1024 * 1024), 1)
            _walkaround_status[vin] = {
                "status": "done",
                "progress": 100,
                "step": "complete",
                "url": f"/storage/{vin}/walkaround/walkaround.mp4",
                "size_mb": size_mb,
            }
            return output_video

    except Exception as e:
        print(f"[Walkaround] FFmpeg stitch failed: {e}")

    _walkaround_status[vin] = {"status": "error", "error": "Video assembly failed"}
    return None


def get_walkaround_status(vin: str) -> dict:
    """Get current walkaround generation status."""
    return _walkaround_status.get(vin, {"status": "none"})


def mark_walkaround_failed(vin: str) -> None:
    """Expose an unexpected worker exception without leaking exception details."""
    _walkaround_status[vin] = {
        "status": "error",
        "error": "Walkaround generation stopped unexpectedly",
    }
