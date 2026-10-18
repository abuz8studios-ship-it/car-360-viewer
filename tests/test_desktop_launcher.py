from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"


def test_gui_launcher_configures_stdio_before_importing_fastapi(tmp_path: Path) -> None:
    data_dir = tmp_path / "user-data"
    code = (
        "import sys; sys.path.insert(0, 'app'); import lotvue_standalone as launcher; "
        "sys.stdout=None; sys.stderr=None; launcher._configure_stdio(); "
        "assert sys.stdout is not None and sys.stderr is not None"
    )
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["LOTVUE_DATA_DIR"] = str(data_dir)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (data_dir / "desktop_stdio.log").is_file()


def test_launcher_exposes_bundled_media_tools_without_overriding_explicit_config(tmp_path: Path) -> None:
    staged_app = tmp_path / "app-code"
    media_bin = staged_app / "bin"
    media_bin.mkdir(parents=True)
    (media_bin / "ffmpeg").write_text("placeholder", encoding="utf-8")
    (media_bin / "ffprobe").write_text("placeholder", encoding="utf-8")
    data_dir = tmp_path / "user-data"
    code = (
        "import json,os,sys; from pathlib import Path; sys.path.insert(0, 'app'); "
        "import lotvue_standalone as launcher; launcher.APP_DIR=Path(sys.argv[1]); "
        "os.environ['PATH']='sentinel'; os.environ['FFMPEG_PATH']='operator-ffmpeg'; "
        "os.environ['FFPROBE_PATH']=''; launcher._configure_bundled_media_tools(); "
        "print(json.dumps({'path':os.environ['PATH'],'ffmpeg':os.environ['FFMPEG_PATH'],"
        "'ffprobe':os.environ['FFPROBE_PATH']}))"
    )
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["LOTVUE_DATA_DIR"] = str(data_dir)
    env["LOTVUE_PORT"] = "18437"
    result = subprocess.run(
        [sys.executable, "-c", code, str(staged_app)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        check=True,
        text=True,
    )
    values = json.loads(result.stdout.strip().splitlines()[-1])
    assert values["path"].split(os.pathsep)[0] == str(media_bin)
    assert values["ffmpeg"] == "operator-ffmpeg"
    assert values["ffprobe"] == str(media_bin / "ffprobe")


def test_frozen_app_starts_its_own_scripts_through_the_launcher(monkeypatch) -> None:
    sys.path.insert(0, str(APP_DIR))
    import app_paths

    monkeypatch.setattr(sys, "executable", "/opt/LotVue360/LotVue360")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert app_paths.python_script_command("/app/gaussian_3d.py", "--vin", "X", no_user_site=True) == [
        "/opt/LotVue360/LotVue360", "--run-script", "/app/gaussian_3d.py", "--vin", "X"]

    monkeypatch.delattr(sys, "frozen")
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
    assert app_paths.python_script_command("/app/clip_classify.py", "/photos", no_user_site=True) == [
        "/usr/bin/python3", "-s", "/app/clip_classify.py", "/photos"]


def test_launcher_runs_app_scripts_and_refuses_others(tmp_path: Path) -> None:
    staged_app = tmp_path / "app"
    staged_app.mkdir()
    (staged_app / "engine.py").write_text(
        "import json, sys; from pathlib import Path\n"
        "Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\n"
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )
    outside = tmp_path / "elsewhere.py"
    outside.write_text("raise SystemExit(0)\n", encoding="utf-8")
    code = (
        "import sys; from pathlib import Path; sys.path.insert(0, 'app'); "
        "import lotvue_standalone as launcher; launcher.APP_DIR = Path(sys.argv[1]); "
        "sys.argv = ['LotVue360', '--run-script', *sys.argv[2:]]; raise SystemExit(launcher.main())"
    )
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["LOTVUE_DATA_DIR"] = str(tmp_path / "user-data")

    def launch(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-c", code, str(staged_app), *args],
                              cwd=ROOT, env=env, capture_output=True, text=True, check=False)

    out = tmp_path / "argv.json"
    ran = launch(str(staged_app / "engine.py"), str(out), "--vin", "X")
    assert ran.returncode == 7, ran.stderr
    assert json.loads(out.read_text(encoding="utf-8")) == ["--vin", "X"]

    refused = launch(str(outside))
    assert refused.returncode == 2
    assert "outside the app directory" in refused.stderr
