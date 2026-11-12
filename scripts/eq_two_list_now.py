# -*- coding: utf-8 -*-
"""当期（2026-11）两榜规模：老将榜=A通道∩G3s中档 / 新锐榜=B通道∩G4s严档。

用 eq_sustain_feat.pkl 的 T1 特征（含逐年收益）重算逐年同格分位后套卷，
补齐 eq_peer_test 只到 2025 起点的缺口——给"人太多就严/人少就宽"提供当期数字。
运行（仓库根）：python scripts/eq_two_list_now.py（缓存，约1min）
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


def main():
    t0 = time.time()
    pool = G.load_pool()
    pairs = G.career_pairs(D._manager_em_table(), pool["code"].tolist())
    pairs = pairs[pairs["career_y"].notna()].reset_index(drop=True)
    cand = set(pairs.loc[pairs["career_y"] > G.CAREER_MIN_Y, "code"])
    f10s = {}
    for c in cand:
        p = NAV_DIR / f"f10_v3_{c}.pkl"
        if p.exists():
            try:
                f10s[c] = pd.read_pickle(p)
            except Exception:
                pass

    hit = pd.read_pickle(ROOT / "data/eq_sustain_feat.pkl")["T1"]
    feat = hit.copy()
    # 展开 yearly → back0..4 + 同格逐年分位（与 eq_peer_test 同口径）
    bdf = feat["yearly"].apply(pd.Series)
    bdf.columns = [f"back{j}" for j in range(bdf.shape[1])]
    feat = pd.concat([feat.drop(columns=["yearly"]), bdf], axis=1)
    for j in range(5):
        feat[f"ps_{j}"] = P.rank_pcts(feat, f"back{j}",
                                      [["type", "bucket"], ["bucket"],
                                       ["type"]])

    qual = P.qualified_at(T1, feat, pairs, f10s)
    shift = 0.0
    car = pairs.groupby("code")["career_y"].max() - shift
    chan = car.reindex(feat.index).map(
        lambda c: "A" if (c == c and c > 10)
        else ("B" if (c == c and c > 6) else "out"))

    def g3s(c):
        ps = [feat.loc[c, f"ps_{j}"] for j in range(5)]
        return all(not pd.isna(x) for x in ps) and \
            sum(1 for x in ps if x > 75) >= 3

    def g4s(c):
        ps = [feat.loc[c, f"ps_{j}"] for j in range(5)]
        return all(not pd.isna(x) for x in ps) and \
            (sum(ps) / 5) >= 70

    vet_f = {c for c in qual if chan.get(c) == "A" and g3s(c)}
    new_f = {c for c in qual if chan.get(c) == "B" and g4s(c)}

    def mgrs_of(codes, ch):
        out = set()
        for row in pairs[pairs["code"].isin(codes)].itertuples():
            cy = row.career_y - shift
            if (ch == "A" and cy > 10) or (ch == "B" and 6 < cy <= 10):
                out.add((row.mgr, row.company))
        return out

    vet_m = mgrs_of(vet_f, "A")
    new_m = mgrs_of(new_f, "B")
    print(f"当期 {T1.date()} 两榜规模（合格池 {len(qual)}）:")
    print(f"  老将榜(A∩G3s中档): 基金 {len(vet_f)} / 经理 {len(vet_m)}")
    print(f"  新锐榜(B∩G4s严档): 基金 {len(new_f)} / 经理 {len(new_m)}")
    print(f"  合计经理 {len(vet_m | new_m)}")
    # 风格构成
    for lab, cs in (("老将榜", vet_f), ("新锐榜", new_f)):
        if cs:
            bc = feat.reindex(list(cs))["bucket"].value_counts(normalize=True)
            top = ", ".join(f"{k} {v:.0%}" for k, v in bc.head(5).items())
            print(f"  {lab}风格: {top}")
    # 对照：若老将也用严档G4s / 若新锐用中档G3s（供"调严/调宽"参考）
    vet_f_g4 = {c for c in qual if chan.get(c) == "A" and g4s(c)}
    new_f_g3 = {c for c in qual if chan.get(c) == "B" and g3s(c)}
    print(f"\n  [对照] 老将改严档G4s: 基金 {len(vet_f_g4)} / "
          f"经理 {len(mgrs_of(vet_f_g4, 'A'))}")
    print(f"  [对照] 新锐改中档G3s: 基金 {len(new_f_g3)} / "
          f"经理 {len(mgrs_of(new_f_g3, 'B'))}")
    print(f"\n总耗时 {(time.time()-t0)/60:.1f}min", flush=True)


if __name__ == "__main__":
    main()
