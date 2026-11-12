# -*- coding: utf-8 -*-
"""老将(A,从业>10年) vs 新锐(B, 6~10年)：风险收益画像与未来确定性检验。

用户主观假设（2026-11-12）："老将经验更多 → 未来稳定给出超额的概率更大；
打法更稳、收益稍低没关系，但波动比激进打法小很多。" 本脚本用数据检验三条：
  H1 波动：老将 5年年化波动 / 最大回撤 更低？
  H2 收益：老将 5年年化收益更低（或持平）？
  H3 确定性：老将未来(1年)同格排位优秀率更高、且考试对老将预测力更强？
口径：起点 2025-11-12（特征缓存 eq_peer_feat_2025），四组对比
（合格池/R1/G1s/G4s 各分 A/B），并报告风格构成（混淆因素排查）。
运行（仓库根）：python scripts/eq_veteran_check.py
结论写入 docs/20 §2.4（老将通道设计依据）。
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
ORIGIN = pd.Timestamp("2025-11-12")     # 特征缓存起点（未来1年前瞻已知）


def risk_metrics(code: str, o: pd.Timestamp) -> tuple:
    """5年窗口（截至 o）的年化波动与最大回撤。"""
    p = NAV_DIR / f"nav_{code}.pkl"
    if not p.exists():
        return (np.nan, np.nan)
    try:
        nav = pd.read_pickle(p)
    except Exception:
        return (np.nan, np.nan)
    if "nav" not in nav.columns or nav.empty:
        return (np.nan, np.nan)
    sub = nav[(nav["date"] > o - pd.DateOffset(years=5))
              & (nav["date"] <= o)]
    if len(sub) < 100:
        return (np.nan, np.nan)
    r = sub["nav"].pct_change().dropna()
    vol = float(r.std() * np.sqrt(252)) if len(r) > 50 else np.nan
    navc = sub["nav"] / sub["nav"].iloc[0]
    mdd = float((1 - navc / navc.cummax()).max())
    return (vol, mdd)


def grp_stats(label: str, codes: list, feat: pd.DataFrame,
              fwd_col: str) -> dict:
    if not codes:
        return None
    sub = feat.reindex(codes)
    pf = sub[fwd_col].dropna()
    exc = (pf >= P.FWD_EXC_PCT) if len(pf) else pd.Series(dtype=float)
    return dict(
        g=label, n=len(codes),
        ret=sub["ret5y"].median(),
        vol=sub["vol5y"].median(),
        mdd=sub["mdd5y"].median(),
        fwd0=sub["fwd0"].median() if "fwd0" in sub else np.nan,
        pf0=pf.median() if len(pf) else np.nan,
        exc=exc.mean() if len(exc) else np.nan,
        n_exc=len(pf),
    )


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

    data = P.build_origin(ORIGIN, pool, dict(zip(pool["code"], pool["name"])),
                          wealth)
    feat = data["feat"].copy()
    qual = P.qualified_at(ORIGIN, feat, pairs, f10s)
    sel = P.select_variants(ORIGIN, feat, qual, pairs, f10s)

    # 通道（起点时点）
    shift = (T1 - ORIGIN).days / 365.25
    car = pairs.groupby("code")["career_y"].max() - shift
    chan = car.reindex(feat.index).map(
        lambda c: "A" if (c == c and c > 10)
        else ("B" if (c == c and c > 6) else "out"))

    # 风险指标（5年窗截至起点）
    print(f"[1] 计算合格池 {len(qual)} 只的5年波动/回撤 …", flush=True)
    vols, mdds = {}, {}
    for i, c in enumerate(qual, 1):
        v, m = risk_metrics(c, ORIGIN)
        vols[c], mdds[c] = v, m
        if i % 400 == 0:
            print(f"    {i}/{len(qual)}", flush=True)
    feat["vol5y"] = pd.Series(vols)
    feat["mdd5y"] = pd.Series(mdds)

    pf_cols = [c for c in feat.columns if c.startswith("pf_")]
    fwd_col = pf_cols[0] if pf_cols else None

    # 分组统计
    print("\n[2] 分组画像（起点2025-11-12，5年风险窗 + 未来1年前瞻）", flush=True)
    groups = []
    q_a = [c for c in qual if chan.get(c) == "A"]
    q_b = [c for c in qual if chan.get(c) == "B"]
    groups.append(("合格池·老将A", q_a))
    groups.append(("合格池·新锐B", q_b))
    for rule in ("R1", "G1s", "G4s"):
        s = sel[rule]
        groups.append((f"{rule}·老将A", [c for c in s if chan.get(c) == "A"]))
        groups.append((f"{rule}·新锐B", [c for c in s if chan.get(c) == "B"]))

    rows = [r for r in (grp_stats(l, cs, feat, fwd_col) for l, cs in groups)
            if r]
    df = pd.DataFrame(rows)
    df["ret/vol"] = (df["ret"] / df["vol"]).round(2)
    show = df.copy()
    for col in ("ret", "vol", "mdd", "fwd0", "pf0"):
        show[col] = show[col].map(
            lambda x: f"{x:.1%}" if col in ("ret", "vol", "mdd", "fwd0")
            else f"{x:.1f}")
    show["exc"] = show["exc"].map(lambda x: f"{x:.1%}" if x == x else "-")
    print(show[["g", "n", "ret", "vol", "mdd", "ret/vol", "fwd0", "pf0",
                "exc"]].to_string(index=False))

    # 风格构成（混淆排查）
    print("\n[3] 风格构成（老将 vs 新锐，合格池）——波动差异是否只是风格差异：")
    for lab, cs in (("老将A", q_a), ("新锐B", q_b)):
        bc = feat.reindex(cs)["bucket"].value_counts(normalize=True)
        top = ", ".join(f"{k} {v:.0%}" for k, v in bc.head(5).items())
        print(f"  {lab}: {top}")

    # 同风格内的波动对比（控制风格混淆：取两组共同的主要风格）
    print("\n[4] 同风格内波动对比（控制风格混淆，中位年化波动）：")
    common = (feat.reindex(q_a)["bucket"].value_counts()
              .index[:4])
    print(f"  {'风格':<8}{'老将A波动':>10}{'新锐B波动':>10}{'老将A回撤':>10}"
          f"{'新锐B回撤':>10}")
    for b in common:
        va = feat.reindex(q_a)[feat.reindex(q_a)["bucket"] == b]["vol5y"]
        vb = feat.reindex(q_b)[feat.reindex(q_b)["bucket"] == b]["vol5y"]
        ma = feat.reindex(q_a)[feat.reindex(q_a)["bucket"] == b]["mdd5y"]
        mb = feat.reindex(q_b)[feat.reindex(q_b)["bucket"] == b]["mdd5y"]
        if len(va.dropna()) >= 10 and len(vb.dropna()) >= 10:
            print(f"  {b:<8}{va.median():>10.1%}{vb.median():>10.1%}"
                  f"{ma.median():>10.1%}{mb.median():>10.1%}")

    print(f"\n总耗时 {(time.time()-t0)/60:.1f}min", flush=True)


if __name__ == "__main__":
    main()
