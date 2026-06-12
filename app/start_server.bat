@echo off
cd /d E:\ABU\360Car\app
REM LotVue360 runs on :8360 — :8001 belongs to the Nemo brain (llama-server).
E:\Miniconda3\envs\lotvue\python.exe -u -m uvicorn main:app --host 0.0.0.0 --port 8360 >> server_stdout.log 2>> server_stderr.log
