#!/usr/bin/env bash
# 周更任务统一入口（2026-11-08：绕开 cmd.exe 批处理解析碎裂问题）
# 用法: weekly_auto.sh update   -> 全量周更（周日 17:00 任务用，2026-11-08 由19:00改17:00留故障余量）
#       weekly_auto.sh index    -> 指数系段级（周五 18:00 任务用，2026-11-08 定保持18:00）
# 失败: 调 notify_fail.ps1 弹 toast（保留原 win.cmd 的通知口径）
set -u
cd "$(dirname "$0")/.."
LOG=data/pipeline_cron.log

case "${1:-}" in
  update) SCRIPT=scripts/weekly_update.sh ;;
  index)  SCRIPT=scripts/weekly_index_update.sh ;;
  *) echo "usage: weekly_auto.sh update|index" >> "$LOG"; exit 2 ;;
esac

echo "[$(date '+%F %T')] weekly_auto start ($1)" >> "$LOG"
bash "$SCRIPT"
RC=$?
if [ "$RC" -ne 0 ]; then
  echo "[$(date '+%F %T')] weekly_auto failed rc=$RC ($1)" >> "$LOG"
  powershell.exe -NoProfile -ExecutionPolicy Bypass \
    -File "scripts/notify_fail.ps1" -Msg "weekly_auto $1 failed rc=$RC, see data/pipeline_cron.log" \
    >> "$LOG" 2>&1 || true
fi
echo "[$(date '+%F %T')] weekly_auto exit=$RC ($1)" >> "$LOG"
exit $RC
