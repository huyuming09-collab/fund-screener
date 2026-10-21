# -*- coding: utf-8 -*-
"""债券指数基金：独立评估口径与穿插排名。

用户 2026-10-19 决议——指数基金与主动债基评价维度不同：
  主动债基 → 看基金经理业绩（S2/S3 闸 + 分位评分卡）；
  指数基金 → 不看经理，只看跟踪标的/业绩/费率/公司规模（S2/S3 已在 screen 层跳过）。

排名规则（业绩口径 = 近三年，用户 2026-10-19：债券指数只看近三年业绩）：
  1. 排名依据 = ret_3y；落在主动榜第 k 与 k+1 名业绩之间 → 排第 k+1；
  2. 分数 = 相邻两只主动基金分数按业绩线性插值
     （"业绩在谁和谁之间，打分就在这两个分数之间"）；
  3. 同一跟踪指数最多 2 只入榜，优先费率低 + 公司非货规模大；
  4. 业绩高于全部主动榜 → 排第 1；低于全部 → 排末位（主动榜已满则落榜）。
     平局一律主动基金优先（指数分数外推仅偏移 1e-6，显示上不可见）。

v5.11 H1′ 条件锚点（2026-10-21 用户拍板 D-I3/D-I5，docs/17 §4.3；LOO 证据 docs/17 §3）：
   A 落在主动 TOP-N 业绩区间内 → 上述双锚线性（不变，实证区间内精度最优）；
   B 越下界 → 以**全体已评分主动候选**为锚双锚线性（消灭"榜内最低分−ε"平钳；
     需 interleave 传入 all_anchors；未传或 interp_scheme="legacy" 时回退旧平钳）；
   C 越上界 → 榜内最高分 + ε（决议④原样，与方案开关无关）。
   score_basis 增 branch 字段（A/B/C）供审计。
"""
from __future__ import annotations

import re

import pandas as pd

from . import config as C

_EPS = 1e-6          # 边界平局的显式让位量（分数显示 1~2 位小数，不可见）


def index_id(benchmark: str, name: str) -> str:
    """跟踪指数标识（"同指数最多 2 只"的分组键）。

    从 F10 业绩比较基准截取指数名：去掉现金拖累尾巴（+银行利率）、
    权重尾巴（×95%）、"收益率/全价"后缀。基准缺失时退回基金名
    （仅能区分不同基金，同指数不同公司的去重会失效——基准缺失属数据异常）。
    """
    bm = (benchmark or "").strip()
    if bm:
        bm = bm.split("+")[0].split("＋")[0]
        bm = re.sub(r"[×x*]\s*\d+(?:\.\d+)?\s*%\s*$", "", bm).strip()
        bm = re.sub(r"收益率$", "", bm).strip()
        bm = re.sub(r"[（(]全价[)）]\s*$", "", bm).strip()
        if len(bm) >= 4:
            return bm
    return re.sub(r"[A-Z]\d*\s*$", "", (name or "").strip()).strip()


def index_family(benchmark: str, name: str) -> tuple[str, str]:
    """(信用属性族, 久期分段) —— 指数编制方案的文本代理（docs/17 §1.1，v5.11 D-I1）。

    从业绩比较基准（优先）/基金名解析发行人与品种：族用于筛选与评分的**同族相对基准**
    （跨族收益差 2.7×、波动差 7.5×，跨族分位无意义——docs/17 §2.2）；久期段供展示。
    解析失败回退 "其他/未识别" 与 "未标期限"（回退族按小族走分支池基准）。
    """
    s = (benchmark or "") + "|" + (name or "")
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


def _interp(ret: float, pts: list[tuple[float, float]]) -> float | None:
    """业绩 ret 在主动榜 (业绩, 分数) 序列上的线性插值分数。

    pts 按业绩升序。高于全部 → 榜首分数+ε；低于全部 → 榜尾分数−ε（主动优先）。
    """
    if not pts:
        return None
    if ret > pts[-1][0]:
        # 高于全部主动业绩：无可插值的上邻 → 锚定**榜内最高分**（业绩最优 → 列首）。
        # 不能锚"业绩最高那只的分数"——主动榜按分数排序、业绩与分数非单调，
        # 锚错会把业绩全场最高的指数基金排到中游（实测错到第 4 名）。
        return max(p[1] for p in pts) + _EPS
    if ret < pts[0][0]:
        # 低于全部主动业绩 → 锚定榜内最低分（同理，排在榜末之后）
        return min(p[1] for p in pts) - _EPS
    for i in range(1, len(pts)):
        p0, p1 = pts[i - 1], pts[i]
        r0, s0 = p0[0], p0[1]
        r1, s1 = p1[0], p1[1]
        if ret <= r1:
            # 业绩与某只主动基金**完全相同** → 主动优先：指数退到该只之后（分数 −ε），
            # 使主动基金保持原名次；否则同分会由 code 字母序裁决，指数可能反超主动。
            if ret == r0:
                return s0 - _EPS
            if ret == r1:
                return s1 - _EPS
            if r1 <= r0:                        # 数据退化（重复业绩）兜底
                return s1 - _EPS
            w = (ret - r0) / (r1 - r0)
            return s0 + w * (s1 - s0)
    return pts[-1][1]


def _basis(ret: float, pts: list, metric: str, score: float | None,
           branch: str | None = None) -> dict:
    """插值依据（卡片展示，让用户可核对"业绩在谁和谁之间、分数取这两个分数之间"）。

    pts 升序，元素 (ret, score, rank, name)；lo = 业绩 ≤ret 的最近一只，
    hi = 业绩 >ret 的最近一只；任一为 None 表示已越过该侧全部锚点。
    branch：v5.11 H1′ 区间标记（A 区间内 / B 越下界·全候选锚 / C 越上界），审计用。
    """
    lo = next((p for p in reversed(pts) if p[0] <= ret), None)
    hi = next((p for p in pts if p[0] > ret), None)

    def anchor(p):
        return None if p is None else {"ret": p[0], "score": p[1],
                                       "rank": p[2], "name": p[3]}

    out = {"metric": metric, "ret": ret, "score": score,
           "lo": anchor(lo), "hi": anchor(hi)}
    if branch is not None:
        out["branch"] = branch
    return out


def interleave(idx_rows: pd.DataFrame, active_res: pd.DataFrame,
               meta: dict, top_n: int | None = None,
               *, all_anchors: pd.DataFrame | None = None) -> tuple[pd.DataFrame, dict]:
    """指数候选穿插进主动榜，返回 (合并榜单, 统计)。

    idx_rows    指数候选排名表（rank_candidates 产物，分数待覆盖）
    active_res  主动 TOP-N（rank_candidates 产物，已按分数排序截断）
    meta        {code: {fee_total, benchmark, company_scale}} 指数候选的份额级元数据
    top_n       合并后榜单上限
    all_anchors 该池**全部已评分主动候选**排名表（rank_candidates top_n=None 产物）——
                v5.11 H1′ 分支 B 越下界时的全候选锚点来源；None 或
                INDEX.interp_scheme="legacy" → 回退旧"榜内最低分−ε"平钳
    """
    stats = {"candidates": 0, "capped": 0, "in_list": 0, "ids": 0,
             "branches": {"A": 0, "B": 0, "C": 0}}
    if idx_rows is None or len(idx_rows) == 0:
        return active_res, stats
    idx = idx_rows.copy()
    idx["is_index"] = True
    stats["candidates"] = len(idx)

    # ---- 3. 同一跟踪指数最多 N 只：按费率升序、公司非货规模降序取前 N ----
    meta = meta or {}
    cap = int(C.INDEX["per_index_max"])
    idx["_idxid"] = [index_id((meta.get(c) or {}).get("benchmark", ""), n)
                     for c, n in zip(idx["code"], idx["name"])]
    idx["_fee"] = [(meta.get(c) or {}).get("fee_total") for c in idx["code"]]
    idx["_cs"] = [(meta.get(c) or {}).get("company_scale") for c in idx["code"]]
    stats["ids"] = int(idx["_idxid"].nunique())
    groups = []
    for _gid, g in idx.groupby("_idxid", sort=False):
        g = g.sort_values(["_fee", "_cs"], ascending=[True, False],
                          na_position="last", kind="mergesort")
        stats["capped"] += max(0, len(g) - cap)
        groups.append(g.head(cap))
    idx = pd.concat(groups, ignore_index=True) if groups else idx.iloc[0:0]

    # ---- 1~2. 按业绩对齐主动榜，分数插值（两侧统一用 INDEX.return_metric=近三年，
    #           债券指数多为 2023 年后成立、5 年窗口会挡掉绝大多数；主动榜用同一列建区间） ----
    # pts 带 (rank, name)：除插值外还产出 score_basis（卡片展示"业绩落在谁和谁之间"，可审计）
    m = C.INDEX["return_metric"]
    pts = []
    for _, r in active_res.iterrows():
        if not pd.notna(r.get(m)) or not pd.notna(r.get("score")):
            continue
        rk = r.get("rank")
        pts.append((float(r[m]), float(r["score"]),
                    int(rk) if pd.notna(rk) else None, str(r.get("name") or "")))
    pts.sort(key=lambda x: x[0])

    # ---- v5.11 H1′ 分支 B 锚点集：全体已评分主动候选（conditional 且显式传入才生效） ----
    pts_all = None
    if (C.INDEX.get("interp_scheme", "conditional") == "conditional"
            and all_anchors is not None and len(all_anchors)):
        pa = []
        for _, r in all_anchors.iterrows():
            if not pd.notna(r.get(m)) or not pd.notna(r.get("score")):
                continue
            rk = r.get("rank")
            pa.append((float(r[m]), float(r["score"]),
                       int(rk) if pd.notna(rk) else None, str(r.get("name") or "")))
        pa.sort(key=lambda x: x[0])
        pts_all = pa or None

    scores, bases = [], []
    branches = {"A": 0, "B": 0, "C": 0}
    for _, r in idx.iterrows():
        if not pd.notna(r.get(m)):
            scores.append(None)
            bases.append(None)
            continue
        ret = float(r[m])
        use, branch = pts, "A"
        if pts and ret < pts[0][0]:
            branch = "B"                    # 越下界（决议④下沿）
            if pts_all is not None:
                use = pts_all               # 全候选锚：真实定位，仍低于全部才退化 min−ε
        elif pts and ret > pts[-1][0]:
            branch = "C"                    # 越上界：恒榜内最高分+ε（决议④，与方案开关无关）
        s = _interp(ret, use)
        scores.append(s)
        bases.append(_basis(ret, use, m, s, branch=branch) if s is not None else None)
        if s is not None:
            branches[branch] += 1
    idx["score"] = scores
    idx["score_basis"] = bases
    idx = idx[idx["score"].notna()].copy()
    stats["in_list"] = len(idx)
    stats["branches"] = branches

    # ---- 合并重排：分数降序（平局按 code 保序），截断 top_n，重编 rank ----
    idx = idx.drop(columns=["_idxid", "_fee", "_cs"], errors="ignore")
    if "is_index" not in active_res.columns:
        active_res = active_res.copy()
        active_res["is_index"] = False
    merged = pd.concat([active_res, idx], ignore_index=True, sort=False)
    merged = merged.sort_values(["score", "code"], ascending=[False, True],
                                kind="mergesort").reset_index(drop=True)
    if top_n:
        merged = merged.head(int(top_n))
    merged["rank"] = range(1, len(merged) + 1)
    stats["in_list"] = int(merged["is_index"].sum())
    return merged, stats
