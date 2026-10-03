#!/usr/bin/env python3
"""Build a self-contained, per-user-installable LotVue360 desktop release.

Run on the same OS/architecture as the intended artifact after installing
requirements-desktop.txt. The build stages clean app source, generates the shared
background catalog, fetches platform FFmpeg/FFprobe, freezes Python dependencies,
and writes a checksummed zip for the release workflow.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = ROOT / "app"


def _run(command: list[str], *, cwd: Path | None = None, env=None) -> None:
    print("+", " ".join(map(str, command)), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def _ignore_app(_directory: str, names: list[str]) -> set[str]:
    ignored = {
        ".env", ".jwt_secret", "vehicles.db", "lotvue.db", "storage", "vehicles",
        "backups", "target", "_3dwork", "app_profile", "__pycache__",
        "standalone.log", "desktop_launch.log", "desktop_stdio.log",
        "server_stdout.log", "server_stderr.log", "lotvue_standalone.py",
        "lotvue_desktop.py", "build_env.py", "setup_stripe_webhook.py",
        "journey_qa.py", "BUILD_SESSION_LOG.md", "SESSION_TRANSCRIPT.md",
        "main_before_op6.py", "main_original.py", "main_v1.py",
        "LotVue360.spec", "LotVue360_dbg.spec", "LotVue360_standalone.spec",
        "LOTVUE_README.md", "static/create_icons.py",
    }
    model_suffixes = {
        ".bin", ".ckpt", ".engine", ".ggml", ".gguf", ".h5", ".keras",
        ".onnx", ".pb", ".pt", ".pth", ".safetensors", ".tflite", ".weights",
    }
    code_suffixes = {
        ".bat", ".c", ".cc", ".cmd", ".cpp", ".css", ".go", ".h", ".hpp",
        ".html", ".java", ".js", ".jsx", ".mjs", ".pyo", ".py", ".pyc",
        ".ps1", ".rs", ".sh", ".ts", ".tsx",
    }
    sensitive_suffixes = (
        ".db", ".db-journal", ".db-shm", ".db-wal", ".jks", ".key",
        ".keystore", ".log", ".p12", ".pem", ".pfx", ".pyc", ".pyo",
        ".sqlite", ".sqlite-journal", ".sqlite-shm", ".sqlite-wal", ".sqlite3", ".bak",
    )

    result = set()
    for name in names:
        lowered = name.lower()
        source_code = Path(lowered).suffix in code_suffixes
        env_file = (
            lowered == ".env"
            or lowered.startswith(".env.")
            or ".env." in lowered
            or lowered.endswith(".env")
        ) and lowered != ".env.example"
        if (
            name in ignored
            or ".bak-" in lowered
            or env_file
            or (not source_code and ("secret" in lowered or "credential" in lowered))
            or Path(lowered).suffix in model_suffixes
            or lowered.endswith(sensitive_suffixes)
        ):
            result.add(name)
    return result


def _is_onnxruntime_test_data(path: str | Path) -> bool:
    """Identify ORT's small sample models, which are test data, not runtime weights."""
    parts = tuple(part.lower() for part in Path(path).parts)
    return any(parts[index:index + 2] == ("onnxruntime", "datasets") for index in range(len(parts) - 1))


def _stage_application(stage_app: Path, build_id: str) -> None:
    shutil.copytree(APP_SOURCE, stage_app, ignore=_ignore_app)
    # Never trust local generated/untracked background files as release inputs.
    # The app's checked-in catalog generator creates the canonical set below.
    shutil.rmtree(stage_app / "static" / "backgrounds", ignore_errors=True)
    (stage_app / "bin").mkdir(parents=True, exist_ok=True)
    (stage_app / ".lotvue-build").write_text(build_id + "\n", encoding="utf-8")

    temp_data = stage_app.parent / "build-data"
    env = os.environ.copy()
    env["LOTVUE_DATA_DIR"] = str(temp_data)
    env["LOTVUE_STORAGE_DIR"] = str(temp_data / "storage")
    env["LOTVUE_FEATURE_PAYMENTS"] = "0"
    env["LOTVUE_PRICING_APPROVED"] = "0"
    # Never let a maintainer's private model opt-in affect build-time behavior.
    env.pop("LOTVUE_REMBG_MODEL", None)
    env.pop("LOTVUE_REMBG_LICENSE_ACCEPTED", None)
    env["PYTHONPATH"] = str(stage_app)
    # Runtime registration materializes packaged backgrounds and thumbnails. Do
    # it in the disposable staged copy so clean-checkout desktop releases contain
    # the complete visual catalog without dirtying source data or a dealer DB.
    script = (
        "import sys; sys.path.insert(0, sys.argv[1]); import main; "
        "from background_catalog import list_background_assets; "
        "assets=list_background_assets(); assert len(assets) >= 40, len(assets); "
        "print('packaged backgrounds:', len(assets))"
    )
    _run([sys.executable, "-c", script, str(stage_app)], cwd=stage_app, env=env)


def _identify_ffmpeg_license(license_text: str, build_config: str) -> tuple[str, str]:
    lowered_license = license_text.lower()
    lowered_config = build_config.lower()
    if (
        "--enable-nonfree" in lowered_config
        or "nonfree" in lowered_license
        or "non-free" in lowered_license
    ):
        raise RuntimeError("Refusing to package an FFmpeg build with nonfree components")

    # Exclude LGPL before checking for GPL: the substring "gpl" occurs inside
    # "lgpl" and must not accidentally result in a more permissive/incorrect notice.
    has_gpl = (
        "gpl" in lowered_license.replace("lgpl", "")
        or "gnu general public license" in lowered_license
        or "--enable-gpl" in lowered_config
    )
    has_lgpl = "lgpl" in lowered_license or "gnu lesser general public license" in lowered_license
    if has_gpl:
        if "version 3" in lowered_license or "--enable-version3" in lowered_config:
            return "GPL-3.0-or-later", "GPL-3.0.txt"
        if "version 2" in lowered_license:
            return "GPL-2.0-or-later", "GPL-2.0.txt"
        raise RuntimeError("FFmpeg is GPL-enabled but its GPL version could not be identified")
    if has_lgpl:
        if "version 3" in lowered_license or "--enable-version3" in lowered_config:
            return "LGPL-3.0-or-later", "LGPL-3.0.txt"
        if "version 2.1" in lowered_license or "version 2" in lowered_license:
            return "LGPL-2.1-or-later", "LGPL-2.1.txt"
        raise RuntimeError("FFmpeg is LGPL-enabled but its LGPL version could not be identified")
    raise RuntimeError("FFmpeg license could not be identified; refusing to create an unreviewed binary")


def _stage_ffmpeg(stage_app: Path) -> dict[str, str]:
    try:
        from static_ffmpeg.run import get_or_fetch_platform_executables_else_raise
    except ImportError as exc:
        raise RuntimeError("Install requirements-desktop.txt before building releases") from exc
    ffmpeg_source, ffprobe_source = get_or_fetch_platform_executables_else_raise()
    suffix = ".exe" if os.name == "nt" else ""
    ffmpeg_target = stage_app / "bin" / f"ffmpeg{suffix}"
    ffprobe_target = stage_app / "bin" / f"ffprobe{suffix}"
    shutil.copy2(Path(ffmpeg_source), ffmpeg_target)
    shutil.copy2(Path(ffprobe_source), ffprobe_target)
    if os.name != "nt":
        ffmpeg_target.chmod(ffmpeg_target.stat().st_mode | 0o111)
        ffprobe_target.chmod(ffprobe_target.stat().st_mode | 0o111)

    def capture(binary: Path, *arguments: str) -> str:
        result = subprocess.run(
            [str(binary), *arguments], capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"{binary.name} {' '.join(arguments)} failed with {result.returncode}")
        return (result.stdout + result.stderr).strip()

    version_text = capture(ffmpeg_target, "-version")
    probe_version = capture(ffprobe_target, "-version")
    license_text = capture(ffmpeg_target, "-L")
    build_config = capture(ffmpeg_target, "-buildconf")
    license_id, license_file = _identify_ffmpeg_license(license_text, build_config)

    license_source = ROOT / "docs" / "licenses" / license_file
    if not license_source.is_file():
        raise RuntimeError(f"The matching FFmpeg license text is missing: {license_source}")
    bundled_license = stage_app / "bin" / f"LICENSE-FFMPEG-{license_file}"
    shutil.copy2(license_source, bundled_license)
    source_info = (
        "FFmpeg/FFprobe are separately invoked command-line programs.\n"
        f"Detected license: {license_id}\n"
        "FFmpeg source release: https://ffmpeg.org/releases/ffmpeg-8.0.tar.xz\n"
        "FFmpeg source tag: https://github.com/FFmpeg/FFmpeg/tree/n8.0\n"
        "Binary distribution/build project: https://github.com/zackees/ffmpeg_bins\n"
        "Build project: https://github.com/zackees/static_ffmpeg\n"
        "Build configuration is recorded in FFMPEG-BUILD-INFO.txt.\n"
        "The FFmpeg source is separately licensed; it is not covered by the LotVue360 proprietary license.\n"
    )
    (stage_app / "bin" / "FFMPEG-LICENSE.txt").write_text(
        source_info + "\n--- ffmpeg -L ---\n" + license_text + "\n", encoding="utf-8",
    )
    (stage_app / "bin" / "FFMPEG-BUILD-INFO.txt").write_text(
        "--- ffmpeg -version ---\n" + version_text
        + "\n\n--- ffprobe -version ---\n" + probe_version
        + "\n\n--- ffmpeg -buildconf ---\n" + build_config + "\n",
        encoding="utf-8",
    )
    return {
        "ffmpeg": ffmpeg_target.name,
        "ffprobe": ffprobe_target.name,
        "license": license_id,
        "ffmpeg_sha256": _checksum(ffmpeg_target),
        "ffprobe_sha256": _checksum(ffprobe_target),
    }


def _architecture() -> str:
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    if machine in {"x86_64", "amd64", "x64"}:
        return "x64"
    return machine or "unknown"


def _zip_tree(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin" and (source.suffix == ".app" or (source / "LotVue360.app").is_dir()):
        _run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
              str(source), str(destination)])
        return
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for item in sorted(source.rglob("*")):
            if item.is_file():
                archive.write(item, item.relative_to(source.parent))


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(platform_name: str, output: Path, build_id: str) -> Path:
    expected = {"windows": "win32", "macos": "darwin", "linux": "linux"}[platform_name]
    if sys.platform != expected:
        raise RuntimeError(f"{platform_name} artifact must be built on {expected}; running on {sys.platform}")
    if sys.version_info < (3, 11) or sys.version_info >= (3, 14):
        raise RuntimeError("Use Python 3.11, 3.12, or 3.13 for the desktop release build")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    arch = _architecture()
    safe_build_id = "".join(ch for ch in build_id if ch.isalnum() or ch in "._-")[:80] or "local"

    build_root = ROOT / "build" / "desktop"
    dist_root = build_root / "dist"
    work_root = build_root / "work"
    shutil.rmtree(build_root, ignore_errors=True)
    dist_root.mkdir(parents=True, exist_ok=True)
    work_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="lotvue-desktop-stage-") as temp:
        stage_app = Path(temp) / "app"
        _stage_application(stage_app, safe_build_id)
        binaries = _stage_ffmpeg(stage_app)

        _run([
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            "--distpath", str(dist_root), "--workpath", str(work_root),
            str(APP_SOURCE / "LotVue360_standalone.spec"),
        ], cwd=ROOT)

        if platform_name == "macos":
            app_bundle = dist_root / "LotVue360.app"
            if not app_bundle.is_dir():
                raise RuntimeError(f"PyInstaller did not create the macOS application bundle: {app_bundle}")
            resource_app = app_bundle / "Contents" / "Resources" / "app"
            resource_app.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(stage_app, resource_app, ignore=_ignore_app)
            package_root = dist_root / f"LotVue360-macOS-{arch}"
            package_root.mkdir(parents=True, exist_ok=True)
            shutil.move(str(app_bundle), str(package_root / "LotVue360.app"))
            package_prefix = f"LotVue360-macOS-{arch}"
        else:
            package_root = dist_root / "LotVue360"
            if not package_root.is_dir():
                raise RuntimeError(f"PyInstaller did not create its onedir output: {package_root}")
            shutil.copytree(stage_app, package_root / "app", ignore=_ignore_app)
            package_prefix = f"LotVue360-{platform_name}-{arch}"

        readme = package_root / "README-INSTALL.txt" if package_root.is_dir() else None
        if readme is not None:
            readme.write_text(
                "LotVue360 desktop release\n"
                "=========================\n\n"
                "This artifact contains the Python runtime, core app dependencies, CPU rembg/ONNX runtime,\n"
                "FFmpeg/FFprobe, app source, and the packaged local backdrop catalog. No rembg model is selected\n"
                "or enabled by default. Verify a checkpoint's commercial terms before configuring it; approved\n"
                "weights download on first inference and may require Internet access.\n\n"
                "First launch creates a private per-user data directory and copies app/.env.example to .env.\n"
                "Open Dealer Studio and sign in or register. External services require your own approved accounts\n"
                "and credentials; they are never embedded in this installer.\n\n"
                "ComfyUI/WAN model weights and CUDA/Gaussian reconstruction tools are optional, hardware-specific\n"
                "integrations and are not bundled. See INSTALLATION.md and THIRD_PARTY_NOTICES.md for setup and\n"
                "redistribution notes. The bundle is unsigned and not notarized.\n",
                encoding="utf-8",
            )

        for document in ("INSTALLATION.md", "THIRD_PARTY_NOTICES.md"):
            source_doc = ROOT / "docs" / document
            if source_doc.is_file():
                shutil.copy2(source_doc, package_root / document)
        if platform_name == "windows":
            for script_name in (
                "Install-LotVue360.ps1", "Install-LotVue360.cmd", "Uninstall-LotVue360.ps1",
            ):
                shutil.copy2(ROOT / script_name, package_root / script_name)
        elif platform_name == "macos":
            for script_name in ("install-lotvue.sh", "Install-LotVue360.command"):
                shutil.copy2(ROOT / script_name, package_root / script_name)

        constraints_path = ROOT / "requirements-release-constraints.txt"
        if not constraints_path.is_file():
            raise RuntimeError(f"Release dependency constraints are missing: {constraints_path}")
        shutil.copy2(constraints_path, package_root / "PYTHON-BUILD-CONSTRAINTS.txt")

        manifest = {
            "product": "LotVue360",
            "build_id": safe_build_id,
            "platform": platform_name,
            "architecture": arch,
            "python": platform.python_version(),
            "python_constraints_file": "PYTHON-BUILD-CONSTRAINTS.txt",
            "bundled_engines": ["CPU background removal runtime", "FFmpeg", "FFprobe"],
            "ffmpeg_files": binaries,
            "not_bundled": [
                "RemBG pretrained model weights (not selected by default; operator must verify and acknowledge license)",
                "ComfyUI/WAN model weights and GPU runtime",
                "CUDA/COLMAP/Brush Gaussian reconstruction toolchain",
                "provider credentials, dealership accounts, and service approval",
            ],
            "pricing_approved": False,
            "signed": False,
            "notarized": False,
        }
        manifest_path = package_root / "BUILD-MANIFEST.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

        archive_path = output / f"{package_prefix}.zip"
        _zip_tree(package_root, archive_path)

    checksum = _checksum(archive_path)
    (archive_path.with_suffix(archive_path.suffix + ".sha256")).write_text(
        f"{checksum}  {archive_path.name}\n", encoding="ascii",
    )
    print(f"release artifact: {archive_path} ({archive_path.stat().st_size:,} bytes)")
    print(f"sha256: {checksum}")
    return archive_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("windows", "macos", "linux"), required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "desktop-release")
    parser.add_argument(
        "--build-id", default=os.getenv("LOTVUE_BUILD_ID") or os.getenv("GITHUB_SHA") or "local",
    )
    args = parser.parse_args()
    build(args.platform, args.output, args.build_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
