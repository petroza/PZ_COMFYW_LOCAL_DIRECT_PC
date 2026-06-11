@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title PZ ComfyW Local Direct - Web only

REM Pouze lokalni web. Comfy nerestartuje.
REM Pro bezny start pouzivej START_ALL.cmd.

if not exist data mkdir data
if not exist data\inputs mkdir data\inputs
if not exist data\outputs mkdir data\outputs
if not exist data\tmp mkdir data\tmp
if not exist data\logs mkdir data\logs

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\STOP_LOCAL_SERVER.ps1" >nul 2>nul

where py >nul 2>nul
if %errorlevel%==0 (
  set "PY=py -3"
) else (
  set "PY=python"
)

echo ============================================================
echo   PZ ComfyW Local Direct - jen web / bez restartu Comfy
echo ============================================================
echo Kontroluji Python moduly...
%PY% -c "import requests, websocket" >nul 2>nul
if errorlevel 1 (
  echo Instaluji requests a websocket-client...
  %PY% -m pip install --upgrade requests websocket-client
)

echo.
echo Spoustim lokalni web...
echo Otevri: http://127.0.0.1:8765
echo Comfy API default: http://127.0.0.1:8000
%PY% local_server.py
pause
