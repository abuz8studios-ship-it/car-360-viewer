"""
Test Kokoro TTS — generates a car commercial voiceover WAV
Run with: .\ComfyUI\.venv\Scripts\python.exe test_kokoro.py
"""
import sys, os

KOKORO_MODEL = r".\MODELS\Kokoro\kokoro-v1_0.pth"
OUTPUT_WAV = r"C:\Temp\lotvue_test_voiceover.wav"

print("Step 1: Import kokoro...")
from kokoro import KPipeline
import soundfile as sf
import numpy as np
print("  OK")

print("Step 2: Load pipeline (lang=a for American English)...")
pipeline = KPipeline(lang_code='a')
print("  OK")

print("Step 3: Generate voiceover...")
text = "2024 Honda Accord Sport. Two point zero liter turbocharged engine. Continuously variable transmission. Available now at your local dealership."

samples = []
generator = pipeline(text, voice='af_heart', speed=0.9, split_pattern=r'\n+')
for i, (gs, ps, audio) in enumerate(generator):
    samples.append(audio)
    print(f"  Chunk {i+1}: {len(audio)} samples")

print("Step 4: Concatenate and save WAV...")
full_audio = np.concatenate(samples)
sf.write(OUTPUT_WAV, full_audio, 24000)
duration = len(full_audio) / 24000
print(f"  Saved: {OUTPUT_WAV}")
print(f"  Duration: {duration:.2f} seconds")
print(f"  Samples: {len(full_audio)}")

print()
print("=== KOKORO TTS: PASS ===")
