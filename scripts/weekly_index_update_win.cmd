@echo off
setlocal EnableExtensions EnableDelayedExpansion
REM Friday 18:00 index-section update entry (schedule decided 2026-11-06).
REM Runs scripts/weekly_index_update.sh in Git Bash: only eq+market sections,
REM bond pools and rank history untouched; guard and "only add web/data.js"
REM follow weekly_update.sh; failure pops toast via notify_fail.ps1.
cd /d "%~dp0.."
set "LOG=data\pipeline_cron.log"
set "BASH=D:\git\Git\bin\bash.exe"
if not exist "%BASH%" set "BASH=bash"

echo [%DATE% %TIME%] weekly_index_update_win.cmd start >> "%LOG%"
"%BASH%" -lc "cd '/c/Users/Lenovo/Desktop/基金选择指南' && bash scripts/weekly_index_update.sh" >> "%LOG%" 2>&1
set "RC=!ERRORLEVEL!"

if not "!RC!"=="0" (
  echo [%DATE% %TIME%] index section update failed rc=!RC! >> "%LOG%"
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0notify_fail.ps1" -Msg "index weekly failed rc=!RC!, check data\pipeline_cron.log" >> "%LOG%" 2>&1
)
echo [%DATE% %TIME%] weekly_index_update_win.cmd exit=!RC! >> "%LOG%"
exit /b !RC!
