@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"
for %%I in ("%~dp0..") do set "PROJECT_ROOT=%%~fI"
set "PROJECT_PYTHON=%PROJECT_ROOT%\.venv\Scripts\python.exe"
if not exist "%PROJECT_PYTHON%" (
  echo 未找到项目虚拟环境，无法确认和关闭服务。
  pause
  exit /b 1
)
cd /d "%PROJECT_ROOT%"
"%PROJECT_PYTHON%" -u "%PROJECT_ROOT%\scripts\app_control.py" stop
set "RESULT=%ERRORLEVEL%"
pause
exit /b %RESULT%
