# -*- coding: utf-8 -*-
"""榜单合理性诊断（2026-11-13 七问自查的数据底座；用户"榜单上的人不合理需要排除"）。

对两榜在册经理的**计分基金**逐只采集：
  · 风格归因：6风格代理绝对空间周频回归(5y) → R²、标签风格β、Σβ·代理5年收益
    → **beta尺度化风格超额**（对照 F3 的单代理β=1假设，量化"F3虚高"）
  · 回撤/波动：5年最大回撤、年化波动（M 模块同口径）
  · 仓位：季报股票仓位10季历史（东财移动API，缓存30天）→ 均值/最低/最新
    ——"低仓位躲熊"检测（偏股合同通常60-95%）
  · 行业集中：前10重仓单行业合计最大占比（hold_*.pkl 缓存）——"行业押注"检测
  · 共管稀释：f10 现任经理数 + 本经理是否任职最久（主执）——"挂名"检测
  · 规模（S5未实施的闸）/机构占比/量化标签（名称正则）/格样本量/缺因子数
输出 data/eq_diag.csv + 汇总表（各"不合理"人簇的人数与名单）。
运行（仓库根）：python scripts/eq_diag.py [--no-fetch]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

warnings.filterwarnings("ignore")
import numpy as np                                     # noqa: E402
import pandas as pd                                    # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from fundscorer import data as D                      # noqa: E402
from fundscorer import eqgate as G                    # noqa: E402
from fundscorer import eqscore as ES                  # noqa: E402
from fundscorer import metrics as M                   # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
NAV_DIR = ROOT / "data" / "cache"
OUT_CSV = ROOT / "data" / "eq_diag.csv"
QUANT_RE = re.compile(r"量化|指数增强|定增|对冲|多因子|套利|CTA")


def fetch_pos_hist(code: str, fetch: bool = True) -> list:
    """季报股票仓位历史 [{FSRQ, GP}]（东财移动API，默认10季≈2.5年；缓存30天）。"""
    p = NAV_DIR / f"pos_{code}.pkl"
    if p.exists():
        try:
            age_h = (time.time() - p.stat().st_mtime) / 3600
            if not fetch or age_h < 24 * 30:
                return pd.read_pickle(p)
        except Exception:
            pass
    if not fetch:
        return []
    try:
        u = ("https://fundmobapi.eastmoney.com/FundMNewApi/FundMNAssetAllocation"
             f"?FCODE={code}&deviceid=Wap&plat=Wap&product=EFund&version=6.2.8")
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
        raw = json.loads(urllib.request.urlopen(req, timeout=12)
                         .read().decode("utf-8", "ignore"))
        out = [{"d": r.get("FSRQ"), "gp": float(r["GP"])}
               for r in (raw.get("Datas") or [])
               if r.get("GP") not in (None, "", "--")]
        if out:
            try:
                pd.to_pickle(out, p)
            except Exception:
                pass
        return out
    except Exception:
        return []


def style_attrib(code: str) -> dict | None:
    """6风格代理绝对空间周频回归（近5年）：R²、各β、标签β、Σβ·代理5年年化。"""
    nav = ES._read_nav_pickle(code)
    if nav is None or nav.empty:
        return None
    end = nav["date"].max()
    sub = nav[nav["date"] > end - pd.DateOffset(years=5)].set_index("date")["nav"]
    rf = sub.resample("W-FRI").last().pct_change().dropna()
    if len(rf) < 100:
        return None
    cols, ann5 = {}, {}
    for pname in G.STYLE_PROXIES:
        cl = G._load_close(pname)
        if cl is None:
            return None
        rp = cl.resample("W-FRI").last().pct_change().dropna()
        cols[pname] = rp
        # 5年年化（代理绝对收益，与 F3 同空间；_load_close 返回日期索引 Series）
        w5 = cl[(cl.index > end - pd.DateOffset(years=5)) & (cl.index <= end)]
        ann5[pname] = (float((w5.iloc[-1] / w5.iloc[0])
                             ** (365.0 / (w5.index[-1] - w5.index[0]).days) - 1)
                       if len(w5) > 100 and (w5.index[-1] - w5.index[0]).days > 30
                       else None)
    X = pd.DataFrame(cols).reindex(rf.index)
    df = pd.concat([rf.rename("f"), X], axis=1).dropna()
    if len(df) < 100:
        return None
    y = df["f"].values
    A = df.drop(columns=["f"]).values
    A = np.column_stack([A, np.ones(len(A))])
    try:
        coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    except Exception:
        return None
    pred = A @ coef
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else None
    betas = {k: float(v) for k, v in zip(list(df.drop(columns=["f"]).columns),
                                         coef[:-1])}
    # beta 尺度化风格贡献（绝对空间，与 F3 单代理β=1 假设对照）
    drag = sum(betas[k] * ann5[k] for k in betas if ann5.get(k) is not None)
    return dict(r2=r2, betas=betas, drag=drag)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true", help="不联网（仓位/持仓只读缓存）")
    args = ap.parse_args()
    fetch = not args.no_fetch
    t0 = time.time()

    mgr = pd.read_csv(ROOT / "data" / "eq_score_mgrs.csv", dtype={"rep_code": str})
    mgr["rep_code"] = mgr["rep_code"].str.zfill(6)
    funds = pd.read_csv(ROOT / "data" / "eq_score_funds.csv", dtype={"code": str})
    funds["code"] = funds["code"].str.zfill(6)
    fb = pd.read_pickle(ES.FEAT_CACHE)
    feat = fb["feat"]
    fdf_nan = funds.set_index("code")[["F1", "F3", "F5", "F2", "F6", "score"]]
    raw_styles = (pd.read_pickle(G.STYLE_CACHE).get("styles") or {})

    # f10（规模/共管）——只读缓存；**口径与引擎一致**（cand_codes 全量，
    # 否则基础名合并漏掉"非任何人的计分基金"的兄弟份额 → 误报壳基金）
    rep_codes = sorted(set(mgr["rep_code"]))
    pool = ES._pool(False)
    name_of = dict(zip(pool["code"], pool["name"]))
    _pairs = G.career_pairs(ES._mgr_table(False), pool["code"].tolist())
    cand_codes = sorted({r.code for r in _pairs.itertuples(index=False)
                         if r.career_y is not None and r.career_y == r.career_y
                         and G.channel(r.career_y) in ("A", "B")
                         and r.code in feat.index})
    f10s = ES._f10s(cand_codes, refresh=False, workers=8, log=lambda *a, **k: None)
    PROXY_OF_BUCKET = {b: p for p, b in G.STYLE_PROXIES.items()}
    # 基础名合并规模（与 G1 闸/展示同源——单份额规模会误判 A/C 拆分的基金）
    base_scale: dict = {}
    for c, info in f10s.items():
        b = ES.base_name(str(name_of.get(c) or ""))
        if b:
            try:
                s = float((info or {}).get("scale_yi") or 0)
            except (TypeError, ValueError):
                s = 0.0
            base_scale[b] = base_scale.get(b, 0.0) + s
    # 前10行业（缺的补抓，refresh 由 --no-fetch 控制）
    for c in rep_codes:
        ES.fetch_hold_top10(c, refresh=fetch)
    # 机构占比（只读）
    inst = {c: v for c, v in
            {c: D._load_cache(f"inst_{c}", 1e9) for c in rep_codes}.items()
            if v is not None}
    # 代理5年年化（给 F3 对照）+ 指数基准
    mkt = G._load_close(G.STYLE_MARKET)
    mkt5 = None
    if mkt is not None:
        w = mkt[(mkt.index > pd.Timestamp.today().normalize()
                 - pd.DateOffset(years=5))]
        if len(w) > 100:
            mkt5 = float((w.iloc[-1] / w.iloc[0])
                         ** (365.0 / (w.index[-1] - w.index[0]).days) - 1)

    # 格样本量（类型×桶）
    cell_n = feat.groupby(["type", "bucket"]).size().to_dict()

    rows = []
    for i, r in enumerate(mgr.itertuples(index=False), 1):
        code = r.rep_code
        name = (feat.at[code, "name"] if code in feat.index else "")
        b_raw = (feat.at[code, "bucket"] if code in feat.index else None)
        typ = (feat.at[code, "type"] if code in feat.index else None)
        # 风格归因
        sa = style_attrib(code)
        # F3（CSV 原值）与 beta 尺度化超额
        f3 = None
        fr = funds[funds.code == code]
        if len(fr):
            v = fr.iloc[0].get("F3")
            f3 = float(v) if pd.notna(v) else None
        ret5y = feat.at[code, "ret5y"] if code in feat.index else None
        scaled_alpha = None
        if sa and sa.get("drag") is not None and ret5y == ret5y:
            scaled_alpha = float(ret5y) - sa["drag"]
        # 回撤/波动
        nav = ES._read_nav_pickle(code)
        mdd = M.max_drawdown(nav, 5) if nav is not None else None
        vol = M.ann_vol(nav, 5) if nav is not None else None
        # 仓位
        pos = fetch_pos_hist(code, fetch=fetch)
        gps = [x["gp"] for x in pos if x.get("gp") is not None]
        # 行业集中
        hold = ES.fetch_hold_top10(code, refresh=False) or []
        ind_tot: dict = {}
        for ind, wgt in hold:
            ind_tot[ind] = ind_tot.get(ind, 0) + wgt
        top_ind, top_ind_w = (max(ind_tot.items(), key=lambda x: x[1])
                              if ind_tot else (None, None))
        # 共管
        info = f10s.get(code) or {}
        mgrs = info.get("managers") or []
        n_mgrs = len(mgrs)
        tenures = []
        for m in mgrs:
            try:
                tenures.append((pd.Timestamp(str(m.get("start") or "")[:10]), m))
            except Exception:
                pass
        is_primary = False
        if tenures:
            earliest = min(t for t, _ in tenures)
            mine = [t for t, m in tenures if m.get("name") == r.name]
            is_primary = bool(mine) and min(mine) == earliest
        # 缺因子
        nan_n = int(fr.iloc[0][["F1", "F3", "F5", "F2", "F6"]].isna().sum()) if len(fr) else 5
        rows.append(dict(
            list=r.list_, rank=r.rank, name=r.name, company=r.company,
            code=code, fund=name, bucket=r.bucket, bucket_raw=b_raw,
            cell_n=cell_n.get((typ, b_raw)) if typ and b_raw else None,
            score=r.score, ret5y=r.ret5y, ann_career=r.ann_career,
            dual=bool(r.dual), entry=r.entry, n_funds=r.n_funds,
            f3=f3, scaled_alpha=scaled_alpha,
            style_r2=(sa or {}).get("r2"),
            label_beta=((sa or {}).get("betas") or {}).get(
                PROXY_OF_BUCKET.get(b_raw, "@@")) if sa else None,
            drag=(sa or {}).get("drag"),
            mdd5y=mdd, vol5y=vol,
            pos_avg=(float(np.mean(gps)) if gps else None),
            pos_min=(float(np.min(gps)) if gps else None),
            pos_n=len(gps),
            top_ind=top_ind, top_ind_w=top_ind_w,
            n_mgrs=n_mgrs, is_primary=is_primary,
            scale=(base_scale.get(ES.base_name(str(name))) or info.get("scale_yi")),
            inst=inst.get(code),
            quant=bool(QUANT_RE.search(name or "")),
            nan_n=nan_n,
            type=typ,
            g3s=(bool(fr.iloc[0]["g3s"]) if len(fr) else None),
            g4s=(bool(fr.iloc[0]["g4s"]) if len(fr) else None),
        ))
        if i % 30 == 0:
            print(f"  {i}/{len(mgr)} {time.strftime('%H:%M:%S')}")
    df = pd.DataFrame(rows)
    df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    # ---- 汇总：各"不合理"人簇 ----
    def show(title, sub):
        print(f"\n【{title}】{len(sub)} 人")
        if len(sub):
            print(sub[["list", "rank", "name", "fund", "ret5y", "score"]]
                  .head(30).to_string(index=False))

    print(f"\n===== 诊断完成 {len(df)} 人 → {OUT_CSV}（{(time.time()-t0)/60:.1f}min）=====")
    print("\n--- 关键分布 ---")
    for col, note in [("ret5y", "计分基金5年年化"), ("score", "评分"),
                      ("style_r2", "风格R²"), ("mdd5y", "5年回撤"),
                      ("pos_avg", "平均仓位"), ("top_ind_w", "前10单行业占比"),
                      ("n_mgrs", "共管经理数"), ("scale", "规模(亿)")]:
        s = pd.to_numeric(df[col], errors="coerce")
        print(f"  {note:<14} p25={s.quantile(.25):.3f} 中位={s.median():.3f} "
              f"p75={s.quantile(.75):.3f} 缺失={int(s.isna().sum())}")
    print("  F3 vs 尺度化超额 差(F3-α) 中位:",
          f"{(pd.to_numeric(df['f3'],errors='coerce')-pd.to_numeric(df['scaled_alpha'],errors='coerce')).median():.3f}")
    print("  缺因子>0 的人:", int((df.nan_n > 0).sum()))
    print("  量化标签:", int(df.quant.sum()), "| 非主执且共管≥3:",
          int(((df.n_mgrs >= 3) & (~df.is_primary)).sum()))

    show("R1 深亏（计分基金5年年化<-5%）", df[pd.to_numeric(df.ret5y, errors="coerce") < -0.05])
    show("R2 平庸低分（0~5% 且 评分<70）",
         df[(pd.to_numeric(df.ret5y, errors="coerce") >= 0)
            & (pd.to_numeric(df.ret5y, errors="coerce") < .05)
            & (pd.to_numeric(df.score, errors="coerce") < 70)])
    show("R3 壳基金（基础名合并规模<2亿）",
         df[pd.to_numeric(df.scale, errors="coerce") < 2])
    pos_need = df["type"].map(ES.POS_MIN_BY_TYPE).fillna(30.0)
    r4 = pd.to_numeric(df.pos_avg, errors="coerce") < pos_need
    show("R4 低仓位（类型感知：股票/偏股<50%，灵活/平衡<30%）", df[r4])
    show("R5 行业押注（前10单行业≥40%）",
         df[pd.to_numeric(df.top_ind_w, errors="coerce") >= .40])
    show("R6 挂名嫌疑（共管≥3且非主执）",
         df[(df.n_mgrs >= 3) & (~df.is_primary)])
    show("R7 量化标签", df[df.quant])
    show("R8 格样本<150（小格）", df[pd.to_numeric(df.cell_n, errors="coerce") < 150])
    show("R9 风格失真（标签β<0.5）",
         df[pd.to_numeric(df.label_beta, errors="coerce").abs() < .5])

    # 多重命中
    flags = pd.DataFrame({
        "R1深亏": pd.to_numeric(df.ret5y, errors="coerce") < -0.05,
        "R2平庸": ((pd.to_numeric(df.ret5y, errors="coerce") >= 0)
                   & (pd.to_numeric(df.ret5y, errors="coerce") < .05)
                   & (pd.to_numeric(df.score, errors="coerce") < 70)),
        "R3壳": pd.to_numeric(df.scale, errors="coerce") < 2,
        "R4低仓位": r4,
        "R5行业": pd.to_numeric(df.top_ind_w, errors="coerce") >= .40,
        "R6挂名": (df.n_mgrs >= 3) & (~df.is_primary),
        "R7量化": df.quant,
        "R9失真": pd.to_numeric(df.label_beta, errors="coerce").abs() < .5,
    }, index=df.index)
    df["n_flags"] = flags.sum(axis=1)
    df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print("\n--- 命中数分布 ---")
    print(df.n_flags.value_counts().sort_index().to_string())
    print("\n--- 命中≥2的经理 ---")
    multi = df[df.n_flags >= 2].sort_values("n_flags", ascending=False)
    print(multi[["list", "rank", "name", "fund", "ret5y", "score", "n_flags"]]
          .to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
