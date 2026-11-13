# -*- coding: utf-8 -*-
"""规则联查（2026-11-13 七问自查）：诊断旗 × 伪前瞻结局。

把 eq_diag.csv（当期在榜140人的九类旗）与 eq_backtest_t0.pkl（T0/Tm 起点的
逐基金未来结局）按计分基金代码对齐 → 每类旗的 cut(命中) vs kept 在
T0→今 / Tm→今 的未来收益、未来同格排位、未来回撤中位数。
回答："按这面旗排除，排掉的人后来真的更差吗？"

限制：在榜者是既成样本（幸存者选择）；仓位旗只有当期值（10季，近2.5年）。
运行（仓库根）：python scripts/eq_rule_join.py
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

ROOT = Path(__file__).resolve().parent.parent


def med(s):
    s = pd.to_numeric(s, errors="coerce")
    return float(s.median()) if s.notna().any() else float("nan")


def main() -> int:
    diag = pd.read_csv(ROOT / "data" / "eq_diag.csv", dtype={"code": str})
    diag["code"] = diag["code"].str.zfill(6)
    d = diag.set_index("code")
    d = d[~d.index.duplicated(keep="first")]             # 同基金双经理→基金级去重
    # 旗定义（与 eq_diag 汇总一致）
    flags = {
        "R1深亏<-5%": d.ret5y < -0.05,
        "R2平庸(0-5%&分<70)": ((d.ret5y >= 0) & (d.ret5y < .05)
                          & (d.score < 70)),
        "R3壳<2亿": pd.to_numeric(d.scale, errors="coerce") < 2,
        "R4低仓位<60%": pd.to_numeric(d.pos_avg, errors="coerce") < 60,
        "R5行业≥40%": pd.to_numeric(d.top_ind_w, errors="coerce") >= .40,
        "R6挂名": (d.n_mgrs >= 3) & (~d.is_primary.astype(bool)),
        "R7量化": d.quant.astype(bool),
        "R8小格<150": pd.to_numeric(d.cell_n, errors="coerce") < 150,
        "R9标签失真|β|<0.5": pd.to_numeric(d.label_beta,
                                       errors="coerce").abs() < .5,
        "E1负收益(门槛现行)": d.ret5y < 0,
        "E2回撤>60%": pd.to_numeric(d.mdd5y, errors="coerce") > .60,
    }
    print(f"{'旗':<20} {'命中':>4} | {'T0未来收益 cut/kept':>22} | "
          f"{'T0未来同格排位':>16} | {'T0未来回撤':>13} | {'Tm未来收益':>18}")
    for tag, fname in [("T0_1y", None), ("Tm_2y", None)]:
        p = ROOT / f"data/eq_bt_{tag}.pkl"
        if not p.exists():
            print(f"（缺 {p.name}，跳过）")
            continue
        bt = pd.read_pickle(p)
        bt.index = bt.index.astype(str).str.zfill(6)
        j = diag.set_index("code").join(bt, how="inner")
        print(f"\n=== {tag}（在榜∩该起点可算 n={len(j)}）===")
        for name, mask in flags.items():
            cut = mask.reindex(j.index).fillna(False)
            if not cut.any():
                print(f"{name:<20} {int(cut.sum()):>4} | （无命中）")
                continue
            a, b = j[cut], j[~cut]
            print(f"{name:<20} {int(cut.sum()):>4} | "
                  f"{med(a.fut_ret):>+7.1%} / {med(b.fut_ret):>+7.1%} | "
                  f"{med(a.fut_rank):>7.1f} / {med(b.fut_rank):>7.1f} | "
                  f"{med(a.fut_mdd):>6.1%} / {med(b.fut_mdd):>6.1%} | "
                  f"{med(a.fut_ret):>+7.1%} / {med(b.fut_ret):>+7.1%}")
        # 多旗叠加
        n_flag = sum(int(v.reindex(j.index).fillna(False).sum())
                     for k, v in flags.items()
                     if k in ("R1深亏<-5%", "R3壳<2亿", "R4低仓位<60%"))
        j2 = j.copy()
        j2["n0"] = sum(v.reindex(j.index).fillna(False).astype(int)
                       for k, v in flags.items()
                       if k in ("R1深亏<-5%", "R3壳<2亿", "R4低仓位<60%"))
        for thr in (1, 2, 3):
            cut = j2["n0"] >= thr
            if cut.any() and (~cut).any():
                a, b = j2[cut], j2[~cut]
                print(f"  P0并集≥{thr}旗            {int(cut.sum()):>4} | "
                      f"{med(a.fut_ret):>+7.1%} / {med(b.fut_ret):>+7.1%} | "
                      f"{med(a.fut_rank):>7.1f} / {med(b.fut_rank):>7.1f} | "
                      f"{med(a.fut_mdd):>6.1%} / {med(b.fut_mdd):>6.1%} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
