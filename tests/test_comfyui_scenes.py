from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from urllib.request import Request as URLRequest

import pytest
from PIL import Image, ImageDraw
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
sys.path.insert(0, str(APP_DIR))

import main  # noqa: E402
from ai_studio import background_studio, upscale  # noqa: E402

VIN = "1HGCM82633A004352"


def _workflow_file(tmp_path: Path, *, valid: bool = True) -> Path:
    workflow = {
        "1": {
            "class_type": "LoadImage",
            "inputs": {"image": "__LOTVUE_INPUT_IMAGE__"},
        },
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "__LOTVUE_SCENE_PROMPT__"},
        },
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "__LOTVUE_NEGATIVE_PROMPT__"},
        },
        "4": {
            "class_type": "SaveImage",
            "inputs": {"images": ["1", 0], "filename_prefix": "__LOTVUE_OUTPUT_PREFIX__"},
        },
    }
    if not valid:
        workflow["1"]["inputs"]["image"] = "fixed-image.png"
    path = tmp_path / "scene-workflow.json"
    path.write_text(json.dumps(workflow), encoding="utf-8")
    return path


def _car_cutout(path: Path) -> None:
    image = Image.new("RGBA", (96, 64), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((12, 20, 84, 52), fill=(180, 45, 35, 255))
    image.save(path)
    image.close()


def _json_request(path: str, payload: dict) -> Request:
    body = json.dumps(payload).encode("utf-8")

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [],
            "client": ("test-client", 12345),
            "server": ("test-server", 80),
        },
        receive,
    )


def _configure_vehicle_composite_route(monkeypatch, source: Path, output_dir: Path) -> None:
    monkeypatch.setattr(main, "_resolve_vehicle_image_ref", lambda _ref: (VIN, source))
    monkeypatch.setattr(main, "_require_vehicle_owner", lambda _request, _vin: None)
    monkeypatch.setattr(
        main,
        "_ensure_safe_vehicle_subdir",
        lambda _vin, _name, create=False: output_dir,
    )


def test_comfyui_scene_uses_configured_workflow_and_cleans_staged_files(tmp_path, monkeypatch) -> None:
    source = tmp_path / "vehicle.png"
    _car_cutout(source)
    workflow_path = _workflow_file(tmp_path)
    input_dir = tmp_path / "comfy-input"
    output_dir = tmp_path / "comfy-output"
    destination = tmp_path / "vehicle_scene.jpg"
    monkeypatch.setenv("LOTVUE_COMFYUI_SCENE_WORKFLOW", str(workflow_path))
    monkeypatch.setenv("LOTVUE_COMFYUI_SCENE_TIMEOUT", "77")
    monkeypatch.setattr(upscale, "COMFY_INPUT_DIR", str(input_dir))
    monkeypatch.setattr(upscale, "COMFY_OUTPUT_DIR", str(output_dir))
    submitted = {}

    def fake_submit(workflow, timeout=120):
        submitted["workflow"] = workflow
        submitted["timeout"] = timeout
        image_name = workflow["1"]["inputs"]["image"]
        assert (input_dir / image_name).is_file()
        prefix = workflow["4"]["inputs"]["filename_prefix"]
        generated = output_dir / f"{prefix}_00001_.png"
        output_dir.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (48, 32), (180, 35, 150)).save(generated)
        return str(generated)

    monkeypatch.setattr(upscale, "submit_and_wait", fake_submit)

    result = background_studio.generate_scene_with_comfyui(
        str(source), "luxury-showroom", str(destination)
    )

    assert result == str(destination)
    assert submitted["timeout"] == 77
    graph = submitted["workflow"]
    assert graph["1"]["inputs"]["image"].endswith(".png")
    assert "Warm glass showroom setting" in graph["2"]["inputs"]["text"]
    assert graph["3"]["inputs"]["text"]
    prefix = graph["4"]["inputs"]["filename_prefix"]
    assert prefix.startswith("lotvue_scene_")
    assert "/" not in prefix and "\\" not in prefix
    assert not list(input_dir.glob("_lotvue_scene_*.png"))
    assert not list(output_dir.glob(f"{prefix}*"))
    with Image.open(destination) as result_image:
        assert result_image.size == (48, 32)
        r, g, b = result_image.convert("RGB").getpixel((10, 10))
        assert r > 140 and b > 110 and g < 70


def test_stage_image_atomically_updates_the_comfyui_input_copy(tmp_path, monkeypatch) -> None:
    source = tmp_path / "vehicle.png"
    source.write_bytes(b"new image")
    input_dir = tmp_path / "comfy-input"
    input_dir.mkdir()
    (input_dir / source.name).write_bytes(b"stale image")
    monkeypatch.setattr(upscale, "COMFY_INPUT_DIR", str(input_dir))

    staged_name = upscale.stage_image(str(source))

    assert staged_name == "vehicle.png"
    assert (input_dir / staged_name).read_bytes() == b"new image"
    assert not list(input_dir.glob(".lotvue-stage-*.tmp"))


def test_stage_image_replaces_a_symlink_without_touching_its_target(tmp_path, monkeypatch) -> None:
    source = tmp_path / "vehicle.png"
    source.write_bytes(b"safe input")
    input_dir = tmp_path / "comfy-input"
    input_dir.mkdir()
    protected = tmp_path / "protected.txt"
    protected.write_bytes(b"must remain unchanged")
    staged_link = input_dir / source.name
    try:
        staged_link.symlink_to(protected)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    monkeypatch.setattr(upscale, "COMFY_INPUT_DIR", str(input_dir))

    upscale.stage_image(str(source))

    assert not staged_link.is_symlink()
    assert staged_link.read_bytes() == b"safe input"
    assert protected.read_bytes() == b"must remain unchanged"


def test_comfyui_scene_requires_explicit_workflow_and_never_falls_back(tmp_path, monkeypatch) -> None:
    source = tmp_path / "vehicle.png"
    _car_cutout(source)
    destination = tmp_path / "vehicle_scene.jpg"
    monkeypatch.delenv("LOTVUE_COMFYUI_SCENE_WORKFLOW", raising=False)

    def forbidden_local_fallback(*_args, **_kwargs):
        raise AssertionError("AI mode must not silently return a local composite")

    monkeypatch.setattr(background_studio, "composite_on_scene", forbidden_local_fallback)
    with pytest.raises(background_studio.ComfyUIWorkflowConfigurationError):
        background_studio.generate_scene_with_comfyui(
            str(source), "clean-studio", str(destination)
        )
    assert not destination.exists()


def test_comfyui_scene_rejects_targets_inside_managed_output_root(tmp_path, monkeypatch) -> None:
    source = tmp_path / "vehicle.png"
    _car_cutout(source)
    workflow_path = _workflow_file(tmp_path)
    input_dir = tmp_path / "comfy-input"
    output_dir = tmp_path / "comfy-output"
    monkeypatch.setenv("LOTVUE_COMFYUI_SCENE_WORKFLOW", str(workflow_path))
    monkeypatch.setattr(upscale, "COMFY_INPUT_DIR", str(input_dir))
    monkeypatch.setattr(upscale, "COMFY_OUTPUT_DIR", str(output_dir))

    with pytest.raises(ValueError, match="outside its managed input/output folders"):
        background_studio.generate_scene_with_comfyui(
            str(source), "clean-studio", str(output_dir / "published.jpg")
        )

    assert not output_dir.exists()


def test_invalid_comfyui_workflow_is_reported_as_unconfigured(tmp_path, monkeypatch) -> None:
    path = _workflow_file(tmp_path, valid=False)
    monkeypatch.setenv("LOTVUE_COMFYUI_SCENE_WORKFLOW", str(path))

    assert background_studio.comfyui_scene_workflow_configured() is False
    with pytest.raises(background_studio.ComfyUIWorkflowConfigurationError):
        background_studio.generate_scene_with_comfyui(
            str(tmp_path / "does-not-need-to-exist.png"), "clean-studio"
        )


def test_system_health_validates_scene_workflow_without_external_probe(tmp_path, monkeypatch) -> None:
    import studio_agent

    workflow_path = _workflow_file(tmp_path)
    monkeypatch.setenv("LOTVUE_COMFYUI_SCENE_WORKFLOW", str(workflow_path))

    snapshot = studio_agent.build_system_health(main)

    assert snapshot["external_services_contacted"] is False
    assert snapshot["subsystems"]["comfyui_scene_generation"]["status"] == "configured"
    assert snapshot["runtime"]["comfyui_scene_workflow_configured"] is True


def test_local_compositor_requires_a_transparent_cutout(tmp_path) -> None:
    opaque_photo = tmp_path / "opaque.jpg"
    Image.new("RGB", (48, 32), (60, 90, 120)).save(opaque_photo)

    with pytest.raises(ValueError, match="transparent vehicle cut-out"):
        background_studio.composite_on_scene(str(opaque_photo), "clean-studio")


def test_comfyui_submit_and_wait_resolves_single_output_under_configured_root(tmp_path, monkeypatch) -> None:
    output_root = tmp_path / "comfy-output"
    output_file = output_root / "batch" / "scene_00001_.png"
    output_file.parent.mkdir(parents=True)
    Image.new("RGB", (12, 8), (10, 20, 30)).save(output_file)
    monkeypatch.setattr(upscale, "COMFY", "http://comfy.test")
    monkeypatch.setattr(upscale, "COMFY_OUTPUT_DIR", str(output_root))
    monkeypatch.setattr(upscale.time, "sleep", lambda _seconds: None)
    replies = [
        {"prompt_id": "test-prompt-1"},
        {
            "test-prompt-1": {
                "status": {"completed": True, "status_str": "success"},
                "outputs": {
                    "4": {
                        "images": [
                            {"filename": "scene_00001_.png", "subfolder": "batch", "type": "output"}
                        ]
                    }
                },
            }
        },
    ]
    requests = []

    class _Response:
        def __init__(self, payload: dict):
            self.payload = json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit=-1):
            return self.payload if limit < 0 else self.payload[:limit]

    def fake_urlopen(request, timeout=30):
        requests.append(request)
        assert timeout <= 30
        return _Response(replies.pop(0))

    monkeypatch.setattr(upscale.urllib.request, "urlopen", fake_urlopen)
    result = upscale.submit_and_wait(
        {"1": {"class_type": "SaveImage", "inputs": {}}}, poll_interval=0.05
    )

    assert Path(result) == output_file.resolve()
    assert len(requests) == 2
    assert isinstance(requests[0], URLRequest)
    assert requests[0].full_url == "http://comfy.test/prompt"
    assert requests[1].full_url == "http://comfy.test/history/test-prompt-1"


def test_comfyui_submit_and_wait_rejects_traversal_output(tmp_path, monkeypatch) -> None:
    output_root = tmp_path / "comfy-output"
    output_root.mkdir()
    monkeypatch.setattr(upscale, "COMFY", "http://comfy.test")
    monkeypatch.setattr(upscale, "COMFY_OUTPUT_DIR", str(output_root))
    monkeypatch.setattr(upscale.time, "sleep", lambda _seconds: None)
    replies = [
        {"prompt_id": "test-prompt-2"},
        {
            "test-prompt-2": {
                "status": {"completed": True, "status_str": "success"},
                "outputs": {
                    "4": {
                        "images": [
                            {"filename": "../outside.png", "subfolder": "", "type": "output"}
                        ]
                    }
                },
            }
        },
    ]

    class _Response:
        def __init__(self, payload: dict):
            self.payload = json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit=-1):
            return self.payload if limit < 0 else self.payload[:limit]

    def fake_urlopen(_request, timeout=30):
        return _Response(replies.pop(0))

    monkeypatch.setattr(upscale.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="invalid output path"):
        upscale.submit_and_wait(
            {"1": {"class_type": "SaveImage", "inputs": {}}}, poll_interval=0.05
        )


def test_comfyui_submit_and_wait_requires_one_output_and_bounds_timeout(tmp_path, monkeypatch) -> None:
    output_root = tmp_path / "comfy-output"
    output_root.mkdir()
    monkeypatch.setattr(upscale, "COMFY", "http://comfy.test")
    monkeypatch.setattr(upscale, "COMFY_OUTPUT_DIR", str(output_root))
    monkeypatch.setattr(upscale.time, "sleep", lambda _seconds: None)
    replies = [
        {"prompt_id": "test-prompt-3"},
        {
            "test-prompt-3": {
                "status": {"completed": True, "status_str": "success"},
                "outputs": {
                    "4": {
                        "images": [
                            {"filename": "one.png", "subfolder": "", "type": "output"},
                            {"filename": "two.png", "subfolder": "", "type": "output"},
                        ]
                    }
                },
            }
        },
    ]

    class _Response:
        def __init__(self, payload: dict):
            self.payload = json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit=-1):
            return self.payload if limit < 0 else self.payload[:limit]

    def fake_urlopen(_request, timeout=30):
        return _Response(replies.pop(0))

    monkeypatch.setattr(upscale.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="exactly one output image"):
        upscale.submit_and_wait(
            {"1": {"class_type": "SaveImage", "inputs": {}}}, poll_interval=0.05
        )
    with pytest.raises(ValueError, match="between 1 and 3600"):
        upscale.submit_and_wait({}, timeout=3601)


def test_comfyui_submit_and_wait_enforces_elapsed_timeout(monkeypatch) -> None:
    monkeypatch.setattr(upscale, "COMFY", "http://comfy.test")
    clock = [0.0]
    monkeypatch.setattr(upscale.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        upscale.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    replies = [
        {"prompt_id": "slow-prompt"},
        {"slow-prompt": {"status": {"status_str": "running"}, "outputs": {}}},
    ]

    class _Response:
        def __init__(self, payload: dict):
            self.payload = json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit=-1):
            return self.payload if limit < 0 else self.payload[:limit]

    requests = []

    def fake_urlopen(request, timeout=30):
        requests.append(request)
        return _Response(replies.pop(0))

    monkeypatch.setattr(upscale.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(TimeoutError, match="timed out after 1s"):
        upscale.submit_and_wait({}, timeout=1, poll_interval=0.5)

    assert len(requests) == 2
    assert clock[0] == 1.0


def test_photo_composite_route_explicitly_selects_comfyui_and_labels_result(tmp_path, monkeypatch) -> None:
    source = tmp_path / "vehicle.png"
    _car_cutout(source)
    output_dir = tmp_path / "studio"
    output_dir.mkdir()
    local_result = output_dir / "vehicle_clean-studio.jpg"
    local_result.write_bytes(b"previous deterministic result")
    called = {}
    _configure_vehicle_composite_route(monkeypatch, source, output_dir)

    def fake_generate(image_path, scene_id, output_path):
        called["args"] = (image_path, scene_id)
        Image.new("RGB", (24, 16), (130, 45, 150)).save(output_path)
        return output_path

    monkeypatch.setattr(background_studio, "generate_scene_with_comfyui", fake_generate)
    request = _json_request(
        "/api/photo/composite",
        {
            "image_path": f"/storage/{VIN}/exterior/vehicle.png",
            "scene": "clean-studio",
            "engine": "comfyui",
        },
    )
    result = asyncio.run(main.composite_endpoint(request))

    assert called["args"] == (str(source), "clean-studio")
    assert result == {
        "status": "ok",
        "engine": "comfyui_workflow",
        "ai_generated": True,
        "review_required": True,
        "output": f"/storage/{VIN}/studio/vehicle_clean-studio_comfyui.jpg",
    }
    assert (output_dir / "vehicle_clean-studio_comfyui.jpg").is_file()
    assert local_result.read_bytes() == b"previous deterministic result"


def test_photo_composite_route_fails_closed_without_scene_workflow(tmp_path, monkeypatch) -> None:
    source = tmp_path / "vehicle.png"
    _car_cutout(source)
    output_dir = tmp_path / "studio"
    output_dir.mkdir()
    _configure_vehicle_composite_route(monkeypatch, source, output_dir)

    def missing_workflow(*_args, **_kwargs):
        raise background_studio.ComfyUIWorkflowConfigurationError("workflow missing")

    monkeypatch.setattr(background_studio, "generate_scene_with_comfyui", missing_workflow)
    request = _json_request(
        "/api/photo/composite",
        {"image_path": f"/storage/{VIN}/exterior/vehicle.png", "engine": "comfyui"},
    )
    with pytest.raises(main.HTTPException) as error:
        asyncio.run(main.composite_endpoint(request))

    assert error.value.status_code == 503
    assert not (output_dir / "vehicle_clean-studio_comfyui.jpg").exists()
    assert main._COMFYUI_SCENE_JOB_SLOTS.acquire(blocking=False)
    main._COMFYUI_SCENE_JOB_SLOTS.release()


def test_photo_composite_route_bounds_concurrent_comfyui_jobs(tmp_path, monkeypatch) -> None:
    source = tmp_path / "vehicle.png"
    _car_cutout(source)
    output_dir = tmp_path / "studio"
    output_dir.mkdir()
    _configure_vehicle_composite_route(monkeypatch, source, output_dir)
    assert main._COMFYUI_SCENE_JOB_SLOTS.acquire(blocking=False)

    try:
        request = _json_request(
            "/api/photo/composite",
            {"image_path": f"/storage/{VIN}/exterior/vehicle.png", "engine": "comfyui"},
        )
        with pytest.raises(main.HTTPException) as error:
            asyncio.run(main.composite_endpoint(request))
    finally:
        main._COMFYUI_SCENE_JOB_SLOTS.release()

    assert error.value.status_code == 429
    assert error.value.headers["Retry-After"] == "30"
    assert not (output_dir / "vehicle_clean-studio_comfyui.jpg").exists()


def test_photo_composite_local_route_rejects_opaque_photo(tmp_path, monkeypatch) -> None:
    source = tmp_path / "vehicle.jpg"
    Image.new("RGB", (48, 32), (60, 90, 120)).save(source)
    output_dir = tmp_path / "studio"
    output_dir.mkdir()
    _configure_vehicle_composite_route(monkeypatch, source, output_dir)
    request = _json_request(
        "/api/photo/composite",
        {"image_path": f"/storage/{VIN}/exterior/vehicle.jpg", "engine": "local"},
    )

    with pytest.raises(main.HTTPException) as error:
        asyncio.run(main.composite_endpoint(request))

    assert error.value.status_code == 422
    assert not (output_dir / "vehicle_clean-studio.jpg").exists()
