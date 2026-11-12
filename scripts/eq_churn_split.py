# -*- coding: utf-8 -*-
"""检验16：分榜视角的真实换血分解——把 B 通道流失拆成「毕业迁移」vs「真出榜」。

为什么需要：检验14 的 B 通道留存（29-49%）把「从业满10年毕业进老将榜」也算成了流失。
分榜方案下毕业 = 换榜不是出榜，所以要拆开看两张榜各自的诚实换血率。
运行（仓库根）：python scripts/eq_churn_split.py（特征缓存，约1min）
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
import scripts.eq_sustain_check as S                # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
NAV_DIR = ROOT / "data" / "cache"
T1 = pd.Timestamp("2026-11-12")
ORIGINS = [pd.Timestamp("2023-11-12"), pd.Timestamp("2024-11-12"),
           pd.Timestamp("2025-11-12")]
RULES = ["R1", "G1s", "G3s", "G4s"]


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

    store = {}
    for o in ORIGINS:
        data = P.build_origin(o, pool, dict(zip(pool["code"], pool["name"])),
                              wealth)
        feat = data["feat"]
        qual = P.qualified_at(o, feat, pairs, f10s)
        sel = P.select_variants(o, feat, qual, pairs, f10s)
        shift = (T1 - o).days / 365.25
        car = pairs.groupby("code")["career_y"].max() - shift
        chan = car.reindex(feat.index).map(
            lambda c: "A" if (c == c and c > 10)
            else ("B" if (c == c and c > 6) else "out"))
        store[o] = dict(feat=feat, qual=qual, sel=sel, chan=chan)

    print("=" * 76)
    print("检验16 分榜视角换血分解：上期在榜者 → 下期去向")
    print("  留任=同榜留下 | 毕业迁移=B→A且下期仍过考（换榜不出榜）| "
          "真出榜=下期考试未过")
    for i in range(len(ORIGINS) - 1):
        o0, o1 = ORIGINS[i], ORIGINS[i + 1]
        print(f"\n  {o0.date()} → {o1.date()}")
        for rule in RULES:
            for ch in ("A", "B"):
                prev = {c for c in store[o0]["sel"][rule]
                        if store[o0]["chan"].get(c) == ch}
                if not prev:
                    continue
                stay = grad = fail = 0
                for c in prev:
                    c1 = store[o1]["chan"].get(c)
                    passed = c in store[o1]["sel"][rule]
                    if c1 == ch and passed:
                        stay += 1
                    elif ch == "B" and c1 == "A" and passed:
                        grad += 1
                    else:
                        fail += 1
                n = len(prev)
                print(f"    {rule:<5}{ch}: 在榜{n:>4} → 留任{stay:>4}"
                      f"({stay/n:>5.0%}) 毕业迁移{grad:>3} "
                      f"真出榜{fail:>4}({fail/n:>5.0%})")
        # A 榜接收的毕业生
        for rule in RULES:
            grad_in = [c for c in store[o0]["sel"][rule]
                       if store[o0]["chan"].get(c) == "B"
                       and store[o1]["chan"].get(c) == "A"
                       and c in store[o1]["sel"][rule]]
            if grad_in:
                a_prev = len([c for c in store[o0]["sel"][rule]
                              if store[o0]["chan"].get(c) == "A"])
                a_now = len([c for c in store[o1]["sel"][rule]
                             if store[o1]["chan"].get(c) == "A"])
                print(f"    [老将榜接收] {rule}: +{len(grad_in)} 毕业生"
                      f"（A榜 {a_prev}→{a_now}）")
    print(f"\n总耗时 {(time.time()-t0)/60:.1f}min", flush=True)


if __name__ == "__main__":
    main()
