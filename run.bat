@echo off
setlocal
cd /d "%~dp0"
start "ArtWebs NewGen" powershell.exe -NoLogo -NoExit -ExecutionPolicy Bypass -File "%~dp0run.ps1"
exit /b 0
