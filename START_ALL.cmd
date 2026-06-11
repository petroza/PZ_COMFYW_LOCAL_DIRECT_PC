@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title PZ ComfyW Local Direct - SAFE START

REM ============================================================
REM  PZ ComfyW Local Direct - SAFE START
REM  1) Bezpecne ukonci stare Comfy / backend na portu 8000.
REM  2) Spusti Comfy znovu jen jako API backend na 127.0.0.1:8000.
REM  3) Ukonci starou instanci local_server.py, aby nebyl obsazeny port 8765.
REM  4) Spusti lokalni web aplikaci.
REM ============================================================

if not exist data mkdir data
if not exist data\inputs mkdir data\inputs
if not exist data\outputs mkdir data\outputs
if not exist data\tmp mkdir data\tmp
if not exist data\logs mkdir data\logs

echo ============================================================
echo   PZ ComfyW Local Direct - SAFE START
echo ============================================================
echo Comfy se VZDY restartuje, aby nebezelo nic stareho v pameti.
echo Spousti se jen API backend, ne Comfy Desktop GUI.
echo.

call "%~dp0START_COMFY.bat"
set "COMFY_RC=%errorlevel%"
if not "%COMFY_RC%"=="0" (
  echo.
  echo [POZOR] Comfy se nemusi spravne spustit. Web otevru i tak,
  echo ale v aplikaci muze byt Comfy offline, dokud neopravite cestu v config.json.
  echo.
)

echo Cistim starou instanci lokalni aplikace na portu 8765...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\STOP_LOCAL_SERVER.ps1" >nul 2>nul

where py >nul 2>nul
if %errorlevel%==0 (
  set "PY=py -3"
) else (
  set "PY=python"
)

echo Kontroluji Python moduly pro lokalni web...
%PY% -c "import requests, websocket" >nul 2>nul
if errorlevel 1 (
  echo Instaluji requests a websocket-client...
  %PY% -m pip install --upgrade requests websocket-client
)

echo.
echo Spoustim lokalni web...
echo Otevri: http://127.0.0.1:8765
echo Log Comfy: data\logs\comfy_safe_restart.log
echo Log aplikace: data\logs\local_server.log
echo.
%PY% local_server.py
pause
