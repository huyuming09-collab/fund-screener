# -*- coding: utf-8 -*-
"""评分器单元测试：合成指标数据验证打分方向、红线、星级配额（不依赖网络）。"""
import numpy as np
import pytest

from fundscorer import config as C
from fundscorer.scorer import _assign_stars, _check_redlines, _quality_scores, classify


def make_fund(code, pool="fixed_plus", **kw):
    base = {
        "code": code, "name": f"测试基金{code}", "pool": pool,
        "age": 6.0, "nav_years": 6.0, "mgr_tenure_days": 2000, "mgr_career_years": 9.0,
        "fee_total": 0.007, "ms_rating": 4.0, "inst_ratio": 0.3,
        "scale_yi": 30.0, "yearly": {}, "mgr_products_ann": 0.04,
        "mdd_1y": None, "redlines": [],
    }
    base.update(kw)
    return base


class TestClassify:
    def test_pure_long_bond(self):
        assert classify({"type_text": "债券型-长债", "name": "XX纯债", "benchmark": ""}, None) == "long_bond"

    def test_short_bond_by_name(self):
        assert classify({"type_text": "债券型-短债", "name": "XX短债", "benchmark": ""}, None) == "short_bond"

    def test_mixed_tier1_is_fixed_plus(self):
        # 已确认口径：债券型-混合一级（可转债增强）→ 固收+
        assert classify({"type_text": "债券型-混合一级", "name": "XX增强回报", "benchmark": ""}, None) == "fixed_plus"

    def test_hybrid_with_equity_benchmark(self):
        f = {"type_text": "混合型-偏债", "name": "XX混合", "benchmark": "沪深300*30%+上证国债*70%"}
        assert classify(f, None) == "fixed_plus"


class TestQuality:
    def test_fee_lower_is_better(self):
        cheap = make_fund("a", fee_total=0.004)
        pricey = make_fund("b", fee_total=0.012)
        qa = _quality_scores(cheap, [cheap, pricey])
        qb = _quality_scores(pricey, [cheap, pricey])
        assert qa["fee"] > qb["fee"]

    def test_scale_u_shape(self):
        # 10~100 亿最优，<0.5 亿 0 分
        mid = make_fund("m", scale_yi=50.0)
        tiny = make_fund("t", scale_yi=0.3)
        huge = make_fund("h", scale_yi=800.0)
        peers = [mid, tiny, huge]
        assert _quality_scores(mid, peers)["fund_scale"] == 100
        assert _quality_scores(tiny, peers)["fund_scale"] == 0
        assert _quality_scores(huge, peers)["fund_scale"] < 100

    def test_inst_ratio_penalty_over_90(self):
        healthy = make_fund("x", inst_ratio=0.5)
        crowded = make_fund("y", inst_ratio=0.95)
        peers = [healthy, crowded]
        assert _quality_scores(healthy, peers)["inst_ratio"] == 100
        assert _quality_scores(crowded, peers)["inst_ratio"] == 0

    def test_young_manager_scores_zero(self):
        f = make_fund("n", mgr_tenure_days=100)
        assert _quality_scores(f, [f])["mgr_tenure"] == 0


class TestRedlines:
    def test_new_manager_cap(self):
        f = make_fund("n", mgr_tenure_days=100)
        hits = _check_redlines(f, "fixed_plus")
        assert any("R7-1" in h for h in hits)

    def test_tiny_scale_cap(self):
        f = make_fund("t", scale_yi=0.3)
        hits = _check_redlines(f, "long_bond")
        assert any("R7-2" in h for h in hits)

    def test_deep_unrecovered_cap(self):
        f = make_fund("u", repair_5y={"signif": True, "depth": 0.12, "repair_days": 300, "recovered": False})
        hits = _check_redlines(f, "fixed_plus")
        assert any("R7-5" in h for h in hits)

    def test_clean_fund_no_redline(self):
        f = make_fund("ok", repair_5y={"signif": False})
        assert _check_redlines(f, "fixed_plus") == []


class TestStars:
    def test_high_score_gets_five(self):
        f = make_fund("a", score_capped=90.0)
        _assign_stars([f])
        assert f["star"] == 5

    def test_insufficient_history_no_star(self):
        f = make_fund("b", score_capped=90.0, insufficient_history=True)
        _assign_stars([f])
        assert f["star"] is None

    def test_under_3y_no_star(self):
        f = make_fund("c", score_capped=90.0, nav_years=2.0)
        _assign_stars([f])
        assert f["star"] is None

    def test_quota_applied_for_large_pool(self):
        # 30 只全部 90 分 → 五星配额 10% = 3 只
        funds = [make_fund(str(i), score_capped=90.0, nav_years=6.0) for i in range(30)]
        _assign_stars(funds)
        assert sum(1 for f in funds if f["star"] == 5) == 3

    def test_quota_exempt_for_small_pool(self):
        # 3 只池不启用配额，全部高分应都拿五星
        funds = [make_fund(str(i), score_capped=90.0, nav_years=6.0) for i in range(3)]
        _assign_stars(funds)
        assert all(f["star"] == 5 for f in funds)


class TestWeightsSum:
    def test_pure_bond_sums_100(self):
        assert sum(C.PURE_BOND_WEIGHTS.values()) == 100

    def test_fixed_plus_sums_100(self):
        assert sum(C.FIXED_PLUS_WEIGHTS.values()) == 100
