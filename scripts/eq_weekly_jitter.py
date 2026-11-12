# -*- coding: utf-8 -*-
"""检验17：周滚动重算的边界抖动——用户拍板"随周更滚动重算"后的体感量化。

对比 2025-11-12（基准）与 2025-11-19（一周后）两期名单：
  · G3s/G4s（两榜卷）的一周留存 → 压线进出的抖动幅度
  · R1（现闸）对照
  · 新进/离开人数 → "只标新进"标签每周会亮多少
风格缓存按月键命中（202511），特征需按新日期重算（约1-2min）。
运行（仓库根）：python scripts/eq_weekly_jitter.py
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
DATES = [pd.Timestamp("2025-11-12"), pd.Timestamp("2025-11-19"),
         pd.Timestamp("2025-12-12")]       # 1周 / 1月 后


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
    for o in DATES:
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
        # 两榜名单（已拍板配置）
        vet = {c for c in sel["G3s"] if chan.get(c) == "A"}
        new = {c for c in sel["G4s"] if chan.get(c) == "B"}
        store[o] = dict(sel=sel, vet=vet, new=new, qual=qual)
        print(f"    {o.date()}: 老将榜 {len(vet)} | 新锐榜 {len(new)} | "
              f"合格池 {len(qual)}", flush=True)

    base = DATES[0]
    print("\n" + "=" * 72)
    print("检验17 周/月滚动抖动（基准 2025-11-12 → 后续时点的名单留存）")
    for o in DATES[1:]:
        print(f"\n  {base.date()} → {o.date()}"
              f"（+{(o-base).days} 天）:")
        for lab, key in (("老将榜(G3s∩A)", "vet"), ("新锐榜(G4s∩B)", "new"),
                         ("对照 R1", "sel")):
            a = store[base][key] if key != "sel" else store[base]["sel"]["R1"]
            b = store[o][key] if key != "sel" else store[o]["sel"]["R1"]
            if not a:
                continue
            keep = a & b
            print(f"    {lab:<16} 留存 {len(keep)/len(a):6.1%} "
                  f"({len(keep)}/{len(a)}) | 离开 {len(a-b):>3} | "
                  f"新进 {len(b-a):>3}")
    print(f"\n总耗时 {(time.time()-t0)/60:.1f}min", flush=True)


if __name__ == "__main__":
    main()
