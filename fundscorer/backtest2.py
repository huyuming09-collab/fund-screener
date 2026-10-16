# -*- coding: utf-8 -*-
"""目标函数回测：为"收益更高 + 曲线平稳"寻找最佳评价指标与权重（docs/06）。

与 docs/05（目标=跑赢同池中位胜率）不同，本模块在回测中直接观测**前瞻风险**：
  · fwd_mdd_*：T 之后持有窗口内的实际最大回撤（曲线平稳度的直接观测）
  · fwd_vol_*：持有窗口内实际年化波动

候选目标函数（前瞻 1 年，均"越大越好"）：
    ret    = 收益                      （纯收益，不控回撤）
    calmar = 收益 / max(回撤, 0.5%)    （收益回撤比）
    u1/u2/u3 = 收益 − λ×回撤, λ=1/2/3  （多担 1% 回撤须多赚 λ% 收益）
    uvol2  = 收益 − 2×波动             （对波动付费而非回撤）
    mdd    = −回撤                     （纯平滑度，仅展示 IC 不生成方案）

候选方案（训练段 7 节点定权重 / 验证段 4 节点检验，IC 均方向对齐）：
    S0     现行 v5.1（基线）
    S_pool 全池等权（"买同类平均"基线）
    S1..S5 IC权重 vs ret/calmar/u1/u2/u3
    S6     混合：固定风险/经理/稳定性块 + IC池 vs u2（cap 15，同 hybrid_weights）
    S7     S1 分数 + 事前平滑过滤（T 时 5 年波动与回撤均 ≤ 池均值×1.5）

验证指标：前20%组合的 前瞻收益 / 前瞻回撤 / 前瞻波动 / 实现calmar / 实现u2 / 进同池前10%概率。

★ 预注册决策规则（跑前定死，防事后挑指标）：
    验证段中 实现u2均值最高 且 平均前瞻回撤 ≤ S0 的方案为"最佳"；
    无方案满足回撤约束时，取回撤最小者中 u2 最高者并如实报告。

用法：
    python -m fundscorer.backtest2
"""
from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd

from . import config as C
from . import metrics as M
from . import scorer as S
from .backtest import (NODES, TRAIN_NODES, PANEL_PATH, MIN_PAIRS, FACTOR_DIR,
                       backtestable_weights, hybrid_weights)

ROOT = PANEL_PATH.parent.parent          # 项目根（PANEL_PATH = <root>/data/...）

TARGETS = {   # 展示名 → 面板列
    "ret": "t_ret", "calmar": "t_calmar",
    "u1": "t_u1", "u2": "t_u2", "u3": "t_u3",
    "uvol2": "t_uvol2", "mdd": "t_mdd",
}
CALMAR_FLOOR = 0.005                       # 回撤地板 0.5%
SMOOTH_MULT = C.RISK_THRESHOLD["full_at_mult"]   # 事前平滑过滤线 = 池均值×1.5

SCHEMES = ["S0", "S_pool", "ret", "calmar", "u1", "u2", "u3", "S6", "S7"]
LABEL = {"S0": "现行v5.1", "S_pool": "全池等权", "ret": "S1=IC(纯收益)",
         "calmar": "S2=IC(calmar)", "u1": "S3=IC(u1)", "u2": "S4=IC(u2)",
         "u3": "S5=IC(u3)", "S6": "混合B(u2)", "S7": "S1+平滑过滤"}
IC_TARGETS_FOR_WEIGHTS = ["ret", "calmar", "u1", "u2", "u3"]


def load_panel(log=print) -> pd.DataFrame:
    df = pd.read_pickle(PANEL_PATH)
    df = df[df["fwd_1y"].notna() & df["fwd_mdd_1y"].notna()].copy()

    # 池级因子补算（依赖同池横截面）
    df["yearly_5y"] = np.nan
    df["rank_stab"] = np.nan
    for (T, pool), g in df.groupby(["T", "pool"]):
        funds = g.to_dict("records")
        stab = S._pool_yearly_ranks(funds)
        ys, rs = [], []
        for f in funds:
            v = S._yearly_score(f, funds, pool)
            ys.append(np.nan if v is None else float(v))
            r = M.rank_stability_score(stab.get(f["code"], []))
            rs.append(np.nan if r is None else float(r))
        df.loc[g.index, "yearly_5y"] = ys
        df.loc[g.index, "rank_stab"] = rs

    # 前瞻目标函数（全部转成"越大越好"）
    fwd, mdd, vol = df["fwd_1y"], df["fwd_mdd_1y"], df["fwd_vol_1y"]
    df["t_ret"] = fwd
    df["t_calmar"] = fwd / mdd.clip(lower=CALMAR_FLOOR)
    df["t_u1"] = fwd - 1.0 * mdd
    df["t_u2"] = fwd - 2.0 * mdd
    df["t_u3"] = fwd - 3.0 * mdd
    df["t_uvol2"] = fwd - 2.0 * vol
    df["t_mdd"] = -mdd
    log(f"面板：{len(df)} 基金节点，{df['code'].nunique()} 只基金；"
        f"前瞻1年回撤 中位 {float(mdd.median()):.3%} / 均值 {float(mdd.mean()):.3%}；"
        f"前瞻年化波动 中位 {float(vol.median()):.3%}")
    return df


def train_ic(df: pd.DataFrame, factor_cols: list, pool: str) -> dict:
    """{target: {factor: 训练段方向对齐 Spearman IC}}；目标与因子均已"越大越好"对齐。"""
    sub = df[(df["pool"] == pool) & (df["T"].isin(NODES[:TRAIN_NODES]))]
    out = {}
    for tname, tcol in TARGETS.items():
        icmap = {}
        for fac in factor_cols:
            pair = sub[[fac, tcol]].dropna()
            if len(pair) < MIN_PAIRS:
                continue
            raw = pair[fac].corr(pair[tcol], method="spearman")
            if pd.notna(raw):
                icmap[fac] = float(raw) * FACTOR_DIR.get(fac, +1)   # 方向对齐
        out[tname] = icmap
    return out


def analyze(log=print) -> None:
    df = load_panel(log=log)
    pools = sorted(df["pool"].unique())
    w_a = {p: backtestable_weights(p) for p in pools}
    factor_cols = [c for c in df.columns
                   if (c in FACTOR_DIR or c in ("yearly_5y", "rank_stab"))]

    # =============== ① 因子 IC × 目标函数（训练段，方向对齐）
    log("\n=== ① 方向对齐 Spearman IC（训练段 7 节点均值；正值=按设计方向有效）===")
    ic_by_pool = {}
    for p in pools:
        ic_by_pool[p] = train_ic(df, factor_cols, p)
        tbl = pd.DataFrame({t: ic_by_pool[p][t] for t in TARGETS}).T
        tbl = tbl[[c for c in tbl.columns if tbl[c].abs().max() > 0.03]]
        log(f"\n--- {S.POOL_LABEL[p]}（只列 |IC|>0.03；行=前瞻目标）---")
        log(tbl.round(3).to_string())

    # =============== ② 各目标的 IC 权重候选
    weights: dict[str, dict] = {"S0": w_a}
    for tname in IC_TARGETS_FOR_WEIGHTS:
        weights[tname] = {}
        for p in pools:
            total = sum(w_a[p].values())
            icm = {k: v for k, v in ic_by_pool[p][tname].items() if k in w_a[p]}
            pos = {k: max(v, 0.0) for k, v in icm.items()}
            tot = sum(pos.values())
            weights[tname][p] = ({k: round(v * total / tot, 2) for k, v in pos.items()}
                                 if tot > 0 else {k: 0.0 for k in pos})
    for p in pools:   # S6 混合（固定块 + IC池 vs u2）
        weights.setdefault("S6", {})[p] = hybrid_weights(p, ic_by_pool[p]["u2"])
    with open(ROOT / "data" / "backtest_obj_weights.json", "w", encoding="utf-8") as fh:
        json.dump(weights, fh, ensure_ascii=False, indent=1)

    log("\n=== ② IC 权重 vs 现行（回测可回溯子集，合计=88）===")
    for p in pools:
        cmp = pd.DataFrame({"现行": w_a[p], "S4=IC(u2)": weights["u2"][p],
                            "S2=IC(calmar)": weights["calmar"][p]}).fillna(0.0)
        log(f"\n--- {S.POOL_LABEL[p]} ---")
        log(cmp.sort_values("S4=IC(u2)", ascending=False).round(2).to_string())

    # =============== ③ 逐 (池,节点) 打分 + 前瞻组合验证
    rows = []
    for (pool, T), gt in df.groupby(["pool", "T"]):
        funds = gt.to_dict("records")
        mdd_vals = [f["mdd_5y"] for f in funds if f.get("mdd_5y") is not None
                    and not np.isnan(f["mdd_5y"])]
        med_dd = float(np.nanmedian(mdd_vals)) if mdd_vals else 0.0
        stab = S._pool_yearly_ranks(funds)
        for f in funds:   # 字段对齐（面板字段名 → 打分字段名）
            f["rank_stab"] = M.rank_stability_score(stab.get(f["code"], []))
            f["mgr_career_years"] = f.get("mgr_career")
            f["fee_total"] = f.get("fee")
            f["repair_5y"] = f.get("repair")
        for f in funds:
            try:
                S._score_one(f, funds, w_a[pool], pool, med_dd, stab.get(f["code"], []))
            except Exception as e:
                f["sub_scores"] = {}
                log(f"  打分异常 {f['code']}@{T.date()}: {e}")
            if f.get("r7_4") and "R7-4 单日异常跌幅且未修复" not in f.get("redlines", []):
                f.setdefault("redlines", []).append("R7-4 单日异常跌幅且未修复")

        def _recomb(wts: dict) -> pd.Series:
            out = {}
            for f in funds:
                sub = f.get("sub_scores") or {}
                tot, ew = 0.0, 0.0
                for k, w in wts.items():
                    sc = sub.get(k)
                    if sc is None or (isinstance(sc, float) and np.isnan(sc)):
                        continue
                    tot += w * sc / 100.0
                    ew += w
                v = round(tot * 100 / ew, 2) if ew else None
                if v is not None and f.get("redlines"):
                    v = min(v, C.REDLINES["score_cap"])
                out[f["code"]] = v
            return pd.Series(out, dtype=float)

        scores = {s: _recomb(weights[s][pool])
                  for s in SCHEMES if s not in ("S_pool", "S7")}
        fwd = pd.Series({f["code"]: f["fwd_1y"] for f in funds}, dtype=float)
        fmdd = pd.Series({f["code"]: f["fwd_mdd_1y"] for f in funds}, dtype=float)
        fvol = pd.Series({f["code"]: f.get("fwd_vol_1y") for f in funds}, dtype=float)
        # 事前平滑过滤（S7）：只用 T 时数据，波动与回撤均 ≤ 池均值×1.5
        v5 = pd.Series({f["code"]: f.get("vol_5y") for f in funds}, dtype=float)
        d5 = pd.Series({f["code"]: f.get("mdd_5y") for f in funds}, dtype=float)
        pass_smooth = (v5 <= v5.mean() * SMOOTH_MULT) & (d5 <= d5.mean() * SMOOTH_MULT)

        ok = scores["S0"].notna() & fwd.notna() & fmdd.notna()
        if int(ok.sum()) < MIN_PAIRS:
            continue
        fpct = fwd[ok].rank(pct=True)          # 前瞻收益在同池分位（1=最高）
        for s in SCHEMES:
            if s == "S_pool":
                m = pd.Series(True, index=ok[ok].index)
            else:
                src = "ret" if s == "S7" else s
                m = scores[src][ok].rank(pct=True) >= 0.8
                if s == "S7":
                    m = m & pass_smooth[ok].fillna(False)
            if int(m.sum()) < 3:
                continue
            idx = m[m].index
            f1, m1 = fwd[idx], fmdd[idx]
            v1 = fvol[idx].dropna()
            rows.append({
                "pool": pool, "T": T, "scheme": s, "n": int(m.sum()),
                "ret": float(f1.mean()), "mdd": float(m1.mean()),
                "vol": float(v1.mean()) if len(v1) else np.nan,
                "calmar": float(f1.mean() / max(float(m1.mean()), CALMAR_FLOOR)),
                "u2": float((f1 - 2 * m1).mean()),
                "top10hit": float((fpct[idx] >= 0.90).mean()),
            })
    cdf = pd.DataFrame(rows)
    cdf["split"] = np.where(cdf["T"] < NODES[TRAIN_NODES], "train", "valid")
    cdf["scheme_label"] = cdf["scheme"].map(LABEL)
    cdf.to_csv(ROOT / "data" / "backtest_obj_nodes.csv", index=False, encoding="utf-8-sig")

    # =============== ④ 汇总 + ★预注册决策
    def _agg(frame: pd.DataFrame) -> pd.DataFrame:
        a = frame.groupby("scheme")[["ret", "mdd", "vol", "calmar", "u2", "top10hit"]].mean()
        return a.reindex([s for s in SCHEMES if s in a.index])

    val, trn = _agg(cdf[cdf["split"] == "valid"]), _agg(cdf[cdf["split"] == "train"])
    log("\n=== ④ 训练段（前7节点）前20%组合前瞻表现（各池平均）===")
    log(trn.round(4).to_string())
    log("\n=== ④ 验证段（后4节点）前20%组合前瞻表现（各池平均）—— 决策依据 ===")
    log("ret/mdd/vol=前瞻年化  calmar=ret/max(mdd,0.5%)  u2=ret−2×mdd  top10hit=进同池前10%概率")
    log(val.round(4).to_string())

    log("\n验证段按池拆分（ret / mdd / u2）：")
    v = cdf[cdf["split"] == "valid"]
    log(v.pivot_table(index="scheme", columns="pool", values=["ret", "mdd", "u2"])
        .round(4).to_string())

    base_mdd = float(val.loc["S0", "mdd"])
    cand = val[val["mdd"] <= base_mdd]
    if len(cand):
        best = cand["u2"].idxmax()
        note = "满足 回撤≤现行 约束"
    else:
        best = val["mdd"].idxmin()
        note = "无方案满足回撤约束，退而取回撤最小者"
    log(f"\n★ 预注册决策：最佳方案 = {LABEL[best]}（{note}）")
    for k in ["u2", "mdd", "ret", "calmar", "top10hit"]:
        log(f"  {k}: 最佳 {val.loc[best, k]:.4f} vs 现行 {val.loc['S0', k]:.4f}")
    decision = {"best": best, "label": LABEL[best], "note": note,
                "valid": val.round(4).to_dict(orient="index"),
                "train": trn.round(4).to_dict(orient="index")}
    with open(ROOT / "data" / "backtest_obj_decision.json", "w", encoding="utf-8") as fh:
        json.dump(decision, fh, ensure_ascii=False, indent=1)
    log("\nDONE")


def main(argv=None):
    def log(*a):
        print(*a, flush=True)
    analyze(log=log)


if __name__ == "__main__":
    main()
