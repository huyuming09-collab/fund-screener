# -*- coding: utf-8 -*-
"""卡片数据契约：榜单 + 详情 → JSON（top20.json / web/data.js）。

数据流：pipeline 筛评出 TOP-N 榜单（res DataFrame）→ build_payload() 组装
网页卡片所需的全部字段（含净值曲线采样），一次输出、离线渲染。

契约（funds[] 每项，docs/08 有字段表）：
    rank / code / name / base / category / score        榜单头部（无星级——用户口径）
    metrics    收益·回撤·波动·夏普·LVI·胜率等（share 级，排序所选份额口径）
    stages     阶段累计收益 1m/3m/6m/1y/3y/5y（现算）
    yearly     近 5 自然年收益
    manager    经理姓名/从业年限/任本基金/全产品年化
    fund_info  规模(基金级合并)/份额数/成立/费率/晨星/机构占比/基准
    screening  透明化"为什么入选"：综合分位/年度中位/近5年最大单日涨幅
    score_detail / sub_scores / redlines                评分明细与红线
    trend      净值曲线（1y/3y/5y，按 trend_max_points 降采样）
分类 tab：payload.categories 来自 config.CARD（框架占位，未启用分类禁用态）。
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from . import config as C
from . import metrics

DISCLAIMER = ("本页榜单由程序基于公开历史数据自动生成，仅为数据分析研究结论，"
              "不构成任何投资建议或基金推介；数据源（天天基金/AkShare 等）可能存在延迟与错误；"
              "历史业绩与评分不代表未来表现，基金有风险，投资须谨慎，据此操作风险自担。")


def _sample_nav(nav: pd.DataFrame, max_points: int) -> list[list]:
    """净值曲线降采样 → [[日期, 值], ...]（JSON 体积控制，保留首尾点）。"""
    if nav is None or nav.empty:
        return []
    n = len(nav)
    step = max(1, n // max_points)
    s = nav.iloc[::step]
    if s.index[-1] != nav.index[-1]:
        s = pd.concat([s, nav.iloc[[-1]]])
    return [[d.strftime("%Y-%m-%d"), round(float(v), 6)]
            for d, v in zip(s["date"], s["nav"])]


def trend_windows(nav: pd.DataFrame, max_points: int) -> dict[str, list]:
    """按窗口切分并采样净值曲线 {1y/3y/5y: [[d,v],...]}。"""
    out: dict[str, list] = {}
    if nav is None or nav.empty:
        return {w: [] for w in C.CARD["trend_windows"]}
    end = nav["date"].max()
    for w in C.CARD["trend_windows"]:
        years = int(w.rstrip("y"))
        sub = nav[nav["date"] > end - pd.DateOffset(years=years)]
        out[w] = _sample_nav(sub, max_points)
    return out


def _r(v, nd: int = 6):
    """NaN/inf → None，浮点按位数收敛（JSON 安全）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return round(f, nd)


def _rec_metric(rec: dict, key: str, nd: int = 6):
    return _r(rec.get(key), nd)


def build_fund(row: pd.Series, rec: dict, nav: pd.DataFrame | None,
               max_points: int) -> dict:
    """单只基金的卡片+详情数据（row=榜单行，rec=排序份额的指标记录）。"""
    stages = {w: _r(metrics.stage_return(nav, w), 6) if nav is not None else None
              for w in C.CARD["stage_windows"]}
    yearly = {str(y): _r(v, 6) for y, v in sorted((rec.get("yearly") or {}).items())}
    repair = rec.get("repair_5y") or {}
    return {
        "rank": int(row["rank"]),
        "code": row["code"],
        "name": row["name"],
        "base": row["base"],
        "category": "short_bond",
        "score": _r(row["score"], 2),
        "metrics": {
            "ret_1y": _rec_metric(rec, "ret_1y"),
            "ret_3y": _rec_metric(rec, "ret_3y"),
            "ret_5y": _rec_metric(rec, "ret_5y"),          # 近五年年化
            "mdd_1y": _rec_metric(rec, "mdd_1y"),
            "mdd_3y": _rec_metric(rec, "mdd_3y"),
            "mdd_5y": _rec_metric(rec, "mdd_5y"),          # 近五年最大回撤
            "vol_1y": _rec_metric(rec, "vol_1y"),
            "vol_5y": _rec_metric(rec, "vol_5y"),
            "sharpe_1y": _r(metrics.sharpe_ratio(rec.get("ret_1y"), rec.get("vol_1y")), 3),
            "sharpe_5y": _r(metrics.sharpe_ratio(rec.get("ret_5y"), rec.get("vol_5y")), 3),
            "lvi_5y": _rec_metric(rec, "lvi_5y", 3),
            "lvi_3y": _rec_metric(rec, "lvi_3y", 3),
            "winrate_m_5y": _rec_metric(rec, "winrate_m_5y", 4),
            "q_win_20q": _rec_metric(rec, "q_win_20q", 4),
            "q_excess_20q": _rec_metric(rec, "q_excess_20q", 4),
            "excess_5y": _rec_metric(rec, "excess_5y"),
            "bench_ann_5y": _rec_metric(rec, "bench_ann_5y"),
            "repair_days": repair.get("repair_days"),
            "repair_signif": bool(repair.get("signif", False)),
            "repair_recovered": bool(repair.get("recovered", False)),
        },
        "stages": stages,
        "yearly": yearly,
        "manager": {
            "name": row["manager"],
            "career_years": _r(row["career"], 1),
            "tenure_days": _r(row.get("tenure"), 0),  # 基金级：同经理份额最大任期
            "products_ann": _rec_metric(rec, "mgr_products_ann", 4),
        },
        "fund_info": {
            "scale_yi": _r(row["scale_sum"], 2),            # 基金级合并规模
            "n_classes": int(row["n_classes"]),
            "establish_date": rec.get("establish_date", ""),
            "age": _rec_metric(rec, "age", 2),
            "nav_years": _r(row["nav_years"], 2),
            "fee_total": _rec_metric(rec, "fee_total", 4),
            "ms_rating": _rec_metric(rec, "ms_rating", 1),
            "inst_ratio": _rec_metric(rec, "inst_ratio", 4),
            "benchmark": rec.get("benchmark", ""),
        },
        "screening": {
            "comp_pct": _r(row["comp_pct"], 4),             # 综合分位（P1 入选依据）
            "yearly_med": _r(row["yearly_med"], 4),         # 年度中位（P2 入选依据）
            "max_gain_5y": _r(row["max_gain_5y"], 6),       # 近5年最大单日涨幅（S5）
            "scale_sum": _r(row["scale_sum"], 2),
        },
        "score_detail": {k: _r(v, 3) for k, v in (rec.get("score_detail") or {}).items()},
        "sub_scores": {k: _r(v, 1) for k, v in (rec.get("sub_scores") or {}).items()},
        "redlines": list(rec.get("redlines") or []),
        "trend": trend_windows(nav, max_points),
    }


def build_payload(res: pd.DataFrame, snapshot: dict, navs: dict,
                  funnel: dict | None = None, asof: str | None = None) -> dict:
    """榜单 → 完整卡片数据契约（TOP-N 榜单 + 每只的详情数据）。"""
    max_points = C.CARD["trend_max_points"]
    funds = []
    for _, row in res.iterrows():
        rec = snapshot.get(row["code"])
        if rec is None:
            rec = snapshot.get(row["src_code"]) or {}
        nav = navs.get(row["code"])
        if nav is None:
            nav = navs.get(row["src_code"])
        funds.append(build_fund(row, rec, nav, max_points))
    return {
        "model_version": C.MODEL_VERSION,
        "generated_at": str(dt.datetime.now()),
        "asof": asof or str(dt.date.today()),
        "category": "short_bond",
        "top_n": C.TOP_N,
        "n": len(funds),
        "sharpe_rf": C.SHARPE_RF,
        "categories": C.CARD["categories"],
        "funnel": funnel or {},
        "funds": funds,
        "disclaimer": DISCLAIMER,
    }


def write_payload(payload: dict, json_path, js_path=None) -> None:
    """原子写出 top20.json 与 web/data.js（data.js = window.CARD_DATA 赋值）。"""
    import json
    from pathlib import Path

    text = json.dumps(payload, ensure_ascii=False, indent=1, default=str)
    jp = Path(json_path)
    jp.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(jp) + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(jp)
    if js_path:
        wp = Path(js_path)
        wp.parent.mkdir(parents=True, exist_ok=True)
        tmpj = Path(str(wp) + ".tmp")
        tmpj.write_text("window.CARD_DATA = " + text + ";\n", encoding="utf-8")
        tmpj.replace(wp)
