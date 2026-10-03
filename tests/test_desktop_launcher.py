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
