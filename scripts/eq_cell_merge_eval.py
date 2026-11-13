# -*- coding: utf-8 -*-
"""小格合并评估（2026-11-14 七问排查 P1）：格样本<150 → 并入三类，双起点回测裁决。

输入：data/eq_bt_T0_1y.pkl / eq_bt_Tm_2y.pkl（type/bkt/yearly/fut_ret/fut_rank）。
  A = 现产线格（type × 7桶·降级后）
  B = A格 n<150 的基金改在（type × 三类）组内取年窗排位；大格不动
指标（每起点，B vs A）：
  M1 考卷稳定性：G3s 过考名单（5窗中≥3窗 >G3S_PCT）两期 Jaccard——越高越稳
  M2 小格基金分半信度：近2窗 vs 旧3窗 均值排位的秩相关——越高越稳
  M3 小格基金信号保留：ps均值 → 未来同格排位 IC（B 重算三类内未来排位）——不许降
判据（P1 拍板口径）：M1↑ 且 M3 不降 → 落地合并；否则记录不动。
运行（仓库根）：python scripts/eq_cell_merge_eval.py
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import numpy as np                                     # noqa: E402
import pandas as pd                                    # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from fundscorer import eqscore as ES                   # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SMALL_N = 150
TAGS = ["T0_1y", "Tm_2y"]


def rank_in(values: pd.Series, group: pd.Series, level) -> pd.Series:
    """level: 'cell'=(type,bkt) | 'bkt' | 'type' | 'c3'=(type,c3) | 'c3only'。
    组内 n≥10 才给分位（引擎同款），否则回退下一档。"""
    if level == "cell":
        keys = ["type", "bkt"]
    elif level == "bkt":
        keys = ["bkt"]
    elif level == "type":
        keys = ["type"]
    elif level == "c3":
        keys = ["type", "c3"]
    else:
        keys = ["c3"]
    # 逐级 groupby（引擎同款回退：主档 n≥10，否则下一档）
    def attempt(ks):
        res = pd.Series(np.nan, index=values.index)
        for _, g in group.groupby(ks):
            if len(g) >= 10:
                res.loc[g.index] = values.loc[g.index].rank(pct=True) * 100
        return res
    if level == "cell":
        out = attempt(["type", "bkt"])
        mask = out.isna()
        if mask.any():
            out[mask] = attempt(["bkt"])[mask]
        mask = out.isna()
        if mask.any():
            out[mask] = attempt(["type"])[mask]
        return out
    if level == "c3":
        out = attempt(["type", "c3"])
        mask = out.isna()
        if mask.any():
            out[mask] = attempt(["c3"])[mask]
        mask = out.isna()
        if mask.any():
            out[mask] = attempt(["type"])[mask]
        return out
    return attempt(keys)


def spearman(a, b) -> float:
    d = pd.concat([pd.Series(a, dtype="float64"),
                   pd.Series(b, dtype="float64")], axis=1).dropna()
    if len(d) < 30:
        return np.nan
    return float(d.iloc[:, 0].rank().corr(d.iloc[:, 1].rank()))


def pass_g3s(ps5: pd.DataFrame) -> set:
    return set(ps5.index[(ps5 > ES.G3S_PCT).sum(axis=1) >= ES.G3S_MIN_TOP])


def main() -> int:
    frames = {}
    for tag in TAGS:
        p = ROOT / f"data/eq_bt_{tag}.pkl"
        if not p.exists():
            print("缺", p.name, "——先跑 eq_backtest_t0.py")
            return 1
        df = pd.read_pickle(p)
        df["c3"] = [ES.display_bucket(b) for b in df["bkt"]]
        frames[tag] = df
        print(f"{tag}: {len(df)} 只 | A格<150 的基金 "
              f"{int(df.groupby(['type','bkt'])['bkt'].transform('size').lt(SMALL_N).sum())}")

    # ---- 每起点：A/B 两套年窗排位 + 过考名单 ----
    passes = {}
    smalls = {}
    for tag, df in frames.items():
        # yearly: list[5]（[0]=最近一年）
        y = pd.DataFrame([v for v in df["yearly"]], index=df.index,
                         columns=[f"w{j}" for j in range(5)])
        cell_n = df.groupby(["type", "bkt"])["bkt"].transform("size")
        small = cell_n < SMALL_N
        smalls[tag] = set(df.index[small])
        psA, psB = {}, {}
        for j in range(5):
            v = y[f"w{j}"]
            psA[j] = rank_in(v, df, "cell")
            # B：小格基金在（type×三类）里排，大格维持 A
            b = rank_in(v, df, "c3")
            psB[j] = pd.Series(np.where(small, b, psA[j]), index=df.index)
        psA = pd.DataFrame(psA, index=df.index)
        psB = pd.DataFrame(psB, index=df.index)
        passes[tag] = (pass_g3s(psA), pass_g3s(psB))
        # 半分信度数据存起来（用 T0 的小格集合做 M2/M3，跨期各算一次）
        frames[tag]["_psA_mean"] = psA.mean(axis=1)
        frames[tag]["_psB_mean"] = psB.mean(axis=1)
        frames[tag]["_small"] = small
        frames[tag]["_half1A"] = psA[[0, 1]].mean(axis=1)
        frames[tag]["_half2A"] = psA[[2, 3, 4]].mean(axis=1)
        b1 = pd.concat([rank_in(y[f"w{j}"], df, "c3") for j in (0, 1)],
                       axis=1).mean(axis=1)
        b2 = pd.concat([rank_in(y[f"w{j}"], df, "c3") for j in (2, 3, 4)],
                       axis=1).mean(axis=1)
        frames[tag]["_half1B"] = pd.Series(np.where(small, b1,
                                                    psA[[0, 1]].mean(axis=1)),
                                           index=df.index)
        frames[tag]["_half2B"] = pd.Series(np.where(small, b2,
                                                    psA[[2, 3, 4]].mean(axis=1)),
                                           index=df.index)

    # ---- M1：过考名单两期 Jaccard（A vs B）----
    print("\n[M1] G3s 过考名单 T0∩Tm Jaccard（越高越稳）")
    (a0, b0), (a1, b1) = passes[TAGS[0]], passes[TAGS[1]]
    for lab, s0, s1 in (("A 现产线", a0, a1), ("B 小格并三类", b0, b1)):
        u = s0 | s1
        j = len(s0 & s1) / len(u) if u else np.nan
        print(f"  {lab:<12} T0过考 {len(s0):>4} | Tm过考 {len(s1):>4} | "
              f"交集 {len(s0 & s1):>4} | Jaccard {j:.3f}")

    # ---- M2/M3：小格基金（每期各自的 A-小格集合）----
    print("\n[M2] 小格基金分半信度（近2窗 vs 旧3窗秩相关，越高越稳）")
    print("[M3] 小格基金信号：ps均值 → 未来同格排位 IC（B 的未来排位按三类重算）")
    for tag, df in frames.items():
        sub = df[df["_small"]].dropna(subset=["fut_ret"])
        if len(sub) < 30:
            print(f"  {tag}: 小格样本 {len(sub)} <30，跳过")
            continue
        m2a = spearman(sub["_half1A"], sub["_half2A"])
        m2b = spearman(sub["_half1B"], sub["_half2B"])
        # M3-A：现产线 fut_rank（A格内）
        m3a = spearman(sub["_psA_mean"], sub["fut_rank"])
        # M3-B：未来排位在（type×三类）内重算（全体排、取子集）
        fr_full = rank_in(df["fut_ret"], df, "c3")
        m3b = spearman(sub["_psB_mean"], fr_full.reindex(sub.index))
        print(f"  {tag}: 小格n={len(sub):>4} | M2信度 A={m2a:.3f} B={m2b:.3f} "
              f"（Δ{m2b - m2a:+.3f}） | M3 IC A={m3a:.3f} B={m3b:.3f} "
              f"（Δ{m3b - m3a:+.3f}）")

    print("\n判据：M1↑ 且 M3 不降 → 落地合并；否则记录不动（结果写 STATUS §6）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
