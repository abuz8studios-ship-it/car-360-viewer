# PyInstaller spec — LotVue360 STANDALONE (runs on any Windows PC, no Python).
# Bundles the Python runtime + the full server's third-party deps. The app's own
# .py modules + static + writable data ship as a REAL app/ folder next to the exe
# (assembled post-build), so main.py's relative paths and data persistence work.

from PyInstaller.utils.hooks import collect_all, collect_submodules

env_bin = r"E:\Miniconda3\envs\lotvue\Library\bin"

# conda-Python C-extensions need these native DLLs from Library\bin, which
# PyInstaller doesn't auto-discover (pyexpat->expat, _ctypes->ffi, _ssl->ssl/crypto,
# _sqlite3->sqlite3, _bz2->bz2, _lzma->lzma).
_support_dlls = ["ffi-8.dll", "ffi.dll", "libexpat.dll", "expat.dll",
                 "libssl-3-x64.dll", "libcrypto-3-x64.dll", "sqlite3.dll",
                 "libbz2.dll", "liblzma.dll", "zlib.dll"]
datas, binaries, hiddenimports = [], [
    (env_bin + "\\" + d, ".") for d in _support_dlls
], []

# Bundle every third-party lib main.py + the LotVue modules import.
for pkg in ("fastapi", "starlette", "uvicorn", "pydantic", "pydantic_core",
            "cv2", "numpy", "PIL", "jose", "stripe", "reportlab", "qrcode",
            "multipart", "pillow_heif", "anyio", "sniffio", "click", "h11",
            "certifi", "charset_normalizer", "idna", "ecdsa", "rsa", "pyasn1",
            "email_validator", "dotenv", "annotated_types", "typing_extensions"):
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
    except Exception:
        pass

hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("fastapi")
hiddenimports += ["tkinter", "pystray._win32", "PIL.Image", "PIL.ImageDraw",
                  "qrcode.image.pil", "reportlab.graphics.barcode",
                  "uvicorn.loops.asyncio", "uvicorn.protocols.http.h11_impl",
                  "uvicorn.protocols.websockets.websockets_impl",
                  "uvicorn.lifespan.on", "anyio._backends._asyncio"]

datas += [("static/desktop/lotvue.png", "static/desktop"),
          ("static/desktop/lotvue.ico", "static/desktop")]

block_cipher = None

a = Analysis(
    ["lotvue_standalone.py"],
    pathex=[r"E:\ABU\360Car\app"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # heavy GPU-only libs: the app degrades gracefully without them (try/except),
    # and a target PC has neither the GPU nor the models, so excluding keeps the
    # bundle lean and the build reliable.
    excludes=["torch", "torchvision", "torchaudio", "rembg", "onnxruntime",
              "scipy", "matplotlib", "kokoro", "transformers", "tensorflow",
              "pandas", "IPython", "notebook", "sympy"],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name="LotVue360",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False,
    icon="static\\desktop\\lotvue.ico",
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, strip=False, upx=False,
               name="LotVue360_standalone")
