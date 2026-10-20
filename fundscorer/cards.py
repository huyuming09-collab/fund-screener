# -*- coding: utf-8 -*-
"""卡片数据契约：榜单 + 详情 → JSON（top20.json / web/data.js）。

数据流：pipeline 筛评出 TOP-N 榜单（res DataFrame）→ build_payload() 组装
网页卡片所需的全部字段（含净值曲线采样），一次输出、离线渲染。

契约（funds[] 每项，docs/08 有字段表）：
    rank / code / name / base / category / score        榜单头部（无星级——用户口径）
    rank_prev / rank_delta / is_new                     环比上一期（无历史 → 全 None/False）
    metrics    收益·回撤·波动·夏普·LVI·胜率等（share 级，排序所选份额口径）
    stages     阶段累计收益 1m/3m/6m/1y/3y/5y（现算）
    yearly     近 5 自然年收益
    manager    经理姓名/从业年限/任本基金/全产品年化
    fund_info  规模(基金级合并)/份额数/成立/费率(申购原价+优惠价+管理等)/晨星/机构占比/基准
    screening  透明化"为什么入选"：综合分位/年度中位/近5年最大单日涨幅
    score_detail / sub_scores / redlines                评分明细与红线
    trend      净值曲线（仅 1y+5y；3y 由前端从 5y 按日期切片，省 60% trend 体积）
分类 tab：payload.categories 来自 config.CARD（未启用分类禁用态）；
    多池时 payload.pools = {pool_id: {n, n_screened, top_n, has_prev, funnel, screen}}，
    逐只 category 随 res 列写入，页面按 tab 过滤 funds（screen = 该池筛选阈值，
    详情页"入选依据/筛选阈值"按池取值）。顶层单池字段取主池（旧页面前向兼容）。
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from . import config as C
from . import data as D
from . import metrics

DISCLAIMER = ("本页榜单由程序基于公开历史数据自动生成，仅为数据分析研究结论，"
              "不构成任何投资建议或基金推介；数据来自公开渠道，可能存在延迟与错误；"
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


def _i(v):
    """整数透传；NaN/None/inf → None（环比字段用）。"""
    f = _r(v, 0)
    return None if f is None else int(f)


def _rec_metric(rec: dict, key: str, nd: int = 6):
    return _r(rec.get(key), nd)


def _flag(row: pd.Series, key: str) -> bool:
    """行内布尔标记（is_new / is_index）：缺列或 NaN 一律 False。

    不能写 bool(row.get(key, False))——pd.concat 对缺该列的行补的是 np.nan，
    而 bool(np.nan) is True：固收+池无指数基金时 res 缺 is_index 列，合并后
    该池 20 只主动基金会被整体误标成指数基金。
    """
    v = row.get(key, False)
    return bool(v) if pd.notna(v) else False


def _opt_dict(row, key):
    """行内可选字典列（score_basis）：缺列/NaN → None（dict 才透传）。"""
    v = row.get(key, None) if hasattr(row, "get") else None
    return v if isinstance(v, dict) else None


def _style_label(ret, vol, med_ret, med_vol):
    """风格标签（池内相对，仅展示不计分，docs/12 §8）：按本池候选中位切四象限。

    进取型 = 收益波动双高；稳健型 = 收益高波动低；防守型 = 双低；震荡型 = 收益低波动高。
    任一输入缺失 → None（前端不渲染标签）。
    """
    if ret is None or vol is None or med_ret is None or med_vol is None:
        return None
    hi_r, hi_v = ret >= med_ret, vol >= med_vol
    if hi_r and hi_v:
        return "进取型"
    if hi_r:
        return "稳健型"
    if hi_v:
        return "震荡型"
    return "防守型"


def build_fund(row: pd.Series, rec: dict, nav: pd.DataFrame | None,
               max_points: int, category: str = "short_bond") -> dict:
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
        "category": category,
        "score": _r(row["score"], 2),
        # 环比上一期：prev_rank=None 表示无历史（首期），is_new 仅在有历史且上期缺席时为 True
        "rank_prev": _i(row.get("rank_prev")),
        "rank_delta": _i(row.get("rank_delta")),
        "is_new": _flag(row, "is_new"),
        # 指数基金穿插入榜标记：前端据此可标注"按跟踪标的评估，不看经理业绩"口径
        "is_index": _flag(row, "is_index"),
        # 申购状态（2026-10-20 用户要求）：suspended = 暂停/封闭/未开放（"限大额"仍可小额申购）
        "purchase_status": rec.get("purchase_status"),
        "suspended": D.sgzt_suspended(rec.get("purchase_status")),
        # 指数评分依据：业绩位置插值的上下邻锚点（主动基金为 None）——卡片可核对
        # "业绩落在谁和谁之间、分数取这两个分数之间"
        "score_basis": _opt_dict(row, "score_basis"),
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
            "products_n": _rec_metric(rec, "mgr_products_n", 0),  # 计入中位数的产品数（透明化）
            # v5.5 共管（docs/12 §9）：现任经理全量；主责=任本基金最久（is_primary 标记）。
            # 旧缓存无 managers → []，前端按单经理渲染。
            "co_managers": [{"name": x.get("name", ""),
                             "tenure_days": _r(x.get("tenure_days"), 0),
                             "is_primary": bool(x.get("name") == row["manager"])}
                            for x in (rec.get("managers") or [])],
            # 任期卡玛（展示用、不计分）：任期年化收益 ÷ max(任期回撤, 0.5%)
            "tenure_calmar": _rec_metric(rec, "mgr_tenure_calmar", 2),
            # 团队系数（v5.7 直觉规则，docs/12 §15）：满 3 年共管人数 + 经理块放大系数
            "team_n": int(rec.get("mgr_team_n") or 0),
            "team_coef": (_r(rec.get("mgr_team_coef"), 2) or 1.0),
            # "seats" = 该经理当期上榜占席数，build_payload 全榜统计后注入
        },
        "fund_info": {
            "scale_yi": _r(row["scale_sum"], 2),            # 基金级合并规模
            "n_classes": int(row["n_classes"]),
            "establish_date": rec.get("establish_date", ""),
            "age": _rec_metric(rec, "age", 2),
            "nav_years": _r(row["nav_years"], 2),
            # 费率（A 类=排序份额口径）：申购原价/天天基金优惠价 + 管理/托管/销售服务
            "purchase_fee": _rec_metric(rec, "purchase_fee", 4),
            "purchase_fee_disc": _rec_metric(rec, "purchase_fee_disc", 4),
            "mgmt_fee": _rec_metric(rec, "mgmt_fee", 4),
            "custody_fee": _rec_metric(rec, "custody_fee", 4),
            "sales_fee": _rec_metric(rec, "sales_fee", 4),
            "fee_total": _rec_metric(rec, "fee_total", 4),
            "ms_rating": _rec_metric(rec, "ms_rating", 1),
            "inst_ratio": _rec_metric(rec, "inst_ratio", 4),
            "benchmark": rec.get("benchmark", ""),
            # 指数基金同指数去重的优先键之一（非货规模，亿元）；主动基金无 → null
            "company_scale": _r(rec.get("company_scale"), 2),
        },
        "screening": {
            "comp_pct": _r(row["comp_pct"], 4),             # 综合分位（P1 入选依据）
            "yearly_med": _r(row["yearly_med"], 4),         # 年度中位（P2 入选依据）
            "max_gain_5y": _r(row["max_gain_5y"], 6),       # 近5年最大单日涨幅（S5）
            "scale_sum": _r(row["scale_sum"], 2),
        },
        "score_detail": {k: _r(v, 3) for k, v in (rec.get("score_detail") or {}).items()},
        "sub_scores": {k: _r(v, 1) for k, v in (rec.get("sub_scores") or {}).items()},
        # 池内对比复算用：Σ score_detail = raw_score；score = raw_score × 100 / earned_weight
        "raw_score": _r(rec.get("raw_score"), 2),
        "earned_weight": _r(rec.get("earned_weight"), 2),
        "redlines": list(rec.get("redlines") or []),
        "trend": trend_windows(nav, max_points),
    }


def build_payload(res: pd.DataFrame, snapshot: dict, navs: dict,
                  funnel: dict | None = None, asof: str | None = None,
                  prev_ranks: dict | None = None,
                  n_screened: int | None = None,
                  pool_stats: dict | None = None) -> dict:
    """榜单 → 完整卡片数据契约（TOP-N 榜单 + 每只的详情数据）。

    prev_ranks  上一期 {base: rank}；None=无历史（首期，全部不算新增、不显示环比）。
                有历史时：本期 base 不在其中 → is_new=True；rank_delta=上期名次−本期名次。
    n_screened  筛后候选数（列表头部"筛出 N 只"显式字段，替代脆弱的漏斗键名）。
    pool_stats  多池：{pool_id: {n, n_screened, top_n, has_prev, funnel, screen}} → payload["pools"]。
                res 需携带 category 列（pipeline 逐池附加；缺省按 short_bond 处理）。
    顶层字段（category/top_n/n/n_screened/has_prev/funnel）在给定 pool_stats 时取**首个池**
                （= PIPELINE.pools 主池）的统计——旧 data.js/旧页面的单池语义不变，
                逐池真值走 pools 映射；不给 pool_stats 时按传入参数（单池调用）原样输出。
    """
    max_points = C.CARD["trend_max_points"]
    funds = []
    for _, row in res.iterrows():
        rec = snapshot.get(row["code"])
        if rec is None:
            rec = snapshot.get(row["src_code"]) or {}
        nav = navs.get(row["code"])
        if nav is None:
            nav = navs.get(row["src_code"])
        cat = row.get("category") or row.get("pool") or "short_bond"
        funds.append(build_fund(row, rec, nav, max_points, category=cat))
    primary = next(iter(pool_stats), None) if pool_stats else None
    ps = pool_stats.get(primary, {}) if primary else {}
    # v5.5 风格标签（池内相对、仅展示不计分）：按本池候选中位切四象限
    for f in funds:
        sb = (pool_stats or {}).get(f["category"], {}).get("style_base") or {}
        f["style"] = _style_label((f.get("metrics") or {}).get("ret_5y"),
                                  (f.get("metrics") or {}).get("vol_5y"),
                                  sb.get("ret_5y"), sb.get("vol_5y"))
    # v5.5 经理占席（全榜跨池统计，提示集中度；仅展示不扣分，docs/12 §9）
    seats: dict = {}
    for f in funds:
        nm = (f.get("manager") or {}).get("name")
        if nm:
            seats[nm] = seats.get(nm, 0) + 1
    for f in funds:
        nm = (f.get("manager") or {}).get("name")
        f["manager"]["seats"] = seats.get(nm) if nm else None
    # 各池统计 + 评分权重/分块（权重来自 config.POOL_WEIGHTS，供网页「池内横向对比」复算与分区）
    pools = {}
    for pid, st in (pool_stats or {}).items():
        pools[pid] = dict(st)
        pools[pid]["weights"] = C.POOL_WEIGHTS.get(pid)
        pools[pid]["weight_groups"] = C.WEIGHT_GROUPS.get(pid)
    return {
        "model_version": C.MODEL_VERSION,
        "generated_at": str(dt.datetime.now()),
        "asof": asof or str(dt.date.today()),
        "category": primary or "short_bond",
        "top_n": ps.get("top_n", C.TOP_N),
        "n": ps.get("n", len(funds)),
        "n_screened": ps.get("n_screened", n_screened),
        "has_prev": ps.get("has_prev", prev_ranks is not None),
        "sharpe_rf": C.SHARPE_RF,
        "categories": C.CARD["categories"],
        "funnel": funnel or ps.get("funnel", {}),
        "pools": pools,
        "funds": funds,
        "disclaimer": DISCLAIMER,
    }


def write_payload(payload: dict, json_path, js_path=None) -> None:
    """原子写出 top20.json（indent 便于人工 diff）与 web/data.js（紧凑省体积）。"""
    import json
    from pathlib import Path

    text = json.dumps(payload, ensure_ascii=False, indent=1, default=str)
    jp = Path(json_path)
    jp.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(jp) + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(jp)
    if js_path:
        # data.js 用紧凑分隔符：周更每周提交+手机弱网加载，体积敏感（实测可省 ~45%）
        compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                             default=str)
        wp = Path(js_path)
        wp.parent.mkdir(parents=True, exist_ok=True)
        tmpj = Path(str(wp) + ".tmp")
        tmpj.write_text("window.CARD_DATA = " + compact + ";\n", encoding="utf-8")
        tmpj.replace(wp)
