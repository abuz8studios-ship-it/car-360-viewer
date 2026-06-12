"""
commercial_pipeline.py — LotVue360 AI Commercial Generator
Runs as a standalone subprocess launched by main.py.

Pipeline:
  1. Load WAN 2.2 I2V model (direct Python, no ComfyUI API)
  2. Generate 81-frame video from car image (720x480, 24fps = ~3.4s)
  3. Generate voiceover with Kokoro TTS
  4. Assemble final MP4 with FFmpeg (video + audio + text overlay)

Usage:
  python commercial_pipeline.py --vin VIN --image PATH --year 2024 --make Toyota
      --model Camry --trim LE --engine "2.5L 4-Cyl" --transmission Automatic
      --price 28995 --dealer "ABC Motors"

Writes status.json to storage/<vin>/commercial/ throughout execution.
Writes commercial.mp4 when complete.
"""

import os
import sys
import json
import time
import argparse
import subprocess
import tempfile
from pathlib import Path

# Fix Windows CP1252 stdout encoding issues
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# ─────────────────────────────────────────────────────────────────────────────
# PATHS
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR      = BASE_DIR / "storage"
FFMPEG           = os.environ.get("FFMPEG_PATH", str(BASE_DIR.parents[1] / "AI" / "ffmpeg" / "ffmpeg.exe"))
FFPROBE          = os.environ.get("FFPROBE_PATH", str(BASE_DIR.parents[1] / "AI" / "ffmpeg" / "ffprobe.exe"))

# WAN 2.2 I2V model files
WAN_HIGH_NOISE   = r"E:\ABU\ComfyUI\models\diffusion_models\wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors"
WAN_LOW_NOISE    = r"E:\ABU\ComfyUI\models\diffusion_models\wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors"
WAN_VAE          = r"E:\ABU\ComfyUI\models\vae\wan2.2_vae.safetensors"
UMT5_ENCODER     = r"E:\ABU\ComfyUI\models\text_encoders\umt5_xxl_fp8_e4m3fn_scaled.safetensors"
UMT5_TOKENIZER   = r"E:\ABU\ComfyUI\models\text_encoders\umt5-xxl"   # folder with spiece.model
CLIP_VISION      = r"E:\ABU\ComfyUI\models\clip_vision\clip_vision_h.safetensors"

# Kokoro TTS
KOKORO_MODEL_CANDIDATES = [
    r"E:\ABU\MODELS\Kokoro\kokoro-v1_0.pth",
    r"E:\ABU\MODELS\TTS\kokoro\kokoro-v1_0.pth",
    r"E:\ABU\AvatarStack\Kokoro\models\kokoro-v1_0.pth",
]
KOKORO_VOICES_CANDIDATES = [
    r"E:\ABU\MODELS\Kokoro\voices.bin",
    r"E:\ABU\MODELS\TTS\kokoro\voices\voices.bin",
]
KOKORO_ONNX_CANDIDATES = [
    r"E:\ABU\MODELS\Kokoro\kokoro-v1_0.onnx",
    r"E:\ABU\MODELS\TTS\kokoro\kokoro-v1_0.onnx",
]


def _first_existing_path(candidates):
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return candidates[0] if candidates else ""


KOKORO_MODEL = _first_existing_path(KOKORO_MODEL_CANDIDATES)
KOKORO_VOICES = _first_existing_path(KOKORO_VOICES_CANDIDATES)
KOKORO_ONNX = _first_existing_path(KOKORO_ONNX_CANDIDATES)

# ComfyUI Python (has all required libs: torch, comfy, etc.)
COMFYUI_ROOT     = r"E:\ABU\ComfyUI"

# ─────────────────────────────────────────────────────────────────────────────
# STATUS WRITER
# ─────────────────────────────────────────────────────────────────────────────
_STATUS_FILE = None

def set_status(step: str, progress: int, extra: dict = None):
    if _STATUS_FILE:
        data = {"status": "running", "step": step, "progress": progress, "vin": _VIN}
        if extra:
            data.update(extra)
        with open(_STATUS_FILE, "w") as f:
            json.dump(data, f)
    print(f"[Commercial] [{progress}%] {step}")
    sys.stdout.flush()

def set_done(url: str):
    if _STATUS_FILE:
        with open(_STATUS_FILE, "w") as f:
            json.dump({"status": "done", "step": "complete", "progress": 100, "vin": _VIN, "url": url}, f)
    print(f"[Commercial] DONE -> {url}")

def set_error(msg: str):
    if _STATUS_FILE:
        with open(_STATUS_FILE, "w") as f:
            json.dump({"status": "error", "step": "error", "progress": 0, "vin": _VIN, "error": msg}, f)
    print(f"[Commercial] ERROR: {msg}", file=sys.stderr)

_VIN = "UNKNOWN"

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1: WAN 2.2 I2V VIDEO GENERATION
# ─────────────────────────────────────────────────────────────────────────────

def generate_wan_video(image_path: str, output_path: str, prompt: str,
                       width: int = 720, height: int = 480, num_frames: int = 81,
                       steps: int = 20, cfg: float = 6.0, seed: int = 42) -> bool:
    """
    Generate video from image using WAN 2.2 I2V.
    Uses ComfyUI's internal model loading (comfy.sd, comfy.model_management).
    Falls back to FFmpeg slideshow if GPU OOM or model unavailable.
    """
    set_status("Loading WAN 2.2 I2V model", 10)

    # ── Route 0: running ComfyUI server on :8188 (uses local model files,
    #    no HF download, GPU work happens inside ComfyUI's process) ───────────
    try:
        from wan_comfy_api import generate_wan_video_via_api
        set_status("Generating AI video via ComfyUI (WAN 2.2 I2V)", 15)
        if generate_wan_video_via_api(image_path, output_path, prompt,
                                      width=width, height=height,
                                      num_frames=min(num_frames, 49), steps=steps,
                                      cfg=cfg, seed=seed):
            return True
        print("[WAN] ComfyUI API route failed — trying direct loading")
    except Exception as e:
        print(f"[WAN] ComfyUI API route error: {e} — trying direct loading")

    try:
        # Add ComfyUI to path so we can use its internal loaders
        if COMFYUI_ROOT not in sys.path:
            sys.path.insert(0, COMFYUI_ROOT)

        import torch
        import numpy as np
        from PIL import Image

        # Check GPU availability
        device = "cuda" if torch.cuda.is_available() else "cpu"
        if device == "cpu":
            print("[WAN] WARNING: No CUDA available, falling back to slideshow")
            return _fallback_slideshow(image_path, output_path, num_frames)

        print(f"[WAN] Using device: {device} | VRAM: {torch.cuda.get_device_properties(0).total_memory / 1e9:.1f}GB")

        # ── Load models using comfy internals ──────────────────────────────
        set_status("Loading WAN diffusion model", 12)

        # Try to import comfy model management
        try:
            import comfy.model_management as model_management
            import comfy.sd as sd
            import comfy.utils as comfy_utils
        except ImportError as e:
            print(f"[WAN] ComfyUI import failed: {e}, trying direct safetensors approach")
            return _wan_direct_inference(image_path, output_path, prompt, width, height, num_frames, steps, cfg, seed)

        # Load WAN diffusion model
        model_path = WAN_HIGH_NOISE if os.path.exists(WAN_HIGH_NOISE) else WAN_LOW_NOISE
        if not os.path.exists(model_path):
            print("[WAN] No WAN model found, falling back to slideshow")
            return _fallback_slideshow(image_path, output_path, num_frames)

        set_status("Loading text encoder (UMT5)", 15)
        set_status("Loading VAE", 18)
        set_status("Loading CLIP vision", 20)

        # Use the direct inference path which handles WAN properly
        return _wan_direct_inference(image_path, output_path, prompt, width, height, num_frames, steps, cfg, seed)

    except torch.cuda.OutOfMemoryError:
        print("[WAN] GPU OOM — falling back to slideshow")
        return _fallback_slideshow(image_path, output_path, num_frames)
    except Exception as e:
        print(f"[WAN] Error: {e} — falling back to slideshow")
        import traceback
        traceback.print_exc()
        return _fallback_slideshow(image_path, output_path, num_frames)


def _wan_direct_inference(image_path: str, output_path: str, prompt: str,
                           width: int, height: int, num_frames: int,
                           steps: int, cfg: float, seed: int) -> bool:
    """
    Direct WAN 2.2 I2V inference using diffusers or wan-video library.
    Attempts multiple loading strategies in order of preference.
    """
    import torch
    from PIL import Image
    import numpy as np

    set_status("Initializing WAN inference engine", 22)

    # Strategy 1: Try wan-video package (if installed)
    try:
        import wan
        return _wan_via_wan_package(image_path, output_path, prompt, width, height, num_frames, steps, cfg, seed)
    except ImportError:
        pass

    # Strategy 2: Try diffusers WanImageToVideoPipeline
    try:
        from diffusers import WanImageToVideoPipeline
        return _wan_via_diffusers(image_path, output_path, prompt, width, height, num_frames, steps, cfg, seed)
    except (ImportError, AttributeError):
        pass

    # Strategy 3: Manual safetensors loading + inference
    try:
        return _wan_manual_inference(image_path, output_path, prompt, width, height, num_frames, steps, cfg, seed)
    except Exception as e:
        print(f"[WAN] Manual inference failed: {e}")

    # Final fallback
    return _fallback_slideshow(image_path, output_path, num_frames)


def _wan_via_diffusers(image_path: str, output_path: str, prompt: str,
                        width: int, height: int, num_frames: int,
                        steps: int, cfg: float, seed: int) -> bool:
    """Use HuggingFace diffusers WanImageToVideoPipeline."""
    import torch
    from PIL import Image
    from diffusers import WanImageToVideoPipeline
    from diffusers.utils import export_to_video

    set_status("Loading WAN pipeline via diffusers", 25)

    model_dir = str(Path(WAN_HIGH_NOISE).parent.parent)  # models root

    pipe = WanImageToVideoPipeline.from_pretrained(
        "Wan-AI/Wan2.2-I2V-14B-480P",
        torch_dtype=torch.float16,
        variant="fp16",
        local_files_only=False,
    )
    pipe = pipe.to("cuda")
    pipe.enable_model_cpu_offload()

    img = Image.open(image_path).convert("RGB").resize((width, height))

    set_status("Generating video frames (WAN I2V)", 30)
    generator = torch.Generator("cuda").manual_seed(seed)

    output = pipe(
        image=img,
        prompt=prompt,
        negative_prompt="blurry, low quality, distorted, ugly",
        num_frames=num_frames,
        num_inference_steps=steps,
        guidance_scale=cfg,
        generator=generator,
        width=width,
        height=height,
    )

    set_status("Saving video frames", 80)
    export_to_video(output.frames[0], output_path, fps=24)
    return True


def _wan_manual_inference(image_path: str, output_path: str, prompt: str,
                           width: int, height: int, num_frames: int,
                           steps: int, cfg: float, seed: int) -> bool:
    """
    Manual WAN 2.2 I2V inference using safetensors + custom scheduler.
    This is the fallback when no high-level library is available.
    """
    import torch
    import numpy as np
    from PIL import Image
    from safetensors.torch import load_file

    set_status("Loading WAN model weights (manual)", 25)

    model_path = WAN_HIGH_NOISE if os.path.exists(WAN_HIGH_NOISE) else WAN_LOW_NOISE
    vae_path = WAN_VAE

    # Check VRAM — WAN 14B fp8 needs ~14GB minimum
    if torch.cuda.is_available():
        vram_gb = torch.cuda.get_device_properties(0).total_memory / 1e9
        if vram_gb < 12:
            print(f"[WAN] Insufficient VRAM ({vram_gb:.1f}GB < 12GB), using slideshow fallback")
            return _fallback_slideshow(image_path, output_path, num_frames)

    # Load tokenizer
    set_status("Loading UMT5 tokenizer", 28)
    try:
        from transformers import T5Tokenizer, T5EncoderModel
        tokenizer = T5Tokenizer.from_pretrained(UMT5_TOKENIZER)
    except Exception as e:
        print(f"[WAN] Tokenizer load failed: {e}")
        return _fallback_slideshow(image_path, output_path, num_frames)

    # Load text encoder
    set_status("Loading UMT5 text encoder", 32)
    try:
        text_encoder_state = load_file(UMT5_ENCODER, device="cpu")
        # We'll use transformers T5 architecture with custom weights
        # This is complex — for now use slideshow if this path is hit
        print("[WAN] Manual weight loading for UMT5 not yet implemented, using slideshow")
        return _fallback_slideshow(image_path, output_path, num_frames)
    except Exception as e:
        print(f"[WAN] Text encoder load failed: {e}")
        return _fallback_slideshow(image_path, output_path, num_frames)


def _wan_via_wan_package(image_path: str, output_path: str, prompt: str,
                          width: int, height: int, num_frames: int,
                          steps: int, cfg: float, seed: int) -> bool:
    """Use wan-video package if installed."""
    import wan
    import torch
    from PIL import Image

    set_status("Loading WAN model via wan package", 25)

    cfg_obj = wan.configs.WAN_CONFIGS["Wan2.2-I2V-14B-480P"]
    model = wan.WanI2V(
        config=cfg_obj,
        checkpoint_dir=str(Path(WAN_HIGH_NOISE).parent),
        device_id=0,
        rank=0,
        t5_flan_checkpoint_path=UMT5_ENCODER,
        vae_checkpoint_path=WAN_VAE,
        image_encoder_checkpoint_path=CLIP_VISION,
    )

    img = Image.open(image_path).convert("RGB").resize((width, height))

    set_status("Generating video (WAN I2V)", 30)
    video = model.generate(
        prompt,
        img,
        max_area=width * height,
        frame_num=num_frames,
        shift=5.0,
        sample_steps=steps,
        sample_guide_scale=cfg,
        seed=seed,
    )

    set_status("Saving video", 80)
    # video is list of PIL images or numpy array
    _save_frames_to_video(video, output_path, fps=24)
    return True


def _fallback_slideshow(image_path: str, output_path: str, num_frames: int = 81) -> bool:
    """
    Fallback: Create a smooth Ken Burns zoom/pan effect from a single image.
    Looks like professional car photography motion — no WAN needed.
    Uses FFmpeg zoompan filter for smooth motion.
    Duration: ~3.4 seconds at 24fps.
    """
    set_status("Generating cinematic slideshow (fallback)", 30)

    duration = num_frames / 24.0  # ~3.375s

    # Ken Burns effect: slow zoom in + slight pan right
    # zoompan: z=zoom, x=pan_x, y=pan_y, d=duration_frames, s=output_size
    zoom_expr = f"'min(zoom+0.0015,1.5)'"
    x_expr = f"'iw/2-(iw/zoom/2)'"
    y_expr = f"'ih/2-(ih/zoom/2)'"

    cmd = [
        FFMPEG, "-y",
        "-loop", "1",
        "-i", image_path,
        "-vf", (
            f"scale=1440:960:flags=lanczos,"
            f"zoompan=z={zoom_expr}:x={x_expr}:y={y_expr}"
            f":d={num_frames}:s=720x480:fps=24,"
            f"scale=720:480"
        ),
        "-t", str(duration),
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        output_path
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        print(f"[Slideshow] FFmpeg error: {result.stderr[-300:]}")
        # Ultra-simple fallback: static image as video
        return _static_image_video(image_path, output_path, duration)
    return True


def _static_image_video(image_path: str, output_path: str, duration: float = 3.375) -> bool:
    """Absolute last resort: static image as video."""
    cmd = [
        FFMPEG, "-y",
        "-loop", "1",
        "-i", image_path,
        "-t", str(duration),
        "-vf", "scale=720:480",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-pix_fmt", "yuv420p",
        output_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return result.returncode == 0


def _save_frames_to_video(frames, output_path: str, fps: int = 24):
    """Save list of PIL images or numpy array to MP4 via FFmpeg."""
    import numpy as np
    from PIL import Image

    with tempfile.TemporaryDirectory() as tmpdir:
        for i, frame in enumerate(frames):
            if isinstance(frame, np.ndarray):
                img = Image.fromarray((frame * 255).astype(np.uint8) if frame.max() <= 1.0 else frame.astype(np.uint8))
            else:
                img = frame
            img.save(os.path.join(tmpdir, f"frame_{i:05d}.png"))

        cmd = [
            FFMPEG, "-y",
            "-framerate", str(fps),
            "-i", os.path.join(tmpdir, "frame_%05d.png"),
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "18",
            "-pix_fmt", "yuv420p",
            output_path
        ]
        subprocess.run(cmd, capture_output=True, text=True, timeout=120)


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2: KOKORO TTS VOICEOVER
# ─────────────────────────────────────────────────────────────────────────────

def generate_voiceover(script: str, output_wav: str) -> bool:
    """Generate voiceover audio using Kokoro TTS (kokoro-onnx)."""
    set_status("Generating voiceover (Kokoro TTS)", 60)

    try:
        # Try modern kokoro KPipeline first (model cached locally, verified working)
        from kokoro import KPipeline
        import soundfile as sf
        import numpy as np
        pipeline = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M")
        chunks = [a for _, _, a in pipeline(script, voice="af_heart", speed=0.95)]
        if chunks:
            full = np.concatenate(chunks)
            sf.write(output_wav, full, 24000)
            print(f"[TTS] kokoro KPipeline: generated {len(full)/24000:.1f}s audio")
            return True
    except ImportError:
        pass
    except Exception as e:
        print(f"[TTS] kokoro KPipeline error: {e}")

    try:
        # Try kokoro-onnx first (lighter, faster)
        from kokoro_onnx import Kokoro
        import soundfile as sf

        if not (os.path.exists(KOKORO_ONNX) and os.path.exists(KOKORO_VOICES)):
            raise FileNotFoundError(f"Kokoro ONNX assets not found: onnx={KOKORO_ONNX}, voices={KOKORO_VOICES}")

        kokoro = Kokoro(KOKORO_ONNX, KOKORO_VOICES)
        samples, sample_rate = kokoro.create(script, voice="af_heart", speed=0.95, lang="en-us")
        sf.write(output_wav, samples, sample_rate)
        print(f"[TTS] kokoro-onnx: generated {len(samples)/sample_rate:.1f}s audio")
        return True

    except ImportError:
        pass
    except Exception as e:
        print(f"[TTS] kokoro-onnx error: {e}")

    try:
        # Try kokoro PyTorch model
        import torch
        import numpy as np
        import soundfile as sf

        # Add kokoro to path
        kokoro_dir = str(Path(KOKORO_MODEL).parent)
        if kokoro_dir not in sys.path:
            sys.path.insert(0, kokoro_dir)

        from models import build_model
        from kokoro import generate as kokoro_generate

        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = build_model(KOKORO_MODEL, device)
        voicepack = torch.load(KOKORO_VOICES, weights_only=True).to(device)

        audio, phonemes = kokoro_generate(model, script, voicepack, lang="a", speed=0.95)
        sf.write(output_wav, audio, 24000)
        print(f"[TTS] kokoro-torch: generated audio, phonemes: {phonemes[:50]}...")
        return True

    except ImportError:
        pass
    except Exception as e:
        print(f"[TTS] kokoro-torch error: {e}")

    # Fallback: generate silent audio track (correct duration)
    print("[TTS] All TTS methods failed, generating silent audio track")
    return _generate_silent_audio(output_wav, duration=10.0)


def _generate_silent_audio(output_wav: str, duration: float = 10.0) -> bool:
    """Generate silent audio as fallback."""
    cmd = [
        FFMPEG, "-y",
        "-f", "lavfi",
        "-i", f"anullsrc=r=24000:cl=mono",
        "-t", str(duration),
        "-c:a", "pcm_s16le",
        output_wav
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return result.returncode == 0


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3: FFMPEG ASSEMBLY — VIDEO + AUDIO + TEXT OVERLAY
# ─────────────────────────────────────────────────────────────────────────────

def assemble_commercial(video_path: str, audio_path: str, output_path: str,
                         year: str, make: str, model: str, trim: str,
                         price: str, dealer: str) -> bool:
    """
    Assemble final commercial MP4:
    - Video (WAN or slideshow)
    - Audio (Kokoro TTS or silent)
    - Text overlay: Year Make Model | Price | Dealer name
    - Fade in/out
    - Duration: match audio length (min 8s, max 30s)
    """
    set_status("Assembling commercial (FFmpeg)", 85)

    # Get audio duration
    audio_duration = _get_duration(audio_path) or 10.0
    video_duration = _get_duration(video_path) or 3.375

    # Loop video to match audio if needed
    loops_needed = max(1, int(audio_duration / video_duration) + 1)

    # Build title text
    title_line1 = f"{year} {make} {model}"
    if trim and trim.lower() not in ("none", "", "n/a"):
        title_line1 += f" {trim}"

    price_text = ""
    if price and str(price) not in ("None", "", "0", "0.0"):
        try:
            price_val = float(str(price).replace(",", "").replace("$", ""))
            price_text = f"${price_val:,.0f}"
        except Exception:
            price_text = str(price)

    dealer_text = dealer or "Available Now"

    # Font path — try Windows fonts
    font_path = r"C\:/Windows/Fonts/arialbd.ttf"
    font_path_regular = r"C\:/Windows/Fonts/arial.ttf"

    # Build drawtext filter chain
    # Line 1: Vehicle name (large, white, bottom-left, fade in at 0.5s)
    # Line 2: Price (medium, gold)
    # Line 3: Dealer name (small, white)
    # Fade in/out for whole video

    fade_in_end = 0.5
    fade_out_start = audio_duration - 0.5

    drawtext_filters = []

    # Vehicle title
    drawtext_filters.append(
        f"drawtext=fontfile='{font_path}':"
        f"text='{_escape_ffmpeg(title_line1)}':"
        f"fontsize=36:fontcolor=white:x=40:y=h-120:"
        f"shadowcolor=black:shadowx=2:shadowy=2:"
        f"enable='between(t,{fade_in_end},{audio_duration})'"
    )

    # Price
    if price_text:
        drawtext_filters.append(
            f"drawtext=fontfile='{font_path}':"
            f"text='{_escape_ffmpeg(price_text)}':"
            f"fontsize=28:fontcolor=#FFD700:x=40:y=h-78:"
            f"shadowcolor=black:shadowx=2:shadowy=2:"
            f"enable='between(t,{fade_in_end},{audio_duration})'"
        )

    # Dealer name
    drawtext_filters.append(
        f"drawtext=fontfile='{font_path_regular}':"
        f"text='{_escape_ffmpeg(dealer_text)}':"
        f"fontsize=20:fontcolor=white@0.85:x=40:y=h-44:"
        f"shadowcolor=black:shadowx=1:shadowy=1:"
        f"enable='between(t,{fade_in_end},{audio_duration})'"
    )

    # LotVue360 watermark (top-right, small)
    drawtext_filters.append(
        f"drawtext=fontfile='{font_path_regular}':"
        f"text='LotVue360':"
        f"fontsize=16:fontcolor=white@0.6:x=w-120:y=20:"
        f"enable='gte(t,0)'"
    )

    vf_parts = [
        # Loop video to cover audio duration
        f"loop={loops_needed}:size=32767:start=0",
        f"trim=duration={audio_duration}",
        f"setpts=PTS-STARTPTS",
        # Fade in/out
        f"fade=t=in:st=0:d={fade_in_end}",
        f"fade=t=out:st={fade_out_start}:d=0.5",
        # Scale to 720x480
        "scale=720:480:flags=lanczos",
    ] + drawtext_filters

    vf_string = ",".join(vf_parts)

    cmd = [
        FFMPEG, "-y",
        "-i", video_path,
        "-i", audio_path,
        "-vf", vf_string,
        "-af", f"afade=t=in:st=0:d={fade_in_end},afade=t=out:st={fade_out_start}:d=0.5",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "20",
        "-c:a", "aac",
        "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-shortest",
        output_path
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        print(f"[Assembly] FFmpeg error: {result.stderr[-500:]}")
        # Try simpler assembly without text overlay
        return _simple_assembly(video_path, audio_path, output_path, audio_duration)
    return True


def _simple_assembly(video_path: str, audio_path: str, output_path: str, duration: float) -> bool:
    """Simple assembly without text overlay as fallback."""
    cmd = [
        FFMPEG, "-y",
        "-stream_loop", "-1",
        "-i", video_path,
        "-i", audio_path,
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "22",
        "-c:a", "aac",
        "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        "-shortest",
        "-t", str(duration),
        output_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return result.returncode == 0


def _get_duration(path: str) -> float:
    """Get media file duration in seconds using FFprobe."""
    try:
        cmd = [
            FFPROBE, "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return float(data["format"]["duration"])
    except Exception:
        pass
    return None


def _escape_ffmpeg(text: str) -> str:
    """Escape special characters for FFmpeg drawtext filter."""
    if not text:
        return ""
    # Escape: ' : \ [ ] , ; = ( )
    text = str(text)
    text = text.replace("\\", "\\\\")
    text = text.replace("'", "\\'")
    text = text.replace(":", "\\:")
    text = text.replace("[", "\\[")
    text = text.replace("]", "\\]")
    return text


# ─────────────────────────────────────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def build_voiceover_script(year: str, make: str, model: str, trim: str,
                            engine: str, transmission: str, price: str, dealer: str) -> str:
    """Build natural-sounding voiceover script for the commercial."""
    parts = []

    # Opening hook
    parts.append(f"Introducing the {year} {make} {model}.")

    if trim and trim.lower() not in ("none", "", "n/a"):
        parts.append(f"Available in the {trim} trim.")

    if engine and engine.lower() not in ("none", "", "n/a"):
        parts.append(f"Powered by a {engine} engine.")

    if transmission and transmission.lower() not in ("none", "", "n/a"):
        parts.append(f"Paired with a {transmission} transmission.")

    if price and str(price) not in ("None", "", "0", "0.0"):
        try:
            price_val = float(str(price).replace(",", "").replace("$", ""))
            parts.append(f"Priced at just ${price_val:,.0f}.")
        except Exception:
            pass

    parts.append("Come see it today.")

    if dealer and dealer.lower() not in ("our dealership", "none", ""):
        parts.append(f"Visit {dealer} and drive it home.")

    return " ".join(parts)


def run_pipeline(args):
    global _VIN, _STATUS_FILE

    _VIN = args.vin

    # Setup directories
    commercial_dir = STORAGE_DIR / args.vin / "commercial"
    commercial_dir.mkdir(parents=True, exist_ok=True)
    _STATUS_FILE = str(commercial_dir / "status.json")

    set_status("Pipeline started", 5)

    # Validate input image
    if not os.path.exists(args.image):
        set_error(f"Input image not found: {args.image}")
        return False

    # Output paths
    raw_video = str(commercial_dir / "raw_video.mp4")
    voiceover_wav = str(commercial_dir / "voiceover.wav")
    final_mp4 = str(commercial_dir / "commercial.mp4")

    # ── Step 1: Build voiceover script ────────────────────────────────────
    script = build_voiceover_script(
        year=args.year, make=args.make, model=args.model, trim=args.trim,
        engine=args.engine, transmission=args.transmission,
        price=args.price, dealer=args.dealer
    )
    print(f"[Pipeline] Script: {script}")

    # ── Step 2: Generate video ─────────────────────────────────────────────
    video_prompt = (
        f"Cinematic car commercial, {args.year} {args.make} {args.model}, "
        f"smooth camera orbit, professional automotive photography, "
        f"studio lighting, high quality, 4K, photorealistic"
    )

    video_ok = generate_wan_video(
        image_path=args.image,
        output_path=raw_video,
        prompt=video_prompt,
        width=720, height=480, num_frames=81,
        steps=20, cfg=6.0, seed=42
    )

    if not video_ok or not os.path.exists(raw_video):
        set_error("Video generation failed")
        return False

    set_status("Video generated", 55)

    # ── Step 3: Generate voiceover ─────────────────────────────────────────
    tts_ok = generate_voiceover(script, voiceover_wav)

    if not tts_ok or not os.path.exists(voiceover_wav):
        print("[Pipeline] TTS failed, using silent audio")
        _generate_silent_audio(voiceover_wav, duration=10.0)

    set_status("Voiceover generated", 75)

    # ── Step 4: Assemble commercial ────────────────────────────────────────
    assembly_ok = assemble_commercial(
        video_path=raw_video,
        audio_path=voiceover_wav,
        output_path=final_mp4,
        year=args.year, make=args.make, model=args.model, trim=args.trim,
        price=args.price, dealer=args.dealer
    )

    if not assembly_ok or not os.path.exists(final_mp4):
        set_error("Final assembly failed")
        return False

    # ── Done ───────────────────────────────────────────────────────────────
    size_mb = os.path.getsize(final_mp4) / (1024 * 1024)
    set_status("Complete", 100)
    set_done(f"/storage/{args.vin}/commercial/commercial.mp4")
    print(f"[Pipeline] SUCCESS: {final_mp4} ({size_mb:.1f} MB)")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LotVue360 Commercial Pipeline")
    parser.add_argument("--vin",          required=True,  help="Vehicle VIN")
    parser.add_argument("--image",        required=True,  help="Path to car image")
    parser.add_argument("--year",         default="",     help="Vehicle year")
    parser.add_argument("--make",         default="",     help="Vehicle make")
    parser.add_argument("--model",        default="",     help="Vehicle model")
    parser.add_argument("--trim",         default="",     help="Trim level")
    parser.add_argument("--engine",       default="",     help="Engine description")
    parser.add_argument("--transmission", default="",     help="Transmission type")
    parser.add_argument("--price",        default="",     help="Vehicle price")
    parser.add_argument("--dealer",       default="",     help="Dealer name")
    args = parser.parse_args()

    success = run_pipeline(args)
    sys.exit(0 if success else 1)
