@echo off
REM Collect environment info for troubleshooting
cd /d "%~dp0"
title SignStudio Diagnose

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
    where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [ERROR] Python not found.
    pause
    exit /b 1
)
%PY% start.py --diag %*
echo.
echo Report file: _诊断报告.txt  (send this file for support)
pause
