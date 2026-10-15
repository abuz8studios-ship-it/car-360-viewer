"""License-plate blur: detector, irreversibility, per-vehicle batch, API."""
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from ai_studio.plate_blur import blur_boxes, blur_vehicle, detect_plates  # noqa: E402

from conftest import register  # noqa: E402

PLATE = (560, 520, 760, 620)  # x1, y1, x2, y2


def car_photo(with_plate=True, tilt=0):
    """A red 'car' bumper on asphalt with an optional white US-style plate."""
    img = Image.new("RGB", (1280, 854), (88, 90, 94))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((200, 250, 1080, 700), radius=80, fill=(170, 25, 30))
    d.rectangle((260, 330, 1020, 420), fill=(30, 30, 34))  # rear window band
    if with_plate:
        plate = Image.new("RGB", (PLATE[2] - PLATE[0], PLATE[3] - PLATE[1]), (236, 236, 230))
        pd = ImageDraw.Draw(plate)
        pd.rectangle((0, 0, plate.width - 1, plate.height - 1), outline=(40, 40, 40), width=3)
        font = ImageFont.load_default(size=64)
        pd.text((plate.width // 2, plate.height // 2 + 4), "ABC 1234", fill=(20, 20, 60),
                font=font, anchor="mm")
        if tilt:
            plate = plate.rotate(tilt, expand=True, fillcolor=(170, 25, 30))
        img.paste(plate, (PLATE[0], PLATE[1]))
    return img


def _overlap(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    return ix * iy / ((b[2] - b[0]) * (b[3] - b[1]))


def test_detects_plate():
    boxes = detect_plates(car_photo())
    assert len(boxes) >= 1
    assert _overlap(boxes[0], PLATE) > 0.6


def test_detects_tilted_plate():
    boxes = detect_plates(car_photo(tilt=8))
    assert boxes, "tilted plate missed"


def test_no_plate_no_detection():
    assert detect_plates(car_photo(with_plate=False)) == []


def test_blur_destroys_characters():
    img = car_photo()
    out = blur_boxes(img, detect_plates(img))
    import cv2

    def detail(im):  # Laplacian energy = character-scale edges
        g = np.asarray(im.convert("L"))[PLATE[1]:PLATE[3], PLATE[0]:PLATE[2]]
        return cv2.Laplacian(g, cv2.CV_64F).var()
    assert detail(out) < detail(img) * 0.02   # >98% of stroke detail destroyed
    outside = (slice(0, 200), slice(0, 200))
    assert np.array_equal(np.asarray(img)[outside], np.asarray(out)[outside])  # rest untouched


def test_blur_vehicle_keeps_originals_and_is_idempotent(tmp_path):
    spin = tmp_path / "TESTVIN" / "spin"
    spin.mkdir(parents=True)
    car_photo().save(spin / "spin_0001.jpg", quality=95)
    car_photo(with_plate=False).save(spin / "spin_0002.jpg", quality=95)
    rep = blur_vehicle(tmp_path, "TESTVIN", dirs=("spin",))
    assert rep["frames"] == 2 and rep["frames_with_plate"] == 1
    assert (spin / ".orig_unblurred" / "spin_0001.jpg").exists()
    blurred_once = np.asarray(Image.open(spin / "spin_0001.jpg"))
    rep2 = blur_vehicle(tmp_path, "TESTVIN", dirs=("spin",))  # re-run from the original
    assert rep2["frames_with_plate"] == 1
    assert np.abs(np.asarray(Image.open(spin / "spin_0001.jpg")).astype(int) - blurred_once).mean() < 2
    # original still has readable plate (high contrast)
    o = np.asarray(Image.open(spin / ".orig_unblurred" / "spin_0001.jpg").convert("L"), float)
    assert o[PLATE[1]:PLATE[3], PLATE[0]:PLATE[2]].std() > 30


def test_blur_plates_endpoint(client, app_module):
    register(client, "plates@example.com")
    vin = "1HGCM82633A004352"
    meta = {"year": "2019", "make": "Honda", "model": "Civic"}  # skip NHTSA lookup
    assert client.post("/api/create-vehicle", data={"vin": vin, **meta}).status_code == 200
    spin = app_module.STORAGE_DIR / vin / "spin"
    spin.mkdir(parents=True, exist_ok=True)
    car_photo().save(spin / "spin_0001.jpg")
    r = client.post(f"/api/vehicle/{vin}/blur-plates")
    assert r.status_code == 200, r.text
    assert r.json()["frames_with_plate"] == 1
    assert client.post("/api/vehicle/NOPE/blur-plates").status_code == 404


def test_plate_blur_changes_a_synthetic_plate_in_its_detection_region() -> None:
    from ai_studio.plate_blur import blur_license_plate

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


def test_plate_blur_with_status_reports_the_regions_it_blurred() -> None:
    from ai_studio.plate_blur import blur_license_plate_with_status

    out, info = blur_license_plate_with_status(car_photo())
    assert info["status"] == "blurred" and info["regions_blurred"] >= 1
    assert out.size == car_photo().size
    _, empty = blur_license_plate_with_status(car_photo(with_plate=False))
    assert empty == {"status": "no_candidate_detected", "regions_blurred": 0,
                     "method": "blackhat_char_verify"}
