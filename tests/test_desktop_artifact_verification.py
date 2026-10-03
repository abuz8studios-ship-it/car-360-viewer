"""Tests for the archive-level checks run before desktop artifacts are uploaded."""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from scripts.verify_desktop_artifact import (
    ArtifactVerificationError,
    verify_desktop_artifact,
)

BUILD_ID = "a" * 40
LICENSE_ID = "LGPL-2.1-or-later"
LICENSE_FILENAME = "LGPL-2.1.txt"


def _make_archive(
    directory: Path,
    platform_name: str,
    architecture: str,
    *,
    extra_files: dict[str, bytes] | None = None,
) -> Path:
    package_root = "LotVue360" if platform_name == "windows" else f"LotVue360-macOS-{architecture}"
    if platform_name == "windows":
        app_prefix = f"{package_root}/app"
        launcher = f"{package_root}/LotVue360.exe"
        installers = (
            f"{package_root}/Install-LotVue360.ps1",
            f"{package_root}/Install-LotVue360.cmd",
            f"{package_root}/Uninstall-LotVue360.ps1",
        )
        suffix = ".exe"
        archive_name = "LotVue360-windows-x64.zip"
    else:
        app_prefix = f"{package_root}/LotVue360.app/Contents/Resources/app"
        launcher = f"{package_root}/LotVue360.app/Contents/MacOS/LotVue360"
        installers = (
            f"{package_root}/install-lotvue.sh",
            f"{package_root}/Install-LotVue360.command",
        )
        suffix = ""
        archive_name = f"LotVue360-macOS-{architecture}.zip"

    ffmpeg = b"synthetic ffmpeg executable"
    ffprobe = b"synthetic ffprobe executable"
    ffmpeg_files = {
        "ffmpeg": f"ffmpeg{suffix}",
        "ffprobe": f"ffprobe{suffix}",
        "license": LICENSE_ID,
        "ffmpeg_sha256": hashlib.sha256(ffmpeg).hexdigest(),
        "ffprobe_sha256": hashlib.sha256(ffprobe).hexdigest(),
    }
    manifest = {
        "product": "LotVue360",
        "build_id": BUILD_ID,
        "platform": platform_name,
        "architecture": architecture,
        "python_constraints_file": "PYTHON-BUILD-CONSTRAINTS.txt",
        "bundled_engines": ["CPU background removal runtime", "FFmpeg", "FFprobe"],
        "ffmpeg_files": ffmpeg_files,
        "not_bundled": [
            "RemBG pretrained model weights (not selected by default)",
            "provider credentials, dealership accounts, and service approval",
        ],
        "pricing_approved": False,
        "signed": False,
        "notarized": False,
    }
    files = {
        f"{package_root}/BUILD-MANIFEST.json": (json.dumps(manifest) + "\n").encode(),
        f"{package_root}/README-INSTALL.txt": b"No rembg model is selected. This bundle is unsigned and not notarized.\n",
        f"{package_root}/INSTALLATION.md": b"Install guide\n",
        f"{package_root}/THIRD_PARTY_NOTICES.md": b"Third-party notices\n",
        f"{package_root}/PYTHON-BUILD-CONSTRAINTS.txt": b"requirements\n",
        f"{app_prefix}/main.py": b"# app\n",
        f"{app_prefix}/.env.example": b"# blank defaults\n",
        f"{app_prefix}/bin/ffmpeg{suffix}": ffmpeg,
        f"{app_prefix}/bin/ffprobe{suffix}": ffprobe,
        f"{app_prefix}/bin/FFMPEG-LICENSE.txt": f"Detected license: {LICENSE_ID}\n".encode(),
        f"{app_prefix}/bin/FFMPEG-BUILD-INFO.txt": b"--- ffmpeg -version ---\n",
        f"{app_prefix}/bin/LICENSE-FFMPEG-{LICENSE_FILENAME}": b"license text\n",
        launcher: b"synthetic frozen launcher",
    }
    files.update({path: b"installer" for path in installers})
    if extra_files:
        files.update(extra_files)

    directory.mkdir(parents=True, exist_ok=True)
    archive_path = directory / archive_name
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, contents in files.items():
            archive.writestr(name, contents)
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    archive_path.with_suffix(archive_path.suffix + ".sha256").write_text(
        f"{digest}  {archive_path.name}\n", encoding="ascii",
    )
    return archive_path


@pytest.mark.parametrize(
    ("platform_name", "architecture"),
    [("windows", "x64"), ("macos", "x64"), ("macos", "arm64")],
)
def test_verifies_supported_platform_archive_and_notices(
    tmp_path: Path,
    platform_name: str,
    architecture: str,
) -> None:
    archive_path = _make_archive(tmp_path, platform_name, architecture)

    summary = verify_desktop_artifact(
        archive_path,
        platform_name=platform_name,
        architecture=architecture,
        expected_build_id=BUILD_ID,
    )

    assert summary["platform"] == platform_name
    assert summary["architecture"] == architecture
    assert summary["build_id"] == BUILD_ID
    assert summary["ffmpeg_license"] == LICENSE_ID
    assert summary["sha256"] == hashlib.sha256(archive_path.read_bytes()).hexdigest()


def test_rejects_mismatched_checksum_sidecar(tmp_path: Path) -> None:
    archive_path = _make_archive(tmp_path, "windows", "x64")
    archive_path.with_suffix(archive_path.suffix + ".sha256").write_text(
        f"{'0' * 64}  {archive_path.name}\n", encoding="ascii",
    )

    with pytest.raises(ArtifactVerificationError, match="does not match"):
        verify_desktop_artifact(
            archive_path,
            platform_name="windows",
            architecture="x64",
            expected_build_id=BUILD_ID,
        )


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "LotVue360/app/.env",
        "LotVue360/app/.env.local",
        "LotVue360/app/.jwt_secret",
        "LotVue360/app/private.pem",
        "LotVue360/app/data/vehicles.db-wal",
        "LotVue360/app/models/rembg.onnx",
        "LotVue360/models/rembg.onnx",
    ],
)
def test_rejects_secret_or_model_files_in_archive_payload(
    tmp_path: Path,
    unsafe_path: str,
) -> None:
    archive_path = _make_archive(
        tmp_path,
        "windows",
        "x64",
        extra_files={unsafe_path: b"must not ship"},
    )

    with pytest.raises(ArtifactVerificationError, match="must not be bundled|packaged"):
        verify_desktop_artifact(
            archive_path,
            platform_name="windows",
            architecture="x64",
            expected_build_id=BUILD_ID,
        )


def test_rejects_private_pem_key_material_but_allows_public_ca_bundle(tmp_path: Path) -> None:
    private_archive = _make_archive(
        tmp_path / "private",
        "windows",
        "x64",
        extra_files={
            "LotVue360/app/server.pem": b"-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----\n",
        },
    )
    with pytest.raises(ArtifactVerificationError, match="Private key material"):
        verify_desktop_artifact(
            private_archive,
            platform_name="windows",
            architecture="x64",
            expected_build_id=BUILD_ID,
        )

    public_archive = _make_archive(
        tmp_path / "public",
        "windows",
        "x64",
        extra_files={
            "LotVue360/_internal/certifi/cacert.pem": b"-----BEGIN CERTIFICATE-----\npublic CA cert\n-----END CERTIFICATE-----\n",
        },
    )
    summary = verify_desktop_artifact(
        public_archive,
        platform_name="windows",
        architecture="x64",
        expected_build_id=BUILD_ID,
    )
    assert summary["files"] > 0


def test_rejects_unsafe_archive_paths(tmp_path: Path) -> None:
    archive_path = _make_archive(
        tmp_path,
        "windows",
        "x64",
        extra_files={"LotVue360/../outside.txt": b"unsafe path"},
    )

    with pytest.raises(ArtifactVerificationError, match="Unsafe path"):
        verify_desktop_artifact(
            archive_path,
            platform_name="windows",
            architecture="x64",
            expected_build_id=BUILD_ID,
        )


def test_allows_dependency_code_modules_with_secret_in_their_module_name(tmp_path: Path) -> None:
    archive_path = _make_archive(
        tmp_path,
        "windows",
        "x64",
        extra_files={"LotVue360/_internal/stripe/apps/_secret_service.py": b"# dependency source"},
    )

    summary = verify_desktop_artifact(
        archive_path,
        platform_name="windows",
        architecture="x64",
        expected_build_id=BUILD_ID,
    )

    assert summary["files"] > 0


def test_rejects_wrong_build_identity(tmp_path: Path) -> None:
    archive_path = _make_archive(tmp_path, "macos", "arm64")

    with pytest.raises(ArtifactVerificationError, match="build_id"):
        verify_desktop_artifact(
            archive_path,
            platform_name="macos",
            architecture="arm64",
            expected_build_id="b" * 40,
        )
