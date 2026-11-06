# -*- coding: utf-8 -*-
"""红利天气打分回测（方案B：股息率水平0.50 + 点位位置0.25 + 股息率分位0.25）

用途：打分体系的科学性校准与复现（docs/19「红利天气」章的回测记录即本脚本产出）。
样本：payload 中 module=weather 的红利指数 × 近10年周频（W-FRI 对齐）；
指标：前瞻12个月收益按天气带分组（均值/中位/正收益占比）+ 各因子 Spearman IC。
依赖：data/cache/lxdyr_v1_*.pkl（理杏仁股息率）与 data/cache/eqcs_v2_*.pkl（中证点位）。

用法：.venv/bin/python scripts/eq_weather_backtest.py
"""
import json
import os
import pickle
import re
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "data", "cache")
sys.path.insert(0, ROOT)

W = {"F1_yield": 0.50, "F2_pos": 0.25, "F3_ypct": 0.25}


def pairs_to_series(pairs):
    if not pairs:
        return pd.Series(dtype=float)
    data = {}
    for d, v in pairs:
        try:
            data[pd.Timestamp(str(d))] = float(v)
        except Exception:
            continue
    s = pd.Series(data).sort_index()
    return s[~s.index.duplicated(keep="last")]


def load_dyr(code):
    p = os.path.join(CACHE, f"lxdyr_v1_{code}.pkl")
    if not os.path.exists(p):
        return None
    try:
        return pairs_to_series(pickle.load(open(p, "rb"))) * 100.0
    except Exception:
        return None


def load_close(cs):
    for k in (f"eqcs_v2_{cs}.pkl", f"eqcs_v1_{cs}.pkl"):
        p = os.path.join(CACHE, k)
        if not os.path.exists(p):
            continue
        try:
            raw = pickle.load(open(p, "rb"))
            return pairs_to_series(raw.get("close") or [])
        except Exception:
            continue
    return None


def yield_score(v):
    pts = [(3.0, 0), (3.5, 1.5), (4.0, 3), (4.5, 4.5), (5.0, 7), (6.0, 8.5), (7.0, 10)]
    if v <= pts[0][0]:
        return 0.0
    for i in range(len(pts) - 1):
        (x0, y0), (x1, y1) = pts[i], pts[i + 1]
        if v <= x1:
            return y0 + (v - x0) / (x1 - x0) * (y1 - y0)
    return 10.0


def trailing_pct(s, t, win_days=1825):
    w = s[(s.index <= t) & (s.index >= t - pd.Timedelta(days=win_days))]
    if len(w) < 100 or t not in s.index:
        return None
    return float((w <= s[t]).mean() * 100)


def factors_at(dyr, close, t):
    if t not in dyr.index or t not in close.index:
        return None
    # 三因子直接百分制（与 eqindex 打分同口径，2026-11-06 去掉 ×10 绕法）
    f1 = yield_score(float(dyr[t])) * 10
    pos = trailing_pct(close, t)
    f2 = None if pos is None else 100 * (1 - pos / 100)
    yp = trailing_pct(dyr, t)
    f3 = yp
    if f2 is None or f3 is None:
        return None
    return {"F1_yield": f1, "F2_pos": f2, "F3_ypct": f3}


def main():
    # weather 指数清单（与 build 同规则：目录中名字含 红利/股息/高股息）
    from fundscorer.eqindex import CATALOG, _cs_code, _is_weather_index
    items = [{"code": c["code"], "cs": c.get("cs") or _cs_code(c["code"]), "name": c["name"]}
             for c in CATALOG if _is_weather_index(c.get("name", ""))]
    print("weather 指数:", len(items))

    rows = []
    for it in items:
        dyr, close = load_dyr(it["code"]), load_close(it["cs"])
        if dyr is None or close is None or len(dyr) < 800 or len(close) < 800:
            print(" 跳过(数据不足):", it["name"])
            continue
        d, c = dyr.resample("W-FRI").last().dropna(), close.resample("W-FRI").last().dropna()
        for t in d.index.intersection(c.index):
            f = factors_at(d, c, t)
            if not f:
                continue
            fut = c[c.index > t + pd.Timedelta(days=365)]
            if not len(fut):
                continue
            fwd = float(fut.iloc[0] / c[t] - 1)
            total = sum(W[k] * f[k] for k in W)          # 直接百分制加权求和
            rows.append({**{"name": it["name"], "fwd12": fwd}, **f,
                         "score": total, "yield_only": yield_score(float(d[t])) * 10})

    df = pd.DataFrame(rows).dropna(subset=["score"])
    print("有效样本:", len(df), "条·周 | 指数:", df["name"].nunique())
    bands = [(0, 15, "暴雨"), (15, 30, "小雨"), (30, 45, "阴天"),
             (45, 70, "多云"), (70, 85, "晴天"), (85, 101, "酷暑")]
    for col, title in (("score", "综合分（方案B）"), ("yield_only", "纯股息率分（对照）")):
        print(f"\n== {title} → 天气带 前瞻12月 ==")
        for lo, hi, lab in bands:
            s = df[(df[col] >= lo) & (df[col] < hi)]["fwd12"]
            if len(s):
                print(f"  {lab:3} [{lo:>2}-{hi:>2}]: n={len(s):5d} "
                      f"均值={s.mean()*100:6.2f}% 中位={s.median()*100:6.2f}% "
                      f"正收益={(s>0).mean()*100:5.1f}%")
    print("\n== 因子 Spearman IC（与前瞻12月）==")
    for k in list(W) + ["yield_only"]:
        print(f"  {k}: {df[[k, 'fwd12']].corr(method='spearman').iloc[0,1]:+.3f}  权重={W.get(k, '-')}")


if __name__ == "__main__":
    main()
