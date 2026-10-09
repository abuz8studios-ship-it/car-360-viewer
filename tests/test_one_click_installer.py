from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from scripts.package_desktop import create_linux_one_click_installer

ROOT = Path(__file__).resolve().parents[1]


def test_linux_single_file_installer_installs_and_preserves_user_data(tmp_path: Path) -> None:
    package = tmp_path / "LotVue360"
    app = package / "app"
    app.mkdir(parents=True)
    launcher = package / "LotVue360"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    (app / "main.py").write_text("# synthetic test payload\n", encoding="utf-8")
    (app / ".env.example").write_text("# blank\n", encoding="utf-8")
    for name in ("Install-LotVue360.sh", "Uninstall-LotVue360.sh"):
        target = package / name
        target.write_bytes((ROOT / name).read_bytes())
        target.chmod(0o755)

    installer = tmp_path / "downloaded LotVue360 installer.run"
    create_linux_one_click_installer(package, installer)
    assert installer.stat().st_mode & 0o111
    sidecar = installer.with_suffix(installer.suffix + ".sha256")
    expected_hash = hashlib.sha256(installer.read_bytes()).hexdigest()
    assert sidecar.read_text(encoding="ascii") == f"{expected_hash}  {installer.name}\n"

    home = tmp_path / 'home "with spaces" and % signs'
    data_home = home / "XDG data"
    prefix = home / "custom install prefix"
    temp_dir = tmp_path / "private staging"
    temp_dir.mkdir()
    env = os.environ.copy()
    env.update({"HOME": str(home), "XDG_DATA_HOME": str(data_home), "TMPDIR": str(temp_dir)})
    env.pop("LOTVUE_INSTALL_PREFIX", None)
    env.pop("LOTVUE_DATA_DIR", None)

    install = subprocess.run(
        [str(installer), "--prefix", str(prefix), "--no-open"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert install.returncode == 0, install.stderr
    assert "Installed:" in install.stdout
    installed = prefix / "LotVue360"
    assert (installed / "LotVue360").is_file()
    assert list(temp_dir.iterdir()) == []  # the embedded extraction directory is removed

    user_data = data_home / "LotVue360"
    user_data.mkdir(parents=True)
    database = user_data / "vehicles.db"
    database.write_bytes(b"keep this customer data")
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
    assert not (data_home / "applications" / "lotvue360.desktop").exists()
    assert database.read_bytes() == b"keep this customer data"


def test_linux_single_file_installer_passes_help_without_installing(tmp_path: Path) -> None:
    package = tmp_path / "LotVue360"
    app = package / "app"
    app.mkdir(parents=True)
    launcher = package / "LotVue360"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    (app / "main.py").write_text("# synthetic test payload\n", encoding="utf-8")
    (app / ".env.example").write_text("# blank\n", encoding="utf-8")
    installer_script = package / "Install-LotVue360.sh"
    installer_script.write_bytes((ROOT / "Install-LotVue360.sh").read_bytes())
    installer_script.chmod(0o755)

    installer = create_linux_one_click_installer(package, tmp_path / "help.run")
    home = tmp_path / "isolated home"
    env = os.environ.copy()
    env.update({"HOME": str(home), "XDG_DATA_HOME": str(home / "data")})
    result = subprocess.run(
        [str(installer), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Usage: Install-LotVue360.sh" in result.stdout
    assert not (home / ".local" / "opt" / "LotVue360").exists()
