# -*- coding: utf-8 -*-
"""一次性回填：metrics_cache 费率组件字段（purchase_fee/purchase_fee_disc/mgmt_fee/...）。

背景：fetch_f10 升级 v2 缓存后新增申购费率字段，但 metrics_cache 里的旧记录只有
fee_total。本脚本对**费率字段缺失的记录**（不限池——短债/中长债都可能缺）重抓 F10 概况页
（8 线程），把费率组件写回记录，原子重写 metrics_cache.pkl。费率变更频率低，跑一次即可；
日常由周更 refresh_pool_metrics 重算时自然携带（crawl 的 f10_v2 缓存 7 天过期后也会再抓）。

用法：
    python scripts/backfill_fees.py                     # 全部缺费率的记录
    python scripts/backfill_fees.py --pools long_bond   # 只回填指定池
    python scripts/backfill_fees.py --dry-run           # 只看数量不抓取
"""
import argparse
import pickle
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))          # 允许 python scripts/backfill_fees.py 直跑

from fundscorer import data
from fundscorer.scorer import _sum_fee

CACHE = ROOT / "data" / "metrics_cache.pkl"
FIELDS = ("mgmt_fee", "custody_fee", "sales_fee", "purchase_fee", "purchase_fee_disc")
# 判定"缺失"只看这两个锚点字段：mgmt/custody 缺 = F10 费率块没抓到；
# sales_fee=None（A 类不收销售服务费）与 purchase_fee_disc=None（无优惠行）是**合法取值**，
# 拿它们当缺失会白抓几千条（实测 5107 条命中、真缺口仅 ~11 条）。
CORE_FIELDS = ("mgmt_fee", "purchase_fee")


def _missing_fee(rec: dict) -> bool:
    """锚点费率字段缺失即需回填（None 判定；0.0 是合法值不可当缺失）。"""
    return any(rec.get(k) is None for k in CORE_FIELDS)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="回填 metrics_cache 费率组件字段")
    ap.add_argument("--pools", default=None,
                    help="只回填这些池（逗号分隔，如 short_bond,long_bond）；默认全部")
    ap.add_argument("--dry-run", action="store_true", help="只统计数量，不抓取")
    args = ap.parse_args(argv)

    recs = pickle.loads(CACHE.read_bytes())
    todo = [r for r in recs if _missing_fee(r)]
    pools = [p for p in (args.pools or "").split(",") if p]
    if pools:
        todo = [r for r in todo if r.get("pool") in pools]
    n_pools = {p: sum(1 for r in todo if r.get("pool") == p) for p in sorted(
        {r.get("pool") for r in todo})}
    print(f"缺费率记录 {len(todo)} / 总 {len(recs)}（按池 {n_pools}）", flush=True)
    if args.dry_run:
        return 0
    if not todo:
        print("无需回填", flush=True)
        return 0

    print("开始重抓 F10 …", flush=True)
    t0 = time.time()
    ok, fail = 0, 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(data.fetch_f10, r["code"]): r for r in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            rec = futs[fut]
            try:
                f10 = fut.result()
                for k in FIELDS:
                    rec[k] = f10.get(k)
                rec["fee_total"] = data_scorer_sum(f10)
                ok += 1
            except Exception as e:
                fail += 1
                print(f"  [失败] {rec['code']}: {e}", flush=True)
            if i % 200 == 0:
                print(f"  {i}/{len(todo)} 成功 {ok} 失败 {fail} "
                      f"已用 {(time.time()-t0)/60:.1f}min", flush=True)
    tmp = Path(str(CACHE) + ".tmp")
    tmp.write_bytes(pickle.dumps(recs, protocol=pickle.HIGHEST_PROTOCOL))
    tmp.replace(CACHE)
    print(f"完成：成功 {ok} 失败 {fail}，总耗时 {(time.time()-t0)/60:.1f}min；"
          f"metrics_cache 已原子更新", flush=True)
    return 0


def data_scorer_sum(f10: dict) -> float | None:
    parts = [f10.get("mgmt_fee"), f10.get("custody_fee"), f10.get("sales_fee")]
    vals = [p for p in parts if p is not None]
    return sum(vals) if vals else None


if __name__ == "__main__":
    raise SystemExit(main())
