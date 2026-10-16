# -*- coding: utf-8 -*-
"""指标层单元测试：不依赖网络，用合成净值序列验证口径正确性。"""
import numpy as np
import pandas as pd
import pytest

from fundscorer import metrics


def make_nav(rets: list[float], start="2020-01-01") -> pd.DataFrame:
    dates = pd.bdate_range(start, periods=len(rets) + 1)
    nav = np.cumprod([1.0] + [1 + r for r in rets])
    return pd.DataFrame({"date": dates[:len(nav)], "nav": nav})


class TestReturns:
    def test_ann_return_exact(self):
        nav = make_nav([0.0001] * 252)
        r = metrics.ann_return(nav, 1)
        # 年化按自然日折算，先算总收益与跨度天数，再年化
        total = nav["nav"].iloc[-1] / nav["nav"].iloc[0] - 1
        days = (nav["date"].iloc[-1] - nav["date"].iloc[0]).days
        assert r == pytest.approx((1 + total) ** (365 / days) - 1, rel=1e-6)

    def test_ann_return_insufficient_uses_full(self):
        nav = make_nav([0.001] * 50)   # 仅 50 天，请求 5 年
        r = metrics.ann_return(nav, 5)
        assert r is not None and r > 0

    def test_ann_return_empty(self):
        nav = make_nav([0.0])[:1]      # 仅 1 行 → 无法计算
        assert metrics.ann_return(nav, 5) is None


class TestDrawdown:
    def test_mdd_known(self):
        # 首日 +10% 到 1.1，随后 30 天每日 -1% → 谷底 1.1×0.99^30
        # 最大回撤 = 1 - 0.99^30
        rets = [0.10] + [-0.01] * 30
        nav = make_nav(rets)
        mdd = metrics.max_drawdown(nav, 5)
        assert mdd == pytest.approx(1 - 0.99 ** 30, rel=1e-6)

    def test_mdd_monotone_positive(self):
        nav = make_nav([0.001] * 100)
        assert metrics.max_drawdown(nav, 5) == pytest.approx(0.0, abs=1e-9)


class TestVolatility:
    def test_zero_vol(self):
        nav = make_nav([0.0] * 100)
        assert metrics.ann_vol(nav, 5) == pytest.approx(0.0, abs=1e-9)

    def test_downside_vol_excludes_gains(self):
        rets = [0.01] * 50 + [-0.01] * 50
        nav = make_nav(rets)
        dvol = metrics.downside_vol(nav, 5)
        # 下行波动只看负收益日，全为 -1% → std=0
        assert dvol == pytest.approx(0.0, abs=1e-9)


class TestRepair:
    def test_no_significant_drawdown(self):
        nav = make_nav([0.001] * 300)
        r = metrics.drawdown_repair(nav, 5, sig_floor=0.03)
        assert r["signif"] is False

    def test_repair_days_after_dip(self):
        # 跌 10% 后连涨回前高：先跌后涨
        rets = [-0.05] * 2 + [0.06] * 10 + [0.001] * 50
        nav = make_nav(rets)
        r = metrics.drawdown_repair(nav, 5, sig_floor=0.03)
        assert r["signif"] is True
        assert r["recovered"] is True
        assert r["repair_days"] is not None and r["repair_days"] > 0

    def test_repair_days_exact_not_one(self):
        # 回归：曾误用窗口起点净值做修复基准 → 修复天数恒为1。正确基准是谷底前高。
        # 场景：+1%×20 → -2%×2 → +0.1%×45。谷底前高 = 1.01^20 = 1.22019，
        # 谷底 = 1.22019×0.98² = 1.17187；需 1.001^k ≥ 1.22019/1.17187 → k ≥ 41。
        rets = [0.01] * 20 + [-0.02] * 2 + [0.001] * 45
        nav = make_nav(rets)
        r = metrics.drawdown_repair(nav, 5, sig_floor=0.03)
        assert r["signif"] is True and r["recovered"] is True
        assert r["repair_days"] > 1, "修复天数不应恒为1（窗口起点≠谷底前高）"
        assert r["repair_days"] == 41

    def test_repair_unrecovered_counts_window(self):
        # 深跌后横盘不修复 → recovered=False，repair_days=谷底至今交易日数
        rets = [0.001] * 30 + [-0.08] * 2 + [0.0005] * 40
        nav = make_nav(rets)
        r = metrics.drawdown_repair(nav, 5, sig_floor=0.03)
        assert r["signif"] is True and r["recovered"] is False
        assert r["repair_days"] == 40


class TestLVI:
    def test_lvi_formula(self):
        # 超额 6%，风险代价 = 0.6×4% + 0.4×2% = 3.2% → LVI ≈ 1.875
        v = metrics.lvi(0.10, 0.04, 0.04, 0.02)
        assert v == pytest.approx(0.06 / (0.6 * 0.04 + 0.4 * 0.02), rel=1e-9)

    def test_lvi_negative_excess(self):
        v = metrics.lvi(0.02, 0.05, 0.03, 0.02)
        assert v < 0

    def test_lvi_zero_risk_returns_none(self):
        assert metrics.lvi(0.05, 0.01, 0.0, 0.0) is None


class TestPercentile:
    def test_higher_better(self):
        s = pd.Series({"a": 1.0, "b": 2.0, "c": 3.0})
        sc = metrics.percentile_score(s, higher_better=True)
        assert sc["c"] > sc["b"] > sc["a"]

    def test_lower_better_inverts(self):
        s = pd.Series({"a": 0.01, "b": 0.05, "c": 0.10})   # 回撤：a 最好
        sc = metrics.percentile_score(s, higher_better=False)
        assert sc["a"] > sc["b"] > sc["c"]

    def test_winsorize_limits_extreme(self):
        s = pd.Series([1.0] * 20 + [1000.0])
        w = metrics.winsorize(s)
        assert w.max() < 1000.0

    def test_nan_preserved(self):
        s = pd.Series({"a": 1.0, "b": np.nan})
        sc = metrics.percentile_score(s, higher_better=True)
        assert np.isnan(sc["b"])


class TestHelpers:
    def test_age_years(self):
        import datetime as dt
        expected = (dt.datetime.now() - dt.datetime(2020, 1, 1)).days / 365.25
        assert metrics.age_years("2020-01-01") == pytest.approx(expected, rel=1e-6)

    def test_age_bad_input(self):
        assert metrics.age_years("") is None
        assert metrics.age_years("not-a-date") is None

    def test_yearly_returns_has_full_years_only(self):
        nav = make_nav([0.001] * 800)   # 约 3 年多
        yr = metrics.yearly_returns(nav, 5)
        # 不完整年份（首年不足20个交易日或无基期）应被剔除
        assert len(yr) >= 2
