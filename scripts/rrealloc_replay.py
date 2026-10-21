# -*- coding: utf-8 -*-
"""R-REALLOC 回测重放（docs/14 §7 决策支持，只读，不改任何生产权重）。

场景：因子归零/块内转移后，释放权重的两种定权口径在训练/验证段的组合表现。
块边界候选（D16-前置）：
  B1 缩水制：固定块允许缩水，释放权重全部进 IC 池；
  B2 块内转移：释放权重只在本块内转移（tenure→career / repair→vol），IC 池不动。
定权口径候选：
  A 等比放大：释放权重按 IC 池现有权重比例注入；
  B 证据注入：释放权重按**训练段方向对齐 IC>0** 比例注入（与现行权重同源信息）。
上限：单因子 ≤15（IC_POOL_CAP 口径），超限水填迭代（超出部分按同规则回分）。

评估口径与 fundscorer.backtest.analyze 组合验证一致：
  · sub_scores 由 _score_one 物化一次（与权重无关），候选权重线性重组；
  · 红线封顶复用；前 20%（rank pct≥0.8）持有 1 年：胜率=跑赢池中位、超额=同池中位差、
    fwdpct=未来收益池内分位均值；训练段=前 7 节点，验证段=后 4 节点。

用法：.venv/bin/python scripts/rrealloc_replay.py
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
from fundscorer import metrics as M           # noqa: E402
from fundscorer import scorer as S            # noqa: E402
from fundscorer.backtest import (FACTOR_DIR, HYBRID_FIXED, MIN_PAIRS, NODES,  # noqa: E402
                                 TRAIN_NODES, backtestable_weights)

CAP = 15.0
TRAIN_NODES_SET = set(NODES[:TRAIN_NODES])
VALID_NODES_SET = set(NODES[TRAIN_NODES:])
DEBT_POOLS = ("short_bond", "long_bond")


# ---------------------------------------------------------------- 分配规则
def _distribute(w: dict, ic_keys: list[str], freed: float,
                mode: str, train_ic: dict) -> dict:
    """把 freed 按 mode 分给 IC 池（A=等比 / B=训练段 IC 比例），并做 15 上限水填。"""
    if freed <= 0:
        return w
    if mode == "A":
        base = {k: max(w.get(k, 0.0), 0.0) for k in ic_keys}
    else:
        base = {k: max(w.get(k, 0.0), 0.0) * max(train_ic.get(k, 0.0), 0.0)
                for k in ic_keys}
        if sum(base.values()) <= 0:          # 无正 IC → 退化为等比
            base = {k: max(w.get(k, 0.0), 0.0) for k in ic_keys}
    tot = sum(base.values())
    if tot <= 0:
        return w
    add = {k: freed * v / tot for k, v in base.items()}
    for k, v in add.items():
        w[k] = w.get(k, 0.0) + v
    # 15 上限水填（IC 池内迭代；全员封顶的极端情形允许突破并标注）
    for _ in range(20):
        over = [k for k in ic_keys if w.get(k, 0.0) > CAP + 1e-9]
        if not over:
            break
        excess = sum(w[k] - CAP for k in over)
        for k in over:
            w[k] = CAP
        room = {k: base[k] for k in ic_keys
                if k not in over and base.get(k, 0) > 0}
        rt = sum(room.values())
        if rt <= 0:
            for k in ic_keys:                # 全员封顶：回加给全员
                w[k] = w.get(k, 0.0) + excess / len(ic_keys)
            break
        for k, v in room.items():
            w[k] += excess * v / rt
    return w


def build_scenario(w_a: dict, ic_keys: list[str], zero_keys: list[str],
                   intra: tuple[str, str] | None, mode: str,
                   train_ic: dict, shrink: dict | None = None) -> dict:
    w = dict(w_a)
    freed = 0.0
    for k, tgt in (shrink or {}).items():        # 部分缩减（如 D9 products 11→4）
        freed += w.get(k, 0.0) - tgt
        w[k] = tgt
    freed += sum(w.get(k, 0.0) for k in zero_keys)
    for k in zero_keys:
        w[k] = 0.0
    if intra:                               # B2 块内转移
        src, dst = intra
        w[dst] = w.get(dst, 0.0) + freed
        return w
    return _distribute(w, ic_keys, freed, mode, train_ic)


# ---------------------------------------------------------------- 主流程
def main() -> None:
    t0 = time.time()
    df = pd.read_pickle(ROOT / "data" / "backtest_panel.pkl")
    df = df[df["fwd_1y"].notna()].copy()

    # 池级因子补齐 + 胜负基准（与 analyze 相同）
    df["yearly_5y"] = np.nan
    df["rank_stab"] = np.nan
    for (T, pool), g in df.groupby(["T", "pool"]):
        funds = g.to_dict("records")
        stab = S._pool_yearly_ranks(funds)
        df.loc[g.index, "yearly_5y"] = [
            np.nan if v is None else float(v)
            for v in (S._yearly_score(f, funds, pool) for f in funds)]
        df.loc[g.index, "rank_stab"] = [
            np.nan if v is None else float(v)
            for v in (M.rank_stability_score(stab.get(f["code"], [])) for f in funds)]
    df["fwd_med"] = df.groupby(["T", "pool"])["fwd_1y"].transform("median")
    df["win"] = df["fwd_1y"] > df["fwd_med"]
    print(f"面板：{len(df)} 基金节点；池级因子补齐 {time.time()-t0:.0f}s")

    # 训练段方向对齐 IC（口径 B 用；与 backtest.analyze 汇总同法）
    train_ic: dict[str, dict[str, float]] = {}
    for pool in DEBT_POOLS:
        g_pool = df[df["pool"] == pool]
        per_fac: dict[str, list[float]] = {k: [] for k in FACTOR_DIR}
        for T in sorted(TRAIN_NODES_SET):
            gt = g_pool[g_pool["T"] == T]
            if len(gt) < MIN_PAIRS:
                continue
            for fac, d in FACTOR_DIR.items():
                if fac not in gt:
                    continue
                s = gt[fac].dropna()
                if len(s) < MIN_PAIRS:
                    continue
                v = s.corr(gt.loc[s.index, "fwd_1y"], method="spearman")
                if v is not None and not np.isnan(v):
                    per_fac[fac].append(float(v) * d)
        train_ic[pool] = {k: (float(np.mean(v)) if v else 0.0)
                          for k, v in per_fac.items()}

    # 物化 sub_scores / redlines（权重无关，_score_one 以 w_a 调用一次）
    scored: dict[tuple, list[dict]] = {}
    for (pool, T), g in df.groupby(["pool", "T"]):
        if pool not in DEBT_POOLS:
            continue
        w_a = backtestable_weights(pool)
        funds = g.to_dict("records")
        med_dd = (float(np.nanmedian([f["mdd_5y"] for f in funds
                                      if f.get("mdd_5y") is not None]))
                  if funds else 0.0)
        stab = S._pool_yearly_ranks(funds)
        for f in funds:
            f["rank_stab"] = M.rank_stability_score(stab.get(f["code"], []))
            f["mgr_career_years"] = f.get("mgr_career")
            f["fee_total"] = f.get("fee")
            f["repair_5y"] = f.get("repair")
        # 字段映射必须先对全部基金完成（_score_one 内部遍历全池 repair_5y）
        for f in funds:
            try:
                S._score_one(f, funds, w_a, pool, med_dd, stab.get(f["code"], []))
            except Exception as e:
                f["score"] = None
                print(f"  打分异常 {f['code']}@{pd.Timestamp(T).date()}: {e}")
            if f.get("r7_4") and "R7-4 单日异常跌幅且未修复" not in f.get("redlines", []):
                f.setdefault("redlines", []).append("R7-4 单日异常跌幅且未修复")
        scored[(pool, T)] = funds
    print(f"sub_scores 物化完成 {time.time()-t0:.0f}s")

    # ---------------------------------------------------------------- 场景
    def base_w(pool):
        return backtestable_weights(pool)

    def ic_keys_of(pool):
        fixed = set(HYBRID_FIXED["pure"])
        return [k for k in base_w(pool) if k not in fixed]

    SCEN: dict[str, list[tuple]] = {
        "short_bond": [
            ("S0 基线",            [],        None, "A"),
            ("D16a tenure→0 (B1-A)", ["mgr_tenure"],   None, "A"),
            ("D16b rank_stab→0 (B1-A)", ["rank_stab"], None, "A"),
            ("D17 repair→0 (B1-A)", ["repair_5y"],     None, "A"),
            ("D10A winrate→0 (B1-A)", ["winrate_m_5y"], None, "A"),
            ("TRP 三因子 (B1-A)",   ["mgr_tenure", "rank_stab", "repair_5y"], None, "A"),
            ("TRPW 全部 (B1-A)",    ["mgr_tenure", "rank_stab", "repair_5y", "winrate_m_5y"], None, "A"),
            ("TRPW 全部 (B1-B)",    ["mgr_tenure", "rank_stab", "repair_5y", "winrate_m_5y"], None, "B"),
            ("B2 tenure→career",    ["mgr_tenure"], ("mgr_tenure", "mgr_career"), "A"),
            ("B2 repair→vol",       ["repair_5y"],   ("repair_5y", "vol_5y"),      "A"),
            # —— D9/D10 拍板件（shrink=部分缩减，释放量按 旧值−目标值 计）——
            ("D9 products→4 (B1-A)", [], None, "A", {"mgr_products_ann": 4.0}),
            ("D9+D10A 全清 (B1-A)", ["winrate_m_5y"], None, "A", {"mgr_products_ann": 4.0}),
        ],
        "long_bond": [
            ("S0 基线",            [],        None, "A"),
            ("D16a tenure→0 (B1-A)", ["mgr_tenure"],   None, "A"),
            ("D16b rank_stab→0 (B1-A)", ["rank_stab"], None, "A"),
            ("D17 repair→0 敏感性 (B1-A)", ["repair_5y"], None, "A"),
            ("D10A winrate→0 (B1-A)", ["winrate_m_5y"], None, "A"),
            ("TR 两因子 (B1-A)",    ["mgr_tenure", "rank_stab"], None, "A"),
            ("TRW 三因子 (B1-A)",   ["mgr_tenure", "rank_stab", "winrate_m_5y"], None, "A"),
            ("TRW 三因子 (B1-B)",   ["mgr_tenure", "rank_stab", "winrate_m_5y"], None, "B"),
            ("B2 tenure→career",    ["mgr_tenure"], ("mgr_tenure", "mgr_career"), "A"),
            # —— D9/D10 拍板件 ——
            ("D9 products→4 (B1-A)", [], None, "A", {"mgr_products_ann": 4.0}),
            ("D10B winrate→2 (B1-A)", [], None, "A", {"winrate_m_5y": 2.0}),
            ("D9+D10A 全清 (B1-A)", ["winrate_m_5y"], None, "A", {"mgr_products_ann": 4.0}),
        ],
    }

    rows, alloc = [], []
    for pool in DEBT_POOLS:
        ic_keys = ic_keys_of(pool)
        w_a = base_w(pool)
        for row in SCEN[pool]:
            label, zero_keys, intra, mode = row[:4]
            shrink = row[4] if len(row) > 4 else None
            w = build_scenario(w_a, ic_keys, zero_keys, intra, mode,
                               train_ic[pool], shrink)
            node_res = []
            for T in sorted({t for (p, t) in scored if p == pool}):
                funds = scored[(pool, T)]
                win = pd.Series({f["code"]: f.get("win") for f in funds}, dtype=bool)
                fwd = pd.Series({f["code"]: f.get("fwd_1y") for f in funds}, dtype=float)
                sc = {}
                for f in funds:
                    sub = f.get("sub_scores") or {}
                    tot = ew = 0.0
                    for k, wt in w.items():
                        if wt <= 0:
                            continue
                        s_ = sub.get(k)
                        if s_ is None or (isinstance(s_, float) and np.isnan(s_)):
                            continue
                        tot += wt * s_ / 100.0
                        ew += wt
                    v = round(tot * 100 / ew, 2) if ew else None
                    if v is not None and f.get("redlines"):
                        v = min(v, C.REDLINES["score_cap"])
                    sc[f["code"]] = v
                sc = pd.Series(sc, dtype=float)
                ok = sc.notna() & fwd.notna()
                if int(ok.sum()) < MIN_PAIRS:
                    continue
                top = sc[ok].rank(pct=True) >= 0.8
                fwd_pct = fwd[ok].rank(pct=True)
                med_f = fwd[ok].median()
                node_res.append({
                    "T": T,
                    "win": float(win[ok][top].mean()),
                    "exc": float((fwd[ok][top] - med_f).mean()),
                    "fwdpct": float(fwd_pct[top].mean()),
                })
            ndf = pd.DataFrame(node_res)
            if ndf.empty:
                print(f"  ! {pool} {label}: 无有效节点，跳过")
                continue
            tr = ndf[ndf["T"].isin(TRAIN_NODES_SET)]
            va = ndf[ndf["T"].isin(VALID_NODES_SET)]
            max_w = max(w.values())
            rows.append({
                "pool": pool, "scenario": label,
                "win_train": tr["win"].mean() if len(tr) else np.nan,
                "win_valid": va["win"].mean() if len(va) else np.nan,
                "exc_valid": va["exc"].mean() if len(va) else np.nan,
                "fwdpct_valid": va["fwdpct"].mean() if len(va) else np.nan,
                "n_valid": len(va),
                "sum_w": round(sum(w.values()), 2),
                "max_w": round(max_w, 2),
                "cap_hit": bool(max_w > CAP + 1e-6),
            })
            if label != "S0 基线" and any(t in label for t in ("TRP", "TRW", "D16a", "D9", "D10")):
                for k in sorted(w, key=lambda x: -(w[x] - w_a.get(x, 0.0))):
                    if abs(w[k] - w_a.get(k, 0.0)) > 1e-6:
                        alloc.append({"pool": pool, "scenario": label, "factor": k,
                                      "before": round(w_a.get(k, 0.0), 2),
                                      "after": round(w[k], 2)})
        print(f"{pool} 完成 {time.time()-t0:.0f}s")

    res = pd.DataFrame(rows)
    res.to_csv(ROOT / "data" / "rrealloc_replay.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(alloc).to_csv(ROOT / "data" / "rrealloc_alloc.csv", index=False,
                               encoding="utf-8-sig")
    pd.set_option("display.width", 200)
    for pool in DEBT_POOLS:
        print(f"\n=== {'短债' if pool=='short_bond' else '长债'}：训练/验证段组合表现 ===")
        print(res[res["pool"] == pool].drop(columns="pool").to_string(index=False))
    print("\n=== 转出分配表（关键场景 before→after）===")
    print(pd.DataFrame(alloc).to_string(index=False))
    print(f"\n口径说明：win=前20%跑赢池中位胜率；exc=前20%对池中位超额（1y）；"
          f"fwdpct=未来池内分位均值；验证段=后4节点（df≈3，t<4 属边缘）。"
          f"\n用时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
