#!/usr/bin/env bash
# 周五 17:00 指数系段级更新（2026-11-06 排期拍板；2026-11-08 改 17:00"五点抓取"）
# 覆盖内容：指数温度计 / 红利天气 / 市场情绪（周六不交易 → 周五统一拉取，
# 供周五晚「红利天气周报」与周六晚「市场周报」两条内容共用）。
# 只刷 eq+market 段（pipeline --sections=eq,market），不碰债基筛选、CSV 与榜史；
# 守卫与"只 add web/data.js"口径同 weekly_update.sh。
set -euo pipefail
cd "$(dirname "$0")/.."
LOG=data/pipeline_cron.log

echo "[$(date '+%F %T')] weekly_index_update start" >> "$LOG"
# 解释器回退链（同 weekly_update.sh）：Linux 用 .venv/bin/python，Windows Git Bash 用 Scripts
PY=.venv/bin/python
[ -x "$PY" ] || PY=.venv/Scripts/python
"$PY" -m fundscorer.pipeline --sections=eq,market 2>&1 | tee -a "$LOG"

git add web/data.js
if git diff --cached --name-only | grep -q '^运营/'; then
  echo "FAIL: 运营/ 混入暂存区" | tee -a "$LOG"
  exit 1
fi
if git diff --cached --quiet; then
  echo "无 data.js 变化，跳过提交" | tee -a "$LOG"
  exit 0
fi
git commit -m "chore(周更): 周五指数系段级更新（温度计+红利天气+市场情绪）" >> "$LOG" 2>&1

rc=1
for i in $(seq 1 15); do
  if git push >> "$LOG" 2>&1; then rc=0; break; fi
  sleep 20
done
echo "[$(date '+%F %T')] weekly_index_update exit=$rc" >> "$LOG"
exit $rc
