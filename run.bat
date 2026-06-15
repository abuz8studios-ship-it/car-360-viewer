@echo off
title LOTVUE - 360 Car Viewer
cd /d .\360Car\app
echo Starting LOTVUE on port 8001...
E:\Miniconda3\envs\lotvue\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8001 --reload
