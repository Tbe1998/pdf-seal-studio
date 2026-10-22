@echo off
REM Windows 启动器：先 python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
setlocal
set HERE=%~dp0
if exist "%HERE%.venv\Scripts\python.exe" (
    "%HERE%.venv\Scripts\python.exe" "%HERE%multisign.py" %*
) else (
    python "%HERE%multisign.py" %*
)
