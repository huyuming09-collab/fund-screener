@echo off
REM 周更 Windows 入口（2026-10-29 建立）：用 Git Bash 跑原有 scripts/weekly_update.sh，
REM 不复制、不改写其逻辑（守卫 / 只 add web/data.js / push 重试 全部沿用）。
setlocal
cd /d "%~dp0.."
echo [%DATE% %TIME%] weekly_update_win.cmd start >> data\pipeline_cron.log
"D:\git\Git\bin\bash.exe" -lc "cd '/c/Users/Lenovo/Desktop/基金选择指南' && bash scripts/weekly_update.sh"
set RC=%ERRORLEVEL%
echo [%DATE% %TIME%] weekly_update_win.cmd exit=%RC% >> data\pipeline_cron.log
exit /b %RC%
