@echo off
setlocal EnableExtensions EnableDelayedExpansion
REM 周更 Windows 入口（2026-10-30 修订：修 %ERRORLEVEL% 解析、把 bash 输出也落盘）
REM 用 Git Bash 跑原有 scripts/weekly_update.sh，不改其守卫与"只 add web/data.js"逻辑。
REM 成功后额外存一份「本周五基准快照」，供当晚文案做名次对比（scripts/weekly_baseline.py）。
cd /d "%~dp0.."
set "LOG=data\pipeline_cron.log"
set "BASH=D:\git\Git\bin\bash.exe"
if not exist "%BASH%" set "BASH=bash"

echo [%DATE% %TIME%] weekly_update_win.cmd start >> "%LOG%"
"%BASH%" -lc "cd '/c/Users/Lenovo/Desktop/基金选择指南' && bash scripts/weekly_update.sh" >> "%LOG%" 2>&1
set "RC=!ERRORLEVEL!"

if "!RC!"=="0" (
  ".venv\Scripts\python.exe" scripts\weekly_baseline.py save >> "%LOG%" 2>&1
  echo [%DATE% %TIME%] weekly done rc=!RC! >> "%LOG%"
) else (
  echo [%DATE% %TIME%] 周更失败 rc=!RC!，未存基准 >> "%LOG%"
)
echo [%DATE% %TIME%] weekly_update_win.cmd exit=!RC! >> "%LOG%"
exit /b !RC!
