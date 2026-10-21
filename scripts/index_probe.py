"""指数型债券数据画像 + 现行插值机制诊断（docs/17 前置，研究脚本，不入主链）

1) 数据画像（短债/长债两池指数分支）：候选漏斗、ret_3y/5y/波动/费率/基准覆盖、
   跟踪指数分组、业绩落点分布（端点率）。
2) 插值机制 LOO 诊断：把**主动候选**假装成指数基金——其真实分数已知，
   用"主动 TOP-N 其余锚点"插值还原，量测误差。四种方案同场对照：
     S0 现行：ret_3y 双锚线性插值（indexfund._interp）
     S1 平滑：ret_3y k=4 距离加权（IDW, p=2）
     S2 多因子 kNN：k=3，标准化 (ret_3y, fee_total, vol_5y)（缺维退化）
     S3 序位插值：ret_3y 排序位置的分数序位线性映射
   分组：G1 = 自身在主动 TOP20 内（锚点含自身，LOO 去自）；G2 = 自身在 TOP20 外。
3) 端点：业绩越过全部锚点 → 与现行同规则 clamp 到分数极值。

输出：打印表 + 落盘 data/index_probe.csv。
用法：.venv/bin/python scripts/index_probe.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fundscorer import config as C            # noqa: E402
from fundscorer import indexfund as IF        # noqa: E402
from fundscorer import pipeline as P          # noqa: E402
from fundscorer import screen as S            # noqa: E402
from fundscorer.scorer import score_pool      # noqa: E402

TOP_N = 20


# ---------------------------------------------------------------- 四种插值
def s0_linear(ret: float, pts: list) -> float | None:
    """现行：双锚线性插值（pts: (ret, score) 升序）。"""
    return IF._interp(ret, pts) if pts else None


def s1_idw(ret: float, pts: list, k: int = 4) -> float | None:
    """距离加权（IDW, p=2）：取 ret 最近 k 只，权重 1/d²；完全重合点直接取其分。"""
    if not pts:
        return None
    if ret >= pts[-1][0]:
        return max(p[1] for p in pts)
    if ret <= pts[0][0]:
        return min(p[1] for p in pts)
    near = sorted(pts, key=lambda p: abs(p[0] - ret))[:k]
    for p in near:
        if p[0] == ret:
            return p[1]
    w = [1.0 / (p[0] - ret) ** 2 for p in near]
    return float(np.average([p[1] for p in near], weights=w))


def s2_knn(ret: float, fee, vol, pts: list, k: int = 3) -> float | None:
    """多因子 kNN：标准化 (ret_3y, fee_total, vol_5y) 欧氏距离，k 只等权平均。

    pts 元素 (ret, score, fee, vol)。某维全缺/自身无该维 → 退化为可用维；
    全维不可用 → 退回 S0 线性插值。
    """
    if not pts:
        return None
    dims = [(ret, [p[0] for p in pts]), (fee, [p[2] for p in pts])]
    dims = [(v, col) for v, col in dims if v is not None and not np.isnan(v)
            and not all(np.isnan(x) for x in col)]
    vol_col = [p[3] for p in pts]
    if vol is not None and not np.isnan(vol) and not all(np.isnan(x) for x in vol_col):
        dims.append((vol, vol_col))
    if not dims:
        return s0_linear(ret, [(p[0], p[1]) for p in pts])
    v0 = [d[0] for d in dims]
    cols = [np.array([np.nan if x is None or (isinstance(x, float) and np.isnan(x))
                      else float(x) for x in d[1]], dtype=float) for d in dims]
    dists = []
    for i, p in enumerate(pts):
        dd = 0.0
        ok = False
        for j, c in enumerate(cols):
            if np.isnan(c[i]) or np.isnan(v0[j]):
                continue
            sd = np.nanstd(c)
            if not sd or np.isnan(sd):
                continue
            dd += ((v0[j] - c[i]) / sd) ** 2
            ok = True
        dists.append((float(np.sqrt(dd)) if ok else 1e9, p[1]))
    dists.sort(key=lambda x: x[0])
    if dists[0][0] >= 1e9:
        return s0_linear(ret, [(p[0], p[1]) for p in pts])
    top = [d for d in dists if d[0] < 1e9][:k]
    w = [1.0 / (d + 1e-6) ** 2 for d, _ in top]
    return float(np.average([s for _, s in top], weights=w))


def s3_rankpos(ret: float, pts: list) -> float | None:
    """序位插值：按 ret 升序取分数序列，用**序位分数**线性插（端点 clamp）。"""
    if not pts:
        return None
    if ret >= pts[-1][0]:
        return max(p[1] for p in pts)
    if ret <= pts[0][0]:
        return min(p[1] for p in pts)
    rets = [p[0] for p in pts]
    pos = np.searchsorted(rets, ret)
    if pos <= 0:
        return pts[0][1]
    r0, r1 = pts[pos - 1][0], pts[pos][0]
    if r1 == r0:
        return pts[pos][1]
    w = (ret - r0) / (r1 - r0)
    return pts[pos - 1][1] + w * (pts[pos][1] - pts[pos - 1][1])


# ---------------------------------------------------------------- 主流程
def main() -> None:
    t0 = time.time()
    rows, loo_rows = [], []
    for pool in ("short_bond", "long_bond"):
        cfg = C.SCREENS[pool]
        records, spikes, yearly, _ = S.load_screen_inputs(asof=None, cfg=cfg)
        act = [r for r in records if not P._is_index(r)]
        idx = [r for r in records if P._is_index(r)]

        cand, funnel = S.screen_candidates(act, spikes, yearly, cfg)
        idx_cand, idx_funnel = S.screen_candidates(idx, spikes, yearly, cfg, index_fund=True)
        cand_codes = {c for codes in cand["codes"] for c in codes}
        quota = sum(1 for r in act if not r.get("insufficient_history"))
        scored = score_pool(act, score_only=cand_codes, quota_n=quota)
        snap = {r["code"]: r for r in scored if "score" in r and not r.get("_unscored")}
        res = S.rank_candidates(cand, snap, cfg, top_n=TOP_N)

        # 指数侧
        idx_codes = {c for codes in idx_cand["codes"] for c in codes}
        idx_quota = sum(1 for r in idx if not r.get("insufficient_history"))
        idx_scored = score_pool(idx, score_only=idx_codes, quota_n=idx_quota)
        idx_snap = {r["code"]: r for r in idx_scored if "score" in r and not r.get("_unscored")}
        idx_rows = S.rank_candidates(idx_cand, idx_snap, cfg, top_n=None)
        idx_meta = P._index_meta(idx)

        tag = "短债" if pool == "short_bond" else "长债"
        print(f"\n{'=' * 70}\n===== {tag}池 =====")
        print("指数漏斗:", " → ".join(f"{k} {v}" for k, v in idx_funnel.items()))

        # ---- 数据画像（指数候选）----
        n = len(idx_rows) if idx_rows is not None else 0
        if n:
            def cov(col):
                if col not in idx_rows:
                    return 0
                s = pd.to_numeric(idx_rows[col], errors="coerce")
                return int(s.notna().sum())
            bm_cov = sum(1 for c in idx_rows["code"]
                         if (idx_meta.get(c) or {}).get("benchmark"))
            fee_vals = pd.to_numeric(idx_rows.get("fee_total"), errors="coerce") \
                if "fee_total" in idx_rows else pd.Series(dtype=float)
            ids = [IF.index_id((idx_meta.get(c) or {}).get("benchmark", ""), nm)
                   for c, nm in zip(idx_rows["code"], idx_rows["name"])]
            print(f"指数候选 {n} 只：ret_3y 覆盖 {cov('ret_3y')} / ret_5y {cov('ret_5y')}"
                  f" / vol_5y {cov('vol_5y')} / mdd_5y {cov('mdd_5y')}"
                  f" / 基准(benchmark) {bm_cov} → 跟踪指数组 {len(set(ids))} 个")
            if fee_vals.notna().any():
                print(f"  指数费率中位 {fee_vals.median():.3f}%")

        # ---- 落点分布：指数候选业绩 vs 主动 TOP20 业绩区间 ----
        m = C.INDEX["return_metric"]
        top = res.copy()
        top[m] = pd.to_numeric(top[m], errors="coerce")
        top_s = top.dropna(subset=[m, "score"])
        lo_e, hi_e = top_s[m].min(), top_s[m].max()
        if idx_rows is not None and len(idx_rows) and m in idx_rows:
            iv = pd.to_numeric(idx_rows[m], errors="coerce").dropna()
            above = int((iv > hi_e).sum())
            below = int((iv < lo_e).sum())
            inside = len(iv) - above - below
            print(f"落点（vs 主动TOP20 业绩区间 [{lo_e:.3f}, {hi_e:.3f}]）："
                  f"区间内 {inside} / 高于全部 {above}（锚最高分）/ 低于全部 {below}（锚最低分）")

        # ---- LOO 诊断：主动候选假装指数 ----
        top20 = top_s[[m, "score", "code"]].values.tolist()   # (ret, score, code)
        fund_rows = []
        for _, r in cand.iterrows():
            c = r["rep_code"]                  # 代表份额（榜单口径）；B/C 份额同指标不重复计
            if c not in snap:
                continue
            sc = snap[c].get("score_capped")
            if sc is None or not pd.notna(r.get(m)):
                continue
            fee = snap[c].get("fee") or (snap[c].get("metrics") or {}).get("fee")
            vol = r.get("vol_5y") if pd.notna(r.get("vol_5y", np.nan)) else None
            fund_rows.append({
                "code": c, "ret": float(r[m]), "score": float(sc),
                "in_top20": c in set(top_s["code"]),
                "fee": fee,
                "vol": float(vol) if vol is not None else np.nan,
            })
        fd = pd.DataFrame(fund_rows)
        # 多因子 pts（fee/vol 维）
        def pts3(exclude):
            out = []
            for _, r in fd.iterrows():
                if r["code"] == exclude:
                    continue
                fee = r["fee"]
                out.append((r["ret"], r["score"],
                            float(fee) if fee is not None and pd.notna(fee) else np.nan,
                            float(r["vol"]) if r["vol"] is not None and not pd.isna(r["vol"]) else np.nan))
            return out
        for _, r in fd.iterrows():
            # G1 用 TOP20 锚点（按 code 精确去自），G2 用全部 TOP20 锚点
            if r["in_top20"]:
                a = sorted([(p[0], p[1]) for p in top20 if p[2] != r["code"]])
                g = "G1(top20内)"
            else:
                a = sorted([(p[0], p[1]) for p in top20])
                g = "G2(top20外)"
            p3 = sorted(pts3(r["code"]))
            a_all = sorted([(p[0], p[1]) for p in p3])
            fee_v = float(r["fee"]) if r["fee"] is not None and pd.notna(r["fee"]) else None
            vol_v = float(r["vol"]) if r["vol"] is not None and not pd.isna(r["vol"]) else None
            est = {
                # A0 现行 = TOP20 锚点 + ret_3y 双锚线性
                "A0 现行(top20+S0)": s0_linear(r["ret"], a),
                # A1~A3 = 锚点集换成**全部主动候选**（生产快照里分数本来就有，只是现行没用）
                "A1 全候选+S0": s0_linear(r["ret"], a_all),
                "A2 全候选+S1(IDW4)": s1_idw(r["ret"], a_all),
                "A3 全候选+S2(kNN3)": s2_knn(r["ret"], fee_v, vol_v, p3),
                # H1 条件锚点：落在 TOP20 业绩区间内 → 现行双锚；越界 → 全候选双锚（消灭 clamp）
                "H1 条件锚点": (s0_linear(r["ret"], a)
                                if a and a[0][0] <= r["ret"] <= a[-1][0]
                                else s0_linear(r["ret"], a_all)),
            }
            for k, v in est.items():
                loo_rows.append({"pool": pool, "group": g, "code": r["code"],
                                 "scheme": k, "true": r["score"],
                                 "est": v, "err": None if v is None else v - r["score"]})
        print(f"LOO 样本：主动候选 {len(fd)}（top20内 {int(fd['in_top20'].sum())}）"
              f"，用时 {time.time() - t0:.0f}s")

    loo = pd.DataFrame(loo_rows)
    loo.to_csv(ROOT / "data" / "index_loo.csv", index=False, encoding="utf-8-sig")
    print(f"\n{'=' * 70}\n===== LOO 误差汇总（|est−true|，分）=====")
    for (pool, grp), g in loo.groupby(["pool", "group"]):
        print(f"\n[{ '短债' if pool=='short_bond' else '长债'}] {grp}")
        for sch, gs in g.groupby("scheme"):
            e = gs["err"].dropna().abs()
            if not len(e):
                continue
            print(f"  {sch}: n={len(e)} MAE={e.mean():.2f} 中位AE={e.median():.2f} "
                  f"max={e.max():.2f} |err|>5 占比={(e > 5).mean():.0%} "
                  f"|err|>10={(e > 10).mean():.0%}")
    print(f"\n用时 {time.time() - t0:.0f}s；落盘 data/index_loo.csv")


if __name__ == "__main__":
    main()
