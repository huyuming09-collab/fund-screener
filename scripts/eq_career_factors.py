# -*- coding: utf-8 -*-
"""生涯多因子探针 → 评分规则重优化依据（2026-11-15 用户："换成生涯标准是不是应该
重新优化评分规则"，并给出验尺：徐彦/刘旭/周云是好经理）。

背景：单指标"生涯时间加权年化"（q_career_df）把吴博俊从第2打到第31（稀释了单基金
归属噪声 ✓），但头部换成郑希/金梓才等、三个验尺落在 #11/#16/#9——单水平指标不构成
"评分规则"。本脚本把生涯标准做成**四因子**，用8期回测面板做前瞻验证、按证据定权重：
  C1 复合年化（水平；全产品含已离职、日收益等权复合→几何年化=引擎 manager_career_ann 同款）
  C2 生涯超额（同窗市场年化扣减 → 消除"生涯窗口年代不同"的不可比）
  C3 生涯年度胜率（复合曲线为正的年份占比 ≥3年才可算；一致性）
  C4 **窗口内**最大回撤（反向；**老将10年/新锐6年**——2026-11-18 用户定通道窗口）
验证：与面板 fwd1y/fut_rank 的逐期 Spearman（防未来函数）+ 留出期（前6定/后2验）；
锚点检查：徐彦/刘旭/周云 在候选规则下的名次（**及格线，不作拟合目标**）。
输入：data/eq_persist_panel.pkl（8期经理面板）+ 经理页 spans + 净值缓存
输出：控制台报告；data/eq_career_factors.pkl（key×origin 因子表）
运行（仓库根）：python scripts/eq_career_factors.py  （首跑含经理页补抓，约3-6min）
"""
from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from fundscorer import data as D                    # noqa: E402
from fundscorer import eqgate as G                  # noqa: E402
from fundscorer import eqscore as ES                # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FACTORS = ["c1", "c2", "c3", "c4"]
ANCHORS = ["徐彦", "刘旭", "周云"]


def spear(a, b) -> float:
    s = pd.concat([pd.Series(a, dtype="float64"),
                   pd.Series(b, dtype="float64")], axis=1).dropna()
    if len(s) < 25:
        return float("nan")
    return float(s.iloc[:, 0].rank().corr(s.iloc[:, 1].rank()))


def _nav_of(code: str):
    nav = ES._read_nav_pickle(code)
    if nav is not None and not nav.empty:
        return nav
    try:
        return D.fetch_nav(code)
    except Exception:
        return None


def main() -> int:
    t0 = time.time()
    panel = pd.read_pickle(ROOT / "data" / "eq_persist_panel.pkl")
    origins = sorted(panel["origin"].unique())
    print(f"面板: {len(panel)} 行 × {len(origins)} 期")

    # 经理页 spans（含已离职产品起止）——v1 缓存缺则补抓
    pool = ES._pool(False)
    pairs = G.career_pairs(ES._mgr_table(False), pool["code"].tolist())
    pairs = pairs[pairs["career_y"].notna()].reset_index(drop=True)
    f10_all = ES._f10s(sorted(set(pool["code"])), refresh=False, workers=8,
                       log=lambda *a, **k: None)
    codes_of_key: dict = {}
    for r in pairs.itertuples(index=False):
        codes_of_key.setdefault(f"{r.mgr}|{r.company}", set()).add(r.code)

    def id_of(k):
        mgr = k.split("|", 1)[0]
        return k, ES._manager_id_of(mgr, codes_of_key.get(k, ()), f10_all)

    keys = list(panel["key"].unique())
    with ThreadPoolExecutor(max_workers=8) as ex:
        ids = dict(ex.map(id_of, keys))

    def spans_of(k):
        mid = ids.get(k)
        if not mid:
            return []
        return D._manager_products_spans(mid)

    with ThreadPoolExecutor(max_workers=6) as ex:
        spans = dict(zip(keys, ex.map(spans_of, keys)))
    print(f"经理页 spans 就绪（有产品的: {sum(1 for v in spans.values() if v)}）")

    mkt = G._load_close(G.STYLE_MARKET)
    mkt = mkt[mkt.index <= pd.Timestamp(origins[-1])]

    def career_at(k, T: pd.Timestamp, chan: str = "A"):
        """四因子@T：产品窗 [start, min(end,T)]，≥250天；共管不拆（按用户规格）。
        c1=复合年化（引擎同款：日收益等权→几何年化）；
        c4=窗口内回撤，窗口按通道（2026-11-18 用户定：老将10年/新锐6年）。"""
        rets = []
        num2 = den2 = 0.0
        for p in spans.get(k, []):
            s0 = p.get("start")
            e0 = p.get("end") or T
            e0 = min(pd.Timestamp(e0), T)
            if s0 is None or e0 <= s0:
                continue
            nav = _nav_of(p["code"])
            if nav is None or nav.empty:
                continue
            sl = nav[(nav["date"] >= s0) & (nav["date"] <= e0)]
            if len(sl) < 250:
                continue
            d0, d1 = sl["date"].iloc[0], sl["date"].iloc[-1]
            days = (d1 - d0).days
            if days < 250:
                continue
            try:
                ann = float(sl["nav"].iloc[-1] / sl["nav"].iloc[0]) ** (365.0 / days) - 1
            except (ZeroDivisionError, OverflowError):
                continue
            rets.append(sl.set_index("date")["nav"].pct_change().dropna())
            m = ES.s_ann(mkt, s0, e0)
            if m is not None and m == m:
                num2 += days * (ann - m)
                den2 += days
        if not rets:
            return None
        port = pd.concat(rets, axis=1).mean(axis=1, skipna=True).dropna()
        if len(port) < 500:
            return None
        tot_days = (port.index[-1] - port.index[0]).days
        if tot_days < 365:
            return None
        try:
            c1 = float((1 + port).prod()) ** (365.0 / tot_days) - 1
        except (OverflowError, ZeroDivisionError):
            return None
        c2 = (num2 / den2) if den2 > 0 else None
        # C3：全历史年度胜率；C4：通道窗口回撤（老将10年/新锐6年）
        c3 = c4 = None
        curve = (1 + port).cumprod()
        yr = curve.resample("YE").last().pct_change().dropna()
        if len(yr) >= 3:
            c3 = float((yr > 0).mean())
        wy = 10.0 if chan == "A" else 6.0
        cut = port.index[-1] - pd.DateOffset(years=wy)
        seg = curve[curve.index > cut]
        if len(seg) >= 200:
            peak = seg.cummax()
            c4 = float(((peak - seg) / peak).max())
        return dict(c1=c1, c2=c2, c3=c3, c4=c4, n=len(rets))

    # 逐 key×origin 计算（nav 缓存内；首跑含补抓）
    rows = []
    done = set()
    for r in panel.itertuples(index=False):
        T = pd.Timestamp(r.origin)
        ck = (r.key, r.origin)
        if ck in done:
            continue
        done.add(ck)
        f = career_at(r.key, T, getattr(r, "chan", "A"))
        if f:
            rows.append(dict(key=r.key, origin=r.origin, **f))
    cf = pd.DataFrame(rows)
    cf.to_pickle(ROOT / "data" / "eq_career_factors.pkl")
    print(f"生涯四因子完成 {len(cf)} 行，耗时 {(time.time()-t0)/60:.1f}min")

    # ---- IC：逐期 Spearman vs 前瞻（防未来函数）----
    mrg = panel.merge(cf, on=["key", "origin"], how="inner")
    mrg["c4r"] = -mrg["c4"]                      # 回撤反向化（浅=好）
    use = ["c1", "c2", "c3", "c4r"]
    print("\n== 生涯四因子 IC（逐期 Spearman 均值 | 符号一致期占比 | 期数）==")
    for ch, lab in (("A", "老将"), ("B", "新锐")):
        sub = mrg[mrg["chan"] == ch]
        print(f"-- {lab} --")
        for f in use:
            rr_f, rr_r, ns = [], [], []
            for o in origins:
                d = sub[sub["origin"] == o]
                fv = pd.to_numeric(d[f], errors="coerce")
                ok = fv.notna() & d["fwd1y"].notna() & d["fut_rank"].notna()
                d2, fv2 = d[ok], fv[ok]
                if len(d2) < 25:
                    continue
                ns.append(o)
                rr_f.append(spear(fv2, d2["fwd1y"]))
                rr_r.append(spear(fv2, d2["fut_rank"]))
            if not ns:
                continue
            mf, mr = np.nanmean(rr_f), np.nanmean(rr_r)
            cons = np.mean([np.sign(x) == np.sign(mf) for x in rr_f if x == x]) if mf == mf else np.nan
            print(f"   {f}: IC_fwd {mf:+.3f} | IC_rank {mr:+.3f} | 一致 {cons:.0%} | 期 {len(ns)}")

    # ---- 候选规则对比（留出期=后2期，训练=前6期定权）----
    print("\n== 候选规则（训练期定权 → 全期与留出期验证）==")
    train_o, test_o = origins[:6], origins[6:]

    def weights_for(data):
        w = {}
        for f in use:
            vals = []
            for o in data["origin"].unique():
                d = data[data["origin"] == o]
                fv = pd.to_numeric(d[f], errors="coerce")
                ok = fv.notna() & d["fwd1y"].notna() & d["fut_rank"].notna()
                d2, fv2 = d[ok], fv[ok]
                if len(d2) < 25:
                    continue
                vals.append((spear(fv2, d2["fwd1y"]) + spear(fv2, d2["fut_rank"])) / 2)
            w[f] = max(float(np.nanmean(vals)) if vals else 0.0, 0.0)
        s = sum(w.values())
        return {f: (x / s if s > 0 else 1 / len(use)) for f, x in w.items()}

    def score_with(data, w):
        s = pd.Series(0.0, index=data.index)
        for f, wt in w.items():
            if wt <= 0:
                continue
            s = s + (pd.to_numeric(data[f], errors="coerce")
                     .rank(pct=True) * 100).fillna(50) * wt
        return s

    for ch, lab in (("A", "老将"), ("B", "新锐")):
        tr = mrg[(mrg["chan"] == ch) & mrg["origin"].isin(train_o)]
        te = mrg[(mrg["chan"] == ch) & mrg["origin"].isin(test_o)]
        if len(tr) == 0:
            continue
        w_ic = weights_for(tr)
        w_eq = {f: 1 / len(use) for f in use}
        print(f"-- {lab} -- IC加权(训练期): " +
              " ".join(f"{f}={w:.2f}" for f, w in w_ic.items()))
        for name, w in (("IC加权", w_ic), ("等权", w_eq)):
            for lab2, dd in (("全期", mrg[mrg["chan"] == ch]),
                             ("留出期", te)):
                if len(dd) == 0:
                    continue
                rf, rr = [], []
                for o in dd["origin"].unique():
                    d = dd[dd["origin"] == o]
                    sc = score_with(d, w)
                    ok = d["fwd1y"].notna() & d["fut_rank"].notna()
                    if ok.sum() < 25:
                        continue
                    rf.append(spear(sc[ok], d.loc[ok, "fwd1y"]))
                    rr.append(spear(sc[ok], d.loc[ok, "fut_rank"]))
                if rf:
                    print(f"   {name}·{lab2}: IC_fwd {np.nanmean(rf):+.3f} "
                          f"| IC_rank {np.nanmean(rr):+.3f} | 期 {len(rf)}")

    # ---- 验尺（用户2026-11-15：徐彦/刘旭/周云是好经理——及格线非拟合目标）----
    print("\n== 验尺检查（老将榜内名次，按不同规则）==")
    mgr = pd.read_csv(ROOT / "data" / "eq_score_mgrs.csv")
    mgr = mgr[mgr["list_"] == "vet"].copy()
    mgr["key"] = mgr["name"] + "|" + mgr["company"]
    last_o = origins[-1]
    late = mrg[(mrg["origin"] == last_o) & (mrg["chan"] == "A")].set_index("key")
    rules = {}
    trA = mrg[(mrg["chan"] == "A") & mrg["origin"].isin(train_o)]
    rules["纯C1年化"] = {f: (1.0 if f == "c1" else 0.0) for f in use}
    rules["IC加权"] = weights_for(trA) if len(trA) else {f: 0.25 for f in use}
    rules["等权四因子"] = {f: 0.25 for f in use}
    for rname, w in rules.items():
        j = mgr.merge(late.reset_index()[["key"] + use], on="key", how="inner")
        if len(j) == 0:
            continue
        sc = score_with(j.set_index("key"), w)
        j = j.assign(sc=sc).sort_values("sc", ascending=False).reset_index(drop=True)
        j["pos"] = j.index + 1
        pos = {r["name"]: int(r["pos"]) for _, r in j.iterrows() if r["name"] in ANCHORS}
        print(f"   {rname:<10} " + " | ".join(
            f"{n}#{pos.get(n, '-')}" for n in ANCHORS) +
            f"  （共{len(j)}人；头部: " +
            "、".join(j.head(5)["name"]) + "）")

    print(f"\n产出: data/eq_career_factors.pkl | 耗时 {(time.time()-t0)/60:.1f}min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())