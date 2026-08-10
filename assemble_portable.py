"""
Assemble the LotVue360 portable program after the PyInstaller standalone build.
Produces:  E:\\ABU\\360Car\\dist\\LotVue360_standalone\\
  LotVue360.exe + _internal\\   (frozen runtime + libs)
  app\\                          (real writable app: code, static, .env, db, storage)
  Start LotVue360.bat
  README.txt
Copy that whole folder to ANY Windows PC and double-click LotVue360.exe.
"""
import os
import shutil
from pathlib import Path

SRC_APP = Path(r"E:\ABU\360Car\app")
DIST = Path(r"E:\ABU\360Car\dist\LotVue360_standalone")
DST_APP = DIST / "app"

if not (DIST / "LotVue360.exe").exists():
    raise SystemExit(f"Standalone build not found at {DIST}. Run the PyInstaller build first.")

# what NOT to ship (cache, build artifacts, the dev launcher's own specs/logs)
SKIP_DIRS = {"__pycache__", "app_profile", "backups", "ai_studio_cache",
             "storage", "vehicles", "target", "_3dwork"}  # data ships empty; clean build recreates
SKIP_SUFFIX = {".log", ".spec", ".pyc"}
SKIP_NAMES = {"desktop_launch.log", "standalone.log", "standalone_stdio.log",
              "desktop_stdio.log", "server_stdout.log", "server_stderr.log",
              "lotvue_standalone.py", "lotvue_desktop.py",
              "build_env.py", "setup_stripe_webhook.py", "journey_qa.py",
              "assemble_portable.py"}

if DST_APP.exists():
    shutil.rmtree(DST_APP, ignore_errors=True)
DST_APP.mkdir(parents=True, exist_ok=True)

copied = 0
skipped_mb = 0.0
for root, dirs, files in os.walk(SRC_APP):
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    rel = os.path.relpath(root, SRC_APP)
    out = DST_APP if rel == "." else DST_APP / rel
    out.mkdir(parents=True, exist_ok=True)
    for f in files:
        if f in SKIP_NAMES or Path(f).suffix in SKIP_SUFFIX:
            continue
        sp = Path(root) / f
        try:
            shutil.copy2(sp, out / f)
            copied += 1
        except Exception as e:
            print(f"skip {sp}: {e}")

# starter launcher + readme
(DIST / "Start LotVue360.bat").write_text(
    "@echo off\r\nstart \"\" \"%~dp0LotVue360.exe\"\r\n", encoding="utf-8")

(DIST / "README.txt").write_text(
    "LotVue360 - Portable\r\n"
    "====================\r\n\r\n"
    "Double-click LotVue360.exe (or 'Start LotVue360.bat') to launch.\r\n"
    "The engine starts inside the app - no Python or install required.\r\n"
    "A window opens to the dealer console; sign in with your account.\r\n\r\n"
    "Runs on any Windows 10/11 PC. Your data lives in the 'app' folder\r\n"
    "(app\\vehicles.db + app\\storage) - back that folder up to keep your inventory.\r\n\r\n"
    "Note: AI background-removal and AI video commercials need this build's\r\n"
    "GPU workstation (ComfyUI + models). On a plain PC, the 360 spin, viewer,\r\n"
    "dashboard, uploads, leads, exports, syndication and mobile capture all work.\r\n",
    encoding="utf-8")

# Conda-Python C-extensions need native DLLs PyInstaller misses. Copy them into
# _internal so pyexpat/_sqlite3/_bz2/_lzma load on a clean PC.
CONDA_BIN = Path(r"E:\Miniconda3\envs\lotvue\Library\bin")
INTERNAL = DIST / "_internal"
for d in ("libexpat.dll", "expat.dll", "sqlite3.dll", "libbz2.dll", "liblzma.dll",
          "zlib.dll", "libssl-3-x64.dll", "libcrypto-3-x64.dll", "ffi-8.dll", "ffi.dll"):
    src = CONDA_BIN / d
    if src.exists():
        shutil.copy2(src, INTERNAL / d)
print("support DLLs ensured in _internal")

total_mb = sum(f.stat().st_size for f in DST_APP.rglob("*") if f.is_file()) / 1e6
print(f"assembled app/: {copied} files, {total_mb:.0f} MB")
print(f"portable folder ready: {DIST}")
