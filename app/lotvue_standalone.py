"""
LotVue360 STANDALONE — the fully self-contained program.
Bundles the Python runtime + every server dependency via PyInstaller, runs the
FastAPI engine IN-PROCESS (no conda, no external Python), and opens the dealer
console in a native window. Runs on ANY Windows PC.

Layout when shipped (portable folder):
    LotVue360/
      LotVue360.exe        <- this, frozen (runtime + libs)
      _internal/           <- PyInstaller libraries
      app/                 <- REAL folder: main.py, modules, static/, .env, vehicles.db, storage/
                              (writable data lives here, NOT inside the frozen bundle)
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

FROZEN = getattr(sys, "frozen", False)
EXE_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent
# the real, writable app folder shipped next to the exe (or this dir from source)
APP_DIR = EXE_DIR / "app" if (EXE_DIR / "app" / "main.py").exists() else \
    (Path(__file__).resolve().parent if (Path(__file__).resolve().parent / "main.py").exists()
     else EXE_DIR)


def _read_port():
    try:
        for line in (APP_DIR / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("LOTVUE_PORT="):
                return int(line.split("=", 1)[1].strip())
    except Exception:
        pass
    return int(os.environ.get("LOTVUE_PORT", "8360"))


PORT = _read_port()
BASE = f"http://{HOST}:{PORT}"
ICON = APP_DIR / "static" / "desktop" / "lotvue.ico"
_phase = {"msg": "Starting engine…"}


def _log(msg):
    try:
        with open(APP_DIR / "standalone.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _healthy(t=2):
    try:
        with urllib.request.urlopen(f"{BASE}/api/health", timeout=t) as r:
            return r.status == 200
    except Exception:
        return False


# ── in-process engine ────────────────────────────────────────────────────────
def start_engine():
    """Run uvicorn serving the in-process FastAPI app in a daemon thread."""
    if _healthy():
        _log("engine already running — attaching")
        return
    os.chdir(str(APP_DIR))
    if str(APP_DIR) not in sys.path:
        sys.path.insert(0, str(APP_DIR))

    def _serve():
        try:
            import uvicorn
            import main as app_module  # noqa: imports the whole server
            config = uvicorn.Config(app_module.app, host="0.0.0.0", port=PORT,
                                    log_level="warning", workers=1)
            server = uvicorn.Server(config)
            server.install_signal_handlers = lambda: None  # not main thread
            _log("engine thread starting uvicorn")
            server.run()
        except Exception as e:
            _log(f"ENGINE CRASH: {e}")
            import traceback
            _log(traceback.format_exc())

    threading.Thread(target=_serve, daemon=True).start()


# ── native window via Edge/Chrome app-mode ───────────────────────────────────
def _find_browser():
    for c in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
        if os.path.exists(c):
            return c
    return None


def open_window(url):
    b = _find_browser()
    prof = APP_DIR / "static" / "desktop" / "app_profile"
    prof.mkdir(parents=True, exist_ok=True)
    for lk in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
        try:
            (prof / lk).unlink()
        except OSError:
            pass
    if b:
        try:
            p = subprocess.Popen(
                [b, f"--app={url}", f"--user-data-dir={prof}", "--new-window",
                 "--window-size=1300,860", "--no-first-run", "--no-default-browser-check"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _log(f"window via {b} pid={p.pid}")
            return p
        except Exception as e:
            _log(f"window launch failed: {e}")
    try:
        os.startfile(url)  # noqa
    except Exception:
        import webbrowser
        webbrowser.open(url)
    return None


# ── splash (instant feedback) ────────────────────────────────────────────────
def show_splash():
    try:
        import tkinter as tk
    except Exception:
        return lambda: None
    root = tk.Tk(); root.overrideredirect(True); root.configure(bg="#2E75B6")
    w, h = 440, 210
    root.geometry(f"{w}x{h}+{(root.winfo_screenwidth()-w)//2}+{(root.winfo_screenheight()-h)//2}")
    root.attributes("-topmost", True)
    tk.Label(root, text="LotVue360", fg="#fff", bg="#2E75B6", font=("Segoe UI", 28, "bold")).pack(pady=(42, 4))
    st = tk.Label(root, text="Starting engine…", fg="#DCE9F6", bg="#2E75B6", font=("Segoe UI", 11)); st.pack()
    tk.Label(root, text="360 vehicle merchandising", fg="#A9C7E6", bg="#2E75B6", font=("Segoe UI", 9)).pack(side="bottom", pady=16)
    alive = {"v": True}

    def tick():
        if not alive["v"]:
            return
        try:
            st.config(text=_phase["msg"]); root.after(400, tick)
        except Exception:
            pass

    root.after(300, tick)

    def pump():
        while alive["v"]:
            try:
                root.update(); time.sleep(0.05)
            except Exception:
                break

    def close():
        alive["v"] = False
        try:
            root.destroy()
        except Exception:
            pass

    threading.Thread(target=pump, daemon=True).start()
    return close


def main():
    if sys.stdout is None or sys.stderr is None:
        try:
            f = open(APP_DIR / "standalone_stdio.log", "a", encoding="utf-8")
            sys.stdout = sys.stdout or f
            sys.stderr = sys.stderr or f
        except Exception:
            pass
    _log(f"=== launch === APP_DIR={APP_DIR} PORT={PORT} frozen={FROZEN}")
    close_splash = show_splash()
    start_engine()
    for i in range(90):
        if _healthy():
            break
        _phase["msg"] = f"Starting engine… ({i+1}s)"
        time.sleep(1)
    _phase["msg"] = "Opening dashboard…"
    time.sleep(0.5)
    win = open_window(f"{BASE}/dashboard")
    time.sleep(1.5)
    close_splash()

    # tray
    try:
        import pystray
        from PIL import Image
        img = Image.open(ICON.with_suffix(".png")) if ICON.with_suffix(".png").exists() else None
        menu = pystray.Menu(
            pystray.MenuItem("Open Dashboard", lambda i, it: open_window(f"{BASE}/dashboard")),
            pystray.MenuItem("Mobile Capture", lambda i, it: open_window(f"{BASE}/mobile")),
            pystray.MenuItem("My Showroom", lambda i, it: open_window(f"{BASE}/showroom/default")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda ic, it: (ic.stop(), os._exit(0))),
        )
        pystray.Icon(APP_NAME, img, APP_NAME, menu).run()
    except Exception as e:
        _log(f"tray unavailable: {e}")
        # keep process alive so the engine + window stay up
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    main()
