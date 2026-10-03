from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"


def _run_isolated(code: str, home: Path, data_dir: str | None = None) -> dict:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env.pop("LOCALAPPDATA", None)
    env.pop("XDG_DATA_HOME", None)
    env.pop("LOTVUE_DATA_DIR", None)
    if data_dir is not None:
        env["LOTVUE_DATA_DIR"] = data_dir
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        check=True,
        text=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_source_run_defaults_data_and_storage_to_app_directory(tmp_path: Path) -> None:
    code = (
        "import json,sys; sys.path.insert(0, 'app'); import app_paths; "
        "print(json.dumps({'base': str(app_paths.BASE_DIR), 'data': str(app_paths.DATA_DIR), "
        "'storage': str(app_paths.STORAGE_DIR)}))"
    )
    paths = _run_isolated(code, tmp_path)
    assert Path(paths["data"]) == APP_DIR.resolve()
    assert Path(paths["storage"]) == (APP_DIR / "storage").resolve()


def test_frozen_macos_uses_per_user_application_support_directory(tmp_path: Path) -> None:
    code = (
        "import json,sys; sys.path.insert(0, 'app'); sys.frozen=True; sys.platform='darwin'; "
        "import app_paths; print(json.dumps({'data': str(app_paths.DATA_DIR)}))"
    )
    paths = _run_isolated(code, tmp_path)
    assert Path(paths["data"]) == tmp_path / "Library" / "Application Support" / "LotVue360"


def test_relative_user_data_override_resolves_from_application_directory(tmp_path: Path) -> None:
    code = (
        "import json,sys; sys.path.insert(0, 'app'); sys.frozen=True; "
        "import app_paths; print(json.dumps({'data': str(app_paths.DATA_DIR)}))"
    )
    paths = _run_isolated(code, tmp_path, "operator-data")
    assert Path(paths["data"]) == APP_DIR / "operator-data"


def test_main_loads_desktop_environment_and_database_from_user_data(tmp_path: Path) -> None:
    data_dir = tmp_path / "user-data"
    data_dir.mkdir()
    (data_dir / ".env").write_text("LOTVUE_PUBLIC_URL=https://dealer.example.test" + chr(10), encoding="utf-8")
    code = (
        "import json,os,sys; sys.path.insert(0, 'app'); import main; "
        "print(json.dumps({'env': str(main._ENV_FILE), 'db': str(main.DB_PATH), "
        "'storage': str(main.STORAGE_DIR), 'public_url': os.getenv('LOTVUE_PUBLIC_URL')}))"
    )
    paths = _run_isolated(code, tmp_path, str(data_dir))
    assert Path(paths["env"]) == data_dir / ".env"
    assert Path(paths["db"]) == data_dir / "vehicles.db"
    assert Path(paths["storage"]) == data_dir / "storage"
    assert paths["public_url"] == "https://dealer.example.test"
