# -*- coding: utf-8 -*-
"""偏股资深经理闸 v1（docs/20 §2.1；参数 2026-11-11 用户拍板）。

用户拍板（2026-11-11；2026-11-12 追加放宽）：
  · S1 池边界：股票型 + 混合型-偏股 + 混合型-灵活 + 混合型-平衡（QDII/指数/FOF/偏债不入）
  · S2 经理从业**双通道**：从业 >10 年全部入围；>6 年且 ≤10 年须"同类近5年年化超额**前20%**"
    （2026-11-12 用户定：S2 只是初筛可适当放宽，严格筛选留给后续评分阶段）；≤6 年出闸
  · S3 现任经理任本基金 ≥5 年；S4 基金净值 ≥5 年
  · 目标：只留最稳定的老将，**结果少是预期不是缺陷**（宁缺毋滥）

口径（docs/20 §2.1 同步）：
  · "同类" = 类型×**风格桶**（2026-11-12 用户质疑"是不是全筛成科技了"后加入）：
    B 通道分位在同类型同风格桶内取（消费和消费比、科技和科技比），样本 <10 只逐层
    回退（同桶全池 → 类型）。**为什么分桶**：数据实锤类型内比较系统性偏向近5年风口
    ——类型内前20%达标率 科技45% vs 消费4% vs 新能源0%（5年窗口恰是科技牛+消费熊），
    筛出的是风格 beta 不是经理能力；分桶后消费/医药/新能源老将回归、科技不再躺赢。
    **为什么放宽到 20%**：S2 是初筛（用户 2026-11-12），放行宽一点，严格线后续评分再收；
    放宽与分桶独立——p80 类型内达标率仍是 科技45% vs 消费4% vs 新能源0%，不分桶白放。
  · 分位人群 = 同格内净值≥5 年的全部池内基金（含从业<6 年经理的基金——分位是基金级
    质量指标，与经理从业无关）
  · 超额基准 = 类型合成（BENCH_W 股指权重，华泰口径；平衡型 50/50 为估设）：
    沪深300 价格指数 + 中债新综合财富。**同类内基准相同 → 超额排序 ≡ 原始收益排序**，
    闸按超额实现（数学上即收益排序），超额列仅供展示；沪深300 未计约 2%/年股息，
    只影响超额绝对值、不影响同类内排序
  · 任本基金年限自 F10 经理简历页上任日起算（与债券池 S3 同源，含回归接管修正）；
    **数据不足不猜测**：pair 缺从业/上任日即剔除并计入漏斗
  · 基金经理以 (姓名, 公司) 为键聚合——mgr_em 行级配对天然无重名歧义

运行：python -m fundscorer.eqgate [--workers 8]
输出：data/eq_gate_managers.csv（经理级榜单）/ eq_gate_funds.csv（通过 pair 明细）
      data/eq_gate_funnel.json + stdout 漏斗
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

from . import batch, data as D, metrics as M
from .scorer import _tenure_days

ROOT = Path(__file__).resolve().parent.parent

# ---- 闸参数（2026-11-11 用户拍板；改动属产品决策，须告知用户）----
POOL_TYPES = ("股票型", "混合型-偏股", "混合型-灵活", "混合型-平衡")   # S1
CAREER_DIRECT_Y = 10.0    # S2 通道A：从业 >10 年全部入围
CAREER_MIN_Y = 6.0        # S2 通道B 下限：>6 年才可走前20%；≤6 年出闸
TOP_PCT = 0.80            # S2 通道B：同类(类型×风格桶)近5年年化收益分位 ≥80%
                          # 2026-11-12 用户放宽：S2 是初筛，严格线留给后续评分阶段
TENURE_MIN_D = 1825       # S3 任本基金 ≥5 年（5×365，与债券池 S3=1095 同口径）
NAV_MIN_Y = 5.0           # S4 净值 ≥5 年
A_FLOOR_PCT = 10.0        # S2-C 垫底底线（2026-11-12 全量审计后新增，A 通道专属）：
                          # 同格收益排位后 10% → 出局。A 通道"直通但去尾"——数据实锤
                          # 不设底线会放进 69.3% 五年负收益、最差 -21.5% 年化的"老将"，
                          # 5 年同风格持续垫底 ≠ 短期跑输，与"最稳定老将"目标矛盾。
                          # B 通道要求前 20%，天然不沾此线。
# 类型合成基准的股指权重（华泰口径 90/70/50；平衡型 50/50 为估设）——仅影响展示的超额数值
BENCH_W = {"股票型": 0.90, "混合型-偏股": 0.70, "混合型-灵活": 0.50, "混合型-平衡": 0.50}

# ---- 风格分类引擎（2026-11-12 全量审计后从"名字关键词"升级为"净值相关性"）----
# 审计实锤：名字分桶与独立相关性分类一致率仅 45.9%，且"其他/均衡"大桶（占通过者 80%）
# 内部偏差原样存在（桶内 p80 达标率：周期31%…消费 0%，135 只全灭）——名字是营销标签，
# 净值相关性才是真实持仓暴露。此引擎 = docs/20 §3 方案A（净值回归）的前置简化版。
STYLE_PROXIES = {                 # eqindex 目录名 → 风格（全部本地 eqcs 缓存，5y 已核）
    "全指信息": "科技",
    "全指医药": "医药",
    "主要消费": "消费",
    "中证新能源": "新能源",
    "中证红利": "价值",
    "中证A股资源产业": "周期",
}
STYLE_MARKET = "沪深300"           # 共同市场成分（超额化用）
STYLE_MIN_CORR = 0.35             # 超额相关下限，低于此 → 均衡
STYLE_TTL_H = 24 * 7              # 分类结果缓存 7 天（5y 窗口下风格极稳定）
STYLE_CACHE = ROOT / "data" / "eq_style_cls.pkl"

# ---- 风格桶（v1 = 基金名关键词粗分；持仓级判定属 v2，docs/20 §3）----
# 2026-11-12 用户质疑"全是涨得好的科技"→ 数据实锤（类型内前10%达标率：科技26.3% vs
# 消费2.3% vs 新能源0%），B 通道分位由"类型内"改为"类型×风格桶内"。
BUCKET_PATTERNS = [
    ("科技", r"科技|半导体|芯片|电子|计算机|人工智能|通信|软件|信息|数字经济"),
    ("医药", r"医药|医疗|生物|健康|创新药"),
    ("消费", r"消费|白酒|食品|饮料|零售|家电"),
    ("新能源", r"新能源|光伏|锂电|电池|碳中和"),
    ("金融价值", r"银行|地产|保险|证券|金融|红利|价值"),
    ("工业", r"军工|国防|制造|工业|机械"),
]
BUCKET_OTHER = "其他/均衡"


def style_bucket(name: str) -> str:
    """基金名关键词 → 风格桶。粗分（v1）：只求把明显赛道基金与均衡基金分开。"""
    for b, pat in BUCKET_PATTERNS:
        if re.search(pat, name or ""):
            return b
    return BUCKET_OTHER


def thr_lookup(thr: dict, type_text: str, bucket: str):
    """分层回退：类型×桶(≥10只) → 全池同桶(≥10只) → 类型(≥10只)。全无 → None。"""
    v = thr["cell"].get((type_text, bucket))
    if v is not None:
        return v
    v = thr["bucket"].get(bucket)
    if v is not None:
        return v
    return thr["type"].get(type_text)


# ---- 相关性风格分类引擎 ----
def _load_close(name: str) -> pd.Series | None:
    """eqindex 目录名 → 指数日收盘序列（本地 eqcs 缓存，v1/v2 前缀、SH/SZ/CSI 前缀均可）。"""
    import glob as _glob
    src = (ROOT / "fundscorer" / "eqindex.py").read_text(encoding="utf-8")
    pairs = re.findall(r'_cat\(\s*"([^"]+)"\s*,\s*"([^"]+)"', src)
    code = next((c for c, n in pairs if n == name), None)
    if code is None:
        return None
    bare = code.replace("SH", "").replace("SZ", "").replace("CSI", "")
    for f in _glob.glob(str(ROOT / "data" / "cache" / "eqcs_*.pkl")):
        m = re.match(r"eqcs_v\d+_(.+)\.pkl$", os.path.basename(f))
        if m and m.group(1) in (code, bare):
            cl = pd.read_pickle(f).get("close") or []
            if cl:
                return pd.Series(
                    {pd.to_datetime(d, format="%Y%m%d"): float(v) for d, v in cl}
                ).sort_index()
    return None


def classify_styles(codes: list[str], force: bool = False) -> dict | None:
    """净值超额 vs 风格指数超额的相关性分类 → {code: 风格}。失败返回 None（回退名字桶）。

    只读本地缓存、无网络；结果缓存 7 天（5y 窗口下风格极稳定）。
    阈值 STYLE_MIN_CORR 以下 → "均衡"。代理指数缺任何一个 → 整体放弃（回退名字桶）。
    """
    if not force and STYLE_CACHE.exists():
        try:
            cached = pd.read_pickle(STYLE_CACHE)
            if time.time() - cached.get("ts", 0) < STYLE_TTL_H * 3600:
                hit = {c: cached["styles"][c] for c in codes
                       if c in cached["styles"]}
                if len(hit) == len(codes):
                    return hit
                # 部分命中：只算缺的（命中部分仍用缓存）
                miss = [c for c in codes if c not in hit]
                fresh = _classify_raw(miss)
                if fresh is None:
                    return hit or None
                hit.update(fresh)
                _save_style_cache(hit)
                return hit
        except Exception:
            pass
    styles = _classify_raw(codes)
    if styles:
        _save_style_cache(styles)
    return styles


def _save_style_cache(styles: dict) -> None:
    try:
        STYLE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        pd.to_pickle({"ts": time.time(), "styles": styles}, STYLE_CACHE)
    except Exception:
        pass


def _classify_raw(codes: list[str]) -> dict | None:
    """核心：周频超额收益相关性 argmax。任何代理指数缺失 → None（调用方回退名字桶）。"""
    if not codes:
        return {}
    # 1) 代理与市场周频超额
    mkt = _load_close(STYLE_MARKET)
    if mkt is None:
        return None
    mkt_w = mkt.resample("W-FRI").last().pct_change().dropna()
    prox_w = {}
    for nm in STYLE_PROXIES:
        s = _load_close(nm)
        if s is None:
            return None
        w = s.resample("W-FRI").last().pct_change().dropna()
        prox_w[STYLE_PROXIES[nm]] = (w - mkt_w).reindex(mkt_w.index)  # 超额化剔共同beta
    # 2) 逐基金银频超额相关
    out = {}
    for c in codes:
        p = ROOT / "data" / "cache" / f"nav_{c}.pkl"
        if not p.exists():
            continue
        try:
            nav = pd.read_pickle(p)
            if "nav" not in nav.columns or nav.empty:
                continue
            end = nav["date"].max()
            sub = nav[nav["date"] > end - pd.DateOffset(years=5)].set_index("date")["nav"]
            fr = sub.resample("W-FRI").last().pct_change().dropna()
            fr_ex = (fr - mkt_w.reindex(fr.index)).dropna()
            if len(fr_ex) < 100:
                continue
            corrs = {}
            for st, pex in prox_w.items():
                both = pd.concat([fr_ex, pex], axis=1).dropna()
                if len(both) >= 100:
                    corrs[st] = both.iloc[:, 0].corr(both.iloc[:, 1])
            if not corrs:
                continue
            best = max(corrs, key=corrs.get)
            out[c] = best if corrs[best] >= STYLE_MIN_CORR else "均衡"
        except Exception:
            continue
    return out or None


# ---------------------------------------------------------------- 1. 池与经理对
def load_pool() -> pd.DataFrame:
    """S1 池：全市场基金列表按四类型过滤。列表缓存 3 天（过期自动重抓，1 请求）。"""
    arr = batch.fetch_fund_list()
    rows = [{"code": str(c).zfill(6), "name": n, "type_text": t}
            for c, _u, n, t, _py in arr if t in POOL_TYPES]
    return pd.DataFrame(rows)


def _career_years(v) -> float | None:
    """mgr_em 累计从业时间 → 年。数值=天（/365，与 data.fetch_manager_career_years 同口径，
    但不取整——闸在 10.0/6.0 边界判定，取整会把 9.96 年错判成 10.0）。'X年' 文本兼容。"""
    n = pd.to_numeric(pd.Series([v]), errors="coerce").iloc[0]
    if pd.notna(n):
        return float(n) / 365.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*年", str(v or ""))
    return float(m.group(1)) if m else None


def career_pairs(mgr_tbl: pd.DataFrame, codes) -> pd.DataFrame:
    """(经理, 基金) 从业对：mgr_em 行级 join（行本身即配对，无重名歧义）。

    返回列 mgr/company/code/career_y。基金不在表中 → 无行 → 该基金无对（漏斗计数）。
    """
    m = mgr_tbl.copy()
    m["code"] = (m["现任基金代码"].astype(str)
                 .str.replace(r"\.0$", "", regex=True).str.zfill(6))
    m = m[m["code"].isin(set(codes))]
    out = pd.DataFrame({
        "mgr": m["姓名"].values,
        "company": m["所属公司"].values,
        "code": m["code"].values,
        "career_y": [ _career_years(v) for v in m["累计从业时间"].values ],
    })
    return out.drop_duplicates(["mgr", "company", "code"]).reset_index(drop=True)


def channel(career_y) -> str:
    """S2 通道：>10 年 A（直通）；>6 年 B（前10%通道）；≤6 年 out；缺数据 unknown。"""
    if career_y is None or pd.isna(career_y):
        return "unknown"
    if career_y > CAREER_DIRECT_Y:
        return "A"
    if career_y > CAREER_MIN_Y:
        return "B"
    return "out"


# ---------------------------------------------------------------- 2. 净值与基准
def nav_stats(codes: list[str], workers: int = 8, log_every: int = 500) -> dict:
    """并行抓净值（缓存 20h，断点=缓存）→ {code: (nav_years, ret5y)}。

    ret5y 仅在 nav_years≥S4 时计算（metrics.ann_return 对不足窗口会退化为全期年化，
    不设闸会把 3 年基金的全期年化当 5 年用——故此处先闸后算）。
    """
    out: dict[str, tuple] = {}
    failed: list[str] = []

    def _one(code: str):
        try:
            nav = D.fetch_nav(code)
            if nav is None or nav.empty:
                return code, None
            ny = (nav["date"].max() - nav["date"].min()).days / 365.25
            r5 = M.ann_return(nav, 5) if ny >= NAV_MIN_Y else None
            return code, (round(ny, 2), r5)
        except Exception:
            return code, None

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_one, c): c for c in codes}
        for i, fut in enumerate(as_completed(futs), 1):
            code, val = fut.result()
            if val is None:
                failed.append(code)
            else:
                out[code] = val
            if i % log_every == 0:
                print(f"  [nav] {i}/{len(codes)} 失败 {len(failed)} "
                      f"已用 {(time.time()-t0)/60:.1f}min", flush=True)
    return {"stats": out, "failed": failed}


def composite_bench_ann_from(hs: pd.Series, bond: pd.Series,
                             w: float, years: int = 5) -> float | None:
    """类型合成基准近 N 年年化：w×沪深300 + (1−w)×中债新综合（日收益加权后复利）。"""
    r_hs = hs.sort_index().pct_change()
    r_b = bond.sort_index().pct_change()
    idx = r_hs.index.intersection(r_b.index)
    end = idx.max()
    idx = idx[idx > end - pd.DateOffset(years=years)]
    r = (w * r_hs.loc[idx] + (1 - w) * r_b.loc[idx]).dropna()
    if r.empty:
        return None
    days = max((idx.max() - idx.min()).days, 1)
    return float((1 + r).prod() ** (365.0 / days) - 1)


def composite_bench_ann(years: int = 5) -> dict[str, float | None]:
    """各类型合成基准近 N 年年化（本地序列，无网络）。序列缺失 → 全 None（超额列留空）。"""
    try:
        import glob as _glob
        cands = sorted(_glob.glob(str(ROOT / "data" / "cache" / "eqcs_v*_000300.pkl")))
        if not cands:
            raise FileNotFoundError("eqcs_*_000300.pkl")
        close = pd.read_pickle(cands[-1])["close"]   # [['YYYYMMDD', val], …]
        hs = pd.Series({pd.to_datetime(d, format="%Y%m%d"): float(v) for d, v in close})
        bond = (pd.read_pickle(ROOT / "data" / "cache" / "bond_index_wealth.pkl")
                .set_index("date")["value"])
        return {t: composite_bench_ann_from(hs, bond, w, years)
                for t, w in BENCH_W.items()}
    except Exception as e:                             # 展示列缺失不阻断闸
        print(f"  [bench] 基准序列不可用（{e}），超额列留空", flush=True)
        return {t: None for t in POOL_TYPES}


# ---------------------------------------------------------------- 3. 预处理与 f10 选择
def prep(pool: pd.DataFrame, pairs: pd.DataFrame, navs: dict,
         styles: dict | None = None) -> tuple:
    """S4 + 分位人群 + pair 富化。返回 (pool_aug, pf, thr)。

    thr = {"cell": {(类型,桶): q}, "桶": {桶: q}, "type": {类型: q}}（各层 ≥10 只才建）。
    B 通道分位在**类型×风格**内取（2026-11-12 修复：类型内比会系统性偏向
    近5年风口风格——科技达标率 26.3% vs 消费 2.3% vs 新能源 0%）。
    风格来源：styles=相关性分类结果（优先）；None → 名字关键词回退
    （审计实锤两者一致率仅 45.9%，相关性是真实持仓暴露，名字只是营销标签）。
    """
    pool = pool.copy()
    pool["ny"] = pool["code"].map(lambda c: navs.get(c, (None, None))[0])
    pool["r5"] = pool["code"].map(lambda c: navs.get(c, (None, None))[1])
    pool["s4"] = pool["ny"].notna() & (pool["ny"] >= NAV_MIN_Y)
    if styles:
        pool["bucket"] = pool["code"].map(styles).fillna(
            pool["name"].map(style_bucket))
        pool["style_src"] = pool["code"].map(
            lambda c: "corr" if c in styles else "name")
    else:
        pool["bucket"] = pool["name"].map(style_bucket)
        pool["style_src"] = "name"
    s4 = pool[pool["s4"]].dropna(subset=["r5"])

    thr = {"cell": {}, "bucket": {}, "type": {}}
    for (t, b), g in s4.groupby(["type_text", "bucket"]):
        if len(g) >= 10:
            thr["cell"][(t, b)] = float(g["r5"].quantile(TOP_PCT))
    for b, g in s4.groupby("bucket"):
        if len(g) >= 10:
            thr["bucket"][b] = float(g["r5"].quantile(TOP_PCT))
    for t, g in s4.groupby("type_text"):
        if len(g) >= 10:
            thr["type"][t] = float(g["r5"].quantile(TOP_PCT))

    # 展示用分位：与闸同层级（cell → bucket → type 回退）
    pool["pctl"] = None
    for lvl, keys in (("cell", ["type_text", "bucket"]),
                      ("bucket", ["bucket"]), ("type", ["type_text"])):
        mask = pool["s4"] & pool["pctl"].isna() & pool["r5"].notna()
        for k, g in pool[mask].groupby(keys):
            if len(g) >= 10:
                pool.loc[g.index, "pctl"] = (g["r5"].rank(pct=True) * 100).round(1)

    pf = pairs.merge(
        pool[["code", "name", "type_text", "bucket", "ny", "r5", "s4", "pctl"]],
        on="code", how="inner")
    pf["s4"] = pf["s4"].fillna(False)
    pf["channel"] = pf["career_y"].map(channel)
    return pool, pf, thr


def f10_targets(pf: pd.DataFrame, thr: dict) -> list[str]:
    """需抓 F10 的基金：（通道A∩S4∩非垫底）∪（通道B∩分位达标∩S4）。

    B 通道先按分位预筛（分位不达标者无需抓 f10——任本基金年限救不了分位）；
    A 通道先按 S2-C 垫底底线预筛（同格后 10% 出局者同样无需 f10）。
    """
    b = pf[pf["channel"] == "B"].copy()
    if not b.empty:
        b["thr"] = [thr_lookup(thr, t, k)
                    for t, k in zip(b["type_text"], b["bucket"])]
        b_ok = b[b["r5"].notna() & b["thr"].notna() & (b["r5"] >= b["thr"])]
    else:
        b_ok = b
    a = pf[(pf["channel"] == "A") & pf["s4"]].copy()
    # S2-C：同格排位后 10% → 出局（pctl 缺失=分位无法计算，不砍——数据不足不猜测）
    floor_ok = a["pctl"].isna() | (a["pctl"] > A_FLOOR_PCT)
    a = a[floor_ok]
    codes = set(a["code"]) | set(b_ok[b_ok["s4"]]["code"])
    return sorted(codes)


def fetch_f10_all(codes: list[str], workers: int = 8, log_every: int = 200) -> dict:
    """并行抓 F10（缓存 7 天）→ {code: info}；失败不入典（漏斗计数）。"""
    out: dict[str, dict] = {}

    def _one(code: str):
        try:
            return code, D.fetch_f10(code)
        except Exception:
            return code, None

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_one, c): c for c in codes}
        for i, fut in enumerate(as_completed(futs), 1):
            code, info = fut.result()
            if info:
                out[code] = info
            if i % log_every == 0:
                print(f"  [f10] {i}/{len(codes)} 已用 {(time.time()-t0)/60:.1f}min",
                      flush=True)
    return out


# ---------------------------------------------------------------- 4. 闸本体
def run_gate(pool: pd.DataFrame, pf: pd.DataFrame, thr: dict,
             f10s: dict, bench: dict) -> tuple:
    """S3 + B 分位复核 → (漏斗, 经理榜 DataFrame, 通过明细 DataFrame)。

    pair 通过 ⟺ 通道≠out/unknown ∧ S4 ∧ f10 里仍是现任 ∧ 任本基金≥S3
                ∧（（通道A ∧ 非同格垫底后10%）∨（通道B ∧ r5 ≥ 类型×风格 分位阈值））
    """
    f = {"S1_池内基金": int(len(pool)),
         "S1_分类型": {t: int(n) for t, n in pool["type_text"].value_counts().items()},
         "S2_无经理数据基金": int(len(pool) - pool["code"].isin(pf["code"]).sum()),
         "S2_通道A_pair": int((pf["channel"] == "A").sum()),
         "S2_通道B_pair": int((pf["channel"] == "B").sum()),
         "S2_出闸_pair_≤6年": int((pf["channel"] == "out").sum()),
         "S2_从业数据缺失_pair": int((pf["channel"] == "unknown").sum()),
         "S4_净值≥5年基金": int(pool["s4"].sum()),
         "分位人群_按类型": {t: int(n) for t, n in
                             pool[pool["s4"]].groupby("type_text").size().items()},
         "风格来源": {s: int(n) for s, n in
                      pool[pool["s4"]].groupby("style_src").size().items()}
         if "style_src" in pool.columns else {},
         f"分位阈值({TOP_PCT:.0%})_类型×风格": {f"{t}|{b}": round(v, 4)
                                 for (t, b), v in thr["cell"].items()},
         f"分位阈值({TOP_PCT:.0%})_回退风格": {b: round(v, 4) for b, v in thr["bucket"].items()},
         "f10_候选基金": 0, "f10_缺失或失败": 0,
         "S3_非现任或任期不足": 0, "B通道_分位不足": 0,
         "S2C_A通道垫底出局": 0,
         "通过_pair": 0, "通过_基金": 0, "通过_经理": 0}

    passed = []
    seen_codes: set[str] = set()
    pool_ix = pool.set_index("code")
    for p in pf.itertuples(index=False):
        if p.channel not in ("A", "B") or not p.s4:
            continue
        if p.channel == "A":
            # S2-C 垫底底线：同格排位后 10% 出局（pctl 缺失不砍——数据不足不猜测）
            pc = pool_ix.at[p.code, "pctl"]
            if pc is not None and not pd.isna(pc) and pc <= A_FLOOR_PCT:
                f["S2C_A通道垫底出局"] += 1
                continue
        else:
            # B 通道先按分位判定（f10_targets 只抓分位达标者，此处兜底归类，避免误记 f10 缺失）
            thrv = thr_lookup(thr, p.type_text, p.bucket)
            if thrv is None or p.r5 is None or pd.isna(p.r5) or p.r5 < thrv:
                f["B通道_分位不足"] += 1
                continue
        seen_codes.add(p.code)
        f10 = f10s.get(p.code)
        if not f10:
            f["f10_缺失或失败"] += 1
            continue
        mg = next((m for m in (f10.get("managers") or [])
                   if m.get("name") == p.mgr), None)
        if mg is None:
            f["S3_非现任或任期不足"] += 1
            continue
        ten = _tenure_days(mg.get("start") or "")
        if ten is None or ten < TENURE_MIN_D:
            f["S3_非现任或任期不足"] += 1
            continue
        excess = (p.r5 - bench.get(p.type_text)
                  if p.r5 is not None and bench.get(p.type_text) is not None else None)
        passed.append({"code": p.code, "name": p.name, "type_text": p.type_text,
                       "bucket": p.bucket, "mgr": p.mgr, "company": p.company,
                       "career_y": p.career_y,
                       "channel": p.channel, "tenure_d": ten, "nav_years": p.ny,
                       "ret5y": p.r5, "excess_5y": excess,
                       "pctl": pool_ix.at[p.code, "pctl"]})
        f["通过_pair"] += 1

    funds = pd.DataFrame(passed)
    if funds.empty:
        funds = pd.DataFrame(columns=["code", "name", "type_text", "bucket", "mgr",
                                      "company", "career_y", "channel", "tenure_d",
                                      "nav_years", "ret5y", "excess_5y", "pctl"])
    f["通过_基金"] = int(funds["code"].nunique())
    f["通过_经理"] = int(funds[["mgr", "company"]].drop_duplicates().shape[0]) \
        if not funds.empty else 0

    # 经理级聚合
    if funds.empty:
        mgrs = pd.DataFrame(columns=["mgr", "company", "career_y", "channel",
                                     "n_funds", "n_funds_base", "codes", "names",
                                     "max_tenure_d"])
    else:
        # A/C 份额去重口径（2026-11-12 审计：通过 1257 code ≈ 900 基础基金，
        # n_funds 被 A/C 虚增）：基础份额 = 去掉名字尾字母 A/C/E
        funds = funds.copy()
        funds["base"] = funds["name"].str.replace(r"[ACE]$", "", regex=True)
        g = funds.groupby(["mgr", "company"], as_index=False)
        mgrs = g.agg(career_y=("career_y", "max"),
                     channel=("channel", lambda s: "A" if (s == "A").any() else "B"),
                     n_funds=("code", "nunique"),
                     n_funds_base=("base", "nunique"),
                     max_tenure_d=("tenure_d", "max"))
        codes_l = g["code"].apply(lambda s: " ".join(sorted(s.unique())))
        names_l = g["name"].apply(lambda s: " / ".join(sorted(s.unique())))
        mgrs = mgrs.merge(codes_l, on=["mgr", "company"]).merge(
            names_l, on=["mgr", "company"])
        mgrs = mgrs.sort_values(["channel", "career_y", "n_funds"],
                                ascending=[True, False, False]).reset_index(drop=True)

    f["f10_候选基金"] = int(len(seen_codes))
    return f, mgrs, funds


# ---------------------------------------------------------------- 5. CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fundscorer.eqgate",
                                 description="偏股资深经理闸 v1（docs/20 §2.1）")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args(argv)
    t0 = time.time()

    print("[1/5] S1 池（四类型，列表缓存 3 天）…", flush=True)
    pool = load_pool()
    print(f"      池内 {len(pool)} 只："
          + " ".join(f"{t}={n}" for t, n in pool['type_text'].value_counts().items()),
          flush=True)

    print("[2/5] 经理从业对（mgr_em 缓存 3 天，过期自动重抓）…", flush=True)
    pairs = career_pairs(D._manager_em_table(), pool["code"].tolist())
    print(f"      从业对 {len(pairs)}，覆盖基金 {pairs['code'].nunique()} "
          f"（无经理数据 {len(pool) - pairs['code'].nunique()}）", flush=True)

    print(f"[3/5] 净值抓取（{len(pool)} 只，缓存 20h 断点）…", flush=True)
    navres = nav_stats(pool["code"].tolist(), workers=args.workers)
    print(f"      成功 {len(navres['stats'])} 失败 {len(navres['failed'])}", flush=True)

    # 风格分类（本地净值×指数超额相关性，结果缓存 7 天；失败回退名字桶）
    s4_codes = [c for c, v in navres["stats"].items() if v[0] >= NAV_MIN_Y]
    print(f"[3.5] 风格分类（相关性引擎，S4 {len(s4_codes)} 只，缓存 7 天）…", flush=True)
    t_sc = time.time()
    styles = classify_styles(s4_codes)
    if styles:
        from collections import Counter
        dist = Counter(styles.values())
        print(f"      完成 {len(styles)} 只，耗时 {(time.time()-t_sc)/60:.1f}min；"
              f"分布 {dict(dist)}", flush=True)
    else:
        print("      分类不可用（代理指数缺失）→ 回退名字关键词分桶", flush=True)

    pool, pf, thr = prep(pool, pairs, navres["stats"], styles=styles)
    print(f"      S4 净值≥5年 {int(pool['s4'].sum())} 只；"
          f"分位层：类型×风格 {len(thr['cell'])} 格 / 回退风格 {len(thr['bucket'])} / "
          f"类型 {len(thr['type'])}", flush=True)

    targets = f10_targets(pf, thr)
    print(f"[4/5] F10 抓取（候选 {len(targets)} 只，缓存 7 天）…", flush=True)
    f10s = fetch_f10_all(targets, workers=args.workers)
    print(f"      成功 {len(f10s)} 失败 {len(targets) - len(f10s)}", flush=True)

    print("[5/5] 闸判定…", flush=True)
    bench = composite_bench_ann()
    funnel, mgrs, funds = run_gate(pool, pf, thr, f10s, bench)

    out_d = ROOT / "data"
    funds.to_csv(out_d / "eq_gate_funds.csv", index=False, encoding="utf-8-sig")
    mgrs.to_csv(out_d / "eq_gate_managers.csv", index=False, encoding="utf-8-sig")
    (out_d / "eq_gate_funnel.json").write_text(
        json.dumps(funnel, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")

    print("\n========== 闸漏斗 ==========", flush=True)
    for k, v in funnel.items():
        print(f"  {k}: {v}", flush=True)
    print(f"\n通过经理 {len(mgrs)} 人 / 基金 {len(funds)} 只（pair 级）")
    if not mgrs.empty:
        print("\n前 20 位经理：")
        for r in mgrs.head(20).itertuples(index=False):
            print(f"  {r.channel} {r.mgr}({r.company}) 从业{r.career_y:.1f}年 "
                  f"任本基金最长{r.max_tenure_d}天 在管入围{r.n_funds}只")
    print(f"\n总耗时 {(time.time()-t0)/60:.1f}min；"
          f"输出 data/eq_gate_{{managers,funds}}.csv + eq_gate_funnel.json", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
