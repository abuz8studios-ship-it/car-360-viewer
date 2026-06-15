@echo off
cd /d .\360Car\app
E:\Miniconda3\envs\abu-mvp\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000 > .\360Car\server.log 2>&1
