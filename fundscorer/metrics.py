# -*- coding: utf-8 -*-
"""指标计算：窗口收益、回撤、波动、修复、LVI、缩尾分位。

所有函数以 pandas 日频净值序列（列 [date, nav]）为输入，纯计算、无 IO。
口径定义见 docs/01-债券基金评分指南.md §5。
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from . import config as C

TRADING_DAYS = 252


# ---------------------------------------------------------------- 窗口切片
def window_returns(nav_df: pd.DataFrame, years: int) -> pd.Series:
    """近 N 年日收益率（不足窗口则返回全部可得）。"""
    end = nav_df["date"].max()
    start = end - pd.DateOffset(years=years)
    sub = nav_df[nav_df["date"] > start]
    return sub.set_index("date")["nav"].pct_change().dropna()


def ann_return(nav_df: pd.DataFrame, years: int) -> float | None:
    """近 N 年年化收益。历史不足窗口上限时用全期年化替代（R2）。"""
    end = nav_df["date"].max()
    start = end - pd.DateOffset(years=years)
    sub = nav_df[nav_df["date"] > start]
    if len(sub) < 2:
        return None
    total = sub["nav"].iloc[-1] / sub["nav"].iloc[0] - 1
    days = (sub["date"].iloc[-1] - sub["date"].iloc[0]).days
    if days <= 0:
        return None
    return (1 + total) ** (365 / days) - 1


def max_drawdown(nav_df: pd.DataFrame, years: int) -> float | None:
    """近 N 年最大回撤（正数，如 0.02 = 回撤 2%）。"""
    rets = window_returns(nav_df, years)
    if rets.empty:
        return None
    nav = (1 + rets).cumprod()
    dd = 1 - nav / nav.cummax()
    return float(dd.max())


def ann_vol(nav_df: pd.DataFrame, years: int) -> float | None:
    rets = window_returns(nav_df, years)
    if len(rets) < 20:
        return None
    return float(rets.std() * np.sqrt(TRADING_DAYS))


def downside_vol(nav_df: pd.DataFrame, years: int) -> float | None:
    """下行波动率：仅负收益日。"""
    rets = window_returns(nav_df, years)
    neg = rets[rets < 0]
    if len(neg) < 10:
        return None
    return float(neg.std() * np.sqrt(TRADING_DAYS))


# ---------------------------------------------------------------- 夏普比率（仅展示不计分，docs/01 §V5-b）
def sharpe_ratio(ann_ret: float | None, ann_vol: float | None,
                 rf: float | None = None) -> float | None:
    """夏普 = (年化收益 − 无风险利率) / 年化波动。rf 缺省取 config.SHARPE_RF。"""
    if rf is None:
        rf = C.SHARPE_RF
    if ann_ret is None or ann_vol is None or ann_vol <= 0:
        return None
    return float((ann_ret - rf) / ann_vol)


# ---------------------------------------------------------------- 阶段累计收益（卡片详情页展示）
def stage_return(nav_df: pd.DataFrame, window: str) -> float | None:
    """近 X 窗口累计收益率（不年化）。window 形如 '1m'/'3m'/'6m'/'1y'/'3y'/'5y'。"""
    if not window:
        return None
    unit, n = window[-1], int(window[:-1])
    if n <= 0:
        return None
    end = nav_df["date"].max()
    start = end - (pd.DateOffset(months=n) if unit == "m" else pd.DateOffset(years=n))
    sub = nav_df[nav_df["date"] > start]
    if len(sub) < 2:
        return None
    return float(sub["nav"].iloc[-1] / sub["nav"].iloc[0] - 1)


# ---------------------------------------------------------------- 回撤修复（§5.1）
def drawdown_repair(nav_df: pd.DataFrame, years: int, sig_floor: float,
                    peer_median_dd: float | None = None) -> dict:
    """近 N 年最深一次显著回撤的修复情况。

    返回 {signif: bool, depth: float|None, repair_days: int|None, recovered: bool}
    repair_days=None 且 signif=False 表示无显著回撤（按成立年限折扣给分，见 scorer）。
    """
    rets = window_returns(nav_df, years)
    if rets.empty:
        return {"signif": False, "depth": None, "repair_days": None, "recovered": False}
    nav = (1 + rets).cumprod()
    cummax = nav.cummax()
    dd = 1 - nav / cummax
    threshold = max(sig_floor, 0.5 * (peer_median_dd or 0.0))
    if dd.max() < threshold:
        return {"signif": False, "depth": float(dd.max()), "repair_days": None, "recovered": True}
    # 找最深谷底
    trough = dd.idxmax()
    depth = float(dd.loc[trough])
    # 修复基准 = 谷底前高（cummax 在谷底处的运行最大值）。
    # 曾误用 cummax.iloc[0]（窗口起点净值 ≈1.0）→ 谷底当日即"已修复"，天数恒为1、
    # 该指标对所有显著回撤基金同档失效（2026-10-16 用户报告后修复，docs/08 §五）。
    peak_val = float(cummax.loc[trough])
    after = nav.loc[trough:]
    vals = after.to_numpy()
    pos = int(np.argmax(vals >= peak_val))
    recovered = bool(vals[pos] >= peak_val)
    # 修复天数 = 谷底之后到修复日的交易日数（不含谷底当日；未修复计至窗口末）
    repair_days = pos if recovered else int(len(vals) - 1)
    return {"signif": True, "depth": depth, "repair_days": repair_days, "recovered": recovered}


# ---------------------------------------------------------------- 月度胜率
def monthly_win_rate(nav_df: pd.DataFrame, years: int) -> float | None:
    rets = window_returns(nav_df, years)
    if rets.empty:
        return None
    monthly = (1 + rets).resample("ME").prod() - 1
    if len(monthly) < 3:
        return None
    return float((monthly > 0).mean())


# ---------------------------------------------------------------- 年度/季度
def yearly_returns(nav_df: pd.DataFrame, max_years: int = 5) -> dict[int, float]:
    """近 max_years 个自然年收益 {年份: 收益率}。"""
    end_year = nav_df["date"].max().year
    out = {}
    for y in range(end_year - max_years + 1, end_year + 1):
        yr = nav_df[(nav_df["date"].dt.year == y)]
        if len(yr) < 20:   # 不完整年份跳过
            continue
        # 年收益用上年末净值到本年内的口径
        prev = nav_df[nav_df["date"] < f"{y}-01-01"]
        base = prev["nav"].iloc[-1] if len(prev) else yr["nav"].iloc[0]
        out[y] = float(yr["nav"].iloc[-1] / base - 1)
    return out


def quarterly_win_ratio(nav_df: pd.DataFrame, years: int) -> float | None:
    """近 N 年正收益季度占比。"""
    rets = window_returns(nav_df, years)
    if rets.empty:
        return None
    q = (1 + rets).resample("QE").prod() - 1
    return float((q > 0).mean()) if len(q) >= 4 else None


def quarterly_excess_ratio(fund_nav: pd.DataFrame, bench: pd.DataFrame, years: int) -> float | None:
    """近 N 年跑赢基准的季度占比。"""
    f = window_returns(fund_nav, years)
    if f.empty:
        return None
    fq = (1 + f).resample("QE").prod() - 1
    b = bench.set_index("date")["value"].pct_change().dropna()
    end = fund_nav["date"].max()
    b = b[b.index > end - pd.DateOffset(years=years)]
    bq = (1 + b).resample("QE").prod() - 1
    joined = pd.concat([fq, bq], axis=1, join="inner").dropna()
    if joined.empty:
        return None
    return float((joined.iloc[:, 0] > joined.iloc[:, 1]).mean())


# ---------------------------------------------------------------- LVI（§5.4）
def lvi(fund_ann: float | None, bench_ann: float | None,
        mdd: float | None, dvol: float | None,
        rc_w_mdd: float = 0.6, rc_w_dvol: float = 0.4,
        rc_floor: float = C.LVI_RC_FLOOR) -> float | None:
    """长期性价比指数 = 超额收益 / 风险代价；风险代价 = 0.6×回撤 + 0.4×下行波动。

    rc_floor：风险代价下限。"理财债/货币替代品"回撤仅 0.01%、几乎无可测风险，此时
    LVI 分母趋零、数值爆炸（±200），无经济意义 → 低于下限直接返回 None（取同池中性分位）。
    """
    if fund_ann is None or mdd is None:
        return None
    rc = rc_w_mdd * mdd + rc_w_dvol * (dvol if dvol is not None else mdd)
    if rc < rc_floor:
        return None   # 风险过低、LVI 不稳定，交由 scorer 取同池中性分位
    excess = fund_ann - (bench_ann or 0.0)
    return excess / rc


def bench_annualized(bench: pd.DataFrame, years: int) -> float | None:
    end = bench["date"].max()
    start = end - pd.DateOffset(years=years)
    sub = bench[bench["date"] > start]
    if len(sub) < 2:
        return None
    total = sub["value"].iloc[-1] / sub["value"].iloc[0] - 1
    days = (sub["date"].iloc[-1] - sub["date"].iloc[0]).days
    return (1 + total) ** (365 / days) - 1 if days > 0 else None


# ---------------------------------------------------------------- 缩尾百分位（§5.4/R4）
def winsorize(s: pd.Series, lower: float = 0.05, upper: float = 0.95) -> pd.Series:
    if s.dropna().empty:
        return s
    lo, hi = s.quantile(lower), s.quantile(upper)
    return s.clip(lo, hi)


def percentile_score(s: pd.Series, higher_better: bool) -> pd.Series:
    """缩尾后转 0~100 分位得分。逆向指标取 100-p。NaN 保持 NaN。"""
    w = winsorize(s)
    if w.dropna().empty:
        return w * 0
    p = w.rank(pct=True) * 100
    return p if higher_better else 100 - p


# ---------------------------------------------------------------- 排名稳定性
def rank_stability_score(yearly_ranks: list[float]) -> float | None:
    """近 5 自然年逐年同类排名百分位的标准差（越小越稳）。至少 3 年。"""
    vals = [r for r in yearly_ranks if r is not None]
    if len(vals) < 3:
        return None
    return float(np.std(vals))


# ---------------------------------------------------------------- 成立年限
def age_years(establish_date: str) -> float | None:
    if not establish_date:
        return None
    try:
        d = dt.datetime.strptime(establish_date[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return (dt.datetime.now() - d).days / 365.25
