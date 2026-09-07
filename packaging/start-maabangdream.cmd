@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 启动前静默检查 GitHub 更新；离线或已是最新时直接继续。
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\update.ps1" -Auto
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-release.ps1"
if errorlevel 1 (
  echo.
  echo 启动失败，请保留本窗口中的错误信息。
  pause
)
