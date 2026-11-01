# -*- coding: utf-8 -*-
"""市场情绪模块（v2.0，用户 2026-11-01）。

四指标 → 综合市场温度（0-100，越高越热）+ 历史回测可视化。
  1. 股债利差 ERP = 1/PE(中证全指) − 10Y国债收益率（大类资产性价比）
  2. 估值分位 PE  = 中证全指 PE-TTM 历史分位（绝对估值标尺）
  3. M1-M2 剪刀差 = M1同比 − M2同比（中期信用周期风向标，月更）
  4. 两融余额分位 = 沪市融资融券余额历史分位（杠杆/情绪温度计；替代成交额，
     因东财成交额接口限流不可用，两融更深历史且更直接反映风险偏好）

数据源：中证官网 PE(load_cs) / 东方财富 10Y国债 / 央行货币供应量 / 沪市两融。
输出 web/market.js（window.MARKET_DATA），供 home.html 渲染。
"""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

import pandas as pd

from . import data as D
from . import eqindex as E

ROOT = Path(__file__).resolve().parent.parent
OUT_JS = ROOT / "web" / "market.js"

# 综合温度权重（用户参考豆包方案：ERP/估值/情绪/流动性）
WEIGHTS = {"erp": 0.30, "pe": 0.30, "m1m2": 0.20, "margin": 0.20}
# 温度等级（0-100，越高越热）
TEMP_LEVELS = [(20, "寒冷"), (40, "偏冷"), (60, "中性"), (80, "偏热"), (100, "过热")]


def _temp_level(score: float | None) -> str | None:
    if score is None:
        return None
    for ub, name in TEMP_LEVELS:
        if score <= ub:
            return name
    return "过热"


def _pct_rank(s: pd.Series, window_years: int = 5) -> float | None:
    """当前值在近 N 年历史中的分位（0-100，越低越便宜）。"""
    if s is None or s.empty or len(s) < 26:
        return None
    s = s.dropna().sort_index()
    end = s.index.max()
    w = s[s.index >= end - pd.DateOffset(years=window_years)]
    if len(w) < 26:
        return None
    cur = float(s.iloc[-1])
    return round(float((w <= cur).mean() * 100), 2)


# ---------------------------------------------------------------- 数据获取（带缓存）
def _fetch_pe() -> pd.Series:
    """中证全指 PE-TTM 日/周频序列。"""
    raw = E.load_cs("000985", refresh=True) or {}
    pairs = raw.get("pe") or []
    return E._pairs_series(pairs)


def _fetch_bond10() -> pd.Series:
    """10 年期国债到期收益率日频序列。"""
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
    """M1/M2 同比增速（月度）。返回 DataFrame[M1同比, M2同比, 剪刀差]。"""
    key = "m1m2"
    cached = D._load_cache(key, 24)
    if cached is not None:
        return cached
    import akshare as ak
    df = ak.macro_china_money_supply()
    m1c = [c for c in df.columns if "货币(M1)-同比增长" in str(c)][0]
    m2c = [c for c in df.columns if "货币和准货币(M2)-同比增长" in str(c)][0]
    # 月份格式为中文「2026年09月」→ 解析为日期
    def _parse_month(v):
        s = str(v)
        try:
            import re
            m = re.match(r"(\d{4})\D+(\d{1,2})", s)
            if m:
                return pd.Timestamp(f"{m.group(1)}-{int(m.group(2)):02d}-01")
        except Exception:                          # noqa: BLE001
            pass
        return pd.NaT
    out = pd.DataFrame({
        "m1": pd.to_numeric(df[m1c], errors="coerce").values,
        "m2": pd.to_numeric(df[m2c], errors="coerce").values,
    }, index=[_parse_month(v) for v in df["月份"]]).dropna()
    out = out[~out.index.isna()]
    out["scissor"] = out["m1"] - out["m2"]
    out = out.sort_index()
    D._save_cache(key, out)
    return out


def _fetch_margin() -> pd.Series:
    """沪市融资融券余额日频序列（两融=情绪/杠杆温度计）。"""
    key = "margin_sh"
    cached = D._load_cache(key, 24)
    if cached is not None:
        return cached
    import akshare as ak
    df = ak.macro_china_market_margin_sh()
    col = "融资融券余额" if "融资融券余额" in df.columns else "融资余额"
    s = pd.Series(pd.to_numeric(df[col], errors="coerce").values,
                  index=pd.to_datetime(df["日期"])).dropna().sort_index()
    D._save_cache(key, s)
    return s


# ---------------------------------------------------------------- 指标计算
def _erp_series(pe: pd.Series, bond: pd.Series) -> pd.Series:
    """股债利差 ERP = 1/PE − 10Y国债收益率（对齐到 PE 的日期；bond 取当日最近值）。"""
    if pe is None or pe.empty:
        return pd.Series(dtype=float)
    ey = 1.0 / pe.replace(0, pd.NA)          # 盈利收益率
    b = bond.reindex(pe.index, method="ffill")
    return (ey - b / 100.0).dropna()          # bond 是百分数（如 2.17 = 2.17%）


def _m1m2_at(mm: pd.DataFrame, when) -> float | None:
    """取 ≤ when 的最新 M1-M2 剪刀差。"""
    if mm is None or mm.empty:
        return None
    sub = mm[mm.index <= when]
    if sub.empty:
        return None
    return float(sub["scissor"].iloc[-1])


def _heat_of(pct: float | None, invert: bool = False) -> float | None:
    """把分位(0-100,越低越便宜)映射为热度(0-100,越高越热)。invert=True 用于「越低越热」指标。"""
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


def build_market(log=print) -> dict | None:
    """组装 market.js payload：当前四指标 + 综合温度 + 回测序列。任何异常降级。"""
    t0 = time.time()
    try:
        pe = _fetch_pe()
        bond = _fetch_bond10()
        mm = _fetch_m1m2()
        margin = _fetch_margin()
    except Exception as e:                          # noqa: BLE001
        log(f"[市场情绪] 数据获取失败：{type(e).__name__}: {e}")
        return None
    if pe is None or pe.empty:
        log("[市场情绪] PE 数据缺失，跳过")
        return None

    # ---- 当前指标 ----
    erp = _erp_series(pe, bond)
    cur = {}
    # ERP：越高越便宜 → 热度取反（分位低=股票贵=热）
    erp_pct = _pct_rank(erp, 5) if not erp.empty else None
    cur["erp"] = {"value": round(float(erp.iloc[-1]) * 100, 2) if not erp.empty else None,
                  "pct": erp_pct, "heat": _heat_of(erp_pct, invert=True),
                  "unit": "%", "label": "股债利差 ERP"}
    # PE 估值分位：越高越贵 → 热度=分位
    pe_pct = _pct_rank(pe, 5)
    cur["pe"] = {"value": round(float(pe.iloc[-1]), 2),
                 "pct": pe_pct, "heat": _heat_of(pe_pct),
                 "unit": "倍", "label": "中证全指 PE"}
    # M1-M2：剪刀差（同比%差）；热度用其历史分位（越高越热=信用扩张）
    cur_m1m2 = _m1m2_at(mm, pe.index.max())
    m1m2_pct = _pct_rank(mm["scissor"], 5) if (mm is not None and "scissor" in mm) else None
    cur["m1m2"] = {"value": round(cur_m1m2, 2) if cur_m1m2 is not None else None,
                   "pct": m1m2_pct, "heat": _heat_of(m1m2_pct),
                   "unit": "%", "label": "M1-M2 剪刀差"}
    # 两融：越高越热 → 热度=分位
    margin_pct = _pct_rank(margin, 5) if margin is not None and not margin.empty else None
    cur["margin"] = {"value": round(float(margin.iloc[-1]) / 1e12, 2) if (margin is not None and not margin.empty) else None,
                     "pct": margin_pct, "heat": _heat_of(margin_pct),
                     "unit": "万亿", "label": "沪市两融余额"}

    heats = {k: cur[k]["heat"] for k in ("erp", "pe", "m1m2", "margin")}
    score = _composite(heats)
    level = _temp_level(score)

    # ---- 回测序列（月度；用截至各时点的历史分位，避免前视）----
    idx = pe.resample("ME").last().dropna().index
    bt = []
    for when in idx:
        h_erp = _heat_of(_interp_pct(erp, when, 5), invert=True) if not erp.empty else None
        h_pe = _heat_of(_interp_pct(pe, when, 5))
        h_mm = _heat_of(_interp_pct(mm["scissor"], when, 5)) if (mm is not None and "scissor" in mm) else None
        h_mg = _heat_of(_interp_pct(margin, when, 5)) if (margin is not None and not margin.empty) else None
        sc = _composite({"erp": h_erp, "pe": h_pe, "m1m2": h_mm, "margin": h_mg})
        # 同步取该月 PE 点位做对照（回测叠加图）
        pe_v = round(float(pe[pe.index <= when].iloc[-1]), 2) if not pe[pe.index <= when].empty else None
        bt.append({"d": when.strftime("%Y-%m"), "score": sc, "pe": pe_v})

    out = {"asof": str(pe.index.max())[:10], "weights": WEIGHTS,
           "indicators": cur, "score": score, "level": level,
           "levels": TEMP_LEVELS, "backtest": bt,
           "generated_at": str(dt.datetime.now())}
    log(f"[市场情绪] 综合温度 {score}（{level}）· 回测 {len(bt)} 个月 · 耗时 {(time.time()-t0):.1f}s")
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
                      "level": payload.get("level") if payload else None},
                     ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
