"""
LotVue Pro — License Plate Blur
Detects and blurs license plates in vehicle photos.
Uses OpenCV cascade classifier or falls back to region-based blur.
"""
import numpy as np
from PIL import Image, ImageFilter


def blur_license_plate(img: Image.Image) -> Image.Image:
    """
    Detect and blur license plate regions in the image.
    Uses a heuristic approach: looks for rectangular regions in the
    lower-center portion of the car where plates typically are.
    Falls back gracefully if no plate is detected.
    """
    try:
        import cv2

        # Convert to OpenCV format
        img_array = np.array(img.convert("RGB"))
        gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)

        h, w = gray.shape

        # Region of interest: bottom 40% of image, center 60% width
        roi_top = int(h * 0.55)
        roi_left = int(w * 0.2)
        roi_right = int(w * 0.8)
        roi = gray[roi_top:, roi_left:roi_right]

        # Try Haar cascade for license plates
        plate_found = False
        try:
            plate_cascade = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_russian_plate_number.xml"
            )
            plates = plate_cascade.detectMultiScale(roi, scaleFactor=1.1, minNeighbors=3, minSize=(60, 20))

            for (px, py, pw, ph) in plates:
                # Map ROI coordinates back to full image
                abs_x = roi_left + px
                abs_y = roi_top + py

                # Add padding
                pad = 8
                x1 = max(0, abs_x - pad)
                y1 = max(0, abs_y - pad)
                x2 = min(w, abs_x + pw + pad)
                y2 = min(h, abs_y + ph + pad)

                # Blur the plate region
                plate_region = img_array[y1:y2, x1:x2]
                blurred = cv2.GaussianBlur(plate_region, (51, 51), 30)
                img_array[y1:y2, x1:x2] = blurred
                plate_found = True
        except Exception:
            pass

        # Fallback: edge detection to find rectangular plate-like regions
        if not plate_found:
            edges = cv2.Canny(roi, 100, 200)
            contours, _ = cv2.findContours(edges, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

            for cnt in contours:
                area = cv2.contourArea(cnt)
                if area < 500 or area > 15000:
                    continue
                peri = cv2.arcLength(cnt, True)
                approx = cv2.approxPolyDP(cnt, 0.04 * peri, True)

                if len(approx) == 4:
                    x, y, cw, ch = cv2.boundingRect(approx)
                    aspect = cw / max(ch, 1)
                    # License plates are typically 2:1 to 4:1 aspect ratio
                    if 1.5 < aspect < 5.0 and ch > 15:
                        abs_x = roi_left + x
                        abs_y = roi_top + y
                        pad = 5
                        x1 = max(0, abs_x - pad)
                        y1 = max(0, abs_y - pad)
                        x2 = min(w, abs_x + cw + pad)
                        y2 = min(h, abs_y + ch + pad)

                        plate_region = img_array[y1:y2, x1:x2]
                        blurred = cv2.GaussianBlur(plate_region, (51, 51), 30)
                        img_array[y1:y2, x1:x2] = blurred
                        plate_found = True
                        break  # Only blur first plate found

        return Image.fromarray(img_array)

    except ImportError:
        # OpenCV not available — skip plate blur
        print("[Plate Blur] OpenCV not available, skipping plate blur")
        return img
    except Exception as e:
        print(f"[Plate Blur] Error: {e}")
        return img
