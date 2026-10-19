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
        # "名字 vs 实际波动"配图数据（?export=namegap）：全量快照里名字带"稳"但实际波动最大的若干只
        "namegap": _namegap(),
        "funnel": funnel or ps.get("funnel", {}),
        "pools": pool_stats or {},
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
def _namegap(n: int = 4) -> dict:
    """封面配图「名字带稳的，实际有多不稳」的实证数据。

    从全量指标缓存（data/metrics_cache.pkl，非榜单子集）里挑名字含"稳/双利/恒益/安心"
    的债券类+偏债类产品，按**年化波动**降序取前 n；对照条取中短债池同期波动中位数。

    合规：**不输出公司名**——展示为「类型标签 · 名称去掉公司前缀」，如
    「偏债混合 · 稳健增长混合」，保留"名字带稳"的反例属性但不指向具体产品；
    同一基金的 A/C/D/E 份额去重。每周随缓存自动更新，不写死。
    """
    import pickle
    import re
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "data" / "metrics_cache.pkl"
    recs = {}
    if src.exists():
        try:
            raw = pickle.load(open(src, "rb"))
            recs = raw if isinstance(raw, dict) else {r.get("code"): r for r in raw}
        except Exception:
            recs = {}
    KW = ("稳", "双利", "恒益", "安心")
    # 对照基准单独一轮：中短债池**全体**（净值≥5年、历史完整、有波动值），
    # 不叠加 mdd/名称/类型过滤 —— 与图3「分类依据」的全池口径保持一致
    base = [float(r["vol_5y"]) for r in recs.values()
            if r.get("pool") == "short_bond" and r.get("vol_5y") is not None
            and (r.get("nav_years") or 0) >= 5 and not r.get("insufficient_history")]
    TYPE_TAG = {"混合型-偏债": "偏债混合", "债券型-混合二级": "二级债基", "债券型-信用债": "信用债",
                "债券型-利率债": "利率债", "债券型-长债": "长债", "债券型-中短债": "中短债",
                "债券型-一级": "一级债基", "混合型-偏债混合": "偏债混合"}
    seen, rows = set(), []
    for r in recs.values():
        nm, v, m = r.get("name") or "", r.get("vol_5y"), r.get("mdd_5y")
        if v is None or m is None or not nm:
            continue
        # 口径闸：图上写「近5年」就必须是真满 5 年净值。ann_return/ann_vol 在历史不足时
        # 会退化为全期计算但字段名不变，故此处硬性拦截（含 insufficient_history 标记）
        if (r.get("nav_years") or 0) < 5 or r.get("insufficient_history"):
            continue
        tt = r.get("type_text") or ""
        if any(x in nm for x in ("指数", "FOF", "货")) or any(x in tt for x in ("指数", "FOF")):
            continue
        if not any(k in nm for k in KW):
            continue
        core = re.sub(r"[A-EＡ-Ｅ]\s*$", "", nm)          # 去份额后缀做去重与展示
        if core in seen:
            continue
        idx = next((nm.find(k) for k in KW if nm.find(k) >= 1), -1)
        rest = nm[idx:].strip() if idx >= 1 else core
        rest = re.sub(r"[A-EＡ-Ｅ]\s*$", "", rest)
        tag = next((v2 for k2, v2 in TYPE_TAG.items() if k2 in tt), (tt[:6] or "债类"))
        # 合规脱敏优先级：机构量级描述（人工维护）> 类型标签兜底；均不输出公司名
        SENS = {"稳健增长混合": "头部机构 · 稳健增长混合", "稳宏债券": "老牌机构 · 稳宏债券",
                "稳健配置混合": "大厂 · 稳健配置混合", "恒益稳健混合": "中型机构 · 恒益稳健混合"}
        label = next((SENS[k] for k in SENS if k in nm), f"{tag} · {rest}")
        seen.add(core)
        rows.append({"label": label, "vol_5y": round(float(v), 4),
                     "mdd_5y": round(float(m), 4),
                     "ret_5y": None if r.get("ret_5y") is None else round(float(r["ret_5y"]), 4)})
    rows.sort(key=lambda x: -x["vol_5y"])
    # 选样：保留波动前 2（"名字带稳却颠簸"的主力证据），另 2 位专取五年年化最差者
    # （优先负收益），避免四行全是正收益而削弱"名不副实"的论点；展示仍按波动降序
    if len(rows) > 2:
        lead = [rows[0]] + ([rows[3]] if len(rows) > 3 else rows[1:2])   # 保留波动第1与第4
        rest = [r for r in rows if r.get("ret_5y") is not None and r["label"] not in {x["label"] for x in lead}]
        negs = sorted([r for r in rest if r["ret_5y"] < 0], key=lambda r: r["ret_5y"])
        pool = negs if len(negs) >= 2 else sorted(rest, key=lambda r: r["ret_5y"])
        picked, seen2 = list(lead), {r["label"] for r in lead}
        for r in pool:
            if len(picked) >= n:
                break
            if r["label"] in seen2:
                continue
            picked.append(r); seen2.add(r["label"])
        rows = sorted(picked, key=lambda x: -x["vol_5y"])[:n]
    base.sort()
    # 对照用分位数而非 min-max：5年口径下池内存在 5.95% 离群值，
    # 写「0.4%~1.5%」会被理解为区间上下限而被反杀，改用中位数 + P90 表述
    med = base[len(base) // 2] if base else None
    p90 = base[len(base) * 9 // 10] if base else None
    return {"top": rows[:n], "base_vol": round(med, 4) if med else None,
            "base_p90": round(p90, 4) if p90 else None}
