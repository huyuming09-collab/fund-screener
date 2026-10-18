# -*- coding: utf-8 -*-
"""周更管线：刷新数据 → 两段筛选 → 筛后评分 → TOP_N → 导出（CSV + 卡片数据）。

设计要点（docs/08-周更与卡片.md，多池扩展见 docs/09-中长债筛选与评分.md）：
  1. 按 config.PIPELINE.pools 逐池处理（短债 + 中长债），不跑全市场打分；
  2. collect_all 会复用陈旧缓存 → 本管线用 batch.refresh_pool_metrics 强制重算；
  3. nav_full 此前无写入代码 → crawl 后 batch.persist_nav 固化净值；
  4. 先筛后评：score_pool(score_only=候选份额, quota_n=该池全池规模)——
      分位基准仍是本池全池，候选分数与全市场快照逐位一致（tests 有等价性证明）；
  5. 按 config.TOP_N 每池各截取前 N（默认 20，两池合计 40 卡）；
  6. **全部池都算完才一次性原子替换正式产物**——任一池失败 = 整体失败、产物不动。

用法：
    python -m fundscorer.pipeline                  # 完整周更（双池）
    python -m fundscorer.pipeline --skip-refresh   # 不抓数据，用现有缓存重筛重评
    python -m fundscorer.pipeline --top 20         # 覆盖 TOP_N
"""
from __future__ import annotations

import argparse
import datetime as dt
import time
from pathlib import Path

import pandas as pd

from . import batch, cards, config as C
from . import screen as S
from .scorer import score_pool

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CSV_PATH = DATA / "screen_report.csv"
JSON_PATH = DATA / "top20.json"
JS_PATH = ROOT / "web" / "data.js"
HISTORY_PATH = DATA / "rank_history.json"      # 历期榜单（环比基准，本地文件）
HISTORY_KEEP = 26                              # 只留最近 26 期
# 随池统计下发给页面的筛选阈值（详情页"入选依据/筛选阈值"文案按池显示，不硬编码）
SCREEN_KEYS = ("scale_min_yi", "comp_pct_min", "yearly_median_min", "spike_max_gain",
               "mdd_5y_max", "vol_5y_min", "vol_5y_max", "ret_5y_max")   # 后四项仅固收+有（缺失即不下发）
# 池中文名（日志用）：与卡片分类 tab 同源，新增池时只改 config.CARD["categories"]
_POOL_NAMES = {c["id"]: c["name"] for c in C.CARD["categories"]}



class _Logger:
    """stdout + 日志文件双写。"""

    def __init__(self, path: Path | None):
        self.path = path
        self._fh = None
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8")

    def __call__(self, msg: str = ""):
        line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}" if msg else ""
        print(line or "", flush=True)
        if self._fh:
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self):
        if self._fh:
            self._fh.close()


def _is_nested(entry) -> bool:
    """历史一期的键结构是否为多池嵌套 {pool: {base: rank}}（否则是旧扁平 {base: rank}}）。"""
    return isinstance(entry, dict) and bool(entry) and \
        all(isinstance(v, dict) for v in entry.values())


def load_prev_ranks(path: Path | None = None,
                    today: str | None = None,
                    pool: str | None = None) -> dict | None:
    """环比基准：日期**严格早于** today 的最近一期 {base: rank}；无历史 → None。

    按基金 base 对比（份额代码可能变，base 不变）；同日重跑仍对比上周，显示稳定。

    键结构（docs/09 §D）：{date: {pool: {base: rank}}}，各池环比互不串池。
    旧扁平格式 {date: {base: rank}}（单池短债时代）按 short_bond 迁移读取——
    现存历史无损保留、短债环比不断档。
    pool=None 时：旧扁平期直接返回；嵌套期仅当只有一个池时自动取之（多池需显式指定）。
    """
    import json
    path = path or HISTORY_PATH
    if not path.exists():
        return None
    try:
        hist: dict = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    today = today or str(dt.date.today())
    prior = {d: v for d, v in hist.items() if d < today}
    if not prior:
        return None
    entry = prior[max(prior)]
    if not isinstance(entry, dict) or not entry:
        return None
    if _is_nested(entry):
        if pool is not None:
            return entry.get(pool) or None
        return next(iter(entry.values())) if len(entry) == 1 else None
    # 旧扁平格式（{base: rank}，全短债时代）
    return entry if pool in (None, "short_bond") else None


def save_history(res, path: Path | None = None, today: str | None = None,
                 pool: str = "short_bond") -> None:
    """导出成功后追加本期榜单（该池）；只保留最近 HISTORY_KEEP 期（原子写）。

    键结构 {date: {pool: {base: rank}}}；读到旧扁平期就地迁移为 short_bond，
    单次写入即完成全文件升级，历史不丢。
    """
    import json
    path = path or HISTORY_PATH
    today = today or str(dt.date.today())
    hist: dict = {}
    if path.exists():
        try:
            hist = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            hist = {}
    # 迁移旧扁平期 → {short_bond: {...}}
    hist = {d: (v if _is_nested(v) else {"short_bond": v})
            for d, v in hist.items() if isinstance(v, dict)}
    hist.setdefault(today, {})[pool] = {str(r["base"]): int(r["rank"])
                                        for _, r in res.iterrows()}
    for d in sorted(hist)[:-HISTORY_KEEP]:
        hist.pop(d, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def attach_rank_changes(res, prev_ranks: dict | None):
    """给榜单补 rank_prev / rank_delta / is_new 三列（环比展示用）。"""
    if res.empty:
        return res
    if prev_ranks is None:                      # 首期：全部无环比、不标新增
        res["rank_prev"] = None
        res["rank_delta"] = None
        res["is_new"] = False
        return res
    # Int64（可空）：新增基金为 pd.NA，CSV 导出为整数/空串而非 3.0
    res["rank_prev"] = res["base"].map(prev_ranks).astype("Int64")
    res["rank_delta"] = (res["rank_prev"] - res["rank"]).astype("Int64")
    res["is_new"] = res["rank_prev"].isna()
    return res


def refresh(pool: str, log) -> dict:
    """步骤 1~3：全集 → 并行抓取 → nav_full 固化 → 指标强制重算合并。"""
    t0 = time.time()
    uni = batch.build_universe()            # 基金列表缓存 3 天，周更自然过期
    uni = batch.load_universe()             # 再套公司规模排除（<300亿），与筛选口径一致
    # 刷新范围 = 池内全集（公司规模排除后）∪ 缓存中该池已有 code（口径与筛选一致，
    # 公司掉档/类型改判的存量基金不因漏刷而陈旧）
    import pickle
    codes = set(uni[uni["pool"] == pool]["code"].tolist())
    cache_path = DATA / "metrics_cache.pkl"
    if cache_path.exists():
        try:
            cached = pickle.loads(cache_path.read_bytes())
            codes |= {r["code"] for r in cached if r.get("pool") == pool}
        except Exception:
            pass
    codes = sorted(codes)
    log(f"[1/4] 全集 {pool} 池 {len(codes)} 只，开始抓取 nav/f10/inst …")

    cr = batch.crawl(codes)
    log(f"[2/4] 抓取完成：成功 {cr['ok']} 失败 {cr['fail']}"
        + (f"（如 {','.join(cr['failed_codes'][:5])}）" if cr["fail"] else ""))

    pv = batch.persist_nav(codes)
    log(f"[3/4] nav_full 固化：成功 {pv['ok']} 失败 {pv['fail']}")

    rm = batch.refresh_pool_metrics(codes, DATA / "metrics_cache.pkl", pool)
    log(f"[4/4] 指标重算：更新 {rm['refreshed']} 沿用旧值 {rm['stale_fallback']} "
        f"缺失 {rm['failed']} 耗时 {rm['minutes']}min")
    if cr["fail"] or rm["failed"]:
        log(f"[警告] 抓取/重算存在失败（陈旧回退 {rm['stale_fallback']} 只），"
            "失败清单见上；下周将自动重试")
    log(f"刷新阶段总耗时 {(time.time()-t0)/60:.1f}min")
    return rm


def _screen_pool(pool: str, top_n: int, asof: str | None, log) -> dict:
    """单池：筛选输入 → 两段筛选 → 筛后评分 → TOP-N → 环比。

    返回 {res（含 category 列）, funnel, snapshot, navs, prev_ranks, n_screened}；
    打分/排序失败抛出 RuntimeError（调用方中止且不动正式产物）。
    """
    cfg = C.SCREENS[pool]
    tag = _POOL_NAMES.get(pool, pool)

    # ---- 2. 筛选输入（缓存 + nav_full，按池过滤） ----
    records, spikes, yearly, navs = S.load_screen_inputs(asof=asof, cfg=cfg)
    n_missing = sum(1 for v in navs.values() if v is None)
    if n_missing:
        log(f"[警告][{tag}] {n_missing} 个份额无净值文件（S5 数据不足不放行）")
    log(f"[2][{tag}] 读取池记录 {len(records)} 份额")

    # ---- 3. 两段筛选（先筛） ----
    candidates, funnel = S.screen_candidates(records, spikes, yearly, cfg)
    log(f"[3][{tag}] 筛选漏斗：" + " → ".join(f"{k} {v}" for k, v in funnel.items()))
    if len(candidates) < top_n:
        log(f"[警告][{tag}] 候选 {len(candidates)} 只 < TOP{top_n}！"
            "阈值为已定方法论不自动放宽，请检查数据或评估调整 SCREEN 阈值")

    # ---- 4. 筛后评分（只评候选；分位基准=本池全池 → 与全市场分数逐位一致） ----
    cand_codes = {c for codes in candidates["codes"] for c in codes}
    quota_n = sum(1 for r in records if not r.get("insufficient_history"))
    scored = score_pool(records, score_only=cand_codes, quota_n=quota_n)
    snapshot = {r["code"]: r for r in scored if "score" in r
                and not r.get("_unscored")}
    n_ok = sum(1 for c in cand_codes if snapshot.get(c, {}).get("score_capped") is not None)
    log(f"[4][{tag}] 筛后评分：候选份额 {len(cand_codes)}，得分 {n_ok}，"
        f"星级配额基数 {quota_n}")
    if n_ok == 0:
        raise RuntimeError(f"{tag} 候选全部打分失败")

    # ---- 5. TOP-N 排序截取 ----
    res = S.rank_candidates(candidates, snapshot, cfg, top_n=top_n)
    if res.empty:
        raise RuntimeError(f"{tag} 排序结果为空")
    if len(res) < top_n:
        log(f"[警告][{tag}] 最终输出 {len(res)} 只 < TOP{top_n}")
    res["category"] = pool                # 逐池归属：cards.category / CSV 的 pool 列

    # ---- 5b. 环比上一期（该池无历史 → 首期口径：无环比、不标新增） ----
    prev_ranks = load_prev_ranks(pool=pool)
    res = attach_rank_changes(res, prev_ranks)
    if prev_ranks is None:
        log(f"[5b][{tag}] 无历史榜单（首期），本期不显示环比")
    else:
        n_new = int(res["is_new"].sum()) if len(res) else 0
        ups = int((res["rank_delta"] > 0).sum()) if len(res) else 0
        log(f"[5b][{tag}] 环比基准=上一期（{len(prev_ranks)} 只）：上升 {ups}，新增 {n_new}")
    return {"res": res, "funnel": funnel, "snapshot": snapshot, "navs": navs,
            "prev_ranks": prev_ranks, "n_screened": len(candidates)}


def run(skip_refresh: bool = False, top: int | None = None,
        asof: str | None = None) -> int:
    """周更主流程（按 config.PIPELINE.pools 逐池）。
    返回 0=成功（正式产物已更新），1=失败（正式产物未动）。"""
    top_n = top or C.TOP_N
    pools = list(C.PIPELINE["pools"])
    log_dir = Path(C.PIPELINE["log_dir"])
    log = _Logger(log_dir / f"pipeline_{dt.date.today():%Y%m%d}.log")
    t0 = time.time()
    try:
        log(f"==== 周更开始（池 {'+'.join(_POOL_NAMES.get(p, p) for p in pools)}，"
            f"TOP{top_n}/池，model {C.MODEL_VERSION}）====")

        # ---- 1. 逐池刷新数据 ----
        if skip_refresh:
            log("[1] --skip-refresh：跳过抓取，使用现有缓存")
        else:
            for pool in pools:
                refresh(pool, log)

        # ---- 2~5b. 逐池筛选评分（全部算完才进入导出，单池失败=整体失败） ----
        out: dict[str, dict] = {}
        for pool in pools:
            out[pool] = _screen_pool(pool, top_n, asof, log)

        # ---- 6. 导出（全部池就绪后原子替换；历史最后写，失败不污染基准） ----
        stats = {p: {"n": len(out[p]["res"]),
                     "n_screened": out[p]["n_screened"],
                     "top_n": top_n,
                     "has_prev": out[p]["prev_ranks"] is not None,
                     "funnel": out[p]["funnel"],
                     # 三闸键（mdd_5y_max 等）仅固收+有 → 缺失不下发，两债池契约零变化
                     "screen": {k: C.SCREENS[p][k] for k in SCREEN_KEYS
                                if k in C.SCREENS[p]}}
                 for p in pools}
        combined = pd.concat([out[p]["res"] for p in pools], ignore_index=True)

        CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_csv = Path(str(CSV_PATH) + ".tmp")
        csv_df = combined.copy()
        csv_df.insert(0, "pool", csv_df["category"])
        csv_df[["pool"] + S.REPORT_COLS + ["rank_prev", "rank_delta"]].to_csv(
            tmp_csv, index=False, encoding="utf-8-sig")
        tmp_csv.replace(CSV_PATH)

        merged_snap = {k: v for p in pools for k, v in out[p]["snapshot"].items()}
        merged_navs = {k: v for p in pools for k, v in out[p]["navs"].items()}
        payload = cards.build_payload(combined, merged_snap, merged_navs,
                                      pool_stats=stats)
        cards.write_payload(payload, JSON_PATH, JS_PATH)
        for pool in pools:                   # 导出成功后才写历史（失败不污染基准）
            save_history(out[pool]["res"], pool=pool)

        log(f"[6] 导出完成：{CSV_PATH.name}（{len(combined)} 行 = "
            + " + ".join(f"{_POOL_NAMES.get(p, p)}{len(out[p]['res'])}" for p in pools)
            + f"）、{JSON_PATH.name}、web/data.js")
        for pool in pools:
            log(f"   {_POOL_NAMES.get(pool, pool)}榜单："
                + ", ".join(f"{int(r['rank'])}.{r['name']}({r['score']:.1f})"
                            for _, r in out[pool]["res"].head(5).iterrows()) + " …")
        log(f"==== 周更完成，总耗时 {(time.time()-t0)/60:.1f}min ====")
        return 0
    except Exception as e:
        log(f"[错误] 管线异常中止：{type(e).__name__}: {e}（正式产物未动）")
        import traceback
        log(traceback.format_exc())
        return 1
    finally:
        log.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="周更管线：逐池刷新→筛选→筛后评分→TOP_N→导出")
    ap.add_argument("--skip-refresh", action="store_true",
                    help="跳过数据抓取，用现有缓存重筛重评（调试）")
    ap.add_argument("--top", type=int, default=None, help="输出前 N 只（默认 config.TOP_N）")
    ap.add_argument("--asof", default=None, help="筛选基准日 YYYY-MM-DD（默认今天）")
    args = ap.parse_args(argv)
    return run(skip_refresh=args.skip_refresh, top=args.top, asof=args.asof)


if __name__ == "__main__":
    raise SystemExit(main())
