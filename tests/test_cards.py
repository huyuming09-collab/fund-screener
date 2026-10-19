# -*- coding: utf-8 -*-
"""cards.py（卡片数据契约）+ pipeline 环比历史 的单元测试。

覆盖：契约字段齐全且无星级、费率组件透传、环比三字段（rank_prev/rank_delta/is_new）、
NaN/inf 守卫、trend 瘦身后窗口（仅 1y+5y）、原子写出与 data.js 紧凑格式。
"""
import json

import numpy as np
import pandas as pd
import pytest

from fundscorer import cards, pipeline


def _nav(rows=500, start="2022-01-01"):
    dates = pd.bdate_range(start, periods=rows)
    return pd.DataFrame({"date": dates,
                         "nav": np.linspace(1.0, 1.15, rows)})


def _rec(**over):
    rec = {
        "code": "002438", "name": "创金合信尊盛纯债A", "pool": "short_bond",
        "ret_1y": 0.021, "ret_3y": 0.025, "ret_5y": 0.028,
        "mdd_1y": -0.003, "mdd_3y": -0.008, "mdd_5y": -0.012,
        "vol_1y": 0.0021, "vol_5y": 0.0035,
        "lvi_5y": 1.2, "lvi_3y": 1.1,
        "winrate_m_5y": 0.62, "q_win_20q": 0.55, "q_excess_20q": 0.58,
        "excess_5y": 0.004, "bench_ann_5y": 0.023,
        "yearly": {2022: 0.021, 2023: 0.027, 2024: 0.030, 2025: 0.026, 2026: 0.019},
        "repair_5y": {"recovered": True, "repair_days": 45, "signif": True},
        "establish_date": "2016-03-01", "age": 10.6,
        "ms_rating": 3.0, "inst_ratio": 0.31,
        "benchmark": "中债-综合全价(1-3年)指数收益率*80%+税后一年期存款利率*20%",
        "fee_total": 0.0032,
        "mgmt_fee": 0.0025, "custody_fee": 0.0007, "sales_fee": 0.0,
        "purchase_fee": 0.008, "purchase_fee_disc": 0.0008,
        "manager": "张某", "career": 11.4, "tenure": 1825,
        "nav_years": 4.8, "mgr_products_ann": 0.031, "mgr_products_n": 6,
        "score_capped": 86.83, "star": 5,
    }
    rec.update(over)
    return rec


def _row(**over):
    row = {
        "rank": 1, "code": "002438", "name": "创金合信尊盛纯债A",
        "base": "002438", "src_code": "002438", "category": "short_bond",
        "score": 86.83, "scale_sum": 9.81, "n_classes": 3,
        "nav_years": 4.8, "manager": "张某", "career": 11.4, "tenure": 1825,
        "comp_pct": 0.87, "yearly_med": 0.91, "max_gain_5y": 0.0031,
        "rank_prev": None, "rank_delta": None, "is_new": False,
    }
    row.update(over)
    return pd.Series(row)


def _payload(prev_ranks=None, n_screened=25):
    res = pd.DataFrame([_row()])
    nav = _nav()
    return cards.build_payload(res, {"002438": _rec()}, {"002438": nav},
                               funnel={"P2": 25}, prev_ranks=prev_ranks,
                               n_screened=n_screened)


class TestIsIndexConcatSafety:
    """回归：pd.concat 对缺 is_index 列的行补 np.nan，而 bool(np.nan) is True。

    2026-10-19 实跑发现：固收+池无指数基金（res 不带该列），与两债池 concat 后
    该池 20 只主动基金被整体误标成 is_index=True。此处锁死两个防御层。
    """

    def test_flag_treats_missing_and_nan_as_false(self):
        assert cards._flag(pd.Series({"a": 1}), "is_index") is False   # 缺列
        assert cards._flag(pd.Series({"is_index": True}), "is_index") is True
        assert cards._flag(pd.Series({"is_index": False}), "is_index") is False
        assert cards._flag(pd.Series({"is_index": np.nan}), "is_index") is False  # bool(nan)=True
        assert cards._flag(pd.Series({"is_new": np.nan}), "is_new") is False

    def test_pool_without_index_funds_not_polluted(self):
        idx_row = _row(rank=4, code="110007", category="short_bond", is_index=True)
        act_row = _row(rank=1, code="002438", category="short_bond", is_index=False)
        fp_row = _row(rank=1, code="000001", category="fixed_plus")     # 缺 is_index 列
        combined = pd.concat([pd.DataFrame([idx_row, act_row]),
                              pd.DataFrame([fp_row])], ignore_index=True)
        assert combined["is_index"].isna().sum() == 1                    # concat 补出的 NaN

        snap = {"002438": _rec(), "110007": _rec(code="110007"),
                "000001": _rec(code="000001", pool="fixed_plus")}
        navs = {k: _nav() for k in snap}
        stats = {p: {"n": 1, "n_screened": 25, "top_n": 20, "has_prev": False,
                     "funnel": {}, "screen": {}}
                 for p in ("short_bond", "fixed_plus")}
        p = cards.build_payload(combined, snap, navs, pool_stats=stats)
        by_code = {f["code"]: f for f in p["funds"]}
        assert by_code["110007"]["is_index"] is True
        assert by_code["002438"]["is_index"] is False
        assert by_code["000001"]["is_index"] is False   # 关键：缺列池不得被误标


class TestContract:
    def test_payload_top_keys(self):
        p = _payload(n_screened=25)
        for k in ("model_version", "asof", "category", "top_n", "n",
                  "n_screened", "has_prev", "sharpe_rf", "categories",
                  "funnel", "funds", "disclaimer"):
            assert k in p
        assert p["n"] == 1 and p["n_screened"] == 25

    def test_fund_keys_and_no_star(self):
        f = _payload()["funds"][0]
        for k in ("rank", "code", "name", "base", "category", "score",
                  "rank_prev", "rank_delta", "is_new",
                  "metrics", "stages", "yearly", "manager", "fund_info",
                  "screening", "score_detail", "sub_scores", "redlines", "trend"):
            assert k in f, k
        # 用户口径：卡片契约任何层级都不得出现星级
        assert "star" not in f
        assert "star" not in json.dumps(f, ensure_ascii=False)

    def test_fee_fields_passthrough(self):
        fi = _payload()["funds"][0]["fund_info"]
        assert fi["mgmt_fee"] == pytest.approx(0.0025)
        assert fi["purchase_fee"] == pytest.approx(0.008)
        assert fi["purchase_fee_disc"] == pytest.approx(0.0008)
        assert fi["custody_fee"] == pytest.approx(0.0007)

    def test_manager_block_median_and_products_n(self):
        """经理口径透明化：指标为**中位数**且带计入产品数（docs/01 §5.5）。"""
        mg = _payload()["funds"][0]["manager"]
        assert mg["products_ann"] == pytest.approx(0.031)
        assert mg["products_n"] == 6
        rec = _rec(mgr_products_ann=None, mgr_products_n=None)
        mg2 = cards.build_payload(pd.DataFrame([_row()]), {"002438": rec},
                                  {"002438": _nav()})["funds"][0]["manager"]
        assert mg2["products_ann"] is None and mg2["products_n"] is None

    def test_fee_fields_missing(self):
        rec = _rec(mgmt_fee=None, purchase_fee=None, purchase_fee_disc=None,
                   custody_fee=None, sales_fee=None)
        p = cards.build_payload(pd.DataFrame([_row()]), {"002438": rec},
                                {"002438": _nav()})
        fi = p["funds"][0]["fund_info"]
        assert fi["mgmt_fee"] is None and fi["purchase_fee"] is None
        assert fi["purchase_fee_disc"] is None

    def test_disclaimer_present(self):
        assert "不构成任何投资建议" in _payload()["disclaimer"]


class TestNaNGuards:
    def test_nan_inf_to_none(self):
        rec = _rec(ret_1y=np.nan, mdd_5y=float("-inf"), lvi_5y=float("inf"),
                   ms_rating=np.nan)
        p = cards.build_payload(pd.DataFrame([_row(score=np.nan)]),
                                {"002438": rec}, {"002438": _nav()})
        f = p["funds"][0]
        assert f["score"] is None
        assert f["metrics"]["ret_1y"] is None
        assert f["metrics"]["mdd_5y"] is None
        assert f["metrics"]["lvi_5y"] is None
        assert f["fund_info"]["ms_rating"] is None
        json.dumps(p)   # 必须可序列化


class TestMultiPoolContract:
    """多池（docs/09 §D）：funds 逐只 category、pools 统计映射、顶层取主池。"""

    @staticmethod
    def _payload():
        res = pd.DataFrame([
            _row(rank=1, code="002438", base="002438", category="short_bond"),
            _row(rank=1, code="900001", name="中长债优选债券A", base="中长债优选债券",
                 category="long_bond"),
        ])
        snap = {"002438": _rec(),
                "900001": _rec(code="900001", name="中长债优选债券A", pool="long_bond")}
        navs = {"002438": _nav(), "900001": _nav()}
        stats = {
            "short_bond": {"n": 20, "n_screened": 23, "top_n": 20, "has_prev": True,
                           "funnel": {"合并基金数": 482},
                           "screen": {"scale_min_yi": 5, "comp_pct_min": 0.45,
                                      "yearly_median_min": 0.55, "spike_max_gain": 0.01}},
            "long_bond": {"n": 20, "n_screened": 74, "top_n": 20, "has_prev": False,
                          "funnel": {"合并基金数": 945},
                          "screen": {"scale_min_yi": 5, "comp_pct_min": 0.45,
                                     "yearly_median_min": 0.55, "spike_max_gain": 0.02}},
        }
        return cards.build_payload(res, snap, navs, pool_stats=stats)

    def test_funds_carry_category(self):
        p = self._payload()
        assert [f["category"] for f in p["funds"]] == ["short_bond", "long_bond"]

    def test_pools_map_per_pool_stats(self):
        p = self._payload()
        assert set(p["pools"]) == {"short_bond", "long_bond"}
        assert p["pools"]["long_bond"]["n_screened"] == 74
        assert p["pools"]["long_bond"]["has_prev"] is False
        assert p["pools"]["long_bond"]["funnel"]["合并基金数"] == 945
        assert p["pools"]["long_bond"]["screen"]["spike_max_gain"] == 0.02

    def test_top_level_follows_primary_pool(self):
        # 顶层 = 首个池（PIPELINE.pools 主池）→ 旧页面单池语义不变
        p = self._payload()
        assert p["category"] == "short_bond"
        assert p["n"] == 20 and p["n_screened"] == 23
        assert p["top_n"] == 20 and p["has_prev"] is True
        assert p["funnel"] == {"合并基金数": 482}
        json.dumps(p)   # 必须可序列化

    def test_category_falls_back_to_pool_column(self):
        row = _row(category="long_bond")
        row = row.drop(labels=["category"])          # 无 category 列
        row["pool"] = "long_bond"                    # pipeline 只给 pool 列时也认
        p = cards.build_payload(pd.DataFrame([row]), {"002438": _rec()},
                                {"002438": _nav()})
        assert p["funds"][0]["category"] == "long_bond"

    def test_category_default_short_bond(self):
        row = _row().drop(labels=["category"])
        p = cards.build_payload(pd.DataFrame([row]), {"002438": _rec()},
                                {"002438": _nav()})
        assert p["funds"][0]["category"] == "short_bond"
        assert p["category"] == "short_bond"


class TestRankChanges:
    def test_no_history_first_issue(self):
        """首期（无历史）：全部无环比、不标新增。"""
        p = _payload(prev_ranks=None)
        f = p["funds"][0]
        assert f["rank_prev"] is None and f["rank_delta"] is None
        assert f["is_new"] is False
        assert p["has_prev"] is False

    def test_delta_up_down_new(self):
        res = pd.DataFrame([
            _row(rank=1, base="A"), _row(rank=2, code="B", base="B"),
            _row(rank=3, code="C", base="C"),
        ])
        res = pipeline.attach_rank_changes(res, {"A": 3, "B": 2})  # C 新增
        assert res.loc[0, "rank_delta"] == 2 and not res.loc[0, "is_new"]
        assert res.loc[1, "rank_delta"] == 0
        assert bool(res.loc[2, "is_new"]) is True
        assert pd.isna(res.loc[2, "rank_prev"])

    def test_history_roundtrip(self, tmp_path):
        path = tmp_path / "rank_history.json"
        res = pd.DataFrame([_row(base="002438", rank=1)])
        pipeline.save_history(res, path=path, today="2026-10-16")
        assert pipeline.load_prev_ranks(path, today="2026-10-23") == {"002438": 1}
        # 同日重跑：基准仍是上一期（严格早于 today）
        assert pipeline.load_prev_ranks(path, today="2026-10-16") is None
        # 无文件 → None
        assert pipeline.load_prev_ranks(tmp_path / "nope.json") is None

    def test_history_keep_limit(self, tmp_path):
        path = tmp_path / "h.json"
        res = pd.DataFrame([_row(base="X", rank=1)])
        for i in range(pipeline.HISTORY_KEEP + 14):
            pipeline.save_history(res, path=path, today=f"2025-{1+i//28:02d}-{1+i%28:02d}")
        hist = json.loads(path.read_text(encoding="utf-8"))
        assert len(hist) == pipeline.HISTORY_KEEP
        assert all(v == {"short_bond": {"X": 1}} for v in hist.values())   # 写入即嵌套


class TestHistoryMigration:
    """键结构 {date: {pool: {base: rank}}}；旧扁平期按 short_bond 迁移，历史不丢。"""

    def test_legacy_flat_read(self, tmp_path):
        path = tmp_path / "rank_history.json"
        path.write_text(json.dumps({"2026-10-16": {"基金甲": 1, "基金乙": 2}}),
                        encoding="utf-8")
        assert pipeline.load_prev_ranks(path, today="2026-10-23",
                                        pool="short_bond") == {"基金甲": 1, "基金乙": 2}
        assert pipeline.load_prev_ranks(path, today="2026-10-23",
                                        pool="long_bond") is None   # 旧期无长债
        assert pipeline.load_prev_ranks(path, today="2026-10-23") == {"基金甲": 1,
                                                                      "基金乙": 2}

    def test_save_migrates_legacy_and_keeps_short_bond(self, tmp_path):
        path = tmp_path / "rank_history.json"
        path.write_text(json.dumps({"2026-10-16": {"基金甲": 1}}), encoding="utf-8")
        res = pd.DataFrame([_row(base="基金丙", rank=1)])
        pipeline.save_history(res, path=path, today="2026-10-23", pool="long_bond")
        hist = json.loads(path.read_text(encoding="utf-8"))
        assert hist["2026-10-16"] == {"short_bond": {"基金甲": 1}}   # 旧期就地升级
        assert hist["2026-10-23"] == {"long_bond": {"基金丙": 1}}
        # 环比各取各池：最新一期只有长债 → 短债再看上一期（10-16，迁移后）
        assert pipeline.load_prev_ranks(path, today="2026-10-30",
                                        pool="long_bond") == {"基金丙": 1}
        assert pipeline.load_prev_ranks(path, today="2026-10-30",
                                        pool="short_bond") is None
        assert pipeline.load_prev_ranks(path, today="2026-10-23",
                                        pool="short_bond") == {"基金甲": 1}

    def test_same_date_two_pools_coexist(self, tmp_path):
        path = tmp_path / "rank_history.json"
        pipeline.save_history(pd.DataFrame([_row(base="短债甲", rank=1)]),
                              path=path, today="2026-10-16", pool="short_bond")
        pipeline.save_history(pd.DataFrame([_row(base="长债乙", rank=1)]),
                              path=path, today="2026-10-16", pool="long_bond")
        hist = json.loads(path.read_text(encoding="utf-8"))
        assert hist["2026-10-16"] == {"short_bond": {"短债甲": 1},
                                      "long_bond": {"长债乙": 1}}
        assert pipeline.load_prev_ranks(path, today="2026-10-23",
                                        pool="short_bond") == {"短债甲": 1}
        assert pipeline.load_prev_ranks(path, today="2026-10-23",
                                        pool="long_bond") == {"长债乙": 1}
        # 同日重跑仍对比上一期
        assert pipeline.load_prev_ranks(path, today="2026-10-16",
                                        pool="long_bond") is None

    def test_multi_pool_latest_period_requires_explicit_pool(self, tmp_path):
        path = tmp_path / "rank_history.json"
        pipeline.save_history(pd.DataFrame([_row(base="A", rank=1)]),
                              path=path, today="2026-10-16", pool="short_bond")
        pipeline.save_history(pd.DataFrame([_row(base="B", rank=1)]),
                              path=path, today="2026-10-16", pool="long_bond")
        assert pipeline.load_prev_ranks(path, today="2026-10-23") is None   # 二义 → 不猜


class TestTrend:
    def test_windows_slimmed(self):
        """瘦身：只下发 1y+5y（3y 由前端切片）；采样降点但保留首尾。"""
        nav = _nav(rows=1500)          # 长序列才会触发降采样（step = n//360）
        p = cards.build_payload(pd.DataFrame([_row()]), {"002438": _rec()},
                                {"002438": nav})
        f = p["funds"][0]
        assert set(f["trend"].keys()) == {"1y", "5y"}
        assert len(f["trend"]["5y"]) < 1500 // 2          # 降采样生效
        for pts in f["trend"].values():
            assert all(len(pp) == 2 for pp in pts)
            dates = [pp[0] for pp in pts]
            assert dates == sorted(dates)                 # 时序单调
        # 首尾保留：5y 窗口末点 = 净值末日
        assert f["trend"]["5y"][-1][0] == nav["date"].max().strftime("%Y-%m-%d")

    def test_empty_nav(self):
        p = cards.build_payload(pd.DataFrame([_row()]), {"002438": _rec()},
                                {"002438": None})
        f = p["funds"][0]
        assert f["trend"] == {"1y": [], "5y": []}
        assert all(v is None for v in f["stages"].values())


class TestWritePayload:
    def test_atomic_write_and_formats(self, tmp_path):
        p = _payload()
        jp, js = tmp_path / "top20.json", tmp_path / "data.js"
        cards.write_payload(p, jp, js)
        # json：可读缩进；js：window.CARD_DATA 前缀 + 紧凑（无换行缩进）
        jtxt = jp.read_text(encoding="utf-8")
        ctxt = js.read_text(encoding="utf-8")
        assert ctxt.startswith("window.CARD_DATA = ") and ctxt.rstrip().endswith(";")
        assert "\n" not in ctxt.strip()[:-1]          # 紧凑：正文仅 1 行
        assert len(ctxt) < len(jtxt)                  # 紧凑确实更小
        assert json.loads(ctxt.split("=", 1)[1].rstrip().rstrip(";")) == json.loads(jtxt)
        assert not list(tmp_path.glob("*.tmp"))        # 原子替换无残留

    def test_no_js_path(self, tmp_path):
        jp = tmp_path / "only.json"
        cards.write_payload(_payload(), jp)
        assert jp.exists()
