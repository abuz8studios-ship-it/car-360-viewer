from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from ai_studio.plate_blur import blur_license_plate  # noqa: E402


def test_plate_blur_changes_a_synthetic_plate_in_its_detection_region() -> None:
    pytest.importorskip("cv2")
    image = Image.new("RGB", (640, 480), (190, 190, 190))
    draw = ImageDraw.Draw(image)
    draw.rectangle((200, 330, 440, 390), fill="white", outline="black", width=5)
    draw.text((250, 347), "1ABC2345", fill="black")

    blurred = blur_license_plate(image)

    original_pixels = np.asarray(image).astype(np.int16)
    blurred_pixels = np.asarray(blurred).astype(np.int16)
    plate_region_difference = np.abs(
        original_pixels[325:395, 195:445] - blurred_pixels[325:395, 195:445]
    )
    assert int(plate_region_difference.sum()) > 10_000
