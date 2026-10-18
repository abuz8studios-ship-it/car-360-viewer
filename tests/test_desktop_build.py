from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_desktop


def test_ffmpeg_lgpl_is_not_misclassified_as_gpl() -> None:
    assert build_desktop._identify_ffmpeg_license(
        "FFmpeg is licensed under the GNU Lesser General Public License version 2.1 or later",
        "configuration: --enable-shared",
    ) == ("LGPL-2.1-or-later", "LGPL-2.1.txt")


def test_ffmpeg_gpl_license_and_configuration_are_detected() -> None:
    assert build_desktop._identify_ffmpeg_license(
        "This software is licensed under the GPL version 3 or later",
        "configuration: --enable-gpl --enable-version3",
    ) == ("GPL-3.0-or-later", "GPL-3.0.txt")


def test_desktop_source_staging_excludes_legacy_secrets_and_backup_modules() -> None:
    ignored = build_desktop._ignore_app(
        "app",
        [
            ".env", ".jwt_secret", "vehicles.db", "main_before_op6.py",
            "main_original.py", "main_v1.py", "main.py.bak-classifier",
            "lotvue_standalone.py", "main.py", "studio_agent.py",
        ],
    )
    assert {
        ".env", ".jwt_secret", "vehicles.db", "main_before_op6.py",
        "main_original.py", "main_v1.py", "main.py.bak-classifier",
        "lotvue_standalone.py",
    } <= ignored
    assert "main.py" not in ignored
    assert "studio_agent.py" not in ignored


def test_ffmpeg_nonfree_or_unidentified_license_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="nonfree"):
        build_desktop._identify_ffmpeg_license(
            "GNU General Public License version 3", "configuration: --enable-nonfree"
        )
    with pytest.raises(RuntimeError, match="could not be identified"):
        build_desktop._identify_ffmpeg_license("unknown license", "configuration: minimal")


def test_stages_pinned_linux_ffmpeg_binaries_and_matching_sources(tmp_path: Path, monkeypatch) -> None:
    source_root = tmp_path / "ffmpeg-source-build"
    binary_dir = source_root / "bin"
    source_dir = source_root / "source"
    binary_dir.mkdir(parents=True)
    source_dir.mkdir()
    fake_binary = binary_dir / "ffmpeg-template"
    fake_binary.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "name = os.path.basename(sys.argv[0])\n"
        "arg = sys.argv[1] if len(sys.argv) > 1 else ''\n"
        "if arg == '-version': print(('ffprobe' if name == 'ffprobe' else 'ffmpeg') + ' version n8.0')\n"
        "elif arg == '-L': print('GNU General Public License, either version 2 of the License or any later version')\n"
        "elif arg == '-buildconf': print('configuration: --enable-gpl --enable-libx264')\n"
        "else: sys.exit(2)\n",
        encoding="utf-8",
    )
    fake_binary.chmod(0o755)
    for name in ("ffmpeg", "ffprobe"):
        (binary_dir / name).write_bytes(fake_binary.read_bytes())
        (binary_dir / name).chmod(0o755)

    archives = {
        "ffmpeg-8.0.tar.gz": b"pinned FFmpeg source archive",
        f"x264-{build_desktop.LINUX_X264_COMMIT}.tar.gz": b"pinned x264 source archive",
    }
    archive_hashes = {}
    for name, content in archives.items():
        (source_dir / name).write_bytes(content)
        archive_hashes[name] = hashlib.sha256(content).hexdigest()
    (source_root / "FFMPEG-SOURCE-INFO.txt").write_text(
        f"FFmpeg commit {build_desktop.LINUX_FFMPEG_COMMIT}; "
        f"x264 commit {build_desktop.LINUX_X264_COMMIT}; GPL-2.0-or-later\n",
        encoding="utf-8",
    )
    build_info = {
        "ffmpeg_commit": build_desktop.LINUX_FFMPEG_COMMIT,
        "x264_commit": build_desktop.LINUX_X264_COMMIT,
        "license": "GPL-2.0-or-later (FFmpeg built with GPL x264)",
        "smoke_test": "synthetic H.264 MP4 encode/decode via ffprobe passed",
        "source_archives": archive_hashes,
    }
    (source_root / "BUILD-INFO.json").write_text(json.dumps(build_info), encoding="utf-8")
    monkeypatch.setenv("LOTVUE_FFMPEG_ROOT", str(source_root))
    stage_app = tmp_path / "staged-app"
    (stage_app / "bin").mkdir(parents=True)

    result = build_desktop._stage_ffmpeg(stage_app)

    assert result["license"] == "GPL-2.0-or-later"
    assert result["source_archives"] == archive_hashes
    assert result["ffmpeg_sha256"] == build_desktop._checksum(stage_app / "bin" / "ffmpeg")
    assert result["ffprobe_sha256"] == build_desktop._checksum(stage_app / "bin" / "ffprobe")
    assert (stage_app / "bin" / "source" / "ffmpeg-8.0.tar.gz").read_bytes() == archives["ffmpeg-8.0.tar.gz"]
    assert (stage_app / "bin" / "FFMPEG-SOURCE-BUILD-INFO.json").is_file()


def test_prebuilt_ffmpeg_license_notice_names_the_detected_license(tmp_path: Path, monkeypatch) -> None:
    # Windows/macOS stage static-ffmpeg's prebuilt binaries; the release verifier
    # requires FFMPEG-LICENSE.txt to name the detected license on every platform.
    import types

    fetched = tmp_path / "static-ffmpeg"
    fetched.mkdir()
    for name in ("ffmpeg", "ffprobe"):
        binary = fetched / name
        binary.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "arg = sys.argv[1] if len(sys.argv) > 1 else ''\n"
            f"if arg == '-version': print('{name} version 8.0-static')\n"
            "elif arg == '-L': print('GNU General Public License, either version 3 of the License or any later version')\n"
            "elif arg == '-buildconf': print('configuration: --enable-gpl --enable-version3')\n"
            "else: sys.exit(2)\n",
            encoding="utf-8",
        )
        binary.chmod(0o755)
    run_module = types.ModuleType("static_ffmpeg.run")
    run_module.get_or_fetch_platform_executables_else_raise = lambda: (str(fetched / "ffmpeg"), str(fetched / "ffprobe"))
    monkeypatch.setitem(sys.modules, "static_ffmpeg", types.ModuleType("static_ffmpeg"))
    monkeypatch.setitem(sys.modules, "static_ffmpeg.run", run_module)
    monkeypatch.delenv("LOTVUE_FFMPEG_ROOT", raising=False)
    monkeypatch.setattr(build_desktop.os, "name", "posix")  # stage the test binaries without an .exe suffix
    stage_app = tmp_path / "staged-app"
    (stage_app / "bin").mkdir(parents=True)

    result = build_desktop._stage_ffmpeg(stage_app)

    assert result["license"] == "GPL-3.0-or-later"
    assert "GPL-3.0-or-later" in (stage_app / "bin" / "FFMPEG-LICENSE.txt").read_text(encoding="utf-8")
    assert "ffmpeg -version" in (stage_app / "bin" / "FFMPEG-BUILD-INFO.txt").read_text(encoding="utf-8")


def _synthetic_model_pins(cache: Path) -> dict:
    """Small stand-ins for the pinned rembg models, pre-seeded in a build cache."""
    cache.mkdir(parents=True, exist_ok=True)
    pins = {}
    for name, role in (("isnet-general-use.onnx", "primary"), ("u2net.onnx", "fallback")):
        content = f"synthetic {name}".encode()
        (cache / name).write_bytes(content)
        pins[name] = {"url": f"https://example.invalid/{name}", "role": role, "license": "test",
                      "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
    return pins


def test_rembg_model_fetch_refuses_a_download_with_the_wrong_hash(tmp_path: Path, monkeypatch) -> None:
    import io

    pins = {"isnet-general-use.onnx": {"url": "https://example.invalid/isnet", "role": "primary",
                                       "license": "test", "sha256": "0" * 64, "bytes": 1}}
    monkeypatch.setattr(build_desktop, "BUNDLED_REMBG_MODELS", pins)
    monkeypatch.setattr(build_desktop.urllib.request, "urlopen",
                        lambda _url, timeout: io.BytesIO(b"tampered weights"))
    with pytest.raises(RuntimeError, match="does not match the pinned"):
        build_desktop._fetch_rembg_models(tmp_path / "cache")


def test_linux_build_emits_single_file_installer(tmp_path: Path, monkeypatch) -> None:
    import subprocess
    import zipfile

    project = tmp_path / "project"
    app_source = project / "app"
    app_source.mkdir(parents=True)
    (app_source / "LotVue360_standalone.spec").write_text("# test spec\n", encoding="utf-8")
    docs = project / "docs"
    docs.mkdir()
    for name in ("INSTALLATION.md", "THIRD_PARTY_NOTICES.md"):
        (docs / name).write_text(f"# {name}\n", encoding="utf-8")
    (project / "requirements-release-constraints.txt").write_text("# test\n", encoding="utf-8")
    for name in ("Install-LotVue360.sh", "Uninstall-LotVue360.sh"):
        (project / name).write_bytes((ROOT / name).read_bytes())
        (project / name).chmod(0o755)

    monkeypatch.setattr(build_desktop, "ROOT", project)
    monkeypatch.setattr(build_desktop, "APP_SOURCE", app_source)
    cache = tmp_path / "model cache"
    pins = _synthetic_model_pins(cache)
    monkeypatch.setattr(build_desktop, "BUNDLED_REMBG_MODELS", pins)
    monkeypatch.setenv("LOTVUE_REMBG_MODEL_CACHE", str(cache))

    def stage_application(stage_app: Path, _build_id: str) -> None:
        stage_app.mkdir(parents=True)
        (stage_app / "main.py").write_text("# test\n", encoding="utf-8")
        (stage_app / ".env.example").write_text("# blank\n", encoding="utf-8")
        (stage_app / "bin").mkdir()

    monkeypatch.setattr(build_desktop, "_stage_application", stage_application)
    monkeypatch.setattr(build_desktop, "_stage_ffmpeg", lambda _stage: {
        "ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "license": "GPL-3.0-or-later",
        "ffmpeg_sha256": "0" * 64, "ffprobe_sha256": "0" * 64,
    })

    def fake_pyinstaller(command, *, cwd=None, env=None) -> None:
        del cwd, env
        dist = Path(command[command.index("--distpath") + 1]) / "LotVue360"
        dist.mkdir(parents=True)
        launcher = dist / "LotVue360"
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        launcher.chmod(0o755)

    monkeypatch.setattr(build_desktop, "_run", fake_pyinstaller)
    output = tmp_path / "release output"
    archive = build_desktop.build("linux", output, "test-build")
    installer = output / "LotVue360-OneClick-linux-x64.run"
    assert archive.is_file()
    assert installer.is_file() and installer.stat().st_mode & 0o111
    sidecar = output / f"{installer.name}.sha256"
    assert sidecar.read_text(encoding="ascii") == (
        f"{build_desktop._checksum(installer)}  {installer.name}\n"
    )
    result = subprocess.run(
        [str(installer), "--help"], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Usage: Install-LotVue360.sh" in result.stdout

    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
        manifest = json.loads(bundle.read("LotVue360/BUILD-MANIFEST.json"))
        readme = bundle.read("LotVue360/README-INSTALL.txt").decode()
    for name, pin in pins.items():
        assert f"LotVue360/app/models/rembg/{name}" in names
    assert {m["name"]: m["sha256"] for m in manifest["bundled_models"]} == {
        name: pin["sha256"] for name, pin in pins.items()}
    assert "isnet-general-use" in readme
