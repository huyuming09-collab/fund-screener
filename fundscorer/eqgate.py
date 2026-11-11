# -*- coding: utf-8 -*-
"""偏股资深经理闸 v1（docs/20 §2.1；参数 2026-11-11 用户拍板）。

用户拍板（2026-11-11）：
  · S1 池边界：股票型 + 混合型-偏股 + 混合型-灵活 + 混合型-平衡（QDII/指数/FOF/偏债不入）
  · S2 经理从业**双通道**：从业 >10 年全部入围；>6 年且 ≤10 年须"同类近5年年化超额前10%"；≤6 年出闸
  · S3 现任经理任本基金 ≥5 年；S4 基金净值 ≥5 年
  · 目标：只留最稳定的老将，**结果少是预期不是缺陷**（宁缺毋滥）

口径（docs/20 §2.1 同步）：
  · "同类" = S1 四类型各自为池；前 10% 分位在**同类型、净值≥5 年的全部池内基金**中取
    （含从业<6 年经理的基金——分位是基金级质量指标，与经理从业无关）
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
CAREER_MIN_Y = 6.0        # S2 通道B 下限：>6 年才可走前10%；≤6 年出闸
TOP_PCT = 0.90            # S2 通道B：同类近5年年化收益分位 ≥90%（同类型统一基准下 ≡ 超额分位）
TENURE_MIN_D = 1825       # S3 任本基金 ≥5 年（5×365，与债券池 S3=1095 同口径）
NAV_MIN_Y = 5.0           # S4 净值 ≥5 年
# 类型合成基准的股指权重（华泰口径 90/70/50；平衡型 50/50 为估设）——仅影响展示的超额数值
BENCH_W = {"股票型": 0.90, "混合型-偏股": 0.70, "混合型-灵活": 0.50, "混合型-平衡": 0.50}


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
def prep(pool: pd.DataFrame, pairs: pd.DataFrame, navs: dict) -> tuple:
    """S4 + 分位人群 + pair 富化。返回 (pool_aug, pf, p90)。

    p90 = {类型: 该类型 S4 人群 ret5y 的 90 分位}（分位人群=同类型全部净值≥5年基金）。
    """
    pool = pool.copy()
    pool["ny"] = pool["code"].map(lambda c: navs.get(c, (None, None))[0])
    pool["r5"] = pool["code"].map(lambda c: navs.get(c, (None, None))[1])
    pool["s4"] = pool["ny"].notna() & (pool["ny"] >= NAV_MIN_Y)
    p90 = {}
    for t, g in pool[pool["s4"]].groupby("type_text"):
        s = g["r5"].dropna()
        p90[t] = float(s.quantile(TOP_PCT)) if len(s) >= 10 else None
    # 池内分位（展示用）
    pool["pctl"] = None
    for t, g in pool[pool["s4"]].groupby("type_text"):
        s = g["r5"].dropna()
        if len(s) >= 10:
            pool.loc[s.index, "pctl"] = (s.rank(pct=True) * 100).round(1)

    pf = pairs.merge(pool[["code", "name", "type_text", "ny", "r5", "s4"]],
                     on="code", how="inner")
    pf["s4"] = pf["s4"].fillna(False)
    pf["channel"] = pf["career_y"].map(channel)
    return pool, pf, p90


def f10_targets(pf: pd.DataFrame, p90: dict) -> list[str]:
    """需抓 F10 的基金：通道A∩S4 ∪（通道B∩分位达标∩S4）。

    B 通道先按分位预筛（分位不达标者无需抓 f10——任本基金年限救不了分位）。
    """
    b = pf[pf["channel"] == "B"].copy()
    if not b.empty:
        b["p90"] = b["type_text"].map(p90)
        b_ok = b[b["r5"].notna() & b["p90"].notna() & (b["r5"] >= b["p90"])]
    else:
        b_ok = b
    a = pf[(pf["channel"] == "A") & pf["s4"]]
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
def run_gate(pool: pd.DataFrame, pf: pd.DataFrame, p90: dict,
             f10s: dict, bench: dict) -> tuple:
    """S3 + B 分位复核 → (漏斗, 经理榜 DataFrame, 通过明细 DataFrame)。

    pair 通过 ⟺ 通道≠out/unknown ∧ S4 ∧ f10 里仍是现任 ∧ 任本基金≥S3
                ∧（通道A ∨（通道B ∧ r5 ≥ 同类 p90））
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
         "分位阈值p90_按类型": {t: (round(v, 4) if v is not None else None)
                                 for t, v in p90.items()},
         "f10_候选基金": 0, "f10_缺失或失败": 0,
         "S3_非现任或任期不足": 0, "B通道_分位不足": 0,
         "通过_pair": 0, "通过_基金": 0, "通过_经理": 0}

    passed = []
    seen_codes: set[str] = set()
    pool_ix = pool.set_index("code")
    for p in pf.itertuples(index=False):
        if p.channel not in ("A", "B") or not p.s4:
            continue
        # B 通道先按分位判定（f10_targets 只抓分位达标者，此处兜底归类，避免误记 f10 缺失）
        if p.channel == "B":
            p90v = p90.get(p.type_text)
            if p90v is None or p.r5 is None or pd.isna(p.r5) or p.r5 < p90v:
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
                       "mgr": p.mgr, "company": p.company, "career_y": p.career_y,
                       "channel": p.channel, "tenure_d": ten, "nav_years": p.ny,
                       "ret5y": p.r5, "excess_5y": excess,
                       "pctl": pool_ix.at[p.code, "pctl"]})
        f["通过_pair"] += 1

    funds = pd.DataFrame(passed)
    if funds.empty:
        funds = pd.DataFrame(columns=["code", "name", "type_text", "mgr", "company",
                                      "career_y", "channel", "tenure_d", "nav_years",
                                      "ret5y", "excess_5y", "pctl"])
    f["通过_基金"] = int(funds["code"].nunique())
    f["通过_经理"] = int(funds[["mgr", "company"]].drop_duplicates().shape[0]) \
        if not funds.empty else 0

    # 经理级聚合
    if funds.empty:
        mgrs = pd.DataFrame(columns=["mgr", "company", "career_y", "channel",
                                     "n_funds", "codes", "names", "max_tenure_d"])
    else:
        g = funds.groupby(["mgr", "company"], as_index=False)
        mgrs = g.agg(career_y=("career_y", "max"),
                     channel=("channel", lambda s: "A" if (s == "A").any() else "B"),
                     n_funds=("code", "nunique"),
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

    pool, pf, p90 = prep(pool, pairs, navres["stats"])
    print(f"      S4 净值≥5年 {int(pool['s4'].sum())} 只；"
          f"p90 阈值 " + " ".join(f"{t}={v:.3f}" if v else f"{t}=NA"
                                    for t, v in p90.items()), flush=True)

    targets = f10_targets(pf, p90)
    print(f"[4/5] F10 抓取（候选 {len(targets)} 只，缓存 7 天）…", flush=True)
    f10s = fetch_f10_all(targets, workers=args.workers)
    print(f"      成功 {len(f10s)} 失败 {len(targets) - len(f10s)}", flush=True)

    print("[5/5] 闸判定…", flush=True)
    bench = composite_bench_ann()
    funnel, mgrs, funds = run_gate(pool, pf, p90, f10s, bench)

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
