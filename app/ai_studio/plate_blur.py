"""Heuristic license-plate blur (not a guaranteed anonymization detector)."""
from __future__ import annotations

import numpy as np
from PIL import Image


def blur_license_plate_with_status(img: Image.Image) -> tuple[Image.Image, dict]:
    """Attempt to blur plate-like regions and report what the heuristic found."""
    try:
        import cv2

        img_array = np.array(img.convert("RGB"))
        gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)
        height, width = gray.shape
        roi_top = int(height * 0.55)
        roi_left = int(width * 0.2)
        roi = gray[roi_top:, roi_left:int(width * 0.8)]
        blurred_regions = 0

        try:
            cascade = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_russian_plate_number.xml"
            )
            plates = cascade.detectMultiScale(
                roi, scaleFactor=1.1, minNeighbors=3, minSize=(60, 20)
            )
            for px, py, plate_w, plate_h in plates:
                x1 = max(0, roi_left + px - 8)
                y1 = max(0, roi_top + py - 8)
                x2 = min(width, roi_left + px + plate_w + 8)
                y2 = min(height, roi_top + py + plate_h + 8)
                if x2 > x1 and y2 > y1:
                    img_array[y1:y2, x1:x2] = cv2.GaussianBlur(
                        img_array[y1:y2, x1:x2], (51, 51), 30
                    )
                    blurred_regions += 1
        except Exception:
            # Contour fallback below; the cascade is optional and heuristic.
            pass

        if blurred_regions == 0:
            edges = cv2.Canny(roi, 100, 200)
            contours, _ = cv2.findContours(
                edges, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE
            )
            for contour in contours:
                area = cv2.contourArea(contour)
                if area < 500 or area > 15_000:
                    continue
                perimeter = cv2.arcLength(contour, True)
                approx = cv2.approxPolyDP(contour, 0.04 * perimeter, True)
                if len(approx) != 4:
                    continue
                x, y, box_w, box_h = cv2.boundingRect(approx)
                aspect = box_w / max(box_h, 1)
                if not (1.5 < aspect < 5.0 and box_h > 15):
                    continue
                x1 = max(0, roi_left + x - 5)
                y1 = max(0, roi_top + y - 5)
                x2 = min(width, roi_left + x + box_w + 5)
                y2 = min(height, roi_top + y + box_h + 5)
                if x2 > x1 and y2 > y1:
                    img_array[y1:y2, x1:x2] = cv2.GaussianBlur(
                        img_array[y1:y2, x1:x2], (51, 51), 30
                    )
                    blurred_regions += 1
                    break

        return Image.fromarray(img_array), {
            "status": "blurred" if blurred_regions else "no_candidate_detected",
            "regions_blurred": blurred_regions,
            "method": "heuristic_roi_cascade_contour",
        }
    except ImportError:
        return img.copy(), {
            "status": "unavailable", "regions_blurred": 0,
            "method": "heuristic_roi_cascade_contour",
        }
    except Exception as exc:
        return img.copy(), {
            "status": "error", "regions_blurred": 0,
            "method": "heuristic_roi_cascade_contour",
            "error_type": type(exc).__name__,
        }


def blur_license_plate(img: Image.Image) -> Image.Image:
    """Backwards-compatible image-only API used by the synthetic microbenchmark."""
    return blur_license_plate_with_status(img)[0]
