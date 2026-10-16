#!/usr/bin/env bash
# 周更 + 自动推送（crontab 每周五 23:00 调用；也可手动执行）
#   1. 跑周更管线（刷新 → 筛选 → 筛后评分 → TOP20 → 导出三件套）
#   2. 成功且 web/data.js 有变化 → commit + push → GitHub Pages 自动重新部署
# 管线失败则整体非零退出，不提交不推送（线上保持上一期数据）。
set -euo pipefail
cd "$(dirname "$0")/.."

LOG=data/pipeline_cron.log
echo "[$(date '+%F %T')] === 周更推送脚本开始 ===" >> "$LOG"
.venv/bin/python -m fundscorer.pipeline >> "$LOG" 2>&1

# 只提交周更产物（data.js 随每周榜单变化；index.html 仅手动改动）
git add web/data.js
if git diff --cached --quiet; then
  echo "[$(date '+%F %T')] data.js 无变化，跳过推送" >> "$LOG"
  exit 0
fi
git -c user.name="fund-screener-bot" -c user.email="fund-screener-bot@users.noreply.github.com" \
    commit -m "data: 周更榜单 $(date +%F)" >> "$LOG" 2>&1
git push >> "$LOG" 2>&1
echo "[$(date '+%F %T')] 已推送，Pages 将自动重新部署" >> "$LOG"
