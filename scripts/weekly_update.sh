#!/usr/bin/env bash
# 周更 + 自动推送（crontab 每周五 23:00 调用；也可手动执行）
#   1. 跑周更管线（刷新 → 筛选 → 筛后评分 → 每池 TOP20 → 导出三件套）
#   2. 生成运营素材包到 运营/素材库/榜单/（仅本地，失败不影响周更）
#   3. 成功且 web/data.js 有变化 → commit + push → GitHub Pages 自动重新部署
# 管线失败则整体非零退出，不提交不推送（线上保持上一期数据）。
set -euo pipefail
cd "$(dirname "$0")/.."

LOG=data/pipeline_cron.log
echo "[$(date '+%F %T')] === 周更推送脚本开始 ===" >> "$LOG"
.venv/bin/python -m fundscorer.pipeline >> "$LOG" 2>&1

# 运营素材包（小红书/公众号取材）：写进 gitignore 的 运营/ 下；失败只记警告
.venv/bin/python scripts/content_digest.py >> "$LOG" 2>&1 \
  || echo "[$(date '+%F %T')] [警告] 运营素材包生成失败（不影响周更推送）" >> "$LOG"

# 防误入库守卫：暂存区任何路径命中「运营/」即中止 —— 运营归档永不 push
if git diff --cached --name-only | grep -q '^运营/'; then
  echo "[$(date '+%F %T')] [错误] 暂存区含 运营/ 路径，中止推送；请 git reset 后排查" >> "$LOG"
  exit 1
fi

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
