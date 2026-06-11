@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title PZ ComfyW - Safe Comfy API restart

echo ============================================================
echo   PZ ComfyW - SAFE COMFY API RESTART
echo ============================================================
echo Ukoncim stare Comfy, spustim ho znovu jen jako API backend.
echo Port: http://127.0.0.1:8000
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\START_COMFY_SAFE.ps1"
set "RC=%errorlevel%"
if not "%RC%"=="0" (
  echo.
  echo [POZOR] Comfy start vratil kod %RC%.
  echo Podrobnosti jsou v data\logs\comfy_safe_restart.log
  echo Pokud je spatna cesta, uprav config.json sekci comfy_start.main_py a python_exe.
)
exit /b %RC%
