"""LotVue360 cross-platform desktop launcher.

The frozen launcher contains the Python runtime and third-party libraries. The
application source and static assets ship beside it (or inside a macOS app
bundle), then are copied into a per-user writable code directory. Databases,
secrets, configuration, and uploads live in a separate per-user data directory.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

APP_NAME = "LotVue360"
HOST = "127.0.0.1"
FROZEN = bool(getattr(sys, "frozen", False))
EXE_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent


def _platform_data_dir() -> Path:
    configured = os.getenv("LOTVUE_DATA_DIR", "").strip()
    if configured:
        value = Path(configured).expanduser()
        return value if value.is_absolute() else EXE_DIR / value
    if not FROZEN:
        return Path(__file__).resolve().parent
    if os.name == "nt":
        root = Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.getenv("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return root / APP_NAME


DATA_DIR = _platform_data_dir().resolve()


def _resource_app_dir() -> Path:
    """Find the shipped Python application next to the frozen launcher."""
    configured = os.getenv("LOTVUE_BUNDLE_APP_DIR", "").strip()
    candidates = [Path(configured).expanduser()] if configured else []
    candidates.extend([
        EXE_DIR / "app",
        EXE_DIR.parent / "Resources" / "app",  # macOS .app/Contents/MacOS -> Resources
        Path(getattr(sys, "_MEIPASS", EXE_DIR)) / "app",
        Path(__file__).resolve().parent,
    ])
    for candidate in candidates:
        try:
            candidate = candidate.resolve(strict=True)
            if (candidate / "main.py").is_file() and (candidate / "app_paths.py").is_file():
                return candidate
        except (OSError, RuntimeError, ValueError):
            continue
    raise FileNotFoundError("LotVue360 application payload (app/main.py) is missing")


def _copy_missing_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        return
    for root, dirs, files in os.walk(source):
        relative = Path(root).relative_to(source)
        out = destination / relative
        out.mkdir(parents=True, exist_ok=True)
        dirs[:] = [directory for directory in dirs if directory not in {"__pycache__", "app_profile"}]
        for filename in files:
            source_file = Path(root) / filename
            target_file = out / filename
            if target_file.exists() or filename.endswith((".pyc", ".log")):
                continue
            try:
                shutil.copy2(source_file, target_file)
            except OSError:
                pass


def _sync_app_payload(source: Path, destination: Path) -> Path:
    """Install/update source files while keeping user data outside the code tree."""
    source_build = source / ".lotvue-build"
    build_id = source_build.read_text(encoding="utf-8").strip() if source_build.is_file() else "source"
    destination_build = destination / ".lotvue-build"
    current_id = destination_build.read_text(encoding="utf-8").strip() if destination_build.is_file() else ""
    if current_id == build_id and (destination / "main.py").is_file():
        return destination

    data_root = destination.parent
    staging = data_root / f".{destination.name}.update-{os.getpid()}"
    previous = data_root / f".{destination.name}.previous-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(previous, ignore_errors=True)
    ignore = shutil.ignore_patterns(
        ".env", ".jwt_secret", "*.db", "*.sqlite", "*.sqlite3", "*.log",
        "__pycache__", "*.pyc", "*.pyo", "*.bak", "*.bak-*", "*.spec.bak",
        "storage", "vehicles", "backups", "app_profile", "target", "_3dwork",
    )
    shutil.copytree(source, staging, ignore=ignore)

    # Keep locally generated or operator-added background assets across code updates.
    old_backgrounds = destination / "static" / "backgrounds"
    if old_backgrounds.is_dir():
        _copy_missing_tree(old_backgrounds, staging / "static" / "backgrounds")
    (staging / ".lotvue-build").write_text(build_id + "\n", encoding="utf-8")

    if destination.exists():
        os.replace(destination, previous)
    try:
        os.replace(staging, destination)
    except OSError:
        if previous.exists() and not destination.exists():
            os.replace(previous, destination)
        raise
    shutil.rmtree(previous, ignore_errors=True)
    return destination


def _migrate_legacy_data(data_dir: Path, resource_app: Path) -> None:
    """Move data from the older adjacent-app layout once, without overwriting."""
    data_dir.mkdir(parents=True, exist_ok=True)
    legacy_roots = [resource_app, EXE_DIR / "app", EXE_DIR]
    for name in ("vehicles.db", ".jwt_secret", ".env"):
        target = data_dir / name
        if target.exists():
            continue
        for legacy_root in legacy_roots:
            legacy = legacy_root / name
            if legacy.is_file() and name != ".env.example":
                try:
                    shutil.copy2(legacy, target)
                    break
                except OSError:
                    pass
    target_storage = data_dir / "storage"
    if not target_storage.exists():
        for legacy_root in legacy_roots:
            legacy_storage = legacy_root / "storage"
            if legacy_storage.is_dir():
                try:
                    shutil.copytree(legacy_storage, target_storage, dirs_exist_ok=True)
                    break
                except OSError:
                    pass


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if value.startswith(("'", '"')) and len(value) >= 2 and value[-1] == value[0]:
                value = value[1:-1]
            if key and key.replace("_", "").isalnum():
                values[key] = value
    except OSError:
        pass
    return values


def _load_data_environment() -> None:
    """Load first-run user settings without overriding explicitly exported vars."""
    env_file = DATA_DIR / ".env"
    if not env_file.exists():
        try:
            shutil.copy2(APP_DIR / ".env.example", env_file)
        except OSError:
            pass
    for key, value in _read_env_file(env_file).items():
        os.environ.setdefault(key, value)


def _legacy_port() -> int:
    for env_file in (DATA_DIR / ".env", APP_DIR / ".env"):
        values = _read_env_file(env_file)
        try:
            port = int(values.get("LOTVUE_PORT", "8360"))
            if 1024 <= port <= 65535:
                return port
        except (TypeError, ValueError):
            continue
    try:
        port = int(os.getenv("LOTVUE_PORT", "8360"))
        return port if 1024 <= port <= 65535 else 8360
    except (TypeError, ValueError):
        return 8360


try:
    _BUNDLE_APP_DIR = _resource_app_dir()
    _migrate_legacy_data(DATA_DIR, _BUNDLE_APP_DIR)
    APP_DIR = _sync_app_payload(_BUNDLE_APP_DIR, DATA_DIR / "app") if FROZEN else _BUNDLE_APP_DIR
except FileNotFoundError:
    if FROZEN:
        raise
    APP_DIR = Path(__file__).resolve().parent

DATA_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("LOTVUE_DATA_DIR", str(DATA_DIR))
os.environ.setdefault("LOTVUE_APP_DIR", str(APP_DIR))
_load_data_environment()


def _configure_bundled_media_tools() -> None:
    """Make the packaged FFmpeg pair visible to both explicit and PATH lookups."""
    executable_suffix = ".exe" if os.name == "nt" else ""
    media_bin = APP_DIR / "bin"
    ffmpeg = media_bin / f"ffmpeg{executable_suffix}"
    ffprobe = media_bin / f"ffprobe{executable_suffix}"
    if not (ffmpeg.is_file() and ffprobe.is_file()):
        return
    current_path = os.environ.get("PATH", "")
    os.environ["PATH"] = str(media_bin) + (os.pathsep + current_path if current_path else "")
    if not os.getenv("FFMPEG_PATH", "").strip():
        os.environ["FFMPEG_PATH"] = str(ffmpeg)
    if not os.getenv("FFPROBE_PATH", "").strip():
        os.environ["FFPROBE_PATH"] = str(ffprobe)


_configure_bundled_media_tools()
PORT = _legacy_port()
BASE = f"http://{HOST}:{PORT}"
ICON = APP_DIR / "static" / "desktop" / "lotvue.ico"
_phase = {"msg": "Starting engine…"}
_server = None
_server_thread: threading.Thread | None = None


def _log(message: str) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with (DATA_DIR / "desktop_launch.log").open("a", encoding="utf-8") as output:
            output.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def _healthy(timeout: float = 2) -> bool:
    try:
        with urllib.request.urlopen(f"{BASE}/api/health", timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def start_engine() -> None:
    """Run the FastAPI engine in-process; packaged users do not need Python."""
    global _server, _server_thread
    if _healthy():
        _log("an app health endpoint already responds; attaching to it")
        return
    os.chdir(str(APP_DIR))
    if str(APP_DIR) not in sys.path:
        sys.path.insert(0, str(APP_DIR))

    def serve() -> None:
        global _server
        try:
            import uvicorn
            import main as app_module

            config = uvicorn.Config(
                app_module.app, host=HOST, port=PORT, log_level="warning", workers=1,
            )
            _server = uvicorn.Server(config)
            _server.install_signal_handlers = lambda: None
            _log("embedded uvicorn starting")
            _server.run()
        except Exception as exc:
            _log(f"ENGINE ERROR: {type(exc).__name__}: {exc}")
            import traceback
            _log(traceback.format_exc())

    _server_thread = threading.Thread(target=serve, name="lotvue-server", daemon=True)
    _server_thread.start()


def stop_engine(timeout: float = 8) -> None:
    global _server, _server_thread
    if _server is not None:
        _server.should_exit = True
    if _server_thread is not None:
        _server_thread.join(timeout=max(0.1, timeout))
    _server = None
    _server_thread = None


def _smoke_test() -> int:
    """Start the frozen engine, verify local health/catalog routes, and exit cleanly."""
    global PORT, BASE
    # Avoid accidentally attaching to a running user instance or colliding with
    # another service on the normal desktop port during CI/archive validation.
    with socket.socket() as listener:
        listener.bind((HOST, 0))
        PORT = int(listener.getsockname()[1])
    BASE = f"http://{HOST}:{PORT}"
    if FROZEN:
        suffix = ".exe" if os.name == "nt" else ""
        for name in (f"ffmpeg{suffix}", f"ffprobe{suffix}"):
            binary = APP_DIR / "bin" / name
            try:
                subprocess.run([str(binary), "-version"], capture_output=True, check=True, timeout=20)
            except (OSError, subprocess.SubprocessError) as exc:
                _log(f"packaged self-test failed: bundled {name} is unavailable ({type(exc).__name__})")
                return 1
    start_engine()
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if _healthy(timeout=2):
            break
        time.sleep(0.25)
    else:
        _log("packaged self-test failed: health endpoint did not start")
        stop_engine()
        return 1
    try:
        with urllib.request.urlopen(f"{BASE}/api/backgrounds", timeout=10) as response:
            backgrounds = json.loads(response.read().decode("utf-8"))
        if len(backgrounds) < 40 or not any(item.get("slug") == "none" for item in backgrounds):
            raise RuntimeError("the 40-background catalog or keep-original option did not initialize")
        with urllib.request.urlopen(f"{BASE}/api/backgrounds/scenes", timeout=10) as response:
            scenes = json.loads(response.read().decode("utf-8"))
        scene_rows = scenes.get("scenes", [])
        if len(scene_rows) != 8 or any(not scene.get("has_preview") for scene in scene_rows):
            raise RuntimeError("compatibility scene previews did not initialize")
        if str(APP_DIR) not in sys.path:
            sys.path.insert(0, str(APP_DIR))
        import main as app_module
        from studio_agent import build_system_health
        health = build_system_health(app_module)
        if health.get("external_services_contacted") is not False:
            raise RuntimeError("the local health snapshot unexpectedly contacted an external service")
        expected_rembg = "configured" if app_module.REMBG_AVAILABLE else "setup_required"
        if health["subsystems"]["background_removal"]["status"] != expected_rembg:
            raise RuntimeError("background-removal readiness disagrees with the explicit model gate")
        rembg_detail = ""
        if app_module.REMBG_AVAILABLE:
            # Prove the bundled weights load offline and cut out an image, so a
            # build missing its models fails here instead of shipping degraded.
            import rembg_runtime
            from PIL import Image
            if FROZEN:  # never let rembg quietly download what the bundle should carry
                wanted = (rembg_runtime.configured_model(), rembg_runtime.fallback_model())
                missing = [name for name in wanted if name and not
                           (rembg_runtime.BUNDLED_MODEL_DIR / f"{name}.onnx").is_file()]
                if missing:
                    raise RuntimeError(f"bundled background-removal model(s) missing: {', '.join(missing)}")
            rembg_runtime.remove(Image.new("RGB", (64, 64), (128, 128, 128)))
            rembg_detail = f", background removal via {rembg_runtime.active_model()}"
        _log(
            f"packaged self-test passed: {len(backgrounds)} backgrounds, 8 scenes, "
            f"local configuration snapshot, no external probes{rembg_detail}"
        )
        return 0
    except Exception as exc:
        _log(f"packaged self-test failed: {type(exc).__name__}: {exc}")
        return 1
    finally:
        stop_engine()


def _open_app(url: str) -> None:
    if sys.platform == "darwin":
        try:
            subprocess.Popen(["open", url], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            pass
    elif os.name == "nt":
        try:
            os.startfile(url)  # type: ignore[attr-defined]
            return
        except (OSError, AttributeError):
            pass
    webbrowser.open(url)


def show_splash() -> callable:
    try:
        import tkinter as tk
    except Exception:
        return lambda: None
    try:
        root = tk.Tk()
        root.title(APP_NAME)
        root.configure(bg="#2E75B6")
        root.overrideredirect(True)
        width, height = 430, 205
        screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{width}x{height}+{(screen_w-width)//2}+{(screen_h-height)//2}")
        root.attributes("-topmost", True)
        tk.Label(root, text=APP_NAME, fg="#fff", bg="#2E75B6",
                 font=("Segoe UI", 27, "bold")).pack(pady=(40, 4))
        status = tk.Label(root, text="Starting local engine…", fg="#DCE9F6", bg="#2E75B6",
                          font=("Segoe UI", 11))
        status.pack()
        alive = {"value": True}

        def close() -> None:
            alive["value"] = False
            try:
                root.destroy()
            except Exception:
                pass

        def pump() -> None:
            while alive["value"]:
                try:
                    status.configure(text=_phase["msg"])
                    root.update()
                    time.sleep(0.1)
                except Exception:
                    break

        threading.Thread(target=pump, name="lotvue-splash", daemon=True).start()
        return close
    except Exception:
        return lambda: None


def _configure_stdio() -> None:
    """Provide a writable log stream for GUI builds whose stdio is detached."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.stderr is None:
        try:
            log_stream = (DATA_DIR / "desktop_stdio.log").open("a", encoding="utf-8")
        except OSError:
            log_stream = open(os.devnull, "w", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = log_stream
        if sys.stderr is None:
            sys.stderr = log_stream


def _run_tray() -> bool:
    try:
        import pystray
        from PIL import Image
    except Exception:
        return False
    try:
        icon_path = ICON.with_suffix(".png")
        tray_image = Image.open(icon_path) if icon_path.is_file() else Image.new("RGB", (64, 64), "#2E75B6")
        menu = pystray.Menu(
            pystray.MenuItem("Open Dealer Studio", lambda _icon, _item: _open_app(f"{BASE}/studio")),
            pystray.MenuItem("Open Dashboard", lambda _icon, _item: _open_app(f"{BASE}/dashboard")),
            pystray.MenuItem("Mobile Capture", lambda _icon, _item: _open_app(f"{BASE}/mobile")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit LotVue360", lambda icon, _item: (icon.stop(), stop_engine())),
        )
        pystray.Icon(APP_NAME, tray_image, APP_NAME, menu).run()
        return True
    except Exception as exc:
        _log(f"system-tray unavailable: {type(exc).__name__}: {exc}")
        return False


def _run_script(script: str, arguments: list[str]) -> int:
    """Run one of the app's own scripts (e.g. gaussian_3d.py) in this runtime.

    Frozen builds have no python executable, so the app starts its engine
    scripts as `LotVue360 --run-script <script> ...`. Only .py files inside the
    app directory are accepted."""
    import runpy

    app_dir = APP_DIR.resolve()
    path = Path(script).resolve()
    if path.suffix != ".py" or not path.is_file() or app_dir not in path.parents:
        _log(f"refused --run-script outside the app directory: {script}")
        print(f"refusing to run a script outside the app directory: {script}", file=sys.stderr)
        return 2
    if str(app_dir) not in sys.path:
        sys.path.insert(0, str(app_dir))
    sys.argv = [str(path), *arguments]
    try:
        runpy.run_path(str(path), run_name="__main__")
    except SystemExit as exc:
        if exc.code is None:
            return 0
        return exc.code if isinstance(exc.code, int) else 1
    return 0


def main() -> int:
    _configure_stdio()
    # Redirect frozen windowed-build streams before importing the app, whose
    # startup diagnostics use print(). This ordering also applies to CI smoke.
    if len(sys.argv) >= 3 and sys.argv[1] == "--run-script":
        return _run_script(sys.argv[2], sys.argv[3:])
    if "--self-test" in sys.argv:
        return _smoke_test()
    _log(f"launching: app={APP_DIR}; data={DATA_DIR}; port={PORT}; frozen={FROZEN}")
    close_splash = show_splash()
    start_engine()
    for index in range(90):
        if _healthy():
            _phase["msg"] = "Opening Dealer Studio…"
            break
        _phase["msg"] = f"Starting local engine… ({index + 1}s)"
        time.sleep(1)
    else:
        _phase["msg"] = "Engine is taking longer than expected; check the local log."
        _log("startup timed out waiting for the health endpoint")
    time.sleep(0.4)
    _open_app(f"{BASE}/studio")
    close_splash()

    if _run_tray():
        return 0
    _log("no system-tray backend; keeping the local server alive until the process exits")
    try:
        while _server_thread is not None and _server_thread.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        stop_engine()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
