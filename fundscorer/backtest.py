# -*- coding: utf-8 -*-
"""历史回测引擎：滚动时点（point-in-time）分层回测，量化各因素对"未来持有胜率"的预测力。

口径（docs/05）：
  · 节点 = 每年末/半年末 2019-06-30 ~ 2024-06-30（11 个），全部数据严格截断到节点 T（无前视）。
  · 因子 = 截面因素在节点 T 的取值；窗口指标复用 metrics 生产函数（天然同口径）。
  · 前瞻收益 = T → T+6月/1年/2年；胜率 = 跑赢同池同节点中位数。
  · 回测不含 晨星/机构比/规模/经理全产品年化（不可无偏回溯），经理任期与从业年限按起始日回推。
  · 评分复用 scorer._score_one（分位/门槛/红线逻辑与生产完全一致）。

用法：
    python -m fundscorer.backtest panel      # 阶段1：构建 因子×前瞻 面板 → data/backtest_panel.pkl
    python -m fundscorer.backtest analyze    # 阶段2：IC / 分层胜率 / IC 权重 / 组合验证
    python -m fundscorer.backtest all
"""
from __future__ import annotations

import json
import pickle
import sys
import time
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd

from . import config as C
from . import metrics as M
from . import scorer as S

# ---------------------------------------------------------------- 节点与常量
NODES = pd.to_datetime([
    "2019-06-30", "2019-12-31", "2020-06-30", "2020-12-31",
    "2021-06-30", "2021-12-31", "2022-06-30", "2022-12-31",
    "2023-06-30", "2023-12-31", "2024-06-30",
])
HORIZONS_M = {"6m": 6, "1y": 12, "2y": 24}      # 前瞻持有期（月）
TRAIN_NODES = 7                                  # 前 7 个节点训练 IC 权重，后 4 个验证
MIN_HISTORY_DAYS = int(3 * 365)                  # 节点处至少 3 年历史（对齐星级门槛）
STALE_TOL_DAYS = 20                              # 节点前 20 天内须有净值（排除停牌断档）
MIN_PAIRS = 30                                   # IC/分层最少样本
REF_DATE = pd.Timestamp("2026-10-12")            # 经理任期/从业年限回推基准（数据最新日）

# 因子方向：+1 越大越好 / -1 越小越好（IC 方向对齐、分层排序用）
FACTOR_DIR = {
    "ret_5y": +1, "ret_3y": +1, "ret_1y": +1, "excess_5y": +1,
    "mdd_5y": -1, "mdd_3y": -1, "mdd_1y": -1,
    "vol_5y": -1, "winrate_m_5y": +1, "repair_5y": +1,
    "yearly_5y": +1, "rank_stab": -1,
    "lvi_5y": +1, "lvi_3y": +1,
    "q_excess_20q": +1, "q_win_20q": +1,
    "age": +1, "mgr_tenure": +1, "mgr_career": +1, "fee": -1,
}

ROOT = Path(__file__).resolve().parent.parent
NAV_DIR = ROOT / "data" / "nav_full"
PANEL_PATH = ROOT / "data" / "backtest_panel.pkl"


def backtestable_weights(pool: str) -> dict:
    """权重表中剔除不可无偏回溯项（晨星/机构比/规模/经理全产品年化）后的回测权重集。"""
    base = C.POOL_WEIGHTS[pool]
    drop = {"ms_rating", "inst_ratio", "fund_scale", "company_scale", "mgr_products_ann"}
    return {k: w for k, w in base.items() if k not in drop and w > 0}


# 混合方案 B：块间配额固定（风险/稳定性/经理/晨星不参与 IC），块内剩余按 IC 加权
HYBRID_FIXED = {
    "pure":   ["mdd_5y", "mdd_3y", "mdd_1y", "vol_5y", "repair_5y",   # 风险块 15
               "rank_stab",                                            # 稳定性 3
               "mgr_tenure", "mgr_career", "mgr_products_ann",         # 经理块 20
               "ms_rating"],                                           # 1
    "fixed_plus": ["mdd_5y", "mdd_3y", "mdd_1y", "repair_5y", "vol_5y",  # 风险块 19
                   "rank_stab",                                          # 3
                   "mgr_tenure", "mgr_career", "mgr_products_ann",       # 20
                   "ms_rating"],                                         # 1
}
HYBRID_CAP = 15.0   # IC 池内单因子上限（防止动量过度集中）


def hybrid_weights(pool: str, train_ic: dict) -> dict:
    """混合方案 B：固定块取现行权重，剩余配额按 max(IC,0) 分配（上限 15，迭代重分）。
    train_ic: {factor: 训练段方向对齐 1y IC}。"""
    base = C.POOL_WEIGHTS[pool]
    grp = "fixed_plus" if pool == "fixed_plus" else "pure"
    fixed_keys = HYBRID_FIXED[grp]
    w = {k: float(v) for k, v in base.items() if k in fixed_keys and v > 0}
    pool_keys = [k for k in base if base[k] > 0 and k not in fixed_keys
                 and k not in ("inst_ratio", "fund_scale", "company_scale")]
    # inst/fund/company 权重为 0，不属固定块也不入 IC 池
    remain = 100.0 - sum(w.values())
    ic_pos = {k: max(train_ic.get(k, 0.0), 0.0) for k in pool_keys}
    tot = sum(ic_pos.values())
    if tot <= 0:   # 池内无正 IC → 等权
        ic_pos = {k: 1.0 for k in pool_keys}
        tot = float(len(pool_keys))
    alloc = {k: remain * v / tot for k, v in ic_pos.items()}
    # 软上限水填：超 15 的部分封顶退出，余量按 IC 比例分给未封顶者；
    # 若全员封顶（池内正 IC 因子太少，容量不足），余量按 IC 比例回加给全员
    # （证据强者多得，仅此情形允许突破 15——固收+ 池即属此类，报告中注明）。
    pos = [k for k in pool_keys if ic_pos[k] > 0]
    capped: set = set()
    for _ in range(20):
        over = [k for k in pos if k not in capped and alloc[k] > HYBRID_CAP + 1e-9]
        if not over:
            break
        excess = sum(alloc[k] - HYBRID_CAP for k in over)
        for k in over:
            alloc[k] = HYBRID_CAP
            capped.add(k)
        free = [k for k in pos if k not in capped]
        targets = free if free else pos   # 容量不足 → 全员回加
        ftot = sum(ic_pos[k] for k in targets)
        for k in targets:
            alloc[k] += excess * ic_pos[k] / ftot
        if not free:
            break
    w.update({k: round(v, 2) for k, v in alloc.items() if v > 0})
    return w


# ================================================================ 阶段1：面板
_G: dict = {}


def _fund_rows(code: str) -> list[dict]:
    """单只基金 → 各节点的因子取值 + 前瞻收益（模块级状态，fork 继承）。"""
    meta = _G["cache"].get(code)
    if meta is None:
        return []
    try:
        nav = pd.read_pickle(NAV_DIR / f"{code}.pkl")
    except Exception:
        return []
    if nav is None or len(nav) < 120:
        return []
    pool = meta["pool"]

    est = pd.to_datetime(meta.get("establish_date"), errors="coerce")
    sig_floor = C.REPAIR["sig_dd_floor"][pool]

    # 经理任期/从业年限回推
    tenure_now = meta.get("mgr_tenure_days")
    mstart = (REF_DATE - pd.Timedelta(days=int(tenure_now))) if tenure_now else None
    career_now = meta.get("mgr_career_years")
    cstart = (REF_DATE - pd.Timedelta(days=float(career_now) * 365.25)) if career_now else None

    rows = []
    for T in _G["nodes"]:
        nav_t = nav[nav["date"] <= T]
        if len(nav_t) < 120:
            continue
        first, last = nav_t["date"].min(), nav_t["date"].max()
        if (T - first).days < MIN_HISTORY_DAYS:
            continue
        if (T - last).days > STALE_TOL_DAYS:
            continue
        age = ((T - (est if not pd.isna(est) else first)).days) / 365.25

        r = {"code": code, "pool": pool, "T": T, "age": age}

        # ---- 收益/风险窗口（复用生产函数，窗口锚定 nav_t 最大日 = T 附近）
        for y, tag in [(5, "5y"), (3, "3y"), (1, "1y")]:
            r[f"ret_{tag}"] = M.ann_return(nav_t, y)
            r[f"mdd_{tag}"] = M.max_drawdown(nav_t, y)
            r[f"vol_{tag}"] = M.ann_vol(nav_t, y)
        r["winrate_m_5y"] = M.monthly_win_rate(nav_t, 5)
        rep = M.drawdown_repair(nav_t, 5, sig_floor)
        r["repair"] = rep
        r["repair_5y"] = S._repair_scalar({**r, "repair_5y": rep, "age": age},
                                          pool, {"repair_5y": 1})
        r["q_win_20q"] = M.quarterly_win_ratio(nav_t, 5)
        r["yearly"] = M.yearly_returns(nav_t, 5) or {}

        # ---- 基准（严格截断到 T，杜绝前视）
        bench_t = (_G["bench_cut_short"] if pool == "short_bond"
                   else _G["bench_cut_full"])[T]
        r["q_excess_20q"] = M.quarterly_excess_ratio(nav_t, bench_t, 5)
        b5 = M.bench_annualized(bench_t, 5)
        r["excess_5y"] = (r["ret_5y"] - b5) if (r["ret_5y"] is not None and b5 is not None) else None
        r["lvi_5y"] = M.lvi(r["ret_5y"], b5, r["mdd_5y"], M.downside_vol(nav_t, 5),
                            **C.LVI_RC_WEIGHTS)
        b3 = M.bench_annualized(bench_t, 3)
        r["lvi_3y"] = M.lvi(r["ret_3y"], b3, r["mdd_3y"], M.downside_vol(nav_t, 3),
                            **C.LVI_RC_WEIGHTS)

        # ---- 质地（回推/静态）
        if mstart is not None and mstart <= T:
            r["mgr_tenure"] = (T - mstart).days / 365.25
            r["mgr_tenure_days"] = (T - mstart).days
        else:
            r["mgr_tenure"] = None
            r["mgr_tenure_days"] = None
        r["mgr_career"] = ((T - cstart).days / 365.25
                           if cstart is not None and T >= cstart else None)
        r["fee"] = meta.get("fee_total")

        # ---- R7-4 单日异常（用节点前 1 年净值判定，避免存整段 nav）
        r["r7_4"] = _r7_4_hit(nav_t, pool)

        # ---- 前瞻收益与前瞻风险（T 之后 6月/1年/2年；风险=同窗口内回撤/波动，衡量曲线平稳度）
        post = nav[nav["date"] > T]
        base = nav_t["nav"].iloc[-1]
        for hname, m in HORIZONS_M.items():
            endT = T + pd.DateOffset(months=m)
            seg = post[post["date"] <= endT]
            if seg.empty:
                r[f"fwd_{hname}"] = None
                r[f"fwd_mdd_{hname}"] = None
                r[f"fwd_vol_{hname}"] = None
                continue
            r[f"fwd_{hname}"] = float(seg["nav"].iloc[-1] / base - 1)
            # 前瞻窗口内回撤：峰值取 max(T点净值, 窗口内净值)
            path = np.concatenate([[base], seg["nav"].to_numpy()])
            peak = np.maximum.accumulate(path)
            r[f"fwd_mdd_{hname}"] = float(np.max(1.0 - path / peak))
            rets = seg["nav"].pct_change().dropna()
            r[f"fwd_vol_{hname}"] = (float(rets.std() * np.sqrt(252))
                                     if len(rets) >= 20 else None)
        rows.append(r)
    return rows


def _r7_4_hit(nav_t: pd.DataFrame, pool: str) -> bool:
    floor = C.REDLINES["single_day_abs_floor"].get(pool)
    if not floor:
        return False
    rets = nav_t.set_index("date")["nav"].pct_change().dropna()
    end = nav_t["date"].max()
    rets = rets[rets.index > end - pd.DateOffset(years=1)]
    if not len(rets):
        return False
    worst = rets.min()
    med_abs = float(rets.abs().median())
    if abs(worst) > max(C.REDLINES["single_day_mult"] * med_abs, floor):
        i = rets.idxmin()
        after = rets.loc[i:]
        if len(after) and (1 + after).prod() < 1 + worst / 2:
            return True
    return False


def build_panel(workers: int = 8, log=print) -> None:
    t0 = time.time()
    cache = {m["code"]: m for m in pickle.loads((ROOT / "data" / "metrics_cache.pkl").read_bytes())}
    bench_full = pd.read_pickle(ROOT / "data" / "cache" / "bond_index_wealth.pkl")
    bench_short = pd.read_pickle(ROOT / "data" / "cache" / "bond_index_wealth_short.pkl")

    codes = [c for c in cache if (NAV_DIR / f"{c}.pkl").exists()]
    log(f"回测面板：{len(codes)} 只基金 × {len(NODES)} 节点")

    _G.clear()
    _G.update(cache=cache, nodes=NODES, bench_full=bench_full, bench_short=bench_short,
              # 每节点基准切片（一次预计算，worker 只读）
              bench_cut_full={T: bench_full[bench_full["date"] <= T] for T in NODES},
              bench_cut_short={T: bench_short[bench_short["date"] <= T] for T in NODES})

    rows: list[dict] = []
    n_done = 0
    if workers <= 1:
        for c in codes:
            rows.extend(_fund_rows(c))
            n_done += 1
            if n_done % 500 == 0:
                log(f"  {n_done}/{len(codes)}  {time.time()-t0:.0f}s")
    else:
        import multiprocessing as mp
        with mp.Pool(workers) as pool:
            for res in pool.imap_unordered(_fund_rows, codes, chunksize=8):
                rows.extend(res)
                n_done += 1
                if n_done % 1000 == 0:
                    log(f"  {n_done}/{len(codes)}  {time.time()-t0:.0f}s")

    df = pd.DataFrame(rows)
    df.to_pickle(PANEL_PATH)
    log(f"面板 {len(df)} 行 × {df['code'].nunique()} 基金 → {PANEL_PATH}  用时 {time.time()-t0:.0f}s")
    log("DONE")


# ================================================================ 阶段2：分析
def _spearman(x: pd.Series, y: pd.Series) -> float | None:
    if len(x) < MIN_PAIRS:
        return None
    if x.nunique() < 2 or y.nunique() < 2:
        return None
    v = x.corr(y, method="spearman")
    return None if v is None or np.isnan(v) else float(v)


def analyze(log=print) -> None:
    df = pd.read_pickle(PANEL_PATH)
    df = df[df["fwd_1y"].notna()].copy()
    log(f"分析样本：{len(df)} 基金节点，{df['code'].nunique()} 基金")

    # ---- 池级因子补算：yearly_5y（同池逐年中位对比）与 rank_stab（同池逐年排名）
    #      二者依赖横截面，单基金面板阶段无法计算，须在此按 (节点,池) 分组补齐。
    df["yearly_5y"] = np.nan
    df["rank_stab"] = np.nan
    for (T, pool), g in df.groupby(["T", "pool"]):
        funds = g.to_dict("records")
        stab = S._pool_yearly_ranks(funds)
        ys = [np.nan if v is None else float(v)
              for v in (S._yearly_score(f, funds, pool) for f in funds)]
        rs = [np.nan if v is None else float(v)
              for v in (M.rank_stability_score(stab.get(f["code"], [])) for f in funds)]
        df.loc[g.index, "yearly_5y"] = ys
        df.loc[g.index, "rank_stab"] = rs
    log(f"池级因子补齐：yearly_5y 非空 {df['yearly_5y'].notna().sum()}，"
        f"rank_stab 非空 {df['rank_stab'].notna().sum()}")

    # ---- 池内中位前瞻收益（胜负基准）
    df["fwd_med"] = df.groupby(["T", "pool"])["fwd_1y"].transform("median")
    df["win"] = df["fwd_1y"] > df["fwd_med"]

    # =============== 1) IC 表（因子 × 持有期 × 池 × 节点）
    ic_rows = []
    for pool, g in df.groupby("pool"):
        for T, gt in g.groupby("T"):
            if len(gt) < MIN_PAIRS:
                continue
            for fac in FACTOR_DIR:
                if fac not in gt or gt[fac].isna().all():
                    continue
                s = gt[fac].dropna()
                # 各持有期 IC
                rec = {"pool": pool, "T": T, "factor": fac, "n": len(s)}
                for hz in HORIZONS_M:
                    y = gt.loc[s.index, f"fwd_{hz}"]
                    ok = y.notna()
                    rec[f"ic_{hz}"] = _spearman(s[ok], y[ok])
                ic_rows.append(rec)
    icdf = pd.DataFrame(ic_rows)

    # 汇总：池内按节点平均 → 对齐方向
    summary = []
    for (pool, fac), g in icdf.groupby(["pool", "factor"]):
        d = FACTOR_DIR[fac]
        row = {"pool": pool, "factor": fac, "n_nodes": len(g)}
        for hz in HORIZONS_M:
            vals = g[f"ic_{hz}"].dropna()
            vals = vals * d   # 方向对齐：>0 = 与设计预期一致
            if len(vals):
                m, s = float(vals.mean()), float(vals.std(ddof=1)) if len(vals) > 1 else np.nan
                row[f"ic_{hz}"] = round(m, 4)
                row[f"t_{hz}"] = round(m / (s / np.sqrt(len(vals))), 2) if s and s > 0 else None
            else:
                row[f"ic_{hz}"], row[f"t_{hz}"] = None, None
        summary.append(row)
    ic_summary = pd.DataFrame(summary)
    ic_summary.to_csv(ROOT / "data" / "backtest_ic.csv", index=False, encoding="utf-8-sig")
    icdf.to_csv(ROOT / "data" / "backtest_ic_nodes.csv", index=False, encoding="utf-8-sig")
    log("\n=== 方向对齐 IC（1y 前瞻，按池、按节点均值）===")
    piv = ic_summary.pivot_table(index="factor", columns="pool", values="ic_1y")
    log(piv.to_string())
    log("\n=== IC t 值（1y）===")
    log(ic_summary.pivot_table(index="factor", columns="pool", values="t_1y").to_string())

    # =============== 2) 分层胜率（五分位，Q1 最差 → Q5 最好，方向调整后）
    q_rows = []
    for pool, g in df.groupby("pool"):
        for T, gt in g.groupby("T"):
            if len(gt) < MIN_PAIRS:
                continue
            for fac, d in FACTOR_DIR.items():
                if fac not in gt:
                    continue
                s = gt[fac]
                if s.notna().sum() < MIN_PAIRS:
                    continue
                order = s * d                      # 方向调整
                try:
                    bucket = pd.qcut(order.rank(method="first"), 5,
                                     labels=False, duplicates="drop")
                except ValueError:
                    continue
                med = gt["fwd_med"]
                for b in sorted(pd.unique(bucket.dropna())):
                    sub = gt[bucket == b]
                    q_rows.append({
                        "pool": pool, "T": T, "factor": fac, "q": int(b) + 1,
                        "n": len(sub), "win": float(sub["win"].mean()),
                        "excess": float((sub["fwd_1y"] - med[bucket == b]).mean()),
                    })
    qdf = pd.DataFrame(q_rows)
    qdf.to_csv(ROOT / "data" / "backtest_quintile_nodes.csv", index=False, encoding="utf-8-sig")

    # 节点间按 n 加权平均
    qdf["w_win"] = qdf["win"] * qdf["n"]
    qdf["w_exc"] = qdf["excess"] * qdf["n"]
    agg = qdf.groupby(["pool", "factor", "q"])[["w_win", "w_exc", "n"]].sum()
    qsum = agg.reset_index()
    qsum["win"] = qsum["w_win"] / qsum["n"]
    qsum["excess"] = qsum["w_exc"] / qsum["n"]
    qsum = qsum[["pool", "factor", "q", "n", "win", "excess"]]
    qsum.to_csv(ROOT / "data" / "backtest_quintile.csv", index=False, encoding="utf-8-sig")
    log("\n=== 分层胜率 Q1→Q5（1y，方向已调：Q5=按因子应更好）===")
    pv = qsum.pivot_table(index=["pool", "factor"], columns="q", values="win")
    pv.columns = [f"Q{int(c)}" for c in pv.columns]
    log(pv.round(3).to_string())

    # =============== 3) IC 加权候选权重（训练节点，1y）
    weights_b: dict[str, dict] = {}
    weights_c: dict[str, dict] = {}
    for pool in df["pool"].unique():
        w_a = backtestable_weights(pool)
        train_nodes = NODES[:TRAIN_NODES]
        ic_pool = icdf[(icdf["pool"] == pool) & (icdf["T"].isin(train_nodes))]
        w_b, train_ic = {}, {}
        for k in w_a:
            sub = ic_pool[ic_pool["factor"] == k]
            if sub.empty:
                w_b[k] = 0.0
                continue
            ic_h = sub["ic_1y"].dropna() * FACTOR_DIR[k]
            train_ic[k] = float(ic_h.mean()) if len(ic_h) else 0.0
            w_b[k] = max(train_ic[k], 0.0)
        tot = sum(w_b.values())
        if tot <= 0:
            w_b = {k: 1.0 for k in w_a}
            tot = sum(w_b.values())
        # 归一到与现行回测集同和（便于对照）
        scale = sum(w_a.values()) / tot
        w_b = {k: round(v * scale, 2) for k, v in w_b.items()}
        weights_b[pool] = w_b
        # 混合方案 B：固定块（风险/稳定性/经理/晨星）+ 剩余按 IC（单因子上限 15）
        weights_c[pool] = hybrid_weights(pool, train_ic)
        log(f"\n=== {S.POOL_LABEL[pool]}：现行 vs IC加权 vs 混合B（训练节点 1y IC）===")
        cmp = pd.DataFrame({"现行": w_a,
                            "IC加权": {k: w_b.get(k, 0) for k in w_a},
                            "混合B": {k: weights_c[pool].get(k, 0) for k in w_a}})
        cmp["ΔB"] = cmp["混合B"] - cmp["现行"]
        log(cmp.sort_values("ΔB", ascending=False).to_string())
        log(f"合计：现行 {sum(w_a.values())} / IC加权 {sum(w_b.values())} / 混合B {sum(weights_c[pool].values())}")

    with open(ROOT / "data" / "backtest_weights_b.json", "w", encoding="utf-8") as fh:
        json.dump(weights_b, fh, ensure_ascii=False, indent=1)
    with open(ROOT / "data" / "backtest_weights_c.json", "w", encoding="utf-8") as fh:
        json.dump(weights_c, fh, ensure_ascii=False, indent=1)

    # =============== 4) 组合验证：现行 vs IC 加权 vs 混合B（训练/验证分开）
    comp_rows = []
    for (pool, T), gt in df.groupby(["pool", "T"]):
        w_a = backtestable_weights(pool)
        w_b = weights_b.get(pool, {})
        w_c = weights_c.get(pool, {})
        funds = gt.to_dict("records")
        # 池级量
        med_dd = (float(np.nanmedian([f["mdd_5y"] for f in funds if f["mdd_5y"] is not None]))
                  if funds else 0.0)
        stab = S._pool_yearly_ranks(funds)
        # _quality_scores 所需字段对齐（面板字段名 → 打分字段名）
        for f in funds:
            f["rank_stab"] = M.rank_stability_score(stab.get(f["code"], []))
            f["mgr_career_years"] = f.get("mgr_career")
            f["fee_total"] = f.get("fee")
            f["repair_5y"] = f.get("repair")   # _repair_scalar 需要字典原值
        for f in funds:
            try:
                S._score_one(f, funds, w_a, pool, med_dd, stab.get(f["code"], []))
            except Exception as e:
                f["score"] = None
                log(f"  打分异常 {f['code']}@{T.date()}: {e}")
            # R7-4（无 _nav_df，用面板预存判定补入）
            if f.get("r7_4") and "R7-4 单日异常跌幅且未修复" not in f.get("redlines", []):
                f.setdefault("redlines", []).append("R7-4 单日异常跌幅且未修复")
                if f.get("score") is not None:
                    f["score_capped"] = min(f["score"], C.REDLINES["score_cap"])
        # IC 权重分（复用同一套 sub_scores 线性组合 + 同一红线封顶）
        for f in funds:
            sub = f.get("sub_scores") or {}
            tot, ew = 0.0, 0.0
            for k, w in w_b.items():
                sc = sub.get(k)
                if sc is None or (isinstance(sc, float) and np.isnan(sc)):
                    continue
                tot += w * sc / 100.0
                ew += w
            f["score_b"] = (round(tot * 100 / ew, 2) if ew else None)
            if f.get("redlines") and f["score_b"] is not None:
                f["score_b"] = min(f["score_b"], C.REDLINES["score_cap"])
            # 混合 B 分：同 sub_scores 重组合（固定块字段缺失时按有数据权重归一）
            tot, ew = 0.0, 0.0
            for k, w in w_c.items():
                sc = sub.get(k)
                if sc is None or (isinstance(sc, float) and np.isnan(sc)):
                    continue
                tot += w * sc / 100.0
                ew += w
            f["score_c"] = (round(tot * 100 / ew, 2) if ew else None)
            if f.get("redlines") and f["score_c"] is not None:
                f["score_c"] = min(f["score_c"], C.REDLINES["score_cap"])

        sc_a = pd.Series({f["code"]: f.get("score_capped") for f in funds}, dtype=float)
        sc_b = pd.Series({f["code"]: f.get("score_b") for f in funds}, dtype=float)
        sc_c = pd.Series({f["code"]: f.get("score_c") for f in funds}, dtype=float)
        win = pd.Series({f["code"]: f.get("win") for f in funds}, dtype=bool)
        fwd = pd.Series({f["code"]: f.get("fwd_1y") for f in funds}, dtype=float)
        ok = sc_a.notna() & fwd.notna()
        if ok.sum() < MIN_PAIRS:
            continue
        top_a = sc_a[ok].rank(pct=True) >= 0.8
        top_b = sc_b[ok].rank(pct=True) >= 0.8
        top_c = sc_c[ok].rank(pct=True) >= 0.8
        med_f = fwd[ok].median()
        # 严格指标：入选组的前瞻排名百分位（1.0=池内未来收益第1名）
        fwd_pct = fwd[ok].rank(pct=True)

        def _strict(mask) -> tuple:
            return (float(fwd_pct[mask].mean()),                  # 平均未来分位
                    float((fwd_pct[mask] >= 0.90).mean()),        # 跑赢90%同类（进前10%）
                    float((fwd_pct[mask] >= 0.99).mean()))        # 进前1%（池内冠军）

        pa, t10a, t1a = _strict(top_a)
        pc, t10c, t1c = _strict(top_c)
        comp_rows.append({
            "pool": pool, "T": T, "n": int(ok.sum()),
            "win_top_a": float(win[ok][top_a].mean()),
            "win_top_b": float(win[ok][top_b].mean()),
            "win_top_c": float(win[ok][top_c].mean()),
            "exc_top_a": float((fwd[ok][top_a] - med_f).mean()),
            "exc_top_b": float((fwd[ok][top_b] - med_f).mean()),
            "exc_top_c": float((fwd[ok][top_c] - med_f).mean()),
            "bottom_a": float(win[ok][sc_a[ok].rank(pct=True) <= 0.2].mean()),
            "fwdpct_a": pa, "fwdpct_c": pc,
            "top10hit_a": t10a, "top10hit_c": t10c,
            "top1hit_a": t1a, "top1hit_c": t1c,
        })
    cdf = pd.DataFrame(comp_rows)
    cdf["split"] = np.where(cdf["T"] < NODES[TRAIN_NODES], "train", "valid")
    cdf.to_csv(ROOT / "data" / "backtest_composite.csv", index=False, encoding="utf-8-sig")

    cols = ["win_top_a", "win_top_b", "win_top_c", "exc_top_a", "exc_top_b",
            "exc_top_c", "bottom_a"]
    log("\n=== 组合验证：前 20% 持有 1 年跑赢同池中位的胜率 ===")
    log("（a=现行回测权重 / b=纯IC加权 / c=混合B）")
    agg = cdf.groupby(["split", "pool"])[cols].mean().round(3)
    log(agg.to_string())
    log("\n全部节点：")
    log(cdf[cols].mean().round(3).to_string())

    log("\n=== 严格指标（入选组=按分数前20%，1年后的池内表现）===")
    s2 = ["fwdpct_a", "fwdpct_c", "top10hit_a", "top10hit_c", "top1hit_a", "top1hit_c"]
    log("fwdpct=平均未来收益分位(0.5=中位)  top10hit=进前10%概率(跑赢90%)  top1hit=进前1%概率")
    log("随机基线：前20%组 fwdpct=0.50 / top10hit=0.10 / top1hit=0.01")
    log(cdf.groupby(["split", "pool"])[s2].mean().round(3).to_string())
    log("\n全部节点：")
    log(cdf[s2].mean().round(3).to_string())
    log("\nDONE")


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    mode = argv[0] if argv else "all"

    def log(*a):
        print(*a, flush=True)

    if mode in ("panel", "all"):
        build_panel(log=log)
    if mode in ("analyze", "all"):
        analyze(log=log)


if __name__ == "__main__":
    main()
