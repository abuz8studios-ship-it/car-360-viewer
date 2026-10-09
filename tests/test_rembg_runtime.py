from __future__ import annotations

import asyncio
import sys
import types
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from starlette.datastructures import UploadFile

sys.path.insert(0, "app")

import rembg_runtime


def _reset_session(monkeypatch) -> None:
    monkeypatch.setattr(rembg_runtime, "_SESSION", None)
    monkeypatch.setattr(rembg_runtime, "_SESSION_MODEL", "")


def test_model_is_disabled_without_explicit_selection_and_license_ack(monkeypatch) -> None:
    monkeypatch.setattr(rembg_runtime, "package_available", lambda: True)
    monkeypatch.delenv("LOTVUE_REMBG_MODEL", raising=False)
    monkeypatch.delenv("LOTVUE_REMBG_LICENSE_ACCEPTED", raising=False)
    _reset_session(monkeypatch)

    assert rembg_runtime.is_enabled() is False
    with pytest.raises(rembg_runtime.ModelNotConfiguredError, match="No model selected"):
        rembg_runtime.require_session()


def test_selected_model_stays_disabled_until_operator_acknowledges_terms(monkeypatch) -> None:
    monkeypatch.setattr(rembg_runtime, "package_available", lambda: True)
    monkeypatch.setenv("LOTVUE_REMBG_MODEL", "owner-approved-model")
    monkeypatch.setenv("LOTVUE_REMBG_LICENSE_ACCEPTED", "0")
    _reset_session(monkeypatch)

    assert rembg_runtime.is_enabled() is False
    with pytest.raises(rembg_runtime.ModelNotConfiguredError, match="acknowledge review"):
        rembg_runtime.require_session()


def test_enabled_model_uses_only_explicit_session_and_never_implicit_rembg_default(monkeypatch) -> None:
    _reset_session(monkeypatch)
    monkeypatch.setattr(rembg_runtime, "package_available", lambda: True)
    monkeypatch.setenv("LOTVUE_REMBG_MODEL", "owner-approved-model")
    monkeypatch.setenv("LOTVUE_REMBG_LICENSE_ACCEPTED", "1")
    sessions = []
    calls = []

    def new_session(model: str, *, providers: list[str]):
        session = {"model": model, "providers": providers}
        sessions.append(session)
        return session

    def remove(image, *args, session=None, **kwargs):
        calls.append((image, session, kwargs))
        return "cutout"

    monkeypatch.setitem(sys.modules, "rembg", types.SimpleNamespace(new_session=new_session, remove=remove))
    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        types.SimpleNamespace(get_available_providers=lambda: ["CPUExecutionProvider"]),
    )

    assert rembg_runtime.is_enabled() is True
    session = rembg_runtime.require_session()
    assert session["model"] == "owner-approved-model"
    assert session["providers"] == ["CPUExecutionProvider"]
    assert rembg_runtime.remove("image-bytes") == "cutout"
    assert sessions == [session]
    assert calls == [("image-bytes", session, {})]


def test_upload_route_returns_503_and_cleans_temp_file_if_model_is_unconfigured(tmp_path, monkeypatch) -> None:
    import main
    from ai_studio import background_studio

    monkeypatch.delenv("LOTVUE_REMBG_MODEL", raising=False)
    monkeypatch.delenv("LOTVUE_REMBG_LICENSE_ACCEPTED", raising=False)
    temp_dir = tmp_path / "background-request"

    def make_temp_dir(prefix: str) -> str:
        assert prefix == "lotvue-background-"
        temp_dir.mkdir()
        return str(temp_dir)

    monkeypatch.setattr("tempfile.mkdtemp", make_temp_dir)

    def no_approved_model(_path: str) -> str:
        raise rembg_runtime.ModelNotConfiguredError("no approved model configured")

    monkeypatch.setattr(background_studio, "remove_background", no_approved_model)
    image_stream = BytesIO()
    Image.new("RGB", (32, 24), (90, 110, 130)).save(image_stream, format="JPEG")
    upload = UploadFile(filename="sample.jpg", file=BytesIO(image_stream.getvalue()))

    with pytest.raises(main.HTTPException) as caught:
        asyncio.run(main.remove_bg_endpoint(upload))

    assert caught.value.status_code == 503
    assert "explicitly approved" in caught.value.detail
    assert not temp_dir.exists()
