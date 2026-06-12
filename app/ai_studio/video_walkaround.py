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
import os
import json
import time
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

STORAGE_DIR = Path(r"E:\ABU\360Car\app\storage")
FACTORY_DIR = Path(r"E:\ABU\ComfyUI\revenue_workflows")
COMFY_INPUT = Path(r"E:\ABU\ComfyUI\input")
COMFY_OUTPUT = Path(r"E:\ABU\ComfyUI\output")
FFMPEG = r"E:\ABU\AI\ffmpeg\ffmpeg.exe"
LOTVUE_PYTHON = r"E:\Miniconda3\envs\lotvue\python.exe"

# Motion prompts per angle
ANGLE_PROMPTS = {
    "front": "slow cinematic dolly zoom, studio lighting, automotive commercial, high detail",
    "side": "smooth tracking shot along vehicle profile, depth of field, studio lighting",
    "rear": "slow pan across rear of vehicle, reflective floor, cinematic lighting",
    "quarter": "cinematic orbit around vehicle, shallow depth of field, studio quality",
}

# Status tracking per VIN
_walkaround_status = {}


def pick_best_angles(exterior_dir: Path) -> dict:
    """
    Pick 4 best angles from exterior photos.
    Assumes photos are ordered: frame_0001.jpg = front-left, going clockwise.
    For N frames, picks: front (1), right side (N/4), rear (N/2), 3/4 front (3*N/4)
    """
    frames = sorted(exterior_dir.glob("frame_*.jpg"))
    if not frames:
        frames = sorted(exterior_dir.glob("*.jpg"))
    if not frames:
        return {}

    n = len(frames)
    angles = {}

    # Map positions to angle names
    if n >= 4:
        angles["front"] = frames[0]
        angles["side"] = frames[n // 4]
        angles["rear"] = frames[n // 2]
        angles["quarter"] = frames[3 * n // 4]
    elif n >= 2:
        angles["front"] = frames[0]
        angles["side"] = frames[n // 2]
    else:
        angles["front"] = frames[0]

    return angles


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
            "http://127.0.0.1:8188/prompt",
            data=data,
            headers={"Content-Type": "application/json"}
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
        prompt_id = resp.get("prompt_id")

        # Wait for completion
        for _ in range(60):
            time.sleep(2)
            h = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:8188/history/{prompt_id}", timeout=30
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
    except:
        return False


def generate_walkaround(vin: str, vehicle_info: dict) -> Optional[str]:
    """
    Generate a full AI walkaround video for a vehicle.
    Returns path to output video or None on failure.
    """
    _walkaround_status[vin] = {"status": "starting", "progress": 0, "step": "Initializing"}

    exterior_dir = STORAGE_DIR / vin / "exterior"
    studio_dir = STORAGE_DIR / vin / "studio"
    walkaround_dir = STORAGE_DIR / vin / "walkaround"
    walkaround_dir.mkdir(exist_ok=True)

    # Use studio photos if available, else raw exterior
    source_dir = studio_dir if studio_dir.exists() and list(studio_dir.glob("*.jpg")) else exterior_dir

    angles = pick_best_angles(source_dir)
    if not angles:
        _walkaround_status[vin] = {"status": "error", "error": "No photos found"}
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
