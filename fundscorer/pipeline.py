# -*- coding: utf-8 -*-
"""周更管线：刷新数据 → 两段筛选 → 筛后评分 → TOP_N → 导出（CSV + 卡片数据）。

设计要点（docs/08-周更与卡片.md，多池扩展见 docs/09-长债筛选与评分.md）：
  1. 按 config.PIPELINE.pools 逐池处理（中短债 + 长债 + 固收+），不跑全市场打分；
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

from . import batch, cards, config as C, data as D
from . import indexfund as IF
from . import screen as S
from .scorer import score_pool, score_index_pool

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CSV_PATH = DATA / "screen_report.csv"
JSON_PATH = DATA / "top20.json"
JS_PATH = ROOT / "web" / "data.js"
HISTORY_PATH = DATA / "rank_history.json"      # 历期榜单（环比基准，本地文件）
HISTORY_KEEP = 26                              # 只留最近 26 期
# 随池统计下发给页面的筛选阈值（详情页"入选依据/筛选阈值"文案按池显示，不硬编码）
SCREEN_KEYS = ("scale_min_yi", "comp_pct_min", "yearly_median_min", "spike_max_gain",
               "mdd_5y_max", "mdd_5y_max_qualified", "vol_5y_min", "vol_5y_max",
               "ret_5y_max")   # 固收+专属键：缺失即不下发（两债池漏斗/页面零影响）
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


def _is_index(r: dict) -> bool:
    """份额记录是否为指数基金（按跟踪标的/费率/公司规模评估，不看基金经理业绩）。"""
    return str(r.get("type_text") or "").startswith("指数型")


def _is_offsite(r: dict) -> bool:
    """场外可买（用户 2026-10-22 拍板：榜单只筛支付宝可买的场外份额）。
    场内 ETF = 名称含 "ETF" 且非 "ETF联接" → 剔除；LOF 与 ETF联接为场外、保留。"""
    nm = str(r.get("name") or "")
    return not ("ETF" in nm and "ETF联接" not in nm)


def _index_meta(idx_recs: list[dict]) -> dict:
    """指数候选穿插所需份额级元数据：费率、跟踪基准、公司非货规模、名称（同指数去重排序键 +
    v5.11 族分类/久期段展示字段的输入）。"""
    cs = batch._load_company_scale()
    out: dict[str, dict] = {}
    for r in idx_recs:
        code = r.get("code")
        scale = None
        if cs is not None and code in cs.index:
            scale = float(cs.loc[code])
        out[code] = {"fee_total": r.get("fee_total"),
                     "benchmark": r.get("benchmark") or "",
                     "company_scale": scale,
                     "name": r.get("name") or ""}
    return out


def _build_index_list(idx_rows, idx_recs, idx_meta, idx_snap, pool, log, tag):
    """债指单列榜行（v5.12 display=separate，用户 2026-10-21 拍板，docs/17 §9）。

    规则：① 同一跟踪指数只展示 1 只——组内按 费率↑ → 公司非货规模↑ → 跟踪偏离↓
    （|ret_3y − 同组中位|，组≥3 可算）取第 1（IF.pick_per_index）；
    ② 按指数评估卡真分（score_capped）降序截 INDEX.list_top_n；
    ③ 每只挂「同指数其他优秀基金」×3（同指数基金级卡分次高者，排除自己）。
    行不进主动 res / CSV / 历史：主动 TOP20 与 INDEX-off 逐位相同（零位移不变量）。
    返回 (DataFrame, 归一后候选数)。"""
    # ---- 基金级同源映射（候选行与跟踪偏离共用一套分组，防 idx 口径不一致） ----
    base_bm, base_info = {}, {}
    for r in idx_recs:
        b = S.base_name(r["name"])
        bm = (r.get("benchmark") or "").strip()
        if bm and not base_bm.get(b):
            base_bm[b] = bm
        info = base_info.setdefault(b, {"name": r.get("name"), "ret": None, "fee": None})
        if info["ret"] is None and r.get("ret_3y") is not None:
            info["ret"] = r.get("ret_3y")
        fee = r.get("fee_total")
        if fee is not None and (info["fee"] is None or fee < info["fee"]):
            info["fee"] = fee
    base_mid = {b: IF.index_id(base_bm.get(b, ""), info["name"] or "")
                for b, info in base_info.items()}
    dev_map = IF.track_dev([(b, base_mid[b], info["ret"])
                            for b, info in base_info.items()])
    # ---- ① 同指数归一（候选行挂 meta / 跟踪偏离） ----
    rows = []
    for _, r in idx_rows.iterrows():
        m = (idx_meta.get(str(r["code"]))
             or idx_meta.get(str(r.get("rep_code"))) or {})
        b = r["base"]
        rows.append({**r.to_dict(),
                     "idx": base_mid.get(b, ""),
                     "fee": m.get("fee_total") if m.get("fee_total") is not None
                     else base_info.get(b, {}).get("fee"),
                     "cs": m.get("company_scale"),
                     "dev": dev_map.get(b)})
    picks = IF.pick_per_index(rows)
    # ---- ② 评估卡真分排序（v5.14：list_top_n=0/None → 全显示，用户拍板去截断） ----
    picks.sort(key=lambda r: (-(r.get("score") or float("-inf")),
                              str(r.get("code") or "")))
    # ---- ②′ 回撤护栏闸（v5.17，用户 2026-10-22 拍板）：组内 mdd_3y > 中位×risk_gate_x
    #      → 组内沉底（分数不动、名次后置）。缺 mdd_3y 或组<3 只 → 不闸。
    _gx = float(C.INDEX.get("risk_gate_x") or 0)
    _gated = 0
    if _gx > 0:
        _mdds = sorted(r["mdd_3y"] for r in picks if r.get("mdd_3y") is not None)
        if len(_mdds) >= 3:
            _med = (_mdds[len(_mdds) // 2] if len(_mdds) % 2
                    else (_mdds[len(_mdds) // 2 - 1] + _mdds[len(_mdds) // 2]) / 2)
            _thr = _med * _gx
            _keep = [r for r in picks
                     if r.get("mdd_3y") is None or r["mdd_3y"] <= _thr]
            _demoted = [r for r in picks
                        if r.get("mdd_3y") is not None and r["mdd_3y"] > _thr]
            if _demoted:
                picks = _keep + _demoted
                _gated = len(_demoted)
    _top_n = int(C.INDEX.get("list_top_n") or 0)
    show = picks[:_top_n] if _top_n > 0 else picks
    # ---- ③ 同指数其他优秀基金 ×3（全部已打分指数基金 → 基金级取最好份额 → 同组卡分次高） ----
    best: dict[str, dict] = {}
    for code, rec in (idx_snap or {}).items():
        if rec.get("score_capped") is None:
            continue
        nm = rec.get("name") or ""
        b = S.base_name(nm)
        if b not in base_mid:
            continue
        row = {"base": b, "idx": base_mid[b], "code": code, "name": nm,
               "score": rec.get("score_capped"), "fee_total": rec.get("fee_total"),
               "company_scale": rec.get("company_scale"), "ret_3y": rec.get("ret_3y")}
        cur = best.get(b)
        if cur is None or (-(row["score"] or -1), str(code)) < \
                (-(cur["score"] or -1), str(cur["code"])):
            best[b] = row
    out_rows = []
    for i, r in enumerate(show, 1):
        same = sorted((f for f in best.values()
                       if f["idx"] == r.get("idx") and f["base"] != r["base"]),
                      key=lambda x: (-(x["score"] or -1), str(x["code"])))
        row = dict(r)
        row["rank"] = i
        row["is_index"] = True
        row["score_basis"] = None
        row["rank_prev"] = None
        row["rank_delta"] = None
        row["is_new"] = False
        row["idx_group"] = pool
        row["index_peers"] = [{k: p[k] for k in
                               ("code", "name", "score", "fee_total",
                                "company_scale", "ret_3y")} for p in same[:3]]
        out_rows.append(row)
    df = pd.DataFrame(out_rows)
    if not df.empty:
        df["idx_group_n"] = len(df)
    log(f"[5a][{tag}] 债指单列榜：候选 {len(idx_rows)} → 同指数归一 {len(picks)}"
        f" → 展示 {len(df)}（选中键 fee低→公司规模大→跟踪偏离小；评估卡真分排序；"
        f"回撤闸沉底 {_gated} 只（中位×{_gx:g}）；偏离可算基金 {len(dev_map)} 只）")
    return df, len(picks)


def _screen_pool(pool: str, top_n: int, asof: str | None, log,
                 refresh: bool = False) -> dict:
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
    # 指数基金与主动债基分流（用户 2026-10-19 决议：指数不看经理业绩，只看跟踪标的/费率/规模）
    act_recs = [r for r in records if not _is_index(r)]
    idx_all = [r for r in records if _is_index(r)]
    idx_recs = [r for r in idx_all if _is_offsite(r)]   # v5.17 场外化：场内 ETF 出局
    n_onsite = len(idx_all) - len(idx_recs)
    log(f"[2][{tag}] 读取池记录 {len(records)} 份额"
        + (f"（主动 {len(act_recs)} / 指数 {len(idx_recs)}"
           + (f"，剔除场内 ETF {n_onsite} 只" if n_onsite else "") + "）"
           if idx_recs else ""))

    # ---- 3. 两段筛选（先筛；指数侧跳过 S2/S3 经理闸） ----
    candidates, funnel = S.screen_candidates(act_recs, spikes, yearly, cfg)
    log(f"[3][{tag}] 筛选漏斗：" + " → ".join(f"{k} {v}" for k, v in funnel.items()))
    if len(candidates) < top_n:
        log(f"[警告][{tag}] 候选 {len(candidates)} 只 < TOP{top_n}！"
            "阈值为已定方法论不自动放宽，请检查数据或评估调整 SCREEN 阈值")
    idx_cand = None
    if idx_recs:
        idx_cand, idx_funnel = S.screen_candidates(
            idx_recs, spikes, yearly, cfg, index_fund=True)
        log(f"[3][{tag}] 指数候选漏斗：" + " → ".join(f"{k} {v}" for k, v in idx_funnel.items()))

    # ---- 4. 筛后评分（只评候选；分位基准=本池全池 → 与全市场分数逐位一致） ----
    # 指数基金单独打分：其分位不参与主动榜（主动榜不与指数同池比经理指标），
    # 分数最终会被"业绩插值"覆盖，此处仅用于取指标与星级配额。
    cand_codes = {c for codes in candidates["codes"] for c in codes}
    quota_n = sum(1 for r in act_recs if not r.get("insufficient_history"))
    scored = score_pool(act_recs, score_only=cand_codes, quota_n=quota_n)
    # 申购状态（2026-10-20 用户要求：暂停申购在卡片标注）：刷新日联网补缺失，平时只读缓存
    sgzt = D.fetch_purchase_status(sorted(cand_codes), refresh=refresh)
    for r in scored:
        r["purchase_status"] = sgzt.get(r["code"])
    snapshot = {r["code"]: r for r in scored if "score" in r
                and not r.get("_unscored")}
    n_ok = sum(1 for c in cand_codes if snapshot.get(c, {}).get("score_capped") is not None)
    log(f"[4][{tag}] 筛后评分：候选份额 {len(cand_codes)}，得分 {n_ok}，"
        f"星级配额基数 {quota_n}")
    if n_ok == 0:
        raise RuntimeError(f"{tag} 候选全部打分失败")

    idx_rows, idx_meta = None, {}
    if not C.INDEX.get("enabled"):
        idx_cand = None          # 债指不入榜：不评分、不穿插（总开关见 config.INDEX["enabled"]，docs/11 §零）
    if idx_cand is not None and len(idx_cand):
        idx_codes = {c for codes in idx_cand["codes"] for c in codes}
        idx_quota = sum(1 for r in idx_recs if not r.get("insufficient_history"))
        # 费率/跟踪基准/公司非货规模/名称：同指数去重排序键 + 族分类输入（v5.11）
        idx_meta = _index_meta(idx_recs)
        # v5.11 D-I2：指数评估卡（族内相对、无经理因子）替代主动卡打分；
        # 公司非货规模注入其"池内绝对分位"因子（缺 → 载分折算）。
        # v5.12 单列模式：全部指数基金打分（详情页「同指数其他优秀基金」需候选外基金真分）；
        # merged 模式维持只评候选（分数仅供插值取指标）。
        _sep = C.INDEX.get("display") == "separate"
        # v5.14（用户 2026-10-21 拍板）：basis=本池主动候选 → 债指分位与主动榜同尺，
        # "业绩差不多的分数也差不多"；无 basis 回退 v5.11 族内相对（测试/退化路径）。
        idx_scored = score_index_pool(
            idx_recs, score_only=None if _sep else idx_codes, quota_n=idx_quota,
            company_scale={c: m["company_scale"] for c, m in idx_meta.items()
                           if m.get("company_scale") is not None},
            basis=[r for r in act_recs if r.get("score") is not None
                   and not r.get("_unscored")])
        idx_snap = {r["code"]: r for r in idx_scored if "score" in r
                    and not r.get("_unscored")}
        # 同指数≤2 去重键与公司规模入快照（"需可核对"），并带 v5.11 族/久期段展示字段
        for _c, _m in idx_meta.items():
            _row = idx_snap.setdefault(_c, {})
            if _m.get("company_scale") is not None:
                _row["company_scale"] = _m["company_scale"]
            _fam, _dur = IF.index_family(_m.get("benchmark", ""), _m.get("name", ""))
            _row["idx_fam"], _row["idx_dur"] = _fam, _dur
        snapshot.update(idx_snap)              # 详情页取指标用（build_fund 按 code 查快照）
        idx_rows = S.rank_candidates(idx_cand, idx_snap, cfg, top_n=None)

    # ---- 5. TOP-N 排序截取 ----
    res = S.rank_candidates(candidates, snapshot, cfg, top_n=top_n)
    if res.empty:
        raise RuntimeError(f"{tag} 排序结果为空")

    # ---- 5a. 指数基金：单列（v5.12，默认）或穿插（merged，休眠回退路径） ----
    idx_stats = {}
    idx_list, idx_n_pick = None, 0
    if idx_rows is not None and C.INDEX.get("display") == "separate":
        # v5.12 用户 2026-10-21 拍板「单列上线」：指数行不进主动榜 → 主动 TOP20 与
        # INDEX-off 逐位相同（零位移不变量）；独立「债指」页签按组展示
        idx_list, idx_n_pick = _build_index_list(idx_rows, idx_recs, idx_meta,
                                                 idx_snap, pool, log, tag)
    elif idx_rows is not None:
        # v5.11 H1′：全体已评分主动候选 = 分支 B（业绩越下界）的锚点池（docs/17 §4.3）
        full_anchors = S.rank_candidates(candidates, snapshot, cfg, top_n=None)
        res, idx_stats = IF.interleave(idx_rows, res, idx_meta, top_n=top_n,
                                       all_anchors=full_anchors)
        if idx_stats["candidates"]:
            in_list = res[res["is_index"]] if "is_index" in res.columns \
                else res.iloc[0:0]
            br = idx_stats.get("branches") or {}
            log(f"[5a][{tag}] 指数穿插：候选 {idx_stats['candidates']}"
                f"（跟踪指数 {idx_stats['ids']} 个，同指数去重挤出 {idx_stats['capped']}，"
                f"分支 A/B/C {br.get('A', 0)}/{br.get('B', 0)}/{br.get('C', 0)}）"
                f"→ 入榜 {len(in_list)} 只；入榜者："
                + ("、".join(f"{int(r['rank'])}.{r['name']}"
                             for _, r in in_list.iterrows()) or "无"))
    # 逐池自备该列：下游按 pd.concat 合并各池，缺列行会补 NaN（bool(np.nan)=True → 污染）
    if "is_index" not in res.columns:
        res["is_index"] = False
    if "score_basis" not in res.columns:        # 仅指数行有值；主动行 None
        res["score_basis"] = None
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
    # v5.5 风格标签基准：本池候选中位（主动候选、有 5 年数据；仅展示不计分，docs/12 §8）
    _cand_recs = [r for c, r in snapshot.items() if c in cand_codes]
    _rets = [r.get("ret_5y") for r in _cand_recs if r.get("ret_5y") is not None]
    _vols = [r.get("vol_5y") for r in _cand_recs if r.get("vol_5y") is not None]
    style_base = {"ret_5y": pd.Series(_rets).median() if _rets else None,
                  "vol_5y": pd.Series(_vols).median() if _vols else None}
    return {"res": res, "funnel": funnel, "snapshot": snapshot, "navs": navs,
            "prev_ranks": prev_ranks,
            "style_base": style_base,
            # v5.12 单列债指：本池指数榜行（不进 res/CSV/历史，仅随 payload 下发）
            "idx_list": idx_list,
            "idx_screened": len(idx_cand) if idx_cand is not None else 0,
            "n_screened": len(candidates)
            + (len(idx_cand) if idx_cand is not None else 0)}


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
            out[pool] = _screen_pool(pool, top_n, asof, log, refresh=not skip_refresh)

        # ---- 6. 导出（全部池就绪后原子替换；历史最后写，失败不污染基准） ----
        stats = {p: {"n": len(out[p]["res"]),
                     "n_screened": out[p]["n_screened"],
                     "top_n": top_n,
                     "has_prev": out[p]["prev_ranks"] is not None,
                     "funnel": out[p]["funnel"],
                     # v5.5 风格标签基准（池内候选中位，仅展示）
                     "style_base": out[p].get("style_base"),
                     # 三闸键（mdd_5y_max 等）仅固收+有 → 缺失不下发，两债池契约零变化
                     "screen": {k: C.SCREENS[p][k] for k in SCREEN_KEYS
                                if k in C.SCREENS[p]}}
                 for p in pools}
        combined = pd.concat([out[p]["res"] for p in pools], ignore_index=True)

        # ---- 6a. 债指单列榜（v5.12 display=separate）：指数行不进 CSV/历史
        #      （主动榜与 INDEX-off 逐位相同），仅并入卡片 payload 的独立「债指」页签 ----
        idx_frames = [out[p]["idx_list"] for p in pools
                      if out[p].get("idx_list") is not None
                      and not out[p]["idx_list"].empty]
        cards_df = combined
        if idx_frames:
            idx_df = pd.concat(idx_frames, ignore_index=True, sort=False)
            idx_df["category"] = "index"
            stats["index"] = {"n": int(len(idx_df)),
                              "n_screened": int(sum(out[p]["idx_screened"] for p in pools)),
                              "top_n": int(C.INDEX.get("list_top_n") or 10),
                              "has_prev": False,               # 首期无历史，环比字段全空
                              "funnel": {},
                              # 详情「入选依据」文案（S1≥5亿 / P1″≥45% / P2″≥55%）；
                              # 尖峰阈值按来源池展示（详情用 idx_group 取 branch screen）
                              "screen": {"scale_min_yi": 5, "comp_pct_min": 0.45,
                                         "yearly_median_min": 0.55},
                              "style_base": None}              # 风格标签仅主动池有（v5.5）
            cards_df = pd.concat([combined, idx_df], ignore_index=True, sort=False)
            log(f"[6][债指] 单列榜随 payload 下发：共 {len(idx_df)} 只"
                + "、".join(f"{_POOL_NAMES.get(p, p)}{len(out[p]['idx_list'])}"
                            for p in pools if out[p].get("idx_list") is not None
                            and not out[p]["idx_list"].empty)
                + "（不进 CSV/历史，主动榜零位移）")

        CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_csv = Path(str(CSV_PATH) + ".tmp")
        csv_df = combined.copy()
        csv_df.insert(0, "pool", csv_df["category"])
        if "is_index" not in csv_df.columns:        # 源端 _screen_pool 已保证；此处兜底防导出中途崩溃
            csv_df["is_index"] = False
        cols = S.REPORT_COLS.copy()
        cols.insert(cols.index("name") + 1, "is_index")   # 紧邻名称：审阅时一眼分辨指数/主动
        csv_df[["pool"] + cols + ["rank_prev", "rank_delta"]].to_csv(
            tmp_csv, index=False, encoding="utf-8-sig")
        tmp_csv.replace(CSV_PATH)

        merged_snap = {k: v for p in pools for k, v in out[p]["snapshot"].items()}
        merged_navs = {k: v for p in pools for k, v in out[p]["navs"].items()}
        payload = cards.build_payload(cards_df, merged_snap, merged_navs,
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
