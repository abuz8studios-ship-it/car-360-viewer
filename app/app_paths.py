"""Canonical application code and writable-data paths shared by runtime modules."""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def configured_path(variable: str, default: Path) -> Path:
    """Resolve a configurable path relative to the app directory, not the CWD."""
    value = os.getenv(variable, "").strip()
    path = Path(value).expanduser() if value else Path(default).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return path.resolve()


def _default_data_dir() -> Path:
    """Use a writable per-user directory for frozen desktop installs."""
    if not getattr(sys, "frozen", False):
        return BASE_DIR
    if os.name == "nt":
        root = Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.getenv("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return root / "LotVue360"


def python_script_command(script: Path | str, *arguments: str, no_user_site: bool = False) -> list[str]:
    """Command that runs one of the app's own scripts in a child process.

    A frozen desktop build has no python executable: sys.executable is the
    LotVue360 launcher, which runs an app script only when given --run-script.
    Without it the launcher would start another copy of the app instead."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--run-script", str(script), *arguments]
    return [sys.executable, *(["-s"] if no_user_site else []), str(script), *arguments]


DATA_DIR = configured_path("LOTVUE_DATA_DIR", _default_data_dir())
ENV_FILE = DATA_DIR / ".env"
DB_PATH = DATA_DIR / "vehicles.db"
STORAGE_DIR = configured_path("LOTVUE_STORAGE_DIR", DATA_DIR / "storage")

# Create only the local writable roots needed by the app. A configured storage
# path may be read-only or offline; leave that failure for the capability report
# rather than preventing unrelated routes from starting.
DATA_DIR.mkdir(parents=True, exist_ok=True)
try:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass
