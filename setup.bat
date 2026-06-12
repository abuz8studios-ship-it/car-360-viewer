@echo off
REM ============================================
REM Dreams360ai — Setup Script
REM Run this ONCE before starting the server
REM ============================================

echo [1/4] Installing Python dependencies...
E:\Miniconda3\envs\abu-mvp\python.exe -m pip install fastapi uvicorn python-multipart Pillow --quiet
echo      Done.

echo [2/4] Attempting HEIC support (optional — OK if it fails)...
E:\Miniconda3\envs\abu-mvp\python.exe -m pip install pillow-heif --quiet 2>nul
if %ERRORLEVEL% EQU 0 (
    echo      HEIC support installed.
) else (
    echo      HEIC not available — iPhone users need "Most Compatible" camera mode.
)

echo [3/4] Verifying FFmpeg...
E:\ABU\AI\ffmpeg\ffmpeg.exe -version >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo      FFmpeg OK.
) else (
    echo      ERROR: FFmpeg not found at E:\ABU\AI\ffmpeg\ffmpeg.exe
    echo      Fix the FFMPEG path in main.py
)

E:\ABU\AI\ffmpeg\ffprobe.exe -version >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo      ffprobe OK.
) else (
    echo      ERROR: ffprobe not found at E:\ABU\AI\ffmpeg\ffprobe.exe
    echo      Fix the FFPROBE path in main.py
)

echo [4/4] Testing imports...
E:\Miniconda3\envs\abu-mvp\python.exe -c "import fastapi, uvicorn, PIL; print('      All imports OK.')"

echo.
echo ============================================
echo Setup complete. To start the server:
echo   E:\ABU\360Car\run.bat
echo Then open http://localhost:8000
echo ============================================
pause
