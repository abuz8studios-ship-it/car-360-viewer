@echo off
cd /d E:\ABU\360Car\app
REM LotVue360 runs on :8360 - :8001 belongs to the Nemo brain (llama-server).
REM Self-healing loop: if uvicorn crashes/exits, wait 5s and relaunch.
REM Fixed: use python.exe directly with -m flag, no PATH dependency.
REM Fixed: handle window-CLOSE by detaching from console.

:loop
REM Check if port is already in use, if so skip restart
netstat -ano | findstr ":8360" | findstr "LISTENING" > nul
if %errorlevel%==0 (
    echo %date% %time% Port 8360 already in use, skipping restart >> watchdog.log
    timeout /t 10 /nobreak > nul
    goto loop
)

REM Start uvicorn detached from console (prevents window-CLOSE kills)
E:\Miniconda3\envs\lotvue\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8360 --log-level info >> server_stdout.log 2>> server_stderr.log
exit_code=%errorlevel%
echo %date% %time% uvicorn exited code %exit_code% -- restarting in 5s >> watchdog.log
timeout /t 5 /nobreak > nul
goto loop
