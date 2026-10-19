# -*- coding: utf-8 -*-
"""评分器：分类分池 → 指标 → 同池分位打分 → 红线 → 星级。

方法论：docs/01-债券基金评分指南.md (V5.2)。
流程：
  1. classify()   R0：纯债型(短债池/中长期池) / 固收+
  2. collect()    抓数据并计算全部指标
  3. score_pool() 同池缩尾分位打分 + 质地绝对分档 + 红线封顶 + 星级配额
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

from . import config as C
from . import data, metrics

SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "data" / "snapshots"


# ================================================================ R0 分类
# ---- 债券指数分池（跟踪标的久期） ----
# 依据抽样实测（151 只债券指数，2026-10-19）：短端标的（1-3年/短融/同业存单）近5年波动
# p50 0.65%、p90 0.79%；长端标的（1-5年/3-5年/7-10年/总财富）p50 1.21%、p10 0.89%——
# 两端重叠仅 0.8~0.9% 一段，故按标的久期分池即可分离；可转债指数实测波动 7.7~9.6%，不入池。
_IDX_EXCLUDE = ("可转债", "可交换", "转债")
# 久期区间：解析 "0-2年"/"1-3年"/"7-10年" 等写法，按区间上限判（≤3 年端、≥5 年长端）。
# 不解析裸 "N年"（如"30年国债""5年恒定久期"）——超长/固定久期极端性由实测波动终判更稳。
_DURATION_RNG = re.compile(r"(\d+)\s*[-~—–至]\s*(\d+)\s*年")
# 无区间写法时的兜底关键词（与上述实测分界一致）。不含"综合/信用债"——
# 这两类无固定久期（京津冀综合实测 0.24% 归短、农发总指数 1.39% 归长），一律交实测终判。
_IDX_SHORT = ("短融", "短期融资券", "同业存单", "存单", "超短", "短债")
_IDX_LONG = ("长久期", "总财富", "总指数", "中长期")


def _duration_pool(text: str) -> str | None:
    """按跟踪标的久期判定债券指数归池。None = 需实测波动终判或含转债。"""
    if not text:
        return None
    if any(k in text for k in _IDX_EXCLUDE):
        return None
    rng = _DURATION_RNG.findall(text)
    if rng:
        top = max(int(b) for _a, b in rng)
        if top <= 3:
            return "short_bond"
        if top >= 5:
            return "long_bond"
        return None                             # 上限 4 年（罕见）→ 交实测
    if any(k in text for k in _IDX_SHORT):
        return "short_bond"
    if any(k in text for k in _IDX_LONG):
        return "long_bond"
    return None                                 # 无区间也无关键词 → 交实测


def index_pool(name: str, benchmark: str = "") -> str | None:
    """债券指数基金分池：跟踪基准优先，名称兜底。

    指数基金买的是跟踪标的，基准才是合同口径——冲突时以基准为准；
    新成立基金名称若无久期关键词，靠基准文本判定（不沿用历史归池结果）。
    """
    if benchmark:
        r = _duration_pool(benchmark)
        if r is not None:
            return r
    return _duration_pool(name)


def _nav_vol_5y(nav_df: pd.DataFrame) -> float | None:
    """近5年年化波动（不足5年取全部），与指标层 vol_5y 同口径（√252）。"""
    if nav_df is None or nav_df.empty or "nav" not in nav_df.columns:
        return None
    idx = "date" if "date" in nav_df.columns else nav_df.columns[0]
    r = nav_df.set_index(idx)["nav"].sort_index().pct_change().dropna()
    if len(r) < 126:                              # 半年以下不判（新基金数据不足不下结论）
        return None
    end = r.index.max()
    r5 = r[r.index > end - pd.DateOffset(years=5)]
    return float(r5.std() * np.sqrt(252))


def classify(f10: dict, nav_df: pd.DataFrame) -> str:
    """返回 'short_bond' | 'long_bond' | 'fixed_plus' | EXCLUDED_POOL。

    规则：
      · 债券指数 → 跟踪标的久期分池（文本判 + 实测波动终判，见下）；
      · 混合二级/偏债混 → 固收+；混合一级按**行为**分层（近5年波动 < 固收+下限
        归长债池，≥ 下限才是真转债增强型 → 固收+）；
      · 纯债 → 短债/长债按久期（名称/类型含'短债'）。
    """
    type_text = f10.get("type_text", "")
    name = f10.get("name", "")
    if type_text.startswith("指数型"):
        # 债券指数三步分池（新成立基金不沿用历史归池，按当时基准+净值现算）：
        #  ① 跟踪标的久期文本判（跟踪基准优先、名称兜底）；
        #  ② 文本判不出（亚债中国/信用债综合/恒定久期/区域综合等）→ 近5年实测波动终判
        #     （实测分界：短端指数 p90=0.79%、长端 p10=0.89%，取 0.7% 为界）；
        #  ③ 可转债/可交换指数**无条件**排除（不看波动）；波动超纯债行为带(≥2.5%)、
        #     净值不足以下久期结论的 → 不入任何池。
        bench = f10.get("benchmark", "")
        p = index_pool(name, bench)
        if p is not None:
            return p
        if any(k in (name or "") + (bench or "") for k in _IDX_EXCLUDE):
            return EXCLUDED_POOL                  # 可转债/可交换指数：无论波动一律不入池
        v = _nav_vol_5y(nav_df)
        if v is None:
            return EXCLUDED_POOL                  # 新基金净值不足，不下久期结论
        if v < 0.007:
            return "short_bond"
        if v < (C.SCREENS["fixed_plus"].get("vol_5y_min") or 0.025):
            return "long_bond"
        return EXCLUDED_POOL                      # 波动超纯债带：可转债指数/30年超长债等
    if "混合" in type_text and "债券" in type_text:
        # 债券型-混合二级 → 固收+；混合一级按行为分层（2026-10-19 用户确认相似属性同池）：
        # 近5年波动 < 固收+下限(2.5%) 的属长债类行为（含少量转债但实测波动与长债无异），
        # 放长债池参与同类比较；≥2.5% 才是真转债增强型 → 固收+。数据不足则归固收+（现口径）。
        if type_text.endswith("混合一级"):
            v = _nav_vol_5y(nav_df)
            thr = C.SCREENS["fixed_plus"].get("vol_5y_min") or 0.025
            if v is not None and v < thr:
                return "long_bond"
            return "fixed_plus"
        return "fixed_plus"
    if "混合" in type_text or "偏债" in type_text or "灵活" in type_text:
        # 混合型-偏债/灵活：看基准是否含股指
        bench = f10.get("benchmark", "")
        if re.search(r"沪深300|中证|上证综指|创业板|恒生|股票", bench):
            return "fixed_plus"
        return "fixed_plus" if nav_df["nav"].pct_change().std() > 0.004 else "long_bond"
    # 纯债：短债池 vs 中长期池
    if "短债" in type_text or "短债" in name or "中短债" in name:
        return "short_bond"
    return "long_bond"


POOL_LABEL = {"short_bond": "纯债型·中短债池", "long_bond": "纯债型·长债池",
              "fixed_plus": "固收+池"}
# 非入池标记：净值指标照常采集，但不参与任何池的筛选/评分（可转债指数、超长债指数、
# 净值不足以判久期的新基金）。用专门值而非抛异常，避免每周重算都被计为失败并报警。
EXCLUDED_POOL = "excluded"


# ================================================================ 单基金指标采集
def collect_metrics(code: str) -> dict:
    """抓取一只基金的全部原始指标（未打分）。"""
    f10 = data.fetch_f10(code)
    nav_df = data.fetch_nav(code)
    if nav_df.empty or len(nav_df) < 60:
        raise ValueError(f"{code} 净值数据不足，无法评分")
    pool = classify(f10, nav_df)

    # 短债池用短端基准（1年以下），其余池用中债综合财富（全久期）
    bench = data.fetch_bond_index(short=(pool == "short_bond"))
    # 修复分档：EXCLUDED_POOL（不入任何池）无对应档，取长债档仅为把指标算完，不参与筛选
    sig_floor = C.REPAIR["sig_dd_floor"].get(pool) or C.REPAIR["sig_dd_floor"]["long_bond"]

    m = {"code": code, "name": f10.get("name", ""), "pool": pool,
         "type_text": f10.get("type_text", ""), "manager": f10.get("manager", ""),
         "establish_date": f10.get("establish_date", ""), "scale_yi": f10.get("scale_yi"),
         "benchmark": f10.get("benchmark", "")}

    # 历史长度
    m["age"] = metrics.age_years(m["establish_date"])
    nav_years = (nav_df["date"].max() - nav_df["date"].min()).days / 365.25
    m["nav_years"] = round(nav_years, 2)
    m["history_5y_partial"] = nav_years < 5
    m["insufficient_history"] = nav_years < 1

    # 收益/风险窗口
    for y, tag in [(5, "5y"), (3, "3y"), (1, "1y")]:
        m[f"ret_{tag}"] = metrics.ann_return(nav_df, y)
        m[f"mdd_{tag}"] = metrics.max_drawdown(nav_df, y)
        m[f"vol_{tag}"] = metrics.ann_vol(nav_df, y)
    m["dvol_5y"] = metrics.downside_vol(nav_df, 5)
    m["winrate_m_5y"] = metrics.monthly_win_rate(nav_df, 5)
    m["repair_5y"] = metrics.drawdown_repair(nav_df, 5, sig_floor)
    m["q_win_20q"] = metrics.quarterly_win_ratio(nav_df, 5)
    m["q_excess_20q"] = metrics.quarterly_excess_ratio(fund_nav=nav_df, bench=bench, years=5)
    m["yearly"] = metrics.yearly_returns(nav_df, 5)

    # 基准年化（纯债/固收+统一用中债综合财富做基准代理；正式版按基金合同基准取数）
    m["bench_ann_5y"] = metrics.bench_annualized(bench, 5)
    m["excess_5y"] = (m["ret_5y"] - m["bench_ann_5y"]) if (m["ret_5y"] is not None and m["bench_ann_5y"] is not None) else None

    # LVI（5y / 3y）
    b3 = metrics.bench_annualized(bench, 3)
    m["lvi_5y"] = metrics.lvi(m["ret_5y"], m["bench_ann_5y"], m["mdd_5y"], m["dvol_5y"],
                              **C.LVI_RC_WEIGHTS)
    m["lvi_3y"] = metrics.lvi(m["ret_3y"], b3, m["mdd_3y"], metrics.downside_vol(nav_df, 3),
                              **C.LVI_RC_WEIGHTS)
    m["_nav_df"] = nav_df   # 单日跌幅红线用

    # 质地
    m["fee_total"] = _sum_fee(f10)
    # 费率组件持久化（卡片展示 A 类口径：申购原价/优惠价 + 管理/托管/销售服务）
    m["mgmt_fee"] = f10.get("mgmt_fee")
    m["custody_fee"] = f10.get("custody_fee")
    m["sales_fee"] = f10.get("sales_fee")
    m["purchase_fee"] = f10.get("purchase_fee")
    m["purchase_fee_disc"] = f10.get("purchase_fee_disc")
    m["ms_rating"] = data.fetch_rating(code)
    m["inst_ratio"] = data.fetch_inst_ratio(code)
    m["mgr_career_years"] = data.fetch_manager_career_years(m["manager"], code) if m["manager"] else None
    prod_anns = data.fetch_manager_products_ann(f10.get("manager_id", ""))
    m["mgr_products_ann"] = float(np.median(prod_anns)) if prod_anns else None
    m["mgr_products_n"] = len(prod_anns)
    m["mgr_tenure_days"] = _tenure_days(f10.get("manager_start", ""))
    return m


def _sum_fee(f10: dict) -> float | None:
    parts = [f10.get("mgmt_fee"), f10.get("custody_fee"), f10.get("sales_fee")]
    vals = [p for p in parts if p is not None]
    return sum(vals) if vals else None


def _tenure_days(start: str) -> int | None:
    if not start:
        return None
    try:
        d = dt.datetime.strptime(start[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return (dt.datetime.now() - d).days


# ================================================================ 池内打分
def score_pool(fund_metrics: list[dict], score_only: set[str] | None = None,
               quota_n: int | dict[str, int] | None = None) -> list[dict]:
    """对同一池内基金打分（分位比较），返回结果列表。

    score_only: 只对这些 code 执行逐只打分；分位序列/池中位数/逐年排名仍由**全池**构建，
                因此子集分数与全量打分逐位一致（周更管线用：先筛后评，只评候选）。
                None = 全池打分（原行为）。
    quota_n: 星级配额的池基数（{pool: n} 或 int）。score_only 模式下必须传全池规模，
             否则配额按已打分数收缩（floor(n×pct) 会误伤高分基金）。None = 用已打分数。
    """
    pools: dict[str, list[dict]] = {}
    for m in fund_metrics:
        pools.setdefault(m["pool"], []).append(m)
    results = []
    for pool, funds in pools.items():
        weights = C.POOL_WEIGHTS[pool]
        if len(funds) < 5:
            for f in funds:
                f.setdefault("flags", []).append(
                    f"小样本池（n={len(funds)}），分位分仅供参考；正式评分需全市场同类池")
        # 池内回撤中位数（修复指标的显著性阈值用）
        med_dd = float(np.nanmedian([f["mdd_5y"] for f in funds if f["mdd_5y"] is not None])) if funds else 0.0
        # 逐年排名分位（rank_stab 用）：按年度收益在池内排位（全池计算——分位基准）
        stab_ranks = _pool_yearly_ranks(funds)
        for f in funds:      # 风险调整收益派生字段（v2 试验：calmar/sharpe → 池内分位计分）
            _risk_ratios(f)
        if score_only is not None:
            for f in funds:      # 标记未参与打分者（_assign_stars 跳过评星）
                if f["code"] not in score_only:
                    f["_unscored"] = True
        # 排名稳定性原值（近5年逐年池内排名百分位的标准差，越小越稳）。
        # 修复（2026-10-16）：赋值移到打分循环之前 → 分位基准=全池而非列表前缀，
        # 消除顺序依赖（原交错行为见 docs/08 §五 #3；重生成快照时一并修正）。
        for f in funds:
            f["rank_stab"] = metrics.rank_stability_score(stab_ranks.get(f["code"], []))
        for f in funds:
            if score_only is not None and f["code"] not in score_only:
                continue          # 只评候选；rank_stab 已赋值供后续 peer 分位使用
            try:
                _score_one(f, funds, weights, pool, med_dd, stab_ranks.get(f["code"], []))
            except Exception as e:      # 单只失败不影响整池（全量场景容错）
                f["score"] = f["score_capped"] = None
                f.setdefault("flags", []).append(f"打分异常: {type(e).__name__}")
        results.extend(funds)
    _assign_stars(results, quota_n)
    return results


def _pool_yearly_ranks(funds: list[dict]) -> dict[str, list[float | None]]:
    years = sorted({y for f in funds for y in f["yearly"].keys()})
    out: dict[str, list[float | None]] = {f["code"]: [] for f in funds}
    for y in years:
        vals = {f["code"]: f["yearly"].get(y) for f in funds if f["yearly"].get(y) is not None}
        if len(vals) < 2:
            for f in funds:
                out[f["code"]].append(None)
            continue
        s = pd.Series(vals)
        pcts = s.rank(pct=True)
        for f in funds:
            out[f["code"]].append(float(pcts.get(f["code"], np.nan)) if f["code"] in pcts else None)
    return out


def _risk_ratios(f: dict) -> None:
    """风险调整收益派生字段（v2 试验 2026-10-18）。

    calmar_5y = 近五年年化 ÷ 近五年最大回撤；sharpe_5y = (近五年年化 − 无风险利率) ÷ 波动。
    纯收益/纯超额保持权重 0（不追历史绝对收益），比率指标分子分母结合，对应
    "风险预算内卡玛最优"。数据不足（None/NaN/分母≈0）→ None（子项不计分）。
    """
    r, mdd, v = f.get("ret_5y"), f.get("mdd_5y"), f.get("vol_5y")
    r = None if (r is None or (isinstance(r, float) and np.isnan(r))) else r
    f["calmar_5y"] = (r / mdd) if (r is not None and mdd and mdd > 0) else None
    f["sharpe_5y"] = ((r - C.SHARPE_RF) / v) if (r is not None and v and v > 0) else None


def _score_one(f: dict, peers: list[dict], weights: dict, pool: str,
               med_dd: float, stab_ranks: list[float | None]) -> None:
    # ---- 分位指标（repair_5y 为字典，单独转标量后并入）
    pct_keys = [k for k in weights if k in C.PERCENTILE_METRICS and k != "repair_5y"]
    series = {k: pd.Series({p["code"]: p.get(k) for p in peers}, dtype=float) for k in pct_keys}
    # 修复指标：先转成标量（无显著回撤按成立年限折扣）
    rep_vals = {}
    for p in peers:
        rep_vals[p["code"]] = _repair_scalar(p, pool, weights)
    series["repair_5y"] = pd.Series(rep_vals, dtype=float)
    if "repair_5y" not in C.PERCENTILE_METRICS:
        pass
    scores = {}
    for k, s in series.items():
        # repair_5y 已转为"修复越快分越高"的标量，方向为 higher
        hb = (k == "repair_5y") or (C.PERCENTILE_METRICS.get(k, "lower") == "higher")
        if len(peers) < 2:
            scores[k] = 50.0 if s.get(f["code"]) is not None else None   # 单只池分位无意义，取中性
            continue
        sc = metrics.percentile_score(s, higher_better=hb)
        scores[k] = float(sc.get(f["code"], np.nan)) if not np.isnan(sc.get(f["code"], np.nan)) else None

    f["_stab_ranks"] = stab_ranks          # 供经理资质 R3 判定
    f["_peers_mdd1y"] = [p.get("mdd_1y") for p in peers if p.get("mdd_1y") is not None]

    # 固收+ 回撤分档打分（经理资质调节）——覆盖纯分位
    if pool == "fixed_plus":
        qual = _manager_qualified(f, peers)
        warn, cap = ((C.QUALIFIER["tol_full_warn"], C.QUALIFIER["tol_full_max"]) if qual
                     else (C.QUALIFIER["tol_plain_warn"], C.QUALIFIER["tol_plain_max"]))
        f["mgr_qualified"] = qual
        for k, full_at in [("mdd_5y", None), ("mdd_3y", None), ("mdd_1y", None)]:
            v = f.get(k)
            if v is None:
                scores[k] = None
            else:
                top = C.FIXED_PLUS_MDD["full_mark_at"]
                scores[k] = 100.0 if v <= top else max(0.0, 100.0 * (cap - v) / (cap - top))
        # 固收+ 波动同样"够稳即满分"（与回撤口径一致，避免低波基金被分位压分）
        if len(peers) >= C.RISK_THRESHOLD["min_pool"]:
            rt = C.RISK_THRESHOLD
            vals = [p.get("vol_5y") for p in peers if p.get("vol_5y") is not None]
            if vals and f.get("vol_5y") is not None:
                mu = float(np.mean(vals))
                if mu > 0:
                    full_at, zero_at = mu * rt["full_at_mult"], mu * rt["zero_at_mult"]
                    v = f["vol_5y"]
                    scores["vol_5y"] = (100.0 if v <= full_at else
                                        0.0 if v >= zero_at else
                                        100.0 * (zero_at - v) / (zero_at - full_at))

    # 纯债 回撤/波动"门槛给满分"——够稳即满分，超出线性扣分（用户口径，替代纯分位）
    if pool in ("short_bond", "long_bond") and len(peers) >= C.RISK_THRESHOLD["min_pool"]:
        rt = C.RISK_THRESHOLD
        for k in ("mdd_5y", "mdd_3y", "mdd_1y", "vol_5y"):
            vals = [p.get(k) for p in peers if p.get(k) is not None]
            if not vals or f.get(k) is None:
                continue
            mu = float(np.mean(vals))
            if mu <= 0:
                continue
            full_at, zero_at = mu * rt["full_at_mult"], mu * rt["zero_at_mult"]
            v = f[k]
            if v <= full_at:
                scores[k] = 100.0
            elif v >= zero_at:
                scores[k] = 0.0
            else:
                scores[k] = 100.0 * (zero_at - v) / (zero_at - full_at)

    # ---- 持续性：逐年表现（绝对规则）
    if "yearly_5y" in weights:
        scores["yearly_5y"] = _yearly_score(f, peers, pool)

    # ---- 质地绝对分档
    q = _quality_scores(f, peers)
    scores.update(q)

    # ---- 总分
    total, detail = 0.0, {}
    for k, w in weights.items():
        sc = scores.get(k)
        if sc is None or (isinstance(sc, float) and np.isnan(sc)):
            continue   # 数据不足子项不计（R2/R5 折算：按可得分等比缩放到 100）
        pts = w * sc / 100.0
        detail[k] = round(pts, 3)
        total += pts
    earned_weight = sum(w for k, w in weights.items()
                        if scores.get(k) is not None and not (isinstance(scores.get(k), float) and np.isnan(scores.get(k))))
    f["raw_score"] = round(total, 2)
    f["earned_weight"] = round(earned_weight, 2)
    f["score"] = round(total * 100 / earned_weight, 2) if earned_weight else None
    f["score_detail"] = detail
    f["sub_scores"] = {k: (round(v, 1) if v is not None and not (isinstance(v, float) and np.isnan(v)) else None)
                       for k, v in scores.items()}

    # ---- 红线 R7
    f["redlines"] = _check_redlines(f, pool, warn, cap) if pool == "fixed_plus" else _check_redlines(f, pool)
    if f["redlines"] and f["score"] is not None:
        f["score_capped"] = min(f["score"], C.REDLINES["score_cap"])
    else:
        f["score_capped"] = f["score"]


def _repair_scalar(p: dict, pool: str, weights: dict) -> float | None:
    """修复指标 → 0~100 标量（占子项满分比例×100），无显著回撤按成立年限折扣。"""
    r = p["repair_5y"]
    wmax = weights.get("repair_5y", 0)
    if wmax == 0:
        return None
    if not r["signif"]:
        age = p.get("age") or 0
        ratio = min(1.0, age / C.REPAIR["age_discount_cap"])
        return ratio * 100
    t = r["repair_days"]
    bands = C.REPAIR["bands_plus"] if pool == "fixed_plus" else C.REPAIR["bands_pure"]
    for lim, ratio in bands:
        if t <= lim:
            return ratio * 100
    return 0.0


def _manager_qualified(f: dict, peers: list[dict]) -> bool:
    """R3：任职期内每年被超过 ≤50%（前半）且平均被超过 ≤33.3%（前1/3），可得年份≥3。

    ranks = 池内收益分位（越高越好）；配置阈值语义为"被超过比例"（越低越好），
    故取 1-r 后比较。2026-10-19 修复原方向反转 bug（r 与阈值直接比大小 → 精英判负、
    垫底判胜）。
    """
    ranks = [r for r in (f.get("_stab_ranks") or []) if r is not None]
    if len(ranks) < C.QUALIFIER["min_years"]:
        return False
    beaten = [1.0 - r for r in ranks]
    return all(b <= C.QUALIFIER["yearly_rank_le"] for b in beaten) and \
        (sum(beaten) / len(beaten)) <= C.QUALIFIER["avg_rank_le"]


def _yearly_score(f: dict, peers: list[dict], pool: str) -> float | None:
    """近5年逐年：为正且前50%=2；为正但落后中位=1.2；为负=0；按可得年份折算。"""
    years = f["yearly"]
    if not years:
        return None
    got, n = 0.0, 0
    for y, ret in years.items():
        n += 1
        if ret <= 0:
            continue
        peer_rets = [p["yearly"].get(y) for p in peers if p["yearly"].get(y) is not None]
        median = np.median(peer_rets) if peer_rets else 0.0
        got += 2.0 if ret >= median else 1.2
    return (got / (2 * n)) * 100 if n else None


def _quality_scores(f: dict, peers: list[dict]) -> dict:
    """质地 9 项绝对分档 → 0~100。"""
    q = C.QUALITY
    out = {}
    # 成立年限
    age = f.get("age")
    out["age"] = None if age is None else (100 if age >= q["age_full_years"]
                                           else (60 if age >= q["age_mid_years"]
                                                 else max(0, age / q["age_mid_years"] * 60)))
    # 本基金任职年限
    t = f.get("mgr_tenure_days")
    if t is None:
        out["mgr_tenure"] = None
    else:
        ty = t / 365.25
        out["mgr_tenure"] = 100 if ty >= 5 else (70 if ty >= 3 else (30 if ty >= 1 else 0))
    # 从业年限
    c = f.get("mgr_career_years")
    out["mgr_career"] = None if c is None else (100 if c >= 8 else (70 if c >= 5 else (40 if c >= 3 else 0)))
    # 费率（同池分位，越低越好；A/C 份额差异由 fee_total 含销售服务费缓解，V6 分份额类别）
    fees = pd.Series({p["code"]: p.get("fee_total") for p in peers}, dtype=float)
    if f.get("fee_total") is None:
        out["fee"] = None
    elif len(peers) < 2:
        out["fee"] = 50.0
    else:
        out["fee"] = float(metrics.percentile_score(fees, higher_better=False).get(f["code"]))
    # 晨星
    r = f.get("ms_rating")
    out["ms_rating"] = 100 * q["ms_rating_map"].get(int(r), 0.5) if r else 50
    # 机构比例
    ir = f.get("inst_ratio")
    if ir is None:
        out["inst_ratio"] = None
    elif q["inst_ratio_full"][0] <= ir <= q["inst_ratio_full"][1]:
        out["inst_ratio"] = 100
    elif ir > q["inst_ratio_full"][1]:
        out["inst_ratio"] = 0
    elif ir >= q["inst_ratio_mid"][0]:
        out["inst_ratio"] = 50
    else:
        out["inst_ratio"] = 30
    # 基金规模 U 型
    s = f.get("scale_yi")
    if s is None:
        out["fund_scale"] = None
    else:
        out["fund_scale"] = next((r_ * 100 for lo, hi, r_ in q["fund_scale_u"] if lo <= s < hi), 0)
    # 公司规模：MVP 先按公司名映射 Top 公司（V6 接入非货规模数据）
    out["company_scale"] = _company_scale(f.get("name", ""))
    return out


_TOP_COMPANIES = {"易方达": 8000, "华夏": 8000, "广发": 6000, "富国": 5000, "招商": 6000,
                  "汇添富": 5000, "博时": 5000, "中欧": 4000, "天弘": 6000, "国泰": 4000,
                  "南方": 5000, "嘉实": 4000, "鹏华": 4000, "工银": 5000, "建信": 4000,
                  "国寿安保": 2000, "银华": 3000, "华安": 3000, "景顺长城": 3000, "兴全": 3000}


def _company_scale(name: str) -> float | None:
    for kw, yi in _TOP_COMPANIES.items():
        if kw in name:
            for threshold, ratio in C.QUALITY["company_scale_map"]:
                if yi >= threshold:
                    return ratio * 100
    return None   # 未收录公司 → 数据缺失不计分


def _check_redlines(f: dict, pool: str, warn: float | None = None,
                    cap: float | None = None) -> list[str]:
    hits = []
    if f.get("mgr_tenure_days") is not None and f["mgr_tenure_days"] < C.REDLINES["mgr_tenure_days"]:
        hits.append("R7-1 现任经理任职<6个月")
    s = f.get("scale_yi")
    if s is not None and s < C.REDLINES["scale_floor_yi"]:
        hits.append("R7-2 规模<5000万")
    # R7-3 回撤双条件
    thr = (cap if pool == "fixed_plus" and cap else C.REDLINES["mdd_abs_threshold"].get(pool))
    if thr and f.get("mdd_1y") is not None:
        peers_mdd = [p for p in f.get("_peers_mdd1y", []) if p is not None]
        med = np.median(peers_mdd) if peers_mdd else 0.0
        systemic = med >= thr / 2
        if systemic:
            if f["mdd_1y"] - med > thr / 2:
                hits.append("R7-3 超额回撤超阈值")
        elif f["mdd_1y"] > thr and (f["mdd_1y"] - med) > thr / 2:
            hits.append("R7-3 回撤双条件触发")
    # R7-4 单日异常跌幅且未修复
    nav_df = f.get("_nav_df")
    floor = C.REDLINES["single_day_abs_floor"].get(pool)
    if nav_df is not None and floor:
        rets = nav_df.set_index("date")["nav"].pct_change().dropna()
        end = nav_df["date"].max()
        rets = rets[rets.index > end - pd.DateOffset(years=1)]
        if len(rets):
            worst = rets.min()
            med_abs = float(rets.abs().median())
            if abs(worst) > max(C.REDLINES["single_day_mult"] * med_abs, floor):
                # 之后 20 个交易日是否修复一半
                i = rets.idxmin()
                after = rets.loc[i:]
                if len(after) and (1 + after).prod() < 1 + worst / 2:
                    hits.append("R7-4 单日异常跌幅且20日内未修复")
    # R7-5 固收+ 深回撤未修复
    if pool == "fixed_plus":
        r = f.get("repair_5y", {})
        if r.get("signif") and (r.get("repair_days") or 0) > C.REDLINES["unrecovered_days"]:
            hits.append("R7-5 深回撤超一年未修复")
    return hits


# ================================================================ 星级（含配额）
def _assign_stars(results: list[dict], quota_n: int | dict[str, int] | None = None) -> None:
    """按池分配星级。quota_n：配额基数（{pool: n} 或 int）——score_only 模式下
    未打分基金不在 scored 里，配额基数须显式传全池规模，保证与全量打分配额一致。"""
    pools: dict[str, list[dict]] = {}
    for r in results:
        pools.setdefault(r["pool"], []).append(r)
    for pool, funds in pools.items():
        scored = [f for f in funds if f.get("score_capped") is not None and not f.get("insufficient_history")]
        scored.sort(key=lambda x: -x["score_capped"])
        n = len(scored)
        base = quota_n if isinstance(quota_n, int) else \
            (quota_n.get(pool, n) if quota_n else n)   # 配额基数（默认=已打分数）
        # 占比配额仅在池样本足够时生效（否则 floor 后配额归零，高分也拿不到高星）
        quota_on = base >= C.STARS_MIN_POOL
        caps = {star: (int(np.floor(base * quota)) if (quota and quota_on) else n)
                for star, (_, quota) in C.STARS.items()}
        counts = {5: 0, 4: 0, 3: 0, 2: 0, 1: 0}
        for f in scored:
            sc = f["score_capped"]
            star = 1
            for s in (5, 4, 3, 2):
                floor_score, quota = C.STARS[s]
                if sc >= floor_score and (quota is None or counts[s] < caps[s]):
                    star = s
                    break
            counts[star] += 1
            f["star"] = star
            if not quota_on and quota:
                f.setdefault("flags", []).append("小样本池未启用星级配额")
        for f in funds:
            if f.get("_unscored"):
                continue   # score_only 模式下未参与打分的基金不评星
            if f.get("insufficient_history"):
                f["star"] = None
                f.setdefault("flags", []).append("insufficient_history 未满1年不入池/不评星")
            elif f.get("nav_years", 0) < C.HISTORY["min_star_years"]:
                f["star"] = None
                f.setdefault("flags", []).append("history_5y_partial 未满3年评分不评星")
            elif "star" not in f:
                f["star"] = 1


# ================================================================ 快照（§7 永不覆盖）
def save_snapshot(results: list[dict], path: Path | None = None) -> Path:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    today = dt.date.today().isoformat()
    path = path or SNAPSHOT_DIR / f"scores_{today}.json"
    slim = []
    for r in results:
        slim.append({k: v for k, v in r.items()
                     if k not in ("_nav_df", "_peers_mdd1y", "_stab_ranks", "_unscored")})
    payload = {"model_version": C.MODEL_VERSION, "generated_at": str(dt.datetime.now()),
               "n": len(slim), "results": slim}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
    return path
