"""债指全量数据画像（docs/17 §1 扩展，研究件，不入主链）

对象 = 两债池全部指数基金（基金级），不只筛后候选：
  A. 指数族分类（编制方案代理）：基准文本 → 信用属性族 + 期限段（政金债/国开/存单/信用…）
  B. 年龄结构（指数成立时间的基金侧投影）：<3 / 3-4 / 4-5 / ≥5 年
  C. 族内风险收益画像（跨族不可比分位的证据）
  D. 同指数组跟踪离散度（无指数点位序列时的跟踪质量代理）
  E. 现行闸门逐级存活（按族×按龄）+ 5 年族缺失对 P1 的杀伤
  F. 改筛模拟（证据，非实施）：SimA = P1 换 3 年族四项 + P2 ≥3 完整年；SimB = SimA + S4≥2 年
产出：打印表 + data/index_full.csv
用法：.venv/bin/python scripts/index_full_probe.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fundscorer import batch as B                     # noqa: E402
from fundscorer import config as C                    # noqa: E402
from fundscorer import indexfund as IF                # noqa: E402
from fundscorer import pipeline as P                  # noqa: E402
from fundscorer import screen as S                    # noqa: E402


def classify(bm: str, name: str) -> tuple[str, str]:
    """(信用属性族, 期限段) —— 编制方案代理，从基准/名称文本解析。"""
    s = (bm or "") + "|" + (name or "")
    if "存单" in s:
        fam = "同业存单"
    elif "二级资本" in s:
        fam = "二级资本债"
    elif any(k in s for k in ("公司债", "科创", "短融", "信用债", "中票", "永续")):
        fam = "信用债"
    elif "国债" in s and "国开" not in s:
        fam = "国债"
    elif "地方" in s:
        fam = "地方债"
    elif "国开" in s:
        fam = "国开债"
    elif "农发" in s:
        fam = "农发债"
    elif "进出口" in s:
        fam = "进出口行债"
    elif any(k in s for k in ("政策性金融", "政金")):
        fam = "政金债(泛)"
    elif any(k in s for k in ("全债", "总财富", "综合债")):
        fam = "综合债"
    else:
        fam = "其他/未识别"
    m = re.search(r"(\d+)\s*[-–~至]\s*(\d+)\s*年", s)
    if m:
        dur = f"{m.group(1)}-{m.group(2)}年"
    else:
        m2 = re.search(r"(\d+)\s*年\s*恒定久期", s)
        dur = f"{m2.group(1)}年恒定久期" if m2 else "未标期限"
    return fam, dur


def main() -> None:
    frames, groups_all = [], []
    for pool in ("short_bond", "long_bond"):
        cfg = C.SCREENS[pool]
        recs, spikes, yearly, _ = S.load_screen_inputs(asof=None, cfg=cfg)
        idx_recs = [r for r in recs if P._is_index(r)]
        if not idx_recs:
            continue
        pool_df = S.merge_shares(idx_recs)
        rec0 = {}
        for r in idx_recs:
            rec0.setdefault(r["code"], r)
        cs = B._load_company_scale()

        # ---- A. 分类 + 元数据 ----
        fams, durs, ids, fees, css, bms = [], [], [], [], [], []
        for _, row in pool_df.iterrows():
            r = rec0.get(row["rep_code"], {})
            bm = (r.get("benchmark") or "").strip()
            f, d = classify(bm, row["rep_name"])
            fams.append(f)
            durs.append(d)
            ids.append(IF.index_id(bm, row["rep_name"]))
            fees.append(r.get("fee_total"))
            v = None
            if cs is not None and row["rep_code"] in cs.index:
                v = float(cs.loc[row["rep_code"]])
            css.append(v)
            bms.append(bm)
        pool_df = pool_df.assign(pool=pool, fam=fams, dur=durs, idxid=ids,
                                 fee_total=fees, company_scale=css, benchmark=bms)
        pool_df["max_gain_5y"] = pool_df["codes"].map(lambda cs_: S._fund_max_gain(cs_, spikes))

        # ---- E. 现行闸门逐级存活（与 screen_candidates 指数分支同逻辑）----
        keep = pool_df["scale_sum"] >= cfg["scale_min_yi"]
        pool_df["k_s1"] = keep
        k4 = pool_df["nav_years"] >= (C.INDEX["min_nav_years"])
        pool_df["k_s4"] = k4
        k5 = pool_df["max_gain_5y"] <= cfg["spike_max_gain"]
        pool_df["k_s5"] = k5
        Pm = S.composite_percentiles(pool_df)          # P1 基准 = 本分支全池（D13-a 口径）
        k_p1 = Pm["mpct"] >= cfg["comp_pct_min"]
        pool_df["k_p1"] = k_p1
        yb = {b: yearly[b] for b in pool_df["base"] if b in yearly}
        Y = pd.DataFrame(yb).T
        med = pd.Series({i: S.yearly_median_pct(Y, b, yb, cfg["yearly_pool_min"],
                                                cfg["yearly_min_years"])
                         for i, b in zip(pool_df.index, pool_df["base"])}, dtype="float64")
        k_p2 = med >= cfg["yearly_median_min"]
        pool_df["k_p2"] = k_p2
        pool_df["pass_cur"] = pool_df[["k_s1", "k_s4", "k_s5", "k_p1", "k_p2"]].all(axis=1)

        # ---- F. 改筛模拟 ----
        # SimA：P1' = 3 年族四项（ret_3y↑, mdd_3y↓, ret_1y↑, vol_1y↓）；P2' ≥3 完整年
        P3 = pd.DataFrame({
            "ret_3y": pool_df["ret_3y"].rank(pct=True),
            "mdd_3y": pool_df["mdd_3y"].rank(pct=True, ascending=False),
            "ret_1y": pool_df["ret_1y"].rank(pct=True),
            "vol_1y": pool_df["vol_1y"].rank(pct=True, ascending=False),
        })
        P3["mpct3"] = P3.mean(axis=1).rank(pct=True)
        k_p1b = P3["mpct3"] >= cfg["comp_pct_min"]
        med3 = pd.Series({i: S.yearly_median_pct(Y, b, yb, cfg["yearly_pool_min"], 3)
                          for i, b in zip(pool_df.index, pool_df["base"])}, dtype="float64")
        k_p2b = med3 >= cfg["yearly_median_min"]
        simA = pool_df["k_s1"] & pool_df["k_s4"] & pool_df["k_s5"] & k_p1b & k_p2b
        # SimB：SimA + S4≥2 年 + P2 ≥2 完整年
        med2 = pd.Series({i: S.yearly_median_pct(Y, b, yb, cfg["yearly_pool_min"], 2)
                          for i, b in zip(pool_df.index, pool_df["base"])}, dtype="float64")
        simB = (pool_df["k_s1"] & (pool_df["nav_years"] >= 2) & pool_df["k_s5"]
                & k_p1b & (med2 >= cfg["yearly_median_min"]))
        # SimC：同族相对分位（族≥30 只用族内基准，否则回退分支池）+ 3 年族四项 P1 + P2 ≥3 完整年
        #   —— 跨族不可比分位的对症方案（存单/信用 vs 利率债风险收益差 2~7 倍）
        fam_n = pool_df["fam"].value_counts()

        def prank(col: str, ascending: bool) -> pd.Series:
            r_all = pool_df[col].rank(pct=True, ascending=ascending)
            r_fam = pool_df.groupby("fam")[col].rank(pct=True, ascending=ascending)
            return r_fam.where(pool_df["fam"].map(fam_n).ge(30), r_all)

        P1c = pd.DataFrame({
            "ret_3y": prank("ret_3y", True), "mdd_3y": prank("mdd_3y", False),
            "ret_1y": prank("ret_1y", True), "vol_1y": prank("vol_1y", False),
        })
        k_p1c = P1c.mean(axis=1) >= cfg["comp_pct_min"]
        k_p2c = []
        for b, f in zip(pool_df["base"], pool_df["fam"]):
            if fam_n.get(f, 0) >= 30:
                fam_bases = set(pool_df.loc[pool_df["fam"] == f, "base"])
                v = S.yearly_median_pct(Y.loc[Y.index.isin(fam_bases)], b, yb,
                                        cfg["yearly_pool_min"], 3)
            else:
                v = S.yearly_median_pct(Y, b, yb, cfg["yearly_pool_min"], 3)
            k_p2c.append(bool(v is not None and v >= cfg["yearly_median_min"]))
        k_p2c = pd.Series(k_p2c, index=pool_df.index)
        simC = pool_df["k_s1"] & pool_df["k_s4"] & pool_df["k_s5"] & k_p1c & k_p2c
        pool_df["pass_simA"], pool_df["pass_simB"], pool_df["pass_simC"] = simA, simB, simC
        pool_df["ret5y_missing"] = pool_df["ret_5y"].isna()
        frames.append(pool_df)
        print(f"== {pool}: 指数基金 {len(pool_df)} 只 | 现行全通 {int(pool_df['pass_cur'].sum())}"
              f"（期望同 screen 指数分支）| SimA {int(simA.sum())} | SimB {int(simB.sum())}"
              f" | SimC {int(simC.sum())}（新增 {int((simC & ~pool_df['pass_cur']).sum())}"
              f" / 移出 {int((pool_df['pass_cur'] & ~simC).sum())}）")

    df = pd.concat(frames, ignore_index=True)
    df.to_csv(ROOT / "data" / "index_full.csv", index=False, encoding="utf-8-sig")

    print("\n" + "=" * 72)
    print("=== A×B. 指数族 × 年龄（基金级，两池合并）===")
    df["age_bucket"] = pd.cut(df["nav_years"], [0, 3, 4, 5, 99],
                              labels=["<3年", "3-4年", "4-5年", "≥5年"])
    tab = pd.crosstab(df["fam"], df["age_bucket"], margins=True)
    print(tab.to_string())

    print("\n=== C. 族内风险收益画像（中位：ret_3y / vol_5y / mdd_3y）===")
    g = df.groupby("fam").agg(
        n=("base", "size"),
        ret3_med=("ret_3y", "median"),
        vol5_med=("vol_5y", "median"),
        mdd3_med=("mdd_3y", "median"),
        fee_med=("fee_total", "median"),
        age_med=("nav_years", "median"),
        cs_cov=("company_scale", lambda x: int(x.notna().sum())),
    ).sort_values("n", ascending=False)
    print(g.round(3).to_string())

    print("\n=== D. 同指数组跟踪离散度（组内 ret_3y 标准差，组≥2 只）===")
    disp = []
    for (pool, gid), gg in df.groupby(["pool", "idxid"]):
        vals = gg["ret_3y"].dropna()
        if len(gg) >= 2 and len(vals) >= 2:
            disp.append({"pool": pool, "idxid": gid, "n": len(gg),
                         "std": float(vals.std()), "rng": float(vals.max() - vals.min())})
    dd = pd.DataFrame(disp)
    if len(dd):
        print(f"可算组 {len(dd)} / 总组 {df.groupby(['pool','idxid']).ngroups}；"
              f"std 中位 {dd['std'].median():.4f}、p90 {dd['std'].quantile(.9):.4f}、"
              f"最大 {dd['std'].max():.4f}（年化收益口径）")
        print(dd.sort_values("std", ascending=False).head(8).to_string(index=False))

    print("\n=== E. 现行闸门逐级存活（按族）===")
    stages = ["k_s1", "k_s4", "k_s5", "k_p1", "k_p2", "pass_cur"]
    e = df.groupby("fam").agg({s: "sum" for s in stages}).astype(int)
    e.insert(0, "n", df.groupby("fam").size())
    e["5y缺失(过S4)"] = df[df["k_s4"]].groupby("fam")["ret5y_missing"].sum().astype(int)
    print(e.to_string())
    print("\n按龄现行通过率:")
    print(df.groupby("age_bucket", observed=True)["pass_cur"].agg(["size", "sum", "mean"])
          .round(2).to_string())
    print(f"\nP1 因 5 年族 NaN 静默不通过: 过 S4 后 {int(df[df['k_s4']]['ret5y_missing'].sum())} 只")
    print("\n=== F. 改筛模拟（证据，非实施）===")
    print(pd.crosstab(df["fam"], df["pass_simA"], margins=True).rename(
        columns={False: "SimA不过", True: "SimA过"}).to_string())
    print(pd.crosstab(df["fam"], df["pass_simB"], margins=True).rename(
        columns={False: "SimB不过", True: "SimB过"}).to_string())
    print("\nSimC（同族相对分位）按族：")
    print(pd.crosstab(df["fam"], df["pass_simC"], margins=True).rename(
        columns={False: "SimC不过", True: "SimC过"}).to_string())
    print("\nSimC × 现行（新增/移出）：")
    print(pd.crosstab(df["pass_cur"], df["pass_simC"], margins=True).to_string())
    print("\n已落盘 data/index_full.csv")


if __name__ == "__main__":
    main()
