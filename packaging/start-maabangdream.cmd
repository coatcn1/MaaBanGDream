@echo off
rem Batch files are parsed with the system ANSI codepage (GBK on zh-CN), so
rem keep this launcher ASCII-only.  Any UTF-8 Chinese here gets mis-decoded
rem and executed as a command on first launch.
chcp 65001 >nul
cd /d "%~dp0"
rem Normalize a versioned install folder after an update. Network update checks
rem are owned by MFA's native VersionChecker after v1.3.6.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\normalize-release-directory.ps1"
rem Exit code 2 means the installer folder is being renamed and relaunched;
rem do not start MFA from the old path in that case.
if errorlevel 2 exit /b 2
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-release.ps1"
if errorlevel 1 (
  echo.
  echo Launch failed. Keep this window open and report the error above.
  pause
)
