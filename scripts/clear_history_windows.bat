@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."
set "PYTHON=%CD%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo 未找到项目虚拟环境。请先按 README 创建并安装依赖。
  pause
  exit /b 1
)
"%PYTHON%" "%~dp0app_control.py" clear-history
set "RESULT=%ERRORLEVEL%"
echo.
echo 当前仅提供只读预览，历史清理尚不可用。
pause
exit /b %RESULT%
