@echo off
REM =====================================================================
REM  SignStudio launcher (Windows). Double click this file.
REM =====================================================================
cd /d "%~dp0"
title SignStudio

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
    where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo.
    echo [ERROR] Python not found on this computer.
    echo         Please install Python 3.10 or newer:
    echo         https://www.python.org/downloads/
    echo         IMPORTANT: tick "Add python.exe to PATH" during setup.
    echo.
    pause
    exit /b 1
)

%PY% start.py %*
if errorlevel 1 (
    echo.
    echo ---------------------------------------------------------------
    echo  Startup failed. Please run DIAGNOSE.bat and send the report.
    echo ---------------------------------------------------------------
    pause
)
