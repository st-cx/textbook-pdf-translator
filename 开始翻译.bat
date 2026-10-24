@echo off
chcp 65001 >nul
setlocal
set "TASK_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%TASK_PYTHON%" set "TASK_PYTHON=python"
"%TASK_PYTHON%" "%~dp0开始翻译.py" %*
if not errorlevel 1 exit /b 0
echo.
echo 界面未能启动。请先按使用说明安装 Python 和依赖，再重新打开。
pause
exit /b 1
