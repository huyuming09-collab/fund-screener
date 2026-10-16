# -*- coding: utf-8 -*-
"""短债两段式筛选：先筛后评（docs/07-短债筛选标准.md，用户 2026-10-15 基金级口径）。

流程：
  1. 份额合并（基金级口径）：同基金 A/B/C/D… 份额按基础名合并。
     - 现任经理 = 规模最大份额（代表份额）所记经理；
     - 经理从业/任本基金任期 = 同经理全部份额中的最大值
       （新发大份额（如长城短债D）成立晚、任期记录短，不能拖累全基金）；
     - 成立年限 = 全部份额中的最大值；
     - 业绩（近5y/3y收益回撤）与年度收益 = 历史最久份额（并列优先 A 类）口径。
  2. 硬条件 S1~S5：
       S1 合并规模 ≥ 5 亿
       S2 现任基金经理从业 ≥ 5 年
       S3 现任经理任本基金 ≥ 3 年（1095 天）
       S4 成立 ≥ 5 年（保证近五年数据完整）
       S5 剔除近 5 年任一份额单日涨幅 > 1% 的异常基金
  3. 性能条件（方案甲；字面规则"四项前40%×每年>55%"全阈值空集后的放宽决策）：
       P1 近5y/3y 收益与回撤四项池内分位的均值，其池内分位 ≥ 45%（综合前 55%）
       P2 近 5 个完整自然年收益分位的中位数 ≥ 55%（可比完整年 ≥ 4）
     百分位基准均为短债合并全池（含未过硬条件者），与干跑验证一致。
  4. 排序：按 v5.2 分数降序（A 类份额优先，无 A 类用代表份额）。
     - 手动模式：读 data/full_scores.json 快照（--snapshot 可换）；
     - 周更管线（fundscorer.pipeline）：筛后即时评分（score_pool score_only=候选），
       分位基准仍是短债全池，分数与全市场快照逐位一致，再按 config.TOP_N 截取前 N。

用法：
    python -m fundscorer.screen                     # 筛选 + 漏斗 + 榜单
    python -m fundscorer.screen --csv out.csv       # 同时导出 CSV
    python -m fundscorer.screen --top 20            # 只保留前 20 名
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pickle
import re
from pathlib import Path

import numpy as np
import pandas as pd

from . import config as C
from . import metrics

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DEFAULT_NAV_DIR = DATA / "nav_full"
DEFAULT_CACHE = DATA / "metrics_cache.pkl"

REPORT_COLS = [
    "rank", "code", "name", "score", "star", "score_source", "manager",
    "scale_sum", "n_classes", "share_scores", "src_name",
    "career", "tenure", "nav_years",
    "ret_1y", "ret_5y", "mdd_5y", "ret_3y", "mdd_3y",
    "vol_5y", "sharpe_1y", "sharpe_5y",
    "comp_pct", "yearly_med", "max_gain_5y",
]


# ---------------------------------------------------------------- 份额处理
def base_name(name: str) -> str:
    """基金基础名：去掉结尾 '/X' 或单个大写字母份额后缀（与研究口径一致）。"""
    n = (name or "").strip()
    n = re.sub(r"/[A-Z]+$", "", n)
    n = re.sub(r"[A-Z]+$", "", n)
    return n


def share_letter(name: str) -> str | None:
    """份额字母（如 'A'/'C'）；'A/B' 双类份额返回 'A'（主类）；无份额后缀返回 None。"""
    n = (name or "").strip()
    m = re.search(r"([A-Z])(?:/[A-Z])?$", n)
    return m.group(1) if m else None


def merge_shares(records: list[dict]) -> pd.DataFrame:
    """份额记录 → 基金级表（基金级口径，用户 2026-10-15 确认）。

    - 代表份额 rep = 规模最大者（其经理为本基金现任经理）；
    - 经理从业 career / 任期 tenure = 同现任经理全部份额中的最大值
      （同经理各份额的 F10 任期记录可能不同——新发份额记为上市日；取最大才反映真实任职）；
    - 成立年限 nav_years = 全部份额最大值；
    - 业绩字段 = 历史最久份额（nav_years 最大，并列优先 A 类）；
    - scale_sum = 全份额求和；codes/names 供 S5 与排序使用。
    """
    groups: dict[str, list[dict]] = {}
    for r in records:
        groups.setdefault(base_name(r["name"]), []).append(r)
    rows = []
    for base, shares in groups.items():
        rep = max(shares, key=lambda x: (x.get("scale_yi") or 0))
        cur_mgr = rep.get("manager")
        same_mgr = [x for x in shares if x.get("manager") == cur_mgr] or [rep]
        src = max(shares, key=lambda x: ((x.get("nav_years") or 0),
                                         1 if share_letter(x["name"]) == "A" else 0))
        careers = [x.get("mgr_career_years") for x in same_mgr
                   if x.get("mgr_career_years") is not None]
        tenures = [x.get("mgr_tenure_days") for x in same_mgr
                   if x.get("mgr_tenure_days") is not None]
        rows.append({
            "base": base,
            "rep_code": rep["code"], "rep_name": rep["name"],
            "src_code": src["code"], "src_name": src["name"],
            "manager": cur_mgr,
            "scale_sum": float(sum((x.get("scale_yi") or 0) for x in shares)),
            "n_classes": len(shares),
            "codes": [x["code"] for x in shares],
            "names": [x["name"] for x in shares],
            "career": max(careers) if careers else None,
            "tenure": max(tenures) if tenures else None,
            "nav_years": max((x.get("nav_years") or 0) for x in shares),
            "ret_5y": src.get("ret_5y"), "mdd_5y": src.get("mdd_5y"),
            "ret_3y": src.get("ret_3y"), "mdd_3y": src.get("mdd_3y"),
            "ret_1y": src.get("ret_1y"),
            "vol_1y": src.get("vol_1y"), "vol_5y": src.get("vol_5y"),
        })
    df = pd.DataFrame(rows)
    for col in ["scale_sum", "career", "tenure", "nav_years",
                "ret_5y", "mdd_5y", "ret_3y", "mdd_3y",
                "ret_1y", "vol_1y", "vol_5y"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


# ---------------------------------------------------------------- 净值特征
def scan_spikes(navs: dict[str, pd.DataFrame | None], anchor,
                window_years: int = 5) -> dict[str, float]:
    """各份额近 N 年单日最大涨幅（复权净值 pct_change；窗口 = anchor 前推 N 年）。"""
    start = pd.Timestamp(anchor) - pd.DateOffset(years=window_years)
    out: dict[str, float] = {}
    for code, nav in navs.items():
        if nav is None or nav.empty:
            out[code] = np.nan
            continue
        sub = nav[nav["date"] > start]
        out[code] = float(sub["nav"].pct_change().max()) if len(sub) >= 2 else np.nan
    return out


def complete_yearly_returns(nav: pd.DataFrame, end_year: int,
                            min_obs: int = 120, n_years: int = 5) -> dict[int, float]:
    """近 n_years 个完整自然年收益（end_year 前推，不含当年）。

    完整年判定：年内净值点 ≥ min_obs 且存在上年基数（无基数的成立当年不计）。
    """
    nav = nav.sort_values("date")
    out: dict[int, float] = {}
    for y in range(end_year - n_years, end_year):
        yr = nav[nav["date"].dt.year == y]
        if len(yr) < min_obs:
            continue
        prev = nav[nav["date"] < pd.Timestamp(f"{y}-01-01")]
        if len(prev) == 0:
            continue
        out[y] = float(yr["nav"].iloc[-1] / prev["nav"].iloc[-1] - 1)
    return out


# ---------------------------------------------------------------- 性能条件
def composite_percentiles(pool: pd.DataFrame) -> pd.DataFrame:
    """四项分位（全池基准）→ mean4 → 池内分位 mpct。索引与 pool 对齐。"""
    P = pd.DataFrame({
        "ret_5y": pool["ret_5y"].rank(pct=True),
        "mdd_5y": pool["mdd_5y"].rank(pct=True, ascending=False),
        "ret_3y": pool["ret_3y"].rank(pct=True),
        "mdd_3y": pool["mdd_3y"].rank(pct=True, ascending=False),
    })
    P["mean4"] = P[["ret_5y", "mdd_5y", "ret_3y", "mdd_3y"]].mean(axis=1)
    P["mpct"] = P["mean4"].rank(pct=True)
    return P


def yearly_median_pct(Y: pd.DataFrame, base: str, yearly: dict,
                      pool_min: int = 30, min_years: int = 4) -> float | None:
    """该基金各完整年收益分位的中位数；可比年不足或样本不足返回 None。"""
    ys = yearly.get(base) or {}
    if len(ys) < min_years:
        return None
    pcts = []
    for y, v in sorted(ys.items()):
        if y not in Y.columns:
            return None
        col = Y[y].dropna()
        if len(col) < pool_min:
            return None
        pcts.append(float((col < v).mean()))
    return float(np.median(pcts))


# ---------------------------------------------------------------- 两段筛选
def _fund_max_gain(codes: list[str], spikes: dict) -> float:
    vals = [spikes[c] for c in codes if c in spikes]
    vals = [v for v in vals if v is not None and not np.isnan(v)]
    return max(vals) if vals else np.nan


def _pick_rank_share(classes: list[tuple[str, str]], rep_code: str,
                     snapshot: dict, prefer_letter: str = "A") -> tuple[str | None, str]:
    """排序用份额：优先指定字母份额（默认 A，多取时选分高者），否则代表份额，
    再否则任一有分数的份额。返回 (code, 来源说明)。"""
    pref = [c for c, n in classes
            if share_letter(n) == prefer_letter and c in snapshot]
    if pref:
        return max(pref, key=lambda c: snapshot[c].get("score_capped") or -1), "A类"
    if rep_code in snapshot:
        return rep_code, "代表份额"
    scored = [c for c, _ in classes if c in snapshot]
    if scored:
        return (max(scored, key=lambda c: snapshot[c].get("score_capped") or -1),
                "最高分份额")
    return None, ""


def screen_candidates(records: list[dict], spikes: dict, yearly: dict,
                      cfg: dict | None = None) -> tuple[pd.DataFrame, dict]:
    """阶段一+二（纯内存计算）：硬条件 S1~S5 + 性能条件 P1~P2，返回候选表与漏斗。

    records  短债池份额级指标记录（metrics_cache 口径）
    spikes   {code: 近5年单日最大涨幅}
    yearly   {基础名: {完整年: 年收益}}（历史最久份额净值口径）
    """
    cfg = dict(C.SCREEN if cfg is None else cfg)
    pool = merge_shares(records)
    funnel: dict[str, int] = {"合并基金数": len(pool)}

    # ---- 阶段一 硬条件 S1~S5 ----
    keep = pool["scale_sum"] >= cfg["scale_min_yi"]
    funnel["S1 规模合并≥5亿"] = int(keep.sum())
    keep &= pool["career"] >= cfg["mgr_career_min_years"]
    funnel["S2 经理从业≥5年"] = int(keep.sum())
    keep &= pool["tenure"] >= cfg["mgr_tenure_min_days"]
    funnel["S3 现经理任本基金≥3年"] = int(keep.sum())
    keep &= pool["nav_years"] >= cfg["min_nav_years"]
    funnel["S4 成立≥5年"] = int(keep.sum())
    pool["max_gain_5y"] = pool["codes"].map(lambda cs: _fund_max_gain(cs, spikes))
    # 数据不足（NaN）不通过 S5：无法验证"无异常涨幅"则不放行
    keep &= pool["max_gain_5y"] <= cfg["spike_max_gain"]
    funnel["S5 无近5年单日涨幅>1%"] = int(keep.sum())

    # ---- 阶段二 性能条件 P1~P2（百分位基准 = 短债合并全池） ----
    P = composite_percentiles(pool)
    keep &= P["mpct"] >= cfg["comp_pct_min"]
    funnel["P1 综合分位≥45%"] = int(keep.sum())
    Y = pd.DataFrame(yearly).T
    med = pd.Series(
        {i: yearly_median_pct(Y, b, yearly, cfg["yearly_pool_min"],
                              cfg["yearly_min_years"])
         for i, b in zip(pool.index, pool["base"])},
        dtype="float64")
    keep &= med >= cfg["yearly_median_min"]
    funnel["P2 年度分位中位≥55%"] = int(keep.sum())

    out = pool[keep].copy()
    out["comp_pct"] = P.loc[keep, "mpct"]
    out["yearly_med"] = med[keep]
    return out, funnel


def rank_candidates(candidates: pd.DataFrame, snapshot: dict,
                    cfg: dict | None = None,
                    top_n: int | None = None) -> pd.DataFrame:
    """阶段三：按 v5.2 分数排序（A 类份额优先）；top_n>0 时只保留前 N 名。

    snapshot {code: 分数记录}（周更=筛后即时评分结果；手动=full_scores.json 快照）
    """
    cfg = dict(C.SCREEN if cfg is None else cfg)
    if candidates.empty:
        return pd.DataFrame()
    rows = []
    for _, f in candidates.iterrows():
        classes = list(zip(f["codes"], f["names"]))
        code, src = _pick_rank_share(classes, f["rep_code"], snapshot,
                                     cfg.get("prefer_letter", "A"))
        rec = snapshot.get(code, {}) if code else {}
        share_scores = " ".join(
            f"{c}:{snapshot[c].get('score_capped'):.1f}"
            for c, _ in classes if c in snapshot and snapshot[c].get("score_capped") is not None)
        rows.append({
            "base": f["base"], "rep_code": f["rep_code"], "rep_name": f["rep_name"],
            "src_code": f["src_code"], "src_name": f["src_name"],
            "code": code, "name": rec.get("name", f["rep_name"]),
            "score": rec.get("score_capped"), "star": rec.get("star"),
            "score_source": src, "manager": f["manager"],
            "scale_sum": f["scale_sum"], "n_classes": f["n_classes"],
            "share_scores": share_scores,
            "career": f["career"], "tenure": f["tenure"], "nav_years": f["nav_years"],
            "ret_1y": f.get("ret_1y"),
            "ret_5y": f["ret_5y"], "mdd_5y": f["mdd_5y"],
            "ret_3y": f["ret_3y"], "mdd_3y": f["mdd_3y"],
            "vol_1y": f.get("vol_1y"), "vol_5y": f.get("vol_5y"),
            "sharpe_1y": metrics.sharpe_ratio(f.get("ret_1y"), f.get("vol_1y")),
            "sharpe_5y": metrics.sharpe_ratio(f.get("ret_5y"), f.get("vol_5y")),
            "comp_pct": f["comp_pct"], "yearly_med": f["yearly_med"],
            "max_gain_5y": f["max_gain_5y"],
        })
    res = pd.DataFrame(rows)
    if res.empty:
        return res
    res = res.sort_values(["score", "code"], ascending=[False, True],
                          kind="mergesort").reset_index(drop=True)
    if top_n:
        res = res.head(int(top_n))
    res.insert(0, "rank", np.arange(1, len(res) + 1))
    return res


def run_screen(records: list[dict], spikes: dict, yearly: dict,
               snapshot: dict, cfg: dict | None = None) -> tuple[pd.DataFrame, dict]:
    """两段筛选主体（纯内存计算，便于测试与复跑）：screen_candidates + rank_candidates。"""
    cfg = dict(C.SCREEN if cfg is None else cfg)
    candidates, funnel = screen_candidates(records, spikes, yearly, cfg)
    res = rank_candidates(candidates, snapshot, cfg)
    return res, funnel


# ---------------------------------------------------------------- IO 与 CLI
def load_screen_inputs(cache_path: Path | None = None, nav_dir: Path | None = None,
                       asof: str | pd.Timestamp | None = None,
                       cfg: dict | None = None) -> tuple[list[dict], dict, dict, dict]:
    """读取 metrics_cache（短债池）与 nav_full，产出筛选输入。

    返回 (records, spikes, yearly, navs)；navs 供周更管线复用（详情页净值曲线等）。
    """
    cfg = dict(C.SCREEN if cfg is None else cfg)
    cache_path = Path(cache_path or DEFAULT_CACHE)
    nav_dir = Path(nav_dir or DEFAULT_NAV_DIR)
    records = pickle.loads(cache_path.read_bytes())
    records = [r for r in records if r.get("pool") == cfg["pool"]]
    if not records:
        raise ValueError(f"缓存中无 {cfg['pool']} 池记录：{cache_path}")

    all_codes = [r["code"] for r in records]
    navs: dict[str, pd.DataFrame | None] = {}
    for c in all_codes:
        p = nav_dir / f"{c}.pkl"
        navs[c] = pd.read_pickle(p) if p.exists() else None

    asof = pd.Timestamp(asof) if asof else pd.Timestamp(dt.date.today())
    spikes = scan_spikes(navs, asof, cfg["spike_window_years"])

    pool = merge_shares(records)
    years_available = [n["date"].max().year for n in navs.values()
                       if n is not None and not n.empty]
    end_year = max(years_available) if years_available else asof.year
    yearly = {}
    for _, f in pool.iterrows():
        nav = navs.get(f["src_code"])   # 基金级口径：年度收益用历史最久份额
        yearly[f["base"]] = (complete_yearly_returns(nav, end_year,
                                                     cfg["yearly_min_obs"])
                             if nav is not None else {})
    return records, spikes, yearly, navs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="短债两段式筛选（先筛后评）")
    ap.add_argument("--cache", default=str(DEFAULT_CACHE))
    ap.add_argument("--nav-dir", default=str(DEFAULT_NAV_DIR))
    ap.add_argument("--snapshot", default=None, help="v5.2 全市场分数快照 JSON")
    ap.add_argument("--csv", default=None, help="导出 CSV 路径")
    ap.add_argument("--top", type=int, default=None,
                    help="只保留分数前 N 名（默认不截断；周更管线用 config.TOP_N）")
    ap.add_argument("--asof", default=None, help="数据基准日 YYYY-MM-DD（默认今天）")
    args = ap.parse_args(argv)

    try:
        records, spikes, yearly, _navs = load_screen_inputs(args.cache, args.nav_dir,
                                                            args.asof)
    except ValueError as e:
        print(e)
        return 1
    cfg = C.SCREEN

    snap_path = Path(args.snapshot) if args.snapshot else ROOT / cfg["score_snapshot"]
    snap = json.loads(snap_path.read_text())
    if snap.get("model_version") != C.MODEL_VERSION:
        print(f"[警告] 快照模型版本 {snap.get('model_version')} ≠ 当前 {C.MODEL_VERSION}，"
              "建议先重跑 rank 刷新快照")
    snapshot = {r["code"]: r for r in snap["results"]}

    candidates, funnel = screen_candidates(records, spikes, yearly)
    res = rank_candidates(candidates, snapshot, top_n=args.top)

    print(f"== 短债两段式筛选漏斗（快照 {snap.get('generated_at', '?')}）==")
    for k, v in funnel.items():
        print(f"   {k}: {v}")
    print(f"\n== 入选榜单（{len(res)} 只，按 v5.2 全市场分数降序"
          f"{'，已截取前 ' + str(args.top) + ' 名' if args.top else ''}）==")
    if not res.empty:
        show = res[["rank", "code", "name", "score", "star", "score_source",
                    "scale_sum", "ret_1y", "ret_5y", "mdd_5y", "sharpe_5y",
                    "comp_pct", "yearly_med"]]
        print(show.to_string(index=False,
                             float_format=lambda x: f"{x:.4f}"))

    if args.csv:
        out = res.copy()
        Path(args.csv).parent.mkdir(parents=True, exist_ok=True)
        out[REPORT_COLS].to_csv(args.csv, index=False, encoding="utf-8-sig")
        print(f"\n已导出 {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
