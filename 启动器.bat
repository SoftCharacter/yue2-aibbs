@echo off
setlocal
title YuE2 Studio
cd /d %~dp0

set PYTHONUTF8=1
set PYTHONNOUSERSITE=1

rem 优先使用项目内虚拟环境，否则回退到系统 Python
set "PYEXE=python"
if exist "%~dp0venv\Scripts\python.exe" set "PYEXE=%~dp0venv\Scripts\python.exe"

rem 若 PATH 中无 ffmpeg，尝试使用项目内 ffmpeg
where ffmpeg >nul 2>nul
if errorlevel 1 (
    if exist "%~dp0ffmpeg\bin\ffmpeg.exe" set "PATH=%~dp0ffmpeg\bin;%PATH%"
)

"%PYEXE%" -m app.main
if errorlevel 1 (
    echo.
    echo [Error] Failed to start YuE2 Studio.
    echo         Please install dependencies first:  pip install -r requirements.txt
    echo         See README.md "Environment Setup" for details.
)
pause
