@echo off
setlocal EnableExtensions EnableDelayedExpansion
REM 周五 18:00 指数系段级更新 Windows 入口（2026-11-06 周更排期拍板）
REM 用 Git Bash 跑 scripts/weekly_index_update.sh（只刷 eq+market 段，
REM 不碰债基榜与榜史；守卫与"只 add web/data.js"口径同周更脚本）。
REM 失败弹 toast（同 weekly_update_win.cmd 的通知口径）。
cd /d "%~dp0.."
set "LOG=data\pipeline_cron.log"
set "BASH=D:\git\Git\bin\bash.exe"
if not exist "%BASH%" set "BASH=bash"

echo [%DATE% %TIME%] weekly_index_update_win.cmd start >> "%LOG%"
"%BASH%" -lc "cd '/c/Users/Lenovo/Desktop/基金选择指南' && bash scripts/weekly_index_update.sh" >> "%LOG%" 2>&1
set "RC=!ERRORLEVEL!"

if not "!RC!"=="0" (
  echo [%DATE% %TIME%] 指数系段级更新失败 rc=!RC! >> "%LOG%"
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0notify_fail.ps1" -Msg "指数系周更失败 rc=!RC!，请查 data\pipeline_cron.log" >> "%LOG%" 2>&1
)
echo [%DATE% %TIME%] weekly_index_update_win.cmd exit=!RC! >> "%LOG%"
exit /b !RC!
