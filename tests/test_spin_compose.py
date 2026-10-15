"""Cut-out-first studio spin: stable size/ground line, no double exposures."""
import io
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import spin_compose as sc  # noqa: E402


def _car(width, height, size=(1200, 800), offset=(0, 0), speck=False):
    """Synthetic 'cut-out': an opaque car body on a transparent photo."""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x = (size[0] - width) // 2 + offset[0]
    y = (size[1] - height) // 2 + offset[1]
    d.rounded_rectangle((x, y, x + width, y + height), radius=20, fill=(180, 30, 30, 255))
    if speck:
        d.rectangle((10, 10, 40, 40), fill=(20, 20, 20, 255))  # other parked car / noise
    return img


def _remove_fn(cut: Image.Image):
    def remove(_png: bytes) -> bytes:
        buffer = io.BytesIO()
        cut.save(buffer, "PNG")
        return buffer.getvalue()
    return remove


def test_cut_out_drops_detached_specks_outside_the_car(tmp_path):
    src = tmp_path / "frame_0001.jpg"
    Image.new("RGB", (1200, 800), "gray").save(src)
    rgba = sc.cut_out(src, _remove_fn(_car(600, 250, speck=True)))
    assert sc.tight_bbox(rgba)[0] > 100   # the speck at (10..40) is gone


def test_every_view_lands_at_one_height_on_one_ground_line():
    views = [_car(700, 240, offset=(-150, 60)), _car(300, 260, offset=(90, -40)),
             _car(520, 200)]
    arrays = [np.array(v) for v in views]
    height = sc.target_height([sc.tight_bbox(a) for a in arrays])
    aligned = [sc.normalize(a, height) for a in arrays]
    boxes = [sc.tight_bbox(a) for a in aligned]
    assert {b[3] for b in boxes} == {sc.GROUND_Y}
    assert {b[3] - b[1] for b in boxes} == {height}
    assert all(abs((b[0] + b[2]) / 2 - sc.CANVAS_W / 2) <= 1 for b in boxes)


def test_spin_has_fixed_frame_count_and_stays_on_the_ground_line():
    arrays = [np.array(_car(w, 240)) for w in (700, 520, 300, 520)]
    height = sc.target_height([sc.tight_bbox(a) for a in arrays])
    aligned = [sc.normalize(a, height) for a in arrays]
    frames = list(sc.spin_frames(aligned, 36))
    report = sc.jitter_report(frames)
    assert report["frames"] == 36
    assert report["ground_line_spread_px"] <= 4


def test_unmorphable_neighbours_cut_cleanly_instead_of_double_exposing():
    wide = sc.normalize(np.array(_car(900, 200)), 200)
    narrow = sc.normalize(np.array(_car(220, 200)), 200)
    assert sc.silhouette_iou(wide, narrow) < sc.MORPH_MIN_IOU
    frames = list(sc.spin_frames([wide, narrow], 8))
    widths = []
    for frame in frames:   # one solid car per frame: no half-transparent pixels
        alpha = frame[..., 3]
        assert not ((alpha > 40) & (alpha < 215)).sum() > 0.02 * (alpha > 0).sum()
        x0, _, x1, _ = sc.tight_bbox(frame)
        widths.append(x1 - x0)
    assert widths[:5] == sorted(widths[:5], reverse=True)  # wide -> narrow, smoothly


def test_cross_fade_is_short():
    assert sc.blend_weight(0.25) == 0.0 and sc.blend_weight(0.75) == 1.0
    assert 0.0 < sc.blend_weight(0.5) < 1.0
