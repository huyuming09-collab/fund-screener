# -*- coding: utf-8 -*-
"""评分器单元测试：合成指标数据验证打分方向、红线、星级配额（不依赖网络）。"""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from fundscorer import config as C
from fundscorer.scorer import _assign_stars, _check_redlines, _manager_qualified, _quality_scores, classify


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


class TestManagerQualified:
    """R3 经理资质（固收+回撤容差调节）：阈值语义 = 被超过比例，2026-10-19 方向修复回归。"""

    def test_elite_every_year_top_half_and_avg_top_third(self):
        # 每年收益分位 0.7/0.8/0.9 → 被超过 0.3/0.2/0.1，全过且均值 0.2 ≤ 1/3
        assert _manager_qualified({"_stab_ranks": [0.7, 0.8, 0.9]}, []) is True

    def test_consistently_poor_not_qualified(self):
        # 常年垫底（分位 0.3/0.4/0.45）——方向反转 bug 下会误判合格，此为回归锚
        assert _manager_qualified({"_stab_ranks": [0.3, 0.4, 0.45]}, []) is False

    def test_one_bad_year_fails_every_year_condition(self):
        # 任一年跌出前半（0.4 → 被超过 0.6 > 0.5）即不合格
        assert _manager_qualified({"_stab_ranks": [0.9, 0.9, 0.4]}, []) is False

    def test_ok_years_but_weak_average_fails(self):
        # 每年都前半（0.6 → 被超过 0.4）但均值 0.4 > 1/3 → 不合格
        assert _manager_qualified({"_stab_ranks": [0.6, 0.6, 0.6]}, []) is False

    def test_insufficient_years_not_qualified(self):
        assert _manager_qualified({"_stab_ranks": [0.9, 0.9, None]}, []) is False
        assert _manager_qualified({"_stab_ranks": []}, []) is False


class TestRiskRatios:
    """v2 试验（2026-10-18）：风险调整收益派生字段 calmar_5y / sharpe_5y。"""

    def test_derivation(self):
        from fundscorer.scorer import _risk_ratios
        f = {"ret_5y": 0.06, "mdd_5y": 0.04, "vol_5y": 0.03}
        _risk_ratios(f)
        assert f["calmar_5y"] == pytest.approx(1.5)                    # 6%/4%
        assert f["sharpe_5y"] == pytest.approx((0.06 - 0.015) / 0.03)  # (ret−rf)/vol

    def test_missing_or_zero_denominator_is_none(self):
        from fundscorer.scorer import _risk_ratios
        f = {"ret_5y": None, "mdd_5y": 0.0, "vol_5y": None}
        _risk_ratios(f)
        assert f["calmar_5y"] is None and f["sharpe_5y"] is None
        g = {"ret_5y": float("nan"), "mdd_5y": 0.04, "vol_5y": 0.03}
        _risk_ratios(g)
        assert g["calmar_5y"] is None and g["sharpe_5y"] is None

    def test_ratio_metrics_registered_as_percentile(self):
        # 权重表引用的派生因子必须在 PERCENTILE_METRICS 注册（否则分位分不生效）
        for k in ("calmar_5y", "sharpe_5y"):
            assert C.PERCENTILE_METRICS[k] == "higher"
        # v6（docs/12，11 截面双段验证）：比率族同族择优——夏普与卡玛 ρ=0.902 冗余，
        # 并入卡玛（卡玛为唯一代表）；族总权重不变（15+10 → 25+0），只换族内代表
        assert C.FIXED_PLUS_WEIGHTS["calmar_5y"] == 25
        assert C.FIXED_PLUS_WEIGHTS["sharpe_5y"] == 0
        assert C.FIXED_PLUS_WEIGHTS["calmar_5y"] + C.FIXED_PLUS_WEIGHTS["sharpe_5y"] == 25


class TestMultiManager:
    """v5.5 共管规则（docs/12 §9）：主责=任本基金最久；共管满3年逐项取 max；不足按主责。"""

    @staticmethod
    def _patch(monkeypatch, careers, products):
        from fundscorer import data as D
        monkeypatch.setattr(D, "fetch_manager_career_years",
                            lambda name, code=None: careers.get(name))
        monkeypatch.setattr(D, "fetch_manager_products_ann", lambda mid: products.get(mid, []))

    def test_core_managers_take_max(self, monkeypatch):
        from fundscorer.scorer import _manager_metrics
        self._patch(monkeypatch, careers={"甲": 12.0, "乙": 6.0},
                    products={"1": [0.05], "2": [0.08, 0.09]})
        f10 = {"managers": [{"name": "甲", "id": "1", "start": "2019-06-01"},
                            {"name": "乙", "id": "2", "start": "2022-01-01"}]}
        out = _manager_metrics(f10, "000001")
        assert out["manager"] == "甲"                          # 主责 = 任期最久
        assert out["mgr_tenure_days"] == (dt.date.today() - dt.date(2019, 6, 1)).days
        assert out["mgr_career_years"] == 12.0                 # 共管满3年 → 从业取 max
        assert out["mgr_products_ann"] == pytest.approx(0.085)  # 全产品年化取 max（中位0.085）
        assert out["mgr_products_n"] == 2
        assert [x["name"] for x in out["managers"]] == ["甲", "乙"]

    def test_young_core_falls_back_to_primary(self, monkeypatch):
        # 全部共管<3年 → 按主责计分；各自数据各归各人 → 新任<1年不背前任战绩
        from fundscorer.scorer import _manager_metrics
        self._patch(monkeypatch, careers={"甲": 12.0, "乙": 6.0},
                    products={"1": [0.06], "2": [0.02]})
        f10 = {"managers": [{"name": "甲", "id": "1", "start": "2026-09-01"},
                            {"name": "乙", "id": "2", "start": "2026-05-01"}]}
        out = _manager_metrics(f10, "000001")
        assert out["manager"] == "乙"                          # 主责 = 任期最长（仍<3年）
        assert out["mgr_career_years"] == 6.0                  # 只按主责，不取甲的 12
        assert out["mgr_products_ann"] == pytest.approx(0.02)   # 甲的 0.06 不进入

    def test_fallback_without_managers_list(self, monkeypatch):
        from fundscorer.scorer import _manager_metrics
        self._patch(monkeypatch, careers={"丙": 9.0}, products={"9": [0.04]})
        f10 = {"managers": [], "manager": "丙", "manager_id": "9",
               "manager_start": "2020-01-01"}
        out = _manager_metrics(f10, "000001")
        assert out["manager"] == "丙" and out["managers"] == []   # 旧缓存单经理回退
        assert out["mgr_career_years"] == 9.0
        assert out["mgr_products_ann"] == pytest.approx(0.04)
        assert out["mgr_tenure_days"] == (dt.date.today() - dt.date(2020, 1, 1)).days


class TestTenureCalmar:
    """经理本基金任期卡玛：展示用（不计分），任期窗口年化 ÷ max(任期回撤, 0.5%)。"""

    @staticmethod
    def _nav():
        dates = pd.date_range("2020-01-31", periods=80, freq="ME")
        return pd.DataFrame({"date": dates, "nav": 1.0015 ** np.arange(80)})

    def test_annual_over_floor(self):
        from fundscorer.scorer import _tenure_calmar
        nav = self._nav()
        f10 = {"managers": [{"name": "甲", "id": "1", "start": "2021-01-01"}]}
        v = _tenure_calmar(nav, f10, {"manager": "甲"})
        sub = nav[nav["date"] >= pd.Timestamp("2021-01-01")]
        days = (sub["date"].iloc[-1] - sub["date"].iloc[0]).days
        ann = (sub["nav"].iloc[-1] / sub["nav"].iloc[0]) ** (365.25 / days) - 1
        assert v == pytest.approx(ann / 0.005, rel=1e-6)   # 单调上涨 → 任期回撤0 → 走地板

    def test_none_when_short_or_missing(self):
        from fundscorer.scorer import _tenure_calmar
        nav = self._nav()
        recent = (dt.date.today() - dt.timedelta(days=100)).isoformat()
        assert _tenure_calmar(nav, {"managers": [{"name": "甲", "id": "1", "start": recent}]},
                              {"manager": "甲"}) is None      # 任期<1年不出数
        assert _tenure_calmar(nav, {"managers": []}, {"manager": "甲"}) is None  # 无起点
        assert _tenure_calmar(None, {"managers": []}, {"manager": "甲"}) is None
