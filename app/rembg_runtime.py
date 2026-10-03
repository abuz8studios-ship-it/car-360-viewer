"""Opt-in rembg adapter; never loads an unapproved model implicitly.

The rembg Python package and model weights have different licenses. A model is
used only when an operator explicitly selects it and records that they reviewed
its terms. This flag is a guardrail, not automated legal verification.
"""
from __future__ import annotations

import importlib.util
import os
import threading
from typing import Any

MODEL_ENV = "LOTVUE_REMBG_MODEL"
ACK_ENV = "LOTVUE_REMBG_LICENSE_ACCEPTED"
_LOCK = threading.Lock()
_SESSION: Any = None
_SESSION_MODEL = ""


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


def require_session():
    """Create/cache one session only after explicit model and license acknowledgement."""
    global _SESSION, _SESSION_MODEL
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

            providers = ["CPUExecutionProvider"]
            try:
                import onnxruntime
                available = onnxruntime.get_available_providers()
                if "CUDAExecutionProvider" in available:
                    providers.insert(0, "CUDAExecutionProvider")
            except Exception:
                pass
            _SESSION = new_session(model, providers=providers)
            _SESSION_MODEL = model
    return _SESSION


def remove(image, *args, session=None, **kwargs):
    """Run rembg with the selected, explicitly enabled model; never its default."""
    from rembg import remove as rembg_remove

    active_session = session if session is not None else require_session()
    return rembg_remove(image, *args, session=active_session, **kwargs)
