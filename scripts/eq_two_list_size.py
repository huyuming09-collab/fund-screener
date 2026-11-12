# -*- coding: utf-8 -*-
"""两榜规模测算：按已拍板配置（老将榜=A通道+G3s中档，新锐榜=B通道+G4s严档）
输出 3 起点的基金/经理规模 + 毕业迁移量，供展示量拍板。
运行（仓库根）：python scripts/eq_two_list_size.py（特征缓存，约1min）
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

    # (mgr, company) → career（经理级通道）
    mgr_career = pairs.groupby(["mgr", "company"])["career_y"].max()

    print("=" * 74)
    print("两榜规模（老将榜=A通道+G3s中档考 / 新锐榜=B通道+G4s严档考）")
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

        vet_f = {c for c in sel["G3s"] if chan.get(c) == "A"}
        new_f = {c for c in sel["G4s"] if chan.get(c) == "B"}
        # 经理：该基金的在考 pair（mgr career 与基金通道一致）
        def mgrs_of(codes, ch):
            out = set()
            for row in pairs[pairs["code"].isin(codes)].itertuples():
                cy = row.career_y - shift
                if (ch == "A" and cy > 10) or (ch == "B" and 6 < cy <= 10):
                    out.add((row.mgr, row.company))
            return out
        vet_m = mgrs_of(vet_f, "A")
        new_m = mgrs_of(new_f, "B")
        # 毕业迁移：新锐榜成员下期若过老将卷且通道转A
        print(f"  {o.date()}: 老将榜 基金{len(vet_f):>3}/经理{len(vet_m):>3} | "
              f"新锐榜 基金{len(new_f):>3}/经理{len(new_m):>3} | "
              f"合计经理 {len(vet_m | new_m):>3}")

    # 毕业迁移量（新锐榜 → 下期老将卷）
    print("\n毕业迁移（新锐榜成员满10年 → 下期按老将G3s卷重考）:")
    for i in range(len(ORIGINS) - 1):
        o0, o1 = ORIGINS[i], ORIGINS[i + 1]
        d0 = P.build_origin(o0, pool, dict(zip(pool["code"], pool["name"])),
                            wealth)
        d1 = P.build_origin(o1, pool, dict(zip(pool["code"], pool["name"])),
                            wealth)
        q0 = P.qualified_at(o0, d0["feat"], pairs, f10s)
        s0 = P.select_variants(o0, d0["feat"], q0, pairs, f10s)
        q1 = P.qualified_at(o1, d1["feat"], pairs, f10s)
        s1 = P.select_variants(o1, d1["feat"], q1, pairs, f10s)
        sh = (T1 - o0).days / 365.25
        c0 = pairs.groupby("code")["career_y"].max() - sh
        sh1 = (T1 - o1).days / 365.25
        c1 = pairs.groupby("code")["career_y"].max() - sh1
        grads = [c for c in s0["G4s"]
                 if (c0.get(c, 0) > 6 and c0.get(c, 0) <= 10)
                 and c1.get(c, 0) > 10]
        pass_vet = [c for c in grads if c in s1["G3s"]]
        print(f"  {o0.date()}→{o1.year}: 新锐榜毕业 {len(grads)} 只，"
              f"其中过老将卷 {len(pass_vet)} "
              f"({len(pass_vet)/max(len(grads),1):.0%}) → 老将榜净增")
    print(f"\n总耗时 {(time.time()-t0)/60:.1f}min", flush=True)


if __name__ == "__main__":
    main()
