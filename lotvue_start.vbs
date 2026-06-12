Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "E:\Miniconda3\envs\lotvue\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8001", 0, False
