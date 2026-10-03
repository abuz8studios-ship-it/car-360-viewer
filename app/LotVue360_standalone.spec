# Cross-platform PyInstaller build for the embedded local LotVue360 desktop app.
# The application source/static directory is copied beside the frozen runtime by
# scripts/build_desktop.py; this spec freezes Python + third-party dependencies.
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

APP_ROOT = Path(SPECPATH).resolve()
ICON_PNG = APP_ROOT / "static" / "desktop" / "lotvue.png"
ICON_ICO = APP_ROOT / "static" / "desktop" / "lotvue.ico"

# Modules reached by the app through dynamic route registration/imports. A
# missing core dependency must stop packaging instead of producing a broken exe.
PACKAGES = [
    "fastapi", "starlette", "uvicorn", "pydantic", "pydantic_core",
    "numpy", "cv2", "PIL", "pillow_heif", "jose", "cryptography", "cffi",
    "stripe", "requests", "reportlab", "qrcode", "multipart", "anyio",
    "sniffio", "click", "h11", "httptools", "websockets", "watchfiles",
    "certifi", "charset_normalizer", "idna", "urllib3", "ecdsa", "rsa",
    "pyasn1", "annotated_types", "typing_extensions",
    "rembg", "onnxruntime", "pymatting", "scipy", "skimage", "numba",
    "llvmlite", "pooch", "tqdm", "imageio", "networkx", "lazy_loader",
    "tifffile", "platformdirs", "filelock", "jsonschema", "flatbuffers",
    "google.protobuf", "pystray", "curl_cffi",
]

datas = []
binaries = []
hiddenimports = [
    "PIL.Image", "PIL.ImageDraw", "PIL.ImageOps",
    "PIL.ImageFont", "PIL.ImageFilter", "PIL.ImageEnhance", "PIL.ImageChops",
    "PIL.ImageFile", "PIL.JpegImagePlugin", "PIL.PngImagePlugin",
    "qrcode.image.pil", "reportlab.graphics.barcode",
    "uvicorn.loops.auto", "uvicorn.loops.asyncio", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl", "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets.auto", "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on", "anyio._backends._asyncio",
]

for package in PACKAGES:
    if importlib.util.find_spec(package) is None:
        raise RuntimeError(f"Desktop build dependency is missing: {package}")
    package_data, package_binaries, package_imports = collect_all(package)
    datas += package_data
    binaries += package_binaries
    hiddenimports += package_imports

hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("fastapi")
hiddenimports += collect_submodules("pystray")
hiddenimports += collect_submodules("curl_cffi")
if importlib.util.find_spec("tkinter") is not None:
    hiddenimports += ["tkinter", "tkinter.ttk"]
if sys.platform == "darwin":
    hiddenimports += collect_submodules("objc")
    hiddenimports += collect_submodules("AppKit")

for icon_path in (ICON_PNG, ICON_ICO):
    if icon_path.is_file():
        datas.append((str(icon_path), "static/desktop"))

analysis = Analysis(
    [str(APP_ROOT / "lotvue_standalone.py")],
    pathex=[str(APP_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "torch", "torchvision", "torchaudio", "tensorflow", "keras",
        "matplotlib", "pandas", "IPython", "notebook", "sympy",
        "jupyter", "pytest", "ruff",
    ],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="LotVue360",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ICON_ICO) if sys.platform == "win32" and ICON_ICO.is_file() else None,
)
collected = COLLECT(
    exe,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    strip=False,
    upx=False,
    name="LotVue360",
)

if sys.platform == "darwin":
    app_bundle = BUNDLE(
        collected,
        name="LotVue360.app",
        bundle_identifier="com.abuz8.lotvue360",
        info_plist={
            "CFBundleName": "LotVue360",
            "CFBundleDisplayName": "LotVue360",
            "CFBundleShortVersionString": "0.1.0",
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.business",
        },
    )
