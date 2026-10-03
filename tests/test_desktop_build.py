from __future__ import annotations

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
