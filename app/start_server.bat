@echo off
cd /d .\360Car\app
E:\Miniconda3\envs\lotvue\python.exe -u -m uvicorn main:app --host 0.0.0.0 --port 8001 >> server_stdout.log 2>> server_stderr.log
