@echo off
rem One-click start of SignalPilot on Windows.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-windows.ps1"
if errorlevel 1 (
  echo.
  echo Start failed - see the message above.
)
pause
