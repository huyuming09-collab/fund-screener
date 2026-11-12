# -*- coding: utf-8 -*-
"""检验9-11：以「逐年跑赢同类」为核心的筛选方法对比 + 诚实换血率。

用户 2026-11-12 下午定方向（docs/20 §2.4 修订）：
  · 目标 = 尽可能捕获「真稳定跑赢同类」的经理（precision + recall 是本分）
  · 不为防换血设计；短输长赢用长窗容纳；不行的如实换（换血率只报告不优化）
  · 推翻前稿"迟滞退出/在榜不复评"——那是为稳定开的保护

方法集（同一合格池上对比）：
  R1  现行闸（A: 同格5年收益分位>10% / B: ≥同格 p80）
  R6  前稿三闸（逐年≥3年正超额 ∧ 去最佳年仍正 ∧ 5年绝对超额>0）
  G1t/G2t  逐年跑赢【类型】中位 ≥3 / ≥4 年
  G1s/G2s  逐年跑赢【类型×风格同格】中位 ≥3 / ≥4 年
  G3s      逐年进同格前25% ≥3 年
  G4s      5年同格平均排位 ≥70
  G6       G1s + 5年绝对超额>0（绝对是否带来增量）

验证：多起点伪前瞻 O = {2023,2024,2025}-11-12 → 未来 3/2/1 年
  · 捕获：未来同格平均排位 ≥60 定义「未来优秀」→ precision / recall / mean_fwd
  · 诚实换血：同一方法在相邻起点的名单留存 |Lo ∩ Lo'| / |Lo|
  · 起点池随年份增长（S4 到龄、资历到龄），留存为保守口径

运行（仓库根）：python scripts/eq_peer_test.py
结论写入 docs/20 §2.4；特征缓存 data/eq_peer_feat_<year>.pkl 供复跑。
"""
from __future__ import annotations

import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import pandas as pd                                 # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from fundscorer import data as D                    # noqa: E402
from fundscorer import eqgate as G                  # noqa: E402
import scripts.eq_sustain_check as S                # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
NAV_DIR = ROOT / "data" / "cache"
T1 = pd.Timestamp("2026-11-12")
ORIGINS = [pd.Timestamp("2023-11-12"), pd.Timestamp("2024-11-12"),
           pd.Timestamp("2025-11-12")]
FWD_EXC_PCT = 60.0          # 未来同格平均排位 ≥60 定义"未来优秀"
VARIANTS = ["R1", "R6", "G1t", "G2t", "G1s", "G2s", "G3s", "G4s", "G6"]


# ---------------------------------------------------------------- 基础工具
def nav_yearly(nav: pd.DataFrame, a: pd.Timestamp,
               b: pd.Timestamp) -> float | None:
    sub = nav[(nav["date"] > a) & (nav["date"] <= b)]
    if len(sub) < 180 or (sub["date"].iloc[-1] - sub["date"].iloc[0]).days < 300:
        return None
    return float(sub["nav"].iloc[-1] / sub["nav"].iloc[0] - 1)


def nav_ann_to(nav: pd.DataFrame, o: pd.Timestamp, years: int) -> float | None:
    navT = nav[nav["date"] <= o]
    if navT.empty:
        return None
    end = navT["date"].max()
    sub = navT[navT["date"] > end - pd.DateOffset(years=years)]
    if len(sub) < 2:
        return None
    days = (sub["date"].iloc[-1] - sub["date"].iloc[0]).days
    if days <= 0:
        return None
    return float((sub["nav"].iloc[-1] / sub["nav"].iloc[0])
                 ** (365.0 / days) - 1)


def rank_pcts(df: pd.DataFrame, col: str, key_sets) -> pd.Series:
    """在 key_sets 的层级上依次回填分位（n≥10 才算），与 eqgate add_pctl 同口径。"""
    out = pd.Series(float("nan"), index=df.index, dtype="float64")
    for keys in key_sets:
        mask = out.isna() & df[col].notna()
        for _k, g in df[mask].groupby(keys):
            if len(g) >= 10:
                out.loc[g.index] = (g[col].rank(pct=True) * 100).round(1)
    return out


# ---------------------------------------------------------------- 风格（按起点）
def styles_at_origin(codes: list[str], o: pd.Timestamp) -> dict:
    cache = ROOT / "data" / f"eq_peer_style_{o.strftime('%Y%m%d')}.pkl"
    if cache.exists():
        try:
            hit = pd.read_pickle(cache)
            if time.time() - hit.get("ts", 0) < 30 * 86400:
                return hit["styles"]
        except Exception:
            pass
    mkt = G._load_close(G.STYLE_MARKET)
    mkt_w = mkt.resample("W-FRI").last().pct_change().dropna()
    mkt_w = mkt_w[mkt_w.index <= o]
    prox = {}
    for nm in G.STYLE_PROXIES:
        s = G._load_close(nm).resample("W-FRI").last().pct_change().dropna()
        s = s[s.index <= o]
        prox[G.STYLE_PROXIES[nm]] = (s - mkt_w).reindex(mkt_w.index)
    out = {}
    for i, c in enumerate(codes, 1):
        p = NAV_DIR / f"nav_{c}.pkl"
        if not p.exists():
            continue
        try:
            nav = pd.read_pickle(p)
        except Exception:
            continue
        if "nav" not in nav.columns or nav.empty:
            continue
        navT = nav[nav["date"] <= o]
        if navT.empty:
            continue
        w = navT.set_index("date")["nav"].resample("W-FRI").last().dropna()
        fw = w.pct_change().dropna()
        fr_ex = (fw - mkt_w.reindex(fw.index)).dropna()
        style = None
        if len(fr_ex) >= 100:
            corrs = {}
            for st, pex in prox.items():
                both = pd.concat([fr_ex, pex], axis=1, sort=False).dropna()
                if len(both) >= 100:
                    corrs[st] = both.iloc[:, 0].corr(both.iloc[:, 1])
            if corrs:
                best = max(corrs, key=corrs.get)
                if corrs[best] >= G.STYLE_MIN_CORR:
                    style = best
        out[c] = style if style else None   # None → 调用方回退名字桶
        if i % 2000 == 0:
            print(f"    style@{o.year} {i}/{len(codes)}", flush=True)
    # None 的用名字桶兜底（由调用方填）
    try:
        pd.to_pickle({"ts": time.time(), "styles": out}, cache)
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- 单起点特征
def build_origin(o: pd.Timestamp, pool: pd.DataFrame, name_map: dict,
                 wealth: dict) -> dict:
    cache = ROOT / f"data/eq_peer_feat_{o.strftime('%Y%m%d')}.pkl"
    if cache.exists():
        try:
            hit = pd.read_pickle(cache)
            if hit.get("ts", 0) and time.time() - hit["ts"] < 30 * 86400:
                print(f"    命中特征缓存 {cache.name}", flush=True)
                return hit
        except Exception:
            pass
    codes = pool["code"].tolist()
    styles = styles_at_origin(codes, o)

    back_win = [(o - pd.DateOffset(years=k), o - pd.DateOffset(years=k - 1))
                for k in range(1, 6)]                     # [0]=最近一年
    fwd_end = T1
    fwd_win = []
    k = 1
    while True:
        a = o + pd.DateOffset(years=k - 1)
        b = o + pd.DateOffset(years=k)
        if a >= fwd_end:
            break
        fwd_win.append((a, min(b, fwd_end)))
        k += 1
        if k > 4:
            break

    rows = []
    for i, r in enumerate(pool.itertuples(index=False), 1):
        p = NAV_DIR / f"nav_{r.code}.pkl"
        if not p.exists():
            continue
        try:
            nav = pd.read_pickle(p)
        except Exception:
            continue
        if "nav" not in nav.columns or nav.empty:
            continue
        navT = nav[nav["date"] <= o]
        if navT.empty:
            continue
        span = (navT["date"].max() - navT["date"].min()).days / 365.25
        if span < G.NAV_MIN_Y:
            continue
        sty = styles.get(r.code) or G.style_bucket(r.name)
        rec = dict(code=r.code, name=r.name, type=r.type_text, bucket=sty,
                   ret5y=nav_ann_to(nav, o, 5))
        rec["back"] = [nav_yearly(nav, a, b) for a, b in back_win]
        rec["fwd"] = [nav_yearly(nav, a, b) for a, b in fwd_win]
        rows.append(rec)
        if i % 3000 == 0:
            print(f"    feat@{o.year} {i}/{len(pool)} "
                  f"{time.strftime('%H:%M:%S')}", flush=True)

    feat = pd.DataFrame(rows).set_index("code")
    # 展开逐年列表 → back0..4 / fwd0..n 列（nav_yearly 保证定长，None 表示该年数据不足）
    bdf = feat["back"].apply(pd.Series)
    bdf.columns = [f"back{j}" for j in range(bdf.shape[1])]
    fdf = feat["fwd"].apply(pd.Series)
    if fdf.shape[1]:
        fdf.columns = [f"fwd{j}" for j in range(fdf.shape[1])]
    feat = pd.concat([feat.drop(columns=["back", "fwd"]), bdf, fdf], axis=1)
    # 各年分位：类型口径 + 同格口径（回填层级同 eqgate）
    for j in range(5):
        col = f"back{j}"
        feat[f"pt_{j}"] = rank_pcts(feat, col, [["type"]])
        feat[f"ps_{j}"] = rank_pcts(feat, col,
                                    [["type", "bucket"], ["bucket"], ["type"]])
    for j in range(len(fwd_win)):
        col = f"fwd{j}"
        feat[f"pf_{j}"] = rank_pcts(feat, col,
                                    [["type", "bucket"], ["bucket"], ["type"]])
    # 5年收益分位（R1 的 B 通道口径）
    feat["pctl5"] = rank_pcts(feat, "ret5y",
                              [["type", "bucket"], ["bucket"], ["type"]])
    # 合成基准逐年超额（R6 口径）：基准按 (type, 年) 预计算，避免逐行重算
    comp_year = {}
    for j in range(5):
        a, b = back_win[j]
        for t in feat["type"].unique():
            comp_year[(t, j)] = S.comp_total(wealth[t], a, b)
    for j in range(5):
        # itertuples 返回 namedtuple → 字段用 getattr 属性访问
        feat[f"cexc_{j}"] = [
            (None if (getattr(r, f"back{j}") is None
                      or comp_year.get((r.type, j)) is None)
             else getattr(r, f"back{j}") - comp_year[(r.type, j)])
            for r in feat.itertuples()]
    comp5 = feat.apply(
        lambda r: (r["ret5y"] is not None
                   and not pd.isna(r["ret5y"])
                   and r["ret5y"] > (S.comp_ann(wealth[r["type"]],
                                                o - pd.DateOffset(years=5), o)
                                     or 1e9)), axis=1)
    feat["R5"] = comp5
    out = {"ts": time.time(), "feat": feat, "back_win": back_win,
           "fwd_win": fwd_win}
    try:
        pd.to_pickle(out, cache)
        print(f"    特征已缓存 {cache.name}", flush=True)
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- 合格池 + 选人
def qualified_at(o: pd.Timestamp, feat: pd.DataFrame, pairs: pd.DataFrame,
                 f10s: dict) -> set:
    """S4(已在feat) + career通道 + S3(任期≥5y) + f10可用。"""
    shift = (T1 - o).days / 365.25
    ok = set()
    for row in pairs.itertuples(index=False):
        if G.channel(row.career_y - shift) == "out":
            continue
        if row.code not in feat.index or row.code in ok:
            continue
        info = f10s.get(row.code)
        if not info:
            continue
        mg = next((m for m in (info.get("managers") or [])
                   if m.get("name") == row.mgr), None)
        if mg is None:
            continue
        try:
            start = pd.Timestamp((mg.get("start") or "")[:10])
        except Exception:
            continue
        if start > o - pd.Timedelta(days=G.TENURE_MIN_D):
            continue
        ok.add(row.code)
    return ok


def select_variants(o: pd.Timestamp, feat: pd.DataFrame, qual: set,
                    pairs: pd.DataFrame, f10s: dict) -> dict:
    """返回 {variant: set(基金code)}。R1/R6 含通道逻辑，G* 为纯排位/超额测试。"""
    shift = (T1 - o).days / 365.25
    # 每基金通道（取最强：任一 A-pair → A）
    chan = {}
    for row in pairs.itertuples(index=False):
        if row.code not in qual:
            continue
        c = G.channel(row.career_y - shift)
        if c == "A":
            chan[row.code] = "A"
        elif c == "B" and chan.get(row.code) != "A":
            chan[row.code] = "B"

    # R1：A 走 pctl5>10 底线；B 走 5年收益≥同格 p80（格阈值回退同 eqgate）
    thr5 = {}
    s4 = feat.loc[feat["ret5y"].notna()]
    for (t, b), g in s4.groupby(["type", "bucket"]):
        if len(g) >= 10:
            thr5[("cell", t, b)] = g["ret5y"].quantile(G.TOP_PCT)
    for b, g in s4.groupby("bucket"):
        if len(g) >= 10:
            thr5[("bucket", b)] = g["ret5y"].quantile(G.TOP_PCT)
    for t, g in s4.groupby("type"):
        if len(g) >= 10:
            thr5[("type", t)] = g["ret5y"].quantile(G.TOP_PCT)

    def thr_of(t, b):
        return (thr5.get(("cell", t, b)) or thr5.get(("bucket", b))
                or thr5.get(("type", t)))

    sel = {v: set() for v in VARIANTS}
    for c in qual:
        f = feat.loc[c]
        ch = chan.get(c)
        # ---- R1 ----
        if ch == "A":
            pc = f["pctl5"]
            r1_ok = not (pd.isna(pc) or pc <= G.A_FLOOR_PCT)
        elif ch == "B":
            tv = thr_of(f["type"], f["bucket"])
            r1_ok = (tv is not None and not pd.isna(f["ret5y"])
                     and f["ret5y"] >= tv)
        else:
            r1_ok = False
        # ---- R6（在 R1 上叠三闸）----
        backs = [f[f"back{j}"] for j in range(5)]
        cexcs = [f[f"cexc_{j}"] for j in range(5)]
        yearly_ok = all(x is not None for x in backs)
        exc_ok = (all(x is not None for x in cexcs)
                  and sum(1 for x in cexcs if x > 0) >= 3)
        if yearly_ok and exc_ok:
            best = max(cexcs)
            r4_ok = (sum(cexcs) - best) > 0
        else:
            r4_ok = False
        r6_ok = r1_ok and exc_ok and r4_ok and bool(f["R5"])
        # ---- G*：逐年排位 ----
        pt = [f[f"pt_{j}"] for j in range(5)]
        ps = [f[f"ps_{j}"] for j in range(5)]
        pt_ok = all(not pd.isna(x) for x in pt)
        ps_ok = all(not pd.isna(x) for x in ps)
        g1t = pt_ok and sum(1 for x in pt if x > 50) >= 3
        g2t = pt_ok and sum(1 for x in pt if x > 50) >= 4
        g1s = ps_ok and sum(1 for x in ps if x > 50) >= 3
        g2s = ps_ok and sum(1 for x in ps if x > 50) >= 4
        g3s = ps_ok and sum(1 for x in ps if x > 75) >= 3
        g4s = ps_ok and (sum(ps) / 5) >= 70
        g6 = g1s and bool(f["R5"])
        for v, ok in (("R1", r1_ok), ("R6", r6_ok), ("G1t", g1t),
                      ("G2t", g2t), ("G1s", g1s), ("G2s", g2s),
                      ("G3s", g3s), ("G4s", g4s), ("G6", g6)):
            if ok:
                sel[v].add(c)
    return sel


# ---------------------------------------------------------------- main
def main():
    t0 = time.time()
    print("[1] 载入池/经理对/基准 …", flush=True)
    pool = G.load_pool()
    name_map = dict(zip(pool["code"], pool["name"]))
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
    print(f"    池 {len(pool)} / 对 {len(pairs)} / f10 {len(f10s)}",
          flush=True)

    origins_res = {}
    for o in ORIGINS:
        print(f"\n[2] 起点 {o.date()} 特征 …", flush=True)
        data = build_origin(o, pool, name_map, wealth)
        feat = data["feat"]
        print(f"    S4人群 {len(feat)}（{len(data['fwd_win'])} 个前瞻年）",
              flush=True)
        qual = qualified_at(o, feat, pairs, f10s)
        print(f"    合格池 {len(qual)}", flush=True)
        sel = select_variants(o, feat, qual, pairs, f10s)
        origins_res[o] = dict(feat=feat, qual=qual, sel=sel,
                              fwd_win=data["fwd_win"])

    # ---- 检验9：多起点捕获力 ----
    print("\n" + "=" * 70)
    print(f"检验9 捕获未来优秀者（未来同格平均排位≥{FWD_EXC_PCT:.0f}=优秀）")
    print("（每个起点：n=入选数 / mean_fwd=入选者未来平均同格排位 / "
          "prec=入选中未来优秀占比 / rec=未来优秀者中被捕获占比）")
    agg = {v: dict(tp=0, fp=0, fn=0, fwd_sum=0.0, n=0) for v in VARIANTS}
    for o, R in origins_res.items():
        feat, qual = R["feat"], R["qual"]
        nfwd = len(R["fwd_win"])
        pf_cols = [f"pf_{j}" for j in range(nfwd)]
        fwd_mean = feat[pf_cols].mean(axis=1)
        is_exc = fwd_mean >= FWD_EXC_PCT
        print(f"\n  起点 {o.date()}（未来 {nfwd} 年，合格池 {len(qual)}，"
              f"未来优秀率 {is_exc.reindex(list(qual)).mean():.1%}）")
        base = fwd_mean.reindex(list(qual)).dropna()
        print(f"    [基线] 合格池整体 mean_fwd={base.mean():.1f}")
        for v in VARIANTS:
            sel = R["sel"][v]
            fm = fwd_mean.reindex(list(sel)).dropna()
            exc_q = is_exc.reindex(list(qual)).fillna(False)
            if len(fm):
                tp = int((exc_q.reindex(fm.index)).sum())
                prec = tp / len(fm)
                rec = tp / int(exc_q.sum()) if exc_q.sum() else 0.0
                print(f"    {v:>3}: n={len(sel):>4} mean_fwd={fm.mean():6.1f} "
                      f"prec={prec:.1%} rec={rec:.1%}")
                a = agg[v]
                a["tp"] += tp
                a["fp"] += len(fm) - tp
                a["fwd_sum"] += float(fm.sum())
                a["n"] += len(fm)
                a["fn"] = a.get("fn", 0)   # 占位，下面统一补
            else:
                print(f"    {v:>3}: n={len(sel):>4}（无前瞻数据）")
        # fn 依赖每个起点的未来优秀总数 → 存下
        for v in VARIANTS:
            sel = R["sel"][v]
            fm = fwd_mean.reindex(list(sel)).dropna()
            exc_q = is_exc.reindex(list(qual)).fillna(False)
            tp = int(exc_q.reindex(fm.index).sum())
            R.setdefault("fn", {})[v] = int(exc_q.sum()) - tp

    print("\n  —— 跨起点汇总 ——")
    print(f"  {'方法':>4} {'n':>5} {'mean_fwd':>9} {'prec':>7} {'rec':>7}")
    for v in VARIANTS:
        a = agg[v]
        if a["n"] == 0:
            continue
        fn = sum(R["fn"][v] for R in origins_res.values())
        prec = a["tp"] / a["n"]
        rec = a["tp"] / (a["tp"] + fn) if (a["tp"] + fn) else 0
        print(f"  {v:>4} {a['n']:>5} {a['fwd_sum']/a['n']:>9.1f} "
              f"{prec:>7.1%} {rec:>7.1%}")

    # ---- 检验10：诚实换血（同方法相邻起点名单留存）----
    print("\n" + "=" * 70)
    print("检验10 诚实换血率（同一方法在相邻起点的名单留存，含池增长因素）")
    os_ = ORIGINS
    for i in range(len(os_) - 1):
        o0, o1 = os_[i], os_[i + 1]
        s0, s1 = origins_res[o0]["sel"], origins_res[o1]["sel"]
        print(f"\n  {o0.date()} → {o1.date()}")
        for v in VARIANTS:
            a, b = s0[v], s1[v]
            if not a:
                continue
            ret = len(a & b) / len(a)
            print(f"    {v:>3}: 留存 {ret:6.1%} ({len(a & b)}/{len(a)}) "
                  f"| {o1.year}名单 {len(b)}")

    # ---- 检验11：各方法名单里"未来平均排位"分位（谁选出的人更拔尖）----
    print("\n" + "=" * 70)
    print("检验11 入选者未来同格排位分布（p25/p50/p75，越高=选出的人越持续拔尖）")
    for o, R in origins_res.items():
        feat = R["feat"]
        pf_cols = [f"pf_{j}" for j in range(len(R["fwd_win"]))]
        fwd_mean = feat[pf_cols].mean(axis=1)
        print(f"\n  起点 {o.date()}:")
        for v in VARIANTS:
            fm = fwd_mean.reindex(list(R["sel"][v])).dropna()
            if len(fm) >= 5:
                q = fm.quantile([.25, .5, .75])
                print(f"    {v:>3}: p25={q.loc[.25]:5.1f} p50={q.loc[.5]:5.1f} "
                      f"p75={q.loc[.75]:5.1f}")

    print(f"\n总耗时 {(time.time()-t0)/60:.1f}min", flush=True)


if __name__ == "__main__":
    main()
