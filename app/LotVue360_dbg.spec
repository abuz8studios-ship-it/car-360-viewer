# Debug build — console=True so we can SEE what the launcher does.
env_bin = r"E:\Miniconda3\envs\lotvue\Library\bin"
datas = [("static/desktop/lotvue.png", "static/desktop"),
         ("static/desktop/lotvue.ico", "static/desktop")]
binaries = [(env_bin + r"\ffi-8.dll", "."), (env_bin + r"\ffi.dll", ".")]
hiddenimports = ["pystray._win32", "PIL.Image", "PIL.ImageDraw"]
a = Analysis(["lotvue_desktop.py"], pathex=[r"E:\ABU\360Car\app"],
    binaries=binaries, datas=datas, hiddenimports=hiddenimports,
    excludes=["torch","cv2","rembg","onnxruntime","numpy","scipy","matplotlib",
              "kokoro","reportlab","stripe","webview","pythonnet","clr"])
pyz = PYZ(a.pure, a.zipped_data)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="LotVue360_dbg",
    debug=False, console=True, icon="static\\desktop\\lotvue.ico")
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, name="LotVue360_dbg")
