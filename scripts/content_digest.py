#!/usr/bin/env python3
"""周更 → 运营素材包（小红书/公众号写作取材）。

读取周更导出的 `data/top20.json`，生成**本地素材包**到
`运营/素材库/榜单/<ISO周>/`（`要点.md` + `小红书骨架.md` + `公众号骨架.md` + `数据切片.json`）。

设计约束：
- 输出目录在 `.gitignore` 的 `运营/` 之下，**永不入库**（素材只服务本机内容生产）；
- 数字一律取自当期 payload，不手抄旧文——防止"旧图新发/数字对不上"；
- 纯标准库、无网络依赖（与 tests 约定一致）；
- 任何一环失败都只影响本次生成，不碰管线产物（周更脚本里用 `|| true` 调用）。

用法：
    python scripts/content_digest.py
    python scripts/content_digest.py --watch 006980,110017 --top 10
    python scripts/content_digest.py --payload data/top20.json --out 运营/素材库/榜单 --week 2026-W42
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PAYLOAD = ROOT / "data" / "top20.json"
DEFAULT_OUT = ROOT / "运营" / "素材库" / "榜单"
DEFAULT_WATCH = "006980"          # 项目锚点样本（国寿安保泰恒纯债）
POOL_LABEL = {"short_bond": "短债", "long_bond": "中长债"}


def _pct(x) -> str:
    return "—" if x is None else f"{x * 100:.2f}%"


def _num(x, fmt: str = "{:.2f}") -> str:
    return "—" if x is None else fmt.format(x)


def _delta_col(fund: dict, has_prev: bool) -> str:
    if not has_prev:
        return "首期—"
    if fund.get("is_new"):
        return "新增"
    d = fund.get("rank_delta")
    if d is None:
        return "—"
    d = int(d)
    return f"↑{d}" if d > 0 else (f"↓{-d}" if d < 0 else "—")


def _m(fund: dict, key: str):
    return (fund.get("metrics") or {}).get(key)


def _top_table(funds: list[dict], has_prev: bool, top_n: int) -> str:
    head = "| 名次 | 代码 | 名称 | 分数 | 环比 | 合并规模 | 近1年 | 近5年回撤 | 夏普 |\n" \
           "|---:|---|---|---:|---|---:|---:|---:|---:|"
    rows = [head]
    for f in funds[:top_n]:
        rows.append(
            "| {rank} | {code} | {name} | {score} | {delta} | {scale} | {r1} | {mdd} | {sh} |".format(
                rank=int(f.get("rank") or 0),
                code=f.get("code", ""),
                name=f.get("name", ""),
                score=_num(f.get("score"), "{:.1f}"),
                delta=_delta_col(f, has_prev),
                scale=_num((f.get("screening") or {}).get("scale_sum")),
                r1=_pct(_m(f, "ret_1y")),
                mdd=_pct(_m(f, "mdd_5y")),
                sh=_num(_m(f, "sharpe_5y")),
            ))
    return "\n".join(rows)


def _movement(funds: list[dict], has_prev: bool) -> str:
    if not has_prev:
        return "该池本期为**首期**（无上一期历史），不显示环比与新增——下期起才有 ↑↓/新增。"
    up = [(int(f["rank"]), f["name"], int(f["rank_delta"])) for f in funds
          if f.get("rank_delta") and int(f["rank_delta"]) > 0 and not f.get("is_new")]
    down = [(int(f["rank"]), f["name"], int(f["rank_delta"])) for f in funds
            if f.get("rank_delta") and int(f["rank_delta"]) < 0 and not f.get("is_new")]
    new = [f["name"] for f in funds if f.get("is_new")]
    if not (up or down or new):
        return "本期名次零变动（20 只与上期完全一致）。"
    lines = []
    if up:
        lines.append("- 上升：" + "、".join(
            f"{n} 第{r}名↑{d}" for r, n, d in sorted(up, key=lambda t: -t[2])))
    if down:
        lines.append("- 下降：" + "、".join(
            f"{n} 第{r}名↓{-d}" for r, n, d in sorted(down, key=lambda t: t[2])))
    if new:
        lines.append("- 新增：" + "、".join(new))
    return "\n".join(lines)


def _watch_lines(payload: dict, watch: list[str]) -> str:
    lines = []
    for code in watch:
        hits = [f for f in payload.get("funds", []) if f.get("code") == code]
        if hits:                       # 同一代码可能同时进两池，逐池都报
            for f in sorted(hits, key=lambda g: str(g.get("category"))):
                pool = POOL_LABEL.get(f.get("category"), f.get("category", ""))
                lines.append(f"- {code} {f.get('name')}：{pool}榜**第 {int(f['rank'])} 名**"
                             f"（{_num(f.get('score'), '{:.1f}')} 分）")
        else:
            lines.append(f"- {code}：**本期未入榜**（两池 TOP20 均无此代码）")
    return "\n".join(lines) or "（未指定跟踪代码：`--watch 006980,...`）"


def build_digest(payload: dict, week: str, watch: list[str], top_n: int) -> dict:
    """返回 {文件名: 文本} 的素材包内容（不落盘，便于单测）。"""
    asof = payload.get("asof", "")
    disclaimer = payload.get("disclaimer", "")
    pools = payload.get("pools") or {}
    cats = [c for c in (payload.get("categories") or [])
            if c.get("enabled") and c.get("id") in pools] or list(pools)
    by_pool = {}
    for f in payload.get("funds", []):
        cat = f.get("category") or payload.get("category") or "short_bond"
        by_pool.setdefault(cat, []).append(f)
    for cat in by_pool:
        by_pool[cat].sort(key=lambda f: int(f.get("rank") or 0))

    # ---------- 要点.md ----------
    L = [f"# 周榜要点包 {week}（数据基准 {asof}）",
         "",
         "> 由 `scripts/content_digest.py` 从当期 `data/top20.json` 生成；**数字勿手抄、勿用于非本期内容**。",
         f"> 模型 {payload.get('model_version', '')} · 生成时间 {payload.get('generated_at', '')}",
         "",
         "## 一、两池概览",
         "",
         "| 池 | 榜单 | 筛出候选 | 漏斗（本期） |",
         "|---|---:|---:|---|"]
    for c in cats:
        pid, label = c.get("id"), c.get("name", c.get("id"))
        ps = pools.get(pid, {})
        funnel = ps.get("funnel") or payload.get("funnel") or {}
        funnel_txt = " → ".join(f"{k} {v}" for k, v in funnel.items())
        L.append(f"| {label} | {ps.get('n', len(by_pool.get(pid, [])))} 只 | "
                 f"{ps.get('n_screened', '—')} 只 | {funnel_txt} |")

    emitted = False
    for c in cats:
        pid, label = c.get("id"), c.get("name", c.get("id"))
        ps, funds = pools.get(pid, {}), by_pool.get(pid, [])
        has_prev = bool(ps.get("has_prev"))
        if not emitted:
            L += ["", "## 二、榜单（双池 TOP10）"]
            emitted = True
        L += ["", f"### {label}榜 TOP{min(top_n, len(funds))}", ""]
        if not funds:
            L.append("（该池本期无榜单）")
            continue
        L.append(_top_table(funds, has_prev, top_n))
        L += ["", f"### {label}名次变动", "", _movement(funds, has_prev)]

    L += ["", "## 三、重点跟踪（`--watch` 指定代码）", "", _watch_lines(payload, watch)]
    top1 = next((f for pid in by_pool for f in by_pool[pid][:1]), None)
    titles = []
    if top1:
        titles.append(f"{POOL_LABEL.get(top1.get('category'), '本期')}榜："
                      f"{top1.get('name')} {_num(top1.get('score'), '{:.1f}')} 分登顶（{asof}）")
    if top1:
        titles.append(f"规模 {_num((top1.get('screening') or {}).get('scale_sum'))} 亿也能排第一？"
                      f"周榜第 1 名拆解")
    titles.append(f"945 → {pools.get('long_bond', {}).get('n_screened', '—')}："
                  f"中长债池是怎么被筛到只有几十只的")
    L += ["", "## 四、本期可写角度（标题候选）", ""]
    L += [f"{i + 1}. {t}" for i, t in enumerate(titles)]
    L += ["", "---", "", f"**免责声明（发布时必须保留）**：{disclaimer}"]
    out = {"要点.md": "\n".join(L) + "\n"}

    # ---------- 小红书骨架.md ----------
    xhs = [f"""---
平台: 小红书
选题: 榜单解读
状态: 草稿
发布日期:
链接: ""
引用数据期数: {asof}
首图: ""
---

# <标题：从「要点.md §四 标题候选」里选，≤20 字>

<开头 2 行：直接抛结论（榜首/最大变化/锚点基金名次）>

## 本周变化（挑 1~2 个点，别贪多）
<复制「要点.md §二 名次变动」里最有故事的一条>

## 为什么（一句话方法论，不展开）
<例：先筛后评——先过硬条件再按分数排，分位基准是全池不是候选集。>

## 数据来源
本期榜单：短债 TOP20 + 中长债 TOP20，数据基准 {asof}，来自开源基金评分项目
github.com/huyuming09-collab/fund-screener。

<免责声明：{disclaimer}>

#基金 #债券基金 #基金筛选 #理财 #中长债
"""]
    out["小红书骨架.md"] = xhs[0]

    # ---------- 公众号骨架.md ----------
    out["公众号骨架.md"] = f"""---
平台: 公众号
选题: 榜单解读
状态: 草稿
发布日期:
链接: ""
引用数据期数: {asof}
首图: ""
---

# <标题：结论 + 数字，≤22 字（候选见「要点.md §四」）>

> 一句话导语：本期两池的关键变化 + 读者能得到什么。

## 一、本期榜单
（两池 TOP10 表格：直接用「要点.md §二」的表，不手抄）

## 二、变化解读
（挑 2~3 条名次变化讲"为什么"；数据从「要点.md §二 名次变动」取）

## 三、方法论一分钟
（可链公开仓 docs/01 评分指南、docs/07 筛选标准、docs/09 中长债摘要；
完整回测推演写"见公众号历史文章"，不要说仓库里有）

## 四、数据与口径
- 数据基准：{asof}；复权净值口径；天天基金公开数据
- 榜单与页码：github.com/huyuming09-collab/fund-screener（周榜页可直接打开）

## 风险提示
{disclaimer}
"""

    # ---------- 数据切片.json ----------
    slim = []
    for f in payload.get("funds", []):
        slim.append({
            "rank": f.get("rank"), "code": f.get("code"), "name": f.get("name"),
            "category": f.get("category"), "score": f.get("score"),
            "rank_prev": f.get("rank_prev"), "rank_delta": f.get("rank_delta"),
            "is_new": f.get("is_new"),
            "scale_sum": (f.get("screening") or {}).get("scale_sum"),
            "comp_pct": (f.get("screening") or {}).get("comp_pct"),
            "yearly_med": (f.get("screening") or {}).get("yearly_med"),
            "max_gain_5y": (f.get("screening") or {}).get("max_gain_5y"),
            "ret_1y": _m(f, "ret_1y"), "mdd_5y": _m(f, "mdd_5y"),
            "sharpe_5y": _m(f, "sharpe_5y"),
        })
    out["数据切片.json"] = json.dumps(
        {"week": week, "asof": asof, "model_version": payload.get("model_version"),
         "pools": pools, "funds": slim, "disclaimer": disclaimer},
        ensure_ascii=False, indent=1)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="周更 → 运营素材包（输出仅本地，不入库）")
    ap.add_argument("--payload", default=str(DEFAULT_PAYLOAD), help="周更导出的 top20.json")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="素材库根目录")
    ap.add_argument("--week", default=None, help="ISO 周标签，如 2026-W42（默认取 asof 所在周）")
    ap.add_argument("--top", type=int, default=10, help="表格收录名次数（默认 10）")
    ap.add_argument("--watch", default=DEFAULT_WATCH, help="重点跟踪代码，逗号分隔；空串关闭")
    args = ap.parse_args(argv)

    payload_path = Path(args.payload)
    if not payload_path.exists():
        print(f"[错误] 找不到 payload：{payload_path}（先跑周更管线）")
        return 1
    try:
        payload = json.loads(payload_path.read_text())
    except json.JSONDecodeError as e:
        print(f"[错误] payload 解析失败：{e}")
        return 1

    asof = str(payload.get("asof") or date.today().isoformat())
    week = args.week
    if not week:
        iso = date.fromisoformat(asof).isocalendar()
        week = f"{iso.year}-W{iso.week:02d}"
    watch = [c.strip() for c in (args.watch or "").split(",") if c.strip()]

    files = build_digest(payload, week, watch, args.top)
    target = Path(args.out) / week
    target.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (target / name).write_text(text, encoding="utf-8")

    print(f"[素材包] {target}")
    for name in files:
        print(f"  - {name}")
    print(f"  期数 asof={asof} · 榜单 {sum(len(v) for v in _by_pool(payload).values())} 只")
    return 0


def _by_pool(payload: dict) -> dict:
    out: dict = {}
    for f in payload.get("funds", []):
        out.setdefault(f.get("category") or payload.get("category") or "short_bond", []).append(f)
    return out


if __name__ == "__main__":
    sys.exit(main())
