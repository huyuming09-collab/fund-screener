# -*- coding: utf-8 -*-
"""市场情绪模块（v2.1，用户 2026-11-01）。

8 指标 → 综合市场温度（0-100，越高越热）+ 历史走势图 + 牛熊对照回测。
核心4（进综合）：股债利差ERP / 估值分位PE / 两融余额(沪深) / M1-M2剪刀差。
进阶/新增4（独立参考）：iVIX波指 / 新发基金规模 / 巴菲特指数 / 全市场破净率。

数据源（均已实测）：中证官网PE+收盘(load_cs) / 东方财富10Y国债 / 央行货币供应量 /
沪深两融 / 50ETF期权iVIX / 天天基金新发 / 统计局GDP+总市值 / 全市场破净统计。
输出 web/market.js（window.MARKET_DATA），供 market.html / home.html 渲染。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from pathlib import Path

import pandas as pd

from . import data as D
from . import eqindex as E

ROOT = Path(__file__).resolve().parent.parent
OUT_JS = ROOT / "web" / "market.js"

# 核心4指标权重（进综合温度；pe/破净/巴菲特/新发为独立参考不进综合）
# 2026-11-02 数据定权：8指标热度相关矩阵129月对齐，iVIX与两融ρ=-0.092近正交，
# 4核+iVIX平均|ρ|=0.304 优于3核0.519、含pe现状0.631（pe与erp ρ=0.825双计）。
# ERP最大=估值优先；两融+iVIX拆分情绪；M1M2最低(月更滞后+2025口径调整)。
WEIGHTS = {"erp": 0.40, "margin": 0.25, "ivix": 0.20, "m1m2": 0.15}
# 温度等级（0-100，越高越热）——保留5级（用户2026-11-02定；D2显示>80档12月-12.47%
# 与60-80档-1.93%差6倍，5级保住热端分级；文案标注冷端弱/热端强）
TEMP_LEVELS = [(20, "寒冷"), (40, "偏冷"), (60, "中性"), (80, "偏热"), (100, "过热")]
# 指标元信息（label/单位/一句话/怎么算/常见范围/数据源；供详情页帮小白理解）
IND_META = {
    "erp": {"label": "股债利差 ERP", "unit": "%", "invert": True,
            "desc": "盈利收益率 − 10年国债，衡量买股 vs 买债谁更划算",
            "how": "ERP = 1/PE(中证全指) − 10Y国债收益率。正值越大 = 股票相对债券越有吸引力。",
            "range": "经验值：ERP>6% 股票性价比高；3~6% 中性；<3% 性价比低。",
            "source": "中证官网 PE + 东方财富 10Y国债"},
    "pe": {"label": "中证全指 PE", "unit": "倍", "invert": False,
           "desc": "市盈率 TTM 的历史分位，最通用的「市场贵不贵」标尺",
           "how": "当前 PE 在近5年历史中的百分位（越低越便宜）。",
           "range": "分位 <30% 偏便宜；30~70% 适中；>70% 偏贵。",
           "source": "中证官网",
           "caveat": "仅PE单维度——PB历史数据不可得（雪球未覆盖中证全指）；且PE与ERP高度同源（ρ=0.83），故降为独立参考、不进综合。"},
    "margin": {"label": "全市场两融余额", "unit": "万亿", "invert": False,
               "desc": "沪深融资融券余额，杠杆/风险偏好温度计",
               "how": "沪市 + 深市融资融券余额合计，历史分位越高 = 场内杠杆情绪越热。",
               "range": "分位 >80% 杠杆过热；<20% 情绪冰点。",
               "source": "上海证券交易所 + 深圳证券交易所",
               "caveat": "用绝对余额分位、未剔除市场扩容影响；近5年窗口下与「两融/总市值」口径分位差约3分（实测3.28，不显著）。"},
    "m1m2": {"label": "M1-M2 剪刀差", "unit": "%", "invert": False,
             "desc": "M1同比 − M2同比，中期信用周期风向标（月更）",
             "how": "剪刀差为正且持续上行 = 信用扩张（常对应大行情）；持续为负 = 信用收缩。",
             "range": ">0 扩张；<0 收缩。看方向比看绝对值更重要。",
             "source": "中国人民银行（月更）",
             "caveat": "M1统计口径2025年1月有调整（纳入个人活期存款等），历史序列存在新旧口径拼接；且月更滞后约2周。"},
    "ivix": {"label": "iVIX 波指", "unit": "点", "invert": True,
             "desc": "50ETF期权隐含波动率，A股版恐慌指数",
             "how": "iVIX 越高 = 市场越恐慌（常对应底部）；越低 = 越平静（可能过热）。与两融互补：两融看杠杆、iVIX 看恐慌。",
             "range": ">30 恐慌；20~30 正常；<20 平静。",
             "source": "上交所 50ETF 期权"},
    "newfund": {"label": "新发基金规模", "unit": "亿", "invert": False,
                "desc": "基金发行募集热度，经典反向情绪指标",
                "how": "单月新发基金募集规模。发行火爆 = 散户入场 = 情绪顶部；发行冰点 = 底部。",
                "range": "分位 >80% 发行过热（顶部信号）；<20% 冰点（底部信号）。",
                "source": "天天基金"},
    "buffett": {"label": "巴菲特指数", "unit": "%", "invert": False,
                "desc": "证券化率 = A股总市值 / GDP，超长周期牛熊锚",
                "how": "总市值 ÷ 过去4季度滚动GDP。巴菲特经典阈值。",
                "range": ">100% 高估；70~100% 适中；<70% 低估。",
                "source": "统计局 GDP + 沪深总市值",
                "caveat": "总市值仅含沪深两市、不含北交所（接口无北交所列）；GDP季度滞后1-2季，为慢变量。"},
    "breaknet": {"label": "全市场破净率", "unit": "%", "invert": True,
                 "desc": "PB<1 个股占比，底部验证强信号",
                 "how": "破净个股数 ÷ 全市场上市公司总数。越高 = 越多股票跌破净资产（常对应大底）。",
                 "range": ">10% 通常对应历史大底；<5% 偏高（顶部区域）。",
                 "source": "全市场每日"},
}
CORE_KEYS = ["erp", "margin", "ivix", "m1m2"]   # 进综合温度的4个（pe已降为参考，ivix升核）


def _temp_level(score: float | None) -> str | None:
    if score is None:
        return None
    for ub, name in TEMP_LEVELS:
        if score <= ub:
            return name
    return "过热"


def _pct_rank(s: pd.Series, window_years: int = 5) -> float | None:
    """当前值在近 N 年历史中的分位（0-100）。"""
    if s is None or s.empty or len(s) < 26:
        return None
    s = s.dropna().sort_index()
    end = s.index.max()
    w = s[s.index >= end - pd.DateOffset(years=window_years)]
    if len(w) < 26:
        return None
    cur = float(s.iloc[-1])
    return round(float((w <= cur).mean() * 100), 2)


def _sample(s: pd.Series, freq: str = "W-FRI", points: int = 520) -> list:
    """重采样到周频并限点数（供走势图，更平滑不粗）。"""
    if s is None or s.empty:
        return []
    s = s.dropna().sort_index()
    r = s.resample(freq).last().dropna()
    if len(r) > points:
        step = len(r) / points
        r = r.iloc[[min(len(r) - 1, int(i * step)) for i in range(points)]]
    return [[d.strftime("%Y-%m-%d"), round(float(v), 4)] for d, v in r.items()]


def _heat_of(pct: float | None, invert: bool = False) -> float | None:
    """分位(0-100) → 热度(0-100,越高越热)。invert=True 用于「越高越便宜/越恐慌」指标。"""
    if pct is None:
        return None
    return round(100.0 - pct, 2) if invert else round(pct, 2)


def _composite(heats: dict) -> float | None:
    """加权综合温度（缺指标自动重分配权重）。"""
    num, den = 0.0, 0.0
    for k, h in heats.items():
        if h is not None:
            w = WEIGHTS.get(k, 0.0)
            num += h * w
            den += w
    return round(num / den, 1) if den > 0 else None


def _interp_pct(s: pd.Series, when, window_years: int = 5) -> float | None:
    """≤when 时点的分位（用截至 when 的历史算，避免前视）。"""
    if s is None or s.empty:
        return None
    hist = s[s.index <= when].dropna()
    if len(hist) < 26:
        return None
    end = hist.index.max()
    w = hist[hist.index >= end - pd.DateOffset(years=window_years)]
    if len(w) < 26:
        return None
    cur = float(hist.iloc[-1])
    return round(float((w <= cur).mean() * 100), 2)


def _bullbear(close: pd.Series, thresh: float = 0.20) -> list:
    """从收盘价用 20% 标准推导牛熊区间（客观，不硬编码观点）。
    状态机：熊（自峰值回撤≥20%）/ 牛（自谷底反弹≥20%），保证交替、无相邻同类段。"""
    if close is None or close.empty or len(close) < 60:
        return []
    s = close.dropna().sort_index()
    segs = []
    peak = float(s.iloc[0]); peak_d = s.index[0]
    trough = float(s.iloc[0]); trough_d = s.index[0]
    state = None                                     # None/‘bear’/‘bull’
    for d, v in s.items():
        v = float(v)
        if v > peak:
            peak, peak_d = v, d
        if v < trough:
            trough, trough_d = v, d
        if state != "bear" and peak > 0 and (peak - v) / peak >= thresh:
            if state == "bull" and segs:
                segs[-1]["end"] = peak_d            # 收口上一段牛市
            state = "bear"
            trough, trough_d = v, d
            segs.append({"start": peak_d, "end": None, "type": "bear"})
        elif state == "bear" and trough > 0 and (v - trough) / trough >= thresh:
            segs[-1]["end"] = trough_d              # 收口熊市
            state = "bull"
            peak, peak_d = v, d
            segs.append({"start": trough_d, "end": None, "type": "bull"})
    if segs and segs[-1]["end"] is None:
        segs[-1]["end"] = s.index[-1]
    # 过滤过短段（<60天）避免噪音
    out = []
    for x in segs:
        if x["end"] is None:
            continue
        if (x["end"] - x["start"]).days >= 60:
            out.append({"start": str(x["start"])[:10], "end": str(x["end"])[:10], "type": x["type"]})
    return out


# ---------------------------------------------------------------- 数据获取（带缓存）
def _pm(v) -> pd.Timestamp:
    """中文月份「2026年09月」→ Timestamp。"""
    m = re.match(r"(\d{4})\D+(\d{1,2})", str(v))
    return pd.Timestamp(f"{m.group(1)}-{int(m.group(2)):02d}-01") if m else pd.NaT


def _fetch_pe_close() -> tuple:
    """中证全指 PE + 收盘点位（一次 load_cs，省一次请求）。"""
    raw = E.load_cs("000985", refresh=True) or {}
    pe = E._pairs_series(raw.get("pe") or [])
    close = E._pairs_series(raw.get("close") or [])
    return pe, close


def _fetch_bond10() -> pd.Series:
    key = "bond10_yield"
    cached = D._load_cache(key, 24)
    if cached is not None:
        return cached
    import akshare as ak
    df = ak.bond_zh_us_rate(start_date="20150101")
    col = [c for c in df.columns if "中国国债收益率10年" in str(c)][0]
    s = pd.Series(df[col].astype(float).values,
                  index=pd.to_datetime(df["日期"])).dropna().sort_index()
    D._save_cache(key, s)
    return s


def _fetch_m1m2() -> pd.DataFrame:
    key = "m1m2"
    cached = D._load_cache(key, 24)
    if cached is not None:
        return cached
    import akshare as ak
    df = ak.macro_china_money_supply()
    m1c = [c for c in df.columns if "货币(M1)-同比增长" in str(c)][0]
    m2c = [c for c in df.columns if "货币和准货币(M2)-同比增长" in str(c)][0]
    out = pd.DataFrame({
        "m1": pd.to_numeric(df[m1c], errors="coerce").values,
        "m2": pd.to_numeric(df[m2c], errors="coerce").values,
    }, index=[_pm(v) for v in df["月份"]]).dropna()
    out = out[~out.index.isna()]
    out["scissor"] = out["m1"] - out["m2"]
    out = out.sort_index()
    D._save_cache(key, out)
    return out


def _fetch_margin() -> pd.Series:
    """全市场两融余额 = 沪市 + 深市（Phase3 2026-11-02：改 inner join，
    仅两市均有观测日求和，消除 ffill 人工填充误差；审计C4实测分位差3.94不显著）。"""
    key = "margin_all"
    cached = D._load_cache(key, 24)
    if cached is not None:
        return cached
    import akshare as ak
    parts = []
    for fn in ("macro_china_market_margin_sh", "macro_china_market_margin_sz"):
        df = getattr(ak, fn)()
        col = "融资融券余额" if "融资融券余额" in df.columns else "融资余额"
        parts.append(pd.Series(pd.to_numeric(df[col], errors="coerce").values,
                               index=pd.to_datetime(df["日期"])))
    # inner join：仅两市均有观测的日期求和（Phase3，替代原 outer+ffill）
    m = pd.concat(parts, axis=1, keys=["sh", "sz"], sort=True)
    s = (m["sh"] + m["sz"]).dropna().sort_index()
    D._save_cache(key, s)
    return s


def _fetch_ivix() -> pd.Series:
    """iVIX 波指（50ETF期权隐含波动率，日频）。"""
    key = "ivix"
    cached = D._load_cache(key, 24)
    if cached is not None:
        return cached
    import akshare as ak
    df = ak.index_option_50etf_qvix()
    s = pd.Series(pd.to_numeric(df["close"], errors="coerce").values,
                  index=pd.to_datetime(df["date"])).dropna().sort_index()
    D._save_cache(key, s)
    return s


def _fetch_newfund() -> pd.Series:
    """新发基金募集规模（月度，亿份）。

    Phase3 2026-11-02：仅保留股票型+混合型（偏股）——债基发行大年会污染
    「发行热度」信号（审计C1：当前分位差0.00不显著，但逻辑上权益发行才反映
    权益情绪，避免未来失真）。
    """
    key = "newfund"
    cached = D._load_cache(key, 24)
    if cached is not None:
        return cached
    import akshare as ak
    df = ak.fund_new_found_em().copy()
    df["d"] = pd.to_datetime(df["成立日期"], errors="coerce")
    df["amt"] = pd.to_numeric(df["募集份额"], errors="coerce")
    df = df.dropna(subset=["d", "amt"])
    # 类型过滤：仅股票/混合（含指数型-股票、QDII-股票等权益类）
    if "基金类型" in df.columns:
        df = df[df["基金类型"].astype(str).str.contains("股票|混合", na=False)]
    s = df.groupby(df["d"].dt.to_period("M").dt.to_timestamp())["amt"].sum().sort_index()
    D._save_cache(key, s)
    return s


def _fetch_buffett() -> pd.Series:
    """巴菲特指数 = A股总市值 / 过去4季度滚动GDP（%）。"""
    key = "buffett"
    cached = D._load_cache(key, 24)
    if cached is not None:
        return cached
    import akshare as ak
    cap = ak.macro_china_stock_market_cap()
    sh = [c for c in cap.columns if "市价总值" in str(c) and "上海" in str(c)][0]
    sz = [c for c in cap.columns if "市价总值" in str(c) and "深圳" in str(c)][0]
    cap_s = pd.Series((pd.to_numeric(cap[sh], errors="coerce")
                       + pd.to_numeric(cap[sz], errors="coerce")).values,
                      index=[_pm(v) for v in cap["数据日期"]]).dropna().sort_index()
    # GDP 为累计值（如「2026年第1-3季度」= Q1~Q3 累计）→ 差分得单季 → 滚动4季
    gdp = ak.macro_china_gdp()
    # 健壮取列：第一个「国内生产总值」且非同比/累计的列（如「国内生产总值-当季值」）
    gc = None
    for c in gdp.columns:
        cs = str(c)
        if "国内生产总值" in cs and "同比" not in cs and "累计" not in cs:
            gc = c
            if "当季" in cs or "值" in cs:
                break
    if gc is None:
        raise ValueError(f"GDP 当季值列未找到: {list(gdp.columns)}")
    rows = []
    for _, r in gdp.iterrows():
        s = str(r["季度"])
        m = re.match(r"(\d{4})年第1-(\d)季度", s) or re.match(r"(\d{4})年第(\d)季度", s)
        if not m:
            continue
        y, q = int(m.group(1)), int(m.group(2))
        rows.append({"y": y, "q": q, "cum": pd.to_numeric(r[gc], errors="coerce")})
    gdf = pd.DataFrame(rows).dropna().sort_values(["y", "q"])
    # 单季 = 本年累计 − 上一累计（Q1 直接取累计）；季末日期直接算
    qend = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}
    prev = None
    singles = []
    for _, r in gdf.iterrows():
        v = r["cum"] if r["q"] == 1 else (r["cum"] - prev if prev is not None else None)
        singles.append({"y": r["y"], "q": r["q"], "v": v,
                        "end": pd.Timestamp(f"{int(r['y'])}-{qend[int(r['q'])]}")})
        prev = r["cum"]
    sdf = pd.DataFrame(singles).dropna(subset=["v"])
    # 滚动4季 TTM，映射到季末日期
    sdf["ttm"] = sdf["v"].rolling(4).sum()
    gdp_ttm = sdf.dropna(subset=["ttm"]).set_index("end")["ttm"].sort_index()
    # 对齐到总市值月份（ffill）
    gdp_m = gdp_ttm.reindex(cap_s.index.union(gdp_ttm.index), method="ffill").reindex(cap_s.index)
    ratio = (cap_s / gdp_m * 100).dropna()
    D._save_cache(key, ratio)
    return ratio


def _fetch_breaknet() -> pd.Series:
    """全市场破净率（%，日频）。"""
    key = "breaknet"
    cached = D._load_cache(key, 24)
    if cached is not None:
        return cached
    import akshare as ak
    df = ak.stock_a_below_net_asset_statistics()
    s = pd.Series(pd.to_numeric(df["below_net_asset_ratio"], errors="coerce").values * 100,
                  index=pd.to_datetime(df["date"])).dropna().sort_index()
    D._save_cache(key, s)
    return s


# ---------------------------------------------------------------- 组装
def _ind(key: str, series: pd.Series, unit_scale: float = 1.0, window: int = 5) -> dict:
    """单指标：当前值 + 分位 + 热度 + 历史序列（走势图）。"""
    meta = IND_META[key]
    s = series.dropna().sort_index() if series is not None else pd.Series(dtype=float)
    if s.empty:
        return {"label": meta["label"], "unit": meta["unit"], "value": None,
                "pct": None, "heat": None, "series": [], **{
                    k: meta.get(k) for k in ("desc", "how", "range", "source", "caveat")}}
    pct = _pct_rank(s, window)
    val = float(s.iloc[-1]) * unit_scale
    return {"label": meta["label"], "unit": meta["unit"],
            "value": round(val, 2), "pct": pct,
            "heat": _heat_of(pct, invert=meta["invert"]),
            "series": _sample(s),
            "desc": meta.get("desc"), "how": meta.get("how"),
            "range": meta.get("range"), "source": meta.get("source"),
            "caveat": meta.get("caveat")}


def build_market(log=print) -> dict | None:
    """组装 market.js：8 指标（含序列）+ 综合温度 + 牛熊对照回测。任何异常降级。"""
    t0 = time.time()
    try:
        pe, close = _fetch_pe_close()
        bond = _fetch_bond10()
        mm = _fetch_m1m2()
        margin = _fetch_margin()
    except Exception as e:                          # noqa: BLE001
        log(f"[市场情绪] 核心数据获取失败：{type(e).__name__}: {e}")
        return None
    if pe is None or pe.empty:
        log("[市场情绪] PE 数据缺失，跳过")
        return None

    # ---- 核心4（进综合）----
    erp = (1.0 / pe.replace(0, pd.NA) - bond.reindex(pe.index, method="ffill") / 100.0).dropna() \
        if not pe.empty else pd.Series(dtype=float)
    core_series = {
        "erp": erp, "margin": margin,
        "m1m2": mm["scissor"] if (mm is not None and "scissor" in mm) else pd.Series(dtype=float),
    }
    # ivix（核心第4项，2026-11-02 升核；抓取失败降级为None→综合权重自动重分配）
    try:
        core_series["ivix"] = _fetch_ivix()
    except Exception as e:                          # noqa: BLE001
        log(f"[市场情绪] ivix 获取失败：{type(e).__name__}（核心降级）")
        core_series["ivix"] = pd.Series(dtype=float)
    # 单位换算：ERP 小数→%（×100）、两融原始值→万亿（×1e-12）
    ind = {
        "erp": _ind("erp", core_series["erp"], unit_scale=100),
        "margin": _ind("margin", core_series["margin"], unit_scale=1e-12),
        "m1m2": _ind("m1m2", core_series["m1m2"]),
        "ivix": _ind("ivix", core_series["ivix"]),
    }
    # ---- 进阶/参考4（pe降为参考 + 3个独立指标，失败不影响核心）----
    for k, fn in (("pe", lambda: pe), ("newfund", _fetch_newfund),
                  ("buffett", _fetch_buffett), ("breaknet", _fetch_breaknet)):
        try:
            ind[k] = _ind(k, fn())
        except Exception as e:                      # noqa: BLE001
            log(f"[市场情绪] {k} 获取失败：{type(e).__name__}: {str(e)[:40]}（跳过该指标）")
            ind[k] = {"label": IND_META[k]["label"], "unit": IND_META[k]["unit"],
                      "value": None, "pct": None, "heat": None, "series": [],
                      **{x: IND_META[k].get(x) for x in ("desc", "how", "range", "source", "caveat")}}

    # ---- 综合温度（仅核心4）----
    heats = {k: ind[k]["heat"] for k in CORE_KEYS}
    score = _composite(heats)
    level = _temp_level(score)
    # 透明度字段（2026-11-02 Phase2）：有效指标数/权重覆盖率/热度离散度
    valid = {k: h for k, h in heats.items() if h is not None}
    n_valid_core = len(valid)
    w_cov = round(sum(WEIGHTS.get(k, 0) for k in valid) / sum(WEIGHTS.values()), 3) if WEIGHTS else 0.0
    heat_std = round(pd.Series(list(valid.values())).std(ddof=0), 2) if len(valid) > 1 else None
    # 多窗口温度（Phase4 2026-11-02：3y/5y/10y，避免单一窗口偏差）
    multiwin = {}
    for wy in (3, 5, 10):
        h = {k: _heat_of(_pct_rank(core_series[k], wy), invert=IND_META[k]["invert"])
             for k in CORE_KEYS}
        s = _composite(h)
        multiwin[f"{wy}y"] = {"score": round(s, 1) if s is not None else None,
                              "level": _temp_level(s)}

    # ---- 对照回测（月度；综合温度 vs 中证全指点位；牛熊区间自动推导）----
    idx = pe.resample("ME").last().dropna().index
    bt = []
    for when in idx:
        h = {k: _heat_of(_interp_pct(s, when, 5), invert=IND_META[k]["invert"])
             for k, s in core_series.items()}
        sc = _composite(h)
        pe_v = round(float(pe[pe.index <= when].iloc[-1]), 2) if not pe[pe.index <= when].empty else None
        bt.append({"d": when.strftime("%Y-%m"), "score": sc, "pe": pe_v})
    # 牛熊阈值敏感性（2026-11-02 Phase2：默认30%，标注20/25/30边界态）
    bb30 = _bullbear(close, thresh=0.30)
    bb20 = _bullbear(close, thresh=0.20)
    bb25 = _bullbear(close, thresh=0.25)
    _st = lambda segs: segs[-1]["type"] if segs else None
    st20, st25, st30 = _st(bb20), _st(bb25), _st(bb30)
    consistent = (st20 == st25 == st30)
    if consistent:
        bb_note = f"三阈值一致：{'牛市' if st30=='bull' else '熊市'}"
    else:
        bb_note = (f"阈值敏感：30%判{'牛市' if st30=='bull' else '熊市'}、"
                   f"20%判{'牛市' if st20=='bull' else '熊市'}，边界区间")
    bullbear = bb30

    # ---- D2 回测统计（2026-11-02 Phase2）：各温度档未来1/3/6/12月中证全指收益 ----
    close_m = close.resample("ME").last().dropna()
    def _fwd(ts, nm):
        c = close_m[close_m.index >= ts + pd.DateOffset(months=nm)]
        return (float(c.iloc[0]) / float(close_m[close_m.index >= ts].iloc[0]) - 1) * 100 if len(c) else None
    band_defs = [("≤20", 0, 20), ("20-40", 20, 40), ("40-60", 40, 60),
                 ("60-80", 60, 80), (">80", 80, 101)]
    band_stats = []
    for name, lo, hi in band_defs:
        pts = {"n": 0, 3: [], 6: [], 12: []}
        for b in bt:
            if b.get("score") is None or not (lo <= b["score"] < hi):
                continue
            ts = pd.Timestamp(b["d"] + "-01")
            r3, r6, r12 = _fwd(ts, 3), _fwd(ts, 6), _fwd(ts, 12)
            if r3 is None and r6 is None and r12 is None:
                continue
            pts["n"] += 1
            for nm, r in ((3, r3), (6, r6), (12, r12)):
                if r is not None:
                    pts[nm].append(r)
        import statistics as _st
        def _med(v): return round(_st.median(v), 2) if v else None
        def _wr(v): return round(sum(1 for x in v if x > 0) / len(v) * 100, 0) if v else None
        band_stats.append({"band": name, "n": pts["n"],
                           "r3_med": _med(pts[3]), "r3_wr": _wr(pts[3]),
                           "r6_med": _med(pts[6]), "r6_wr": _wr(pts[6]),
                           "r12_med": _med(pts[12]), "r12_wr": _wr(pts[12])})

    # 分级解读（动态生成，2026-11-02：基于当期 band_stats，避免与展示数字矛盾）
    _w12 = [(b["band"], b["r12_med"], b["n"]) for b in band_stats if b["r12_med"] is not None]
    if _w12:
        worst = min(_w12, key=lambda x: x[1])
        best = max(_w12, key=lambda x: x[1])
        mono = all(_w12[i][1] >= _w12[i+1][1] for i in range(len(_w12)-1))
        level_note = (f"回测（各档未来12月中证全指收益中位数）：最差档 {worst[0]} = {worst[1]:+.1f}%（n={worst[2]}），"
                      f"最好档 {best[0]} = {best[1]:+.1f}%。"
                      + ("各档单调递减，分级具区分度。" if mono else
                         "各档非单调，冷/中间档区分度弱，仅最高档警示性更强；样本小的档位仅作参考。"))
    else:
        level_note = "历史样本不足，暂无分档回测统计。"

    out = {"asof": str(pe.index.max())[:10], "weights": WEIGHTS,
           "core_keys": CORE_KEYS, "levels": TEMP_LEVELS,
           "indicators": ind, "score": score, "level": level,
           # Phase2 透明度字段：有效核心指标数/权重覆盖率/热度离散度/牛熊阈值敏感性
           "n_valid_core": n_valid_core, "weight_coverage": w_cov,
           "heat_std": heat_std, "bb_note": bb_note,
           "band_stats": band_stats,
           "level_note": level_note,
           "multiwindow": multiwin,
           # Phase4 已知缺口与决策（诚实标注，2026-11-02）
           "gaps_note": ("成交额源现可用（中证全指日频amount，3702行）但与两融热度ρ=0.673"
                         "（活动度维度已重复），加核反增冗余故不加——两融已覆盖；"
                         "PB仍仅PE单维度（中证全指PB接口不可得）；"
                         "权重为判断值未用PCA定权（核心仅4项，PCA不稳定故不采用）。"),
           "backtest": bt, "bullbear": bullbear,
           "close": _sample(close),
           "generated_at": str(dt.datetime.now())}
    log(f"[市场情绪] 综合温度 {score}（{level}）· 核心有效{n_valid_core}/4 覆盖{w_cov:.0%}"
        f" · 牛熊{len(bullbear)}段 · {bb_note} · 耗时 {(time.time()-t0):.1f}s")
    return out


def write_market(payload: dict | None) -> bool:
    """写 web/market.js（window.MARKET_DATA）。"""
    if not payload:
        return False
    js = "window.MARKET_DATA = " + json.dumps(payload, ensure_ascii=False) + ";\n"
    OUT_JS.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(OUT_JS) + ".tmp")
    tmp.write_text(js, encoding="utf-8")
    tmp.replace(OUT_JS)
    return True


def main(argv=None) -> int:
    payload = build_market(log=print)
    ok = write_market(payload)
    print(json.dumps({"ok": ok, "score": payload.get("score") if payload else None,
                      "level": payload.get("level") if payload else None,
                      "n": len(payload.get("indicators") or {}) if payload else 0},
                     ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
