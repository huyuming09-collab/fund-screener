# -*- coding: utf-8 -*-
"""指数估值模块（eqindex）单元测试——不依赖网络。

覆盖：基准解析 / 目录别名匹配（最长+排除+歧义） / 分位与档位 / 主口径回落 /
基金筛选与排序 / payload 组装契约 / 准入过滤。
"""
import pandas as pd
import pytest

from fundscorer import config as C
from fundscorer import eqindex as E


# ---------------------------------------------------------------- 工具
def raw_with(pe_vals, pb_vals, pe=None, pb=None, start="2020-01-05", weeks=130):
    """合成雪球估值缓存结构：周频 ts + detail 现值。"""
    dates = pd.date_range(start, periods=weeks, freq="W")
    def hist(key, vals):
        return [{"ts": int(pd.Timestamp(d).timestamp() * 1000), key: v}
                for d, v in zip(dates, vals)]
    return {
        "detail": {"pe": pe if pe is not None else pe_vals[-1],
                   "pb": pb if pb is not None else pb_vals[-1],
                   "yeild": 0.03, "roe": 0.10, "date": "10-29"},
        "pe_hist": hist("pe", pe_vals),
        "pb_hist": hist("pb", pb_vals),
    }


def entry(code):
    return E.CATALOG_BY_CODE[code]


# ---------------------------------------------------------------- 基准解析
class TestParseBenchmark:
    @pytest.mark.parametrize("bench,expect", [
        ("沪深300指数收益率*95%+银行活期存款利率(税后)*5%", "沪深300指数"),
        ("中证白酒指数收益率*95%+金融机构人民币活期存款基准利率(税后)*5%", "中证白酒指数"),
        ("恒生科技指数收益率(使用估值汇率折算)*95%+人民币活期存款收益率(税后)*5%", "恒生科技指数"),
        ("人民币计价的纳斯达克100总收益指数收益率", "纳斯达克100总收益指数"),
        ("95%×中证红利指数收益率+5%×银行活期存款利率(税后)", "中证红利指数"),
        ("沪深300指数收益率×80%+中证综合债指数收益率×20%", "沪深300指数"),
        ("标普500指数收益率(经汇率调整)", "标普500指数"),
        ("中证银行指数收益率×95%＋银行活期存款利率（税后）×5%", "中证银行指数"),
    ])
    def test_extracts_index_component(self, bench, expect):
        comps = E.parse_benchmark(bench)
        assert comps and comps[0] == expect

    def test_cash_only_returns_empty(self):
        assert E.parse_benchmark("银行活期存款利率(税后)") == []

    def test_blank(self):
        assert E.parse_benchmark("") == []
        assert E.parse_benchmark(None) == []


class TestMatchCatalog:
    @pytest.mark.parametrize("text,code", [
        ("沪深300指数", "SH000300"),
        ("中证500指数", "SH000905"),
        ("中证小盘500指数", "SH000905"),
        ("中证500低波动指数", "CSI930782"),
        ("中证500行业中性低波动指数", "CSI930782"),
        ("沪深300价值指数", "SH000919"),
        ("中证红利指数", "SH000922"),
        ("中证红利低波动指数", "CSIH30269"),
        ("中证全指证券公司指数", "SZ399975"),
        ("中证银行指数", "SZ399986"),
        ("中证全指医药卫生指数", "SH000991"),
        ("中证主要消费指数", "SH000932"),
        ("中证A100指数", "SH000903"),
        ("创业板指数", "SZ399006"),
        ("科创50指数", "SH000688"),
        ("纳斯达克100指数", "NDX"),
        ("恒生科技指数", "HKHSTECH"),
        ("标普500指数", "SP500"),
        ("5G通信主题指数", "CSI931079"),
        ("国证新能源车指数", "SZ399417"),
        ("国证房地产指数", "SZ399393"),
    ])
    def test_alias_hits(self, text, code):
        got, alias = E.match_catalog(text)
        assert got == code, (text, got, alias)

    @pytest.mark.parametrize("text", [
        "中证500质量成长指数",       # 未收录变种 → 基名被 exclude 挡住
        "创业板50指数",              # 不是创业板指
        "中证红利低波动100指数",     # 红利低波被 100 排除
        "中证食品饮料指数",          # 目录只有国证食品/主要消费
        "中证半导体产业指数",        # 不在 63 目录
        "",
    ])
    def test_unmatched(self, text):
        assert E.match_catalog(text)[0] is None

    def test_ambiguity_rejected(self):
        # 人为构造两个同长别名同时命中 → 歧义返回 None
        old = E.CATALOG
        try:
            E.CATALOG = [
                E._cat("X1", "甲", "宽基", "宽基", "pe", ["测试指数"]),
                E._cat("X2", "乙", "宽基", "宽基", "pe", ["测试指数"]),
            ]
            assert E.match_catalog("测试指数")[0] is None
        finally:
            E.CATALOG = old

    def test_benchmark_multi_component_picks_longest(self):
        code, comp, alias = E.match_benchmark(
            "沪深300价值指数收益率×70%+中证综合债指数收益率×30%")
        assert code == "SH000919"
        assert comp == "沪深300价值指数"

    def test_price_index_variant(self):
        # 深证红利价格指数 → 走 clean_component 归一后命中深证红利
        code, comp, _ = E.match_benchmark("深证红利价格指数收益率×95%+银行活期存款税后收益率×5%")
        assert code == "SZ399324" and comp == "深证红利指数"


# ---------------------------------------------------------------- 分位 / 档位
class TestPercentile:
    def test_current_top(self):
        vals = list(range(1, 101))
        dates = pd.date_range("2025-01-05", periods=100, freq="W")
        out = E.percentile_of(vals, dates, current=100)
        assert out["pct"] == 100.0 and out["weeks"] == 100
        assert out["min"] == 1 and out["max"] == 100

    def test_current_mid(self):
        vals = list(range(1, 101))
        dates = pd.date_range("2025-01-05", periods=100, freq="W")
        assert E.percentile_of(vals, dates, current=50)["pct"] == 50.0

    def test_window_trims_old_points(self):
        dates = pd.date_range("2015-01-04", periods=500, freq="W")
        vals = list(range(500))
        out = E.percentile_of(vals, dates, current=500)
        assert out["weeks"] < 500
        cutoff = (dates[-1] - pd.DateOffset(years=5)).strftime("%Y-%m-%d")
        assert out["window_start"] >= cutoff

    def test_min_weeks_flags(self):
        dates = pd.date_range("2023-01-01", periods=30, freq="W")
        out = E.percentile_of(list(range(30)), dates, current=29)
        assert out["enough_for_tier"] is True
        assert out["sufficient"] is False

    def test_too_few_for_tier(self):
        dates = pd.date_range("2025-06-01", periods=10, freq="W")
        out = E.percentile_of(list(range(10)), dates, current=9)
        assert out["enough_for_tier"] is False

    def test_zero_and_nan_dropped(self):
        dates = pd.date_range("2024-01-07", periods=6, freq="W")
        out = E.percentile_of([0, 1, None, 3, 4, 5], dates, current=5)
        assert out["weeks"] == 4

    def test_empty(self):
        assert E.percentile_of([], [])["pct"] is None


class TestTier:
    @pytest.mark.parametrize("pct,tier", [
        (0, "极低估"), (20, "极低估"), (20.1, "低估"), (40, "低估"),
        (40.1, "合理偏低"), (60, "合理偏低"), (60.1, "合理偏高"),
        (80, "合理偏高"), (80.1, "高估"), (100, "高估"),
    ])
    def test_boundaries(self, pct, tier):
        assert E.tier_of(pct) == tier

    def test_none(self):
        assert E.tier_of(None) is None


# ---------------------------------------------------------------- 主口径回落
class TestMetric:
    def test_pe_primary(self):
        raw = raw_with([10 + i * 0.1 for i in range(130)],
                       [1 + i * 0.01 for i in range(130)])
        v = E.build_index_valuation(entry("SH000300"), raw)
        assert v["metric"] == "pe" and v["tier"] in (
            "极低估", "低估", "合理偏低", "合理偏高", "高估")

    def test_pe_invalid_falls_back_to_pb(self):
        raw = raw_with([0] * 130, [1 + i * 0.01 for i in range(130)])
        v = E.build_index_valuation(entry("SH000300"), raw)
        assert v["metric"] == "pb"
        assert "PE" in v["why"] and "PB" in v["why"]

    def test_pb_primary_for_bank(self):
        raw = raw_with([5 + i * 0.01 for i in range(130)],
                       [0.5 + i * 0.001 for i in range(130)])
        v = E.build_index_valuation(entry("SZ399986"), raw)
        assert v["metric"] == "pb"
        assert v["tier"] == "高估"

    def test_too_short_history_returns_none(self):
        raw = raw_with([1] * 12, [1] * 12, weeks=12)
        raw["pe_hist"] = raw["pe_hist"][:4]
        raw["pb_hist"] = raw["pb_hist"][:4]
        assert E.build_index_valuation(entry("SH000300"), raw) is None

    def test_missing_raw(self):
        assert E.build_index_valuation(entry("SH000300"), None) is None
        assert E.build_index_valuation(entry("SH000300"), {}) is None

    def test_gap_flag(self):
        # PE 处于窗口低位（约 0 分位）、PB 处于高位（100 分位）→ 分位差 ≥30
        pe = [20 - i * 0.05 for i in range(130)]
        pb = [1 + i * 0.01 for i in range(130)]
        raw = raw_with(pe, pb, start="2020-01-05", weeks=130)
        v = E.build_index_valuation(entry("SH000300"), raw)
        assert v["pe_pb_gap"] is not None and v["pe_pb_gap"] >= 30


class TestSeries:
    def test_monthly_capped(self):
        dates = pd.date_range("2016-01-03", periods=520, freq="W")
        vals = [1 + i * 0.01 for i in range(520)]
        out = E._monthly_series(vals, dates, points=60)
        assert 1 < len(out) <= 60
        assert all(len(p[0]) == 7 for p in out)


# ---------------------------------------------------------------- 准入 / 基金筛选
class TestEligibility:
    @pytest.mark.parametrize("name,ptype,ok", [
        ("天弘沪深300ETF联接A", "指数型-股票", True),
        ("华夏沪深300ETF", "指数型-股票", False),
        ("富国沪深300指数增强A", "指数型-股票", False),
        ("华夏纳斯达克100ETF联接A", "指数型-海外股票", True),
        ("某某黄金ETF联接A", "指数型-其他", False),
        ("某某债券指数A", "指数型-固收", False),
    ])
    def test_filters(self, name, ptype, ok):
        assert E.eligible_fund(name, ptype) is ok

    @pytest.mark.parametrize("name,cls", [
        ("天弘沪深300ETF联接A", "A"),
        ("招商中证白酒指数(LOF)C", "C"),
        ("某某指数A(后端)", "A"),
        ("某某指数", ""),
    ])
    def test_class(self, name, cls):
        assert E.fund_class(name) == cls


class TestSelectFunds:
    def _rec(self, code, fee, name="基金A"):
        return {"code": code, "name": name, "cls": "A", "fee_total": fee,
                "mgmt_fee": fee, "custody_fee": 0.0, "sales_fee": 0.0,
                "purchase_fee": 0.0, "purchase_fee_disc": None,
                "scale_yi": 10.0}

    def test_fee_asc_and_limit(self):
        recs = [self._rec("1", 0.005), self._rec("2", 0.002), self._rec("3", 0.003),
                self._rec("4", 0.004), self._rec("5", 0.001), self._rec("6", 0.006)]
        out = E.select_funds(recs, {}, {}, {}, {}, 5)
        assert [x["code"] for x in out] == ["5", "2", "3", "4", "1"]

    def test_suspended_last(self):
        recs = [self._rec("1", 0.001), self._rec("2", 0.002)]
        out = E.select_funds(recs, {"1": "暂停申购", "2": "开放申购"}, {}, {}, {}, 5)
        assert [x["code"] for x in out] == ["2", "1"]
        assert out[1]["suspended"] is True

    def test_missing_fee_dropped(self):
        recs = [self._rec("1", None), self._rec("2", 0.002)]
        out = E.select_funds(recs, {}, {}, {}, {}, 5)
        assert [x["code"] for x in out] == ["2"]

    def test_returns_and_company_injected(self):
        recs = [self._rec("1", 0.002)]
        out = E.select_funds(recs, {}, {"1": {"ret_1y": 0.1, "ret_3y": 0.2}},
                             {"1": "公司A"}, {"公司A": 500.0}, 5)
        assert out[0]["ret_1y"] == 0.1
        assert out[0]["company_scale"] == 500.0


# ---------------------------------------------------------------- build() 契约
class TestBuildPayload:
    def _fund(self, code, idx, fee, name=None):
        return {"code": code, "name": name or ("基金" + code), "type": "指数型-股票",
                "benchmark": "x", "idx_code": idx, "cls": "A", "fee_total": fee,
                "mgmt_fee": fee, "custody_fee": 0.0, "sales_fee": 0.0,
                "purchase_fee": 0.0, "purchase_fee_disc": None, "scale_yi": 10.0}

    def test_contract(self, monkeypatch):
        funds = [self._fund("000001", "SH000300", 0.002),
                 self._fund("000002", "SH000300", 0.003),
                 self._fund("000003", "SZ399986", 0.001)]
        monkeypatch.setattr(E, "_load_fund_cache",
                            lambda: {"fetched_at": "2026-10-29", "funds": funds})
        monkeypatch.setattr(E, "fund_returns", lambda refresh: {})
        monkeypatch.setattr(E, "load_valuation",
                            lambda code, refresh: raw_with(
                                [10 + i * 0.1 for i in range(130)],
                                [1 + i * 0.01 for i in range(130)]))
        def fake_cs(code, refresh):
            dates = pd.date_range("2020-01-05", periods=300, freq="W")
            return {"publish": "2019-01-01",
                    "pe": [[d.strftime("%Y%m%d"), 10 + i * 0.02] for i, d in enumerate(dates)],
                    "close": [[d.strftime("%Y%m%d"), 100 + i * 0.1] for i, d in enumerate(dates)]}
        monkeypatch.setattr(E, "load_cs", fake_cs)
        monkeypatch.setattr(E.D, "fetch_purchase_status",
                            lambda codes, refresh=False: {})
        out = E.build(log=lambda *a, **k: None, refresh=False)
        assert out["catalog_n"] == len(E.CATALOG)
        assert out["version"] == C.EQ_INDEX["version"]
        assert out["hot_per_group"] == C.EQ_INDEX["hot_per_group"]
        # v1.3：目录全量进 payload（含无匹配基金者，fund_count=0）
        assert len(out["indices"]) == len(E.CATALOG)
        codes = {x["code"] for x in out["indices"]}
        assert {"SH000300", "SZ399986"} <= codes
        for x in out["indices"]:
            assert x["tier"] in ("极低估", "低估", "合理偏低", "合理偏高", "高估")
            assert 0 <= x["percentile"] <= 100
            assert 0 <= len(x["funds"]) <= C.EQ_INDEX["fund_list_n"]
            assert isinstance(x["mainstream"], bool)
            assert "action" not in x      # v1.2 删除定投倍数
            assert isinstance(x["series"], dict) and "pe" in x["series"]
            assert set(x["windows"]) == {"3y", "5y", "10y", "all"}
        # 有匹配基金的条目：基金清单非空且 fund_count 一致
        h = [x for x in out["indices"] if x["code"] == "SH000300"][0]
        assert h["fund_count"] == 2 and len(h["funds"]) == 2
        assert h["method_label"] and h["history_label"]

    def test_no_cache_returns_none(self, monkeypatch):
        monkeypatch.setattr(E, "_load_fund_cache", lambda: {})
        assert E.build(log=lambda *a, **k: None, refresh=False) is None


# ---------------------------------------------------------------- v1.2：目录分流（策略 vs 红利）
def test_catalog_strategy_vs_dividend():
    """非红利策略（价值/基本面/低波）上「策略」tab；纯红利/价值留待「红利类」页签。"""
    strat = {"SH000919", "SH000925", "SZ399701", "SZ399702", "CSI930782", "SPCQVCP"}
    div = {"SH000922", "SH000015", "SZ399324", "CSIH30269", "CSI931157",
           "CSI930740", "CSIH30094", "CSPSADRP", "SPACEVCP"}
    for c in strat:
        assert E.CATALOG_BY_CODE[c]["mainstream"] is True, c
        assert E.CATALOG_BY_CODE[c]["defer"] is False, c
    for c in div:
        assert E.CATALOG_BY_CODE[c]["defer"] is True, c


def test_group_order_has_strategy():
    assert E.GROUP_ORDER == ["宽基", "行业", "策略", "海外"]


def test_strategy_default_metric_is_pb():
    """价值/低波/基本面策略用 PB 为主（盈利波动大，PB 更贴近资产端）。"""
    for c in ("SH000919", "SH000925", "CSI930782"):
        assert E.CATALOG_BY_CODE[c]["default_metric"] == "pb", c


def test_bond_pools_untouched():
    assert C.PIPELINE["pools"] == ["short_bond", "long_bond", "fixed_plus"]
    assert "eq_index" not in C.PIPELINE["pools"]
    assert C.POOL_VERSIONS["index"] == "v5.19"
