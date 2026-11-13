# -*- coding: utf-8 -*-
"""偏股两榜 top 归因体检（用户2026-11-13反馈："标注医药但持仓30%、业绩不亮眼却第一"）。

对当期老将/新锐 top10 的代表作逐只重算：
  · 分类来源判定：名字含风格词（name桶） vs 相关性桶（corr）
  · 相关性余量：6 代理周频超额相关 top1/top2 差 + 医药排第几（0.35 阈值下多弱）
  · 绝对业绩：5 年年化 vs 全池/同桶中位（用户"业绩不亮眼"核验）
  · 桶内规模：同桶入围基金数（分位分母）
输出紧凑表格。运行（仓库根）：python scripts/eq_top_audit.py（全离线，约1min）
"""
from __future__ import annotations

import re
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import numpy as np                                    # noqa: E402
import pandas as pd                                   # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from fundscorer import eqgate as G                    # noqa: E402
from fundscorer import eqscore as ES                  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
NAV_DIR = ROOT / "data" / "cache"


def name_bucket(name: str) -> str:
    """eqgate 名字桶（BUCKET_PATTERNS 同款回退）。"""
    return G.style_bucket(name)


def corr_profile(code: str) -> dict | None:
    """周频超额相关 vs6 代理（**近5年窗，与 eqgate._classify_raw 同口径**）：
    {bucket: corr} 降序；nav 不足 → None。"""
    p = NAV_DIR / f"nav_{code}.pkl"
    if not p.exists():
        return None
    try:
        nav = pd.read_pickle(p)
    except Exception:
        return None
    if "nav" not in nav.columns or nav.empty:
        return None
    end = nav["date"].max()
    sub = nav[nav["date"] > end - pd.DateOffset(years=5)].set_index("date")["nav"]
    mkt = G._load_close(G.STYLE_MARKET)
    if mkt is None:
        return None
    mkt_w = mkt.resample("W-FRI").last().pct_change().dropna()
    fr = sub.resample("W-FRI").last().pct_change().dropna()
    fex = (fr - mkt_w.reindex(fr.index)).dropna()
    if len(fex) < 100:
        return None
    out = {}
    for pname, bkt in G.STYLE_PROXIES.items():
        cl = G._load_close(pname)
        if cl is None:
            continue
        pex = (cl.resample("W-FRI").last().pct_change().dropna()
               - mkt_w).dropna()
        both = pd.concat([fex, pex], axis=1).dropna()
        if len(both) >= 100:
            out[bkt] = float(both.iloc[:, 0].corr(both.iloc[:, 1]))
    return out


def main() -> int:
    pool = ES._pool(False)
    pairs = G.career_pairs(ES._mgr_table(False), pool["code"].tolist())
    navres = ES._nav_stats(pool["code"].tolist(), refresh=False, workers=8,
                           log=lambda *a, **k: None)
    s4 = [c for c, v in navres["stats"].items() if v[0] >= G.NAV_MIN_Y]
    styles = G.classify_styles(s4) or {}
    fb = ES.build_features(pool, styles, log=lambda *a, **k: None)
    feat = fb["feat"]

    mgr = pd.read_csv(ROOT / "data" / "eq_score_mgrs.csv", dtype={"rep_code": str})
    funds = pd.read_csv(ROOT / "data" / "eq_score_funds.csv",
                        dtype={"code": str})
    # CSV code 可能丢前导零 → 用 zfill 对齐
    funds["code"] = funds["code"].str.zfill(6)
    name_by_code = feat["name"].to_dict()
    # 持仓校验前的原始相关桶（eq_style_cls 缓存）→ 识别"降级↓"
    _st = pd.read_pickle(G.STYLE_CACHE)
    raw_styles = _st.get("styles", _st) if isinstance(_st, dict) else {}

    for lst in ("vet", "new"):
        top = mgr[mgr.list_ == lst].head(10)
        print(f"\n===== {'老将' if lst == 'vet' else '新锐'}榜 top10 =====")
        hdr = (f"{'#':>2} {'经理':<6} {'桶':<4} {'分':>5} 代表作"
               f"{'':<18} {'5y年化':>7} {'桶中位':>7} {'池中位':>7} "
               f"{'corr桶':<4} {'余量':>5} {'医药r':>5} {'名字桶':<4}")
        print(hdr)
        for _, r in top.iterrows():
            code = str(r["rep_code"]).zfill(6)
            nm = name_by_code.get(code, "")
            prof = corr_profile(code)
            corr_bucket, margin, med_r = "—", "—", "—"
            if prof:
                ordered = sorted(prof.items(), key=lambda x: -x[1])
                corr_bucket = ordered[0][0]
                margin = f"{ordered[0][1] - ordered[1][1]:.2f}"
                rk = [b for b, _ in ordered].index("医药") + 1 if "医药" in prof else "-"
                med_r = str(rk)
            nb = name_bucket(nm)
            r5 = feat.at[code, "ret5y"] if code in feat.index else None
            disp = feat["bucket"].map(ES.display_bucket)
            codes_in_cell = feat[disp == r["bucket"]].index
            cm = float(np.nanmedian([feat.at[c, "ret5y"]
                                     for c in codes_in_cell
                                     if c in feat.index]))
            pm = float(np.nanmedian([feat.at[c, "ret5y"]
                                     for c in feat.index]))
            raw = raw_styles.get(code)                  # 持仓校验前的原始相关桶
            down = "↓" if (raw and raw != "均衡"
                           and feat.at[code, "bucket"] == "均衡") else ""
            print(f"{r['rank']:>2} {r['name']:<6} {r['bucket'] + down:<5} {r['score']:>5} "
                  f"{nm[:16]:<18} "
                  f"{(f'{r5:.1%}' if r5 is not None and r5 == r5 else '—'):>7} "
                  f"{cm:>7.1%} {pm:>7.1%} "
                  f"{corr_bucket:<4} {margin:>5} {med_r:>5} {nb:<4}")

    # ---- 苏秉毅深潜：全部入围基金 corr 全貌 ----
    print("\n===== 苏秉毅 深潜（全部入围基金）=====")
    su = funds[funds.mgr == "苏秉毅"]
    for _, r in su.iterrows():
        code = r["code"]
        nm = name_by_code.get(code, "")
        prof = corr_profile(code)
        r5 = feat.at[code, "ret5y"] if code in feat.index else None
        print(f"  {code} {nm} | 桶={r['bucket']} ret5y="
              f"{f'{r5:.1%}' if r5 is not None and r5 == r5 else '—'} "
              f"F3={r['F3']:.3f} F1={r['F1']:.1f}")
        if prof:
            for b, v in sorted(prof.items(), key=lambda x: -x[1]):
                print(f"      {b:<4} {v:+.3f}")

    # ---- 全量体检：105 人代表作绝对业绩分布 + 标签余量分布 ----
    print("\n===== 全量体检（两榜105人）=====")
    rows = []
    for _, r in mgr.iterrows():
        code = str(r["rep_code"]).zfill(6)
        r5 = feat.at[code, "ret5y"] if code in feat.index else None
        prof = corr_profile(code)
        margin = (max(prof.values()) - sorted(prof.values())[-2]
                  if prof and len(prof) >= 2 else None)
        corr_b = max(prof, key=prof.get) if prof else None
        rows.append(dict(name=r["name"], lst=r["list_"], bucket=r["bucket"],
                         score=r["score"], rep_code=code, ret5y=r5,
                         ann_career=r.get("ann_career"), dual=r.get("dual"),
                         corr_b=corr_b, margin=margin,
                         label_ok=(ES.display_bucket(corr_b) == r["bucket"])
                         if corr_b else None))
    a = pd.DataFrame(rows)
    print("代表作5y年化分档：")
    print("  <0% :", int((a.ret5y < 0).sum()), "| 0~5%:",
          int(((a.ret5y >= 0) & (a.ret5y < .05)).sum()),
          "| 5~10%:", int(((a.ret5y >= .05) & (a.ret5y < .10)).sum()),
          "| ≥10%:", int((a.ret5y >= .10).sum()))
    print("标签 vs 5y-corr桶（三类口径）：", a.label_ok.value_counts().to_dict(),
          "| corr余量<0.10 的人数:", int((a.margin < .10).sum()))
    print("双十在榜:", int(a.dual.fillna(False).sum()),
          "| 从业年化中位:", f"{a.ann_career.median():.1%}"
          if a.ann_career.notna().any() else "—")
    neg = a[a.ret5y < 0]
    print(f"负收益计分基金仍在榜: {len(neg)}（应全部为双十豁免）",
          "| 其中非双十(应为0):", int((~neg.dual.fillna(False)).sum()))
    if len(neg):
        print(neg[["name", "lst", "bucket", "ret5y", "dual"]].round(
            {"ret5y": 3}).to_string(index=False))
    # 降级统计（持仓校验）
    st = pd.read_pickle(G.STYLE_CACHE)
    raw = st.get("styles", st) if isinstance(st, dict) else {}
    fb = feat["bucket"]
    n_down = sum(1 for c in feat.index
                 if raw.get(c) and raw.get(c) != "均衡" and fb[c] == "均衡")
    print(f"持仓校验降级：S4 共 {n_down} 只由相关桶降为均衡")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
