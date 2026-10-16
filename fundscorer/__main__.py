# -*- coding: utf-8 -*-
"""命令行入口。

用法：
    python -m fundscorer 110017 006980 009689 160323
    python -m fundscorer 110017 --json out.json --snapshot
"""
import argparse
import json
import sys

from . import score_funds
from .scorer import POOL_LABEL, save_snapshot


def main(argv=None):
    ap = argparse.ArgumentParser(prog="fundscorer", description="债券类基金评分器（纯债/固收+）")
    ap.add_argument("codes", nargs="+", help="基金代码，如 110017 006980")
    ap.add_argument("--json", metavar="PATH", help="结果另存为 JSON 文件")
    ap.add_argument("--snapshot", action="store_true", help="保存评分快照（历史不覆盖）")
    args = ap.parse_args(argv)

    results = score_funds(args.codes)
    results.sort(key=lambda r: (r["pool"], -(r.get("score_capped") or 0)))

    # ---- 终端表格
    print(f"\n{'代码':<8}{'名称':<16}{'池':<14}{'总分':>7}{'星级':>6}  红线/备注")
    print("-" * 88)
    for r in results:
        star = "★" * r["star"] if r.get("star") else "—"
        notes = "；".join(r.get("redlines", []) + r.get("flags", []))
        name = (r["name"] or "")[:14]
        pool = POOL_LABEL.get(r["pool"], r["pool"])
        score = f"{r['score_capped']:.1f}" if r.get("score_capped") is not None else "N/A"
        print(f"{r['code']:<8}{name:<16}{pool:<14}{score:>7}{star:>6}  {notes}")

    print("\n维度得分（百分制子分）：")
    for r in results:
        subs = r.get("sub_scores", {})
        keys = ["ret_5y", "mdd_5y", "repair_5y", "lvi_5y", "yearly_5y", "fee", "mgr_products_ann"]
        brief = " ".join(f"{k}={subs.get(k)}" for k in keys if subs.get(k) is not None)
        print(f"  {r['code']} {brief}")

    # ---- JSON 输出
    payload = json.dumps(results, ensure_ascii=False, indent=2, default=str)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            f.write(payload)
        print(f"\n已写入 {args.json}")
    else:
        print("\n--- JSON ---")
        print(payload)

    if args.snapshot:
        p = save_snapshot(results)
        print(f"快照已保存：{p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
