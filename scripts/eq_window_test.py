# -*- coding: utf-8 -*-
"""窗口敏感性-预测力：同一批 tenure≥10y 基金上，5/7/10 年回看窗口的选人规则
对「未来同格平均排位」的预测力对比 + 变长窗口（按任期取窗）规则。

为什么单独测：窗口必须 ≤ 任期（S3 归因），拉长窗口同时改变
(a) 信息量（历史更长更稳）(b) 人群（只剩老将、风格更陈旧）——
本脚本在**同一批人**上对比，隔离出窗口本身的效应。

规则（比例阈值跨窗口可比）：
  MAJ_W     ≥50% 年份跑赢同格中位（5y:3 / 7y:4 / 10y:5）
  STRONG_W  ≥70% 年份跑赢同格中位（5y:4 / 7y:5 / 10y:7）
  AVG70_W   W 年同格平均排位 ≥70
  AVG70_T   按任期取窗（≥5y、封顶10y）平均排位 ≥70（老将吃满历史）
输出：每规则 × 起点：n / mean_fwd / prec / rec（未来同格平均排位≥60=优秀）
运行（仓库根）：python scripts/eq_window_test.py
结论写入 docs/20 §2.4；特征缓存 data/eq_window_feat_<year>.pkl。
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
import scripts.eq_peer_test as P                    # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
NAV_DIR = ROOT / "data" / "cache"
T1 = pd.Timestamp("2026-11-12")
ORIGINS = [pd.Timestamp("2023-11-12"), pd.Timestamp("2024-11-12"),
           pd.Timestamp("2025-11-12")]
FWD_EXC = 60.0
MAXW = 10


def extended_back(pool: pd.DataFrame, o: pd.Timestamp) -> pd.DataFrame:
    """back5..back9（更早的年度收益），缓存复用。"""
    cache = ROOT / f"data/eq_window_feat_{o.year}.pkl"
    if cache.exists():
        try:
            hit = pd.read_pickle(cache)
            if hit.get("ts", 0) and time.time() - hit["ts"] < 30 * 86400:
                print(f"    命中 {cache.name}", flush=True)
                return hit["ext"]
        except Exception:
            pass
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
        rec = {"code": r.code}
        for j in range(5, MAXW):
            a = o - pd.DateOffset(years=j)
            b = o - pd.DateOffset(years=j - 1)
            rec[f"back{j}"] = P.nav_yearly(nav, a, b)
        rows.append(rec)
        if i % 3000 == 0:
            print(f"    ext@{o.year} {i}/{len(pool)}", flush=True)
    ext = pd.DataFrame(rows).set_index("code")
    try:
        pd.to_pickle({"ts": time.time(), "ext": ext}, cache)
    except Exception:
        pass
    return ext


def main():
    t0 = time.time()
    pool = G.load_pool()
    pairs = G.career_pairs(D._manager_em_table(), pool["code"].tolist())
    pairs = pairs[pairs["career_y"].notna()].reset_index(drop=True)

    # 基金级任期（现任经理中最长）与从业通道
    starts = {}
    for c in pairs["code"].unique():
        p = NAV_DIR / f"f10_v3_{c}.pkl"
        if not p.exists():
            continue
        try:
            info = pd.read_pickle(p)
        except Exception:
            continue
        for m in info.get("managers") or []:
            try:
                starts[(m["name"], c)] = pd.Timestamp(m["start"][:10])
            except Exception:
                pass
    pairs["start"] = [starts.get((r.mgr, r.code)) for r in pairs.itertuples()]
    pairs["ten_T1"] = pairs["start"].map(
        lambda s: (T1 - s).days / 365.25
        if s is not None and not pd.isna(s) else float("nan"))
    fund_ten = pairs.groupby("code")["ten_T1"].max()
    fund_career = pairs.groupby("code")["career_y"].max()
    has_f10 = {c for (_m, c) in starts.keys()}   # 有 f10 任期数据的基金

    results = {}
    for o in ORIGINS:
        shift = (T1 - o).days / 365.25
        base = pd.read_pickle(ROOT / f"data/eq_peer_feat_{o.year}.pkl")["feat"]
        ext = extended_back(pool, o)
        feat = base.join(ext, how="left")
        # back5..9 分位（同格口径）
        for j in range(5, MAXW):
            feat[f"ps_{j}"] = P.rank_pcts(feat, f"back{j}",
                                          [["type", "bucket"], ["bucket"],
                                           ["type"]])
        # 人群过滤
        ten_o = fund_ten.reindex(feat.index) - shift
        car_o = fund_career.reindex(feat.index) - shift
        f10_ok = feat.index.isin(has_f10)
        # P10：back0..9 全覆盖 + 分位可用 + 任期≥10 + career通道
        need_cols = [f"back{j}" for j in range(MAXW)] + \
                    [f"ps_{j}" for j in range(MAXW)]
        full = feat[need_cols].notna().all(axis=1)
        p10 = feat.index[full & (ten_o >= 10) & (car_o > 6) & f10_ok]
        # P5：back0..4 + 任期≥5 + career>6
        need5 = [f"back{j}" for j in range(5)] + \
                [f"ps_{j}" for j in range(5)]
        full5 = feat[need5].notna().all(axis=1)
        p5 = feat.index[full5 & (ten_o >= 5) & (car_o > 6) & f10_ok]
        print(f"\n[起点 {o.date()}] P10(可算10y窗口) {len(p10)} | "
              f"P5(可算5y窗口) {len(p5)}", flush=True)

        pf_cols = [c for c in feat.columns if c.startswith("pf_")]
        fwd_mean = feat[pf_cols].mean(axis=1)

        def stats(sel: set, qual: pd.Index, label: str):
            fm = fwd_mean.reindex(list(sel)).dropna()
            if len(fm) < 5:
                return None
            exc = (fwd_mean.reindex(qual) >= FWD_EXC).fillna(False)
            tp = int(exc.reindex(fm.index).sum())
            prec = tp / len(fm)
            rec = tp / int(exc.sum()) if exc.sum() else 0.0
            return dict(label=label, n=len(fm), mean=float(fm.mean()),
                        prec=prec, rec=rec)

        out = []
        # —— P10 上 5/7/10 同台 ——
        for W in (5, 7, 10):
            ps = feat[[f"ps_{j}" for j in range(W)]]
            # MAJ ≥50%：5y→3, 7y→4, 10y→5；STRONG ≥70%：5y→4, 7y→5, 10y→7
            maj = (ps > 50).sum(axis=1) >= int(W * 0.5 + 0.999)
            strong = (ps > 50).sum(axis=1) >= int(W * 0.7 + 0.999)
            avg70 = ps.mean(axis=1) >= 70
            for label, mask in ((f"MAJ{W}", maj), (f"STRONG{W}", strong),
                                (f"AVG70_{W}", avg70)):
                sel = set(feat.index[mask.reindex(feat.index,
                                                  fill_value=False)
                           & feat.index.isin(p10)])
                r = stats(sel, p10, label)
                if r:
                    out.append(r)
        # —— P5 上 AVG70_5 基线 + AVG70_T 变长 ——
        ps5 = feat[[f"ps_{j}" for j in range(5)]]
        sel5 = set(feat.index[(ps5.mean(axis=1) >= 70).reindex(
            feat.index, fill_value=False) & feat.index.isin(p5)])
        r = stats(sel5, p5, "AVG70_5(全池)")
        if r:
            out.append(r)
        # 变长：k = min(10, floor(任期), nav覆盖年数)，要求 ≥5 年；ps_0..ps_{k-1} 均值
        cov = feat[[f"back{j}" for j in range(MAXW)]].notna().sum(axis=1)
        k_ser = pd.Series(
            [int(min(MAXW, max(5, int(t)) if t == t and t is not None
                    else 5, int(c)))
             for t, c in zip(ten_o.fillna(0), cov)],
            index=feat.index)
        avg_t = []
        for idx, k in k_ser.items():
            cols = [f"ps_{j}" for j in range(min(k, MAXW))]
            v = feat.loc[idx, cols]
            avg_t.append(v.mean() if v.notna().all() else float("nan"))
        avg_t = pd.Series(avg_t, index=feat.index)
        sel_t = set(feat.index[(avg_t >= 70).reindex(feat.index,
                                                     fill_value=False)
                     & feat.index.isin(p5)])
        r = stats(sel_t, p5, "AVG70_T(按任期)")
        if r:
            out.append(r)

        results[o] = out
        print(f"  {'规则':<14} {'n':>5} {'mean_fwd':>9} {'prec':>7} {'rec':>7}",
              flush=True)
        for r in out:
            print(f"  {r['label']:<14} {r['n']:>5} {r['mean']:>9.1f} "
                  f"{r['prec']:>7.1%} {r['rec']:>7.1%}", flush=True)

    # ---- 跨起点汇总（P10 规则可比；P5 两规则可比）----
    print("\n" + "=" * 70)
    print("窗口敏感性-跨起点汇总（同一批人上 5/7/10 年对决）")
    labels = []
    for out in results.values():
        for r in out:
            if r["label"] not in labels:
                labels.append(r["label"])
    print(f"  {'规则':<14} {'n':>5} {'mean_fwd':>9} {'prec':>7} {'rec':>7}")
    for lab in labels:
        ns = [r for out in results.values() for r in out if r["label"] == lab]
        if not ns:
            continue
        n = sum(r["n"] for r in ns)
        mean = sum(r["mean"] * r["n"] for r in ns) / n
        # prec/rec 汇总用 tp/fn 重构需要每起点 exc 总数——这里用 n 加权近似
        prec = sum(r["prec"] * r["n"] for r in ns) / n
        rec = sum(r["rec"] for r in ns) / len(ns)
        print(f"  {lab:<14} {n:>5} {mean:>9.1f} {prec:>7.1%} {rec:>7.1%}")
    print(f"\n总耗时 {(time.time()-t0)/60:.1f}min", flush=True)


if __name__ == "__main__":
    main()
