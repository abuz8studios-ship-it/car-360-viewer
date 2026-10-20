"""360 spin modes (AI / blended / real photos), photo order, and the ComfyUI bridge
driven end to end against a fake ComfyUI HTTP server."""
from __future__ import annotations

import io
import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import gaussian_3d
import wan_comfy_api

SHADES = (30, 90, 150, 210)


def _photos(directory: Path, names=("p_0001.jpg", "p_0002.jpg", "p_0003.jpg", "p_0004.jpg")) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, shade in zip(names, SHADES):
        path = directory / name
        Image.new("RGB", (640, 480), (shade, 80, 120)).save(path, "JPEG", quality=95)
        paths.append(path)
    return paths


def _red(path: Path) -> int:
    return Image.open(path).convert("RGB").getpixel((640, 427))[0]


@pytest.fixture()
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(gaussian_3d, "STORAGE_DIR", tmp_path / "storage")
    # no background-removal model in tests: build_spin letterboxes the photos instead
    monkeypatch.setattr(gaussian_3d, "_stabilize_frames", lambda *a, **k: False)
    monkeypatch.setattr(gaussian_3d, "_STATE", {})
    monkeypatch.setattr(gaussian_3d, "_STATUS", None)
    monkeypatch.setattr(gaussian_3d, "_VIN", "UNKNOWN")
    return tmp_path / "storage"


def test_upload_order_is_kept_unless_auto_sort_is_asked_for(storage, tmp_path, monkeypatch):
    frames = _photos(tmp_path / "in", names=("a.jpg", "b.jpg", "c.jpg", "d.jpg"))
    sorted_calls = []
    monkeypatch.setattr(gaussian_3d, "order_photos_by_angle",
                        lambda sel: sorted_calls.append(list(sel)) or (list(reversed(sel)), None))

    gaussian_3d.build_spin(frames, "VIN1", spin_mode="photos")
    spin = sorted((storage / "VIN1" / "spin").glob("spin_*.jpg"))
    assert sorted_calls == []
    assert abs(_red(spin[0]) - SHADES[0]) < 8

    gaussian_3d.build_spin(frames, "VIN1", spin_mode="photos", photo_order="auto")
    spin = sorted((storage / "VIN1" / "spin").glob("spin_*.jpg"))
    assert len(sorted_calls) == 1
    assert abs(_red(spin[0]) - SHADES[-1]) < 8


def test_photo_folder_is_read_in_name_order_across_extensions(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for name, shade in (("p_0001.png", 10), ("p_0002.jpg", 20), ("p_0003.jpeg", 30)):
        Image.new("RGB", (64, 64), (shade, 0, 0)).save(src / name, "PNG" if name.endswith("png") else "JPEG")
    copied = gaussian_3d.copy_photos(src, tmp_path / "out")
    reds = [Image.open(p).convert("RGB").getpixel((32, 32))[0] for p in copied]
    assert len(copied) == 3 and reds == sorted(reds)


def test_real_photos_only_holds_each_photo_with_nothing_invented(storage, tmp_path):
    frames = _photos(tmp_path / "in")

    count = gaussian_3d.build_spin(frames, "VIN2", spin_mode="photos")

    spin_dir = storage / "VIN2" / "spin"
    spin = sorted(spin_dir.glob("spin_*.jpg"))
    assert count == len(spin) == gaussian_3d.SPIN_DENSE
    assert json.loads((spin_dir / "manifest.json").read_text())["engine"] == "photos"
    assert not (spin_dir / "_keys").exists()
    # every frame is byte-identical to one of the four stabilized photos, in order
    distinct = []
    for frame in spin:
        data = frame.read_bytes()
        if not distinct or distinct[-1] != data:
            distinct.append(data)
    assert len(distinct) == 4
    assert [_red(spin[i * 9]) for i in range(4)] == pytest.approx(SHADES, abs=8)


def test_blended_spin_keeps_keyframes_for_the_ai_upgrade(storage, tmp_path):
    frames = _photos(tmp_path / "in")

    count = gaussian_3d.build_spin(frames, "VIN3", spin_mode="blend")

    spin_dir = storage / "VIN3" / "spin"
    assert count == gaussian_3d.SPIN_DENSE
    assert json.loads((spin_dir / "manifest.json").read_text())["engine"] == "optical-flow"
    assert len(list((spin_dir / "_keys").glob("key_*.jpg"))) == 4


class _FakeComfyUI(BaseHTTPRequestHandler):
    """Just enough of ComfyUI's HTTP API for the WAN first-last-frame path."""
    prompts: list = []
    jobs: dict = {}

    def log_message(self, *args):
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/system_stats":
            return self._json({"devices": [{"name": "fake NVIDIA"}]})
        if url.path == "/queue":
            return self._json({"queue_running": [], "queue_pending": []})
        if url.path.startswith("/history/"):
            prompt_id = url.path.rsplit("/", 1)[1]
            job = self.jobs.get(prompt_id)
            if not job:
                return self._json({})
            images = [{"filename": f"{job['prefix']}_{i + 1:05d}_.png", "subfolder": "", "type": "output"}
                      for i in range(job["length"])]
            return self._json({prompt_id: {"status": {"status_str": "success", "completed": True},
                                           "outputs": {"12": {"images": images}}}})
        if url.path == "/view":
            name = parse_qs(url.query)["filename"][0]
            index = int(name.rsplit("_", 2)[-2])
            buf = io.BytesIO()
            Image.new("RGB", (720, 480), (min(255, index * 10), 200, 40)).save(buf, "PNG")
            body = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return None
        return self._json({}, status=404)

    def do_POST(self):
        payload = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path == "/upload/image":
            return self._json({"name": f"upload_{len(self.prompts)}.jpg", "subfolder": "", "type": "input"})
        if self.path == "/prompt":
            workflow = json.loads(payload)["prompt"]
            prompt_id = f"job{len(self.prompts) + 1}"
            self.prompts.append(workflow)
            self.jobs[prompt_id] = {"prefix": workflow["12"]["inputs"]["filename_prefix"],
                                    "length": workflow["9"]["inputs"]["length"]}
            return self._json({"prompt_id": prompt_id, "number": len(self.prompts)})
        return self._json({}, status=404)


@pytest.fixture()
def fake_comfyui(tmp_path, monkeypatch):
    _FakeComfyUI.prompts = []
    _FakeComfyUI.jobs = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeComfyUI)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    output = tmp_path / "comfy_output"
    output.mkdir()
    monkeypatch.setattr(wan_comfy_api, "COMFYUI_URL", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setattr(wan_comfy_api, "COMFYUI_OUTPUT", str(output))
    monkeypatch.setattr(wan_comfy_api, "POLL_SECONDS", 0)
    try:
        yield types.SimpleNamespace(prompts=_FakeComfyUI.prompts, output=output)
    finally:
        server.shutdown()
        server.server_close()


def test_flf_frames_come_back_over_http_and_local_copies_are_removed(fake_comfyui, tmp_path, monkeypatch):
    start, end = _photos(tmp_path / "in")[:2]
    original_post = wan_comfy_api._post

    def post(path, payload, timeout=30):
        if path == "/prompt":  # a ComfyUI on this PC also keeps a copy in its output folder
            prefix = payload["prompt"]["12"]["inputs"]["filename_prefix"]
            for i in range(9):
                (fake_comfyui.output / f"{prefix}_{i + 1:05d}_.png").write_bytes(b"x")
        return original_post(path, payload, timeout)

    monkeypatch.setattr(wan_comfy_api, "_post", post)
    frames = wan_comfy_api.generate_wan_flf_frames(str(start), str(end), str(tmp_path / "arc"),
                                                    num_frames=9, log=lambda *_: None)

    assert len(frames) == 9
    assert Image.open(frames[0]).size == (720, 480)
    assert list(fake_comfyui.output.iterdir()) == []
    workflow = fake_comfyui.prompts[0]
    assert workflow["4"]["inputs"]["vae_name"] == "wan_2.1_vae.safetensors"
    assert workflow["1"]["inputs"]["unet_name"] == wan_comfy_api.FLF_UNET_HIGH
    assert workflow["1b"]["inputs"]["unet_name"] == wan_comfy_api.FLF_UNET_LOW


def test_ai_mode_replaces_the_blend_with_wan_frames(storage, tmp_path, fake_comfyui, monkeypatch):
    monkeypatch.setenv("LOTVUE_SPIN_GEN", "1")
    gaussian_3d.build_spin(_photos(tmp_path / "in"), "VIN4", spin_mode="ai")

    count = gaussian_3d.generative_upgrade("VIN4")

    spin_dir = storage / "VIN4" / "spin"
    manifest = json.loads((spin_dir / "manifest.json").read_text())
    assert count == manifest["count"] == len(list(spin_dir.glob("spin_*.jpg"))) >= 70
    assert manifest["engine"] == "wan-flf"
    assert len(fake_comfyui.prompts) == 4  # one generated arc between each pair of photos
    assert "spin_ai_skipped" not in gaussian_3d._STATE


def _run_args(images: Path, spin_mode: str):
    return types.SimpleNamespace(vin="vin5", video="", images=str(images), steps=2000, max_frames=150,
                                 style="", backdrop="", spin_mode=spin_mode, photo_order="upload")


def test_ai_mode_without_comfyui_keeps_the_blend_and_says_why(storage, tmp_path, monkeypatch):
    monkeypatch.setenv("LOTVUE_SPIN_GEN", "1")
    monkeypatch.setattr(wan_comfy_api, "comfyui_up", lambda timeout=6: False)
    monkeypatch.setattr(gaussian_3d, "export_mp4", lambda vin: None)
    images = tmp_path / "in"
    _photos(images)

    assert gaussian_3d.run(_run_args(images, "ai"))

    status = json.loads((storage / "VIN5" / "splat" / "status.json").read_text())
    manifest = json.loads((storage / "VIN5" / "spin" / "manifest.json").read_text())
    assert status["status"] == "done" and status["spin_ready"]
    assert manifest["engine"] == "optical-flow"
    assert "AI in-between frames were skipped" in status["note"]
    assert "ComfyUI is not running at" in status["note"]


def test_blend_and_photo_modes_never_call_comfyui(storage, tmp_path, monkeypatch):
    monkeypatch.setattr(gaussian_3d, "generative_upgrade",
                        lambda vin: pytest.fail("AI upgrade must not run in this mode"))
    monkeypatch.setattr(gaussian_3d, "export_mp4", lambda vin: None)
    images = tmp_path / "in"
    _photos(images)
    for mode, engine in (("blend", "optical-flow"), ("photos", "photos")):
        assert gaussian_3d.run(_run_args(images, mode))
        status = json.loads((storage / "VIN5" / "splat" / "status.json").read_text())
        manifest = json.loads((storage / "VIN5" / "spin" / "manifest.json").read_text())
        assert manifest["engine"] == engine
        assert "AI in-between" not in status["note"]
