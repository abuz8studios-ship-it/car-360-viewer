"""Opt-in rembg adapter; never loads an unapproved model implicitly.

The rembg Python package and model weights have different licenses. A model is
used only when an operator explicitly selects it and records that they reviewed
its terms. This flag is a guardrail, not automated legal verification.

Desktop releases bundle the owner-approved models (isnet-general-use, with
u2net as LOTVUE_REMBG_FALLBACK_MODEL) under app/models/rembg; rembg is pointed
there so a fresh install works offline and never downloads weights.
"""
from __future__ import annotations

import importlib.util
import os
import threading
from pathlib import Path
from typing import Any

MODEL_ENV = "LOTVUE_REMBG_MODEL"
FALLBACK_ENV = "LOTVUE_REMBG_FALLBACK_MODEL"
ACK_ENV = "LOTVUE_REMBG_LICENSE_ACCEPTED"
MODEL_DIR_ENV = "LOTVUE_REMBG_MODEL_DIR"
BUNDLED_MODEL_DIR = Path(__file__).resolve().parent / "models" / "rembg"
_LOCK = threading.Lock()
_SESSION: Any = None

# ONNX Runtime 1.30 on Linux uploads device info to Microsoft
# (mobile.events.data.microsoft.com) from every inference session. A local dealer
# tool must not phone home, and the switch only works if set before onnxruntime is
# imported, which every rembg/onnxruntime import in the app does lazily below.
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
_SESSION_MODEL = ""
_ACTIVE_MODEL = ""


class ModelNotConfiguredError(RuntimeError):
    """No operator-approved background-removal model is configured."""


def configured_model() -> str:
    return os.getenv(MODEL_ENV, "").strip()


def package_available() -> bool:
    try:
        return importlib.util.find_spec("rembg") is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def is_enabled() -> bool:
    """Require runtime package, explicit model selection, and owner acknowledgement."""
    return bool(
        package_available()
        and configured_model()
        and os.getenv(ACK_ENV, "").strip() == "1"
    )


def fallback_model() -> str:
    return os.getenv(FALLBACK_ENV, "").strip()


def active_model() -> str:
    """The model actually loaded (the fallback if the primary failed), or ''."""
    return _ACTIVE_MODEL


def use_bundled_models() -> Path | None:
    """Point rembg at the packaged weights unless U2NET_HOME is already chosen."""
    if os.getenv("U2NET_HOME"):
        return None
    directory = Path(os.getenv(MODEL_DIR_ENV) or BUNDLED_MODEL_DIR)
    if directory.is_dir() and any(directory.glob("*.onnx")):
        os.environ["U2NET_HOME"] = str(directory)
        return directory
    return None


def require_session():
    """Create/cache one session only after explicit model and license acknowledgement."""
    global _SESSION, _SESSION_MODEL, _ACTIVE_MODEL
    model = configured_model()
    if not model:
        raise ModelNotConfiguredError(
            f"No model selected. Set {MODEL_ENV} only after verifying its commercial-use and redistribution terms."
        )
    if os.getenv(ACK_ENV, "").strip() != "1":
        raise ModelNotConfiguredError(
            f"Model inference is disabled. Explicitly acknowledge review of the selected model's terms, then set {ACK_ENV}=1."
        )
    if not package_available():
        raise ModuleNotFoundError("The optional rembg runtime is not installed")

    with _LOCK:
        if _SESSION is None or _SESSION_MODEL != model:
            from rembg import new_session

            use_bundled_models()
            providers = ["CPUExecutionProvider"]
            try:
                import onnxruntime
                available = onnxruntime.get_available_providers()
                if "CUDAExecutionProvider" in available:
                    providers.insert(0, "CUDAExecutionProvider")
            except Exception:
                pass
            candidates = [model]
            if fallback_model() and fallback_model() != model:
                candidates.append(fallback_model())
            last_error: Exception | None = None
            for candidate in candidates:
                try:
                    session = new_session(candidate, providers=providers)
                except Exception as exc:  # missing/corrupt weights, unknown name, ORT load error
                    last_error = exc
                    print(f"[rembg] model {candidate!r} failed to load: {type(exc).__name__}: {exc}")
                    continue
                if candidate != model:
                    print(f"[rembg] using fallback model {candidate!r} (primary {model!r} unavailable)")
                _SESSION, _SESSION_MODEL, _ACTIVE_MODEL = session, model, candidate
                break
            else:
                raise last_error  # type: ignore[misc]
    return _SESSION


def remove(image, *args, session=None, **kwargs):
    """Run rembg with the selected, explicitly enabled model; never its default."""
    from rembg import remove as rembg_remove

    active_session = session if session is not None else require_session()
    return rembg_remove(image, *args, session=active_session, **kwargs)
