@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 Python，请先安装 Python 3.10+ 并加入 PATH。
  pause
  exit /b 1
)

where node >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 Node.js，请先安装 Node.js 16+。
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] 创建虚拟环境...
  python -m venv .venv
  if errorlevel 1 ( pause & exit /b 1 )
) else (
  echo [1/3] 使用已有虚拟环境 .venv
)

echo [2/3] 安装 Python 依赖...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 ( pause & exit /b 1 )

echo [3/3] 启动控制台：http://localhost:8420/
".venv\Scripts\python.exe" main.py --open

if errorlevel 1 pause
endlocal
