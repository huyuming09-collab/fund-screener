# -*- coding: utf-8 -*-
"""一次性回填：metrics_cache 费率组件字段（purchase_fee/purchase_fee_disc/mgmt_fee/...）。

背景：fetch_f10 升级 v2 缓存后新增申购费率字段，但 metrics_cache 里的旧记录只有
fee_total。本脚本对短债池全部份额重抓 F10 概况页（8 线程），把费率组件写回记录，
原子重写 metrics_cache.pkl。收藏费率变更频率低，跑一次即可；日常由周更
refresh_pool_metrics 重算时自然携带。
"""
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


def main() -> int:
    recs = pickle.loads(CACHE.read_bytes())
    todo = [r for r in recs if r.get("pool") == "short_bond"]
    print(f"短债池 {len(todo)} 份额 / 总 {len(recs)} 条记录，开始重抓 F10 …", flush=True)
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
