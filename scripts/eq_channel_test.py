# -*- coding: utf-8 -*-
"""新锐/老将分开筛选方案评估——通道分解检验（检验13-15）。

背景：用户提出评估"新锐(B,从业6~10年)与老将(A,>10年)分开筛选"方案。
本脚本用 3 起点缓存特征回答三个决定性问题：
  检验13 考试对两组是否同样有效：各规则 × A/B 的 n/mean_fwd/prec/rec
          → 若精确率相近 = 同一张卷子即可，分开只是展示选择；
            若差异大 = 分开阈值有数据依据
  检验14 两组先天差异与换血：合格池未来优秀率（A/B 基线）+ 相邻起点留存
          → 决定两张榜各自的"诚实换血率"会长什么样
  检验15 边界流动：每年有多少新锐从业满10年"毕业"进老将
          → 分榜方案的边界迁移问题规模

运行（仓库根）：python scripts/eq_channel_test.py（特征缓存命中，约1-2min）
结论写入 docs/20 §2.4。
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
RULES = ["R1", "R6", "G1s", "G3s", "G4s"]


def main():
    t0 = time.time()
    pool = G.load_pool()
    pairs = G.career_pairs(D._manager_em_table(), pool["code"].tolist())
    pairs = pairs[pairs["career_y"].notna()].reset_index(drop=True)
    import scripts.eq_sustain_check as S
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

    # 每起点的特征/合格池/选人（复用 eq_peer_test，缓存命中）
    store = {}
    for o in ORIGINS:
        data = P.build_origin(o, pool, dict(zip(pool["code"], pool["name"])),
                              wealth)
        feat = data["feat"]
        qual = P.qualified_at(o, feat, pairs, f10s)
        sel = P.select_variants(o, feat, qual, pairs, f10s)
        # 每基金通道（起点时点）：career_max(o) > 10 → A
        shift = (T1 - o).days / 365.25
        car = pairs.groupby("code")["career_y"].max() - shift
        chan = car.reindex(feat.index).map(
            lambda c: "A" if (c == c and c > 10)
            else ("B" if (c == c and c > 6) else "out"))
        pf_cols = [c for c in feat.columns if c.startswith("pf_")]
        fwd_mean = feat[pf_cols].mean(axis=1)
        store[o] = dict(feat=feat, qual=qual, sel=sel, chan=chan,
                        fwd=fwd_mean, car=car)

    # ---- 检验13：考试对两组是否同样有效（跨起点汇总）----
    print("=" * 78)
    print("检验13 考试×通道：各规则在 A/B 上的捕获力（3起点汇总）")
    print(f"{'规则':<7}{'通道':<4}{'n':>5}{'mean_fwd':>10}{'prec':>8}{'rec':>8}")
    for rule in RULES:
        for ch in ("A", "B"):
            tp = fp = fn = n = 0
            fwd_sum = 0.0
            for o, R in store.items():
                sel = [c for c in R["sel"][rule]
                       if R["chan"].get(c) == ch]
                fm = R["fwd"].reindex(sel).dropna()
                if not len(fm):
                    continue
                exc = (R["fwd"].reindex(R["qual"]) >= P.FWD_EXC_PCT).fillna(False)
                tpp = int(exc.reindex(fm.index).sum())
                tp += tpp
                fp += len(fm) - tpp
                fn += int(exc.reindex(
                    [c for c in R["qual"] if R["chan"].get(c) == ch]
                ).sum()) - tpp
                n += len(fm)
                fwd_sum += float(fm.sum())
            if n:
                prec = tp / n
                rec = tp / (tp + fn) if (tp + fn) else 0.0
                print(f"{rule:<7}{ch:<4}{n:>5}{fwd_sum/n:>10.1f}"
                      f"{prec:>8.1%}{rec:>8.1%}")
        print()

    # ---- 检验14：两组基线（合格池未来优秀率）+ 换血 ----
    print("=" * 78)
    print("检验14 两组先天差异：合格池未来优秀率基线 & 换血率")
    for o, R in store.items():
        for ch in ("A", "B"):
            q = [c for c in R["qual"] if R["chan"].get(c) == ch]
            exc = (R["fwd"].reindex(q) >= P.FWD_EXC_PCT).fillna(False)
            print(f"  {o.date()} {ch}通道 合格池 {len(q):>4} "
                  f"未来优秀率 {exc.mean():>6.1%}")
    print("\n  相邻起点名单留存（分通道）:")
    for i in range(len(ORIGINS) - 1):
        o0, o1 = ORIGINS[i], ORIGINS[i + 1]
        for ch in ("A", "B"):
            parts = []
            for rule in RULES:
                a = {c for c in store[o0]["sel"][rule]
                     if store[o0]["chan"].get(c) == ch}
                if not a:
                    continue
                b = {c for c in store[o1]["sel"][rule]
                     if store[o1]["chan"].get(c) == ch}
                parts.append(f"{rule} {len(a & b)/len(a):.0%}")
            print(f"  {o0.date()}→{o1.year} {ch}: " + " | ".join(parts))

    # ---- 检验15：边界流动（新锐毕业进老将）----
    print("\n" + "=" * 78)
    print("检验15 边界流动：从业满10年'毕业'规模（分榜方案的迁移问题）")
    for i in range(len(ORIGINS) - 1):
        o0, o1 = ORIGINS[i], ORIGINS[i + 1]
        c0, c1 = store[o0]["chan"], store[o1]["chan"]
        grad = [c for c in c0.index
                if c0.get(c) == "B" and c1.get(c) == "A"]
        # 毕业生里当时在各榜的
        on_b = [c for c in grad if c in store[o0]["sel"]["R1"]]
        on_g4 = [c for c in grad if c in store[o0]["sel"]["G4s"]]
        print(f"  {o0.date()}→{o1.year}: B→A 毕业 {len(grad)} 只基金"
              f"（其中当时在 R1 榜 {len(on_b)}、G4s 榜 {len(on_g4)}）")

    # 毕业生的未来表现（毕业后是否仍优秀 → 老将榜准入是否可直接沿用）
    print("\n  毕业生 vs 留任新锐的未来优秀率（毕业 = 通道标签变，考试不变）:")
    for i in range(len(ORIGINS) - 1):
        o0, o1 = ORIGINS[i], ORIGINS[i + 1]
        c0, c1 = store[o0]["chan"], store[o1]["chan"]
        grad = [c for c in c0.index
                if c0.get(c) == "B" and c1.get(c) == "A"]
        stay = [c for c in c0.index
                if c0.get(c) == "B" and c1.get(c) == "B"]
        for lab, grp in (("毕业生", grad), ("留任新锐", stay)):
            if not grp:
                continue
            exc = (store[o1]["fwd"].reindex(grp) >= P.FWD_EXC_PCT).fillna(False)
            print(f"    {o0.date()} {lab}: n={len(grp)} "
                  f"毕业后未来优秀率={exc.mean():.1%}")

    print(f"\n总耗时 {(time.time()-t0)/60:.1f}min", flush=True)


if __name__ == "__main__":
    main()
