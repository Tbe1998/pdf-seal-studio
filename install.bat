@echo off
REM 一键安装依赖（Windows）。双击或在 cmd 里运行： install.bat
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 没找到 python，请先安装 Python 3.10+ 并勾选 "Add to PATH"
  pause & exit /b 1
)

if not exist ".venv" (
  echo ^>^> 创建虚拟环境 .venv
  python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install -U pip -i https://pypi.tuna.tsinghua.edu.cn/simple
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

echo.
echo 安装完成。运行方式：
echo    .venv\Scripts\python.exe multisign.py --help
echo    或先执行 .venv\Scripts\activate.bat 再 python multisign.py --help
pause
