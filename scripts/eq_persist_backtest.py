# -*- coding: utf-8 -*-
"""长期回测 + "持续上榜"稳定性指标（2026-11-14，资产配置目标自检配套）。

问题（用户）：能否做一个长期回测，设立稳定性指标——在某一体系下**持续上榜**的基金经理。
验证（用户假设）：持续上榜者是否真的更强（未来绝对收益/同格排位）。

设计
----
- 起点：年度 2018-11-12 … 2025-11-12（8 期；净值≥5年与5年窗口决定早期可评估集）
- 每起点重建（能回溯的全回溯）：
    · 风格 = [T-5y,T] 周频超额 vs 6代理 argmax≥0.35（eq_backtest_t0 同法，非现值）
    · 逐年同格 ps（类型×桶→桶→类型回退 n≥10；**含小格合并**：A格<150 改在类型×三类内取）
    · F1/F2/F3r/F5r + F6（任期= f10 start 到 T；从业 = career_y 平移到 T）
    · G2 r5(T) ≥ -5%；**G1 现值近似**（f10 scale_yi 按 base_name 合并 ≥2亿）
    · 双十 @T = 从业@T>10 且 **复合年化@T>10%**（经理产品页起止 + nav 切到 T 的等权复合）
    · 通道 A/B = 从业@T（>10 / >6）
    · 考卷 G3s = 5窗中>75分位≥3；G4s = 5窗均值≥70（阈值同产线）
  上榜 S_t =（双十 OR 过考）∩ 通道 ∩ G1 ∩ G2（计分基金维度，同产线语义）
- 稳定性指标：持续上榜率 P=在榜期数/可评估期数、最长连任、近3期连续、进出场次数；
  分层 P≥80% 核心稳定 / 50-80% 常驻 / <50% 波动
- 验证（防未来函数）：逐起点 t 用"截至 t 的 P"分组 → t 之后 1 年绝对收益与同格排位，
  persistent / occasional / never + 随机同规模基线

披露（固有局限，报告与 STATUS 均须带）
----
1. 池 = 现存且净值≥5年 → **幸存者偏差**（历史收益偏乐观）
2. 风格为窗口重建，**不含持仓校验降级**（2026-11 才上线）
3. G1 规模为**现值近似**（历史规模不可得）
4. G3 仓位闸**不回溯**（仅10季仓位史）；机构占比/严格 L1 资格闸不回溯（span≥5年近似）

运行（仓库根）：python scripts/eq_persist_backtest.py  （首跑含经理页/产品净值抓取，约15-40min）
输出：data/eq_persist.pkl（经理×起点面板 + 指标）+ 控制台报告
"""
from __future__ import annotations

import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from fundscorer import data as D                    # noqa: E402
from fundscorer import eqgate as G                  # noqa: E402
from fundscorer import eqscore as ES                # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ORIGINS = [pd.Timestamp(f"{y}-11-12") for y in range(2018, 2026)]
FWD_Y = 1.0                                          # 每起点前瞻 1 年
W3 = ES.W3
TODAY = pd.Timestamp.today().normalize()


# ------------------------------------------------------------------ 工具
def style_at(nav, T, prox_w, mkt_w):
    """[T-5y,T] 周频超额相关 argmax≥0.35 → 风格桶，否则均衡（eq_backtest_t0 同法）。"""
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


def composite_ann_at(spans, T, nav_of):
    """复合年化@T：产品各自 [start, min(end,T)] 日收益等权复合（口径同 manager_career_ann）。"""
    rets = []
    for p in spans:
        s0 = p["start"]
        if s0 is None or s0 > T:
            continue
        e0 = p["end"] if p["end"] is not None else T
        e0 = min(e0, T)
        if e0 <= s0:
            continue
        nav = nav_of(p["code"])
        if nav is None or nav.empty:
            continue
        s = nav[(nav["date"] >= s0) & (nav["date"] <= e0)]
        if len(s) < 250:
            continue
        r = s.set_index("date")["nav"].pct_change().dropna()
        if len(r) >= 250:
            rets.append(r)
    if not rets:
        return None
    port = pd.concat(rets, axis=1).mean(axis=1, skipna=True).dropna()
    if len(port) < 250:
        return None
    yrs = (port.index[-1] - port.index[0]).days / 365.0
    if yrs < 1:
        return None
    try:
        val = float((1 + port).prod() ** (1.0 / yrs) - 1)
    except (OverflowError, ZeroDivisionError):
        return None
    return val if val == val and abs(val) < 1.0 else None


def rank_pcts_merged(df, col):
    """产线同法 ps：A格(type×bkt) n≥10；A格 n<CELL_MERGE_N 时改在（类型×三类）内取。"""
    a = ES.rank_pcts(df, col, [["type", "bkt"], ["bkt"], ["type"]])
    b = ES.rank_pcts(df, col, [["type", "_c3"], ["_c3"], ["type"]])
    small = df.groupby(["type", "bkt"])["bkt"].transform("size") < ES.CELL_MERGE_N
    return pd.Series(np.where(small, b, a), index=df.index)


def spearman(a, b):
    s = pd.concat([pd.Series(a, dtype="float64"),
                   pd.Series(b, dtype="float64")], axis=1).dropna()
    if len(s) < 20:
        return float("nan")
    return float(s.iloc[:, 0].rank().corr(s.iloc[:, 1].rank()))


# ------------------------------------------------------------------ 主流程
def main() -> int:
    t0 = time.time()
    pool = ES._pool(False)
    pairs = G.career_pairs(ES._mgr_table(False), pool["code"].tolist())
    pairs = pairs[pairs["career_y"].notna()].reset_index(drop=True)
    f10_all = ES._f10s(sorted(set(pool["code"])), refresh=False, workers=8,
                       log=lambda *a, **k: None)
    type_of = dict(zip(pool["code"], pool["type_text"]))
    name_of = dict(zip(pool["code"], pool["name"]))
    mkt = G._load_close(G.STYLE_MARKET)
    mkt_w = mkt.resample("W-FRI").last().pct_change().dropna()
    prox_weekly, prox_close = {}, {}
    for pname, bkt in G.STYLE_PROXIES.items():
        cl = G._load_close(pname)
        if cl is None:
            print("代理缺失:", pname)
            return 1
        prox_close[bkt] = cl
        prox_weekly[bkt] = cl.resample("W-FRI").last().pct_change().dropna()
    wealth = ES.composite_wealth()

    # G1 现值近似：f10 scale_yi 按 base_name 合并（eq_diag 同法）
    base_scale: dict = {}
    for c, info in f10_all.items():
        s = float((info or {}).get("scale_yi") or 0)
        b = ES.base_name(str(name_of.get(c, c)))
        base_scale[b] = base_scale.get(b, 0.0) + s

    # 各起点代理/合成基准
    org = {}
    for T in ORIGINS:
        mw = mkt_w[mkt_w.index <= T]
        prox_w, prox_yearly, prox_5y = {}, {}, {}
        for bkt, cl in prox_close.items():
            rp = prox_weekly[bkt]
            prox_w[bkt] = (rp - mw.reindex(rp.index)).dropna()
            prox_w[bkt] = prox_w[bkt][prox_w[bkt].index <= T]
            prox_yearly[bkt] = [ES.s_total(cl, T - pd.DateOffset(years=k),
                                           T - pd.DateOffset(years=k - 1))
                                for k in range(1, 6)]
            prox_5y[bkt] = ES.s_ann(cl, T - pd.DateOffset(years=5), T)
        comp_yearly = {t: [ES.s_total(wealth[t], T - pd.DateOffset(years=k),
                                      T - pd.DateOffset(years=k - 1))
                           for k in range(1, 6)] for t in wealth}
        comp_5y = {t: ES.s_ann(wealth[t], T - pd.DateOffset(years=5), T)
                   for t in wealth}
        org[T] = dict(prox_w=prox_w, prox_yearly=prox_yearly, prox_5y=prox_5y,
                      comp_yearly=comp_yearly, comp_5y=comp_5y)
        print(f"[起点 {T.date()}] 代理与合成基准就绪")

    # 单遍 nav：每起点帧
    frames = {T: {} for T in ORIGINS}
    codes = sorted(set(pool["code"]))
    for i, code in enumerate(codes, 1):
        nav = ES._read_nav_pickle(code)
        if nav is None or nav.empty:
            continue
        for T in ORIGINS:
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
            bkt = style_at(nav, T, org[T]["prox_w"], mkt_w)
            if bkt is None:
                continue
            # 前瞻1年绝对收益（T → T+1y，净值须跟到 T+1y±45天）
            nav_T = sub["nav"].iloc[-1]
            fut = nav[nav["date"] <= T + pd.DateOffset(years=1)]
            fwd = None
            if len(fut) and (T + pd.DateOffset(years=1) - fut["date"].iloc[-1]).days <= 45 \
                    and nav_T > 0:
                fwd = float(fut["nav"].iloc[-1] / nav_T - 1)
            frames[T][code] = dict(yearly=yearly, r5=r5, bkt=bkt, fwd=fwd)
        if i % 3000 == 0:
            print(f"  nav {i}/{len(codes)} {time.strftime('%H:%M:%S')}")
    print(f"帧构建完成 {(time.time()-t0)/60:.1f}min")

    # pairs 辅助：key↔code、时点从业
    key_of_code: dict = {}
    codes_of_key: dict = {}
    for r in pairs.itertuples(index=False):
        k = f"{r.mgr}|{r.company}"
        key_of_code[r.code] = k
        codes_of_key.setdefault(k, set()).add(r.code)
    car_now = pairs.groupby(["mgr", "company"])["career_y"].max()
    shift = (TODAY - ORIGINS[0]).days / 365.25  # 占位，逐期算

    # 双十@T 懒计算（经理页+产品nav首跑联网，之后缓存）
    _dual_cache: dict = {}

    def dual_at(k, T, frame_codes):
        ck = (k, T)
        if ck in _dual_cache:
            return _dual_cache[ck]
        car_t = float(car_now.get(tuple(k.split("|", 1)), np.nan))
        car_t = car_t - (TODAY - T).days / 365.25 if car_t == car_t else np.nan
        res = False
        if car_t > ES.DUAL_CAREER_MIN_Y:
            mgr, comp = k.split("|", 1)
            mid = ES._manager_id_of(mgr, frame_codes, f10_all)
            if mid:
                spans = D._manager_products_spans(mid)
                ann = composite_ann_at(spans, T,
                                       lambda c: ES._read_nav_pickle(c)
                                       if ES._read_nav_pickle(c) is not None
                                       else _fetch_nav_safe(c))
                res = ann is not None and ann > ES.DUAL_ANN_MIN
        _dual_cache[ck] = res
        return res

    def _fetch_nav_safe(c):
        try:
            return D.fetch_nav(c)
        except Exception:
            return None

    # ---------------- 逐期：经理层上榜集 ----------------
    panel_rows = []          # 每期每经理一行
    for T in ORIGINS:
        fr = frames[T]
        if not fr:
            continue
        df = pd.DataFrame.from_dict(fr, orient="index")
        df.index.name = "code"
        df["type"] = [type_of.get(c) for c in df.index]
        df["_c3"] = [ES.display_bucket(b) for b in df["bkt"]]
        for j in range(5):
            df[f"back{j}"] = [fr[c]["yearly"][j] for c in df.index]
            df[f"ps{j}"] = rank_pcts_merged(df, f"back{j}")
        ok = df[[f"ps{j}" for j in range(5)]].notna().all(axis=1)
        df = df[ok].copy()
        psm = df[[f"ps{j}" for j in range(5)]].astype(float)
        df["F1"] = psm.mean(axis=1)
        df["F2"] = (psm > 50).sum(axis=1)
        # F3r/F5r（产线 raw：单代理 β=1 / 类型合成）
        o = org[T]
        f3r, f5r = [], []
        for c, row in df.iterrows():
            bkt = row["bkt"]
            if bkt in o["prox_5y"] and o["prox_5y"][bkt] is not None:
                f3r.append(row["r5"] - o["prox_5y"][bkt])
                adj = [row["yearly"][j] - o["prox_yearly"][bkt][j]
                       if o["prox_yearly"][bkt][j] is not None else None
                       for j in range(5)]
            else:
                t = type_of.get(c)
                f3r.append(row["r5"] - o["comp_5y"].get(t)
                           if o["comp_5y"].get(t) is not None else None)
                adj = [row["yearly"][j] - o["comp_yearly"].get(t, [None] * 5)[j]
                       if o["comp_yearly"].get(t, [None] * 5)[j] is not None else None
                       for j in range(5)]
            f5r.append(sum(adj) - max(adj) if all(x is not None for x in adj)
                       else None)
        df["F3r"], df["F5r"] = f3r, f5r
        # F6（任期@T = f10 start→T；从业@T = career_y 平移）
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
            k = key_of_code.get(c)
            cy = float(car_now.get(tuple(k.split("|", 1)), np.nan)) if k else np.nan
            carv[c] = cy - (TODAY - T).days / 365.25 if cy == cy else np.nan
        df["ten"] = pd.Series(ten)
        df["car"] = pd.Series(carv)
        df["F6"] = (df["ten"].rank(pct=True) + df["car"].rank(pct=True)) / 2
        # W3（产线 raw 分位）
        cols = {"F1": "F1", "F2": "F2", "F3": "F3r", "F5": "F5r", "F6": "F6"}
        pr = pd.DataFrame({k: pd.to_numeric(df[v], errors="coerce")
                           .rank(pct=True) * 100 for k, v in cols.items()})
        df["W3"] = sum(pr[k] * W3[k] for k in W3)
        # 考卷标签（产线阈值）
        df["g3s"] = (psm > ES.G3S_PCT).sum(axis=1) >= ES.G3S_MIN_TOP
        df["g4s"] = psm.mean(axis=1) >= ES.G4S_MEAN
        # G1 现值近似 + G2
        df["g1"] = [base_scale.get(ES.base_name(str(name_of.get(c, c))), 0) >= 2.0
                    for c in df.index]
        df["g2"] = pd.to_numeric(df["r5"], errors="coerce") >= ES.DEEP_LOSS_MIN
        df["hard"] = df["g1"] & df["g2"]           # G3仓位不回溯（披露）
        # 同格未来排位（1年，n≥10）
        df["cell"] = list(zip(df["type"], df["bkt"]))
        df["fut_rank"] = np.nan
        for _, g in df.groupby("cell"):
            if len(g) >= 10 and g["fwd"].notna().sum() >= 10:
                df.loc[g.index, "fut_rank"] = (g["fwd"].rank(pct=True) * 100)

        # 经理层
        groups: dict = {}
        for c in df.index:
            k = key_of_code.get(c)
            if k:
                groups.setdefault(k, []).append(c)
        for k, fs in groups.items():
            sub = df.loc[fs]
            car_t = float(car_now.get(tuple(k.split("|", 1)), np.nan))
            car_t = car_t - (TODAY - T).days / 365.25 if car_t == car_t else np.nan
            if not (car_t == car_t) or car_t <= 6:
                continue                          # 通道外（含从业不足）
            chan = "A" if car_t > ES.DUAL_CAREER_MIN_Y else "B"
            score = float(pd.to_numeric(sub["W3"], errors="coerce").mean())
            dual = dual_at(k, T, set(fs)) if chan == "A" else False
            hard = sub[sub["hard"]]
            if chan == "A":
                exam_pass = bool(hard[hard["g3s"]].shape[0]) or dual
                via = "dual" if dual and not hard[hard["g3s"]].shape[0] else (
                    "exam" if hard[hard["g3s"]].shape[0] else "none")
                if dual and hard[hard["g3s"]].shape[0]:
                    via = "dual+exam"
            else:
                exam_pass = bool(hard[hard["g4s"]].shape[0])
                via = "exam" if exam_pass else "none"
            on = exam_pass
            fwd = float(pd.to_numeric(hard["fwd"], errors="coerce").mean()) \
                if len(hard) and hard["fwd"].notna().any() else \
                (float(pd.to_numeric(sub["fwd"], errors="coerce").mean())
                 if sub["fwd"].notna().any() else np.nan)
            fr_ = pd.to_numeric(sub["fut_rank"], errors="coerce")
            fr_ = float(fr_.mean()) if fr_.notna().any() else np.nan
            panel_rows.append(dict(
                origin=str(T.date()), key=k, chan=chan, score=score,
                dual=bool(dual), exam=bool(hard[hard["g3s" if chan == "A" else "g4s"]]
                                            .shape[0]), on=bool(on), via=via,
                n_funds=len(fs), fwd1y=fwd, fut_rank=fr_,
                eligible=True))
        n_on = sum(1 for r in panel_rows if r["origin"] == str(T.date()) and r["on"])
        n_el = sum(1 for r in panel_rows if r["origin"] == str(T.date()))
        print(f"[{T.date()}] 可评估经理 {n_el} · 上榜 {n_on}"
              f"（dual {sum(1 for r in panel_rows if r['origin']==str(T.date()) and r['via'].startswith('dual'))}）")

    panel = pd.DataFrame(panel_rows)
    panel.to_pickle(ROOT / "data" / "eq_persist_panel.pkl")

    # ---------------- 稳定性指标 ----------------
    origins_s = [str(T.date()) for T in ORIGINS]
    piv_on = panel.pivot_table(index="key", columns="origin", values="on",
                               aggfunc="first").reindex(columns=origins_s)
    piv_el = panel.pivot_table(index="key", columns="origin", values="eligible",
                               aggfunc="first").reindex(columns=origins_s)
    on_c = piv_on.fillna(False).sum(axis=1).rename("on_c")
    el_c = piv_el.fillna(False).sum(axis=1).rename("el_c")
    P = (on_c / el_c.replace(0, np.nan)).rename("persistence")
    # 最长连任
    def max_streak(row):
        best = cur = 0
        for v in row:
            cur = cur + 1 if v else 0
            best = max(best, cur)
        return best
    streak = piv_on.fillna(False).apply(max_streak, axis=1).rename("streak")
    last3 = piv_on.fillna(False)[origins_s[-3:]].all(axis=1).rename("last3_on")
    churn = []
    for k in piv_on.index:
        v = list(piv_on.loc[k].fillna(False))
        churn.append(sum(1 for i in range(1, len(v)) if v[i] != v[i - 1]))
    churn = pd.Series(churn, index=piv_on.index, name="transitions")
    metrics = pd.concat([P, streak, last3, churn, on_c, el_c], axis=1)
    metrics["tier"] = pd.cut(P, [-0.01, 0.5, 0.8, 1.01],
                             labels=["波动", "常驻", "核心稳定"])
    metrics.to_pickle(ROOT / "data" / "eq_persist.pkl")

    print("\n================ 持续上榜稳定性 ================")
    print(f"可评估经理（≥1期在池）: {len(metrics)}")
    print("P 分布:", metrics["tier"].value_counts().to_dict())
    core = metrics[(metrics["persistence"] >= 0.8) &
                   (metrics["el_c"] >= 3)].sort_values(
        ["persistence", "streak"], ascending=False)
    print(f"\n核心稳定（P≥80% 且可评估≥3期）: {len(core)} 人，示例前15：")
    for k, r in core.head(15).iterrows():
        print(f"  {k:<28} P={r['persistence']:.0%} 连任{int(r['streak'])}期 "
              f"近3期在榜={bool(r['last3_on'])} 在榜{int(r['on_c'])}/{int(r['el_c'])}期")

    # ---------------- 验证：持续 vs 偶发 vs 从未（防未来函数） ----------------
    print("\n================ 验证：持续上榜者是否更强 ================")
    print("（分组用截至该期的 P；前瞻=该期之后1年绝对收益/同格排位）")
    pooled = {"persistent": [[], []], "occasional": [[], []],
              "never": [[], []], "random": [[], []]}
    for idx in range(3, len(ORIGINS)):            # 需≥3期历史
        T = ORIGINS[idx]
        ts = str(T.date())
        past = origins_s[:idx + 1]
        on_p = piv_on.reindex(columns=past).fillna(False).sum(axis=1)
        el_p = piv_el.reindex(columns=past).fillna(False).sum(axis=1)
        P_up = (on_p / el_p.replace(0, np.nan))
        cur = panel[panel["origin"] == ts].set_index("key")
        cur = cur[cur["fwd1y"].notna()]
        have_hist = el_p >= 3
        g_p = cur.index[(P_up.reindex(cur.index) >= 0.8) & have_hist.reindex(cur.index, fill_value=False)]
        g_n = cur.index[(P_up.reindex(cur.index) == 0) & have_hist.reindex(cur.index, fill_value=False)]
        g_o = cur.index[(P_up.reindex(cur.index) > 0) &
                        (P_up.reindex(cur.index) < 0.8) &
                        have_hist.reindex(cur.index, fill_value=False)]
        rng = np.random.default_rng(idx)
        rnd_keys = rng.choice(list(cur.index), size=min(len(g_p), len(cur.index)),
                              replace=False)
        row = []
        for name, ks in (("persistent", g_p), ("occasional", g_o),
                         ("never", g_n), ("random", list(rnd_keys))):
            if len(ks) == 0:
                row.append(f"{name}: —")
                continue
            f = cur.loc[ks, "fwd1y"]
            r = cur.loc[ks, "fut_rank"]
            pooled[name][0].extend(f.tolist())
            pooled[name][1].extend(r.dropna().tolist())
            row.append(f"{name}: n={len(ks)} fwd1y {f.mean()*100:+.1f}%"
                       f" 排位{r.mean():.1f}")
        print(f"  {ts} | " + " | ".join(row))
    print("\n汇总（各期拼接）:")
    for name in ("persistent", "occasional", "never", "random"):
        f, r = pooled[name]
        if not f:
            print(f"  {name}: —")
            continue
        print(f"  {name:<11} n={len(f):>4} fwd1y均值 {np.mean(f)*100:+.1f}%"
              f" | 同格排位均值 {np.mean(r):.1f}" if r else
              f"  {name:<11} n={len(f):>4} fwd1y均值 {np.mean(f)*100:+.1f}%")

    # ---------------- 断言与披露 ----------------
    assert len(metrics) > 50, "可评估经理过少"
    assert on_c.max() >= min(5, len(ORIGINS)), "无连续在榜者"
    n_last = int(piv_on[origins_s[-1]].fillna(False).sum())
    assert 5 <= n_last <= 400, f"末期上榜数异常: {n_last}"
    print("\n披露：①幸存者偏差（池=现存≥5年）②风格重建不含持仓降级 "
          "③G1规模现值近似 ④G3仓位/机构占比/严格L1不回溯")
    print(f"产出: data/eq_persist.pkl（指标）+ data/eq_persist_panel.pkl（面板）"
          f" | 总耗时 {(time.time()-t0)/60:.1f}min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())