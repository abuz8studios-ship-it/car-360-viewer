from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

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


def test_linux_installer_help_after_prefix_option() -> None:
    result = subprocess.run(
        ["sh", str(ROOT / "Install-LotVue360.sh"), "--prefix", "/tmp/lotvue-help", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Usage: Install-LotVue360.sh" in result.stdout


@pytest.mark.skipif(sys.platform != "linux", reason="the packaged Linux installer is Linux-only")
def test_linux_installer_and_uninstaller_keep_user_data_by_default(tmp_path: Path) -> None:
    package = tmp_path / "LotVue360 Linux bundle"
    app = package / "app"
    app.mkdir(parents=True)
    launcher = package / "LotVue360"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    (app / "main.py").write_text("# app\n", encoding="utf-8")
    (app / ".env.example").write_text("# blank\n", encoding="utf-8")
    icon_dir = app / "static" / "desktop"
    icon_dir.mkdir(parents=True)
    (icon_dir / "lotvue.png").write_bytes(b"synthetic icon")
    for name in ("Install-LotVue360.sh", "Uninstall-LotVue360.sh"):
        (package / name).write_bytes((ROOT / name).read_bytes())
        (package / name).chmod(0o755)

    home = tmp_path / 'home\\directory "quoted" % sign'
    data_home = home / "XDG data with spaces"
    prefix = home / "custom install prefix"
    env = os.environ.copy()
    env.update({"HOME": str(home), "XDG_DATA_HOME": str(data_home)})
    env.pop("LOTVUE_INSTALL_PREFIX", None)
    env.pop("LOTVUE_DATA_DIR", None)

    install = subprocess.run(
        [str(package / "Install-LotVue360.sh"), "--prefix", str(prefix), "--no-open"],
        cwd=package,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert install.returncode == 0, install.stderr
    installed = prefix / "LotVue360"
    assert (installed / "LotVue360").is_file()
    desktop_entry = data_home / "applications" / "lotvue360.desktop"
    contents = desktop_entry.read_text(encoding="utf-8")
    escaped_exec = str(installed / "LotVue360").replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    assert f'Exec="{escaped_exec}"' in contents
    assert "X-LotVue360-Managed=true" in contents

    user_data = data_home / "LotVue360"
    user_data.mkdir(parents=True)
    (user_data / "vehicles.db").write_bytes(b"preserve")
    uninstall = subprocess.run(
        [str(installed / "Uninstall-LotVue360.sh"), "--prefix", str(prefix)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert uninstall.returncode == 0, uninstall.stderr
    assert not installed.exists()
    assert not desktop_entry.exists()
    assert (user_data / "vehicles.db").read_bytes() == b"preserve"

    reinstall = subprocess.run(
        [str(package / "Install-LotVue360.sh"), "--prefix", str(prefix), "--no-open"],
        cwd=package,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert reinstall.returncode == 0, reinstall.stderr
    remove_data = subprocess.run(
        [str(installed / "Uninstall-LotVue360.sh"), "--prefix", str(prefix), "--remove-data", "--yes"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert remove_data.returncode == 0, remove_data.stderr
    assert not installed.exists()
    assert not user_data.exists()
