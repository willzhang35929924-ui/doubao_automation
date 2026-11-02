@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title Doubao Dashboard Launcher
if not exist "%~dp0.tmp" mkdir "%~dp0.tmp"
call "%~dp0start_desktop_bridge.bat"
echo 正在启动豆包仪表盘，请稍候...
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo 错误：找不到项目 Python 环境：%~dp0.venv\Scripts\python.exe
  echo 请先安装项目依赖。
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -u "%~dp0launch_dashboard.py"
if errorlevel 1 (
  echo.
  echo 启动失败。请查看上方错误及 logs\dashboard_start_error.log。
  pause
  exit /b 1
)
echo.
echo 仪表盘已就绪：http://127.0.0.1:8782/
echo 如果浏览器未弹出，请手动打开以上地址。
echo 关闭此启动窗口不会停止后台服务。
pause
exit /b 0

