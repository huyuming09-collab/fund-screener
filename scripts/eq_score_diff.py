# -*- coding: utf-8 -*-
"""规模差异归因：eq_two_list_now 口径（fund 级 S3）vs eqscore 口径（pair 级 S3）。

同一批数据下算两套名单，输出只差在哪——验证"65/40 vs 70/44"是否全部由
pair 级 S3 从严造成（设计稿 §2.4 L1 的 S3 是 pair 级，eqgate 闸也是 pair 级）。
运行（仓库根）：python scripts/eq_score_diff.py（全离线，约1min）
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import pandas as pd                                 # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from fundscorer import data as D                    # noqa: E402
from fundscorer import eqgate as G                  # noqa: E402
from fundscorer import eqscore as ES                # noqa: E402

T1 = pd.Timestamp("2026-11-12")


def main():
    pool = ES._pool(False)
    pairs = G.career_pairs(ES._mgr_table(False), pool["code"].tolist())
    pairs = pairs[pairs["career_y"].notna()].reset_index(drop=True)
    navres = ES._nav_stats(pool["code"].tolist(), refresh=False, workers=8,
                           log=lambda *a, **k: None)
    s4 = [c for c, v in navres["stats"].items() if v[0] >= G.NAV_MIN_Y]
    styles = G.classify_styles(s4) or {}
    fb = ES.build_features(pool, styles, log=lambda *a, **k: None)
    feat, o = fb["feat"], fb["o"]
    cand_codes = sorted({r.code for r in pairs.itertuples(index=False)
                         if G.channel(r.career_y) in ("A", "B")
                         and r.code in feat.index})
    f10s = ES._f10s(cand_codes, refresh=False, workers=8,
                    log=lambda *a, **k: None)
    exams = ES.exam_flags(feat)
    fch = ES.fund_channels(pairs)

    # ---- 口径A：eq_two_list_now 同款（fund 级 S3：任一 pair 达标即基金入围）
    qual_fund = set()
    for row in pairs.itertuples(index=False):
        if G.channel(row.career_y) == "out" or row.code not in feat.index:
            continue
        info = f10s.get(row.code)
        if not info or not info.get("managers"):
            continue
        mg = next((m for m in info["managers"] if m.get("name") == row.mgr), None)
        if mg is None:
            continue
        try:
            start = pd.Timestamp(str(mg.get("start") or "")[:10])
        except Exception:
            continue
        if start > o - pd.Timedelta(days=G.TENURE_MIN_D):
            continue                             # 本人任期<5y → 该 pair 不算
        qual_fund.add(row.code)
    vet_f = {c for c in qual_fund if fch.get(c) == "A"
             and exams.get(c) and "G3s" in exams[c]}
    new_f = {c for c in qual_fund if fch.get(c) == "B"
             and exams.get(c) and "G4s" in exams[c]}
    print(f"[debug] feat={len(feat)} o={o.date()} cand={len(cand_codes)} "
          f"f10s={len(f10s)} qual_fund={len(qual_fund)} vet_f={len(vet_f)} "
          f"new_f={len(new_f)} examsG3s="
          f"{sum(1 for v in exams.values() if v and 'G3s' in v)}")
    def mgrs_of(codes, ch):
        out = set()
        for row in pairs[pairs["code"].isin(codes)].itertuples():
            cy = row.career_y
            if (ch == "A" and cy > 10) or (ch == "B" and 6 < cy <= 10):
                out.add((row.mgr, row.company))
        return out

    vetA, newA = mgrs_of(vet_f, "A"), mgrs_of(new_f, "B")

    # ---- 口径B：引擎（pair 级 S3：本人任期≥5y 才算入围 pair）
    qual_pair = ES.qualify_pairs(pairs, feat, f10s, o)
    cand = ES.build_candidates(pairs, qual_pair, exams, fch, f10s, navres["ok"])
    vetB = {tuple(k.split("|", 1)) for k, c in cand.items()
            if c["channel"] == "A" and c["pass_funds"]}
    newB = {tuple(k.split("|", 1)) for k, c in cand.items()
            if c["channel"] == "B" and c["pass_funds"]}

    print(f"口径A fund级 S3：老将 {len(vetA)} / 新锐 {len(newA)}")
    print(f"口径B pair级 S3：老将 {len(vetB)} / 新锐 {len(newB)}")
    for name, A, B in (("老将", vetA, vetB), ("新锐", newA, newB)):
        onlyA, onlyB = A - B, B - A
        print(f"\n{name}榜：只在A(引擎剔除) {len(onlyA)} / 只在B {len(onlyB)}")
        for m, co in sorted(onlyA)[:8]:
            # 找出该经理在A口径下靠哪只基金、自己任期多少
            rows = qual_pair[(qual_pair["mgr"] == m)
                             & (qual_pair["company"] == co)]
            codes = [r.code for r in pairs.itertuples(index=False)
                     if r.mgr == m and r.company == co]
            ten = []
            for c in codes:
                info = f10s.get(c) or {}
                mg = next((x for x in info.get("managers") or []
                           if x.get("name") == m), None)
                try:
                    d = (o - pd.Timestamp(str((mg or {}).get("start") or "")[:10])).days
                    ten.append((c, round(d / 365.25, 1)))
                except Exception:
                    ten.append((c, None))
            print(f"  - {m}({co}) 本人入围pair {len(rows)} 只；"
                  f"全部在管 tenure {ten}")


if __name__ == "__main__":
    main()
