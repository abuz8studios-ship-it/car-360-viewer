from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_macos_installer_help_after_applications_option_does_not_skip_flag() -> None:
    result = subprocess.run(
        ["bash", str(ROOT / "install-lotvue.sh"), "--applications", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Usage: bash install-lotvue.sh" in result.stdout
