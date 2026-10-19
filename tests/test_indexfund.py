# -*- coding: utf-8 -*-
"""债券指数分池 / 混合一级行为分层 / 指数穿插排名 / FOF 排除 的单元测试（不依赖网络）。

对应用户 2026-10-19 决议：
  · FOF 全部排除；池名改 中短债/长债/固收+；
  · 指数基金不看基金经理业绩，只看跟踪标的/业绩/费率/公司规模；
  · 指数业绩只看近三年，与同池主动 TOP-N 按业绩穿插、分数取相邻两只插值；
  · 同一跟踪指数最多 2 只，优先低费率 + 公司非货规模大；
  · 新成立基金按当时跟踪基准 + 净值现算归池，不沿用历史数据。
"""
import numpy as np
import pandas as pd
import pytest

from fundscorer import batch, config as C
from fundscorer.indexfund import index_id, interleave
from fundscorer.screen import base_name, screen_candidates
from fundscorer.scorer import (
    EXCLUDED_POOL,
    classify,
    index_pool,
)


# ---------------------------------------------------------------- 构造数据
def make_nav(days=1400, ann_vol=0.015, start="2019-01-01"):
    """合成净值序列，使近5年年化波动 ≈ ann_vol（√252 换算）。"""
    rng = np.random.default_rng(11)
    dates = pd.bdate_range(start, periods=days)
    daily = rng.normal(0.0002, ann_vol / np.sqrt(252), days)
    nav = (1 + pd.Series(daily)).cumprod()
    return pd.DataFrame({"date": dates, "nav": nav.values})


def rec(code, name, scale=10.0, career=8.0, tenure=2000, nav_years=6.0,
        ret_5y=0.03, mdd_5y=0.004, ret_3y=0.028, mdd_3y=0.003,
        pool="short_bond", type_text="债券型-中短债"):
    return {"code": code, "name": name, "pool": pool, "type_text": type_text,
            "scale_yi": scale, "mgr_career_years": career,
            "mgr_tenure_days": tenure, "nav_years": nav_years,
            "ret_5y": ret_5y, "mdd_5y": mdd_5y, "ret_3y": ret_3y,
            "mdd_3y": mdd_3y, "manager": "张三"}


def yearly_all(v, years=range(2021, 2026)):
    return {y: v for y in years}


def index_records(n=40, career=8.0, tenure=2000, nav_years=6.0):
    records, yearly, spikes = [], {}, {}
    for i in range(n):
        code = f"ix{i:04d}"
        name = f"测试指数{i}债券指数A"
        records.append(rec(code, name, scale=10.0 + i, career=career,
                           tenure=tenure, nav_years=nav_years,
                           ret_5y=0.01 + i * 0.005, mdd_5y=0.05 - i * 0.003,
                           ret_3y=0.01 + i * 0.004, mdd_3y=0.04 - i * 0.002,
                           type_text="指数型-固收"))
        yearly[base_name(name)] = yearly_all(0.01 + i * 0.01)
        spikes[code] = 0.005
    return records, yearly, spikes


def active_res(rows):
    """rows = [(ret_3y, score)] → 模拟 rank_candidates 输出的主动榜。"""
    out = []
    for i, (ret3, score) in enumerate(rows):
        out.append({"rank": i + 1, "code": f"a{i:03d}", "name": f"主动基金{i}",
                    "score": score, "ret_3y": ret3, "ret_5y": ret3,
                    "is_index": False})
    return pd.DataFrame(out)


def idx_rows(rows, meta=None):
    """rows = [(code, name, ret_3y)] → 模拟 rank_candidates 输出的指数候选。"""
    meta = meta or {}
    out = []
    for i, (code, name, ret3) in enumerate(rows):
        out.append({"rank": i + 1, "code": code, "name": name,
                    "score": 60.0, "ret_3y": ret3, "ret_5y": ret3})
    return pd.DataFrame(out), meta


# ================================================================ 债券指数分池（文本层）
class TestIndexPoolText:
    @pytest.mark.parametrize("name,expect", [
        ("华夏中债1-3年政金债指数A", "short_bond"),
        ("南方中债0-2年国开行债券指数A", "short_bond"),   # 0-2年 上限≤3 → 短
        ("汇添富彭博政金债1-3年A", "short_bond"),
        ("招商中证同业存单AAA指数7天持有", "short_bond"),
        ("海富通上清所短融债券C", "short_bond"),
        ("南方中债7-10年国开行债券指数E", "long_bond"),     # 上限≥5 → 长
        ("大成中债3-5年国开债A", "long_bond"),
        ("蜂巢中债1-5年政策性金融债C", "long_bond"),
        ("广发中债农发债总指数A", "long_bond"),
        ("华宝中证中长期纯债指数A", "long_bond"),
    ])
    def test_duration_by_name(self, name, expect):
        assert index_pool(name) == expect

    @pytest.mark.parametrize("name", [
        "可转债ETF博时", "海富通可转债ETF", "某可交换债券指数",
    ])
    def test_convertible_returns_none(self, name):
        # 可转债指数文本层即判 None（classify 中走"无条件排除"，不看波动）
        assert index_pool(name) is None

    @pytest.mark.parametrize("name", ["华夏亚债中国指数A", "京管泰富中债京津冀综合A",
                                      "30年国债ETF鹏扬", "华富中证5年恒定久期国开债指数A"])
    def test_ambiguous_returns_none(self, name):
        assert index_pool(name) is None

    def test_benchmark_takes_priority_over_name(self):
        # 名称判不出时用跟踪基准定案（新成立基金名称常无久期关键词）
        assert index_pool("某某债券指数A",
                          "中债-1-3年政策性金融债指数收益率×95%+银行活期存款利率×5%"
                          ) == "short_bond"
        assert index_pool("某某债券指数A",
                          "中债-7-10年国开行债券指数收益率") == "long_bond"

    def test_benchmark_convertible_excluded(self):
        assert index_pool("某某指数",
                          "中证可转债及可交换债券指数收益率") is None


# ================================================================ classify 指数分支
class TestClassifyIndex:
    def test_text_resolved_pool(self):
        f10 = {"type_text": "指数型-固收", "name": "南方中债7-10年国开行债券指数E",
               "benchmark": ""}
        assert classify(f10, make_nav()) == "long_bond"

    def test_text_unknown_low_vol_to_short(self):
        f10 = {"type_text": "指数型-固收", "name": "华夏亚债中国指数A", "benchmark": ""}
        assert classify(f10, make_nav(ann_vol=0.004)) == "short_bond"

    def test_text_unknown_mid_vol_to_long(self):
        f10 = {"type_text": "指数型-固收", "name": "华夏亚债中国指数A", "benchmark": ""}
        assert classify(f10, make_nav(ann_vol=0.015)) == "long_bond"

    def test_text_unknown_high_vol_excluded(self):
        # 波动超纯债行为带（≥2.5%）：30年超长债/可转债等不入任何池
        f10 = {"type_text": "指数型-固收", "name": "30年国债ETF鹏扬", "benchmark": ""}
        assert classify(f10, make_nav(ann_vol=0.06)) == EXCLUDED_POOL

    def test_insufficient_nav_excluded(self):
        # 新成立基金净值不足半年 → 不下久期结论（等数据够了下期再判）
        f10 = {"type_text": "指数型-固收", "name": "新发中债3年期指数A", "benchmark": ""}
        assert classify(f10, make_nav(days=60)) == EXCLUDED_POOL

    def test_convertible_excluded_even_if_low_vol(self):
        # 可转债指数无条件排除：即便实测波动侥幸低于阈值也不入池
        f10 = {"type_text": "指数型-固收", "name": "可转债ETF博时",
               "benchmark": "中证可转债及可交换债券指数收益率"}
        assert classify(f10, make_nav(ann_vol=0.004)) == EXCLUDED_POOL

    def test_new_fund_rerouted_by_current_benchmark(self):
        # 新成立基金按"当时"基准现算，不沿用历史归池：同名不同时期基准不同 → 归池不同
        f10 = {"type_text": "指数型-固收", "name": "某公司新开债券指数A",
               "benchmark": "中证同业存单AAA指数收益率"}
        assert classify(f10, make_nav()) == "short_bond"
        f10["benchmark"] = "中债-10年期国债财富指数收益率"
        assert classify(f10, make_nav()) == "long_bond"


# ================================================================ 混合一级行为分层
class TestMixedFirstTierSplit:
    def _f10(self):
        return {"type_text": "债券型-混合一级", "name": "某混合一级债券A",
                "benchmark": "中债总指数(全价)"}

    def test_low_vol_to_long_bond(self):
        # 近5年波动 < 固收+下限(2.5%) → 行为即长债，归长债池（用户：相似属性同池）
        assert classify(self._f10(), make_nav(ann_vol=0.015)) == "long_bond"

    def test_high_vol_to_fixed_plus(self):
        assert classify(self._f10(), make_nav(ann_vol=0.05)) == "fixed_plus"

    def test_threshold_follows_config(self):
        # 分层阈值与固定上限 S7 下限同源（不硬编码两套）
        assert classify(self._f10(), make_nav(ann_vol=0.03)) == "fixed_plus"
        thr = C.SCREENS["fixed_plus"]["vol_5y_min"]
        assert thr == 0.025

    def test_insufficient_nav_falls_back_to_fixed_plus(self):
        # 净值不足以判 → 保持 v1.1 既有口径（归固收+），不误塞进长债池
        assert classify(self._f10(), make_nav(days=60)) == "fixed_plus"

    def test_mixed_second_tier_unchanged(self):
        f10 = {"type_text": "债券型-混合二级", "name": "某混合二级债券A",
               "benchmark": ""}
        assert classify(f10, make_nav(ann_vol=0.06)) == "fixed_plus"


# ================================================================ FOF / 类型排除
class TestTypeExclusions:
    def test_fof_never_in_bond_types(self):
        fof = {"FOF-稳健型", "FOF-均衡型", "FOF-进取型"}
        assert fof.isdisjoint(batch.BOND_TYPES)

    def test_excluded_types_all_out_of_bond_types(self):
        # EXCLUDED_TYPES 是显式意图固化：任一被排除类型都不得被收进全集
        for t in batch.EXCLUDED_TYPES:
            assert t not in batch.BOND_TYPES

    def test_bond_index_included(self):
        assert batch.INDEX_TYPE in batch.BOND_TYPES

    def test_expired_guaranteed_excluded(self):
        # 名字像"绝对收益"的陷阱类也被显式排除
        assert "混合型-绝对收益" in batch.EXCLUDED_TYPES
        assert "指数型-其他" in batch.EXCLUDED_TYPES   # 实为黄金/商品期货ETF联接


# ================================================================ 指数穿插排名
class TestInterleave:
    def test_score_interpolates_between_neighbors(self):
        # 主动榜 4 只：ret_3y 1%/2%/3%/4%，分数 70/75/80/85
        act = active_res([(0.01, 70.0), (0.02, 75.0), (0.03, 80.0), (0.04, 85.0)])
        idx, meta = idx_rows([("ix1", "某指数A", 0.025)])
        res, stats = interleave(idx, act, meta, top_n=10)
        row = res[res["is_index"]].iloc[0]
        # 业绩落在 2% 与 3% 正中 → 分数应为 75 与 80 的中点 77.5，名次排在两者之间
        assert row["score"] == pytest.approx(77.5)
        assert row["rank"] == 3
        assert stats["in_list"] == 1

    @pytest.mark.parametrize("ret3,expect_rank", [
        (0.09, 1),     # 业绩高于全部 → 排第 1
        (0.025, 3),    # 恰在中间
    ])
    def test_placement_by_return(self, ret3, expect_rank):
        act = active_res([(0.01, 70.0), (0.02, 75.0), (0.03, 80.0), (0.04, 85.0)])
        idx, meta = idx_rows([("ix1", "某指数A", ret3)])
        res, _ = interleave(idx, act, meta, top_n=10)
        assert int(res[res["is_index"]].iloc[0]["rank"]) == expect_rank

    def test_below_all_active_dropped_when_list_full(self):
        # 业绩低于全部主动榜 → 分数垫底；榜已满 top_n 时落榜（不挤占主动名额）
        act = active_res([(0.01, 70.0), (0.02, 75.0), (0.03, 80.0), (0.04, 85.0)])
        idx, meta = idx_rows([("ix1", "某指数A", 0.005)])
        res, stats = interleave(idx, act, meta, top_n=4)
        assert not res["is_index"].any()
        assert len(res) == 4

    def test_below_all_active_included_when_list_has_room(self):
        act = active_res([(0.01, 70.0), (0.02, 75.0)])
        idx, meta = idx_rows([("ix1", "某指数A", 0.005)])
        res, _ = interleave(idx, act, meta, top_n=5)
        assert res[res["is_index"]].iloc[0]["rank"] == 3

    def test_active_wins_tie_at_boundary(self):
        # 业绩与榜内某只完全持平 → 主动优先：主动保持原名次，指数退到其后一位
        act = active_res([(0.02, 75.0), (0.03, 80.0)])
        idx, meta = idx_rows([("ix1", "某指数A", 0.02)])
        res, _ = interleave(idx, act, meta, top_n=10)
        assert res[res["is_index"]].iloc[0]["rank"] == 3        # 80 → a001#1, 75 → a000#2
        assert int(res[res["code"] == "a000"]["rank"].iloc[0]) == 2   # 主动名次未被挤掉

    def test_no_index_candidates_returns_active_unchanged(self):
        act = active_res([(0.02, 75.0), (0.03, 80.0)])
        res, stats = interleave(pd.DataFrame(), act, {}, top_n=10)
        assert len(res) == 2
        assert stats["candidates"] == 0

    def test_missing_metric_drops_index_fund(self):
        # 业绩缺失（无 3 年数据）的指数基金无法定位 → 不入榜
        act = active_res([(0.02, 75.0), (0.03, 80.0)])
        idx, meta = idx_rows([("ix1", "某指数A", np.nan)])
        res, _ = interleave(idx, act, meta, top_n=10)
        assert not res["is_index"].any()

    def test_extreme_anchor_uses_score_extremes(self):
        # 主动榜按分数排序、业绩与分数**非单调**（回归：实测曾错排第 4 名）。
        # 极端情形无可插值的对端，必须锚"分数极值"而不是"业绩极端那只的分数"。
        # 构造：分数 90/88/86/84 递减，对应业绩 0.02/0.03/0.04/0.05 递增。
        act = active_res([(0.02, 90.0), (0.03, 88.0), (0.04, 86.0), (0.05, 84.0)])
        # 业绩高于全部 → 列首、分数 = 榜内最高分 + ε
        idx_hi, _ = idx_rows([("ix1", "业绩最优指数A", 0.06)])
        r_hi, _ = interleave(idx_hi, act, {}, top_n=10)
        hi = r_hi[r_hi["is_index"]].iloc[0]
        assert hi["rank"] == 1
        assert hi["score"] == pytest.approx(90.0 + 1e-6)
        assert hi["score_basis"]["hi"] is None
        # 业绩低于全部 → 榜末之后、分数 = 榜内最低分 − ε
        idx_lo, _ = idx_rows([("ix2", "业绩最弱指数A", 0.01)])
        r_lo, _ = interleave(idx_lo, act, {}, top_n=10)
        lo = r_lo[r_lo["is_index"]].iloc[0]
        assert lo["rank"] == 5
        assert lo["score"] == pytest.approx(84.0 - 1e-6)
        assert lo["score_basis"]["lo"] is None


class TestIndexIdAndCap:
    def test_index_id_strips_weight_and_cash_leg(self):
        bm = "中债-1-3年政策性金融债指数收益率×95%+银行活期存款利率(税后)×5%"
        assert index_id(bm, "华夏中债1-3年政金债指数A") == "中债-1-3年政策性金融债指数"

    def test_same_index_across_companies_shares_key(self):
        # 不同公司跟踪同一指数 → 同一分组键（因此会被"最多2只"约束）
        k1 = index_id("中债-1-3年国开行债券指数收益率", "汇添富中债1-3年国开债A")
        k2 = index_id("中债-1-3年国开行债券指数收益率×95%+银行活期存款利率×5%",
                      "创金合信中债1-3年国开债C")
        assert k1 == k2

    def test_cap_two_per_index_prefers_low_fee(self):
        # 同一跟踪指数 3 只 → 只留 2，且优先费率低者
        act = active_res([(0.01, 70.0), (0.02, 75.0), (0.03, 80.0), (0.04, 85.0)])
        rows = [("c1", "甲公司短债指数A", 0.025), ("c2", "乙公司短债指数A", 0.026),
                ("c3", "丙公司短债指数A", 0.024)]
        idx, _ = idx_rows(rows)
        bm = "中债-1-3年国开行债券指数收益率"
        meta = {"c1": {"benchmark": bm, "fee_total": 0.0030, "company_scale": 500.0},
                "c2": {"benchmark": bm, "fee_total": 0.0050, "company_scale": 900.0},
                "c3": {"benchmark": bm, "fee_total": 0.0010, "company_scale": 300.0}}
        res, stats = interleave(idx, act, meta, top_n=20)
        kept = set(res[res["is_index"]]["code"])
        assert stats["capped"] == 1
        assert kept == {"c1", "c3"}          # c2 费率最高被挤出

    def test_cap_ties_fee_broken_by_company_scale(self):
        act = active_res([(0.01, 70.0), (0.02, 75.0), (0.03, 80.0), (0.04, 85.0)])
        rows = [("c1", "甲公司短债指数A", 0.025), ("c2", "乙公司短债指数A", 0.026),
                ("c3", "丙公司短债指数A", 0.024)]
        idx, _ = idx_rows(rows)
        bm = "中债-1-3年国开行债券指数收益率"
        meta = {"c1": {"benchmark": bm, "fee_total": 0.0030, "company_scale": 300.0},
                "c2": {"benchmark": bm, "fee_total": 0.0030, "company_scale": 900.0},
                "c3": {"benchmark": bm, "fee_total": 0.0030, "company_scale": 500.0}}
        res, stats = interleave(idx, act, meta, top_n=20)
        kept = set(res[res["is_index"]]["code"])
        assert stats["capped"] == 1
        assert kept == {"c2", "c3"}          # 费率相同 → 公司规模大者优先（900 > 500 > 300）

    def test_different_indexes_not_capped(self):
        act = active_res([(0.01, 70.0), (0.02, 75.0), (0.03, 80.0), (0.04, 85.0)])
        rows = [("c1", "甲公司1-3年指数A", 0.025), ("c2", "乙公司7-10年指数A", 0.026),
                ("c3", "丙公司1-3年指数A", 0.024)]
        idx, _ = idx_rows(rows)
        meta = {"c1": {"benchmark": "中债-1-3年国开行债券指数收益率",
                       "fee_total": 0.0030, "company_scale": 500.0},
                "c2": {"benchmark": "中债-7-10年国开行债券指数收益率",
                       "fee_total": 0.0050, "company_scale": 900.0},
                "c3": {"benchmark": "中债-1-3年国开行债券指数收益率",
                       "fee_total": 0.0010, "company_scale": 300.0}}
        res, stats = interleave(idx, act, meta, top_n=20)
        # 上限按"同指数分组"约束，而非全局限额：c1/c3 同指数 2 只恰好未超上限，
        # c2 是另一指数 → 三只全部保留（对比上例：同指数 3 只才会挤出 1 只）
        assert stats["capped"] == 0
        assert set(res[res["is_index"]]["code"]) == {"c1", "c2", "c3"}


# ================================================================ 指数筛选闸
class TestIndexScreenGates:
    def test_manager_gates_skipped_for_index(self):
        # career 0.5 年 / tenure 100 天：主动口径 S2/S3 全灭，指数口径放行
        records, yearly, spikes = index_records(n=40, career=0.5, tenure=100)
        _, f_active = screen_candidates(records, spikes, yearly,
                                        C.SCREENS["short_bond"])
        assert f_active["S2 经理从业≥5年"] == 0
        _, f_idx = screen_candidates(records, spikes, yearly,
                                     C.SCREENS["short_bond"], index_fund=True)
        assert "S2 经理从业≥5年" not in f_idx
        assert "S3 现经理任本基金≥1.4年" not in f_idx
        assert any(k.startswith("S4") and "3年" in k for k in f_idx)

    def test_index_s4_uses_three_years(self):
        # 3.5 年历史的指数基金：主动口径(5年)挡掉，指数口径(3年)放行
        records, yearly, spikes = index_records(n=40, nav_years=3.5)
        _, f_active = screen_candidates(records, spikes, yearly,
                                        C.SCREENS["short_bond"])
        assert f_active["S4 成立≥5年"] == 0
        _, f_idx = screen_candidates(records, spikes, yearly,
                                     C.SCREENS["short_bond"], index_fund=True)
        assert f_idx["S4 成立≥3年（指数基金口径）"] == 40

    def test_active_s4_still_five_years(self):
        records, yearly, spikes = index_records(n=40, nav_years=3.5)
        _, f = screen_candidates(records, spikes, yearly, C.SCREENS["short_bond"])
        assert f["S4 成立≥5年"] == 0
        assert C.INDEX["min_nav_years"] == 3.0


# ================================================================ 插值锚点（可审计）
class TestScoreBasis:
    """score_basis：卡片须能显示"业绩落在谁和谁之间、分数取这两个分数之间"。

    指数基金显示分 = 插值分，与主动评分卡分项和必然不等（实测最大差 24.7 分）——
    若不展示锚点，详情页"分项贡献分"与卡片分数自相矛盾，用户无从核对。
    """

    def test_basis_brackets_neighbors(self):
        act = active_res([(0.01, 70.0), (0.02, 75.0), (0.03, 80.0), (0.04, 85.0)])
        idx, meta = idx_rows([("ix1", "某指数A", 0.025)])
        res, _ = interleave(idx, act, meta, top_n=10)
        row = res[res["is_index"]].iloc[0]
        b = row["score_basis"]
        assert b["metric"] == C.INDEX["return_metric"] == "ret_3y"
        assert b["ret"] == pytest.approx(0.025)
        assert b["lo"]["ret"] == pytest.approx(0.02) and b["lo"]["score"] == 75.0
        assert b["lo"]["rank"] == 2 and b["lo"]["name"] == "主动基金1"
        assert b["hi"]["ret"] == pytest.approx(0.03) and b["hi"]["score"] == 80.0
        assert b["hi"]["rank"] == 3 and b["hi"]["name"] == "主动基金2"
        assert b["score"] == pytest.approx(row["score"]) == pytest.approx(77.5)
        assert b["lo"]["score"] <= b["score"] <= b["hi"]["score"]   # 分数在两锚点之间

    def test_basis_missing_side_at_extremes(self):
        act = active_res([(0.02, 75.0), (0.03, 80.0)])
        idx1, _ = idx_rows([("c1", "低于全部A", 0.01)])
        r1, _ = interleave(idx1, act, {}, top_n=10)
        b1 = r1[r1["is_index"]].iloc[0]["score_basis"]
        assert b1["lo"] is None and b1["hi"] is not None
        assert b1["score"] == pytest.approx(75.0 - 1e-6)

        idx2, _ = idx_rows([("c2", "高于全部A", 0.09)])
        r2, _ = interleave(idx2, act, {}, top_n=10)
        b2 = r2[r2["is_index"]].iloc[0]["score_basis"]
        assert b2["hi"] is None and b2["lo"] is not None
        assert b2["score"] == pytest.approx(80.0 + 1e-6)

    def test_active_rows_have_no_basis(self):
        act = active_res([(0.02, 75.0), (0.03, 80.0)])
        idx, meta = idx_rows([("c1", "某指数A", 0.025)])
        res, _ = interleave(idx, act, meta, top_n=10)
        actives = res[~res["is_index"]]
        assert len(actives) == 2
        assert actives["score_basis"].isna().all()
        assert res[res["is_index"]].iloc[0]["score_basis"] is not None

    def test_basis_survives_card_contract(self):
        # 卡片侧：主动基金 score_basis=None（非 dict 不透传），指数基金为 dict
        from fundscorer import cards
        row = pd.Series({"a": 1})
        assert cards._opt_dict(row, "score_basis") is None
        assert cards._opt_dict(pd.Series({"score_basis": np.nan}), "score_basis") is None
        d = {"metric": "ret_3y", "ret": 0.03, "score": 80.0, "lo": None, "hi": None}
        assert cards._opt_dict(pd.Series({"score_basis": d}), "score_basis") == d
