# PyInstaller spec — LotVue360 desktop launcher (thin native shell).
# Native window via Edge/Chrome app-mode (no pywebview/.NET). The heavy server
# (torch/rembg/etc.) runs as a subprocess via the env python, so this bundle is
# small: pystray + PIL + stdlib only. Bundles ffi DLLs so _ctypes loads in the
# frozen conda-sourced build.

env_bin = r"E:\Miniconda3\envs\lotvue\Library\bin"

datas = [("static/desktop/lotvue.png", "static/desktop"),
         ("static/desktop/lotvue.ico", "static/desktop")]
binaries = [
    (env_bin + r"\ffi-8.dll", "."),
    (env_bin + r"\ffi.dll", "."),
]
hiddenimports = ["pystray._win32", "PIL.Image", "PIL.ImageDraw", "tkinter"]

block_cipher = None

a = Analysis(
    ["lotvue_desktop.py"],
    pathex=["E:\\ABU\\360Car\\app"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["torch", "cv2", "rembg", "onnxruntime", "numpy", "scipy",
              "matplotlib", "kokoro", "reportlab", "stripe", "webview",
              "pythonnet", "clr"],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name="LotVue360",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False,  # windowed — no console
    icon="static\\desktop\\lotvue.ico",
)
coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, name="LotVue360",
)
