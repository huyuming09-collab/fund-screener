# -*- coding: utf-8 -*-
"""固收+ 评分体系 v6 探索（研究脚本，不入主链、无单测——与 backtest.py/backtest2.py 同类）。

需求（2026-10-19 用户）：
  1) 双目标：「高收益高波动」与「中高收益低波动」两类都要能上榜且排名靠前；
  2) 指标不设限：所列表仅作参考（夏普/索提诺可合并冗余），未点名因子（如近1年回撤）亦可用；
  3) 流程：设计 → 探索 → 验证 → **回检**（回头自检是否符合实际逻辑）。

数据：data/backtest_panel.pkl（11 截面 T=2019-06-30..2024-06-30，含前瞻 1y/2y 收益·回撤·波动）
      data/nav_full/*.pkl（日净值，算下行波动/净值≥5年闸）

用法：
  python -m fundscorer.explore_v6 ic       # 因子 IC：训练/验证双段 × {前瞻收益, 前瞻回撤, 前瞻卡玛}
  python -m fundscorer.explore_v6 corr     # 因子 Spearman 冗余矩阵（找重复族）
  python -m fundscorer.explore_v6 schemes  # 候选权重方案对比（TOP20% 前瞻 + 双目标达成度）
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from . import config as C
from . import metrics as M
from . import scorer as S

ROOT = Path(__file__).resolve().parent.parent
PANEL_PATH = ROOT / "data" / "backtest_panel.pkl"
NAV_DIR = ROOT / "data" / "nav_full"
CACHE_PATH = ROOT / "data" / "explore_v6_panel.pkl"

POOL = "fixed_plus"
TRAIN_T = pd.Timestamp("2021-12-31")     # 前 7 节点训练 / 后 4 节点验证（同 backtest.py TRAIN_NODES=7）
FLOOR = 0.005                           # 卡玛回撤地板（同 backtest2.CALMAR_FLOOR）
RF = C.SHARPE_RF                        # 1.5%
MIN_PAIRS = 30

# 因子全集（面板直取 + 派生），DIRECTION = IC 方向对齐（+1 越大越好）
PANEL_FACTORS = ["ret_5y", "ret_3y", "ret_1y", "mdd_5y", "mdd_3y", "mdd_1y", "vol_5y",
                 "winrate_m_5y", "q_win_20q", "q_excess_20q", "excess_5y",
                 "lvi_5y", "lvi_3y", "repair_5y", "age",
                 "mgr_tenure", "mgr_career", "fee"]
DERIVED_FACTORS = ["calmar_5y", "sharpe_5y", "sortino_5y", "u2_5y", "yearly_5y", "rank_stab"]
DIRECTION = {**{k: +1 for k in ("ret_5y", "ret_3y", "ret_1y", "winrate_m_5y", "q_win_20q",
                                "q_excess_20q", "excess_5y", "lvi_5y", "lvi_3y", "age",
                                "mgr_tenure", "mgr_career", "calmar_5y", "sharpe_5y",
                                "sortino_5y", "u2_5y", "yearly_5y")},
             **{k: -1 for k in ("mdd_5y", "mdd_3y", "mdd_1y", "vol_5y", "fee", "rank_stab")},
             # repair_5y 面板值来自 _repair_scalar（修复越快分越高）→ 判向 +1（backtest.FACTOR_DIR 同）
             "repair_5y": +1}
ALL_FACTORS = PANEL_FACTORS + DERIVED_FACTORS
TARGETS = ["fwd_1y", "fwd_mdd_1y", "fwd_calmar"]


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- 1) 面板富化（带缓存）
def _spearman(x: pd.Series, y: pd.Series) -> float | None:
    if len(x) < MIN_PAIRS or x.nunique() < 2 or y.nunique() < 2:
        return None
    v = x.corr(y, method="spearman")
    return None if v is None or np.isnan(v) else float(v)


def build_panel(force: bool = False) -> pd.DataFrame:
    """面板 + 派生因子（卡玛/夏普/索提诺/u2/年度分位/排名稳定）+ 净值≥5年闸。"""
    if CACHE_PATH.exists() and not force:
        return pd.read_pickle(CACHE_PATH)
    df = pd.read_pickle(PANEL_PATH)
    df = df[df["pool"] == POOL].copy()

    # ---- 派生比率因子（与 scorer 同口径）
    df["calmar_5y"] = df["ret_5y"] / df["mdd_5y"].clip(lower=FLOOR)
    df["sharpe_5y"] = np.where(df["vol_5y"] > 0, (df["ret_5y"] - RF) / df["vol_5y"], np.nan)
    df["u2_5y"] = df["ret_5y"] - 2 * df["mdd_5y"]

    # ---- 索提诺（下行波动需净值，逐 code×T 现算）+ 净值≥5年闸
    dv, nav5 = {}, {}
    codes = df["code"].unique()
    for i, code in enumerate(codes, 1):
        path = NAV_DIR / f"{code}.pkl"
        if not path.exists():
            continue
        nav = pd.read_pickle(path)
        nav["date"] = pd.to_datetime(nav["date"])
        nav = nav.sort_values("date")
        first = nav["date"].min()
        for T in df.loc[df["code"] == code, "T"].unique():
            nav5[(code, T)] = bool(first <= T - pd.DateOffset(years=5))
            sub = nav[(nav["date"] > T - pd.DateOffset(years=5)) & (nav["date"] <= T)]
            if len(sub) >= 100:
                dv[(code, T)] = M.downside_vol(sub, 5)
        if i % 400 == 0:
            log(f"  净值处理 {i}/{len(codes)}")
    df["dvol_5y"] = [dv.get((c, t), np.nan) for c, t in zip(df["code"], df["T"])]
    df["nav5_ok"] = [nav5.get((c, t), False) for c, t in zip(df["code"], df["T"])]
    df["sortino_5y"] = np.where(df["dvol_5y"] > 0, (df["ret_5y"] - RF) / df["dvol_5y"], np.nan)

    # ---- 池级因子（年度分位 / 排名稳定，同 backtest.analyze 口径）
    df["yearly_5y"] = np.nan
    df["rank_stab"] = np.nan
    for T, g in df.groupby("T"):
        funds = g.to_dict("records")
        if len(funds) < MIN_PAIRS:
            continue
        ranks = S._pool_yearly_ranks(funds)
        df.loc[g.index, "yearly_5y"] = [
            np.nan if v is None else float(v) for v in (S._yearly_score(f, funds, POOL) for f in funds)]
        df.loc[g.index, "rank_stab"] = [
            np.nan if v is None else float(v) for v in
            (M.rank_stability_score(ranks.get(f["code"], [])) for f in funds)]

    # ---- 前瞻目标
    df["fwd_calmar"] = df["fwd_1y"] / df["fwd_mdd_1y"].clip(lower=FLOOR)
    df["split"] = np.where(df["T"] <= TRAIN_T, "train", "valid")
    df.to_pickle(CACHE_PATH)
    log(f"富化面板 {len(df)} 行 × {df['code'].nunique()} 基金 → {CACHE_PATH.name}")
    return df


# ---------------------------------------------------------------- 2) IC 表
def stage_ic() -> None:
    df = build_panel()
    strict = df[df["nav5_ok"]].copy()
    log(f"\n=== 因子 IC（pool={POOL}，净值≥5年闸，n={len(strict)} 行 / "
        f"{strict['code'].nunique()} 基金）===")
    log(f"训练段 T≤2021-12-31，验证段 2022 起 | 目标：前瞻收益/前瞻回撤/前瞻卡玛"
        f"（卡玛=前瞻收益÷max(前瞻回撤,{FLOOR:.3f})）\n")

    rows = []
    # 目标判向：前瞻收益/前瞻卡玛越大越好；前瞻回撤越小越好（故 ×−1 对齐）
    TGT_DIR = {"fwd_1y": +1, "fwd_mdd_1y": -1, "fwd_calmar": +1}
    for (split, T), g in strict.groupby(["split", "T"]):
        if len(g) < MIN_PAIRS:
            continue
        for fac in ALL_FACTORS:
            if fac not in g or g[fac].isna().all():
                continue
            s = g[fac].dropna()
            rec = {"split": split, "T": T, "factor": fac, "n": len(s)}
            for tgt in TARGETS:
                rec[tgt] = _spearman(s, g.loc[s.index, tgt])
            rows.append(rec)
    ic = pd.DataFrame(rows)

    # 汇总：因子方向 × 目标方向 对齐（全表 >0 = 方向正确），按段取均值与 ICIR
    out = []
    for fac, g in ic.groupby("factor"):
        d = DIRECTION.get(fac, +1)
        rec = {"factor": fac, "dir": d}
        for split in ("train", "valid"):
            gs = g[g["split"] == split]
            for tgt in TARGETS:
                v = gs[tgt].dropna() * d * TGT_DIR[tgt]
                rec[f"{split}_{tgt}"] = v.mean() if len(v) else np.nan
                rec[f"{split}_{tgt}_icir"] = (v.mean() / v.std(ddof=1)) if len(v) > 1 and v.std(ddof=1) > 0 else np.nan
                rec[f"{split}_n"] = len(v)
        out.append(rec)
    res = pd.DataFrame(out).set_index("factor")

    pd.set_option("display.width", 220, "display.max_columns", 30)
    cols = ["train_fwd_1y", "valid_fwd_1y", "train_fwd_mdd_1y", "valid_fwd_mdd_1y",
            "train_fwd_calmar", "valid_fwd_calmar", "valid_fwd_calmar_icir", "valid_n"]
    log("—— 方向对齐后 IC（>0 = 方向正确）；排序按 验证段·前瞻卡玛 ——")
    show = res[cols].rename(columns={
        "train_fwd_1y": "trn_收益", "valid_fwd_1y": "val_收益",
        "train_fwd_mdd_1y": "trn_回撤", "valid_fwd_mdd_1y": "val_回撤",
        "train_fwd_calmar": "trn_卡玛", "valid_fwd_calmar": "val_卡玛",
        "valid_fwd_calmar_icir": "val_卡玛ICIR", "valid_n": "val_n"}).round(3)
    log(show.sort_values("val_卡玛", ascending=False).to_string())
    res.to_csv(ROOT / "data" / "explore_v6_ic.csv")
    log("\n→ data/explore_v6_ic.csv")


# ---------------------------------------------------------------- 3) 冗余矩阵
def stage_corr(threshold: float = 0.70) -> None:
    df = build_panel()
    strict = df[df["nav5_ok"]]
    facs = [f for f in ALL_FACTORS if strict[f].notna().sum() >= MIN_PAIRS]
    # 方向不重要——看 |ρ| 是否冗余；用秩相关
    sub = strict[facs].rank()
    cm = sub.corr(method="pearson")   # 已 rank → 等价 spearman
    pd.set_option("display.width", 240, "display.max_columns", 40)
    log(f"\n=== 因子冗余矩阵（|ρ|，pool={POOL}，净值≥5年，n={strict.shape[0]}）===\n")
    log(cm.abs().round(2).to_string())

    pairs = []
    for i, a in enumerate(facs):
        for b in facs[i + 1:]:
            r = cm.loc[a, b]
            if pd.notna(r) and abs(r) >= threshold:
                pairs.append((a, b, round(float(r), 3)))
    pairs.sort(key=lambda p: -abs(p[2]))
    log(f"\n—— |ρ|≥{threshold:.0%} 的冗余对（设计时同族择优/合并）——")
    for a, b, r in pairs:
        log(f"  {a:16s} ↔ {b:16s}  ρ={r:+.3f}")
    if not pairs:
        log("  （无）")


# ---------------------------------------------------------------- 4) 方案对比
# ★ 预注册判定（2026-10-19 跑前定死，防事后挑方案——沿用 backtest2 纪律）：
#   合格 = 验证段 TOP20 前瞻卡玛 ≥ 0.95 × S0（不显著劣化名单前瞻质量）；
#   合格者中，双目标表示度 = min(高波高收益组 H 表示度, 中高收益低波组 L 表示度) 最大者胜；
#   并列 → 验证段前瞻卡玛更高者。
UNAVAIL = {"inst_ratio", "fund_scale", "company_scale", "ms_rating",
           "mgr_products_ann", "mgr_tenure_calmar"}   # 面板无字段 → 模拟中丢权重、earned 折算

SCHEMES = {
    # S0 = 现行 B′（v2.0 线上权重，风险门槛式 + 卡玛/夏普 + 胜率 + 费率 + 经理）
    "S0 现行B′": {
        "mdd_1y": 10, "mdd_3y": 8, "mdd_5y": 6, "vol_5y": 11,
        "calmar_5y": 15, "sharpe_5y": 10,
        "winrate_m_5y": 9, "q_win_20q": 8,
        "fee": 6, "mgr_tenure": 3,
        "inst_ratio": 2, "mgr_products_ann": 11, "ms_rating": 1},
    # S1 = 用户参考指标表原样（不参考其分数；面板不可得的 3 项丢弃）
    "S1 用户表": {
        "calmar_5y": 15, "sharpe_5y": 10, "sortino_5y": 10,
        "mdd_5y": 10, "mdd_3y": 5, "repair_5y": 10,
        "ret_5y": 6, "ret_3y": 4, "winrate_m_5y": 5, "q_win_20q": 5,
        "fee": 4, "fund_scale": 4, "inst_ratio": 4,
        "mgr_tenure": 3, "mgr_career": 2, "mgr_tenure_calmar": 3},
    # S2 = v6 证据方案：风险门槛守门 + 比率族三选一（calmar，ICIR 最高）+ 胜率 + 费率；收益/超额 0
    "S2 v6证据": {
        "mdd_1y": 8, "mdd_3y": 5, "mdd_5y": 5, "vol_5y": 8,      # 风险门槛 26
        "calmar_5y": 16,                                          # 比率 16（三选一，ρ≥0.90 同族）
        "winrate_m_5y": 13, "q_win_20q": 11,                      # 胜率 24
        "fee": 10, "age": 3, "mgr_tenure": 5,                     # 运营 18
        "inst_ratio": 4, "fund_scale": 4, "mgr_products_ann": 6, "ms_rating": 2},  # 结构 16
    # S3 = 收益可见度实验：S2 不变，把无证据的结构块 16→4 挪 12 分给 收益/超额
    "S3 S2+收益超额": {
        "mdd_1y": 8, "mdd_3y": 5, "mdd_5y": 5, "vol_5y": 8,
        "calmar_5y": 16,
        "winrate_m_5y": 13, "q_win_20q": 11,
        "ret_5y": 6, "q_excess_20q": 6,                           # 收益可见度 12（挪自结构块）
        "fee": 10, "age": 3, "mgr_tenure": 5,
        "inst_ratio": 2, "fund_scale": 2},
}

TOPN = 20          # 榜单规模（与线上 TOP_N 同口径）


def _pct_score(s: pd.Series, higher: bool) -> pd.Series:
    w = M.winsorize(s)
    p = w.rank(pct=True) * 100
    return p if higher else 100 - p


def _mech(fac: str) -> str:
    if fac in ("mdd_1y", "mdd_3y", "mdd_5y", "vol_5y"):
        return "thr"          # 门槛式（线上 RISK_THRESHOLD：够稳即满分）
    if fac == "mgr_tenure":
        return "tier_t"       # 绝对分档 100/70/30/0（线上 _manager 分档）
    if fac == "age":
        return "tier_a"       # 绝对分档 100/60/线性（线上 _quality）
    return "pct"              # 池内分位（判向 DIRECTION）


def _node_subscores(g: pd.DataFrame, schemes: dict | None = None) -> dict:
    """单节点内各因子子分（机制与线上同口径；面板缺失因子自动跳过）。"""
    schemes = schemes if schemes is not None else SCHEMES
    out = {}
    for fac in {k for w in schemes.values() for k in w}:
        if fac in UNAVAIL or fac not in g:
            continue
        s = g[fac]
        if s.notna().sum() < 5:
            continue
        m = _mech(fac)
        if m == "thr":
            mu = float(s.dropna().mean())
            if mu <= 0:
                continue
            full = mu * C.RISK_THRESHOLD["full_at_mult"]
            zero = mu * C.RISK_THRESHOLD["zero_at_mult"]
            out[fac] = s.map(lambda v: np.nan if pd.isna(v) else
                             (100.0 if v <= full else (0.0 if v >= zero
                              else 100.0 * (zero - v) / (zero - full))))
        elif m == "tier_t":
            out[fac] = s.map(lambda v: np.nan if pd.isna(v) else
                             (100.0 if v >= 5 else 70.0 if v >= 3 else 30.0 if v >= 1 else 0.0))
        elif m == "tier_a":
            out[fac] = s.map(lambda v: np.nan if pd.isna(v) else
                             (100.0 if v >= 5 else 60.0 if v >= 3 else max(0.0, v / 3 * 60)))
        else:
            out[fac] = _pct_score(s, DIRECTION.get(fac, +1) > 0)
    return out


def _apply_scheme(g: pd.DataFrame, subs: dict, weights: dict) -> pd.Series:
    """总分 = Σ(w×子分/100) × 100 / earned_weight（与 scorer 同折算）。"""
    tot = pd.Series(0.0, index=g.index)
    earned = pd.Series(0.0, index=g.index)
    for fac, w in weights.items():
        if fac in UNAVAIL or fac not in subs:
            continue
        s = subs[fac]
        ok = s.notna()
        tot[ok] += w * s[ok] / 100.0
        earned[ok] += w
    return pd.Series(np.where(earned > 0, tot * 100 / earned.replace(0, np.nan), np.nan),
                     index=g.index)


def _quasi_screen(g: pd.DataFrame) -> pd.DataFrame:
    """准筛后池：S4 成立≥5年 · S6 回撤≤10% · S7 波动带2.5%~8%。
    S2/S3（经理闸）不进方案对比：它们是身份过滤而非评分项，且历史截面上早期从业/任期
    普遍偏短（2019 节点全池仅 21%/8% 过闸），合并后节点样本不足（11~56）无法做 TOP20；
    经理因子（mgr_tenure 等）仍以评分项参与，所有方案同池，横向可比不受影响。
    （S1 规模 / S5 单日异动 面板无字段，放弃。）"""
    return g[(g["age"] >= 5) & (g["mdd_5y"] <= 0.10)
             & (g["vol_5y"] >= 0.025) & (g["vol_5y"] <= 0.08)].copy()


def stage_schemes() -> None:
    df = build_panel()
    df = df[df["nav5_ok"] & df["fwd_1y"].notna()].copy()

    node_rows, rank_rows = [], []
    for T, g0 in df.groupby("T"):
        g0 = g0.assign(split=np.where(g0["T"] <= TRAIN_T, "train", "valid"))
        g = _quasi_screen(g0)
        if len(g) < 60:                      # 样本太少不进方案对比
            continue
        # 双目标象限：池内按 (收益中位, 波动中位) 切 H/L（高收益高波动 / 中高收益低波）
        med_r, med_v = g["ret_5y"].median(), g["vol_5y"].median()
        g["grp"] = np.where((g["ret_5y"] >= med_r) & (g["vol_5y"] >= med_v), "H",
                   np.where((g["ret_5y"] >= med_r) & (g["vol_5y"] < med_v), "L",
                   np.where((g["ret_5y"] < med_r) & (g["vol_5y"] >= med_v), "M", "Lo")))
        share_pool = g["grp"].value_counts(normalize=True).to_dict()
        subs = _node_subscores(g)
        k = max(10, min(TOPN, round(0.2 * len(g))))
        n = len(g)
        for name, w in SCHEMES.items():
            sc = _apply_scheme(g, subs, w)
            if sc.notna().sum() < 30:
                continue
            g["score"] = sc
            g["rank"] = g["score"].rank(ascending=False, method="first")
            g["rank_pct"] = (n - g["rank"] + 1) / n * 100
            top = g[g["rank"] <= k]
            node_rows.append({
                "scheme": name, "T": T, "n_pool": n, "k": len(top),
                "fwd_ret": top["fwd_1y"].mean(), "fwd_mdd": top["fwd_mdd_1y"].mean(),
                "fwd_calmar": top["fwd_1y"].mean() / max(top["fwd_mdd_1y"].mean(), FLOOR),
                "rho": _spearman(sc, g["fwd_calmar"]),
                "split": g0["split"].iloc[0],
                "share_pool": share_pool,
                "share_top": top["grp"].value_counts(normalize=True).to_dict()})
            for h, sub in g.groupby("grp"):
                rank_rows.append({"scheme": name, "T": T, "grp": h,
                                  "split": g0["split"].iloc[0],
                                  "med_rank_pct": float(sub["rank_pct"].median()),
                                  "top_in": int((sub["rank"] <= k).sum()),
                                  "n_grp": int(len(sub))})
    nodes = pd.DataFrame(node_rows)
    ranks = pd.DataFrame(rank_rows)

    pd.set_option("display.width", 240, "display.max_columns", 30)
    lines = ["=== 方案对比（准筛后池，TOP20；前瞻=其后 1 年真实表现）==="]
    agg = nodes.groupby(["scheme", "split"]).agg(
        前瞻收益=("fwd_ret", "mean"), 前瞻回撤=("fwd_mdd", "mean"),
        前瞻卡玛=("fwd_calmar", "mean"), rho=("rho", "mean"),
        节点数=("T", "nunique")).reset_index()
    for split in ("train", "valid"):
        lines.append(f"\n—— {split} 段 ——")
        sub = agg[agg["split"] == split].set_index("scheme").drop(columns="split")
        lines.append(sub.round(4).to_string())

    lines.append("\n\n=== 双目标达成度（H=高收益高波动组 / L=中高收益低波组）===")
    lines.append("表示度 = TOP 内占比 ÷ 池内占比（1.0=未被排斥，<1=被压，>1=受偏爱）")
    for split in ("train", "valid"):
        lines.append(f"\n—— {split} 段 ——")
        rows = []
        for name in SCHEMES:
            sub = nodes[(nodes["scheme"] == name) & (nodes["split"] == split)]
            if not len(sub):
                continue
            r = {"scheme": name}
            for gp, lbl in (("H", "H表示度"), ("L", "L表示度")):
                p = np.mean([x.get(gp, 0) for x in sub["share_pool"]])
                t = np.mean([x.get(gp, 0) for x in sub["share_top"]])
                r[lbl] = (t / p) if p > 0 else np.nan
            rs = ranks[(ranks["scheme"] == name) & (ranks["split"] == split)]
            for gp, lbl in (("H", "H名次分位"), ("L", "L名次分位")):
                x = rs[rs["grp"] == gp]["med_rank_pct"]
                r[lbl] = x.mean() if len(x) else np.nan
            rows.append(r)
        lines.append(pd.DataFrame(rows).set_index("scheme").round(3).to_string())

    lines.append("\n=== ★ 预注册判定（验证段；合格=前瞻卡玛≥0.95×S0，取 min(H,L) 表示度最大）===")
    base = agg[(agg["scheme"] == "S0 现行B′") & (agg["split"] == "valid")]
    if len(base):
        b = float(base["前瞻卡玛"].iloc[0])
        verdict = []
        for name in SCHEMES:
            row = agg[(agg["scheme"] == name) & (agg["split"] == "valid")]
            if not len(row):
                continue
            fc = float(row["前瞻卡玛"].iloc[0])
            sub = nodes[(nodes["scheme"] == name) & (nodes["split"] == "valid")]
            mins = []
            for gp in ("H", "L"):
                p = np.mean([x.get(gp, 0) for x in sub["share_pool"]])
                t = np.mean([x.get(gp, 0) for x in sub["share_top"]])
                mins.append((t / p) if p > 0 else np.nan)
            verdict.append((name, "合格" if fc >= 0.95 * b else "劣化", fc, float(np.nanmin(mins))))
        vd = pd.DataFrame(verdict, columns=["scheme", "判定", "验证段前瞻卡玛", "min(H,L)表示度"])
        lines.append(vd.round(4).to_string(index=False))
        elig = vd[vd["判定"] == "合格"].sort_values(["min(H,L)表示度", "验证段前瞻卡玛"],
                                                    ascending=False)
        lines.append("\n★ 预注册胜出：" + (
            f"{elig.iloc[0]['scheme']}（min表示度 {elig.iloc[0]['min(H,L)表示度']:.3f}，"
            f"前瞻卡玛 {elig.iloc[0]['验证段前瞻卡玛']:.4f}，门槛 {b * 0.95:.4f}）"
            if len(elig) else "无合格方案（全部劣化）→ 保留 S0 / 调整结构重验"))
    log("\n".join(lines))
    nodes.to_csv(ROOT / "data" / "explore_v6_schemes.csv", index=False)
    ranks.to_csv(ROOT / "data" / "explore_v6_groups.csv", index=False)
    log("\n→ data/explore_v6_schemes.csv / explore_v6_groups.csv")


# ---------------------------------------------------------------- 5) 跑后探索（★非预注册，诚实标注）
# 两个回检触发的追加实验：
#   (a) 剂量-反应：预注册判定显示"给收益/超额加分"可提 H 组可见度但伤前瞻质量（S3），
#       这里把它做成剂量曲线（0/3/6/9 分，资金一律从无证据的结构块挪出），量化权衡比；
#   (b) 同族择优：S0 的 calmar 与 sharpe 同族（ρ=0.902），S4 把夏普 10 分并入卡玛，
#       检验"只留同族代表"是否改变榜单行为（若不变 → 结构清理是免费的）。
DOSE = {
    "S0 现行B′": SCHEMES["S0 现行B′"],
    "S2 剂量0": SCHEMES["S2 v6证据"],
    "D3 剂量3": {**SCHEMES["S2 v6证据"],
                 "inst_ratio": 2, "fund_scale": 2, "ms_rating": 0,
                 "ret_5y": 1.5, "q_excess_20q": 1.5},
    "D6 剂量6": SCHEMES["S3 S2+收益超额"],
    "D9 剂量9": {**SCHEMES["S2 v6证据"],
                 "mgr_products_ann": 0, "inst_ratio": 2, "fund_scale": 2, "ms_rating": 1,
                 "ret_5y": 4.5, "q_excess_20q": 4.5},
    "S4 同族择优": {**SCHEMES["S0 现行B′"], "sharpe_5y": 0, "calmar_5y": 25},
}


def stage_dose() -> None:
    df = build_panel()
    df = df[df["nav5_ok"] & df["fwd_1y"].notna()].copy()
    rows = []
    for T, g0 in df.groupby("T"):
        g0 = g0.assign(split=np.where(g0["T"] <= TRAIN_T, "train", "valid"))
        g = _quasi_screen(g0)
        if len(g) < 60:
            continue
        med_r, med_v = g["ret_5y"].median(), g["vol_5y"].median()
        g["grp"] = np.where((g["ret_5y"] >= med_r) & (g["vol_5y"] >= med_v), "H",
                   np.where((g["ret_5y"] >= med_r) & (g["vol_5y"] < med_v), "L", "x"))
        p_h = float(g["grp"].eq("H").mean())
        subs = _node_subscores(g, DOSE)
        k = max(10, min(TOPN, round(0.2 * len(g))))
        for name, w in DOSE.items():
            sc = _apply_scheme(g, subs, w)
            if sc.notna().sum() < 30:
                continue
            g["score"] = sc
            g["rank"] = g["score"].rank(ascending=False, method="first")
            top = g[g["rank"] <= k]
            rows.append({
                "scheme": name, "T": T, "split": g0["split"].iloc[0],
                "fwd_calmar": top["fwd_1y"].mean() / max(top["fwd_mdd_1y"].mean(), FLOOR),
                "fwd_ret": top["fwd_1y"].mean(), "fwd_mdd": top["fwd_mdd_1y"].mean(),
                "rho": _spearman(sc, g["fwd_calmar"]),
                "H_repr": (float(top["grp"].eq("H").mean()) / p_h) if p_h > 0 else np.nan})
    d = pd.DataFrame(rows)
    base = d[d["scheme"] == "S0 现行B′"].set_index("T")["fwd_calmar"]
    pd.set_option("display.width", 220)
    log("\n=== ★跑后探索（非预注册）(a) 收益/超额剂量-反应 + (b) 同族择优 A/B ===")
    for split in ("train", "valid"):
        sub = d[d["split"] == split]
        agg = sub.groupby("scheme").agg(
            前瞻卡玛=("fwd_calmar", "mean"), 前瞻收益=("fwd_ret", "mean"),
            前瞻回撤=("fwd_mdd", "mean"), rho=("rho", "mean"),
            H表示度=("H_repr", "mean")).reset_index()
        # 逐节点胜场（对 S0，按本段节点对齐后比较）
        ts = list(sub["T"].unique())
        base_s = base.reindex(ts)
        wins = {n: int((sub[sub["scheme"] == n].set_index("T")["fwd_calmar"]
                        .reindex(ts) > base_s).sum())
                for n in DOSE if n in set(sub["scheme"])}
        agg["胜S0节点"] = agg["scheme"].map(wins)
        log(f"\n—— {split} 段（节点数 {sub['T'].nunique()}）——")
        log(agg.drop(columns=[]).round(4).to_string(index=False))
    log("\n读法：剂量↑ → H表示度↑ = 高波高收益组更可见；同时看前瞻卡玛是否跌破 0.95×S0。")


# ---------------------------------------------------------------- 入口
def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    stage = argv[0] if argv else "ic"
    force = "--force" in argv
    if force:
        argv = [a for a in argv if a != "--force"]
    if stage == "ic":
        stage_ic()
    elif stage == "corr":
        stage_corr()
    elif stage == "schemes":
        stage_schemes()
    elif stage == "dose":
        stage_dose()
    else:
        log(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
