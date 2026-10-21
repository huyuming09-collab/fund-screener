"""D13 · P1/P2 分位基准统一实验（M4 方案对照，研究脚本，不入主链）

现行口径（docs/13 §4.5-1）：主动侧 P1 基准 = 主动分支池，P2 基准 = 全池（含指数）；
指数侧 P1 基准 = 指数子池，P2 基准 = 全池 —— 四者基准互不相同。

  现行   = 上述口径（复现，作对照锚）
  方案a  = P2 两侧都用分支合并池（yearly 字典限制到本分支基础名）
  方案b  = P1 用含指数的合并池（四项分位跨分支重排），P2 维持全池

输出：两债池 × 三方案的漏斗计数、候选集对称差、当期 TOP20 成员的通过情况；
     落盘 data/d13_basis.csv。INDEX.enabled=False 现状下方案 b 主要影响"复开后"的口径。

用法：.venv/bin/python scripts/d13_basis_experiment.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fundscorer import config as C            # noqa: E402
from fundscorer import pipeline as P          # noqa: E402
from fundscorer import screen as S            # noqa: E402


def screen_merged_p1(act_recs: list[dict], idx_recs: list[dict], spikes: dict,
                     yearly: dict, cfg: dict) -> tuple[pd.Series, dict]:
    """方案 b：S 闸按分支口径（指数跳过 S2/S3），P1 四项分位用含指数合并池重排，P2 照旧全池。"""
    pool_m = S.merge_shares(act_recs + idx_recs)
    idx_bases = set(S.merge_shares(idx_recs)["base"]) if idx_recs else set()
    m = ~pool_m["base"].isin(idx_bases)
    keep = pool_m["scale_sum"] >= cfg["scale_min_yi"]
    keep.loc[m] &= pool_m.loc[m, "career"] >= cfg["mgr_career_min_years"]
    keep.loc[m] &= pool_m.loc[m, "tenure"] >= cfg["mgr_tenure_min_days"]
    keep &= pool_m["nav_years"] >= cfg["min_nav_years"]
    pool_m["max_gain_5y"] = pool_m["codes"].map(lambda cs: S._fund_max_gain(cs, spikes))
    keep &= pool_m["max_gain_5y"] <= cfg["spike_max_gain"]
    keep &= pool_m["mdd_5y"].notna()  # 无回撤数据者四项分位不成立（对齐现行隐含行为）
    Pc = S.composite_percentiles(pool_m)
    keep &= Pc["mpct"] >= cfg["comp_pct_min"]
    Y = pd.DataFrame(yearly).T
    med = pd.Series({i: S.yearly_median_pct(Y, b, yearly, cfg["yearly_pool_min"],
                                            cfg["yearly_min_years"])
                     for i, b in zip(pool_m.index, pool_m["base"])}, dtype="float64")
    keep &= med >= cfg["yearly_median_min"]
    fn = {"合并(跨分支)": len(pool_m), "S1~S5+P1": int(keep.sum())}
    return keep[keep].index, fn


def main() -> None:
    t = json.loads((ROOT / "data" / "top20.json").read_text(encoding="utf-8"))
    top_bases = {cat: {f["base"] for f in t["funds"] if f["category"] == cat}
                 for cat in ("short_bond", "long_bond")}

    rows = []
    for pool in ("short_bond", "long_bond"):
        cfg = C.SCREENS[pool]
        records, spikes, yearly, _ = S.load_screen_inputs(asof=None, cfg=cfg)
        act = [r for r in records if not P._is_index(r)]
        idx = [r for r in records if P._is_index(r)]

        # —— 现行 ——
        c_act, f_act = S.screen_candidates(act, spikes, yearly, cfg)
        c_idx, f_idx = S.screen_candidates(idx, spikes, yearly, cfg, index_fund=True)

        # —— 方案 a：P2 用分支池 ——
        b_act = set(S.merge_shares(act)["base"])
        b_idx = set(S.merge_shares(idx)["base"]) if idx else set()
        ya = {b: v for b, v in yearly.items() if b in b_act}
        yi = {b: v for b, v in yearly.items() if b in b_idx}
        c_act_a, f_act_a = S.screen_candidates(act, spikes, ya, cfg)
        c_idx_a, f_idx_a = S.screen_candidates(idx, spikes, yi, cfg, index_fund=True)

        # —— 方案 b：P1 用合并池 ——
        b_idx_s, f_mb = screen_merged_p1(act, idx, spikes, yearly, cfg)
        b_idx_set = set(b_idx_s)

        # 当期 TOP20 成员是否仍是候选（按 base 匹配）
        tb = top_bases[pool]
        def hit(cands: pd.Series) -> int:
            return len(tb & set(cands.index))
        for name, cands in (("现行", c_act), ("方案a", c_act_a)):
            rows.append({"pool": pool, "variant": name, "branch": "主动",
                         "candidates": len(cands), "top20命中": f"{hit(cands)}/{len(tb)}",
                         "P1末档人数": f_act_a.get("P1 综合分位≥45%", None) if name != "现行" else f_act.get("P1 综合分位≥45%"),
                         "P2末档人数": None})
        rows.append({"pool": pool, "variant": "方案b", "branch": "主动(跨分支P1,未叠加P2)",
                     "candidates": len(set(b_idx_set) & b_act),
                     "top20命中": f"{len(tb & set(b_idx_set))}/{len(tb)}",
                     "P1末档人数": f_mb.get("S1~S5+P1"), "P2末档人数": None})
        rows.append({"pool": pool, "variant": "现行", "branch": "指数",
                     "candidates": len(c_idx), "top20命中": "-", "P1末档人数": None, "P2末档人数": None})
        rows.append({"pool": pool, "variant": "方案a", "branch": "指数",
                     "candidates": len(c_idx_a), "top20命中": "-", "P1末档人数": None, "P2末档人数": None})

        # 候选集差异明细
        print(f"\n===== {pool} =====")
        print("现行主动漏斗:", " → ".join(f"{k} {v}" for k, v in f_act.items()))
        print("方案a主动漏斗:", " → ".join(f"{k} {v}" for k, v in f_act_a.items()))
        if idx:
            print("现行指数漏斗:", " → ".join(f"{k} {v}" for k, v in f_idx.items()))
            print("方案a指数漏斗:", " → ".join(f"{k} {v}" for k, v in f_idx_a.items()))
        sa, sb = set(c_act.index), set(c_act_a.index)
        print(f"主动候选 现行 {len(sa)} vs 方案a {len(sb)}：a新增 {len(sb - sa)}，a剔除 {len(sa - sb)}")
        # 份额代码级匹配（候选索引=merge_shares 的 base，与 top20 的 code 不同域）
        pool_act = S.merge_shares(act)
        tb_codes = {f["code"] for f in t["funds"] if f["category"] == pool}
        def _codes(bases):
            out: set = set()
            for b in bases:
                out |= set(pool_act.loc[b, "codes"])
            return out
        rm_c, add_c = _codes(sa - sb), _codes(sb - sa)
        print(f"   剔除候选涉及份额 {len(rm_c)}，其中命中当期TOP20: {sorted(rm_c & tb_codes) or '无'}")
        print(f"   新增候选涉及份额 {len(add_c)}，其中原在当期TOP20: {sorted(add_c & tb_codes) or '无'}")
        print(f"方案b(跨分支P1) 通过 S1~S5+P1: {f_mb.get('S1~S5+P1')}；"
              f"其中主动 {len(set(b_idx_set) & b_act)} / 指数 {len(set(b_idx_set) & b_idx)}"
              f"；命中现行TOP20 {len(tb & b_idx_set)}/{len(tb)}")
        # 方案b 相对现行在主动侧的增减
        c_act_b = set(b_idx_set) & b_act  # 方案b 通过 P1 的主动分支（P2 未叠加的上界）
        print(f"  [P1口径对照] 主动分支: 现行P1后 {len(c_act.index)} vs 方案bP1后 {len(c_act_b)}")

    pd.DataFrame(rows).to_csv(ROOT / "data" / "d13_basis.csv", index=False,
                              encoding="utf-8-sig")
    print("\n已落盘 data/d13_basis.csv")
    print("口径注：方案b 下 P2 仍为全池（其候选全量需叠加 P2 过滤，此处给 P1 维度对照）；"
          "INDEX.enabled=False 现状下方案 b 的实际生效时点为债指复开后。")


if __name__ == "__main__":
    main()
