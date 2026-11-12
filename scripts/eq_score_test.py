# -*- coding: utf-8 -*-
"""评分系统 v1 设计验证：检验18 预测力 / 检验19 前列稳定性 / 检验20 同类公平
+ 权重方案择优（W1 等权 / W2 核心导向 / W3 LOOCV-IC）。

用户 2026-11-12 要求：① 名单前列一定范围内相对稳定、变换不激烈；
② 同类区分好，不因评分不合适把优秀经理排低分；③ 数据验证、科学合理。

因子（全部先转"分位 0-100"再加权；分位归一保证跨格可比）：
  F1 持续性 = 5年同格逐年排位均值        （检验9 最强单一信号）
  F2 胜率   = 5年中跑赢同格中位年数
  F3 风格调整超额 = 5年收益 − 自身风格指数5年收益（均衡/无代理桶用类型合成）
  F4 稳定性 = −(风格调整超额逐年标准差)
  F5 防运气 = 去最佳年后风格调整超额合计
  F6 质地   = (任期分位 + 从业分位)/2     （小权重）
验证：
  检验18 3个年度起点：两榜内按各方案取前20（基金）→ 未来同格排位/优秀率；
        全合格池 score-vs-未来排位 IC；对照 随机 / 单F1 / 计数序
  检验19 周(+7d)/月(+30d)：经理级前10/20 留存（纯滚动 vs 双周确认两种政策）；
        对照 计数序 / F1序 / 从业年限序
  检验20 风格顺风 vs 逆风 cell：榜内高分者的未来表现是否相当（不冤枉逆风优秀者）
运行（仓库根）：python scripts/eq_score_test.py（特征缓存命中，约2-4min）
结论写入 docs/20 §2.2（评分定稿）。
"""
from __future__ import annotations

import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import numpy as np                                  # noqa: E402
import pandas as pd                                 # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from fundscorer import data as D                    # noqa: E402
from fundscorer import eqgate as G                  # noqa: E402
import scripts.eq_peer_test as P                    # noqa: E402
import scripts.eq_sustain_check as S                # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
NAV_DIR = ROOT / "data" / "cache"
T1 = pd.Timestamp("2026-11-12")
ANNUAL = [pd.Timestamp("2023-11-12"), pd.Timestamp("2024-11-12"),
          pd.Timestamp("2025-11-12")]
STAB = [pd.Timestamp("2025-11-12"), pd.Timestamp("2025-11-19"),
        pd.Timestamp("2025-12-12")]
FACTORS = ["F1", "F2", "F3", "F4", "F5", "F6"]
W_EQUAL = dict(zip(FACTORS, [1 / 6] * 6))
W_CORE = {"F1": .30, "F2": .10, "F3": .20, "F4": .15, "F5": .15, "F6": .10}
PROXY_BY_BUCKET = {b: p for p, b in G.STYLE_PROXIES.items()}


def s_total(s: pd.Series, a, b) -> float | None:
    sub = s[(s.index > a) & (s.index <= b)]
    if len(sub) < 2:
        return None
    return float(sub.iloc[-1] / sub.iloc[0] - 1)


def s_ann(s: pd.Series, a, b) -> float | None:
    sub = s[(s.index > a) & (s.index <= b)]
    if len(sub) < 2:
        return None
    days = (sub.index[-1] - sub.index[0]).days
    if days <= 0:
        return None
    return float((sub.iloc[-1] / sub.iloc[0]) ** (365.0 / days) - 1)


def build_factors(o, feat, qual, pairs, wealth, f10s) -> pd.DataFrame:
    """合格池上的 6 因子表（分位 0-100）+ 通道 + 任期/从业。"""
    shift = (T1 - o).days / 365.25
    # 风格指数年度/5年收益
    mkt_ok = {}
    for bkt, proxy in PROXY_BY_BUCKET.items():
        cl = G._load_close(proxy)
        mkt_ok[bkt] = cl
    back_win = [(o - pd.DateOffset(years=k), o - pd.DateOffset(years=k - 1))
                for k in range(1, 6)]
    # 预切风格指数逐年/5年
    proxy_y, proxy_5y = {}, {}
    for bkt, cl in mkt_ok.items():
        proxy_y[bkt] = [s_total(cl, a, b) for a, b in back_win]
        proxy_5y[bkt] = s_ann(cl, o - pd.DateOffset(years=5), o)
    comp5 = {t: S.comp_ann(w, o - pd.DateOffset(years=5), o)
             for t, w in wealth.items()}

    rows = {}
    for c in qual:
        f = feat.loc[c]
        bkt = f["bucket"]
        # 逐年风格调整超额
        if bkt in proxy_y and proxy_y[bkt][0] is not None:
            adj = [None if (f[f"back{j}"] is None or proxy_y[bkt][j] is None)
                   else f[f"back{j}"] - proxy_y[bkt][j] for j in range(5)]
            a5 = (None if (pd.isna(f["ret5y"]) or proxy_5y.get(bkt) is None)
                  else f["ret5y"] - proxy_5y[bkt])
        else:                                    # 均衡/无代理 → 类型合成
            adj = [f[f"cexc_{j}"] for j in range(5)]
            a5 = (None if (pd.isna(f["ret5y"]) or comp5.get(f["type"]) is None)
                  else f["ret5y"] - comp5[f["type"]])
        ps = [f[f"ps_{j}"] for j in range(5)]
        ok_ps = all(not pd.isna(x) for x in ps)
        ok_adj = all(x is not None for x in adj)
        f1 = float(np.mean(ps)) if ok_ps else None
        f2 = float(sum(1 for x in ps if x > 50)) if ok_ps else None
        f3 = a5
        f4 = -float(np.std(adj, ddof=1)) if (ok_adj and
                                             sum(x is not None for x in adj) >= 3
                                             and all(x is not None for x in adj)) else None
        f5 = (sum(adj) - max(adj)) if ok_adj else None
        rows[c] = dict(F1=f1, F2=f2, F3=f3, F4=f4, F5=f5,
                       bucket=bkt, type=f["type"], ret5y=f["ret5y"])
    df = pd.DataFrame(rows).T
    # 任期/从业（shift 后）
    ten = pairs.groupby("code").apply(
        lambda g: max((o - pd.Timestamp(str(s)[:10])).days / 365.25
                      for s in g["start"] if s is not None and s == s)
        if any(s is not None and s == s for s in g["start"]) else np.nan,
        include_groups=False) if "start" in pairs.columns else None
    # pairs 无 start 列 → 从 f10 取
    if ten is None:
        ten = {}
        for c in df.index:
            info = f10s.get(c)
            if not info:
                continue
            starts = []
            for m in info.get("managers") or []:
                try:
                    starts.append(o - pd.Timestamp(m["start"][:10]))
                except Exception:
                    pass
            ten[c] = max((d.days / 365.25 for d in starts), default=np.nan)
        ten = pd.Series(ten)
    car = pairs.groupby("code")["career_y"].max() - shift
    df["ten"] = ten.reindex(df.index)
    df["car"] = car.reindex(df.index)
    df["chan"] = df["car"].map(lambda x: "A" if x == x and x > 10
                               else ("B" if x == x and x > 6 else "out"))
    # 分位归一（合格池内）
    for col in ["F1", "F2", "F3", "F4", "F5", "ten", "car"]:
        v = pd.to_numeric(df[col], errors="coerce")
        df[col + "_p"] = (v.rank(pct=True) * 100).round(2)
    df["F6_p"] = df[["ten_p", "car_p"]].mean(axis=1)
    return df


def scores(df: pd.DataFrame, w: dict) -> pd.Series:
    cols = [f + "_p" for f in w]
    m = df[cols].astype(float)
    wts = pd.Series([w[f] for f in w], index=cols)   # 权重索引须与列名一致
    return (m * wts).sum(axis=1) / sum(w.values())


def ic(factor: pd.Series, future: pd.Series) -> float:
    d = pd.concat([factor.astype(float), future.astype(float)],
                  axis=1).dropna()
    if len(d) < 30:
        return np.nan
    # Spearman = 秩的 Pearson（不引 scipy，仓库依赖只有 pandas/numpy）
    r1 = d.iloc[:, 0].rank()
    r2 = d.iloc[:, 1].rank()
    return float(r1.corr(r2))


def mgr_top(funds, scores_s, pairs, shift, k):
    """基金 → 经理分（成员基金均分）→ 前k（分数降序，任期降序破平）。"""
    sub = pairs[pairs["code"].isin(funds)]
    if sub.empty:
        return []
    sub = sub.assign(sc=sub["code"].map(scores_s))
    sub = sub.dropna(subset=["sc"])
    g = sub.groupby(["mgr", "company"]).agg(
        sc=("sc", "mean"), ten=("code", "count"))
    g = g.sort_values(["sc", "ten"], ascending=[False, False])
    return list(g.head(k).index)


def main():
    t0 = time.time()
    pool = G.load_pool()
    pairs = G.career_pairs(D._manager_em_table(), pool["code"].tolist())
    pairs = pairs[pairs["career_y"].notna()].reset_index(drop=True)
    wealth = S.build_composite()
    cand = set(pairs.loc[pairs["career_y"] > G.CAREER_MIN_Y, "code"])
    f10s = {}
    for c in cand:
        p = NAV_DIR / f"f10_v3_{c}.pkl"
        if p.exists():
            try:
                f10s[c] = pd.read_pickle(p)
            except Exception:
                pass
    # 任期（每基金现任经理最长 tenure，截至各时点）
    starts_map = {}
    for c in pairs["code"].unique():
        info = f10s.get(c)
        if not info:
            continue
        for m in info.get("managers") or []:
            try:
                starts_map[(m["name"], c)] = pd.Timestamp(m["start"][:10])
            except Exception:
                pass
    pairs["start"] = [starts_map.get((r.mgr, r.code)) for r in pairs.itertuples()]

    store = {}
    for o in dict.fromkeys(ANNUAL + STAB):
        data = P.build_origin(o, pool, dict(zip(pool["code"], pool["name"])),
                              wealth)
        feat = data["feat"]
        qual = P.qualified_at(o, feat, pairs, f10s)
        df = build_factors(o, feat, qual, pairs, wealth, f10s)
        sel = P.select_variants(o, feat, qual, pairs, f10s)
        shift = (T1 - o).days / 365.25
        pf = [c for c in feat.columns if c.startswith("pf_")]
        fut = feat[pf].mean(axis=1) if pf else pd.Series(dtype=float)
        store[o] = dict(feat=feat, df=df, sel=sel, qual=qual, fut=fut,
                        shift=shift)
        print(f"    {o.date()}: 合格 {len(qual)} 因子可算 {len(df)}", flush=True)

    # ---- W3 LOOCV-IC 权重（年度起点上因子-未来排位 IC）----
    print("\n[IC] 各年度起点 因子→未来同格排位 Spearman IC:", flush=True)
    ics = {}
    for f in FACTORS:
        vals = [ic(store[o]["df"][f + "_p"], store[o]["fut"])
                for o in ANNUAL]
        ics[f] = vals
        print(f"  {f}: " + " | ".join(f"{v:+.3f}" if v == v else "  n/a"
                                     for v in vals), flush=True)

    def w3_for(holdout):
        ws = {}
        for f in FACTORS:
            peers = [v for o, v in zip(ANNUAL, ics[f])
                     if o != holdout and v == v]
            ws[f] = max(float(np.mean(peers)), 0.0) if peers else 0.0
        if sum(ws.values()) <= 0:
            return W_EQUAL
        return {f: v / sum(ws.values()) for f, v in ws.items()}

    schemes = {"W1等权": W_EQUAL, "W2核心": W_CORE,
               "W3(IC-LOOCV)": None}       # W3 按留出起点定
    print(f"\n  W2核心权重: {W_CORE}")

    # ---- 检验18：预测力（年度起点，榜内前20基金）----
    print("\n" + "=" * 76)
    print("检验18 评分预测力（两榜内按方案取前20基金 → 未来同格排位）")
    for o in ANNUAL:
        st = store[o]
        df, sel = st["df"], st["sel"]
        w3 = w3_for(o)
        lists = {"老将榜": {c for c in sel["G3s"] if c in df.index
                            and df.at[c, "chan"] == "A"},
                 "新锐榜": {c for c in sel["G4s"] if c in df.index
                            and df.at[c, "chan"] == "B"}}
        print(f"\n  起点 {o.date()}（未来 "
              f"{len([c for c in st['feat'].columns if c.startswith('pf_')])} 年）")
        for lname, base in lists.items():
            base = {c for c in base if c in df.index}
            if len(base) < 10:
                continue
            parts = []
            # 随机基线
            rng = np.random.default_rng(7)
            rnd = list(rng.choice(sorted(base), size=min(20, len(base)),
                                  replace=False))
            for label, w in [("随机", None), ("单F1", "F1"),
                             ("W1", W_EQUAL), ("W2", W_CORE),
                             ("W3", w3)]:
                if w is None:
                    pick = rnd
                elif w == "F1":
                    pick = (df.loc[sorted(base), "F1_p"]
                            .sort_values(ascending=False).head(20).index.tolist())
                else:
                    sc = scores(df.loc[sorted(base)], w)
                    pick = sc.sort_values(ascending=False).head(20).index.tolist()
                fut = st["fut"].reindex(pick).dropna()
                if len(fut) >= 5:
                    exc = (fut >= P.FWD_EXC_PCT).mean()
                    parts.append(f"{label} {fut.mean():.1f}/{exc:.0%}"
                                 f"(n{len(fut)})")
            print(f"    {lname}({len(base)}只): " + " | ".join(parts))
        # 全合格池 IC
        row = []
        for label, w in [("W1", W_EQUAL), ("W2", W_CORE), ("W3", w3)]:
            sc = scores(df, w)
            row.append(f"{label} IC={ic(sc, st['fut']):+.3f}")
        print(f"    全合格池: " + " | ".join(row))

    # ---- 检验19：前列稳定性（经理级，周/月）----
    print("\n" + "=" * 76)
    print("检验19 前列稳定性（经理级 top10/top20 留存；纯滚动 vs 双周确认）")
    w3s = w3_for(ANNUAL[2])
    d0, d1, d2 = STAB
    s0, s1, s2 = store[d0], store[d1], store[d2]

    def list_mgrs(st, rule, ch, order, w=None):
        df = st["df"]
        base = {c for c in st["sel"][rule] if c in df.index
                and df.at[c, "chan"] == ch}
        if not base:
            return []
        sub = df.loc[sorted(base)]
        if order == "count":
            col = "F2_p"      # 胜率分位 ≈ 计数序（同格内）
            key = sub["F1_p"] * 0 + sub["F2_p"]
            idx = sub.assign(k=sub["F2_p"]).sort_values(
                "k", ascending=False).index.tolist()
        elif order == "F1":
            idx = sub["F1_p"].sort_values(ascending=False).index.tolist()
        elif order == "career":
            idx = sub["car"].astype(float).sort_values(
                ascending=False).index.tolist()
        else:
            sc = scores(sub, w)
            idx = sc.sort_values(ascending=False).index.tolist()
        # 基金 → 经理
        sub2 = pairs[pairs["code"].isin(idx)].copy()
        sub2["rk"] = sub2["code"].map({c: i for i, c in enumerate(idx)})
        g = sub2.groupby(["mgr", "company"])["rk"].min()
        return list(g.sort_values().index)

    for gap, later in ((7, s1), (30, s2)):
        print(f"\n  +{gap}天（{d0.date()} → "
              f"{STAB[1 if gap == 7 else 2].date()}）:")
        for lname, rule, ch in (("老将榜", "G3s", "A"), ("新锐榜", "G4s", "B")):
            out = []
            for label, order in (("计数序", "count"), ("F1序", "F1"),
                                 ("年限序", "career"),
                                 ("W1", "sc1"), ("W2", "sc2"),
                                 ("W3", "sc3")):
                w = {"sc1": W_EQUAL, "sc2": W_CORE, "sc3": w3s}.get(order)
                a = list_mgrs(s0, rule, ch, order, w)
                b = list_mgrs(later, rule, ch, order, w)
                if not a:
                    continue
                res = []
                for K in (10, 20):
                    if len(a) >= K and len(b) >= K:
                        res.append(f"top{K}:"
                                   f"{len(set(a[:K]) & set(b[:K]))/K:.0%}")
                    else:
                        res.append(f"top{K}:—")
                out.append(f"{label} " + "/".join(res))
            if out:
                print(f"    {lname:<6} " + " | ".join(out))
        # 纯滚动 vs 双周确认（成员政策）
        for rule, ch in (("G3s", "A"), ("G4s", "B")):
            a = {c for c in s0["sel"][rule] if c in s0["df"].index
                 and s0["df"].at[c, "chan"] == ch}
            b = {c for c in later["sel"][rule] if c in later["df"].index
                 and later["df"].at[c, "chan"] == ch}
            if not a:
                continue
            raw = len(a & b) / len(a)
            # 双周确认：t0 成员首过不过不出（硬条件除外，此处忽略）→ 留存100%+新进
            print(f"    [政策] {rule}∩{ch}: 纯滚动留存 {raw:.0%} | "
                  f"双周确认：首过不出→留存100%，新进 +{len(b - a)}")

    # ---- 检验20：同类公平（风格顺风 vs 逆风）----
    print("\n" + "=" * 76)
    print("检验20 同类公平（风格顺风/逆风 cell 的高分者未来表现）")
    for o in ANNUAL:
        st = store[o]
        df = st["df"]
        # cell 风格周期位置：cell 内 (ret5y − 类型合成) 中位
        comp5 = {t: S.comp_ann(w, o - pd.DateOffset(years=5), o)
                 for t, w in wealth.items()}
        df = df.copy()
        df["absx"] = [np.nan if pd.isna(r["ret5y"])
                      or comp5.get(r["type"]) is None
                      else float(r["ret5y"]) - comp5[r["type"]]
                      for _, r in df.iterrows()]
        cell = df.groupby(["type", "bucket"])["absx"].transform("median")
        df["tail"] = cell > 0          # True=顺风
        sc = scores(df, W_CORE)
        df["sc"] = sc
        # 各 cell 平均分（构造检查：分位归一后应接近）
        cell_mean = df.groupby(["type", "bucket"])["sc"].mean()
        print(f"\n  {o.date()}: cell平均分 极差 "
              f"{cell_mean.max() - cell_mean.min():.1f}（分位归一→应小）")
        for rule, ch in (("G3s", "A"), ("G4s", "B")):
            sub = df[(df["chan"] == ch) & df.index.isin(st["sel"][rule])]
            if len(sub) < 15:
                continue
            parts = []
            for tail_lab, mask in (("顺风", sub["tail"]),
                                   ("逆风", ~sub["tail"])):
                g = sub[mask].sort_values("sc", ascending=False).head(10)
                fut = st["fut"].reindex(g.index).dropna()
                if len(fut) >= 5:
                    parts.append(f"{tail_lab} n{len(g)} 未来排位 "
                                 f"{fut.mean():.1f} 优秀率 {(fut >= P.FWD_EXC_PCT).mean():.0%}")
            if parts:
                print(f"    {rule}∩{ch} top10分组: " + " | ".join(parts))
        # 分数 vs 原始收益 的相关（越低越不被风格收益污染）
        print(f"    score vs 原始ret5y 相关 {ic(sc, df['ret5y']):+.3f} | "
              f"score vs 未来排位 IC {ic(sc, st['fut']):+.3f}")

    print(f"\n总耗时 {(time.time()-t0)/60:.1f}min", flush=True)


if __name__ == "__main__":
    main()
