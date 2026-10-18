#!/usr/bin/env python3
"""Build the Linux FFmpeg/FFprobe pair from pinned source, including its GPL source.

This avoids a binary Git-LFS download on the Linux release runner. The x264
encoder is GPL-2.0-or-later; matching FFmpeg/x264 source archives and license
notices are placed beside the binaries for owner/legal review before release.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FFMPEG_REPOSITORY = "https://github.com/FFmpeg/FFmpeg.git"
FFMPEG_REF = "n8.0"
FFMPEG_COMMIT = "140fd653aed8cad774f991ba083e2d01e86420c7"
X264_REPOSITORY = "https://github.com/mirror/x264.git"
X264_REF = "master"
X264_COMMIT = "c24e06c2e184345ceb33eb20a15d1024d9fd3497"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str], *, cwd: Path, env: dict[str, str], log_path: Path | None = None) -> str:
    rendered = " ".join(command)
    print(f"+ {rendered}", flush=True)
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout + result.stderr
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(output, encoding="utf-8", errors="replace")
    if result.returncode:
        tail = "\n".join(output.splitlines()[-80:])
        raise RuntimeError(f"Command failed ({result.returncode}): {rendered}\n{tail}")
    return output


def _clone_source(
    repository: str, reference: str, expected_commit: str, destination: Path, *, tag: str = "",
) -> str:
    """Fetch exactly ``expected_commit``; with ``tag``, also fetch that tag and
    require it to peel to the same commit. FFmpeg's version string comes from
    ``git describe --tags``, so without the tag a release build reports a bare
    commit hash instead of its release name."""
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    _run(["git", "init", "--quiet", str(destination)], cwd=destination.parent, env=env)
    _run(["git", "-C", str(destination), "remote", "add", "origin", repository], cwd=destination.parent, env=env)
    _run(
        ["git", "-C", str(destination), "fetch", "--quiet", "--depth=1", "origin", expected_commit],
        cwd=destination.parent,
        env=env,
    )
    actual = subprocess.run(
        ["git", "-C", str(destination), "rev-parse", "FETCH_HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if actual != expected_commit:
        raise RuntimeError(
            f"Pinned source fetch mismatch: {repository} {reference} resolved to {actual}, expected {expected_commit}"
        )
    _run(
        ["git", "-C", str(destination), "checkout", "--quiet", "--detach", expected_commit],
        cwd=destination.parent,
        env=env,
    )
    actual_head = subprocess.run(
        ["git", "-C", str(destination), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if actual_head != expected_commit:
        raise RuntimeError(
            f"Pinned source checkout mismatch: {repository} {reference} resolved to {actual_head}, expected {expected_commit}"
        )
    if tag:
        _run(
            ["git", "-C", str(destination), "fetch", "--quiet", "--depth=1", "origin",
             f"+refs/tags/{tag}:refs/tags/{tag}"],
            cwd=destination.parent,
            env=env,
        )
        tagged = subprocess.run(
            ["git", "-C", str(destination), "rev-parse", f"refs/tags/{tag}^{{commit}}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if tagged != expected_commit:
            raise RuntimeError(
                f"Pinned release tag mismatch: {repository} {tag} points to {tagged}, expected {expected_commit}"
            )
    return actual_head


def _pkg_config_shim(path: Path, prefix: Path) -> None:
    # x264's .pc metadata is sufficient here. Keeping this tiny helper avoids
    # needing a system pkg-config installation on a clean Ubuntu runner.
    quoted_prefix = "'" + str(prefix).replace("'", "'\\''") + "'"
    path.write_text(
        "#!/bin/sh\n"
        f"prefix={quoted_prefix}\n"
        "want_cflags=0\n"
        "want_libs=0\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    --version) echo 'lotvue-pkg-config-shim 1.0'; exit 0 ;;\n"
        "    --exists) exit 0 ;;\n"
        "    --modversion) echo '0.164'; exit 0 ;;\n"
        "    --cflags) want_cflags=1 ;;\n"
        "    --libs|--static) want_libs=1 ;;\n"
        "  esac\n"
        "  shift\n"
        "done\n"
        "if [ \"$want_cflags\" -eq 1 ]; then printf '%s ' \"-I$prefix/include\"; fi\n"
        "if [ \"$want_libs\" -eq 1 ]; then printf '%s ' \"-L$prefix/lib -lx264 -lpthread -lm\"; fi\n"
        "printf '\\n'\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def build(output: Path, jobs: int) -> Path:
    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise RuntimeError("The pinned desktop FFmpeg build currently targets Linux x86_64 only")
    for executable in ("git", "gcc", "make", "tar"):
        if shutil.which(executable) is None:
            raise RuntimeError(f"The Linux FFmpeg build requires {executable} on PATH")

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.stage-", dir=output.parent))
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    env["GIT_TERMINAL_PROMPT"] = "0"

    try:
        with tempfile.TemporaryDirectory(prefix="lotvue-ffmpeg-src-") as temp_name:
            work = Path(temp_name)
            sources = work / "sources"
            sources.mkdir()
            prefix = work / "prefix"
            prefix.mkdir()
            ffmpeg_source = sources / "ffmpeg"
            x264_source = sources / "x264"

            print(f"Cloning FFmpeg {FFMPEG_REF} ({FFMPEG_COMMIT})", flush=True)
            _clone_source(FFMPEG_REPOSITORY, FFMPEG_REF, FFMPEG_COMMIT, ffmpeg_source, tag=FFMPEG_REF)
            print(f"Cloning x264 {X264_COMMIT}", flush=True)
            _clone_source(X264_REPOSITORY, X264_REF, X264_COMMIT, x264_source)

            source_dir = staging / "source"
            source_dir.mkdir()
            ffmpeg_archive = source_dir / "ffmpeg-8.0.tar.gz"
            x264_archive = source_dir / f"x264-{X264_COMMIT}.tar.gz"
            _run(
                ["git", "-C", str(ffmpeg_source), "archive", "--format=tar.gz",
                 "--prefix=ffmpeg-8.0/", f"--output={ffmpeg_archive}", FFMPEG_COMMIT],
                cwd=work,
                env=env,
            )
            _run(
                ["git", "-C", str(x264_source), "archive", "--format=tar.gz",
                 f"--prefix=x264-{X264_COMMIT}/", f"--output={x264_archive}", X264_COMMIT],
                cwd=work,
                env=env,
            )

            x264_log = staging / "build-logs" / "x264-configure.log"
            assembler = "nasm" if shutil.which("nasm") else "yasm" if shutil.which("yasm") else ""
            x264_configure = [
                str(x264_source / "configure"),
                f"--prefix={prefix}",
                "--enable-static",
                "--disable-shared",
                "--disable-opencl",
            ]
            if not assembler:
                x264_configure.append("--disable-asm")
            _run(
                x264_configure,
                cwd=x264_source,
                env=env,
                log_path=x264_log,
            )
            print(f"Building x264 with {jobs} worker(s)", flush=True)
            _run(["make", f"-j{jobs}"], cwd=x264_source, env=env,
                 log_path=staging / "build-logs" / "x264-make.log")
            _run(["make", "install"], cwd=x264_source, env=env,
                 log_path=staging / "build-logs" / "x264-install.log")

            pkg_config = work / "pkg-config-x264"
            _pkg_config_shim(pkg_config, prefix)
            ffmpeg_configure = [
                str(ffmpeg_source / "configure"),
                f"--prefix={prefix}",
                f"--pkg-config={pkg_config}",
                "--pkg-config-flags=--static",
                f"--extra-cflags=-I{prefix / 'include'}",
                f"--extra-ldflags=-L{prefix / 'lib'}",
                "--extra-libs=-lpthread -lm",
                "--enable-gpl",
                "--enable-libx264",
                "--enable-static",
                "--disable-shared",
                "--disable-autodetect",
                "--disable-doc",
                "--disable-debug",
                "--disable-ffplay",
            ]
            if not assembler:
                ffmpeg_configure.append("--disable-x86asm")
            _run(
                ffmpeg_configure,
                cwd=ffmpeg_source,
                env=env,
                log_path=staging / "build-logs" / "ffmpeg-configure.log",
            )
            print(f"Building FFmpeg {FFMPEG_REF} with {jobs} worker(s)", flush=True)
            _run(["make", f"-j{jobs}"], cwd=ffmpeg_source, env=env,
                 log_path=staging / "build-logs" / "ffmpeg-make.log")
            _run(["make", "install"], cwd=ffmpeg_source, env=env,
                 log_path=staging / "build-logs" / "ffmpeg-install.log")

            ffmpeg_binary = prefix / "bin" / "ffmpeg"
            ffprobe_binary = prefix / "bin" / "ffprobe"
            if not ffmpeg_binary.is_file() or not ffprobe_binary.is_file():
                raise RuntimeError("FFmpeg build did not create both ffmpeg and ffprobe executables")
            for binary in (ffmpeg_binary, ffprobe_binary):
                binary.chmod(binary.stat().st_mode | 0o111)
            version = _run([str(ffmpeg_binary), "-version"], cwd=work, env=env)
            license_text = _run([str(ffmpeg_binary), "-L"], cwd=work, env=env)
            build_config = _run([str(ffmpeg_binary), "-buildconf"], cwd=work, env=env)
            # A tagless fetch of the pinned commit reports its short hash (or the
            # RELEASE file's "8.0") instead of git-describe's "n8.0".
            accepted = ("ffmpeg version n8.0", "ffmpeg version 8.0", f"ffmpeg version {FFMPEG_COMMIT[:7]}")
            if not version.startswith(accepted):
                raise RuntimeError(f"Built FFmpeg did not report the pinned n8.0 release: {version.splitlines()[0]!r}")
            normalized_license = license_text.lower()
            if (
                "gnu general public license" not in normalized_license
                or "version 2 of the license" not in normalized_license
                or "any later version" not in normalized_license
            ):
                raise RuntimeError("The FFmpeg/x264 build did not report its expected GPL-2.0-or-later license")
            if "--enable-gpl" not in build_config or "--enable-libx264" not in build_config:
                raise RuntimeError("FFmpeg build configuration is missing its GPL x264 encoder")
            if "--enable-nonfree" in build_config:
                raise RuntimeError("Refusing to package FFmpeg with nonfree components")

            smoke_mp4 = work / "synthetic-h264-smoke.mp4"
            _run(
                [
                    str(ffmpeg_binary), "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=black:s=64x64:r=1:d=1",
                    "-frames:v", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-y", str(smoke_mp4),
                ],
                cwd=work,
                env=env,
                log_path=staging / "build-logs" / "ffmpeg-h264-smoke.log",
            )
            probe = _run(
                [str(ffprobe_binary), "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1",
                 str(smoke_mp4)],
                cwd=work,
                env=env,
            )
            if probe.strip() != "h264":
                raise RuntimeError(f"Synthetic H.264 smoke test returned an unexpected codec: {probe!r}")

            binary_dir = staging / "bin"
            binary_dir.mkdir()
            shutil.copy2(ffmpeg_binary, binary_dir / "ffmpeg")
            shutil.copy2(ffprobe_binary, binary_dir / "ffprobe")
            gcc_version = subprocess.run(
                ["gcc", "--version"], capture_output=True, text=True, check=True,
            ).stdout.splitlines()[0]
            info = {
                "ffmpeg_repository": FFMPEG_REPOSITORY,
                "ffmpeg_ref": FFMPEG_REF,
                "ffmpeg_commit": FFMPEG_COMMIT,
                "x264_repository": X264_REPOSITORY,
                "x264_ref": X264_REF,
                "x264_commit": X264_COMMIT,
                "assembler": assembler or "none (C-only)",
                "license": "GPL-2.0-or-later (FFmpeg built with GPL x264)",
                "configure": [str(arg).replace(str(work), "<build-root>") for arg in ffmpeg_configure],
                "compiler": gcc_version,
                "smoke_test": "synthetic 64x64 H.264 MP4 encode/decode via ffprobe passed",
                "source_archives": {
                    ffmpeg_archive.name: _sha256(ffmpeg_archive),
                    x264_archive.name: _sha256(x264_archive),
                },
            }
            (staging / "BUILD-INFO.json").write_text(
                json.dumps(info, indent=2) + "\n", encoding="utf-8",
            )
            source_info = (
                "Linux FFmpeg/FFprobe are built from the exact source commits below.\n"
                f"FFmpeg: {FFMPEG_REPOSITORY} at tag {FFMPEG_REF}, commit {FFMPEG_COMMIT}.\n"
                f"x264: {X264_REPOSITORY} at ref {X264_REF}, commit {X264_COMMIT}.\n"
                "The FFmpeg binary uses --enable-gpl --enable-libx264 and reports GPL-2.0-or-later.\n"
                "Matching, unmodified source archives are bundled under app/bin/source/; their SHA-256\n"
                "values are recorded in BUILD-MANIFEST.json. This technical bundle is not a legal review\n"
                "or owner approval for redistribution, codec/patent terms, or commercial use.\n"
            )
            (staging / "FFMPEG-SOURCE-INFO.txt").write_text(source_info, encoding="utf-8")

        old = output.with_name(f".{output.name}.old")
        if old.exists():
            shutil.rmtree(old)
        if output.exists():
            output.rename(old)
        try:
            staging.rename(output)
        except OSError:
            if old.exists() and not output.exists():
                old.rename(output)
            raise
        shutil.rmtree(old, ignore_errors=True)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    print(f"FFmpeg source build output: {output}", flush=True)
    print(f"ffmpeg sha256: {_sha256(output / 'bin' / 'ffmpeg')}")
    print(f"ffprobe sha256: {_sha256(output / 'bin' / 'ffprobe')}")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "build" / "ffmpeg" / "linux-x64",
    )
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1))
    args = parser.parse_args()
    if args.jobs < 1 or args.jobs > 16:
        parser.error("--jobs must be between 1 and 16")
    build(args.output, args.jobs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
