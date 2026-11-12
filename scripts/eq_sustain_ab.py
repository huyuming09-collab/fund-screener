# -*- coding: utf-8 -*-
"""补充：T1 时点各规则下 A/B 通道存活分解（复用 eq_sustain_check 的缓存特征）。"""
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import pandas as pd                                 # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import scripts.eq_sustain_check as S                # noqa: E402
from fundscorer import eqgate as G                  # noqa: E402
from fundscorer import data as D                    # noqa: E402


def main():
    pool = G.load_pool()
    S.CODE_INFO = dict(zip(pool["code"], zip(pool["name"], pool["type_text"])))
    S.PAIRS = G.career_pairs(D._manager_em_table(), pool["code"].tolist())
    S.PAIRS = S.PAIRS[S.PAIRS["career_y"].notna()].reset_index(drop=True)
    S.PAIR_CAREER = {(r.mgr, r.code): r.career_y for r in S.PAIRS.itertuples()}
    wealth = S.build_composite()
    feat = pd.read_pickle("data/eq_sustain_feat.pkl")   # 缓存命中，秒级

    # f10
    cand = set(S.PAIRS.loc[S.PAIRS["career_y"] > G.CAREER_MIN_Y, "code"])
    f10s = {}
    for c in cand:
        p = Path(f"data/cache/f10_v3_{c}.pkl")
        if p.exists():
            try:
                f10s[c] = pd.read_pickle(p)
            except Exception:
                pass

    T1 = S.T1
    thr5 = S.build_thr(feat["T1"], "r5")
    res = S.evaluate(feat["T1"], thr5, S.build_thr(feat["T1"], "r3"),
                     T1, 0, S.RULES_FULL, wealth, f10s)

    # 新进者 P闸通过率（T1新进 R1 中过 R6 的比例 → 名单稳态增速估计）
    T0 = T1 - pd.Timedelta(days=365)
    res0 = S.evaluate(feat["T0"], S.build_thr(feat["T0"], "r5"),
                      S.build_thr(feat["T0"], "r3"), T0, 365,
                      ["R1", "R6"], wealth, f10s)
    new_in = res["codes"]["R1"] - res0["codes"]["R1"]
    new_r6 = new_in & res["codes"]["R6"]
    print(f"\n新进者P闸通过率: T1新进R1 {len(new_in)} → 过R6 {len(new_r6)} "
          f"({len(new_r6)/max(len(new_in),1):.1%})")
    print(f"  → 稳态年流入估计: R6名单 +{len(new_r6)} 只（vs 当前R6 "
          f"{len(res['codes']['R6'])}）")


    # 通道归属：按 pair 的 career
    def chan_of(code):
        # 该基金通过 pair 中任一的通道（取最强：A优先）
        chans = {G.channel(S.PAIR_CAREER.get((m, c), 0))
                 for m, co, c in res["pairs"]["R1"] if c == code}
        return "A" if "A" in chans else ("B" if "B" in chans else "?")

    print("=== T1 各规则下 A/B 通道存活（基金/经理）===")
    for r in S.RULES_FULL:
        cs = res["codes"][r]
        ps = res["pairs"][r]
        a_cs = {c for c in cs if chan_of(c) == "A"}
        b_cs = cs - a_cs
        a_mgr = {(m, co) for m, co, c in ps if c in a_cs}
        b_mgr = {(m, co) for m, co, c in ps if c in b_cs}
        print(f"  {r}: A通道 基金{len(a_cs)}/经理{len(a_mgr)} | "
              f"B通道 基金{len(b_cs)}/经理{len(b_mgr)} | "
              f"合计 经理{len(S.mgr_set(ps))}")

    # R6 通过者的通道构成 + 从业年限分布
    r6 = res["pairs"]["R6"]
    if r6:
        rows = [{"mgr": m, "co": co, "code": c,
                 "career": S.PAIR_CAREER.get((m, c))}
                for m, co, c in r6]
        df = pd.DataFrame(rows).drop_duplicates(["mgr", "co"])
        print(f"\nR6 通过经理 {len(df)} 人，从业年限分布:")
        print(df["career"].describe(
            percentiles=[.25, .5, .75]).round(1).to_string())
        print(f"  其中 >10年(A通道): {int((df['career'] > 10).sum())} 人, "
              f"6~10年(B通道): {int((df['career'] <= 10).sum())} 人")


if __name__ == "__main__":
    main()
