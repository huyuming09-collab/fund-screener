# -*- coding: utf-8 -*-
"""偏股闸 v2「持续优秀筛选」设计验证（docs/20 §2.4 的数据依据）。

三项检验（全部只用本地缓存，无网络）：
  1) 伪前瞻：把评估时点拨回 T0=1年前，仅用 ≤T0 数据重跑闸 → pass@T0，
     看 (T0,T1] 前瞻超额：pass vs 对照组（career/S3/S4 全合格、但分位/底线未过）。
     闸若真选出"持续优秀"，pass 应显著跑赢对照。
  2) 换血率：pass@T0 vs 真实 pass@T1 的名单留存（基金级+经理级），
     并模拟"迟滞退出"（在榜者只查硬条件：S2-C 底线）→ 为防换血机制定量。
     附 90 天 / 半年两个中间时点画换血曲线；顺带量化风格分类本身的时变率。
  3) 运气画像：pass@T0 内按 逐年胜率/去最佳年仍为正/绝对超额>0 分组，
     看前瞻差异 → 定防运气规则的阈值与取舍。

规则集（候选，全部在 pass@T0 上测）：
  R1 现闸；R2=R1+3y双窗也过 p80；R3=R1+5年中≥3年超额为正；
  R4=R1+去掉最佳年后超额仍为正；R5=R1+5年绝对超额>0；
  C1=R1∧R2∧R3；C2=R1∧R3∧R5

运行（仓库根目录）：python scripts/eq_sustain_check.py
结论写入 docs/20 §2.4；本脚本保留供复跑回核。
"""
from __future__ import annotations

import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")          # pandas 弃用警告刷屏（曾致 stderr 200KB）
import pandas as pd                         # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from fundscorer import data as D              # noqa: E402
from fundscorer import eqgate as G            # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
NAV_DIR = ROOT / "data" / "cache"
T1 = pd.Timestamp("2026-11-12")               # 与真实闸运行日一致（S3 用 now 判定）
DELTA = {"T0": 365, "T90": 90, "T05": 183}    # 回拨天数；T1=现时点
RULES_FULL = ["R1", "R2", "R3", "R4", "R5", "C1", "C2", "R6"]
RULES_LIGHT = ["R1"]                          # 中间时点只算现闸（换血曲线）


# ---------------------------------------------------------------- 工具
def nav_ann(navT: pd.DataFrame, years: int) -> float | None:
    """与 metrics.ann_return 同口径：end−N年 窗口，不足2点 → None。"""
    end = navT["date"].max()
    sub = navT[navT["date"] > end - pd.DateOffset(years=years)]
    if len(sub) < 2:
        return None
    days = (sub["date"].iloc[-1] - sub["date"].iloc[0]).days
    if days <= 0:
        return None
    return float((sub["nav"].iloc[-1] / sub["nav"].iloc[0]) ** (365.0 / days) - 1)


def build_composite() -> dict:
    """类型合成基准日财富序列（w×沪深300 + (1−w)×中债新综合），供任意窗口切片。"""
    hs = G._load_close(G.STYLE_MARKET)
    bond = (ROOT / "data" / "cache" / "bond_index_wealth.pkl")
    bond = pd.read_pickle(bond).set_index("date")["value"]
    r_hs = hs.sort_index().pct_change()
    r_b = bond.sort_index().pct_change()
    idx = r_hs.index.intersection(r_b.index)
    out = {}
    for t, w in G.BENCH_W.items():
        r = (w * r_hs.loc[idx] + (1 - w) * r_b.loc[idx]).dropna()
        out[t] = (1 + r).cumprod()
    return out


def comp_total(wealth: pd.Series, a: pd.Timestamp, b: pd.Timestamp) -> float | None:
    sub = wealth[(wealth.index > a) & (wealth.index <= b)]
    if len(sub) < 2:
        return None
    return float(sub.iloc[-1] / sub.iloc[0] - 1)


def comp_ann(wealth: pd.Series, a: pd.Timestamp, b: pd.Timestamp) -> float | None:
    sub = wealth[(wealth.index > a) & (wealth.index <= b)]
    if len(sub) < 2:
        return None
    days = (sub.index[-1] - sub.index[0]).days
    if days <= 0:
        return None
    return float((sub.iloc[-1] / sub.iloc[0]) ** (365.0 / days) - 1)


# ---------------------------------------------------------------- 特征（一次读nav，多时点复用）
def build_features(codes: list[str], T_keys: list[str]) -> dict:
    """{Tkey: DataFrame[code → span,r5,r3,yearly,bucket,style_src]}。
    每只基金读一次 nav；T1/T0 全量（含风格相关性分类与逐年切片），
    中间时点只算 span/r5/r3、风格复用 T1（5y 窗口下风格稳定，另出失效率）。"""
    T_map = {"T1": T1}
    for k, d in DELTA.items():
        T_map[k] = T1 - pd.Timedelta(days=d)

    # 代理与市场（T1/T0 各算一份周频超额）
    mkt = G._load_close(G.STYLE_MARKET)
    prox_ex = {}
    for Tkey in ("T1", "T0"):
        T = T_map[Tkey]
        mkt_w = mkt.resample("W-FRI").last().pct_change().dropna()
        mkt_w = mkt_w[mkt_w.index <= T]
        d = {}
        for nm in G.STYLE_PROXIES:
            s = G._load_close(nm).resample("W-FRI").last().pct_change().dropna()
            s = s[s.index <= T]
            d[G.STYLE_PROXIES[nm]] = (s - mkt_w).reindex(mkt_w.index)
        prox_ex[Tkey] = (mkt_w, d)

    rows = {k: [] for k in T_keys}
    t0 = time.time()
    for i, code in enumerate(codes, 1):
        p = NAV_DIR / f"nav_{code}.pkl"
        if not p.exists():
            continue
        try:
            nav = pd.read_pickle(p)
        except Exception:
            continue
        if "nav" not in nav.columns or nav.empty:
            continue
        name_type = CODE_INFO.get(code)
        if name_type is None:
            continue
        name, ptype = name_type
        rec = {}
        for Tkey in T_keys:
            T = T_map[Tkey]
            navT = nav[nav["date"] <= T]
            if navT.empty:
                continue
            span = (navT["date"].max() - navT["date"].min()).days / 365.25
            if span < G.NAV_MIN_Y:
                continue
            r5 = nav_ann(navT, 5)
            r3 = nav_ann(navT, 3)
            yearly = None
            if Tkey in ("T1", "T0"):
                # 5 个等长年度窗（锚在 T），任一窗数据不足 → yearly=None（R3/R4 判失败）
                ys, ok = [], True
                for k in range(1, 6):
                    a = T - pd.DateOffset(years=k)
                    b = T - pd.DateOffset(years=k - 1)
                    sub = navT[(navT["date"] > a) & (navT["date"] <= b)]
                    if len(sub) < 180 or (sub["date"].iloc[-1] - sub["date"].iloc[0]).days < 300:
                        ok = False
                        break
                    ys.append(float(sub["nav"].iloc[-1] / sub["nav"].iloc[0] - 1))
                yearly = ys if ok else None
                # 风格：周频超额相关（与 eqgate 同口径）
                w = navT.set_index("date")["nav"].resample("W-FRI").last().dropna()
                fw = w.pct_change().dropna()
                mkt_w, pdx = prox_ex[Tkey]
                fr_ex = (fw - mkt_w.reindex(fw.index)).dropna()
                style, src = None, "name"
                if len(fr_ex) >= 100:
                    corrs = {}
                    for st, pex in pdx.items():
                        both = pd.concat([fr_ex, pex], axis=1).dropna()
                        if len(both) >= 100:
                            corrs[st] = both.iloc[:, 0].corr(both.iloc[:, 1])
                    if corrs:
                        best = max(corrs, key=corrs.get)
                        if corrs[best] >= G.STYLE_MIN_CORR:
                            style, src = best, "corr"
                if style is None:
                    style = G.style_bucket(name)
                rec_style = (style, src)
            else:
                rec_style = None            # 中间时点复用 T1 风格
            rec[Tkey] = dict(span=span, r5=r5, r3=r3, yearly=yearly,
                             _style=rec_style)
        # 中间时点补 T1 风格
        t1_style = rec.get("T1", {}).get("_style")
        for Tkey in T_keys:
            if Tkey in rec and rec[Tkey]["_style"] is None:
                rec[Tkey]["_style"] = t1_style
        for Tkey, v in rec.items():
            st, src = v["_style"] or (G.style_bucket(name), "name")
            rows[Tkey].append(dict(code=code, name=name, type=ptype,
                                   span=v["span"], r5=v["r5"], r3=v["r3"],
                                   yearly=v["yearly"], bucket=st, style_src=src))
        if i % 2000 == 0:
            print(f"  features {i}/{len(codes)}  {(time.time()-t0)/60:.1f}min",
                  flush=True)
    return {k: pd.DataFrame(v).set_index("code") for k, v in rows.items() if v}


# ---------------------------------------------------------------- 阈值与分位（与 eqgate 同口径）
def build_thr(feat: pd.DataFrame, col: str) -> dict:
    s4 = feat.dropna(subset=[col])
    thr = {"cell": {}, "bucket": {}, "type": {}}
    for (t, b), g in s4.groupby(["type", "bucket"]):
        if len(g) >= 10:
            thr["cell"][(t, b)] = float(g[col].quantile(G.TOP_PCT))
    for b, g in s4.groupby("bucket"):
        if len(g) >= 10:
            thr["bucket"][b] = float(g[col].quantile(G.TOP_PCT))
    for t, g in s4.groupby("type"):
        if len(g) >= 10:
            thr["type"][t] = float(g[col].quantile(G.TOP_PCT))
    return thr


def add_pctl(feat: pd.DataFrame) -> pd.DataFrame:
    feat = feat.copy()
    feat["pctl"] = None
    for keys in (["type", "bucket"], ["bucket"], ["type"]):
        mask = feat["pctl"].isna() & feat["r5"].notna()
        for _k, g in feat[mask].groupby(keys):
            if len(g) >= 10:
                feat.loc[g.index, "pctl"] = (g["r5"].rank(pct=True) * 100).round(1)
    return feat


# ---------------------------------------------------------------- 闸评估（回拨时点版）
def evaluate(feat: pd.DataFrame, thr5: dict, thr3: dict | None,
             T: pd.Timestamp, delta_days: int, rules: list[str],
             wealth: dict, f10s: dict) -> dict:
    """回拨 delta_days 天重跑闸。返回 {rule: set(基金code)} + 统计 + pair清单。"""
    feat = add_pctl(feat)
    career_shift = delta_days / 365.25
    comp5 = {t: comp_ann(w, T - pd.DateOffset(years=5), T)
             for t, w in wealth.items()}
    out = {r: set() for r in rules}
    out_pairs = {r: set() for r in rules}          # (mgr, company, code)
    stat = dict(eligible_pairs=0, s3_fail=0, floor_cut=0, b_fail=0,
                no_f10=0, not_current=0, unknown_career=0)

    for row in PAIRS.itertuples(index=False):
        career_T = row.career_y - career_shift
        ch = G.channel(career_T)
        if ch == "out":
            continue
        code = row.code
        if code not in feat.index:
            continue
        f = feat.loc[code]
        info = f10s.get(code)
        if not info:
            stat["no_f10"] += 1
            continue
        mg = next((m for m in (info.get("managers") or [])
                   if m.get("name") == row.mgr), None)
        if mg is None:
            stat["not_current"] += 1
            continue
        try:
            start = pd.Timestamp((mg.get("start") or "")[:10])
        except Exception:
            stat["s3_fail"] += 1
            continue
        if start > T - pd.Timedelta(days=G.TENURE_MIN_D):
            stat["s3_fail"] += 1
            continue
        stat["eligible_pairs"] += 1
        # 分位/底线
        if ch == "A":
            if f["pctl"] is not None and not pd.isna(f["pctl"]) \
                    and f["pctl"] <= G.A_FLOOR_PCT:
                stat["floor_cut"] += 1
                continue
        else:
            thrv = G.thr_lookup(thr5, f["type"], f["bucket"])
            if thrv is None or f["r5"] is None or pd.isna(f["r5"]) \
                    or f["r5"] < thrv:
                stat["b_fail"] += 1
                continue
        # ---- 规则 extras（都在 eligible∧过闸 之上叠加）----
        base_ok = True
        extra = {}
        if "R2" in rules or "C1" in rules:
            if thr3 is None:
                extra["R2"] = False
            else:
                t3 = G.thr_lookup(thr3, f["type"], f["bucket"])
                extra["R2"] = (t3 is not None and f["r3"] is not None
                               and not pd.isna(f["r3"]) and f["r3"] >= t3)
        if "R3" in rules or "C1" in rules or "C2" in rules or "R6" in rules:
            ys = f["yearly"]
            if ys is None:
                extra["R3"] = False
                extra["R4"] = False
            else:
                # 年度超额（同类型合成基准逐年对齐）
                w = wealth[f["type"]]
                excs = []
                T_ = T
                for k in range(1, 6):
                    a = T_ - pd.DateOffset(years=k)
                    b = T_ - pd.DateOffset(years=k - 1)
                    ct = comp_total(w, a, b)
                    excs.append(None if ct is None else ys[k - 1] - ct)
                if any(e is None for e in excs):
                    extra["R3"] = extra["R4"] = False
                else:
                    extra["R3"] = sum(1 for e in excs if e > 0) >= 3
                    extra["R4"] = (sum(excs) - max(excs)) > 0
        if "R5" in rules or "C2" in rules or "R6" in rules:
            c5 = comp5.get(f["type"])
            extra["R5"] = (c5 is not None and f["r5"] is not None
                           and not pd.isna(f["r5"]) and f["r5"] > c5)

        def ok(rname):
            if rname == "R1":
                return True
            if rname == "C1":
                return extra.get("R2") and extra.get("R3")
            if rname == "C2":
                return extra.get("R3") and extra.get("R5")
            if rname == "R6":
                return (extra.get("R3") and extra.get("R4")
                        and extra.get("R5"))
            return bool(extra.get(rname))

        for rname in rules:
            if ok(rname):
                out[rname].add(code)
                out_pairs[rname].add((row.mgr, row.company, code))
    return dict(codes=out, pairs=out_pairs, stat=stat)


# ---------------------------------------------------------------- 前瞻超额
def forward_excess(codes, T0, T1, wealth, type_of) -> pd.Series:
    vals = {}
    for c in codes:
        p = NAV_DIR / f"nav_{c}.pkl"
        if not p.exists():
            continue
        nav = pd.read_pickle(p)
        sub = nav[(nav["date"] > T0) & (nav["date"] <= T1)]
        if len(sub) < 2:
            continue
        days = (sub["date"].iloc[-1] - sub["date"].iloc[0]).days
        if days <= 0:
            continue
        f_ann = float((sub["nav"].iloc[-1] / sub["nav"].iloc[0])
                      ** (365.0 / days) - 1)
        c_ann = comp_ann(wealth[type_of[c]], T0, T1)
        if c_ann is None:
            continue
        vals[c] = f_ann - c_ann
    return pd.Series(vals)


def mgr_set(pairs) -> set:
    return {(m, co) for m, co, _c in pairs}


# ---------------------------------------------------------------- main
def main():
    t_start = time.time()
    print("[1] 载入池/经理对/基准 …", flush=True)
    global CODE_INFO, PAIRS
    pool = G.load_pool()
    CODE_INFO = dict(zip(pool["code"], zip(pool["name"], pool["type_text"])))
    mgr_tbl = D._manager_em_table()
    PAIRS = G.career_pairs(mgr_tbl, pool["code"].tolist())
    PAIRS = PAIRS[PAIRS["career_y"].notna()].reset_index(drop=True)
    wealth = build_composite()
    print(f"    池 {len(pool)} / 从业对 {len(PAIRS)}", flush=True)

    print("[2] 特征（一次读 nav，T1/T0 全量 + 两中间时点）…", flush=True)
    codes = pool["code"].tolist()
    feat_cache = ROOT / "data" / "eq_sustain_feat.pkl"
    if feat_cache.exists():
        try:
            feat = pd.read_pickle(feat_cache)
            if set(feat) >= {"T1", "T0", "T90", "T05"}:
                print(f"    命中特征缓存 {feat_cache.name}（跳过计算）", flush=True)
            else:
                feat = None
        except Exception:
            feat = None
    else:
        feat = None
    if feat is None:
        feat = build_features(codes, ["T1", "T0", "T90", "T05"])
        try:
            pd.to_pickle(feat, feat_cache)
            print(f"    特征已缓存 → {feat_cache.name}", flush=True)
        except Exception:
            pass
    for k, v in feat.items():
        print(f"    {k}: S4人群 {len(v)}", flush=True)

    print("[3] F10（补抓后应全覆盖候选）…", flush=True)
    cand = set(PAIRS.loc[PAIRS["career_y"] > G.CAREER_MIN_Y, "code"])
    need = [c for c in cand
            if not (NAV_DIR / f"f10_v3_{c}.pkl").exists()]
    print(f"    候选 {len(cand)}，仍缺 f10 {len(need)}", flush=True)
    f10s = {}
    for c in cand:
        p = NAV_DIR / f"f10_v3_{c}.pkl"
        if p.exists():
            try:
                f10s[c] = pd.read_pickle(p)
            except Exception:
                pass
    print(f"    f10 可用 {len(f10s)}", flush=True)

    # ---- 阈值（T0 与 T1 各建，防未来函数）----
    thr5_T1 = build_thr(feat["T1"], "r5")
    thr5_T0 = build_thr(feat["T0"], "r5")
    thr3_T0 = build_thr(feat["T0"], "r3")

    print("[4] 闸评估：T1（应≈真实结果）/ T0（伪前瞻）/ T90 / T05 …", flush=True)
    res = {}
    res["T1"] = evaluate(feat["T1"], thr5_T1, build_thr(feat["T1"], "r3"),
                         T1, 0, RULES_FULL, wealth, f10s)
    res["T0"] = evaluate(feat["T0"], thr5_T0, thr3_T0,
                         T1 - pd.Timedelta(days=365), 365, RULES_FULL,
                         wealth, f10s)
    for k in ("T90", "T05"):
        Tk = T1 - pd.Timedelta(days=DELTA[k])
        res[k] = evaluate(feat[k], build_thr(feat[k], "r5"), None,
                          Tk, DELTA[k], RULES_LIGHT, wealth, f10s)

    # ---- 校验：T1 复算 vs 真实 CSV ----
    print("\n" + "=" * 60)
    real_f = pd.read_csv(ROOT / "data/eq_gate_funds.csv", dtype={"code": str})
    real_codes = set(real_f["code"].str.zfill(6))
    real_m = pd.read_csv(ROOT / "data/eq_gate_managers.csv")
    real_mgrs = set(zip(real_m["mgr"], real_m["company"]))
    my_codes = res["T1"]["codes"]["R1"]
    my_mgrs = mgr_set(res["T1"]["pairs"]["R1"])
    print(f"[校验] T1复算基金 {len(my_codes)} vs 真实 {len(real_codes)} "
          f"交集 {len(my_codes & real_codes)}")
    print(f"[校验] T1复算经理 {len(my_mgrs)} vs 真实 {len(real_mgrs)} "
          f"交集 {len(my_mgrs & real_mgrs)}")
    print(f"[校验] 特征差异：S4 T1={len(feat['T1'])} vs 真实4864；"
          f"风格来源 {feat['T1']['style_src'].value_counts().to_dict()}")

    # ---- 检验1：伪前瞻 ----
    print("\n" + "=" * 60)
    print("检验1 伪前瞻（T0=2025-11-12 选人 → 未来1年超额，中位数/均值/为正率）")
    type_of = dict(zip(feat["T0"].index, feat["T0"]["type"]))
    ctrl_pairs = res["T0"]["pairs"]["R1"]          # 基础闸的 eligible 已含在 stat；对照=eligible未过闸
    # 对照组：eligible 但不在 R1 通过集（重新扫一遍太贵——用 stat 不够，改为：所有
    # career/S3 合格但 R1 未过的基金。evaluate 已经记录 eligible；这里重建对照：
    elig_codes = _eligible_codes(feat["T0"], T1 - pd.Timedelta(days=365),
                                 f10s, thr5_T0)
    ctrl = elig_codes - res["T0"]["codes"]["R1"]
    fwd = forward_excess(sorted(res["T0"]["codes"]["R1"] | ctrl),
                         T1 - pd.Timedelta(days=365), T1, wealth, type_of)
    mkt_f = comp_ann(wealth["混合型-偏股"], T1 - pd.Timedelta(days=365), T1)
    print(f"    市场基准（偏股合成）前瞻: {mkt_f:+.2%}" if mkt_f else "")
    for label, s in (("pass@T0", fwd[fwd.index.isin(res["T0"]["codes"]["R1"])]),
                     ("对照(eligible未过闸)", fwd[fwd.index.isin(ctrl)])):
        if len(s):
            print(f"    {label}: n={len(s)} 中位={s.median():+.2%} "
                  f"均值={s.mean():+.2%} 为正率={(s > 0).mean():.1%}")
    # 各规则 pass 的前瞻
    print("    —— 各规则 pass@T0 的前瞻表现与规模 ——")
    for r in RULES_FULL:
        cs = res["T0"]["codes"][r]
        s = fwd[fwd.index.isin(cs)]
        if len(s):
            print(f"    {r}: n={len(cs)} 前瞻中位={s.median():+.2%} "
                  f"为正率={(s > 0).mean():.1%}")
        else:
            print(f"    {r}: n={len(cs)}")

    # ---- 检验2：换血率 ----
    print("\n" + "=" * 60)
    print("检验2 换血率（pass@T0 名单在后续时点的留存）")
    for k in ("T90", "T05", "T1"):
        c0, c1 = res["T0"]["codes"]["R1"], res[k]["codes"]["R1"]
        m0 = mgr_set(res["T0"]["pairs"]["R1"])
        m1 = mgr_set(res[k]["pairs"]["R1"])
        f_ret = len(c0 & c1) / len(c0) if c0 else 0
        m_ret = len(m0 & m1) / len(m0) if m0 else 0
        print(f"    T0→{k}: 基金留存 {f_ret:.1%} ({len(c0 & c1)}/{len(c0)}) "
              f"经理留存 {m_ret:.1%} | 新进基金 {len(c1 - c0)}")
    # 迟滞模拟：T0 名单在 T1 只查硬条件（S2-C 底线，即 pctl>10）→ 留存
    feat1 = add_pctl(feat["T1"])
    pctl1 = feat1["pctl"]
    floor_breach = set()
    for _m, _co, code in res["T0"]["pairs"]["R1"]:
        pc = pctl1.get(code)
        if pc is not None and not pd.isna(pc) and pc <= G.A_FLOOR_PCT:
            floor_breach.add(code)
    hyst_f = (len(res["T0"]["codes"]["R1"]) - len(floor_breach)) / \
        len(res["T0"]["codes"]["R1"])
    print(f"    迟滞退出（只查 S2-C 底线）基金留存: {hyst_f:.1%} "
          f"（底线击穿 {len(floor_breach)} 只）")
    # 风格分类时变率（换血的另一个来源）
    both = feat["T1"].join(feat["T0"], lsuffix="_1", rsuffix="_0",
                           how="inner")
    drift = (both["bucket_1"] != both["bucket_0"]).mean()
    print(f"    风格分类 1 年漂移率: {drift:.1%}（T1 与 T0 各自重算）")
    # 严格重跑下 T1 新进（=展示页"换血"的进端）
    print(f"    严格重跑 T1 新进基金 {len(res['T1']['codes']['R1'] - res['T0']['codes']['R1'])}")

    # ---- 检验3：运气画像（pass@T0 内分组 → 前瞻）----
    print("\n" + "=" * 60)
    print("检验3 运气画像（pass@T0=R1 内分组 → 未来1年超额）")
    p0 = res["T0"]["codes"]["R1"]
    f0 = feat["T0"].loc[feat["T0"].index.isin(p0)]
    fwd0 = fwd[fwd.index.isin(p0)]

    def grp(mask, label):
        idx = f0.index[mask.reindex(f0.index, fill_value=False)]
        s = fwd0[fwd0.index.isin(idx)]
        if len(s):
            print(f"    {label}: n={len(s)} 前瞻中位={s.median():+.2%} "
                  f"为正率={(s > 0).mean():.1%}")

    # R3 逐年胜率
    has_y = f0["yearly"].notna()
    pos_years = f0["yearly"].map(
        lambda ys: sum(1 for y in ys if y > 0) if isinstance(ys, list) else None)
    grp(has_y & (pos_years >= 3), "R3过(≥3年正超额)")
    grp(has_y & (pos_years < 3), "R3不过(<3年正超额)")
    # R4 去最佳年
    # R5 绝对超额
    c5 = {t: comp_ann(w, T1 - pd.DateOffset(years=365) - pd.DateOffset(years=5),
                      T1 - pd.Timedelta(days=365))
          for t, w in wealth.items()}
    abs_pos = f0.apply(
        lambda r: (r["r5"] is not None and not pd.isna(r["r5"])
                   and c5.get(r["type"]) is not None
                   and r["r5"] > c5[r["type"]]), axis=1)
    grp(abs_pos, "R5过(5年绝对超额>0)")
    grp(~abs_pos, "R5不过(5年绝对超额≤0)")
    # R4
    r4 = []
    for code, r in f0.iterrows():
        ys = r["yearly"]
        if not isinstance(ys, list):
            r4.append(False)
            continue
        w = wealth[r["type"]]
        excs = []
        for k in range(1, 6):
            a = T1 - pd.Timedelta(days=365) - pd.DateOffset(years=k)
            b = T1 - pd.Timedelta(days=365) - pd.DateOffset(years=k - 1)
            ct = comp_total(w, a, b)
            excs.append(None if ct is None else ys[k - 1] - ct)
        r4.append(bool(excs) and all(e is not None for e in excs)
                  and (sum(excs) - max(excs)) > 0)
    r4 = pd.Series(r4, index=f0.index)
    grp(r4, "R4过(去最佳年仍为正)")
    grp(~r4, "R4不过(收益靠单年)")

    # ---- 检验4：A通道加 R5 的影响（待拍板项量化）----
    print("\n" + "=" * 60)
    print("检验4 若 R5(绝对超额>0) 也用于 A 通道的影响（T1 真实时点）")
    a_pairs = res["T1"]["pairs"]["R1"]
    feat1 = res["T1"]  # stat only
    # A 通道 pair 的 r5 分布
    a_codes = {c for m, co, c in a_pairs
               if G.channel(PAIR_CAREER.get((m, c), 0)) == "A"}
    # 简化：直接用 T1 特征里 A 通道通过基金的 r5>comp 统计
    f1 = feat["T1"]
    a_pass = f1[f1.index.isin(my_codes)]
    # A/B 无法从 feat 区分，这里用真实 CSV 的 channel
    real_f = real_f.copy()
    real_f["bucket2"] = real_f["name"]  # 占位
    a_csv = real_f[real_f["channel"] == "A"].drop_duplicates("code")
    a_in = a_csv[a_csv["code"].isin(f1.index)]
    comp5_1 = {t: comp_ann(w, T1 - pd.DateOffset(years=5), T1)
               for t, w in wealth.items()}
    a_pos = a_in.apply(
        lambda r: r["ret5y"] > (comp5_1.get(r["type_text"]) or 1e9), axis=1)
    print(f"    A通道通过基金 {len(a_in)}：5年绝对超额>0 的 "
          f"{int(a_pos.sum())} ({a_pos.mean():.1%}) → "
          f"若加 R5 会砍 {int((~a_pos).sum())} 只 / "
          f"涉及经理约 {a_csv[a_csv['code'].isin(a_in[~a_pos]['code'])][['mgr','company']].drop_duplicates().shape[0]} 人")

    # ---- 检验5：迟滞出场线（防换血机制选型）----
    print("\n" + "=" * 60)
    print("检验5 迟滞出场线（T0在榜基金 → T1 同格分位分布，定退出线）")
    t0_codes = res["T0"]["codes"]["R1"]
    pcs = []
    for c in t0_codes:
        pc = pctl1.get(c)
        pcs.append(None if pc is None or pd.isna(pc) else float(pc))
    n_none = sum(1 for p in pcs if p is None)
    for line in (10, 20, 30, 50):
        stayed = sum(1 for p in pcs if p is None or p > line)
        print(f"    退出线=同格分位<{line}%：留存 {stayed}/{len(pcs)} "
              f"= {stayed / len(pcs):.1%}")
    print(f"    （T1 分位缺失 {n_none} 只——S4仍过但格<10只回退失败等，按留存计）")

    # ---- 检验6：严格版名单规模（T1 真实时点，防运气规则交集）----
    print("\n" + "=" * 60)
    print("检验6 严格版名单规模（T1 现闸通过者再叠加防运气规则）")
    r1c, r1p = res["T1"]["codes"]["R1"], res["T1"]["pairs"]["R1"]

    def combo(keys):
        cs, ps = set(r1c), set(r1p)
        for k in keys:
            cs &= res["T1"]["codes"][k]
            ps &= res["T1"]["pairs"][k]
        return cs, ps

    for label, keys in [
        ("R1 现闸（基线）", []),
        ("∧R3 逐年≥3年正超额", ["R3"]),
        ("∧R4 去最佳年仍为正", ["R4"]),
        ("∧R5 5年绝对超额>0", ["R5"]),
        ("∧R3∧R5（C2 口径）", ["R3", "R5"]),
        ("∧R3∧R4∧R5（三闸全上=R6）", ["R3", "R4", "R5"]),
    ]:
        cs, ps = combo(keys)
        print(f"    {label}: 基金 {len(cs)} / 经理 {len(mgr_set(ps))}")

    # ---- 检验8：P闸复评的换血代价（在榜者若每年重查防运气规则，多少人掉出）----
    print("\n" + "=" * 60)
    print("检验8 P闸复评代价（T0在榜者 → T1 重查各规则的掉出率）")
    t0_pass = res["T0"]["codes"]["R1"]
    for rule in ("R3", "R4", "R5"):
        still = res["T1"]["codes"].get(rule, set())
        # T0 在榜 ∩ T1 仍过 R1 ∩ T1 过 rule = 复评存活
        alive_r1 = res["T1"]["codes"]["R1"]
        kept = t0_pass & alive_r1 & still
        print(f"    复查{rule}: T0在榜 {len(t0_pass)} → T1仍过 {len(kept)} "
              f"(留存 {len(kept)/len(t0_pass):.1%})")
    # R6 复查
    kept6 = t0_pass & res["T1"]["codes"]["R1"] & res["T1"]["codes"].get("R6", set())
    print(f"    复查R6(三闸): T0在榜 {len(t0_pass)} → T1仍过 {len(kept6)} "
          f"(留存 {len(kept6)/len(t0_pass):.1%})")

    # ---- 检验7：换血来源分解（T1 新进基金从哪来）----
    print("\n" + "=" * 60)
    print("检验7 换血来源分解（T1新进 = T1过闸但T0未过闸）")
    new = res["T1"]["codes"]["R1"] - res["T0"]["codes"]["R1"]
    if new:
        t0_idx = set(feat["T0"].index)
        not_s4_t0 = {c for c in new if c not in t0_idx}
        old = new - not_s4_t0
        elig_t0 = _eligible_codes(feat["T0"],
                                  T1 - pd.Timedelta(days=365), f10s, thr5_T0)
        aged_in = old & elig_t0        # T0 已 S4 且已 eligible → 纯分位/阈值波动
        newly_aged = old - elig_t0     # T0 时 career/tenure 未到龄 → 到龄新达标
        n = len(new)
        print(f"    T1新进 {n} 只，分解：")
        print(f"      a) 新满5年（S4 新达标）: {len(not_s4_t0)} "
              f"({len(not_s4_t0) / n:.1%})")
        print(f"      b) 资历到龄（T0 时 career/任期不足）: {len(newly_aged)} "
              f"({len(newly_aged) / n:.1%})")
        print(f"      c) 纯分位/阈值波动（T0 已合格但未过线）: {len(aged_in)} "
              f"({len(aged_in) / n:.1%})")
    else:
        print("    无新进")

    print(f"\n总耗时 {(time.time()-t_start)/60:.1f}min", flush=True)


def _eligible_codes(feat, T, f10s, thr5) -> set:
    """career/S3 合格（=eligible）的基金集合：与 evaluate 的 stat 口径一致。"""
    out = set()
    career_shift = (T1 - T).days / 365.25
    for row in PAIRS.itertuples(index=False):
        if G.channel(row.career_y - career_shift) == "out":
            continue
        if row.code not in feat.index:
            continue
        info = f10s.get(row.code)
        if not info:
            continue
        mg = next((m for m in (info.get("managers") or [])
                   if m.get("name") == row.mgr), None)
        if mg is None:
            continue
        try:
            start = pd.Timestamp((mg.get("start") or "")[:10])
        except Exception:
            continue
        if start > T - pd.Timedelta(days=G.TENURE_MIN_D):
            continue
        out.add(row.code)
    return out


if __name__ == "__main__":
    # evaluate 内部用到的 pair→career 快查
    PAIR_CAREER = {}
    main()
