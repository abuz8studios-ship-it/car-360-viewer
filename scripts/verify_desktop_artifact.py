#!/usr/bin/env python3
"""Verify a built desktop ZIP, its SHA-256 sidecar, and release manifest.

This checks the actual archive before GitHub Actions uploads it. The subsequent
native-runner smoke steps still extract, install, launch, and exercise the app.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO

LICENSE_FILES = {
    "GPL-2.0-or-later": "GPL-2.0.txt",
    "GPL-3.0-or-later": "GPL-3.0.txt",
    "LGPL-2.1-or-later": "LGPL-2.1.txt",
    "LGPL-3.0-or-later": "LGPL-3.0.txt",
}
MODEL_SUFFIXES = {
    ".bin", ".ckpt", ".engine", ".ggml", ".gguf", ".h5", ".keras", ".onnx",
    ".pb", ".pt", ".pth", ".safetensors", ".tflite", ".weights",
}
SECRET_NAMES = {".env", ".jwt_secret", "desktop_launch.log", "desktop_stdio.log"}
DATABASE_NAMES = {"vehicles.db", "lotvue.db"}
USER_DATA_SUFFIXES = (
    ".db", ".db-journal", ".db-shm", ".db-wal", ".log", ".bak",
    ".sqlite", ".sqlite-journal", ".sqlite-shm", ".sqlite-wal", ".sqlite3",
)
SECRET_SUFFIXES = (".jks", ".key", ".keystore", ".p12", ".pem", ".pfx")
CODE_SUFFIXES = {
    ".bat", ".c", ".cc", ".cmd", ".cpp", ".css", ".go", ".h", ".hpp",
    ".html", ".java", ".js", ".jsx", ".mjs", ".pyo", ".py", ".pyc",
    ".ps1", ".rs", ".sh", ".ts", ".tsx",
}


def _is_sensitive_path_part(part: str, *, is_file: bool) -> bool:
    lowered = part.lower()
    if lowered == ".env.example":
        return False
    if lowered in SECRET_NAMES | DATABASE_NAMES:
        return True
    source_code = Path(lowered).suffix in CODE_SUFFIXES
    return (
        lowered == ".env"
        or lowered.startswith(".env.")
        or ".env." in lowered
        or lowered.endswith(".env")
        or (is_file and not source_code and ("secret" in lowered or "credential" in lowered))
        or lowered.endswith(SECRET_SUFFIXES)
        or ".bak-" in lowered
    )


class ArtifactVerificationError(ValueError):
    """Raised when the archive fails a release-integrity or content check."""


def _sha256_stream(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return _sha256_stream(stream)


def _require(entries: set[str], path: str) -> None:
    if path not in entries:
        raise ArtifactVerificationError(f"Required archive file is missing: {path}")


def _text(archive: zipfile.ZipFile, path: str) -> str:
    try:
        return archive.read(path).decode("utf-8")
    except (KeyError, UnicodeDecodeError) as exc:
        raise ArtifactVerificationError(f"Required UTF-8 text file is unreadable: {path}") from exc


def _app_bundle_paths(platform_name: str, package_root: str) -> tuple[str, str, tuple[str, ...]]:
    if platform_name == "windows":
        app_prefix = f"{package_root}/app"
        launcher = f"{package_root}/LotVue360.exe"
        installers = (
            f"{package_root}/Install-LotVue360.ps1",
            f"{package_root}/Install-LotVue360.cmd",
            f"{package_root}/Uninstall-LotVue360.ps1",
        )
    else:
        app_prefix = f"{package_root}/LotVue360.app/Contents/Resources/app"
        launcher = f"{package_root}/LotVue360.app/Contents/MacOS/LotVue360"
        installers = (
            f"{package_root}/install-lotvue.sh",
            f"{package_root}/Install-LotVue360.command",
        )
    return app_prefix, launcher, installers


def verify_desktop_artifact(
    archive_path: Path,
    *,
    platform_name: str,
    architecture: str,
    expected_build_id: str,
) -> dict[str, object]:
    """Verify a platform archive and return a compact summary for CI logs."""
    if platform_name not in {"windows", "macos"}:
        raise ArtifactVerificationError(f"Unsupported desktop platform: {platform_name}")
    if platform_name == "windows" and architecture != "x64":
        raise ArtifactVerificationError("The current Windows desktop build supports x64 only")
    if platform_name == "macos" and architecture not in {"x64", "arm64"}:
        raise ArtifactVerificationError(f"Unsupported macOS architecture: {architecture}")

    archive_path = archive_path.resolve()
    if not archive_path.is_file():
        raise ArtifactVerificationError(f"Artifact ZIP does not exist: {archive_path}")
    sidecar_path = archive_path.with_suffix(archive_path.suffix + ".sha256")
    if not sidecar_path.is_file():
        raise ArtifactVerificationError(f"Artifact checksum sidecar is missing: {sidecar_path}")

    sidecar = sidecar_path.read_text(encoding="ascii").strip().split()
    if len(sidecar) != 2 or sidecar[1] != archive_path.name or not re.fullmatch(r"[0-9a-fA-F]{64}", sidecar[0]):
        raise ArtifactVerificationError("Checksum sidecar must contain SHA-256 and the exact ZIP filename")
    actual_checksum = _sha256_file(archive_path)
    if actual_checksum.lower() != sidecar[0].lower():
        raise ArtifactVerificationError("Artifact ZIP SHA-256 does not match its sidecar")

    expected_archive_name = (
        "LotVue360-windows-x64.zip"
        if platform_name == "windows"
        else f"LotVue360-macOS-{architecture}.zip"
    )
    if archive_path.name != expected_archive_name:
        raise ArtifactVerificationError(
            f"Artifact name does not match target platform: expected {expected_archive_name}"
        )
    package_root = "LotVue360" if platform_name == "windows" else f"LotVue360-macOS-{architecture}"
    app_prefix, launcher, installer_paths = _app_bundle_paths(platform_name, package_root)

    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.namelist()
            file_entries = {name.rstrip("/") for name in entries if not name.endswith("/")}
            if len(file_entries) != len([name for name in entries if not name.endswith("/")]):
                raise ArtifactVerificationError("Artifact ZIP contains duplicate file entries")
            for name in entries:
                path = PurePosixPath(name)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in name
                    or "\x00" in name
                    or re.match(r"^[a-zA-Z]:", name)
                ):
                    raise ArtifactVerificationError(f"Unsafe path in artifact ZIP: {name}")

            manifest_path = f"{package_root}/BUILD-MANIFEST.json"
            manifest_text = _text(archive, manifest_path)
            try:
                manifest = json.loads(manifest_text)
            except json.JSONDecodeError as exc:
                raise ArtifactVerificationError("Build manifest is not valid JSON") from exc
            if not isinstance(manifest, dict):
                raise ArtifactVerificationError("Build manifest must be a JSON object")

            expected_manifest = {
                "product": "LotVue360",
                "platform": platform_name,
                "architecture": architecture,
                "build_id": expected_build_id,
                "python_constraints_file": "PYTHON-BUILD-CONSTRAINTS.txt",
            }
            for key, expected in expected_manifest.items():
                if manifest.get(key) != expected:
                    raise ArtifactVerificationError(
                        f"Build manifest field {key!r} is {manifest.get(key)!r}, expected {expected!r}"
                    )
            for key in ("pricing_approved", "signed", "notarized"):
                if manifest.get(key) is not False:
                    raise ArtifactVerificationError(f"Build manifest must explicitly set {key}=false")

            raw_not_bundled = manifest.get("not_bundled")
            if not isinstance(raw_not_bundled, list) or not all(isinstance(item, str) for item in raw_not_bundled):
                raise ArtifactVerificationError("Build manifest has an invalid not_bundled list")
            not_bundled = [item.lower() for item in raw_not_bundled]
            required_not_bundled = ("rembg pretrained model weights", "provider credentials")
            if not all(any(required in item for item in not_bundled) for required in required_not_bundled):
                raise ArtifactVerificationError("Build manifest does not list model weights and provider credentials as excluded")

            ffmpeg_files = manifest.get("ffmpeg_files")
            if not isinstance(ffmpeg_files, dict):
                raise ArtifactVerificationError("Build manifest has no FFmpeg/FFprobe file record")
            suffix = ".exe" if platform_name == "windows" else ""
            expected_binaries = {"ffmpeg": f"ffmpeg{suffix}", "ffprobe": f"ffprobe{suffix}"}
            for key, expected_name in expected_binaries.items():
                if ffmpeg_files.get(key) != expected_name:
                    raise ArtifactVerificationError(f"Build manifest has unexpected {key} binary name")
                binary_path = f"{app_prefix}/bin/{expected_name}"
                _require(file_entries, binary_path)
                recorded_hash = ffmpeg_files.get(f"{key}_sha256")
                if not isinstance(recorded_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", recorded_hash):
                    raise ArtifactVerificationError(f"Build manifest has no valid {key} SHA-256")
                with archive.open(binary_path) as stream:
                    bundled_hash = _sha256_stream(stream)
                if bundled_hash.lower() != recorded_hash.lower():
                    raise ArtifactVerificationError(f"Bundled {key} hash disagrees with the build manifest")

            license_id = ffmpeg_files.get("license")
            if not isinstance(license_id, str):
                raise ArtifactVerificationError("Build manifest has no FFmpeg license identifier")
            license_filename = LICENSE_FILES.get(license_id)
            if license_filename is None:
                raise ArtifactVerificationError(f"Build manifest has an unsupported FFmpeg license: {license_id!r}")
            required_files = [
                f"{package_root}/README-INSTALL.txt",
                f"{package_root}/INSTALLATION.md",
                f"{package_root}/THIRD_PARTY_NOTICES.md",
                f"{package_root}/PYTHON-BUILD-CONSTRAINTS.txt",
                f"{app_prefix}/main.py",
                f"{app_prefix}/.env.example",
                f"{app_prefix}/bin/FFMPEG-LICENSE.txt",
                f"{app_prefix}/bin/FFMPEG-BUILD-INFO.txt",
                f"{app_prefix}/bin/LICENSE-FFMPEG-{license_filename}",
                launcher,
                *installer_paths,
            ]
            for path in required_files:
                _require(file_entries, path)

            # Scan the complete archive, not only staged app source: a frozen runtime or
            # root-level installer payload must not smuggle in a model or user secret.
            for name in file_entries:
                path = PurePosixPath(name)
                sensitive_parts = any(
                    _is_sensitive_path_part(part, is_file=index == len(path.parts) - 1)
                    for index, part in enumerate(path.parts)
                )
                if sensitive_parts or name.lower().endswith(USER_DATA_SUFFIXES):
                    raise ArtifactVerificationError(f"User secret, log, or database was packaged: {name}")
                if path.suffix.lower() in MODEL_SUFFIXES:
                    raise ArtifactVerificationError(f"Model weights must not be bundled: {name}")
                if any(part.lower() == "storage" for part in path.parts):
                    raise ArtifactVerificationError(f"Writable user storage must not be bundled: {name}")

            raw_engines = manifest.get("bundled_engines")
            if not isinstance(raw_engines, list) or not all(isinstance(item, str) for item in raw_engines):
                raise ArtifactVerificationError("Build manifest has an invalid bundled_engines list")
            if "ffmpeg" not in [item.lower() for item in raw_engines]:
                raise ArtifactVerificationError("Build manifest does not report bundled FFmpeg")
            license_notice = _text(archive, f"{app_prefix}/bin/FFMPEG-LICENSE.txt")
            build_info = _text(archive, f"{app_prefix}/bin/FFMPEG-BUILD-INFO.txt")
            if license_id not in license_notice or "ffmpeg -version" not in build_info.lower():
                raise ArtifactVerificationError("FFmpeg license/build notices are incomplete")
            readme = _text(archive, f"{package_root}/README-INSTALL.txt")
            if "not notarized" not in readme.lower() or "no rembg model" not in readme.lower():
                raise ArtifactVerificationError("Installer README is missing model-gate or signing status")

    except zipfile.BadZipFile as exc:
        raise ArtifactVerificationError(f"Artifact is not a readable ZIP: {archive_path.name}") from exc

    return {
        "archive": archive_path.name,
        "bytes": archive_path.stat().st_size,
        "sha256": actual_checksum,
        "files": len(file_entries),
        "build_id": expected_build_id,
        "platform": platform_name,
        "architecture": architecture,
        "ffmpeg_license": license_id,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--platform", choices=("windows", "macos"), required=True)
    parser.add_argument("--architecture", choices=("x64", "arm64"), required=True)
    parser.add_argument("--build-id", required=True)
    args = parser.parse_args()
    try:
        summary = verify_desktop_artifact(
            args.archive,
            platform_name=args.platform,
            architecture=args.architecture,
            expected_build_id=args.build_id,
        )
    except ArtifactVerificationError as exc:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            message = str(exc).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print(f"::error title=Desktop artifact verification failed::{message}", file=sys.stderr)
        parser.error(str(exc))
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
