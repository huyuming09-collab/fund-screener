# -*- coding: utf-8 -*-
"""偏股两榜评分引擎（docs/20 §2.2/§2.4）。

三层结构（§2.4 两榜考卷制，2026-11-12 五轮问答定稿）：
  L1 资格 = S1池(4类型) ∧ S3 任本基金≥5年 ∧ S4 净值≥5年 ∧ 从业>6年
  L2 考卷 = 老将榜(从业>10年) G3s 中档：5年中≥3年进同格前25%
            新锐榜(从业6~10年) G4s 严档：5年同格平均排位≥70
  L3 复评 = 周滚动 + 出榜双周确认（首过不出，连续两次不过才出；
            资格失效/离职/净值断档(>180天)即时出；毕业(新→老)即考老将卷，不过落榜）

评分（§2.2，检验18-20 验证）：6 因子 → 合格池内分位 0-100 → W3 加权
  F1 持续性 .27 / F3 风格调整超额 .25 / F5 防运气 .19 / F2 胜率 .15 / F6 质地 .14
  （F4 稳定性三起点负 IC 数据证伪 → 权重归零不进分，仅存 CSV 供回核）
  经理分 = 名下过考基金的 W3 最高分（检验19 min-rank 同口径）；
  保护期（本期未过考）回落至入围基金最高分。

口径要点（改动=产品决策，须过用户 AG §7）：
  · 同格 = 类型×相关性风格桶（eqgate 风格引擎），分位回退 桶→类型，n≥10
  · 通道按**经理本人**从业（>10 A / 6~10 B）；G3s/G4s 为基金级逐年排位考卷
  · 进榜须**本人**任本基金≥5年（pair 级 S3，与 eqgate 闸同口径；
    验证脚本 eq_two_list_now 为 fund 级宽松口径，本引擎从严，规模会有小差）
  · 展示桶（徽章/7桶筛选）与考卷桶（原始）分离：金融价值→价值、工业→周期、
    其他/均衡→均衡——只影响显示，不影响同格分位
  · 特征缓存按指纹（净值/风格/基准 mtime）失效：数据没变就秒开
  · F10 只读缓存+补缺失（start 是历史事实；离任由 mgr_em 3 天表兜住）
  · --offline / pipeline --skip-refresh：不刷旧缓存，缺啥补啥

输出：返回 payload dict（pipeline 嵌 data.js 的 mgr 段）+ 写
  data/eq_score_{mgrs,funds}.csv（回核）+ data/eq_roster.json（名单状态：
  入场期/连续不过次数/毕业标记——gitignore 运行态，丢了=榜龄重新起算）。
"""
from __future__ import annotations

import argparse
import json
import os
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

warnings.filterwarnings("ignore")
import numpy as np                                     # noqa: E402
import pandas as pd                                    # noqa: E402

from . import data as D                                # noqa: E402
from . import metrics as M                             # noqa: E402
from . import eqgate as G                              # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
NAV_DIR = ROOT / "data" / "cache"
ROSTER_PATH = ROOT / "data" / "eq_roster.json"
FEAT_CACHE = ROOT / "data" / "eq_score_feat.pkl"
CSV_MGR = ROOT / "data" / "eq_score_mgrs.csv"
CSV_FUND = ROOT / "data" / "eq_score_funds.csv"

# W3 权重（docs/20 §2.2：LOOCV-IC 三起点定权，F4 负 IC 归零）——产品决策
W3 = {"F1": 0.27, "F3": 0.25, "F5": 0.19, "F2": 0.15, "F6": 0.14}
# 考卷阈值（docs/20 §2.4 L2）——产品决策
G3S_MIN_TOP = 3
G3S_PCT = 75.0
G4S_MEAN = 70.0
# 净值断档阈值：末值距锚点超过此天数视为"断数据"→ 资格失效即时出（§2.4 L3）
NAV_STALE_D = 180
# 展示桶归一（考卷仍用原始桶；此处只影响徽章/筛选）
DISPLAY_ALIAS = {"金融价值": "价值", "工业": "周期", "其他/均衡": "均衡"}
CANON7 = ["科技", "医药", "消费", "新能源", "价值", "周期", "均衡"]


def display_bucket(b) -> str:
    return DISPLAY_ALIAS.get(b, b)


# ---------------------------------------------------------------- 0. 本地小工具
# （layering：fundscorer 不得 import scripts；验证脚本里的小函数在此复刻）
def nav_yearly(nav: pd.DataFrame, a, b) -> float | None:
    """(a, b] 窗口总收益；数据不足（<180 点或跨度 <300 天）→ None。"""
    sub = nav[(nav["date"] > a) & (nav["date"] <= b)]
    if len(sub) < 180 or (sub["date"].iloc[-1] - sub["date"].iloc[0]).days < 300:
        return None
    return float(sub["nav"].iloc[-1] / sub["nav"].iloc[0] - 1)


def nav_ann_to(nav: pd.DataFrame, o, years: int) -> float | None:
    navT = nav[nav["date"] <= o]
    if navT.empty:
        return None
    end = navT["date"].max()
    sub = navT[navT["date"] > end - pd.DateOffset(years=years)]
    if len(sub) < 2:
        return None
    days = (sub["date"].iloc[-1] - sub["date"].iloc[0]).days
    if days <= 0:
        return None
    return float((sub["nav"].iloc[-1] / sub["nav"].iloc[0]) ** (365.0 / days) - 1)


def rank_pcts(df: pd.DataFrame, col: str, key_sets) -> pd.Series:
    """层级回填分位（类型×风格 → 桶 → 类型，n≥10 才算），与 eqgate 同口径。"""
    out = pd.Series(float("nan"), index=df.index, dtype="float64")
    for keys in key_sets:
        mask = out.isna() & df[col].notna()
        for _k, g in df[mask].groupby(keys):
            if len(g) >= 10:
                out.loc[g.index] = (g[col].rank(pct=True) * 100).round(1)
    return out


def s_total(s: pd.Series, a, b) -> float | None:
    sub = s[(s.index > a) & (s.index <= b)]
    if len(sub) < 2:
        return None
    return float(sub.iloc[-1] / sub.iloc[0] - 1)


def s_ann(s: pd.Series, a, b) -> float | None:
    sub = s[(s.index > a) & (s.index <= b)]
    if len(sub) < 2:
        return None
    days = (sub.index[-1] - sub.index[0]).days
    if days <= 0:
        return None
    return float((sub.iloc[-1] / sub.iloc[0]) ** (365.0 / days) - 1)


def composite_wealth() -> dict:
    """类型合成基准日财富序列（w×沪深300 + (1−w)×中债新综合），本地缓存无网络。"""
    hs = G._load_close(G.STYLE_MARKET)
    bond = pd.read_pickle(NAV_DIR / "bond_index_wealth.pkl").set_index("date")["value"]
    r_hs = hs.sort_index().pct_change()
    r_b = bond.sort_index().pct_change()
    idx = r_hs.index.intersection(r_b.index)
    return {t: (1 + (w * r_hs.loc[idx] + (1 - w) * r_b.loc[idx]).dropna()).cumprod()
            for t, w in G.BENCH_W.items()}


def comp_total(wealth: pd.Series, a, b) -> float | None:
    sub = wealth[(wealth.index > a) & (wealth.index <= b)]
    if len(sub) < 2:
        return None
    return float(sub.iloc[-1] / sub.iloc[0] - 1)


def comp_ann(wealth: pd.Series, a, b) -> float | None:
    sub = wealth[(wealth.index > a) & (wealth.index <= b)]
    if len(sub) < 2:
        return None
    days = (sub.index[-1] - sub.index[0]).days
    if days <= 0:
        return None
    return float((sub.iloc[-1] / sub.iloc[0]) ** (365.0 / days) - 1)


# ---------------------------------------------------------------- 1. 数据装载
def _pool(refresh: bool) -> pd.DataFrame:
    if refresh:
        return G.load_pool()
    arr = D._load_cache("fund_list_all", 1e9)
    if arr is None:
        return G.load_pool()                      # 缓存真没有 → 只能联网（1 个请求）
    return pd.DataFrame([{"code": str(c).zfill(6), "name": n, "type_text": t}
                         for c, _u, n, t, _py in arr if t in G.POOL_TYPES])


def _mgr_table(refresh: bool) -> pd.DataFrame:
    if refresh:
        return D._manager_em_table()
    cached = D._load_cache("mgr_em_all", 1e9)
    if cached is None:
        return D._manager_em_table()
    return cached


def _read_nav_pickle(code: str):
    p = NAV_DIR / f"nav_{code}.pkl"
    if not p.exists():
        return None
    try:
        nav = pd.read_pickle(p)
    except Exception:
        return None
    if "nav" not in nav.columns or nav.empty or "date" not in nav.columns:
        return None
    return nav


def _nav_stats(codes: list[str], refresh: bool, workers: int, log) -> dict:
    """{code: (span_y, ret5y)} + 净值可用集合。

    refresh=True：先走 G.nav_stats（20h 网络缓存）预热，抓取失败的**回退读 pickle**
    （陈旧但可用 → 瞬时网络故障不会误判"断数据"）；refresh=False：全量离线读 pickle。
    """
    out: dict = {}
    failed: list[str] = []
    if refresh:
        res = G.nav_stats(codes, workers=workers)
        out.update(res["stats"])
        failed = res["failed"]
        log(f"      净值网络 {len(out)} / 失败 {len(failed)} → pickle 兜底")
    else:
        failed = list(codes)
    for c in failed:
        if c in out:
            continue
        nav = _read_nav_pickle(c)
        if nav is None:
            continue
        ny = (nav["date"].max() - nav["date"].min()).days / 365.25
        r5 = M.ann_return(nav, 5) if ny >= G.NAV_MIN_Y else None
        out[c] = (round(ny, 2), r5)
    log(f"      净值可用 {len(out)} / 彻底无数据 {len(codes) - len(out)}")
    return {"stats": out, "ok": set(out)}


def _f10s(codes: list[str], refresh: bool, workers: int, log) -> dict:
    """F10（现任经理+任期起始+规模）。

    已有缓存**永不回抓**（start 是历史事实；离任由 mgr_em 3 天表兜住；
    回归接管修正走 fetch_f10 读端逻辑）——只对**缺失**的 code 补抓。
    这样周更代价≈0，而资格判定不会因 f10 陈旧出错（分析见模块 docstring）。
    """
    out: dict = {}
    missing: list[str] = []

    def _read(code):
        p = NAV_DIR / f"f10_v3_{code}.pkl"
        if not p.exists():
            return code, None
        try:
            return code, D.fetch_f10(code, max_age_hours=1e9)
        except Exception:
            return code, None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_read, c): c for c in codes}
        for fut in as_completed(futs):
            code, info = fut.result()
            if info:
                out[code] = info
            else:
                missing.append(code)
    if missing and refresh:
        def _fetch(code):
            try:
                return code, D.fetch_f10(code)
            except Exception:
                return code, None
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_fetch, c): c for c in missing}
            for fut in as_completed(futs):
                code, info = fut.result()
                if info:
                    out[code] = info
        log(f"      F10 缺失补抓 {len(missing)} → 成功 "
            f"{sum(1 for c in missing if c in out)}")
    elif missing:
        log(f"      F10 离线缺失 {len(missing)}（相关 pair 视为数据不全，冻结不判）")
    return out


def _f10_ok(info) -> bool:
    """资格校验用的 F10 可用性：有条目且 managers 非空（空=解析异常，视为数据不全）。"""
    return bool(info) and bool(info.get("managers"))


# ---------------------------------------------------------------- 2. 特征（指纹缓存）
def _feat_fingerprint() -> tuple:
    """特征 = f(净值, 风格分类, 合成基准)；任一变动即失效。"""
    def _stat(path: Path):
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return 0
    nav_n, nav_mx = 0, 0
    try:
        with os.scandir(NAV_DIR) as it:
            for e in it:
                if e.name.startswith("nav_") and e.name.endswith(".pkl"):
                    nav_n += 1
                    try:
                        m = e.stat().st_mtime_ns
                        if m > nav_mx:
                            nav_mx = m
                    except OSError:
                        pass
    except OSError:
        pass
    hs_mx = 0
    try:
        with os.scandir(NAV_DIR) as it:
            for e in it:
                if e.name.startswith("eqcs_") and e.name.endswith("_000300.pkl"):
                    try:
                        m = e.stat().st_mtime_ns
                        if m > hs_mx:
                            hs_mx = m
                    except OSError:
                        pass
    except OSError:
        pass
    return (nav_n, nav_mx, _stat(G.STYLE_CACHE),
            _stat(NAV_DIR / "bond_index_wealth.pkl"), hs_mx)


def build_features(pool: pd.DataFrame, styles: dict, log) -> dict:
    """S4 人群特征：back0..4 逐年收益 + ps_0..4 同格逐年分位 + cexc 合成超额
    + ret5y + nav_end（断档判定）。锚点 o = 构建日；指纹没变直接复用缓存。
    """
    fp = _feat_fingerprint()
    if FEAT_CACHE.exists():
        try:
            hit = pd.read_pickle(FEAT_CACHE)
            if hit.get("fp") == fp:
                log(f"      特征缓存命中（asof {pd.Timestamp(hit['asof']).date()}，"
                    f"锚点 {pd.Timestamp(hit['o']).date()}）")
                return hit
        except Exception:
            pass
    t0 = time.time()
    o = pd.Timestamp.today().normalize()
    back_win = [(o - pd.DateOffset(years=k), o - pd.DateOffset(years=k - 1))
                for k in range(1, 6)]                      # [0]=最近一年
    rows, asof = [], None
    for i, r in enumerate(pool.itertuples(index=False), 1):
        nav = _read_nav_pickle(r.code)
        if nav is None:
            continue
        navT = nav[nav["date"] <= o]
        if navT.empty:
            continue
        dmax = navT["date"].max()
        if asof is None or dmax > asof:
            asof = dmax
        span = (dmax - navT["date"].min()).days / 365.25
        if span < G.NAV_MIN_Y:
            continue
        sty = styles.get(r.code) or G.style_bucket(r.name)
        rows.append(dict(code=r.code, name=r.name, type=r.type_text, bucket=sty,
                         nav_end=dmax, ret5y=nav_ann_to(nav, o, 5),
                         back=[nav_yearly(nav, a, b) for a, b in back_win]))
        if i % 3000 == 0:
            log(f"      特征 {i}/{len(pool)} {time.strftime('%H:%M:%S')}")
    if not rows:
        raise RuntimeError("S4 特征为空（净值缓存全缺？先跑 eqgate 预热）")
    feat = pd.DataFrame(rows).set_index("code")
    bdf = feat["back"].apply(pd.Series)
    bdf.columns = [f"back{j}" for j in range(bdf.shape[1])]
    feat = pd.concat([feat.drop(columns=["back"]), bdf], axis=1)
    # 同格逐年分位（类型×风格 → 桶 → 类型，n≥10）
    for j in range(5):
        feat[f"ps_{j}"] = rank_pcts(feat, f"back{j}",
                                    [["type", "bucket"], ["bucket"], ["type"]])
    # 合成基准逐年超额（无代理桶的 F5/F3 兜底）
    try:
        wealth = composite_wealth()
    except Exception as e:
        wealth = {}
        log(f"      [warn] 合成基准不可用（{e}）→ 无代理桶因子降级")
    comp_year = {}
    for j in range(5):
        a, b = back_win[j]
        for t in feat["type"].unique():
            comp_year[(t, j)] = (comp_total(wealth[t], a, b) if t in wealth else None)
    for j in range(5):
        feat[f"cexc_{j}"] = [
            (None if (getattr(r, f"back{j}") is None
                      or comp_year.get((r.type, j)) is None)
             else getattr(r, f"back{j}") - comp_year[(r.type, j)])
            for r in feat.itertuples()]
    out = {"fp": fp, "o": o, "asof": asof, "feat": feat, "wealth": wealth}
    try:
        pd.to_pickle(out, FEAT_CACHE)
    except Exception:
        pass
    log(f"      特征构建 {len(feat)} 只 S4，耗时 {(time.time() - t0) / 60:.1f}min"
        f"（asof {pd.Timestamp(asof).date() if asof is not None else '-'}）")
    return out


# ---------------------------------------------------------------- 3. 资格 / 考卷
def qualify_pairs(pairs: pd.DataFrame, feat: pd.DataFrame, f10s: dict,
                  o) -> pd.DataFrame:
    """pair 级资格：从业>6 ∧ S4 ∧ 净值未断档 ∧ F10 现任 ∧ 本人任本基金≥1825 天。

    与 eqgate 闸同口径（pair 级 S3）；验证脚本 eq_two_list_now 是 fund 级
    （任一 pair 达标即全基金入围，且不查本人任期）——本引擎从严。
    """
    stale = o - pd.Timedelta(days=NAV_STALE_D)
    out = []
    for row in pairs.itertuples(index=False):
        ch = G.channel(row.career_y)
        if ch not in ("A", "B"):
            continue
        if row.code not in feat.index:
            continue
        if feat.at[row.code, "nav_end"] < stale:
            continue                               # 净值断档 → 不入围（资格失效即时出）
        info = f10s.get(row.code)
        if not _f10_ok(info):
            continue
        mg = next((m for m in info.get("managers") or []
                   if m.get("name") == row.mgr), None)
        if mg is None:
            continue
        try:
            start = pd.Timestamp(str(mg.get("start") or "")[:10])
        except Exception:
            continue
        if (o - start).days < G.TENURE_MIN_D:
            continue
        out.append(dict(mgr=row.mgr, company=row.company, code=row.code,
                        career_y=row.career_y, channel=ch,
                        tenure_y=round((o - start).days / 365.25, 2)))
    return pd.DataFrame(out, columns=["mgr", "company", "code", "career_y",
                                      "channel", "tenure_y"])


def exam_flags(feat: pd.DataFrame) -> dict:
    """{code: 'G3s'/'G4s'/'G3sG4s'/None}。毕业判定要用 G3s，故可同时过两卷。"""
    out = {}
    for c in feat.index:
        ps = [feat.at[c, f"ps_{j}"] for j in range(5)]
        if any(pd.isna(x) for x in ps):
            out[c] = None
            continue
        tag = ""
        if sum(1 for x in ps if x > G3S_PCT) >= G3S_MIN_TOP:
            tag += "G3s"
        if (sum(ps) / 5) >= G4S_MEAN:
            tag += "G4s"
        out[c] = tag or None
    return out


def fund_channels(pairs: pd.DataFrame) -> pd.Series:
    """基金通道 = 名下经理从业最大值（验证脚本同口径）。"""
    return pairs.groupby("code")["career_y"].max().map(G.channel)


# ---------------------------------------------------------------- 4. 因子与评分
def build_factors(o, feat: pd.DataFrame, qual_funds: set,
                  pairs: pd.DataFrame, f10s: dict, wealth: dict) -> pd.DataFrame:
    """合格基金 6 因子 + 池内分位 + W3 分（检验18 同口径，现时点 shift=0）。"""
    proxy_close = {b: G._load_close(p) for p, b in G.STYLE_PROXIES.items()}
    back_win = [(o - pd.DateOffset(years=k), o - pd.DateOffset(years=k - 1))
                for k in range(1, 6)]
    proxy_y, proxy_5y = {}, {}
    for bkt, cl in proxy_close.items():
        if cl is None:
            proxy_y[bkt] = [None] * 5
            proxy_5y[bkt] = None
            continue
        proxy_y[bkt] = [s_total(cl, a, b) for a, b in back_win]
        proxy_5y[bkt] = s_ann(cl, o - pd.DateOffset(years=5), o)
    comp5 = {t: comp_ann(w, o - pd.DateOffset(years=5), o)
             for t, w in wealth.items()}

    rows = {}
    for c in qual_funds:
        if c not in feat.index:
            continue
        f = feat.loc[c]
        bkt = f["bucket"]
        if bkt in proxy_y and proxy_y[bkt][0] is not None:
            adj = [None if (f[f"back{j}"] is None or proxy_y[bkt][j] is None)
                   else f[f"back{j}"] - proxy_y[bkt][j] for j in range(5)]
            a5 = (None if (pd.isna(f["ret5y"]) or proxy_5y.get(bkt) is None)
                  else f["ret5y"] - proxy_5y[bkt])
        else:                                    # 金融价值/工业/均衡等无代理 → 类型合成
            adj = [f[f"cexc_{j}"] for j in range(5)]
            a5 = (None if (pd.isna(f["ret5y"]) or comp5.get(f["type"]) is None)
                  else f["ret5y"] - comp5[f["type"]])
        ps = [f[f"ps_{j}"] for j in range(5)]
        ok_ps = all(not pd.isna(x) for x in ps)
        ok_adj = all(x is not None for x in adj)
        rows[c] = dict(
            F1=float(np.mean(ps)) if ok_ps else None,
            F2=float(sum(1 for x in ps if x > 50)) if ok_ps else None,
            F3=a5,
            F4=(-float(np.std(adj, ddof=1))) if ok_adj else None,
            F5=(sum(adj) - max(adj)) if ok_adj else None,
            bucket=bkt, type=f["type"], ret5y=f["ret5y"])
    df = pd.DataFrame(rows).T
    if df.empty:
        return df
    # fund 级任期（现任经理最长）/ 从业（pair 最大）
    ten = {}
    for c in df.index:
        info = f10s.get(c)
        starts = []
        for m in (info or {}).get("managers") or []:
            try:
                starts.append((o - pd.Timestamp(str(m.get("start") or "")[:10])).days
                              / 365.25)
            except Exception:
                pass
        ten[c] = max(starts) if starts else np.nan
    df["ten"] = pd.Series(ten, dtype="float64")
    df["car"] = pairs.groupby("code")["career_y"].max().reindex(df.index)
    df["F6"] = pd.to_numeric(df["ten"], errors="coerce").add(
        pd.to_numeric(df["car"], errors="coerce"), fill_value=np.nan) / 2
    # 合格池内分位（0-100）→ W3 加权
    for col in ["F1", "F2", "F3", "F4", "F5", "ten", "car"]:
        v = pd.to_numeric(df[col], errors="coerce")
        df[col + "_p"] = (v.rank(pct=True) * 100).round(2)
    df["F6_p"] = df[["ten_p", "car_p"]].mean(axis=1)
    cols = [f + "_p" for f in W3]
    m = df[cols].astype(float)
    wts = pd.Series([W3[f] for f in W3], index=cols)
    df["score"] = ((m * wts).sum(axis=1) / sum(W3.values())).round(1)
    return df


# ---------------------------------------------------------------- 5. 名单状态机
def roster_update(state: dict, cand: dict, eval_now: bool,
                  today: str, asof) -> tuple[dict, list]:
    """L3 复评状态机（纯函数，可单测）。

    cand = {key: {name, company, career, channel, qual_funds, pass_funds,
                  data_complete, has_pairs}}
      · 资格失效（离职/整组数据不全且无入围）→ 每次构建即时出，不受双周保护
      · 考卷只在 eval_now（周节奏 + 净值有更新）时判：首过不出，连续两次不过才出
      · 换榜（毕业 新→老 / 数据修正 老→新）当期即考目标榜卷：过则迁移（榜龄保留），
        不过落榜（§2.4 毕业原文）
    """
    members = state.setdefault("members", {})
    exits = state.setdefault("exits", [])
    changes: list[str] = []

    def _exit(key, reason):
        m = members.pop(key)
        exits.append(dict(key=key, name=m.get("name", ""), date=today,
                          reason=reason, list=m.get("list", "")))
        changes.append(f"出榜 {key}（{reason}）")

    # 即时硬条件（每次构建都查）
    for key in list(members):
        c = cand.get(key)
        if c is None or not c["has_pairs"]:
            _exit(key, "离职")
            continue
        if c["channel"] == "out":
            _exit(key, "从业出闸")
            continue
        if c["channel"] == "unknown":
            continue                              # 从业数据缺失 → 冻结不判
        if not c["qual_funds"] and c["data_complete"]:
            _exit(key, "资格失效")
            continue
    if not eval_now:
        return state, changes

    # 考卷复评 + 换榜
    for key in list(members):
        c = cand.get(key)
        m = members[key]
        if c is None or c["channel"] in ("out", "unknown"):
            continue                              # 上面已处理/冻结
        if not c["qual_funds"]:
            continue                              # 数据不全冻结，留待下期
        target = "vet" if c["channel"] == "A" else "new"
        passed = bool(c["pass_funds"])
        if passed:
            if m.get("list") != target:
                changes.append(f"换榜 {key}: {m.get('list')}→{target}")
                if target == "vet":
                    m["graduated"] = today        # 毕业标记（榜龄保留=换榜非出榜）
            m["list"] = target
            m["funds"] = list(c["pass_funds"])
            m["fails"] = 0
        else:
            if m.get("list") != target:
                _exit(key, "毕业未过老将卷" if target == "vet" else "换榜未过新锐卷")
                continue
            m["fails"] = m.get("fails", 0) + 1
            if m["fails"] >= 2:
                _exit(key, "连续两次不过")         # 双周确认
                continue
            changes.append(f"首过不出 {key}（连续不过 {m['fails']}）")
        m["last_eval"] = today
        m["name"] = c["name"]
        m["company"] = c["company"]

    # 新候选入场
    for key, c in cand.items():
        if key in members or not c["pass_funds"]:
            continue
        if c["channel"] not in ("A", "B"):
            continue
        members[key] = dict(list="vet" if c["channel"] == "A" else "new",
                            entry=today, funds=list(c["pass_funds"]),
                            fails=0, last_eval=today,
                            name=c["name"], company=c["company"])
        changes.append(f"入场 {key}（{'老将' if c['channel'] == 'A' else '新锐'}）")

    state["eval_no"] = int(state.get("eval_no", 0)) + 1
    state["eval_date"] = today
    state["last_asof"] = str(asof)[:10] if asof is not None else None
    return state, changes


def load_roster() -> dict | None:
    if not ROSTER_PATH.exists():
        return None
    try:
        return json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_roster(state: dict) -> None:
    state["exits"] = state.get("exits", [])[-50:]        # 审计留痕，封顶50条
    ROSTER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(ROSTER_PATH) + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(ROSTER_PATH)


# ---------------------------------------------------------------- 6. 候选
def build_candidates(pairs: pd.DataFrame, qual: pd.DataFrame, exams: dict,
                     fch: pd.Series, f10s: dict, nav_ok: set) -> dict:
    """{mgr|company: {...}}——通道按本人从业；过考基金=本人入围基金里过本人卷的。

    B 卷还要求**基金通道=B**（验证脚本同口径：与资深共管的基金算资深的卷，
    新锐不能借它入新锐榜）。data_complete：本人所有在管基金都有可用 F10
    （否则"资格失效 vs 数据不全"分不清 → 冻结不判）。
    """
    qf: dict = {}
    if not qual.empty:
        for r in qual.itertuples(index=False):
            qf.setdefault(f"{r.mgr}|{r.company}", []).append(r)
    pf: dict = {}
    for r in pairs.itertuples(index=False):
        pf.setdefault(f"{r.mgr}|{r.company}", []).append(r.code)
    cand: dict = {}
    for key, rows in pf.items():
        mgr, company = key.split("|", 1)
        cand[key] = dict(mgr=mgr, company=company, name=mgr,
                         career=None, channel="unknown",
                         qual_funds=[], pass_funds=[],
                         data_complete=False, has_pairs=True)
    # 本人从业 = 名下 pair 最大值
    for r in pairs.itertuples(index=False):
        key = f"{r.mgr}|{r.company}"
        cy = r.career_y
        if cy is not None and cy == cy:
            cur = cand[key]["career"]
            if cur is None or cy > cur:
                cand[key]["career"] = float(cy)
    for key, c in cand.items():
        c["channel"] = G.channel(c["career"])
        rows = qf.get(key, [])
        c["qual_funds"] = [r.code for r in rows]
        for r in rows:
            tag = exams.get(r.code)
            if not tag:
                continue
            if c["channel"] == "A" and "G3s" in tag:
                c["pass_funds"].append(r.code)
            elif (c["channel"] == "B" and "G4s" in tag
                  and fch.get(r.code) == "B"):
                c["pass_funds"].append(r.code)
        codes = pf.get(key, [])
        c["data_complete"] = (bool(codes)
                              and all(_f10_ok(f10s.get(x)) for x in codes)
                              and all(x in nav_ok for x in codes))
    return cand


# ---------------------------------------------------------------- 7. 明细块
def _nav_at(nav: pd.DataFrame, d):
    sub = nav[nav["date"] <= d]
    return float(sub["nav"].iloc[-1]) if len(sub) else None


def _mgr_detail(codes: list[str], mgr: str, feat: pd.DataFrame,
                fdf: pd.DataFrame, f10s: dict, wealth: dict, o,
                weekly_ex: dict) -> dict:
    """代表作3只 + 5 分析块（a 净值曲线/b 逐年/c 逐季胜率/d 风格暴露/e 风险）。"""
    # ---- 代表作：R1 任期最久 / R2 规模最大 / R3 业绩最佳（F1=同格平均排位），去重
    def _tenure(code):
        info = f10s.get(code) or {}
        best = -1
        for m in info.get("managers") or []:
            if m.get("name") != mgr:
                continue
            try:
                d = (o - pd.Timestamp(str(m.get("start") or "")[:10])).days
                best = max(best, d)
            except Exception:
                pass
        return best

    def _scale(code):
        v = (f10s.get(code) or {}).get("scale_yi")
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _f1(code):
        if code not in fdf.index:
            return None
        v = fdf.at[code, "F1"]
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return None if np.isnan(v) else v

    reps, seen = [], set()
    picks = (
        ("任期最久", max(codes, key=_tenure, default=None)),
        ("规模最大", max((c for c in codes if _scale(c) is not None),
                         key=_scale, default=None)),
        ("业绩最佳", max((c for c in codes if _f1(c) is not None),
                         key=_f1, default=None)))
    for rule, code in picks:
        if code is None or code in seen:
            continue
        seen.add(code)
        info = f10s.get(code) or {}
        mg = next((m for m in info.get("managers") or []
                   if m.get("name") == mgr), None)
        try:
            ten = round((o - pd.Timestamp(str((mg or {}).get("start") or "")[:10])).days
                        / 365.25, 1)
        except Exception:
            ten = None
        r5 = feat.at[code, "ret5y"] if code in feat.index else None
        try:
            r5 = float(r5)
        except (TypeError, ValueError):
            r5 = None
        if r5 is not None and np.isnan(r5):
            r5 = None
        t = feat.at[code, "type"] if code in feat.index else None
        comp5 = (comp_ann(wealth[t], o - pd.DateOffset(years=5), o)
                 if t in wealth else None)
        reps.append(dict(code=code,
                         name=feat.at[code, "name"] if code in feat.index else code,
                         rule=rule, scale=_scale(code), tenure=ten,
                         ret5y=None if r5 is None else round(r5, 4),
                         excess=None if (r5 is None or comp5 is None)
                         else round(r5 - comp5, 4)))
    empty = dict(reps=reps, trend=None, yearly=[], quarterly=[],
                 style=None, risk=None)
    if not reps:
        return empty
    primary = reps[0]["code"]                       # R1 任期最久 = 行内代表作
    nav = _read_nav_pickle(primary)
    if nav is None:
        return empty
    t = feat.at[primary, "type"] if primary in feat.index else None
    wealth_t = wealth.get(t)

    # ---- a. 5 年净值曲线 vs 类型合成（月频，归一化累计收益）
    trend = None
    a5 = o - pd.DateOffset(years=5)
    try:
        nm = nav[nav["date"] > a5].set_index("date")["nav"].resample("ME").last()
        nm = nm.dropna()
        if wealth_t is not None and len(nm) >= 2:
            bm = wealth_t[(wealth_t.index > a5)
                          & (wealth_t.index <= o)].resample("ME").last().dropna()
            idx = nm.index.union(bm.index)
            nf = nm.reindex(idx).ffill()
            bf = bm.reindex(idx).ffill()
            ok = nf.notna() & bf.notna()
            nf, bf = nf[ok], bf[ok]
            if len(nf) >= 2:
                trend = dict(d=[str(d.date()) for d in nf.index],
                             f=[round(v / nf.iloc[0] - 1, 5) for v in nf],
                             b=[round(v / bf.iloc[0] - 1, 5) for v in bf])
    except Exception:
        trend = None

    # ---- b. 逐年收益（日历年，今年=至今）
    yearly = []
    if wealth_t is not None:
        for y in range(o.year - 4, o.year + 1):
            e = pd.Timestamp(year=y, month=12, day=31)
            p_end = _nav_at(nav, min(e, o))
            p_prev = _nav_at(nav, pd.Timestamp(year=y - 1, month=12, day=31))
            if p_end is None or p_prev is None or p_prev <= 0:
                continue
            fr = p_end / p_prev - 1
            sub = wealth_t[(wealth_t.index > pd.Timestamp(year=y - 1, month=12, day=31))
                           & (wealth_t.index <= min(e, o))]
            br = float(sub.iloc[-1] / sub.iloc[0] - 1) if len(sub) >= 2 else None
            yearly.append(dict(y=f"{y}至今" if y == o.year else str(y),
                               f=round(fr, 4),
                               b=None if br is None else round(br, 4),
                               e=None if br is None else round(fr - br, 4)))

    # ---- c. 近 20 季超额胜率
    quarterly = []
    if wealth_t is not None:
        qends = [q for q in (p_.end_time.normalize()
                             for p_ in pd.period_range(end=o.to_period("Q"),
                                                       periods=22, freq="Q"))
                 if q <= o]
        for prev, cur in zip(qends[:-1], qends[1:]):
            p_prev, p_cur = _nav_at(nav, prev), _nav_at(nav, cur)
            if p_prev is None or p_cur is None or p_prev <= 0:
                continue
            sub = wealth_t[(wealth_t.index > prev) & (wealth_t.index <= cur)]
            if len(sub) < 2:
                continue
            fr = p_cur / p_prev - 1
            br = float(sub.iloc[-1] / sub.iloc[0] - 1)
            quarterly.append(dict(q=f"{cur.year}Q{cur.quarter}",
                                 f=round(fr, 4), b=round(br, 4),
                                 e=round(fr - br, 4), win=bool(fr > br)))
        quarterly = quarterly[-20:]

    # ---- d. 风格暴露时序：滚动 1 年超额 β（vs6 风格代理），季频
    style = None
    try:
        mkt_w = weekly_ex.get("mkt")
        if mkt_w is not None and len(mkt_w) > 60:
            qdates = [q for q in (p_.end_time.normalize()
                                  for p_ in pd.period_range(end=o.to_period("Q"),
                                                            periods=21, freq="Q"))
                      if q <= o][-20:]
            w = nav.set_index("date")["nav"].resample("W-FRI").last().dropna()
            fw = w.pct_change().dropna()
            fex = (fw - mkt_w.reindex(fw.index)).dropna()
            betas = {}
            for st, pex in weekly_ex.get("prox", {}).items():
                series = []
                for q in qdates:
                    a = q - pd.Timedelta(weeks=52)
                    f1_ = fex[(fex.index > a) & (fex.index <= q)]
                    p1_ = pex[(pex.index > a) & (pex.index <= q)]
                    both = pd.concat([f1_, p1_], axis=1, keys=["f", "p"]).dropna()
                    if len(both) >= 40 and both["p"].var() > 0:
                        series.append(round(float(np.cov(both["f"], both["p"])[0, 1]
                                                  / both["p"].var()), 3))
                    else:
                        series.append(None)
                betas[st] = series
            if qdates and any(any(v is not None for v in s)
                              for s in betas.values()):
                style = dict(d=[str(q.date()) for q in qdates], s=betas)
    except Exception:
        style = None

    # ---- e. 风险（5 年窗）
    risk = None
    try:
        mdd = M.max_drawdown(nav, 5)
        vol = M.ann_vol(nav, 5)
        r5y = M.ann_return(nav, 5)
        risk = dict(mdd=None if mdd is None else round(mdd, 4),
                    vol=None if vol is None else round(vol, 4),
                    calmar=None if (not mdd or not r5y) else round(r5y / mdd, 3))
    except Exception:
        risk = None
    return dict(reps=reps, trend=trend, yearly=yearly, quarterly=quarterly,
                style=style, risk=risk)


# ---------------------------------------------------------------- 8. 主构建
def build(refresh: bool = True, workers: int = 8, log=print) -> dict | None:
    t0 = time.time()
    pool = _pool(refresh)
    pairs = G.career_pairs(_mgr_table(refresh), pool["code"].tolist())
    log(f"[1/6] 池 {len(pool)} 只 / 从业对 {len(pairs)}")

    navres = _nav_stats(pool["code"].tolist(), refresh, workers, log)
    s4 = [c for c, v in navres["stats"].items() if v[0] >= G.NAV_MIN_Y]
    log(f"[2/6] S4 净值≥5年 {len(s4)} 只")

    styles = G.classify_styles(s4) or {}
    log(f"[3/6] 风格分类 {len(styles)} 只（缓存7天）")

    feat_bundle = build_features(pool, styles, log)
    feat, o, asof = feat_bundle["feat"], feat_bundle["o"], feat_bundle["asof"]
    wealth = feat_bundle.get("wealth") or {}

    # F10 + 资格 + 考卷
    cand_codes = sorted({r.code for r in pairs.itertuples(index=False)
                         if r.career_y is not None and r.career_y == r.career_y
                         and G.channel(r.career_y) in ("A", "B")
                         and r.code in feat.index})
    f10s = _f10s(cand_codes, refresh, workers, log)
    qual = qualify_pairs(pairs, feat, f10s, o)
    qset = set(qual["code"]) if not qual.empty else set()
    exams = exam_flags(feat)
    fch = fund_channels(pairs)
    log(f"[4/6] 入围 pair {len(qual)} / 入围基金 {len(qset)}")

    # 因子 + W3 分
    fdf = build_factors(o, feat, qset, pairs, f10s, wealth)
    if fdf.empty:
        log("[5/6] 无因子可算")
        fdf = pd.DataFrame()
    else:
        sc = pd.to_numeric(fdf["score"], errors="coerce")
        log(f"[5/6] 因子可算 {len(fdf)} 只，均分 {sc.mean():.1f}")

    # 候选 + 名单状态机
    cand = build_candidates(pairs, qual, exams, fch, f10s, navres["ok"])
    today = str(pd.Timestamp.today().date())
    state = load_roster() or dict(members={}, exits=[], eval_no=0)
    last_eval = state.get("eval_date")
    asof_s = str(asof)[:10] if asof is not None else ""
    eval_now = (not last_eval) or (
        str(state.get("last_asof") or "") != asof_s
        and (pd.Timestamp.today() - pd.Timestamp(last_eval)).days >= 6)
    state, changes = roster_update(state, cand, eval_now, today, asof)
    save_roster(state)
    n_v = sum(1 for m in state["members"].values() if m["list"] == "vet")
    n_n = sum(1 for m in state["members"].values() if m["list"] == "new")
    log(f"[6/6] 名单复评：{'执行' if eval_now else '未到节奏/净值未更新→跳过'}"
        f"（老将 {n_v} / 新锐 {n_n}，第 {state.get('eval_no', 0)} 期）")
    for s in changes:
        log(f"      · {s}")

    # ---- payload
    weekly_ex = {"mkt": None, "prox": {}}
    try:
        mkt = G._load_close(G.STYLE_MARKET)
        if mkt is not None:
            weekly_ex["mkt"] = mkt.resample("W-FRI").last().pct_change().dropna()
            for pname, bkt in G.STYLE_PROXIES.items():
                cl = G._load_close(pname)
                if cl is None:
                    continue
                pw = cl.resample("W-FRI").last().pct_change().dropna()
                weekly_ex["prox"][bkt] = (
                    pw - weekly_ex["mkt"].reindex(pw.index)).dropna()
    except Exception as e:
        log(f"      [warn] 风格周频序列不可用（{e}）→ 明细 d 块降级")

    qual_by_key: dict = {}
    if not qual.empty:
        for k, g in qual.groupby(["mgr", "company"]):
            qual_by_key[f"{k[0]}|{k[1]}"] = g

    def _score_of(key, c):
        mem = state["members"].get(key)
        basis = []
        if mem:
            basis = [x for x in mem.get("funds", []) if x in fdf.index]
        if not basis:
            basis = [x for x in c["qual_funds"] if x in fdf.index]
        if not basis or fdf.empty:
            return None
        v = pd.to_numeric(fdf.loc[basis, "score"], errors="coerce").max()
        return None if pd.isna(v) else float(v)

    lists = {"vet": [], "new": []}
    detail = {}
    for key, m in state["members"].items():
        c = cand.get(key)
        if c is None:
            continue                                # 已被硬条件剔除
        sc = _score_of(key, c)
        qg = qual_by_key.get(key)
        qcodes = qg["code"].tolist() if qg is not None else []
        try:
            det = _mgr_detail(qcodes, c["mgr"], feat, fdf, f10s, wealth,
                              o, weekly_ex)
        except Exception as e:
            log(f"      [warn] 明细构建失败 {key}: {e}")
            det = dict(reps=[], trend=None, yearly=[], quarterly=[],
                       style=None, risk=None)
        rep0 = det["reps"][0] if det["reps"] else {}
        bucket = "均衡"
        if rep0 and rep0.get("code") in feat.index:
            bucket = display_bucket(feat.at[rep0["code"], "bucket"])
        entry = str(m.get("entry", today))
        row = dict(key=key, name=c["name"], company=c["company"],
                   career=round(c["career"], 1) if c["career"] else None,
                   tenure=(round(float(qg["tenure_y"].max()), 1)
                           if qg is not None and len(qg) else None),
                   bucket=bucket, score=sc, entry=entry[:7],
                   is_new=(entry == state.get("eval_date")
                           and int(state.get("eval_no", 0)) > 1),
                   n_funds=len(c["qual_funds"]),
                   rep_code=rep0.get("code"), rep_name=rep0.get("name"))
        lst = m.get("list")
        lists[lst if lst in ("vet", "new") else "vet"].append(row)
        detail[key] = det

    def _order(rows):
        return sorted(rows, key=lambda r: (r["score"] is None,
                                           -(r["score"] or 0),
                                           -(r["career"] or 0),
                                           r["name"]))
    for k in lists:
        lists[k] = _order(lists[k])
        for i, r in enumerate(lists[k], 1):
            r["rank"] = i

    buckets = sorted({r["bucket"] for r in lists["vet"] + lists["new"]},
                     key=lambda b: CANON7.index(b) if b in CANON7 else 99)
    payload = dict(asof=asof_s or None,
                   eval_date=state.get("eval_date"),
                   eval_no=int(state.get("eval_no", 0)),
                   buckets=buckets, vet=lists["vet"], new=lists["new"],
                   detail=detail)

    # ---- CSV 回核
    try:
        mgr_rows = [dict(list_=k, **{kk: vv for kk, vv in r.items()
                                     if kk != "key"})
                    for k in ("vet", "new") for r in lists[k]]
        pd.DataFrame(mgr_rows).to_csv(CSV_MGR, index=False, encoding="utf-8-sig")
        frows = []
        for r in qual.itertuples(index=False):
            tag = exams.get(r.code) or ""
            fr = fdf.loc[r.code] if (not fdf.empty and r.code in fdf.index) else None
            row = dict(code=r.code, mgr=r.mgr, company=r.company,
                       channel=r.channel, tenure_y=r.tenure_y,
                       bucket=feat.at[r.code, "bucket"],
                       g3s="G3s" in tag, g4s="G4s" in tag,
                       **{f"ps_{j}": feat.at[r.code, f"ps_{j}"] for j in range(5)})
            if fr is not None:
                for k in ["F1", "F2", "F3", "F4", "F5", "F6", "score"]:
                    row[k] = fr.get(k)
            frows.append(row)
        pd.DataFrame(frows).to_csv(CSV_FUND, index=False, encoding="utf-8-sig")
    except Exception as e:
        log(f"      [warn] CSV 写出失败（{e}）")

    log(f"完成：老将榜 {len(payload['vet'])} 人 / 新锐榜 {len(payload['new'])} 人，"
        f"明细 {len(detail)}，耗时 {(time.time() - t0) / 60:.1f}min"
        f" → eq_score_{{mgrs,funds}}.csv")
    return payload


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fundscorer.eqscore",
                                 description="偏股两榜评分引擎（docs/20 §2.2/§2.4）")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--offline", action="store_true",
                    help="不刷旧缓存（净值/池/经理表走缓存，F10 只读不抓）")
    args = ap.parse_args(argv)
    payload = build(refresh=not args.offline, workers=args.workers)
    if payload is None:
        print("构建失败")
        return 1
    print("\n老将榜 top5：")
    for r in payload["vet"][:5]:
        print(f"  {r['rank']}. {r['name']}({r['company']}) {r['score']}分 "
              f"从业{r['career']}年 {r['bucket']} 代表作 {r['rep_name']}")
    print("\n新锐榜 top5：")
    for r in payload["new"][:5]:
        print(f"  {r['rank']}. {r['name']}({r['company']}) {r['score']}分 "
              f"从业{r['career']}年 {r['bucket']} 代表作 {r['rep_name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
