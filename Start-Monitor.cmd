@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
  if errorlevel 1 goto fail
)
".venv\Scripts\python.exe" -c "import sys, streamlit, akshare, tommy_capital; assert sys.version_info >= (3,11); assert (1,50) <= tuple(map(int,streamlit.__version__.split('.')[:2])) < (2,0)" >nul 2>&1
if errorlevel 1 (
  ".venv\Scripts\python.exe" -m pip install -e ".[ui]"
  if errorlevel 1 goto fail
)
".venv\Scripts\python.exe" -m streamlit run monitor_app.py --server.address 127.0.0.1 --server.port 8501 --server.headless false
if errorlevel 1 goto fail
exit /b 0
:fail
echo Startup failed. Install Python 3.11 or newer, then retry. See docs/monitor-windows.md.
pause
exit /b 1
