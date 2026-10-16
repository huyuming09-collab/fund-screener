# -*- coding: utf-8 -*-
"""全池评分 + 榜单生成。

用法：
    python -m fundscorer.rank            # 全量评分，输出各池高分榜
    python -m fundscorer.rank --top 20   # 每池取前 20（默认）
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from .batch import collect_all, load_universe
from .scorer import POOL_LABEL, score_pool, save_snapshot

OUT_DIR = Path(__file__).resolve().parent.parent / "data"
REPORT_COLS = [
    "pool", "code", "name", "score_capped", "star",
    "ret_5y", "mdd_5y", "vol_5y", "lvi_5y", "repair_days",
    "excess_5y", "yearly_win", "mgr_tenure_days", "mgr_career_years",
    "mgr_products_ann", "fee_total", "ms_rating", "inst_ratio", "scale_yi",
    "age", "redlines",
]


def build_report(results: list[dict]) -> pd.DataFrame:
    rows = []
    for r in results:
        rep = r.get("repair_5y", {})
        yearly = r.get("yearly", {})
        yearly_win = (sum(1 for v in yearly.values() if v > 0) / len(yearly)) if yearly else None
        subs = r.get("sub_scores", {})
        rows.append({
            "pool": POOL_LABEL.get(r["pool"], r["pool"]),
            "code": r["code"], "name": r["name"],
            "score_capped": r.get("score_capped"), "star": r.get("star"),
            # 入榜资格 = 有星 = 满 3 年（回测验证域；<3 年打分仅展示、不入推荐榜）
            "in_board": "是" if r.get("star") is not None else "否",
            "ret_5y": _pct(r.get("ret_5y")), "mdd_5y": _pct(r.get("mdd_5y")),
            "vol_5y": _pct(r.get("vol_5y")),
            "lvi_5y": round(r["lvi_5y"], 3) if r.get("lvi_5y") is not None else None,
            "repair_days": rep.get("repair_days"),
            "excess_5y": _pct(r.get("excess_5y")),
            "yearly_win": _pct(yearly_win),
            "mgr_tenure_days": r.get("mgr_tenure_days"),
            "mgr_career_years": r.get("mgr_career_years"),
            "mgr_products_ann": _pct(r.get("mgr_products_ann")),
            "fee_total": round(r["fee_total"] * 100, 2) if r.get("fee_total") is not None else None,
            "ms_rating": r.get("ms_rating"), "inst_ratio": _pct(r.get("inst_ratio")),
            "scale_yi": r.get("scale_yi"), "age": round(r.get("age") or 0, 1),
            "redlines": "；".join(r.get("redlines", [])),
            "_lvi_score": subs.get("lvi_5y"), "_mdd_score": subs.get("mdd_5y"),
        })
    return pd.DataFrame(rows)


def _pct(v, nd=2):
    return round(v * 100, nd) if v is not None else None


def print_pool_top(df: pd.DataFrame, top: int) -> None:
    for pool in ["纯债型·短债池", "纯债型·中长期纯债池", "固收+池"]:
        all_ = df[df["pool"] == pool].copy()
        if all_.empty:
            continue
        # 推荐榜只出 ≥3 年（有星）基金：<3 年回测从未验证过，只在全量明细中展示
        sub = all_[all_["star"].notna()].sort_values("score_capped", ascending=False)
        n_young = len(all_) - len(sub)
        out = sub.head(top)
        print(f"\n{'='*100}\n{pool}（共 {len(all_)} 只，可入榜 {len(sub)} 只"
              f"（满3年有星），<3年 {n_young} 只不入榜；下列前 {len(out)}）")
        if out.empty:
            continue
        show = out[["code", "name", "score_capped", "star", "ret_5y", "mdd_5y",
                    "lvi_5y", "repair_days", "fee_total", "ms_rating", "inst_ratio",
                    "scale_yi", "age", "redlines"]].copy()
        show.columns = ["代码", "名称", "总分", "星", "5y年化%", "5y回撤%", "LVI",
                        "修复天", "费率%", "晨星", "机构%", "规模亿", "成立年", "红线"]
        print(show.to_string(index=False))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args(argv)

    uni = load_universe()
    print(f"全集 {len(uni)} 只，开始计算指标（读缓存）...", flush=True)
    collected = collect_all(uni, cache_path=OUT_DIR / "metrics_cache.pkl")
    print(f"成功采集 {len(collected)} 只，开始全池分位打分...", flush=True)
    import traceback
    try:
        results = score_pool(collected)
    except Exception:
        traceback.print_exc()
        raise
    print(f"打分完成 {len(results)} 只。", flush=True)

    snap = save_snapshot(results, OUT_DIR / "full_scores.json")
    print(f"完整结果已存 {snap}")

    df = build_report(results)
    df.to_csv(OUT_DIR / "full_report.csv", index=False, encoding="utf-8-sig")
    print(f"报告 CSV 已存 {OUT_DIR/'full_report.csv'}")

    print_pool_top(df, args.top)


if __name__ == "__main__":
    main()
