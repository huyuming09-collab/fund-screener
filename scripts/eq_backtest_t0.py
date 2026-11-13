# -*- coding: utf-8 -*-
"""T0 伪前瞻回测（2026-11-13 七问自查；回答"排除规则是否排对了人、评分变体是否更准"）。

双起点：T0=2025-11-12（1年伪前瞻）、Tm=2024-11-12（2年伪前瞻）。
每起点在**当时可见数据**上重建（无前视）：
  · 风格 = 该起点 [T-5y, T] 周频超额 vs 6代理 argmax≥0.35（eqgate 同法、窗口锚 T）
  · 同格逐年排位 ps（类型×风格→桶→类型回退，n≥10）
  · 因子两变体：F3/F5 用【单代理β=1】（现产 F3）vs【6代理回归β尺度化】（待验改进）
    F1/F2/F6 同产（F6=任期/从业分位；任期用 f10 现值——起点近似，已注明）
  · 结局 = 起点→今 的同格排位（区间收益在起点同格内的分位）/ 区间收益 / 区间回撤
输出：
  ① IC 表：各因子/两版 W3 对未来同格排位的 spearman（分起点）
  ② 排除规则验证表：cut vs kept 的未来结局（E1 年化<0 / E2 回撤>60% / E3 回撤>75% /
     E4 规模<2亿(现值近似) / E5 F3虚高>10pp）
  ③ 当期在榜者在起点的处境（在榜子集上的同表）
限制（如实）：单起点族（仅2点）、风格用窗口锚T重建但不含持仓降级（11-13才上线）、
规模为现值近似、F6任期为现值近似；权重沿用生产 W3（其三起点验证见检验18）。
运行（仓库根）：python scripts/eq_backtest_t0.py
"""
from __future__ import annotations

import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import numpy as np                                     # noqa: E402
import pandas as pd                                    # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from fundscorer import eqgate as G                    # noqa: E402
from fundscorer import eqscore as ES                  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
NAV_DIR = ROOT / "data" / "cache"
OUT = ROOT / "data" / "eq_backtest_t0.csv"
ORIGINS = [("T0_1y", pd.Timestamp("2025-11-12")),
           ("Tm_2y", pd.Timestamp("2024-11-12"))]
W3 = ES.W3                                            # 生产权重（检验18 已验）


def spearman(a, b) -> float:
    s = pd.concat([pd.Series(a, dtype="float64"),
                   pd.Series(b, dtype="float64")], axis=1).dropna()
    if len(s) < 30:
        return float("nan")
    return float(s.iloc[:, 0].rank().corr(s.iloc[:, 1].rank()))


def windowed_mdd(nav: pd.DataFrame, T, years=5) -> float | None:
    """[T-years, T] 窗口内相对区间高点的最大回撤（正数）。"""
    sub = nav[nav["date"] <= T]
    if sub.empty:
        return None
    end = sub["date"].max()
    w = sub[sub["date"] > end - pd.DateOffset(years=years)]
    if len(w) < 100:
        return None
    navs = w["nav"].values
    peak = np.maximum.accumulate(navs)
    dd = (peak - navs) / peak
    return float(dd.max())


def style_at(nav: pd.DataFrame, T, prox_w: dict, mkt_w: pd.Series) -> str:
    """[T-5y, T] 周频超额相关 argmax≥0.35 → 风格桶，否则均衡（eqgate 同法锚 T）。"""
    sub = nav[nav["date"] <= T]
    if sub.empty:
        return None
    end = sub["date"].max()
    w = sub[sub["date"] > end - pd.DateOffset(years=5)].set_index("date")["nav"]
    fr = w.resample("W-FRI").last().pct_change().dropna()
    fr_ex = (fr - mkt_w.reindex(fr.index)).dropna()
    if len(fr_ex) < 100:
        return None
    corrs = {}
    for bkt, pw in prox_w.items():
        both = pd.concat([fr_ex, pw.reindex(fr_ex.index)], axis=1).dropna()
        if len(both) >= 100:
            corrs[bkt] = both.iloc[:, 0].corr(both.iloc[:, 1])
    if not corrs:
        return None
    best = max(corrs, key=corrs.get)
    return best if corrs[best] >= G.STYLE_MIN_CORR else "均衡"


def main() -> int:
    t0 = time.time()
    pool = ES._pool(False)
    pairs = G.career_pairs(ES._mgr_table(False), pool["code"].tolist())
    f10_all = ES._f10s(sorted(set(pool["code"])), refresh=False, workers=8,
                       log=lambda *a, **k: None)
    mkt = G._load_close(G.STYLE_MARKET)
    type_of = dict(zip(pool["code"], pool["type_text"]))   # 一次性映射，避免 O(n²)
    prox_weekly = {}                                         # 代理全历史周收益（只算一次）
    for pname, bkt in G.STYLE_PROXIES.items():
        cl = G._load_close(pname)
        if cl is None:
            print("代理缺失:", pname)
            return 1
        prox_weekly[bkt] = cl.resample("W-FRI").last().pct_change().dropna()
    # 各起点：代理周频超额（窗口 [T-5y, T]）与代理/合成的逐年+5年收益
    org = {}
    for tag, T in ORIGINS:
        mkt_w = mkt.resample("W-FRI").last().pct_change().dropna()
        mkt_w = mkt_w[mkt_w.index <= T]
        prox_w, prox_yearly, prox_5y = {}, {}, {}
        for pname, bkt in G.STYLE_PROXIES.items():
            cl = G._load_close(pname)
            rp = prox_weekly[bkt]
            prox_w[bkt] = (rp - mkt_w.reindex(rp.index)).dropna()[lambda s: s.index <= T]
            prox_yearly[bkt] = [ES.s_total(cl, T - pd.DateOffset(years=k),
                                           T - pd.DateOffset(years=k - 1))
                                for k in range(1, 6)]
            prox_5y[bkt] = ES.s_ann(cl, T - pd.DateOffset(years=5), T)
        wealth = ES.composite_wealth()
        comp_yearly = {t: [ES.s_total(wealth[t], T - pd.DateOffset(years=k),
                                      T - pd.DateOffset(years=k - 1))
                           for k in range(1, 6)] for t in wealth}
        comp_5y = {t: ES.s_ann(wealth[t], T - pd.DateOffset(years=5), T)
                   for t in wealth}
        org[tag] = dict(T=T, prox_w=prox_w, prox_yearly=prox_yearly,
                        prox_5y=prox_5y, mkt_w=mkt_w,
                        comp_yearly=comp_yearly, comp_5y=comp_5y)
        print(f"[{tag}] 代理与合成基准就绪 @ {T.date()}")

    # ---- 单遍 nav：各起点的帧（span/yearly/r5/风格/β/结局） ----
    frames = {tag: {} for tag, _ in ORIGINS}
    for i, code in enumerate(sorted(set(pool["code"])), 1):
        nav = ES._read_nav_pickle(code)
        if nav is None or nav.empty:
            continue
        for tag, T in ORIGINS:
            sub = nav[nav["date"] <= T]
            if sub.empty:
                continue
            span = (sub["date"].max() - sub["date"].min()).days / 365.25
            if span < G.NAV_MIN_Y:
                continue
            yearly = [ES.nav_yearly(nav, T - pd.DateOffset(years=k),
                                    T - pd.DateOffset(years=k - 1))
                      for k in range(1, 6)]
            if any(v is None for v in yearly):
                continue
            r5 = ES.nav_ann_to(nav, T, 5)
            if r5 is None:
                continue
            o = org[tag]
            bkt = style_at(nav, T, o["prox_w"], o["mkt_w"])
            if bkt is None:
                continue
            # 6代理β尺度化（绝对空间，窗口 [T-5y,T]；代理周收益预计算复用）
            sub5 = sub[sub["date"] > sub["date"].max() - pd.DateOffset(years=5)]
            rf = sub5.set_index("date")["nav"].resample("W-FRI").last().pct_change().dropna()
            dfx = pd.concat([rf.rename("f"), pd.DataFrame(prox_weekly)],
                            axis=1, join="inner").dropna()
            beta = None
            if len(dfx) >= 100:
                A = np.column_stack([dfx.drop(columns=["f"]).values,
                                     np.ones(len(dfx))])
                try:
                    coef, *_ = np.linalg.lstsq(A, dfx["f"].values, rcond=None)
                    beta = dict(zip(list(dfx.columns[:-1]), coef[:-1]))
                except Exception:
                    beta = None
            # 结局：T→今天
            fut = nav[nav["date"] > T]
            fut_ret = None
            if len(fut) > 20 and sub["nav"].iloc[-1] > 0:
                fut_ret = float(fut["nav"].iloc[-1] / sub["nav"].iloc[-1] - 1)
            fut_mdd = None
            if len(fut) > 20:
                peak = np.maximum.accumulate(fut["nav"].values)
                fut_mdd = float(((peak - fut["nav"].values) / peak).max())
            frames[tag][code] = dict(yearly=yearly, r5=r5, bkt=bkt,
                                     beta=beta, fut_ret=fut_ret,
                                     fut_mdd=fut_mdd,
                                     mdd=windowed_mdd(nav, T))
        if i % 3000 == 0:
            print(f"  nav {i}/{len(pool)} {time.strftime('%H:%M:%S')}")
    print(f"帧构建完成 {(time.time()-t0)/60:.1f}min")

    mgr_csv = pd.read_csv(ROOT / "data" / "eq_score_mgrs.csv",
                          dtype={"rep_code": str})
    roster_codes = set(mgr_csv["rep_code"].str.zfill(6))
    car = pairs.groupby("code")["career_y"].max()

    report = {}
    all_rows = []
    for tag, T in ORIGINS:
        fr = frames[tag]
        if not fr:
            continue
        df = pd.DataFrame.from_dict(fr, orient="index")
        df.index.name = "code"
        df["type"] = [type_of.get(c) for c in df.index]
        # 同格逐年排位（与产线同法）
        for j in range(5):
            df[f"ps{j}"] = [fr[c]["yearly"][j] for c in df.index]
            df[f"back{j}"] = df[f"ps{j}"]
        for j in range(5):
            df[f"ps{j}"] = ES.rank_pcts(df, f"back{j}",
                                        [["type", "bkt"], ["bkt"], ["type"]])
        ok = df[[f"ps{j}" for j in range(5)]].notna().all(axis=1)
        df = df[ok].copy()
        # F1/F2
        psm = df[[f"ps{j}" for j in range(5)]].astype(float)
        df["F1"] = psm.mean(axis=1)
        df["F2"] = (psm > 50).sum(axis=1)
        # F3/F5 两变体
        o = org[tag]
        f3r, f3s, f5r, f5s = [], [], [], []
        for c, row in df.iterrows():
            bkt = row["bkt"]
            if bkt in o["prox_5y"] and o["prox_5y"][bkt] is not None:
                f3r.append(row["r5"] - o["prox_5y"][bkt])
                adjr = [row["yearly"][j] - o["prox_yearly"][bkt][j]
                        if o["prox_yearly"][bkt][j] is not None else None
                        for j in range(5)]
            else:
                t = type_of.get(c)
                f3r.append(row["r5"] - o["comp_5y"].get(t)
                           if o["comp_5y"].get(t) is not None else None)
                adjr = [row["yearly"][j] - o["comp_yearly"].get(t, [None]*5)[j]
                        if o["comp_yearly"].get(t, [None]*5)[j] is not None else None
                        for j in range(5)]
            beta = row["beta"]
            if beta is None:
                f3s.append(None); f5s.append(None)
            else:
                drag = sum(beta.get(b, 0) * (o["prox_5y"].get(b) or 0)
                           for b in beta)
                f3s.append(row["r5"] - drag)
                adjs = []
                for j in range(5):
                    d = sum(beta.get(b, 0) * (o["prox_yearly"].get(b, [None]*5)[j]
                           or 0) for b in beta)
                    adjs.append(row["yearly"][j] - d)
                f5s.append(sum(adjs) - max(adjs))
            f5r.append(sum(adjr) - max(adjr)
                       if all(x is not None for x in adjr) else None)
        df["F3r"], df["F3s"] = f3r, f3s
        df["F5r"], df["F5s"] = f5r, f5s
        # F6（任期/从业分位；任期= f10 现值近似）
        ten, carv = {}, {}
        for c in df.index:
            info = f10_all.get(c) or {}
            starts = []
            for m in info.get("managers") or []:
                try:
                    starts.append((T - pd.Timestamp(str(m.get("start") or "")[:10]))
                                  .days / 365.25)
                except Exception:
                    pass
            ten[c] = max(starts) if starts else np.nan
            carv[c] = car.get(c, np.nan)
        df["ten"] = pd.Series(ten); df["car"] = pd.Series(carv)
        df["F6"] = (df["ten"].rank(pct=True) + df["car"].rank(pct=True)) / 2
        # 分位 → 两版 W3
        cols_r = {"F1": "F1", "F2": "F2", "F3": "F3r", "F5": "F5r", "F6": "F6"}
        cols_s = {"F1": "F1", "F2": "F2", "F3": "F3s", "F5": "F5s", "F6": "F6"}
        pr = pd.DataFrame({k: df[v].rank(pct=True) * 100
                           for k, v in cols_r.items()})
        ps_ = pd.DataFrame({k: df[v].rank(pct=True) * 100
                            for k, v in cols_s.items()})
        df["W3r"] = sum(pr[k] * W3[k] for k in W3)
        df["W3s"] = sum(ps_[k] * W3[k] for k in W3)
        # 结局：同格排位（在起点同格内按未来收益排名，n≥10）
        cell = list(zip(df["type"], df["bkt"]))
        df["cell"] = cell
        fut_rank = []
        for cl, g in df.groupby("cell"):
            fr_ = g["fut_ret"]
            if len(g) >= 10 and fr_.notna().sum() >= 10:
                fut_rank.extend((fr_.rank(pct=True) * 100).tolist())
            else:
                fut_rank.extend([np.nan] * len(g))
        df["fut_rank"] = fut_rank
        # ---- IC 表 ----
        ics = {}
        for col in ["F1", "F2", "F3r", "F3s", "F5r", "F5s", "F6",
                    "W3r", "W3s", "r5"]:
            ics[col] = spearman(df[col], df["fut_rank"])
        ics["-mdd"] = spearman(-df["mdd"].astype(float), df["fut_rank"])
        ics["fut_ret"] = spearman(df["W3r"], df["fut_ret"])   # W3 vs 未来绝对收益
        report[tag] = dict(n=len(df), ics=ics)
        # ---- 掄除规则验证（cut vs kept） ----
        rules = {
            "E1 5年年化<0": df["r5"] < 0,
            "E2 回撤>60%": df["mdd"].astype(float) > 0.60,
            "E3 回撤>75%": df["mdd"].astype(float) > 0.75,
            "E4 规模<2亿(现值)": pd.Series(
                {c: (f10_all.get(c) or {}).get("scale_yi") or 0
                 for c in df.index}) < 2,
            "E5 F3虚高>10pp": (df["F3r"] - df["F3s"]) > 0.10,
        }
        for rname, cut in rules.items():
            cut = cut.fillna(False)
            rep = {}
            for gname, m in [("cut", cut), ("kept", ~cut)]:
                sub = df[m]
                rep[gname] = dict(
                    n=len(sub),
                    fut_ret=float(sub["fut_ret"].median()),
                    fut_rank=float(sub["fut_rank"].median()),
                    fut_mdd=float(sub["fut_mdd"].median()))
            report[tag].setdefault("rules", {})[rname] = rep
            all_rows.append(dict(origin=tag, kind="rule", rule=rname,
                                 cut_n=rep["cut"]["n"],
                                 cut_ret=rep["cut"]["fut_ret"],
                                 kept_ret=rep["kept"]["fut_ret"],
                                 cut_rank=rep["cut"]["fut_rank"],
                                 kept_rank=rep["kept"]["fut_rank"],
                                 cut_mdd=rep["cut"]["fut_mdd"],
                                 kept_mdd=rep["kept"]["fut_mdd"]))
        # 在榜子集上的同表
        sub_df = df[df.index.isin(roster_codes)]
        for rname, cut in rules.items():
            cut = cut.reindex(sub_df.index).fillna(False)
            if cut.any() and (~cut).any():
                all_rows.append(dict(
                    origin=tag, kind="roster", rule=rname,
                    cut_n=int(cut.sum()),
                    cut_ret=float(sub_df[cut]["fut_ret"].median()),
                    kept_ret=float(sub_df[~cut]["fut_ret"].median()),
                    cut_rank=float(sub_df[cut]["fut_rank"].median()),
                    kept_rank=float(sub_df[~cut]["fut_rank"].median()),
                    cut_mdd=float(sub_df[cut]["fut_mdd"].median()),
                    kept_mdd=float(sub_df[~cut]["fut_mdd"].median())))
        # 在榜者未来最差者（ground truth）
        ros = df[df.index.isin(roster_codes)].dropna(subset=["fut_rank"])
        worst = ros.nsmallest(15, "fut_rank")
        for c, row in worst.iterrows():
            all_rows.append(dict(origin=tag, kind="worst_roster", code=c,
                                 fut_rank=row["fut_rank"],
                                 fut_ret=row["fut_ret"], r5=row["r5"],
                                 mdd=row["mdd"], W3r=row["W3r"]))
        # 逐基金明细存盘（供规则联查：仓位/量化/小格/失真旗 × 未来结局）
        keep = df[["type", "bkt", "r5", "mdd", "fut_ret", "fut_mdd", "fut_rank",
                   "F1", "F2", "F3r", "F3s", "F5r", "F5s", "F6",
                   "W3r", "W3s"]].copy()
        keep.to_pickle(ROOT / f"data/eq_bt_{tag}.pkl")

    # ---- 打印 ----
    for tag in report:
        r = report[tag]
        print(f"\n===== {tag}（n={r['n']}）：对未来同格排位的 IC =====")
        for k, v in r["ics"].items():
            print(f"  {k:<6} {v:+.3f}")
        print("  排除规则（全池 cut vs kept，未来=T→今）：")
        for rn, rep in r["rules"].items():
            a, b = rep["cut"], rep["kept"]
            print(f"    {rn:<18} cut n={a['n']:<4} 未来收益 {a['fut_ret']:+.1%}"
                  f" vs kept {b['fut_ret']:+.1%} | 未来同格排位 "
                  f"{a['fut_rank']:.0f} vs {b['fut_rank']:.0f}"
                  f" | 未来回撤 {a['fut_mdd']:.0%} vs {b['fut_mdd']:.0%}")
    out = pd.DataFrame(all_rows)
    out.to_csv(OUT, index=False, encoding="utf-8-sig")
    print(f"\n明细 → {OUT}；总耗时 {(time.time()-t0)/60:.1f}min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
