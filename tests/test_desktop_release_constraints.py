"""Regression tests for native-platform desktop dependency pins and staging filters."""
from __future__ import annotations

from pathlib import Path

import pytest
from packaging.markers import default_environment
from packaging.requirements import Requirement

from scripts.build_desktop import _ignore_app, _is_onnxruntime_test_data

ROOT = Path(__file__).resolve().parents[1]
PLATFORM_DEPENDENCIES = {
    "cryptography": {"Intel macOS": "48.0.1", "other": "50.0.2"},
    "llvmlite": {"Intel macOS": "0.45.1", "other": "0.50.0"},
    "numba": {"Intel macOS": "0.62.1", "other": "0.68.0"},
    "numpy": {"Intel macOS": "2.3.5", "other": "2.4.6"},
    "onnxruntime": {"Intel macOS": "1.23.2", "other": "1.30.0"},
}


@pytest.mark.parametrize(
    ("label", "sys_platform", "platform_machine", "use_intel_pins"),
    [
        ("Intel macOS", "darwin", "x86_64", True),
        ("Apple Silicon macOS", "darwin", "arm64", False),
        ("Windows x64", "win32", "AMD64", False),
        ("Linux x64", "linux", "x86_64", False),
    ],
)
def test_release_constraints_select_one_compatible_stack_per_platform(
    label: str,
    sys_platform: str,
    platform_machine: str,
    use_intel_pins: bool,
) -> None:
    environment = default_environment()
    environment.update(sys_platform=sys_platform, platform_machine=platform_machine)
    constraints = [
        Requirement(line.strip())
        for line in (ROOT / "requirements-release-constraints.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    for package, versions in PLATFORM_DEPENDENCIES.items():
        applicable = [
            requirement
            for requirement in constraints
            if requirement.name.lower() == package
            and (requirement.marker is None or requirement.marker.evaluate(environment))
        ]
        expected = versions["Intel macOS"] if use_intel_pins else versions["other"]
        assert len(applicable) == 1, f"{label}: expected one {package} pin, found {applicable}"
        assert str(applicable[0].specifier) == f"=={expected}", (
            f"{label}: unexpected {package} pin {applicable[0].specifier}"
        )


def test_desktop_staging_excludes_secrets_models_and_user_data_but_keeps_env_template() -> None:
    names = [
        ".env",
        ".env.local",
        "dealer.env",
        ".jwt_secret",
        "stripe_credentials.json",
        "private.pem",
        "signing.key",
        "vehicles.db",
        "custom.sqlite3",
        "desktop_launch.log",
        "stale_module.pyc",
        "rembg.onnx",
        "weights.safetensors",
        "storage",
        "vehicles",
        ".env.example",
        "main.py",
        "_secret_service.py",
        "credential_helpers.js",
    ]

    ignored = _ignore_app("app", names)

    allowed = {".env.example", "main.py", "_secret_service.py", "credential_helpers.js"}
    assert set(names) - allowed <= ignored
    assert allowed.isdisjoint(ignored)


def test_onnxruntime_test_graphs_are_excluded_without_exempting_real_model_weights() -> None:
    assert _is_onnxruntime_test_data(
        "/site-packages/onnxruntime/datasets/mul_1.onnx",
    )
    assert not _is_onnxruntime_test_data(
        "/site-packages/rembg/models/u2net.onnx",
    )
    assert not _is_onnxruntime_test_data(
        "/site-packages/onnxruntime/capi/onnxruntime_pybind11_state.so",
    )
