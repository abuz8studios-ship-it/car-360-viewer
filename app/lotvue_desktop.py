"""
LotVue360 Desktop — the bundled program.
A native Windows application that runs the whole LotVue360 platform locally:
  • on launch, starts (or attaches to) the FastAPI server on :8001
  • opens the dealer dashboard in a native window (no browser, no console)
  • lives in the system tray: Dashboard / Mobile Capture / Public Site / Restart / Quit
  • bundled to LotVue360.exe via PyInstaller (see build_desktop.py)

Runs frozen (PyInstaller) or from source (python lotvue_desktop.py).
"""

import os
import sys
import time
import threading
import subprocess
import urllib.request
from pathlib import Path

APP_NAME = "LotVue360"
HOST = "127.0.0.1"

# When frozen, _MEIPASS holds bundled data; app code + env live next to the install.
FROZEN = getattr(sys, "frozen", False)
HERE = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent
APP_DIR = Path(os.environ.get("LOTVUE_APP_DIR", str(HERE if (HERE / "main.py").exists()
                                                   else r"E:\ABU\360Car\app")))
ENV_PY = os.environ.get("LOTVUE_PYTHON", r"E:\Miniconda3\envs\lotvue\python.exe")
ICON = APP_DIR / "static" / "desktop" / "lotvue.ico"


def _port_from_env():
    # single source of truth: app/.env LOTVUE_PORT (frozen exe can't see process env)
    try:
        for line in (APP_DIR / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("LOTVUE_PORT="):
                return int(line.split("=", 1)[1].strip())
    except Exception:
        pass
    return int(os.environ.get("LOTVUE_PORT", "8360"))


PORT = _port_from_env()
BASE = f"http://{HOST}:{PORT}"

_server_proc = None
_we_started_it = False
_LOG = None


def log(msg):
    global _LOG
    try:
        if _LOG is None:
            _LOG = open(APP_DIR / "desktop_launch.log", "a", encoding="utf-8")
        _LOG.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
        _LOG.flush()
    except Exception:
        pass


# ── server lifecycle ─────────────────────────────────────────────────────────
def _healthy(timeout=3):
    try:
        with urllib.request.urlopen(f"{BASE}/api/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_server(splash=None):
    """Attach to a running server, else spawn one and wait for health."""
    global _server_proc, _we_started_it
    if _healthy():
        return True
    # spawn detached, no console window
    creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    log = APP_DIR / "server_stdout.log"
    err = APP_DIR / "server_stderr.log"
    _server_proc = subprocess.Popen(
        [ENV_PY, "-u", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", str(PORT)],
        cwd=str(APP_DIR),
        stdin=subprocess.DEVNULL,
        stdout=open(log, "a", encoding="utf-8"),
        stderr=open(err, "a", encoding="utf-8"),
        creationflags=creationflags,
    )
    _we_started_it = True
    for i in range(60):
        if _healthy():
            return True
        if splash:
            splash(f"Starting LotVue360 engine… ({i+1}s)")
        time.sleep(1)
    return _healthy()


def stop_server():
    global _server_proc
    # only stop the server if THIS app launched it (don't kill the live public service)
    if _we_started_it and _server_proc and _server_proc.poll() is None:
        try:
            _server_proc.terminate()
        except Exception:
            pass


# ── icon (generated once, idempotent) ────────────────────────────────────────
def ensure_icon():
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    ICON.parent.mkdir(parents=True, exist_ok=True)
    png = ICON.with_suffix(".png")
    if not ICON.exists() or not png.exists():
        size = 256
        img = Image.new("RGB", (size, size), "#2E75B6")
        d = ImageDraw.Draw(img)
        m = size // 8
        d.ellipse([m, size * 0.32, size - m, size * 0.78], outline="#FFFFFF", width=size // 28)
        d.ellipse([size * 0.42, size * 0.47, size * 0.58, size * 0.63], fill="#F8FAFC")
        d.polygon([(size - m, size * 0.55), (size - m - size * 0.08, size * 0.48),
                   (size - m - size * 0.08, size * 0.62)], fill="#FFFFFF")
        img.save(png, "PNG")
        img.save(ICON, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return ICON


# ── system tray ──────────────────────────────────────────────────────────────
def _open(url):
    import webbrowser
    webbrowser.open(url)


def run_tray(on_quit):
    try:
        import pystray
        from PIL import Image
    except Exception:
        return None
    img = Image.open(ICON.with_suffix(".png")) if ICON.with_suffix(".png").exists() else None
    menu = pystray.Menu(
        pystray.MenuItem("Open Dashboard", lambda: _open(f"{BASE}/dashboard")),
        pystray.MenuItem("Mobile Capture", lambda: _open(f"{BASE}/mobile")),
        pystray.MenuItem("Public Site", lambda: _open("https://lotvue.abuz8ai.com")),
        pystray.MenuItem("Restart Engine", lambda: (stop_server(), time.sleep(1), ensure_server())),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", lambda icon: (icon.stop(), on_quit())),
    )
    icon = pystray.Icon(APP_NAME, img, APP_NAME, menu)
    threading.Thread(target=icon.run, daemon=True).start()
    return icon


# ── native app window via Chromium app-mode ──────────────────────────────────
# Edge/Chrome --app=URL gives a real standalone window (own taskbar icon, no
# browser chrome). Rock-solid and dependency-free vs pywebview/.NET.
def _find_browser():
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def _clear_profile_locks(profile):
    # A killed Edge leaves Singleton lock files that make a new --app launch
    # silently absorb with no window. Clear them when no app-mode Edge is live.
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
        try:
            (profile / name).unlink()
        except (OSError, FileNotFoundError):
            pass


def open_app_window(url):
    browser = _find_browser()
    profile = APP_DIR / "static" / "desktop" / "app_profile"
    profile.mkdir(parents=True, exist_ok=True)
    _clear_profile_locks(profile)
    if browser:
        try:
            # DEVNULL stdio is essential: a frozen --windowed app has no valid
            # std handles, and inheriting them makes Popen die silently.
            p = subprocess.Popen(
                [browser, f"--app={url}", f"--user-data-dir={profile}",
                 "--new-window", "--window-size=1280,840",
                 "--no-first-run", "--no-default-browser-check"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            log(f"launched app window via {browser} pid={p.pid}")
            return p
        except Exception as e:
            log(f"app-mode launch failed: {e}; falling back to default browser")
    # fallback: default browser (still works, just with browser chrome)
    try:
        os.startfile(url)  # noqa
        log("opened via os.startfile")
    except Exception as e:
        log(f"os.startfile failed: {e}")
        import webbrowser
        webbrowser.open(url)
    return None


# ── splash (instant feedback during engine cold-start) ───────────────────────
def show_splash(get_status):
    """A tiny always-on-top Tk window so the user NEVER sees 'nothing happening'
    while the engine boots. Returns a close() callable. Tk is stdlib → freezes clean."""
    try:
        import tkinter as tk
    except Exception:
        return lambda: None
    root = tk.Tk()
    root.title(APP_NAME)
    root.configure(bg="#2E75B6")
    root.overrideredirect(True)
    w, h = 420, 200
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
    root.attributes("-topmost", True)
    try:
        if ICON.exists():
            root.iconbitmap(str(ICON))
    except Exception:
        pass
    tk.Label(root, text="LotVue360", fg="#FFFFFF", bg="#2E75B6",
             font=("Segoe UI", 26, "bold")).pack(pady=(38, 4))
    status = tk.Label(root, text="Starting engine…", fg="#DCE9F6", bg="#2E75B6",
                      font=("Segoe UI", 11))
    status.pack()
    tk.Label(root, text="360 vehicle merchandising", fg="#A9C7E6", bg="#2E75B6",
             font=("Segoe UI", 9)).pack(side="bottom", pady=14)

    state = {"alive": True}

    def tick():
        if not state["alive"]:
            return
        try:
            status.config(text=get_status())
            root.after(500, tick)
        except Exception:
            pass

    root.after(300, tick)

    def pump():
        # run Tk event loop in this thread until close() is called
        while state["alive"]:
            try:
                root.update()
                time.sleep(0.05)
            except Exception:
                break

    def close():
        state["alive"] = False
        try:
            root.destroy()
        except Exception:
            pass

    threading.Thread(target=pump, daemon=True).start()
    return close


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    # CRITICAL: a frozen --windowed app has sys.stdout/stderr == None; any library
    # write to them crashes the process before anything renders. Redirect to log.
    try:
        if sys.stdout is None or sys.stderr is None:
            _f = open(APP_DIR / "desktop_stdio.log", "a", encoding="utf-8")
            if sys.stdout is None:
                sys.stdout = _f
            if sys.stderr is None:
                sys.stderr = _f
    except Exception:
        pass

    ensure_icon()
    _phase = {"msg": "Starting engine…"}
    close_splash = show_splash(lambda: _phase["msg"])

    def _sp(msg):
        _phase["msg"] = msg
        log(msg)

    ok = ensure_server(splash=_sp)
    _phase["msg"] = "Opening dashboard…" if ok else "Engine slow to start — opening anyway…"
    time.sleep(0.6)
    win = open_app_window(f"{BASE}/dashboard")
    time.sleep(1.5)
    close_splash()

    # tray keeps the program alive in the background
    try:
        import pystray
        from PIL import Image
        img = Image.open(ICON.with_suffix(".png")) if ICON.with_suffix(".png").exists() else None

        def _restart_engine(icon, item):
            stop_server(); time.sleep(1); ensure_server()

        def _quit(icon, item):
            icon.stop()
            try:
                if win and win.poll() is None:
                    win.terminate()
            except Exception:
                pass
            os._exit(0)

        menu = pystray.Menu(
            pystray.MenuItem("Open Dashboard", lambda i, it: open_app_window(f"{BASE}/dashboard")),
            pystray.MenuItem("Mobile Capture", lambda i, it: _open(f"{BASE}/mobile")),
            pystray.MenuItem("Public Site", lambda i, it: _open("https://lotvue.abuz8ai.com")),
            pystray.MenuItem("Restart Engine", _restart_engine),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", _quit),
        )
        pystray.Icon(APP_NAME, img, APP_NAME, menu).run()  # blocks until Quit
    except Exception as e:
        print(f"[Desktop] tray unavailable ({e}); window open, exiting launcher.")


if __name__ == "__main__":
    main()
