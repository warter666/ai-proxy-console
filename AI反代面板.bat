@echo off
rem AI Proxy Hub - visual gateway panel (single instance via named mutex)
chcp 65001 >nul
title AI 反代面板
cd /d "%~dp0"

rem Python: AI_PROXY_PYTHON -> repo .venv -> venv -> PATH
set "PY=%AI_PROXY_PYTHON%"
if defined PY if not exist "%PY%" set "PY="
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY if exist "%~dp0venv\Scripts\python.exe" set "PY=%~dp0venv\Scripts\python.exe"
if not defined PY set "PY=python"
set "PYW=%PY:python.exe=pythonw.exe%"
if /i "%PY%"=="python" set "PYW=pythonw"

"%PY%" -c "import webview" >nul 2>nul
if errorlevel 1 (
  echo [!] 缺少 pywebview，改用命令行开关。
  "%PY%" "%~dp0ai_hub.py" menu
  if errorlevel 1 pause
  exit /b 1
)

start "" "%PYW%" "%~dp0gateway_ui.py"
exit /b 0
