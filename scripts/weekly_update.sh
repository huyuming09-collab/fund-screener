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

# 解释器选择：Linux/macOS 用 .venv/bin/python；Windows(venv 在 Git Bash 下) 回退 .venv/Scripts/python
PY=.venv/bin/python
[ -x "$PY" ] || PY=.venv/Scripts/python

# 净值新鲜度基线：记录运行前 nav_full 最新 mtime（epoch 秒），跑完比对——
# 2026-10-31 修：周五那次周更 1.5s 静默失败、净值全停在 10-29，但页面照标"今天"。
# 现在显式校验「这次到底有没有真的抓到新净值」，没有就大声报警。
NAV_DIR=data/nav_full
nav_latest_mtime() { find "$NAV_DIR" -name '*.pkl' -printf '%T@\n' 2>/dev/null | sort -n | tail -1; }
NAV_BEFORE=$(nav_latest_mtime)
NAV_BEFORE=${NAV_BEFORE:-0}
echo "[$(date '+%F %T')] [体检] 运行前 nav_full 最新 mtime epoch=$NAV_BEFORE ($(date -d @${NAV_BEFORE%.*} '+%F %T' 2>/dev/null || echo '?'))" >> "$LOG"

echo "[$(date '+%F %T')] [步骤] 启动管线 pipeline（含净值抓取）…" >> "$LOG"
PIPE_RC=0
"$PY" -m fundscorer.pipeline >> "$LOG" 2>&1 || PIPE_RC=$?
echo "[$(date '+%F %T')] [步骤] 管线退出码=$PIPE_RC" >> "$LOG"
if [ "$PIPE_RC" -ne 0 ]; then
  echo "[$(date '+%F %T')] [错误] 管线失败(rc=$PIPE_RC)，不提交不推送，线上保持上一期" >> "$LOG"
  exit "$PIPE_RC"
fi

# 净值是否真的更新了（防静默用旧数据）
NAV_AFTER=$(nav_latest_mtime)
NAV_AFTER=${NAV_AFTER:-0}
echo "[$(date '+%F %T')] [体检] 运行后 nav_full 最新 mtime epoch=$NAV_AFTER ($(date -d @${NAV_AFTER%.*} '+%F %T' 2>/dev/null || echo '?'))" >> "$LOG"
if [ "${NAV_AFTER%.*}" = "${NAV_BEFORE%.*}" ]; then
  echo "[$(date '+%F %T')] [警报] ⚠ nav_full 净值 mtime 未变化——本次可能未抓到新净值（用的旧缓存）！" >> "$LOG"
  echo "[$(date '+%F %T')] [警报] ⚠ 页面「数据基准」将如实显示旧净值日期（见 cards._true_asof），不会谎称今天。" >> "$LOG"
else
  echo "[$(date '+%F %T')] [体检] ✓ 净值已刷新（$NAV_BEFORE → $NAV_AFTER）" >> "$LOG"
fi

# 运营素材包（小红书/公众号取材）：写进 gitignore 的 运营/ 下；失败只记警告
"$PY" scripts/content_digest.py >> "$LOG" 2>&1 \
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
# push 带重试：2026-10-16 实际发生过单次 TLS 失败（GnuTLS recv error -110）导致静默漏推
for i in 1 2 3; do
  if git push >> "$LOG" 2>&1; then
    echo "[$(date '+%F %T')] 已推送，Pages 将自动重新部署" >> "$LOG"
    break
  fi
  echo "[$(date '+%F %T')] [警告] 第 $i 次推送失败，10s 后重试" >> "$LOG"
  if [ "$i" = 3 ]; then
    echo "[$(date '+%F %T')] [错误] 推送重试耗尽——本地已提交，下一次运行会携带重推" >> "$LOG"
    exit 1
  fi
  sleep 10
done
