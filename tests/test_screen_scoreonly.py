# -*- coding: utf-8 -*-
"""筛后评分（score_only）等价性、夏普比率、TOP-N 截断单元测试（无网络）。"""
import copy

import numpy as np
import pandas as pd
import pytest

from fundscorer import config as C
from fundscorer import metrics
from fundscorer.scorer import score_pool


def make_record(i: int, pool="short_bond") -> dict:
    """合成份额指标（覆盖打分所需字段）。"""
    rng = np.random.default_rng(1000 + i)
    yearly = {y: float(rng.normal(0.03, 0.01)) for y in range(2021, 2026)}
    return {
        "code": f"{i:06d}", "name": f"测试短债{i}A", "pool": pool,
        "type_text": "债券型-短债", "manager": f"经理{i % 7}",
        "establish_date": "2018-01-01", "scale_yi": float(rng.uniform(2, 80)),
        "benchmark": "中债",
        "age": float(rng.uniform(5, 9)), "nav_years": float(rng.uniform(5, 9)),
        "history_5y_partial": False, "insufficient_history": False,
        "ret_5y": float(rng.uniform(0.02, 0.05)), "ret_3y": float(rng.uniform(0.02, 0.05)),
        "ret_1y": float(rng.uniform(0.01, 0.04)),
        "mdd_5y": float(rng.uniform(0.001, 0.01)), "mdd_3y": float(rng.uniform(0.001, 0.008)),
        "mdd_1y": float(rng.uniform(0.0005, 0.005)),
        "vol_5y": float(rng.uniform(0.001, 0.004)), "vol_3y": float(rng.uniform(0.001, 0.004)),
        "vol_1y": float(rng.uniform(0.001, 0.004)),
        "dvol_5y": float(rng.uniform(0.001, 0.003)),
        "winrate_m_5y": float(rng.uniform(0.6, 0.95)),
        "repair_5y": {"signif": bool(rng.random() > 0.5), "depth": 0.004,
                      "repair_days": int(rng.integers(5, 100)), "recovered": True},
        "q_win_20q": float(rng.uniform(0.5, 0.9)), "q_excess_20q": float(rng.uniform(0.4, 0.9)),
        "yearly": yearly, "bench_ann_5y": 0.025, "excess_5y": None,
        "lvi_5y": float(rng.uniform(0.5, 4)), "lvi_3y": float(rng.uniform(0.5, 4)),
        "fee_total": float(rng.uniform(0.002, 0.008)), "ms_rating": float(rng.integers(1, 6)),
        "inst_ratio": float(rng.uniform(0.0, 0.6)),
        "mgr_career_years": float(rng.uniform(3, 15)),
        "mgr_products_ann": float(rng.uniform(0.01, 0.06)),
        "mgr_products_n": 5, "mgr_tenure_days": int(rng.integers(400, 4000)),
        "rank_stab": None, "redlines": [],
    }


class TestScoreOnlyEquivalence:
    """score_only 子集打分 ≡ 全量打分（分位基准保留全池）。"""

    def _pool(self, n=40):
        return [make_record(i) for i in range(n)]

    def test_subset_scores_identical(self):
        pool = self._pool()
        full = {r["code"]: r for r in score_pool(copy.deepcopy(pool))}
        subset_codes = {pool[i]["code"] for i in (3, 10, 17, 25, 33)}
        sub = {r["code"]: r for r in
               score_pool(copy.deepcopy(pool), score_only=subset_codes,
                          quota_n=len(pool))}
        for code in subset_codes:
            assert sub[code]["score"] == full[code]["score"], code
            assert sub[code]["score_capped"] == full[code]["score_capped"], code
            assert sub[code]["raw_score"] == full[code]["raw_score"], code
            assert sub[code]["redlines"] == full[code]["redlines"], code
            assert sub[code]["sub_scores"] == full[code]["sub_scores"], code

    def test_subset_stars_identical_with_quota_n(self):
        pool = self._pool(40)
        full = {r["code"]: r for r in score_pool(copy.deepcopy(pool))}
        subset_codes = {c for c, r in full.items()
                        if r.get("score_capped") is not None and r["score_capped"] >= 60}
        assert subset_codes, "合成池应有中高分基金"
        sub = {r["code"]: r for r in
               score_pool(copy.deepcopy(pool), score_only=subset_codes,
                          quota_n=len(pool))}
        for code in subset_codes:
            assert sub[code]["star"] == full[code]["star"], \
                f"{code}: {sub[code].get('star')} != {full[code].get('star')}"

    def test_unscored_members_get_no_star(self):
        pool = self._pool(30)
        subset = {pool[0]["code"]}
        out = score_pool(copy.deepcopy(pool), score_only=subset, quota_n=30)
        for r in out:
            if r["code"] in subset:
                assert r.get("star") is not None
            else:
                assert "star" not in r
                assert r.get("_unscored") is True

    def test_default_quota_n_uses_scored_count(self):
        # 不传 quota_n：配额基数=子集打分数（小池豁免/收缩行为文档化）
        pool = self._pool(10)
        subset = {pool[i]["code"] for i in range(5)}
        out = score_pool(copy.deepcopy(pool), score_only=subset)
        scored = [r for r in out if r.get("score_capped") is not None]
        assert len(scored) == 5


class TestSharpe:
    def test_normal(self):
        assert metrics.sharpe_ratio(0.05, 0.01) == pytest.approx((0.05 - C.SHARPE_RF) / 0.01)

    def test_custom_rf(self):
        assert metrics.sharpe_ratio(0.04, 0.02, rf=0.02) == pytest.approx(1.0)

    def test_zero_vol_returns_none(self):
        assert metrics.sharpe_ratio(0.05, 0.0) is None

    def test_missing_returns_none(self):
        assert metrics.sharpe_ratio(None, 0.01) is None
        assert metrics.sharpe_ratio(0.05, None) is None


class TestStageReturn:
    def _nav(self, n=400):
        dates = pd.bdate_range("2024-01-01", periods=n)
        navs = 1.0 * np.cumprod(1 + np.full(n, 0.0002))
        return pd.DataFrame({"date": dates, "nav": navs})

    def test_1m_window(self):
        df = self._nav(60)
        v = metrics.stage_return(df, "1m")
        assert v is not None and 0 < v < 0.05

    def test_short_history_returns_available_part(self):
        df = self._nav(20)
        assert metrics.stage_return(df, "1y") is not None


class TestTopN:
    def test_rank_candidates_truncates(self):
        from fundscorer.screen import rank_candidates
        rows = []
        for i in range(24):
            rows.append({
                "base": f"基金{i}", "rep_code": f"{i:06d}", "rep_name": f"基金{i}A",
                "src_code": f"{i:06d}", "src_name": f"基金{i}A",
                "codes": [f"{i:06d}"], "names": [f"基金{i}A"],
                "manager": "某经理", "scale_sum": 10.0, "n_classes": 1,
                "career": 8.0, "tenure": 2000, "nav_years": 6.0,
                "ret_1y": 0.03, "ret_5y": 0.03, "mdd_5y": 0.004,
                "ret_3y": 0.03, "mdd_3y": 0.003,
                "vol_1y": 0.002, "vol_5y": 0.002,
                "comp_pct": 0.8, "yearly_med": 0.8, "max_gain_5y": 0.005,
            })
        cands = pd.DataFrame(rows)
        # 分数 = i（越大越好）→ 截取前 20 应是 23..4
        snapshot = {f"{i:06d}": {"score_capped": float(i), "star": 3,
                                "name": f"基金{i}A"} for i in range(24)}
        res = rank_candidates(cands, snapshot, top_n=20)
        assert len(res) == 20
        assert list(res["rank"]) == list(range(1, 21))
        assert res["score"].iloc[0] == 23.0
        assert res["score"].iloc[-1] == 4.0
        # 不截断 = 24
        full = rank_candidates(cands, snapshot)
        assert len(full) == 24

    def test_report_cols_exist_in_result(self):
        from fundscorer.screen import rank_candidates, REPORT_COLS
        row = {
            "base": "基金A", "rep_code": "000001", "rep_name": "基金A",
            "src_code": "000001", "src_name": "基金A",
            "codes": ["000001"], "names": ["基金A"],
            "manager": "某经理", "scale_sum": 10.0, "n_classes": 1,
            "career": 8.0, "tenure": 2000, "nav_years": 6.0,
            "ret_1y": 0.03, "ret_5y": 0.03, "mdd_5y": 0.004,
            "ret_3y": 0.03, "mdd_3y": 0.003,
            "vol_1y": 0.002, "vol_5y": 0.002,
            "comp_pct": 0.8, "yearly_med": 0.8, "max_gain_5y": 0.005,
        }
        res = rank_candidates(pd.DataFrame([row]),
                              {"000001": {"score_capped": 80.0, "star": 4, "name": "基金A"}})
        missing = [c for c in REPORT_COLS if c not in res.columns]
        assert not missing, f"缺列: {missing}"
        assert res["sharpe_1y"].iloc[0] == pytest.approx((0.03 - C.SHARPE_RF) / 0.002)
