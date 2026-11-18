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
  · **持仓校验降级（2026-11-13 用户Q2"医药第一怎么办"）**：相关性桶 X 的基金，
    前10重仓中 X 系行业合计 <HOLD_MIN(15%) → 同格降为均衡——用持仓事实纠正
    "标医药但持仓5%"的错格不公（前10足以识别行业基金：行业基金前10同行业占绝对多数；
    全量行业接口不通，前10是现有最好持仓信号；只降级不升级——错格伤害只在降级方向）
  · **双十直通（2026-11-13 用户定）**：双十 = 从业>10年 且 从业年化>10%
    → 免考卷免绝对门槛全部上榜（从业年化=任职最久现任基金的几何年化，
    复权净值自上任日——与各平台该基金"任职年化"同公式同起始日）
  · **绝对门槛（2026-11-13 用户选"1"）**：非双十经理的计分基金须5年年化≥0
    （先按门槛滤基金再取最高分作计分基金——展示的计分数字必然≥0）
  · 通道按**经理本人**从业（>10 A / 6~10 B）；G3s/G4s 为基金级逐年排位考卷
  · 进榜须**本人**任本基金≥5年（pair 级 S3，与 eqgate 闸同口径；
    验证脚本 eq_two_list_now 为 fund 级宽松口径，本引擎从严，规模会有小差）
  · **展示桶=三类（2026-11-13 用户定）**：成长(科技/新能源/医药)·价值(价值/周期/消费)
    ·均衡——徽章/筛选/详情/导出全用三类；评分同格仍7桶细分（检验18-20已验证口径）
  · 代表作 = 计分基金（R1任期规则退役，过渡期结束）+ 规模最大（A/C份额按
    base_name 合并同基金不重复展示）+ 业绩最佳——按基金基础名去重
  · 特征缓存按指纹（净值/风格/基准 mtime + 风格字典哈希）失效：数据没变就秒开
  · F10 只读缓存+补缺失（start 是历史事实；离任由 mgr_em 3 天表兜住）
  · --offline / pipeline --skip-refresh：不刷旧缓存，缺啥补啥（持仓/机构占比同）

输出：返回 payload dict（pipeline 嵌 data.js 的 mgr 段）+ 写
  data/eq_score_{mgrs,funds}.csv（回核）+ data/eq_roster.json（名单状态：
  入场期/连续不过次数/毕业标记——gitignore 运行态，丢了=榜龄重新起算）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import urllib.request
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

warnings.filterwarnings("ignore")
import numpy as np                                     # noqa: E402
import pandas as pd                                    # noqa: E402

from . import data as D                                # noqa: E402
from . import metrics as M                             # noqa: E402
from . import eqgate as G                              # noqa: E402
from .screen import base_name                          # noqa: E402  A/C 份额合并

ROOT = Path(__file__).resolve().parent.parent
NAV_DIR = ROOT / "data" / "cache"
ROSTER_PATH = ROOT / "data" / "eq_roster.json"
FEAT_CACHE = ROOT / "data" / "eq_score_feat.pkl"
CSV_MGR = ROOT / "data" / "eq_score_mgrs.csv"
CSV_FUND = ROOT / "data" / "eq_score_funds.csv"

# W3 权重（docs/20 §2.2：LOOCV-IC 三起点定权，F4 负 IC 归零）——产品决策
W3 = {"F1": 0.27, "F3": 0.25, "F5": 0.19, "F2": 0.15, "F6": 0.14}
# F3/F5 因子源开关（2026-11-14 评分基准修复，七问排查 P1 落地）：
#   "raw"    = 单代理β=1 / 类型合成（原产线；检验18-20 基线口径）
#   "scaled" = 6代理周频回归β尺度化（F3=ret−Σβᵢ·代理ᵢ；β缺失回退raw，不产生新NaN）
# **2026-11-14 复测裁决：不切换，保持 "raw"**——eq_score_test 三起点全量复测：
#   IC 2/3 起点劣化（2025起点 +0.184→+0.145）、检验20 score-vs-ret5y 相关
#   0.66→0.78（尺度化反而更贴原始收益）、检验19 留存打平 → 未过"不劣化"闸。
#   矛盾证据如实记：eq_backtest_t0（全池、单段累计未来度量）曾显示 W3s 略优
#   ——以官方检验18-20 为准（docs/20 §2.2 验收口径）。双列 F3s/F5s 保留供复测。
# 因子每次 build 现算，feat 缓存只存输入（yearly/ret5y），切换本开关无需动缓存指纹。
W3_FACTOR_SRC = "raw"
# 考卷阈值（docs/20 §2.4 L2）——产品决策
G3S_MIN_TOP = 3
G3S_PCT = 75.0
G4S_MEAN = 70.0
# 净值断档阈值：末值距锚点超过此天数视为"断数据"→ 资格失效即时出（§2.4 L3）
NAV_STALE_D = 180
# 双十（定义）：从业>10年 且 含债复合年化>10% → 仅展示标签（11-15 免考卷取消、11-16 加分删除）
DUAL_CAREER_MIN_Y = 10.0
DUAL_ANN_MIN = 0.10
# ---- 基金级闸（2026-11-18 重构后保留）----
# G1 规模闸：计分基金**基础名合并规模** ≥2 亿元（S5 落地；Tm起点 cut+17.2% vs
#    kept+32.7%、T0 +1.5% vs +3.0%，池级两起点同向）
SCALE_MIN_YI = 2.0
# G3 类型感知仓位闸：10季平均股票仓位（**百分数**）——股票型/偏股≥50%、灵活/平衡≥30%
#    （偏股雷达要的是真权益管理者；8%仓位的"灵活"基金是债性产品。合同允许低仓的
#     灵活/平衡用低阈值防误伤；R4组伪前瞻 Tm +6.7% vs kept+32.9%、T0 同格排位25 vs49）
POS_MIN_BY_TYPE = {"股票型": 50.0, "混合型-偏股": 50.0,
                   "混合型-灵活": 30.0, "混合型-平衡": 30.0}
# ---- 经理级两闸（2026-11-18 用户拍板：G2/0%门槛/新锐回撤 三闸合并为比例闸）----
# 比例闸 r = 窗口复合年化 / 窗口复合最大回撤（窗口按通道：老将10年/新锐6年）。
#   证据（eq_persist_backtest 8期回测，剔债口径）：T=0.05 过闸更好 +3.93pp（4/4期）；
#   T=0.10 +1.87pp（正向但变薄）；**T≥0.15 反转**（-0.3~-3.6pp，高比值=防守型前瞻
#   反而差）。2026-11-18 用户先收紧到0.10、同日又改回0.05（拍板记录：以最优证据档
#   为准）；r≥0.05 ⇒ 复合年化必为正 ⇒ G2/0%门槛仍被严格蕴含。
RATIO_MIN = 0.05
# 规模闸：经理现任基金资产总规模 >10 亿（2026-11-18 用户拍板；数据缺失 fail-open）
AUM_MIN_YI = 10.0
# 回撤修复闸（2026-11-18 用户拍板："回撤修复大于6年的拦下"）：窗口内最大回撤
# 谷底→回前高 >6年（2190天）拦；未修复者按窗口封顶（老将10年/新锐6年均>6年 →
# 天然被拦）。数据缺失 fail-open。注：回撤修复作为**评分**因子8期IC≈0（无增量），
# 仅作闸用（定性规则：6年还没爬出坑 = 不算把握市场）。
RECOVER_MAX_D = 6 * 365
# 展示上限（2026-11-18 用户拍板"老将40+新锐25"）：资格=过全部新闸（当期全量进
# roster 状态与 CSV），页面只展示各通道按分排序的前 N；导出图仍截前20
VET_SHOW_N = 40
NEW_SHOW_N = 25
# 小格合并（2026-11-14 七问排查 P1 落地）：A格(type×7桶)样本 < 此值 →
# 改在（类型×三类）组内取年窗分位，大格不动。
# eq_cell_merge_eval 双起点判据全过：M1 考卷两期 Jaccard 0.312→0.377、
# M2 小格秩分半信度 0.003→0.218（A下≈噪声）、M3 信号 IC 0.019→0.245。
CELL_MERGE_N = 150
# 双体系评分（2026-11-15 用户拍板"按推荐做"，留出期验证见 STATUS）：
#   老将(A通道) = 近1年动量 + 风格超额 + 回撤浅者优 各1/3（IC_fwd +0.143 vs 旧W3 -0.016）
#   新锐(B通道) = 风格超额 + 近1年动量 各1/2（+0.071 vs 旧 +0.036；加F1稀释→不加）
#   F6资历/F2/F5/F1 退出评分（证据：F6对老将显著为负）；滚动walk-forward与固定等权均验证
BIG_SCALE_YI = 100.0        # 拍板②A：规模不入分，≥100亿打"大"旗（仅展示）
# 双十：2026-11-16 用户拍板"加分也删"——仅作展示标签，不参与任何计分（DUAL_SCORE_BONUS 已移除）
# 生涯四因子权重（2026-11-16 用户拍板上线）：(c1水平, c2超额, c3年度胜率, c4回撤反向)
# = 0.2/0.2/0.2/0.4——回撤加倍有前瞻IC背书（四因子里 c4r 最高 +0.142）；
# 权重网格验证三验尺全进前4（周云#1/徐彦#3/刘旭#4，吴博俊压#13）；复合口径 c1≈支付宝
#（刘旭我们10.93% vs 支付宝11.62%，差0.69pp 在验证 MAE0.60 带内）。
CAREER_W = {"c1": 0.2, "c2": 0.2, "c3": 0.2, "c4r": 0.4}   # 新锐四因子（及基线）
# 2026-11-18 用户拍板："（剩余Alpha）直接进老将评分"——老将五因子。
#   权重=旧四因子**原比例缩放**（.18/.18/.18/.36 与 .2/.2/.2/.4 比例完全一致，
#   保持经11-16网格验证的相对结构）+ alpha0.10；依据（8期同台回测）：alpha 老将侧
#   唯一正信号（a_med IC+0.049一致83%；融合Δ+0.018 唯一为正）；新锐侧为负 → 不加。
#   迭代记录：.15×3+.4+.15 版回撤相对权重升至47% → 刘旭被挤到#5（破验尺）→ 回调。
CAREER_W_VET = {"c1": 0.17, "c2": 0.17, "c3": 0.17, "c4r": 0.34, "alpha": 0.15}
# 持仓校验降级（2026-11-13）：前10重仓中相关桶合计 <15% → 同格降均衡
HOLD_MIN = 0.15
HOLD_TTL_H = 24 * 30
# 展示桶归一（考卷仍用原始桶；此处只影响徽章/筛选/详情/导出）
DISPLAY_ALIAS = {"金融价值": "价值", "工业": "周期", "其他/均衡": "均衡"}
CANON7 = ["科技", "医药", "消费", "新能源", "价值", "周期", "均衡"]
# 三类展示（2026-11-13 用户定）：不细分行业，成长/价值/均衡
GROWTH_BUCKETS = {"科技", "新能源", "医药"}
VALUE_BUCKETS = {"价值", "周期", "消费"}
CANON3 = ["成长", "价值", "均衡"]
# 申万一级行业 → 7桶（持仓校验用；未列行业回退均衡）
IND2BUCKET = {
    "电子": "科技", "计算机": "科技", "通信": "科技", "传媒": "科技",
    "国防军工": "科技",
    "医药生物": "医药",
    "食品饮料": "消费", "家用电器": "消费", "商贸零售": "消费",
    "社会服务": "消费", "美容护理": "消费", "纺织服饰": "消费",
    "农林牧渔": "消费", "汽车": "消费",
    "电力设备": "新能源",
    "银行": "价值", "非银金融": "价值", "房地产": "价值",
    "公用事业": "价值", "环保": "价值",
    "煤炭": "周期", "钢铁": "周期", "石油石化": "周期",
    "有色金属": "周期", "基础化工": "周期", "建筑材料": "周期",
    "建筑装饰": "周期", "交通运输": "周期", "机械设备": "周期",
    "轻工制造": "周期",
}


def display_bucket(b) -> str:
    """展示桶 = 三类（2026-11-13 用户定）：只影响徽章/筛选/详情/导出，不影响评分同格。"""
    b7 = DISPLAY_ALIAS.get(b, b)
    if b7 in GROWTH_BUCKETS:
        return "成长"
    if b7 in VALUE_BUCKETS:
        return "价值"
    return "均衡"


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


def _inst_ratios(codes: list[str], refresh: bool, workers: int, log) -> dict:
    """机构持有比例 {code: 0~1}（f10 cyrjg 页，缓存7天）——代表作"机构占比"维度。
    refresh=False 只读缓存不联网。"""
    out: dict = {}
    if not codes:
        return out

    def _one(code):
        if refresh:
            return code, D.fetch_inst_ratio(code)
        v = D._load_cache(f"inst_{code}", 1e9)
        try:
            return code, (float(v) if v is not None else None)
        except (TypeError, ValueError):
            return code, None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for code, v in ex.map(_one, codes):
            if v is not None:
                out[code] = v
    log(f"      机构占比：{len(out)}/{len(codes)} 只有数")
    return out


def _positions(codes: list[str], refresh: bool, workers: int, log) -> dict:
    """季报股票仓位 {code: 平均仓位%}（东财移动API10季历史，缓存30天，
    与 scripts/eq_diag.py 共用 pos_*.pkl）。缺数据=不参与 G3 判定（fail-open）。"""
    out: dict = {}
    if not codes:
        return out

    def _one(code):
        p = NAV_DIR / f"pos_{code}.pkl"
        if p.exists():
            try:
                age_h = (time.time() - p.stat().st_mtime) / 3600
                if not refresh or age_h < HOLD_TTL_H:
                    return code, pd.read_pickle(p)
            except Exception:
                pass
        if not refresh:
            return code, None
        try:
            u = ("https://fundmobapi.eastmoney.com/FundMNewApi/"
                 f"FundMNAssetAllocation?FCODE={code}&deviceid=Wap"
                 "&plat=Wap&product=EFund&version=6.2.8")
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            raw = json.loads(urllib.request.urlopen(req, timeout=12)
                             .read().decode("utf-8", "ignore"))
            hist = [{"d": r.get("FSRQ"), "gp": float(r["GP"])}
                    for r in (raw.get("Datas") or [])
                    if r.get("GP") not in (None, "", "--")]
            if hist:
                try:
                    pd.to_pickle(hist, p)
                except Exception:
                    pass
                return code, hist
            return code, None
        except Exception:
            return code, None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for code, hist in ex.map(_one, codes):
            if hist:
                gps = [x["gp"] for x in hist if x.get("gp") is not None]
                if gps:
                    out[code] = float(np.mean(gps))
    log(f"      仓位闸：{len(out)}/{len(codes)} 只有10季仓位史"
        f"（缺数据者该闸跳过，不冤枉）")
    return out


def _asset_hist(code: str, refresh: bool) -> list | None:
    """季报资产配置史 [{d,gp,zq,hb,qt}...]（东财 FundMNAssetAllocation，缓存30天）。

    项8a（2026-11-17）：详情页"股票/债券/现金/其他"100%堆叠子图的数据源。
    字段已实测（110017 样例：GP/ZQ/HB/QT 四项+FSRQ 日期）。
    与 G3 仓位闸的 `_positions` 同一 API、不同缓存键——互不影响。
    refresh=False 只读未过期缓存（平时跑管线不联网，AGENTS §8）。"""
    p = NAV_DIR / f"posa_{code}.pkl"
    if p.exists():
        try:
            age_h = (time.time() - p.stat().st_mtime) / 3600
            if not refresh or age_h < HOLD_TTL_H:
                return pd.read_pickle(p)
        except Exception:
            pass
    if not refresh:
        return None
    try:
        u = ("https://fundmobapi.eastmoney.com/FundMNewApi/"
             f"FundMNAssetAllocation?FCODE={code}&deviceid=Wap"
             "&plat=Wap&product=EFund&version=6.2.8")
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
        raw = json.loads(urllib.request.urlopen(req, timeout=12)
                         .read().decode("utf-8", "ignore"))

        def _num(v):
            try:
                x = float(v or "")
                return None if x != x else x
            except (TypeError, ValueError):
                return None

        hist = []
        for r in (raw.get("Datas") or []):
            gp = _num(r.get("GP"))
            if r.get("FSRQ") and gp is not None:
                hist.append(dict(d=str(r["FSRQ"]), gp=gp, zq=_num(r.get("ZQ")),
                                 hb=_num(r.get("HB")), qt=_num(r.get("QT"))))
        hist.sort(key=lambda x: x["d"])          # API 最新在前 → 升序（时间轴从旧到新）
        if hist:
            try:
                pd.to_pickle(hist, p)
            except Exception:
                pass
            return hist
        return None
    except Exception:
        return None


# ---------------------------------------------------------------- 1b. 持仓校验降级
def fetch_hold_top10(code: str, refresh: bool = True) -> list | None:
    """前10重仓 [(行业, 占净值比例)]——天天基金移动 API（申万行业标签自带）。
    缓存 30 天（持仓季度更新）；refresh=False 只读缓存不联网。"""
    p = NAV_DIR / f"hold_{code}.pkl"
    if p.exists():
        try:
            age_h = (time.time() - p.stat().st_mtime) / 3600
            if not refresh or age_h < HOLD_TTL_H:
                return pd.read_pickle(p)
        except Exception:
            pass
    if not refresh:
        return None
    try:
        u = ("https://fundmobapi.eastmoney.com/FundMNewApi/FundMNInverstPosition"
             f"?FCODE={code}&deviceid=Wap&plat=Wap&product=EFund&version=6.2.8")
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
        raw = json.loads(urllib.request.urlopen(req, timeout=15)
                         .read().decode("utf-8", "ignore"))
        stocks = ((raw.get("Datas") or {}).get("fundStocks")) or []
        out = [(s.get("INDEXNAME") or "", float(s.get("JZBL") or 0) / 100.0)
               for s in stocks if s.get("INDEXNAME")]
        if not out:
            return None
        try:
            pd.to_pickle(out, p)
        except Exception:
            pass
        return out
    except Exception:
        return None


def apply_holdings_check(styles: dict, refresh: bool, log) -> dict:
    """持仓校验降级（2026-11-13 用户Q2"医药第一怎么办"的解法）。

    相关性桶 X 的基金，前10重仓中 X 系行业合计 <HOLD_MIN → 降为均衡：
    苏的两只基金（医药相关但前10医药5%/11%）→ 均衡格（与真分散基金比，
    不再白拿医药崩盘格的相对高分）；真行业基金（前10同行业30%+）不受影响。
    只降级不升级；fetch 失败（无数据）保留原桶。refresh=False 只读已有缓存。
    """
    todo = {c: b for c, b in styles.items() if b and b != "均衡"}
    if not todo:
        return styles
    down = miss = 0

    def _one(item):
        c, b = item
        top = fetch_hold_top10(c, refresh=refresh)
        if top is None:
            return c, None
        tot = sum(w for ind, w in top if IND2BUCKET.get(ind, "均衡") == b)
        return c, tot

    with ThreadPoolExecutor(max_workers=8) as ex:
        for c, tot in ex.map(_one, todo.items()):
            if tot is None:
                miss += 1
            elif tot < HOLD_MIN:
                styles[c] = "均衡"
                down += 1
    log(f"      持仓校验：非均衡 {len(todo)} 只 → 降级 {down} 只"
        f"（前10该行业合计<{HOLD_MIN:.0%}）/无持仓数据 {miss}")
    return styles


# ---------------------------------------------------------------- 1c. 双十判定
def _manager_id_of(mgr: str, codes, f10s: dict) -> str | None:
    """在该经理的基金集合里找他的经理页 id：f10 主经理名与本人一致才可信
    （共管基金的 jbgk 主经理可能是别人——同名/串页防护）。"""
    for code in codes:
        info = f10s.get(code)
        if info and info.get("manager") == mgr and info.get("manager_id"):
            return str(info["manager_id"])
    return None


def career_dual_flags(pairs: pd.DataFrame, f10s: dict, log) -> dict:
    """双十（2026-11-13 用户定；**2026-11-14 年化口径改版**）：从业>10年 且 从业年化>10%。

    从业年化 = **等权组合复合年化**（`D.manager_career_ann`）——2026-11-14 用户
    报10个支付宝样本钉死：该经理全部非债非货基产品各自上任日起的等权组合年化，
    MAE=0.60pp（8/10 在±0.6内）；旧口径"任职最久单基金"MAE4.4、逐产品均值4.6、
    中位5.4 全被样本否决（见 STATUS §6）。
    返回 {key: {"dual": bool, "ann": float|None, "code": None}}；
    只算从业>10 年者；8线程并行（经理页7天缓存、净值20h缓存、结果7天缓存）。"""
    career: dict = {}
    codes_of_key: dict = {}
    for r in pairs.itertuples(index=False):
        k = f"{r.mgr}|{r.company}"
        cy = r.career_y
        if cy is not None and cy == cy:
            career[k] = max(career.get(k, 0.0), float(cy))
        codes_of_key.setdefault(k, set()).add(r.code)
    keys = [k for k, cy in career.items() if cy > DUAL_CAREER_MIN_Y]

    def _ann(k):
        mid = _manager_id_of(k.split("|", 1)[0], codes_of_key.get(k, set()), f10s)
        if not mid:
            return k, None
        return k, D.manager_career_ann(mid)

    with ThreadPoolExecutor(max_workers=8) as ex:
        anns = dict(ex.map(_ann, keys))
    out: dict = {}
    n_dual = n_none = 0
    for k in keys:
        ann = anns.get(k)
        dual = ann is not None and ann > DUAL_ANN_MIN
        n_dual += bool(dual)
        n_none += ann is None
        out[k] = dict(dual=dual, ann=ann, code=None)
    log(f"      双十判定：从业>10年 {len(keys)} 人 → "
        f"双十(等权组合年化>{DUAL_ANN_MIN:.0%}) {n_dual} 人（2026-11-16 起仅展示标签、"
        f"不参与计分；年化不可得 {n_none} 人按非双十）")
    return out


# ---------------------------------------------------------------- 生涯四因子（2026-11-16 上线）
def career_factors_for(key: str, qg, f10s: dict, mkt, window_y: float | None = None,
                       weekly: dict | None = None):
    """单经理生涯因子@今天（用户2026-11-15 规格：所有管理过的含已离职、共管不拆）。

    c1 = 等权复合年化（`D.manager_career_ann`，≈支付宝口径，2026-11-17 含债修订 MAE1.42pp）；
    c2 = 逐产品 (任职年化−同期市场年化) 按管理天数加权；
    c3 = 复合曲线年度胜率（≥3年才可算，全历史）；
    c4 = **窗口内**复合最大回撤（2026-11-18 用户定：老将近10年/新锐近6年，
         window_y 由调用方按通道传入；None=全历史回退）。窗口超过其生涯长度时取全部可得。
    a_med/a_min = **剩余Alpha**（2026-11-18 用户拍板进老将评分）：窗口内滚动回归
         截距（周频，市场+6风格，2年窗季度步）的中位/最差，年化——weekly 由调用方
         预建（None → alpha 不算，评分按缺失50中性）。
    产品窗 [start, min(end, 今天)]，≥250天；任一失败该因子 NaN（装配时中性50分位）。"""
    codes = set(qg["code"].tolist())
    mid = _manager_id_of(key.split("|", 1)[0], codes, f10s)
    if not mid:
        return None
    c1 = D.manager_career_ann(mid)
    spans = D._manager_products_spans(mid) or []
    now = pd.Timestamp.today()
    num2 = den2 = 0.0
    rets = []
    aum_eq = 0.0
    for p in spans:
        # 2026-11-18 剔债（张清华案例，用户定模块定位）：债券/偏债产品不计主动权益能力，
        # c2/c3/c4/r 与 c1（manager_career_ann 同款过滤）保持同一产品口径
        if D._is_bond_like(p.get("code", ""), p.get("name") or ""):
            continue
        # 在管非债产品的 f10 规模和（2026-11-18 用户定：管理规模只统计计算从业年化
        # 的产品——偏债不计；仅池内代码有 f10 缓存，池外权益如 QDII 规模量级小不计）
        if p.get("end") is None:
            try:
                aum_eq += float((f10s.get(p.get("code")) or {}).get("scale_yi") or 0)
            except (TypeError, ValueError):
                pass
        s0 = p.get("start")
        e0 = p.get("end") or now
        try:
            s0 = pd.Timestamp(s0) if s0 is not None else None
            e0 = min(pd.Timestamp(e0), now)
        except (ValueError, TypeError):
            continue
        if s0 is None or e0 <= s0:
            continue
        nav = _read_nav_pickle(p.get("code"))
        if nav is None or nav.empty:
            try:
                nav = D.fetch_nav(p.get("code"))
            except Exception:
                nav = None
        if nav is None or nav.empty:
            continue
        sl = nav[(nav["date"] >= s0) & (nav["date"] <= e0)]
        if len(sl) < 250:
            continue
        days = (sl["date"].iloc[-1] - sl["date"].iloc[0]).days
        if days < 250:
            continue
        try:
            ann = float(sl["nav"].iloc[-1] / sl["nav"].iloc[0]) ** (365.0 / days) - 1
        except (ZeroDivisionError, OverflowError, ValueError):
            continue
        rets.append(sl.set_index("date")["nav"].pct_change().dropna())
        if mkt is not None:
            try:
                m = s_ann(mkt, s0, e0)
            except Exception:
                m = None
            if m is not None and m == m:
                num2 += days * (ann - m)
                den2 += days
    c2 = (num2 / den2) if den2 > 0 else float("nan")
    c3 = c4 = float("nan")
    r = float("nan")
    a_med = a_min = None
    rec = None
    if rets:
        try:
            port = pd.concat(rets, axis=1).mean(axis=1, skipna=True).dropna()
            if len(port) >= 500:
                curve = (1 + port).cumprod()
                yr = curve.resample("YE").last().pct_change().dropna()
                if len(yr) >= 3:
                    c3 = float((yr > 0).mean())
                # c4：通道窗口（老将10年/新锐6年）；数据不足200点→NaN
                seg = curve
                if window_y:
                    cut = port.index[-1] - pd.DateOffset(years=window_y)
                    seg = curve[curve.index > cut]
                if len(seg) >= 200:
                    peak = seg.cummax()
                    c4 = float(((peak - seg) / peak).max())
                    # 回撤修复天数（2026-11-18 用户拍板进筛选闸：>6年拦下）：
                    # 窗口内最大回撤谷底→回前高；未修复→窗口封顶（老将10年/新锐6年
                    # 封顶值均 >6年 → 未修复者天然被拦，语义自洽）
                    ddv = ((peak - seg) / peak).values
                    ti = int(np.argmax(ddv))
                    after = seg.values[ti:]
                    peak_at = peak.values[ti]
                    hit = np.where(after[1:] >= peak_at)[0]
                    if len(hit):
                        rec = int((seg.index[ti + int(hit[0]) + 1]
                                   - seg.index[ti]).days)
                    else:
                        rec = int((seg.index[-1] - seg.index[0]).days)
                    # 比例闸 r（2026-11-18 用户定；回测4/4期+3.9pp，T=0.05）
                    # = 同窗几何年化 / 同窗回撤（回撤0.05%下限防除零；负年化→负比自然淘汰）
                    try:
                        yrs_w = (seg.index[-1] - seg.index[0]).days / 365.0
                        ann_w = (float(seg.iloc[-1] / seg.iloc[0]) ** (1.0 / yrs_w) - 1
                                 if yrs_w >= 1 else float("nan"))
                    except (ZeroDivisionError, OverflowError, ValueError, IndexError):
                        ann_w = float("nan")
                    if ann_w == ann_w:
                        r = ann_w / (c4 if c4 > 0.0005 else 0.0005)
                # 剩余Alpha（2026-11-18 用户拍板进老将评分）：窗口滚动回归截距，
                # 周频复利聚合、市场+6风格、2年窗季度步，年化（与回测脚本同构）
                if weekly and window_y:
                    pw = port[port.index > cut]
                    if len(pw) >= 500:
                        wser = (1 + pw).resample("W-FRI").prod() - 1
                        X = pd.concat(
                            [weekly["m"].rename("m")]
                            + [s.rename(b) for b, s in weekly["prox"].items()],
                            axis=1).reindex(wser.index)
                        dfx = pd.concat([wser.rename("y"), X], axis=1).dropna()
                        if len(dfx) >= 150:
                            als = []
                            for st in range(0, len(dfx) - 104 + 1, 13):
                                sg2 = dfx.iloc[st:st + 104]
                                Xm = np.column_stack(
                                    [sg2.drop(columns=["y"]).values,
                                     np.ones(len(sg2))])
                                try:
                                    coef, *_ = np.linalg.lstsq(
                                        Xm, sg2["y"].values, rcond=None)
                                except np.linalg.LinAlgError:
                                    continue
                                als.append(coef[-1] * 52)
                            if len(als) >= 4:
                                aa = np.array(als)
                                a_med = float(np.median(aa))
                                a_min = float(np.min(aa))
        except Exception:
            pass
    return dict(c1=c1, c2=c2, c3=c3, c4=c4, r=r, rec=rec,
                a_med=a_med, a_min=a_min, aum=round(aum_eq, 2))


def career_weighted_scores(facts: dict, chan_of: dict) -> dict:
    """生涯因子 → 通道内分位加权分（纯函数，供单测）。

    2026-11-18 用户拍板："（剩余Alpha）直接进老将评分"——
      老将(A通道) = CAREER_W_VET 五因子（c1/c2/c3 各0.15 + c4r0.4 + alpha0.15）；
      新锐(B通道) = CAREER_W 四因子不变（alpha 在新锐侧 IC 为负，数据不支持）。
    alpha = 剩余Alpha（2026-11-18 用户拍板进老将评分）：**滚动区间中位数**的
    通道分位（主轴主判断"多区间中位数为正"）。最差区间 a_min 保留进 data.js 供
    展示/回核但**不进分**——数据裁定：双条件取弱分位版会把验尺徐彦从#1压到#8
    （其最差窗-13.5%被重罚），而 fwd IC 单条件几乎不亏（+0.018 vs +0.020），
    验尺优先（2026-11-18 用户"具体要看数据分析"授权）。
    每因子在本通道内对非缺失值取分位（0-100），缺失=50 中性；c4 反向；
    全因子缺失的 key 不产出（调用方回落）。"""
    out: dict = {}
    for ch in ("A", "B"):
        keys = [k for k in facts
                if chan_of.get(k) == ch and facts[k] is not None]
        if not keys:
            continue
        wmap = CAREER_W_VET if ch == "A" else CAREER_W
        for f, w in wmap.items():
            if f == "alpha":
                #只取区间中位数分位（双条件版破坏验尺，见 docstring）
                vals = {k: facts[k].get("a_med") for k in keys}
                ok = {kk: vv for kk, vv in vals.items()
                      if vv is not None and vv == vv}
                pct = pd.Series(ok, dtype=float).rank(pct=True) * 100
                for k in keys:
                    out.setdefault(k, 0.0)
                    out[k] += float(pct.get(k, 50.0)) * w
                continue
            src = "c4" if f == "c4r" else f
            vals = {k: facts[k].get(src) for k in keys}
            ok = {k: v for k, v in vals.items()
                  if v is not None and v == v}          # NaN 排除
            s = pd.Series(ok, dtype=float)              # 空 → 全通道中性50（刻度恒满）
            if f == "c4r":
                s = -s                                  # 回撤浅=高分
            pct = s.rank(pct=True) * 100
            for k in keys:
                out.setdefault(k, 0.0)
                out[k] += float(pct.get(k, 50.0)) * w   # 缺失因子=50 中性

    def _has_num(d: dict) -> bool:
        return any(v is not None and v == v for v in d.values())

    return {k: round(v, 1) for k, v in out.items()
            if _has_num(facts.get(k) or {})}


# ---------------------------------------------------------------- 2. 特征（指纹缓存）
def _feat_fingerprint(styles: dict | None = None) -> tuple:
    """特征 = f(净值, 风格分类, 合成基准, 风格字典内容)；任一变动即失效。

    风格字典哈希（2026-11-13 加）：持仓校验降级会改 styles 但不改 eqgate 缓存
    文件 mtime——必须把字典内容本身纳入指纹，否则降级后特征缓存不刷新。
    """
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
    sty_h = hashlib.md5(
        json.dumps(sorted((styles or {}).items()),
                   ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return (nav_n, nav_mx, _stat(G.STYLE_CACHE),
            _stat(NAV_DIR / "bond_index_wealth.pkl"), hs_mx, sty_h,
            f"cellm{CELL_MERGE_N}", "mdd+dualw1")   # 11-14小格合并；11-15 mdd特征+双体系


def windowed_mdd(nav: pd.DataFrame, T, years: int = 5) -> float | None:
    """[T-years, T] 窗口内相对区间高点的最大回撤（正数）；样本不足 → None。"""
    sub = nav[nav["date"] <= T]
    if sub.empty:
        return None
    end = sub["date"].max()
    w = sub[sub["date"] > end - pd.DateOffset(years=years)]
    if len(w) < 100:
        return None
    v = w["nav"].values
    peak = np.maximum.accumulate(v)
    return float(((peak - v) / peak).max())


def build_features(pool: pd.DataFrame, styles: dict, log) -> dict:
    """S4 人群特征：back0..4 逐年收益 + ps_0..4 同格逐年分位 + cexc 合成超额
    + ret5y + nav_end（断档判定）。锚点 o = 构建日；指纹没变直接复用缓存。
    """
    fp = _feat_fingerprint(styles)
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
                         mdd=windowed_mdd(nav, o),
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
    # 2026-11-14 小格合并（七问排查 P1；eq_cell_merge_eval 双起点判据全过：
    #   M1 考卷两期 Jaccard 0.312→0.377、M2 小格秩分半信度 0.003→0.218
    #   （A 格下小格分位≈纯噪声）、M3 信号 IC 0.019→0.245）：
    #   A格样本 < CELL_MERGE_N → 改在（类型×三类）组内取分位，大格维持 A。
    feat["_c3"] = [display_bucket(b) for b in feat["bucket"]]
    small_cell = (feat.groupby(["type", "bucket"])["bucket"]
                  .transform("size") < CELL_MERGE_N)
    for j in range(5):
        ps_a = rank_pcts(feat, f"back{j}",
                         [["type", "bucket"], ["bucket"], ["type"]])
        ps_b = rank_pcts(feat, f"back{j}",
                         [["type", "_c3"], ["_c3"], ["type"]])
        feat[f"ps_{j}"] = pd.Series(np.where(small_cell, ps_b, ps_a),
                                    index=feat.index)
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
def _fund_beta(code: str, o, proxy_w: dict) -> dict | None:
    """6代理周频回归β（窗口[基金净值末日-5y, 末日]∩≤o，绝对空间，带截距）。
    F3/F5 尺度化用（W3_FACTOR_SRC='scaled'）；样本<100周/无净值 → None（调用方回退raw）。"""
    nav = _read_nav_pickle(code)
    if nav is None or nav.empty:
        return None
    sub = nav[nav["date"] <= o]
    if sub.empty:
        return None
    end = sub["date"].max()
    w = sub[sub["date"] > end - pd.DateOffset(years=5)].set_index("date")["nav"]
    rf = w.resample("W-FRI").last().pct_change().dropna()
    if len(rf) < 100:
        return None
    X = pd.DataFrame({b: s for b, s in proxy_w.items() if s is not None})
    if X.empty:
        return None
    dfx = pd.concat([rf.rename("f"), X], axis=1, join="inner").dropna()
    if len(dfx) < 100:
        return None
    A = np.column_stack([dfx.drop(columns=["f"]).values, np.ones(len(dfx))])
    try:
        coef, *_ = np.linalg.lstsq(A, dfx["f"].values, rcond=None)
    except Exception:
        return None
    return dict(zip(list(dfx.columns), coef[:-1]))


def build_factors(o, feat: pd.DataFrame, qual_funds: set,
                  pairs: pd.DataFrame, f10s: dict, wealth: dict) -> pd.DataFrame:
    """合格基金 6 因子 + 池内分位 + W3 分（检验18 同口径，现时点 shift=0）。

    2026-11-14：额外产出 **F3s/F5s（6代理β尺度化变体）**——β 缺失回退 raw 值
    （不产生新 NaN）；score 按 W3_FACTOR_SRC 选 F3/F5 分位（raw=原产线）。"""
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
    proxy_w = {b: (cl.resample("W-FRI").last().pct_change().dropna()
                   if cl is not None else None)
               for b, cl in proxy_close.items()}
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
        # ---- 尺度化变体（W3_FACTOR_SRC='scaled' 的候选；β缺失回退 raw）----
        beta = _fund_beta(c, o, proxy_w)
        if beta:
            d5 = sum(beta[b] * proxy_5y[b] for b in beta
                     if proxy_5y.get(b) is not None)
            a5s = (None if pd.isna(f["ret5y"]) else f["ret5y"] - d5)
            adjs = []
            for j in range(5):
                drag = sum(beta[b] * proxy_y[b][j] for b in beta
                           if b in proxy_y and proxy_y[b][j] is not None)
                adjs.append(None if f[f"back{j}"] is None else f[f"back{j}"] - drag)
            f5s = (sum(adjs) - max(adjs)
                   if all(x is not None for x in adjs) else None)
        else:
            a5s, f5s = a5, (sum(adj) - max(adj)
                            if all(x is not None for x in adj) else None)
        ps = [f[f"ps_{j}"] for j in range(5)]
        ok_ps = all(not pd.isna(x) for x in ps)
        ok_adj = all(x is not None for x in adj)
        rows[c] = dict(
            F1=float(np.mean(ps)) if ok_ps else None,
            F2=float(sum(1 for x in ps if x > 50)) if ok_ps else None,
            F3=a5,
            F4=(-float(np.std(adj, ddof=1))) if ok_adj else None,
            F5=(sum(adj) - max(adj)) if ok_adj else None,
            F3s=a5s, F5s=f5s,
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
    # 合格池内分位（0-100，展示用 F1..F6，口径不变）
    for col in ["F1", "F2", "F3", "F4", "F5", "F3s", "F5s", "ten", "car"]:
        v = pd.to_numeric(df[col], errors="coerce")
        df[col + "_p"] = (v.rank(pct=True) * 100).round(2)
    df["F6_p"] = df[["ten_p", "car_p"]].mean(axis=1)
    # ---- 双体系评分（2026-11-15 拍板"按推荐做"；固定等权，留出期验证见 STATUS）----
    # 老将A = 近1年动量 + 风格超额 + 回撤浅者优 各1/3（IC_fwd +0.143 vs 旧W3 -0.016）
    # 新锐B = 风格超额 + 近1年动量 各1/2（+0.071 vs 旧 +0.036）
    # 分位在**通道内**取（与回测口径一致）；F6/F2/F5/F1 退出评分（证据见设计提案§1.1）
    carv = pd.to_numeric(df["car"], errors="coerce")
    chan = pd.Series(np.where(carv > DUAL_CAREER_MIN_Y, "A",
                              np.where(carv > 6, "B", None)), index=df.index)
    mom1 = pd.Series([float(feat.at[c, "back0"]) if c in feat.index else np.nan
                      for c in df.index], index=df.index, dtype="float64")
    mdd5 = pd.Series([float(feat.at[c, "mdd"]) if c in feat.index else np.nan
                      for c in df.index], index=df.index, dtype="float64")

    def _chan_pct(s: pd.Series, flip: bool = False) -> pd.Series:
        """通道内分位（0-100）；该通道<10只时回退全池分位。"""
        out = pd.Series(np.nan, index=df.index)
        pooled = pd.to_numeric(s, errors="coerce")
        for ch in ("A", "B"):
            idx = df.index[chan == ch]
            v = pooled.loc[idx]
            if flip:
                v = -v
            if len(idx) >= 10:
                out.loc[idx] = v.rank(pct=True) * 100
            else:
                pv = -pooled if flip else pooled
                out.loc[idx] = pv.rank(pct=True) * 100
        return out

    f3_cp = _chan_pct(pd.to_numeric(df["F3"], errors="coerce"))
    mom_cp = _chan_pct(mom1)
    mdd_cp = _chan_pct(mdd5, flip=True)          # 浅回撤=高分（老将体系第三因子）
    score = pd.Series(np.nan, index=df.index)
    a, b = chan == "A", chan == "B"
    score[a] = (mom_cp[a] + f3_cp[a] + mdd_cp[a]) / 3
    score[b] = (f3_cp[b] + mom_cp[b]) / 2
    # 通道分位与原始因子留列（回核 + 后续"因子拆解/差距归因"展示用）
    df["chan"] = chan
    df["mom1"], df["mdd5"] = mom1, mdd5
    df["mom1_cp"], df["F3_cp"], df["mdd_cp"] = mom_cp, f3_cp, mdd_cp
    df["score"] = score.round(1)
    return df


# ---------------------------------------------------------------- 5. 名单状态机
def roster_update(state: dict, cand: dict, eval_now: bool,
                  today: str, asof) -> tuple[dict, list]:
    """L3 复评状态机（纯函数，可单测）。

    cand = {key: {name, company, career, channel, qual_funds, pass_funds,
                  gate_ok, data_complete, has_pairs}}
      · 资格失效（离职/整组数据不全且无入围）→ 每次构建即时出，不受节奏保护
      · **2026-11-18 用户拍板：考卷删除**——资格 = pass_funds（基金级 G1+G3）
        ∧ gate_ok（经理级比例闸 r≥RATIO_MIN + 规模>AUM_MIN_YI）；复评时点判，
        换榜（毕业 新→老）只看通道（career>10），不过目标闸即出（榜龄保留的
        "毕业卷"随考卷一并删除）；首过不出/连续两期黄牌（考卷衍生）一并删除，
        名单=每期过闸者快照（与 2026-11-13 禁迟滞拍板一致）。
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

    # 闸复评 + 换榜（2026-11-18：考卷删除；比例/规模两闸不过即出，无黄牌缓冲）
    for key in list(members):
        c = cand.get(key)
        m = members[key]
        if c is None or c["channel"] in ("out", "unknown"):
            continue                              # 上面已处理/冻结
        if not c["qual_funds"]:
            continue                              # 数据不全冻结，留待下期
        target = "vet" if c["channel"] == "A" else "new"
        if not c.get("gate_ok", True):
            _exit(key, "经理闸未过（比例/规模）")
            continue
        if not c["pass_funds"]:
            _exit(key, "无过闸基金（G1/G3）")
            continue
        if m.get("list") != target:
            changes.append(f"换榜 {key}: {m.get('list')}→{target}")
            if target == "vet":
                m["graduated"] = today            # 毕业标记（榜龄保留=换榜非出榜）
        m["list"] = target
        m["funds"] = list(c["pass_funds"])
        m["fails"] = 0
        m["last_eval"] = today
        m["name"] = c["name"]
        m["company"] = c["company"]

    # 新候选入场（2026-11-18：过闸即入，无考卷）
    for key, c in cand.items():
        if key in members or not c["pass_funds"]:
            continue
        if c["channel"] not in ("A", "B"):
            continue
        if not c.get("gate_ok", True):
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
def build_candidates(pairs: pd.DataFrame, qual: pd.DataFrame,
                     f10s: dict, nav_ok: set,
                     feat: pd.DataFrame | None = None,
                     dual: dict | None = None,
                     positions: dict | None = None,
                     base_scale: dict | None = None) -> dict:
    """{mgr|company: {...}}——通道按本人从业；pass_funds = 本人入围基金里过基金级闸的。

    data_complete：本人所有在管基金都有可用 F10（否则"资格失效 vs 数据不全"分不清 → 冻结不判）。

    2026-11-18 用户拍板重构（本次评审计划）：
      · **考卷删除**（G3s/G4s 不再卡资格——同格=名字关键词分桶不准，用户批评成立；
        exam_flags 仍计算，仅供展示/回核）；
      · **G2/0%门槛/新锐回撤闸合并为经理级比例闸** r≥RATIO_MIN（在 build() 以
        gate_ok 施加，证据与阈值见 RATIO_MIN 注释）；
      · 新增经理级**规模闸** aum>AUM_MIN_YI（同在 build() gate_ok）；
      · 基金级保留 **G1 规模 + G3 仓位**（缺数据 fail-open 不冤枉）。
    """
    dual = dual or {}
    positions = positions or {}
    base_scale = base_scale or {}
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
                         data_complete=False, has_pairs=True,
                         dual=False, career_ann=None)
    # 本人从业 = 名下 pair 最大值
    for r in pairs.itertuples(index=False):
        key = f"{r.mgr}|{r.company}"
        cy = r.career_y
        if cy is not None and cy == cy:
            cur = cand[key]["career"]
            if cur is None or cy > cur:
                cand[key]["career"] = float(cy)

    def _hard_ok(code):
        """G1 规模 / G3 仓位两闸（2026-11-18 起仅此两闸；G2 已并入经理级比例闸）。
        数据缺失 → 跳过该闸（fail-open，不因数据空洞冤枉人）。"""
        if feat is None or code not in feat.index:
            return False
        sc = base_scale.get(base_name(str(feat.at[code, "name"])))
        if sc is not None and 0 < sc < SCALE_MIN_YI:
            return False                                  # G1 壳基金
        pos = positions.get(code)
        if pos is not None:
            need = POS_MIN_BY_TYPE.get(str(feat.at[code, "type"]), 30.0)
            if pos < need:
                return False                              # G3 低仓位
        return True

    for key, c in cand.items():
        c["channel"] = G.channel(c["career"])
        rows = qf.get(key, [])
        c["qual_funds"] = [r.code for r in rows]
        info = dual.get(key) or {}
        c["dual"] = bool(info.get("dual"))
        c["career_ann"] = info.get("ann")
        # 2026-11-18：过闸即评分——pass_funds 只看基金级 G1+G3（考卷/0%/新锐回撤已删，
        # 经理级比例闸+规模闸由 build() 的 gate_ok 施加在入场与复评上）
        c["pass_funds"] = [r.code for r in rows if _hard_ok(r.code)]
        codes = pf.get(key, [])
        c["data_complete"] = (bool(codes)
                              and all(_f10_ok(f10s.get(x)) for x in codes)
                              and all(x in nav_ok for x in codes))
    return cand


# ---------------------------------------------------------------- 7. 明细块
def _nav_at(nav: pd.DataFrame, d):
    sub = nav[nav["date"] <= d]
    return float(sub["nav"].iloc[-1]) if len(sub) else None


def _mgr_detail(codes: list[str], basis: list[str], mgr: str, feat: pd.DataFrame,
                fdf: pd.DataFrame, f10s: dict, wealth: dict, o,
                weekly_ex: dict, inst_map: dict | None = None,
                base_scale: dict | None = None,
                base_n: dict | None = None,
                refresh: bool = True) -> dict:
    """代表作 + 5 分析块（a 净值曲线/b 逐年/c 逐季胜率/d 风格暴露/e 风险）+ 资产配置史。

    代表作规则（2026-11-13 用户反馈后改版，R1任期最久退役）：
      ① 评分最高 = 撑起该经理分数的基金（basis 里基金分最高——行内/详情首卡都用它，
        杜绝"展示-3.4%的基金、分数却来自+12%的基金"错位；标签 2026-11-17 由"计分"改名）
      ② 规模最大 = A/C 份额按 base_name 合并后的规模合计最大基金
      ③ 业绩最佳 = F1（同格平均排位）最高
      按基金基础名去重（同基金 A/C 不重复展示）；规模显示=合并规模。
      2026-11-17（界面优化项6/8a）：每张代表作补 5年最大回撤+夏普；产出 alloc 资产
      配置史（股/债/现金/其他堆叠子图，refresh=False 时只读未过期缓存）。
    """
    inst_map = inst_map or {}

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
        if fdf is None or fdf.empty or code not in fdf.index:
            return None
        v = fdf.at[code, "F1"]
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return None if np.isnan(v) else v

    def _score(code):
        if fdf is None or fdf.empty or code not in fdf.index:
            return None
        v = fdf.at[code, "score"]
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return None if np.isnan(v) else v

    def _cname(code):
        return feat.at[code, "name"] if code in feat.index else code

    # ---- 计分基金（basis 中 score 最高者 = 分数来源）
    sc_map = {c: _score(c) for c in (basis or [])}
    sc_map = {c: v for c, v in sc_map.items() if v is not None}
    count_code = max(sc_map, key=sc_map.get) if sc_map else None

    # ---- A/C 份额合并：按基础名分组（同基金不重复展示，规模求和）
    groups: dict = {}
    for c in codes:
        groups.setdefault(base_name(_cname(c)), []).append(c)

    def _g_scale(b):
        return sum(_scale(x) or 0.0 for x in groups[b])

    def _g_f1(b):
        vals = [_f1(x) for x in groups[b]]
        vals = [v for v in vals if v is not None]
        return max(vals) if vals else None

    # ---- 三个规则位（按组选，组内展示份额：评分最高=该位基金，其余=规模最大份额）
    picks: list = []
    if count_code is not None:
        picks.append(("评分最高", base_name(_cname(count_code)), count_code))
    for b in sorted(groups, key=_g_scale, reverse=True):
        if b not in {p[1] for p in picks} and (_g_scale(b) or 0) > 0:
            picks.append(("规模最大", b, None))
            break
    for b in sorted(groups,
                    key=lambda x: (_g_f1(x) is not None, _g_f1(x) or 0),
                    reverse=True):
        if b not in {p[1] for p in picks} and _g_f1(b) is not None:
            picks.append(("业绩最佳", b, None))
            break

    reps = []

    def _rep_risk(code):
        """项6（2026-11-17）：单基金5年最大回撤 + 夏普（nav缓存内算，缺→None）。"""
        nav_r = _read_nav_pickle(code)
        if nav_r is None:
            return dict(mdd5=None, sharpe5=None)
        try:
            mdd = M.max_drawdown(nav_r, 5)
            sh = M.sharpe_ratio(M.ann_return(nav_r, 5), M.ann_vol(nav_r, 5))
        except Exception:
            return dict(mdd5=None, sharpe5=None)
        return dict(mdd5=None if mdd is None else round(mdd, 4),
                    sharpe5=None if sh is None else round(sh, 2))

    for rule, b, forced in picks:
        g = groups[b]
        if forced is not None:
            code = forced
        else:
            code = max(g, key=lambda x: (_scale(x) or 0))   # 组内规模最大份额展示
        ten = _tenure(code)
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
        # 规模 = 基础名全份额合并（与 G1 闸同源；缺则回退组内求和）
        bkey = base_name(_cname(code))
        sc = (base_scale or {}).get(bkey)
        if sc is None:
            sc = _g_scale(b) or None
        reps.append(dict(code=code, name=_cname(code), rule=rule,
                         scale=round(sc, 2) if sc else None,
                         n_shares=(base_n or {}).get(bkey) or len(g),
                         tenure=None if ten < 0 else round(ten / 365.25, 1),
                         ret5y=None if r5 is None else round(r5, 4),
                         excess=None if (r5 is None or comp5 is None)
                         else round(r5 - comp5, 4),
                         inst=inst_map.get(code),
                         # 项6（2026-11-17）：代表作补 5年回撤 + 夏普（rf=SHARPE_RF）
                         **_rep_risk(code)))
    empty = dict(reps=reps, trend=None, yearly=[], quarterly=[],
                 style=None, risk=None)
    if not reps:
        return empty
    primary = reps[0]["code"]                       # 评分最高基金 = 分析块主体
    # 项8a（2026-11-17）：资产配置史（股/债/现金/其他 堆叠子图）——refresh=False 时
    # 只读未过期缓存（平时跑管线不联网，AGENTS §8）
    alloc = None
    try:
        raw_hist = _asset_hist(primary, refresh)
        if raw_hist:
            hist = sorted(raw_hist, key=lambda x: str(x.get("d") or ""))
            alloc = dict(d=[x["d"] for x in hist],
                         gp=[x.get("gp") for x in hist],
                         zq=[x.get("zq") for x in hist],
                         hb=[x.get("hb") for x in hist],
                         qt=[x.get("qt") for x in hist])
    except Exception:
        alloc = None
    nav = _read_nav_pickle(primary)
    if nav is None:
        return dict(empty, alloc=alloc)
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
                style=style, risk=risk, alloc=alloc)


# ---------------------------------------------------------------- 8. 主构建
def build(refresh: bool = True, workers: int = 8, log=print) -> dict | None:
    t0 = time.time()
    pool = _pool(refresh)
    em = _mgr_table(refresh)
    pairs = G.career_pairs(em, pool["code"].tolist())
    log(f"[1/6] 池 {len(pool)} 只 / 从业对 {len(pairs)}")

    navres = _nav_stats(pool["code"].tolist(), refresh, workers, log)
    s4 = [c for c, v in navres["stats"].items() if v[0] >= G.NAV_MIN_Y]
    log(f"[2/6] S4 净值≥5年 {len(s4)} 只")

    styles = G.classify_styles(s4) or {}
    log(f"[3/6] 风格分类 {len(styles)} 只（缓存7天）")
    # 持仓校验降级（2026-11-13）：前10重仓背离相关桶 → 同格降均衡（错格不公解法）
    styles = apply_holdings_check(styles, refresh=refresh, log=log)

    feat_bundle = build_features(pool, styles, log)
    feat, o, asof = feat_bundle["feat"], feat_bundle["o"], feat_bundle["asof"]
    wealth = feat_bundle.get("wealth") or {}

    # F10 + 资格 + 考卷
    cand_codes = sorted({r.code for r in pairs.itertuples(index=False)
                         if r.career_y is not None and r.career_y == r.career_y
                         and G.channel(r.career_y) in ("A", "B")
                         and r.code in feat.index})
    f10s = _f10s(cand_codes, refresh, workers, log)
    dual = career_dual_flags(pairs, f10s, log)      # 双十：从业>10y 且 年化>10%
    qual = qualify_pairs(pairs, feat, f10s, o)
    qset = set(qual["code"]) if not qual.empty else set()
    exams = exam_flags(feat)
    log(f"[4/6] 入围 pair {len(qual)} / 入围基金 {len(qset)}")

    # 因子 + W3 分
    fdf = build_factors(o, feat, qset, pairs, f10s, wealth)
    if fdf.empty:
        log("[5/6] 无因子可算")
        fdf = pd.DataFrame()
    else:
        sc = pd.to_numeric(fdf["score"], errors="coerce")
        log(f"[5/6] 因子可算 {len(fdf)} 只，均分 {sc.mean():.1f}")

    # 候选 + 名单状态机（2026-11-18：考卷删除——过闸即评分；基金级 G1+G3 在
    # build_candidates，经理级比例闸+规模闸在下方 gate_ok；状态机只留即时出+闸复评）
    # 基础名合并规模（G1 与展示同源）：f10 全份额按 base_name 求和
    name_of = dict(zip(pool["code"], pool["name"]))
    base_scale: dict = {}
    base_n: dict = {}
    for code, info in f10s.items():
        b = base_name(str(name_of.get(code) or ""))
        if not b:
            continue
        try:
            s = float((info or {}).get("scale_yi") or 0)
        except (TypeError, ValueError):
            s = 0.0
        base_scale[b] = base_scale.get(b, 0.0) + s
        base_n[b] = base_n.get(b, 0) + 1
    # 仓位（G3）：全部入围基金都要判（考卷已删，不再只抓过考∪双十；缓存外缺数据 fail-open）
    need_pos = set(qset)
    positions = _positions(sorted(need_pos), refresh, workers, log)
    cand = build_candidates(pairs, qual, f10s, navres["ok"],
                            feat=feat, dual=dual,
                            positions=positions, base_scale=base_scale)
    today = str(pd.Timestamp.today().date())
    state = load_roster() or dict(members={}, exits=[], eval_no=0)
    last_eval = state.get("eval_date")
    asof_s = str(asof)[:10] if asof is not None else ""
    eval_now = (not last_eval) or (
        str(state.get("last_asof") or "") != asof_s
        and (pd.Timestamp.today() - pd.Timestamp(last_eval)).days >= 6)
    qual_by_key: dict = {}
    if not qual.empty:
        for k, g in qual.groupby(["mgr", "company"]):
            qual_by_key[f"{k[0]}|{k[1]}"] = g

    # ---- 生涯四因子评分（2026-11-16 上线；2026-11-18 前移到名单复评之前——比例闸要用 r）----
    # 全候选（A/B 通道）并行：c1=复合年化(≈支付宝) c2=超额时间加权 c3=年度胜率
    # c4=通道窗口回撤(老将10年/新锐6年) r=同窗年化/回撤(比例闸)。
    # 权重 CAREER_W=(0.2/0.2/0.2/0.4)，通道内分位；近期三因子(mom1/F3/mdd)降为展示列。
    t_car = time.time()
    mkt_c = G._load_close(G.STYLE_MARKET)
    # 剩余Alpha所需周频序列（2026-11-18）：市场+6风格代理，一次构建全候选共用
    weekly = None
    if mkt_c is not None:
        weekly = {"m": mkt_c.resample("W-FRI").last().pct_change().dropna(),
                  "prox": {}}
        for _pn, _bkt in G.STYLE_PROXIES.items():
            _cl = G._load_close(_pn)
            if _cl is not None:
                weekly["prox"][_bkt] = _cl.resample("W-FRI").last().pct_change().dropna()
    cand_keys = [k for k, c in cand.items() if c.get("channel") in ("A", "B")]

    def _cf(k):
        qg = qual_by_key.get(k)
        if qg is None:
            return k, None
        # 2026-11-18 用户定：c4/比例闸/Alpha窗口按通道——老将10年 / 新锐6年
        wy = 10.0 if cand[k].get("channel") == "A" else 6.0
        try:
            return k, career_factors_for(k, qg, f10s, mkt_c, window_y=wy,
                                         weekly=weekly)
        except Exception:
            return k, None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        facts = dict(ex.map(_cf, cand_keys))
    facts = {k: v for k, v in facts.items() if v}
    car_scores = career_weighted_scores(
        facts, {k: cand[k]["channel"] for k in facts})
    log(f"      生涯四因子：可算 {len(facts)}/{len(cand_keys)}"
        f"（复合口径，权重 {CAREER_W}），耗时 {time.time() - t_car:.0f}s")

    # ---- 经理级三闸（2026-11-18 用户拍板）：比例闸 r≥RATIO_MIN + 规模>AUM_MIN_YI
    #      + 回撤修复≤RECOVER_MAX_D（>6年拦下）。证据见各常量注释；
    #      数据缺失一律 fail-open（不冤枉），复评节奏由 roster_update 管。
    n_rcut = n_acut = n_reccut = n_na = n_recna = 0
    for k, c in cand.items():
        if c.get("channel") not in ("A", "B"):
            continue
        r = (facts.get(k) or {}).get("r")
        r_ok = True
        if r is None or r != r:
            n_na += 1
        elif r < RATIO_MIN:
            r_ok = False
            n_rcut += 1
        # 规模闸用"在管非债规模"（2026-11-18 用户定：管理规模只统计计算从业年化
        # 的产品，偏债不计；来源=facts.aum 池内口径）。无 facts → None → fail-open。
        av = (facts.get(k) or {}).get("aum")
        aum_ok = True
        if av is not None and av <= AUM_MIN_YI:
            aum_ok = False
            n_acut += 1
        rec = (facts.get(k) or {}).get("rec")
        rec_ok = True
        if rec is None or rec != rec:
            n_recna += 1
        elif rec > RECOVER_MAX_D:
            rec_ok = False
            n_reccut += 1
        c["ratio"] = (r if r is not None and r == r else None)
        c["rec"] = rec
        c["aum"] = av
        c["gate_ok"] = r_ok and aum_ok and rec_ok
    log(f"      经理三闸：比例闸拦 {n_rcut}（r缺失放行 {n_na}）| 规模闸拦 {n_acut}"
        f" | 修复闸拦 {n_reccut}（缺失放行 {n_recna}）")

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

    # 从业年化补全（2026-11-14 等权组合口径，显示用）：双十集合只算了从业>10 年者，
    # 在榜其余成员的卡片也要显示从业年化 → 补算（经理页/净值均带缓存，8线程）
    miss = [k for k in state["members"]
            if k in cand and cand[k].get("career_ann") is None]
    if miss:
        def _fill(k):
            qg = qual_by_key.get(k)
            if qg is None:
                return k, None
            mid = _manager_id_of(k.split("|", 1)[0],
                                 set(qg["code"].tolist()), f10s)
            return k, (D.manager_career_ann(mid) if mid else None)
        with ThreadPoolExecutor(max_workers=8) as ex:
            got = dict(ex.map(_fill, miss))
        for k, v in got.items():
            if v is not None:
                cand[k]["career_ann"] = v
        log(f"      从业年化补全：在榜缺年化 {len(miss)} 人 → 算出 "
            f"{sum(1 for v in got.values() if v is not None)} 人")

    # 机构占比（代表作维度）：只抓在榜经理的入围基金；refresh 才联网
    member_codes: set = set()
    for key in state["members"]:
        qg = qual_by_key.get(key)
        if qg is not None:
            member_codes.update(qg["code"].tolist())
    inst_map = _inst_ratios(sorted(member_codes), refresh, workers, log)

    def _basis_of(key, c):
        """计分基准 = 在榜基金（roster 记录）∩可算分，回落入围基金——与分数同源。"""
        mem = state["members"].get(key)
        if mem:
            b = [x for x in mem.get("funds", []) if x in fdf.index]
            if b:
                return b
        return [x for x in c["qual_funds"] if x in fdf.index]

    def _score_of(key, c):
        # 2026-11-16 上线：经理分 = 生涯四因子通道分位加权（复合口径≈支付宝）
        if key in car_scores:
            return car_scores[key]
        # 回落：生涯因子全缺 → 计分基金分MAX（fail-open 不冤枉）
        basis = _basis_of(key, c)
        if not basis or fdf.empty:
            return None
        v = pd.to_numeric(fdf.loc[basis, "score"], errors="coerce").max()
        # 双十加分已删（2026-11-16 用户拍板），双十仅作展示标签
        return None if pd.isna(v) else float(v)

    lists = {"vet": [], "new": []}
    detail = {}
    for key, m in state["members"].items():
        c = cand.get(key)
        if c is None:
            continue                                # 已被硬条件剔除
        sc = _score_of(key, c)
        basis = _basis_of(key, c)
        qg = qual_by_key.get(key)
        qcodes = qg["code"].tolist() if qg is not None else []
        try:
            det = _mgr_detail(qcodes, basis, c["mgr"], feat, fdf, f10s, wealth,
                              o, weekly_ex, inst_map,
                              base_scale=base_scale, base_n=base_n,
                              refresh=refresh)
        except Exception as e:
            log(f"      [warn] 明细构建失败 {key}: {e}")
            det = dict(reps=[], trend=None, yearly=[], quarterly=[],
                       style=None, risk=None, alloc=None)
        rep0 = det["reps"][0] if det["reps"] else {}
        bucket = "均衡"
        if rep0 and rep0.get("code") in feat.index:
            bucket = display_bucket(feat.at[rep0["code"], "bucket"])
        entry = str(m.get("entry", today))
        ann = c.get("career_ann")
        # 管理规模（2026-11-18 用户定）：只统计计算从业年化的产品=在管非债规模（池内口径），
        # 偏债不计——em 总规模口径作废
        av = c.get("aum")
        row = dict(key=key, name=c["name"], company=c["company"],
                   career=round(c["career"], 1) if c["career"] else None,
                   tenure=(round(float(qg["tenure_y"].max()), 1)
                           if qg is not None and len(qg) else None),
                   bucket=bucket, score=sc, entry=entry[:7],
                   is_new=(entry == state.get("eval_date")
                           and int(state.get("eval_no", 0)) > 1),
                   n_funds=len(c["qual_funds"]),
                   rep_code=rep0.get("code"), rep_name=rep0.get("name"),
                   # 2026-11-13 用户加：计分基金5年年化 + 从业年化（平台口径）
                   ret5y=rep0.get("ret5y"),
                   ann_career=(round(ann, 4) if ann is not None else None),
                   dual=bool(c.get("dual")),
                   # 拍板②A（2026-11-15）：规模不入分，计分基金基础名合并≥100亿打"大"旗
                   big_scale=(base_scale.get(
                       base_name(str(rep0.get("name") or "")), 0.0)
                       >= BIG_SCALE_YI),
                   # 生涯四因子原值（回核/后续因子拆解；c1=ann_career 列已有）
                   **{f"car_{f}": (round(facts[key][f], 4)
                                   if key in facts
                                   and facts[key].get(f) is not None
                                   and facts[key].get(f) == facts[key].get(f)
                                   else None)
                       for f in ("c2", "c3", "c4", "a_med", "a_min")},
                   aum=av)
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

    # 展示上限（2026-11-18 用户拍板：老将前40 + 新锐前25）——资格（过全部新闸）在
    # roster 状态里是全量，这里只切展示层与明细；CSV 回核仍写全量（lists_full）
    lists_full = {k: list(v) for k, v in lists.items()}
    for _k, _cap in (("vet", VET_SHOW_N), ("new", NEW_SHOW_N)):
        lists[_k] = lists[_k][:_cap]
    _shown = {r["key"] for _l in lists.values() for r in _l}
    detail = {k: v for k, v in detail.items() if k in _shown}

    buckets = sorted({r["bucket"] for r in lists["vet"] + lists["new"]},
                     key=lambda b: CANON3.index(b) if b in CANON3 else 99)
    payload = dict(asof=asof_s or None,
                   eval_date=state.get("eval_date"),
                   eval_no=int(state.get("eval_no", 0)),
                   buckets=buckets, vet=lists["vet"], new=lists["new"],
                   detail=detail)

    # ---- CSV 回核
    try:
        mgr_rows = [dict(list_=k, **{kk: vv for kk, vv in r.items()
                                     if kk != "key"})
                    for k in ("vet", "new") for r in lists_full[k]]
        pd.DataFrame(mgr_rows).to_csv(CSV_MGR, index=False, encoding="utf-8-sig")
        frows = []
        for r in qual.itertuples(index=False):
            tag = exams.get(r.code) or ""
            fr = fdf.loc[r.code] if (not fdf.empty and r.code in fdf.index) else None
            row = dict(code=r.code, mgr=r.mgr, company=r.company,
                       channel=r.channel, tenure_y=r.tenure_y,
                       bucket=feat.at[r.code, "bucket"],
                       g3s="G3s" in tag, g4s="G4s" in tag,
                       ret5y=feat.at[r.code, "ret5y"],
                       # 双体系配套（2026-11-15）：5年回撤 + ≥100亿"大"旗
                       mdd=(feat.at[r.code, "mdd"]
                            if "mdd" in feat.columns else None),
                       big=(base_scale.get(
                           base_name(str(feat.at[r.code, "name"])), 0.0)
                           >= BIG_SCALE_YI),
                       **{f"ps_{j}": feat.at[r.code, f"ps_{j}"] for j in range(5)})
            if fr is not None:
                for k in ["F1", "F2", "F3", "F4", "F5", "F6", "score"]:
                    row[k] = fr.get(k)
            frows.append(row)
        pd.DataFrame(frows).to_csv(CSV_FUND, index=False, encoding="utf-8-sig")
    except Exception as e:
        log(f"      [warn] CSV 写出失败（{e}）")

    log(f"完成：老将榜 {len(payload['vet'])} 人 / 新锐榜 {len(payload['new'])} 人"
        f"（展示上限 {VET_SHOW_N}/{NEW_SHOW_N}，资格全量 "
        f"{sum(len(v) for v in lists_full.values())} 人），"
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
