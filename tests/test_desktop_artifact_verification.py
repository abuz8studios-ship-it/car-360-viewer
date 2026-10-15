"""Tests for the archive-level checks run before desktop artifacts are uploaded."""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

import scripts.verify_desktop_artifact as verifier
from scripts.verify_desktop_artifact import (
    ArtifactVerificationError,
    verify_desktop_artifact,
)

BUILD_ID = "a" * 40
LICENSE_ID = "LGPL-2.1-or-later"
LICENSE_FILENAME = "LGPL-2.1.txt"
# Small stand-ins for the pinned rembg models (the real files are ~175 MB each).
MODELS = {"isnet-general-use.onnx": b"synthetic isnet weights", "u2net.onnx": b"synthetic u2net weights"}


@pytest.fixture(autouse=True)
def _synthetic_model_pins(monkeypatch) -> None:
    monkeypatch.setattr(verifier, "BUNDLED_REMBG_MODELS", {
        name: {"sha256": hashlib.sha256(content).hexdigest()} for name, content in MODELS.items()
    })


def _make_archive(
    directory: Path,
    platform_name: str,
    architecture: str,
    *,
    extra_files: dict[str, bytes] | None = None,
    non_executable_paths: set[str] | None = None,
    installer_name_override: str | None = None,
    corrupt_source_archive_hash: bool = False,
    models: dict[str, bytes] | None = None,
    manifest_models: dict[str, bytes] | None = None,
) -> Path:
    models = MODELS if models is None else models
    manifest_models = models if manifest_models is None else manifest_models
    if platform_name == "windows":
        package_root = "LotVue360"
        app_prefix = f"{package_root}/app"
        launcher = f"{package_root}/LotVue360.exe"
        installers = (
            f"{package_root}/Install-LotVue360.ps1",
            f"{package_root}/Install-LotVue360.cmd",
            f"{package_root}/Uninstall-LotVue360.ps1",
        )
        suffix = ".exe"
        archive_name = "LotVue360-windows-x64.zip"
    elif platform_name == "macos":
        package_root = f"LotVue360-macOS-{architecture}"
        app_prefix = f"{package_root}/LotVue360.app/Contents/Resources/app"
        launcher = f"{package_root}/LotVue360.app/Contents/MacOS/LotVue360"
        installers = (
            f"{package_root}/install-lotvue.sh",
            f"{package_root}/Install-LotVue360.command",
        )
        suffix = ""
        archive_name = f"LotVue360-macOS-{architecture}.zip"
    else:
        package_root = "LotVue360"
        app_prefix = f"{package_root}/app"
        launcher = f"{package_root}/LotVue360"
        installers = (
            f"{package_root}/Install-LotVue360.sh",
            f"{package_root}/Uninstall-LotVue360.sh",
        )
        suffix = ""
        archive_name = f"LotVue360-linux-{architecture}.zip"

    ffmpeg = b"synthetic ffmpeg executable"
    ffprobe = b"synthetic ffprobe executable"
    license_id = "GPL-2.0-or-later" if platform_name == "linux" else LICENSE_ID
    license_filename = "GPL-2.0.txt" if platform_name == "linux" else LICENSE_FILENAME
    ffmpeg_files = {
        "ffmpeg": f"ffmpeg{suffix}",
        "ffprobe": f"ffprobe{suffix}",
        "license": license_id,
        "ffmpeg_sha256": hashlib.sha256(ffmpeg).hexdigest(),
        "ffprobe_sha256": hashlib.sha256(ffprobe).hexdigest(),
    }
    source_files: dict[str, bytes] = {}
    source_build_info: dict[str, object] = {}
    if platform_name == "linux":
        source_files = {
            "ffmpeg-8.0.tar.gz": b"synthetic pinned FFmpeg source archive",
            "x264-c24e06c2e184345ceb33eb20a15d1024d9fd3497.tar.gz": b"synthetic pinned x264 source archive",
        }
        source_hashes = {name: hashlib.sha256(contents).hexdigest() for name, contents in source_files.items()}
        if corrupt_source_archive_hash:
            source_hashes["ffmpeg-8.0.tar.gz"] = "0" * 64
        ffmpeg_files["source_archives"] = source_hashes
        source_build_info = {
            "ffmpeg_commit": "140fd653aed8cad774f991ba083e2d01e86420c7",
            "x264_commit": "c24e06c2e184345ceb33eb20a15d1024d9fd3497",
            "license": "GPL-2.0-or-later (FFmpeg built with GPL x264)",
            "source_archives": source_hashes,
        }
    one_click_installer = installer_name_override or {
        "windows": "LotVue360-OneClick-windows-x64.exe",
        "macos": f"LotVue360-OneClick-macOS-{architecture}.dmg",
        "linux": f"LotVue360-OneClick-linux-{architecture}.run",
    }[platform_name]
    manifest = {
        "product": "LotVue360",
        "build_id": BUILD_ID,
        "platform": platform_name,
        "architecture": architecture,
        "one_click_installer": one_click_installer,
        "python_constraints_file": "PYTHON-BUILD-CONSTRAINTS.txt",
        "bundled_engines": ["CPU background removal runtime", "FFmpeg", "FFprobe"],
        "ffmpeg_files": ffmpeg_files,
        "bundled_models": [
            {"name": name, "sha256": hashlib.sha256(content).hexdigest()}
            for name, content in manifest_models.items()
        ],
        "not_bundled": [
            "provider credentials, dealership accounts, and service approval",
        ],
        "pricing_approved": False,
        "signed": False,
        "notarized": False,
    }
    files = {
        f"{package_root}/BUILD-MANIFEST.json": (json.dumps(manifest) + "\n").encode(),
        f"{package_root}/README-INSTALL.txt": (
            b"Bundles isnet-general-use and u2net. This bundle is unsigned and not notarized.\n"),
        f"{package_root}/INSTALLATION.md": b"Install guide\n",
        f"{package_root}/THIRD_PARTY_NOTICES.md": b"Third-party notices\n",
        f"{package_root}/PYTHON-BUILD-CONSTRAINTS.txt": b"requirements\n",
        f"{app_prefix}/main.py": b"# app\n",
        f"{app_prefix}/.env.example": b"# blank defaults\n",
        f"{app_prefix}/bin/ffmpeg{suffix}": ffmpeg,
        f"{app_prefix}/bin/ffprobe{suffix}": ffprobe,
        f"{app_prefix}/bin/FFMPEG-LICENSE.txt": f"Detected license: {license_id}\n".encode(),
        f"{app_prefix}/bin/FFMPEG-BUILD-INFO.txt": b"--- ffmpeg -version ---\n",
        f"{app_prefix}/bin/LICENSE-FFMPEG-{license_filename}": b"license text\n",
        f"{app_prefix}/bin/LICENSE-PYTHON-PSF-2.0.txt": b"Python Software Foundation License\n",
        launcher: b"synthetic frozen launcher",
    }
    files.update({f"{app_prefix}/models/rembg/{name}": content for name, content in models.items()})
    if platform_name == "linux":
        files[f"{app_prefix}/bin/FFMPEG-SOURCE-BUILD-INFO.json"] = (
            json.dumps(source_build_info) + "\n"
        ).encode()
        files.update({f"{app_prefix}/bin/source/{name}": contents for name, contents in source_files.items()})
    files.update({path: b"installer" for path in installers})
    if extra_files:
        files.update(extra_files)

    directory.mkdir(parents=True, exist_ok=True)
    archive_path = directory / archive_name
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, contents in files.items():
            if platform_name == "linux" and name in {launcher, *installers}:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                permission = 0o644 if non_executable_paths and name in non_executable_paths else 0o755
                info.external_attr = (0o100000 | permission) << 16
                archive.writestr(info, contents)
            else:
                archive.writestr(name, contents)
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    archive_path.with_suffix(archive_path.suffix + ".sha256").write_text(
        f"{digest}  {archive_path.name}\n", encoding="ascii",
    )
    return archive_path


@pytest.mark.parametrize(
    ("platform_name", "architecture"),
    [("windows", "x64"), ("macos", "x64"), ("macos", "arm64"), ("linux", "x64")],
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
    expected_license = "GPL-2.0-or-later" if platform_name == "linux" else LICENSE_ID
    assert summary["ffmpeg_license"] == expected_license
    if platform_name == "linux":
        assert set(summary["ffmpeg_source_archives"]) == {
            "ffmpeg-8.0.tar.gz",
            "x264-c24e06c2e184345ceb33eb20a15d1024d9fd3497.tar.gz",
        }
    assert summary["sha256"] == hashlib.sha256(archive_path.read_bytes()).hexdigest()


def test_rejects_linux_archive_without_executable_installer_bits(tmp_path: Path) -> None:
    script = "LotVue360/Install-LotVue360.sh"
    archive_path = _make_archive(
        tmp_path,
        "linux",
        "x64",
        non_executable_paths={script},
    )

    with pytest.raises(ArtifactVerificationError, match="not marked executable"):
        verify_desktop_artifact(
            archive_path,
            platform_name="linux",
            architecture="x64",
            expected_build_id=BUILD_ID,
        )


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


def test_rejects_wrong_one_click_installer_name_in_manifest(tmp_path: Path) -> None:
    archive_path = _make_archive(
        tmp_path,
        "linux",
        "x64",
        installer_name_override="LotVue360-linux.zip",
    )

    with pytest.raises(ArtifactVerificationError, match="one_click_installer"):
        verify_desktop_artifact(
            archive_path,
            platform_name="linux",
            architecture="x64",
            expected_build_id=BUILD_ID,
        )


def test_rejects_linux_source_archive_hash_mismatch(tmp_path: Path) -> None:
    archive_path = _make_archive(
        tmp_path,
        "linux",
        "x64",
        corrupt_source_archive_hash=True,
    )

    with pytest.raises(ArtifactVerificationError, match="source archive hash"):
        verify_desktop_artifact(
            archive_path,
            platform_name="linux",
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
            "LotVue360/app/server.pem": (
                b"x" * (64 * 1024 - 10)
                + b"-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----\n"
            ),
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


def test_rejects_bundled_model_that_differs_from_its_pinned_hash(tmp_path: Path) -> None:
    archive_path = _make_archive(
        tmp_path, "windows", "x64",
        models={**MODELS, "isnet-general-use.onnx": b"swapped weights"},
        manifest_models=MODELS,
    )
    with pytest.raises(ArtifactVerificationError, match="does not match its pinned SHA-256"):
        verify_desktop_artifact(archive_path, platform_name="windows", architecture="x64",
                                expected_build_id=BUILD_ID)


def test_rejects_release_missing_an_approved_model(tmp_path: Path) -> None:
    only_primary = {"isnet-general-use.onnx": MODELS["isnet-general-use.onnx"]}
    archive_path = _make_archive(tmp_path, "windows", "x64", models=only_primary)
    with pytest.raises(ArtifactVerificationError, match="exactly the approved rembg models"):
        verify_desktop_artifact(archive_path, platform_name="windows", architecture="x64",
                                expected_build_id=BUILD_ID)
