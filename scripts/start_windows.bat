@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"
for %%I in ("%~dp0..") do set "PROJECT_ROOT=%%~fI"
set "PROJECT_PYTHON=%PROJECT_ROOT%\.venv\Scripts\python.exe"
if not exist "%PROJECT_PYTHON%" (
  echo 未找到项目虚拟环境，请先按照 README 安装依赖。
  pause
  exit /b 1
)
cd /d "%PROJECT_ROOT%"
"%PROJECT_PYTHON%" -u "%PROJECT_ROOT%\scripts\app_control.py" start
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
  echo 启动失败，请查看上方提示。
  pause
  exit /b %RESULT%
)
if defined DOUBAO_DATA_DIR (
  echo 已使用自定义数据目录；现有 Windows 桌面助手只支持默认数据目录，打开文件夹功能暂不可用。
) else (
  if exist "%PROJECT_ROOT%\start_desktop_bridge.bat" (
    call "%PROJECT_ROOT%\start_desktop_bridge.bat"
    echo 已请求启动 Windows 桌面助手。
  ) else (
    echo 未找到 Windows 桌面助手，打开文件夹功能暂不可用。
  )
)
echo 已请求打开工作台。关闭此窗口不会关闭后台服务。
pause
exit /b 0
