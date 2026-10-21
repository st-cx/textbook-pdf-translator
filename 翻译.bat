@echo off
chcp 65001 >nul
setlocal
set "TASK_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%TASK_PYTHON%" set "TASK_PYTHON=python"
"%TASK_PYTHON%" "%~dp0translate_pdf.py" %*
if errorlevel 1 echo 本次尚未完成，请查看上方提示。已通过的译文已保存在任务目录，可继续运行。
pause
