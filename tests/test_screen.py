# -*- coding: utf-8 -*-
"""screen 模块单元测试：合成数据验证基金级合并、硬条件、性能条件与 A 类排序（不依赖网络）。"""
import numpy as np
import pandas as pd
import pytest

from fundscorer import config as C
from fundscorer.screen import (
    base_name,
    complete_yearly_returns,
    composite_percentiles,
    load_screen_inputs,
    merge_shares,
    run_screen,
    scan_spikes,
    share_letter,
    yearly_median_pct,
)


# ---------------------------------------------------------------- 构造数据
def rec(code, name, scale=10.0, career=8.0, tenure=2000, nav_years=6.0,
        ret_5y=0.03, mdd_5y=0.004, ret_3y=0.028, mdd_3y=0.003, pool="short_bond"):
    return {"code": code, "name": name, "pool": pool, "scale_yi": scale,
            "mgr_career_years": career, "mgr_tenure_days": tenure,
            "nav_years": nav_years, "ret_5y": ret_5y, "mdd_5y": mdd_5y,
            "ret_3y": ret_3y, "mdd_3y": mdd_3y, "manager": "张三"}


def yearly_all(v, years=range(2021, 2026)):
    return {y: v for y in years}


class TestShareNames:
    def test_base_name_strips_letter(self):
        assert base_name("测试短债债券A") == "测试短债债券"
        assert base_name("测试短债债券C") == "测试短债债券"

    def test_base_name_strips_slash_letter(self):
        assert base_name("测试短债债券/AC") == "测试短债债券"

    def test_share_letter(self):
        assert share_letter("测试短债债券A") == "A"
        assert share_letter("测试短债债券/C") == "C"
        assert share_letter("测试短债债券A/B") == "A"   # 双类份额按主类 A
        assert share_letter("测试短债债券") is None

    def test_merge_groups_classes(self):
        rs = [rec("000001", "测试短债债券A", scale=3.0),
              rec("000002", "测试短债债券C", scale=7.0)]
        df = merge_shares(rs)
        assert len(df) == 1
        row = df.iloc[0]
        assert row["base"] == "测试短债债券"
        assert row["scale_sum"] == 10.0
        assert row["n_classes"] == 2
        assert row["rep_code"] == "000002"  # 代表份额 = 规模最大者

    def test_merge_keeps_distinct_funds(self):
        rs = [rec("000001", "基金甲债券A"), rec("000002", "基金乙债券A")]
        assert len(merge_shares(rs)) == 2


class TestFundLevelMerge:
    """基金级口径（2026-10-15 确认）：经理字段取同经理份额最大值，业绩取历史最久份额。"""

    def test_new_large_share_does_not_drag_manager_fields(self):
        # 长城短债场景：新发 D 份额规模最大（成为代表份额）但成立晚、任期记录短
        rs = [rec("007194", "长城短债A", scale=50.0, tenure=2603, nav_years=7.12),
              rec("019872", "长城短债D", scale=303.4, tenure=1083, nav_years=2.96)]
        row = merge_shares(rs).iloc[0]
        assert row["rep_code"] == "019872"          # 代表份额仍是规模最大的 D
        assert row["tenure"] == 2603                 # 任期取同经理份额最大值
        assert row["nav_years"] == 7.12              # 成立年限取全份额最大值
        assert row["src_code"] == "007194"           # 业绩口径 = 历史最久份额

    def test_metrics_come_from_oldest_share(self):
        rs = [rec("000001", "测试短债债券A", scale=20.0, nav_years=6.0, ret_5y=0.03),
              rec("000002", "测试短债债券C", scale=7.0, nav_years=3.0, ret_5y=0.09)]
        row = merge_shares(rs).iloc[0]
        assert row["src_code"] == "000001"
        assert row["ret_5y"] == pytest.approx(0.03)  # 用最久份额而非代表份额

    def test_oldest_tie_prefers_a_class(self):
        rs = [rec("000001", "测试短债债券A", scale=1.0, nav_years=6.0),
              rec("000002", "测试短债债券C", scale=9.0, nav_years=6.0)]
        row = merge_shares(rs).iloc[0]
        assert row["src_code"] == "000001"           # 年限并列优先 A 类

    def test_different_managers_use_rep_manager_only(self):
        # 经理更换场景：仅现任经理（代表份额所记）的份额计入任期
        rs = [rec("000001", "测试短债债券A", scale=30.0, tenure=2600),
              rec("000002", "测试短债债券C", scale=10.0, tenure=200)]
        rs[0]["manager"] = "张三"
        rs[1]["manager"] = "李四"                    # C 份额记的是新经理
        row = merge_shares(rs).iloc[0]
        assert row["manager"] == "张三"              # 代表份额 = 规模最大 A → 张三
        assert row["tenure"] == 2600


# ---------------------------------------------------------------- S5 异常涨幅
class TestSpikeScan:
    def _nav(self, jump_at=300, jump=0.05, n=500, start="2022-01-01"):
        dates = pd.bdate_range(start, periods=n)
        nav = np.ones(n)
        nav[jump_at:] *= (1 + jump)
        return pd.DataFrame({"date": dates, "nav": nav})

    def test_spike_inside_window_detected(self):
        nav = self._nav()  # 窗口内第300个交易日跳涨5%
        out = scan_spikes({"000001": nav}, pd.Timestamp("2026-10-15"))
        assert out["000001"] == pytest.approx(0.05, rel=1e-6)

    def test_spike_before_window_ignored(self):
        # 跳涨发生在 5 年窗口（2021-10-15）之前，窗口内只有平稳日收益
        nav = self._nav(jump_at=10, jump=0.05, n=2000, start="2019-01-01")
        out = scan_spikes({"000001": nav}, pd.Timestamp("2026-10-15"))
        assert out["000001"] < 0.01

    def test_small_gains_pass(self):
        nav = pd.DataFrame({
            "date": pd.bdate_range("2024-01-01", periods=300),
            "nav": np.cumprod([1.0] + [1.001] * 299)})
        out = scan_spikes({"000001": nav}, pd.Timestamp("2026-10-15"))
        assert out["000001"] <= 0.01

    def test_missing_nav_is_nan(self):
        out = scan_spikes({"000001": None}, pd.Timestamp("2026-10-15"))
        assert np.isnan(out["000001"])


# ---------------------------------------------------------------- 年度收益
class TestYearlyReturns:
    def test_complete_year_included(self):
        # 2020-12-31 基数 1.0，2021 全年 250 个交易日各 +0.01% → 年收益可算
        dates = pd.bdate_range("2020-12-01", periods=400)  # 覆盖2021全年
        nav = np.cumprod([1.0] + [1.0001] * 399)
        df = pd.DataFrame({"date": dates, "nav": nav})
        out = complete_yearly_returns(df, end_year=2022, min_obs=120, n_years=1)
        assert 2021 in out
        assert out[2021] == pytest.approx(1.0001 ** sum(d.year == 2021 for d in dates) - 1,
                                          rel=1e-6)

    def test_first_year_without_prev_excluded(self):
        # 无上年基数 → 成立当年即使交易日足够也不算
        dates = pd.bdate_range("2021-01-04", periods=250)
        df = pd.DataFrame({"date": dates, "nav": np.cumprod([1.0] + [1.0001] * 249)})
        out = complete_yearly_returns(df, end_year=2022, min_obs=120, n_years=1)
        assert out == {}

    def test_short_year_excluded(self):
        # 2021 年内只有约 50 个交易日（<120）→ 即使有上年基数也不算完整年
        dates = pd.bdate_range("2020-06-01", periods=200)
        nav = np.cumprod([1.0] + [1.0001] * 199)
        df = pd.DataFrame({"date": dates, "nav": nav})
        out = complete_yearly_returns(df, end_year=2022, min_obs=120, n_years=1)
        assert out == {}

    def test_window_excludes_current_partial_year(self):
        dates = pd.bdate_range("2024-12-01", periods=400)  # 2025 全年有数
        df = pd.DataFrame({"date": dates,
                           "nav": np.cumprod([1.0] + [1.0001] * 399)})
        out = complete_yearly_returns(df, end_year=2026, min_obs=120, n_years=5)
        # end_year=2026 → 窗口 2021~2025，2025 数据齐全应入选
        assert set(out) <= {2021, 2022, 2023, 2024, 2025}
        assert 2025 in out


# ---------------------------------------------------------------- P1/P2
class TestPerformanceConditions:
    def test_composite_direction(self):
        # 池内 5 只：收益递增、回撤递减 → 最后一只四项分位最高
        rs = [rec(f"00000{i}", f"基金{i}债券A", ret_5y=0.01 + i * 0.01,
                  mdd_5y=0.05 - i * 0.005, ret_3y=0.01 + i * 0.01,
                  mdd_3y=0.04 - i * 0.005) for i in range(5)]
        df = merge_shares(rs)
        P = composite_percentiles(df)
        assert P["mpct"].iloc[-1] == 1.0
        assert P["mpct"].iloc[0] == pytest.approx(0.2)

    def test_yearly_median_pct(self):
        yearly = {"基金A": yearly_all(0.05), "基金B": yearly_all(0.01)}
        # 池内 2 只：分位 = 池内严格低于自己的比例 → A 有 1/2 在下 = 0.5，B = 0.0
        Y = pd.DataFrame(yearly).T
        assert yearly_median_pct(Y, "基金A", yearly, pool_min=2, min_years=4) == 0.5
        assert yearly_median_pct(Y, "基金B", yearly, pool_min=2, min_years=4) == 0.0

    def test_yearly_median_needs_min_years(self):
        yearly = {"基金A": {2024: 0.05, 2025: 0.05}}
        Y = pd.DataFrame(yearly).T
        assert yearly_median_pct(Y, "基金A", yearly, pool_min=1, min_years=4) is None


# ---------------------------------------------------------------- 中长债池（docs/09）
class TestLongBondPool:
    """SCREENS["long_bond"]：漏斗文案随池阈值、S5 池别化（>2%）、S1 保留 9 亿级样本。"""

    def _setup(self, n=40):
        records, yearly, snapshot, spikes = TestRunScreen()._setup(n)
        for r in records:
            r["pool"] = "long_bond"
        return records, yearly, snapshot, spikes

    def test_funnel_labels_follow_pool_cfg(self):
        records, yearly, snapshot, spikes = self._setup()
        _, funnel = run_screen(records, spikes, yearly, snapshot,
                               cfg=C.SCREENS["long_bond"])
        assert funnel["合并基金数"] == 40
        assert funnel["S1 规模合并≥5亿"] == 40
        assert funnel["S5 无近5年单日涨幅>2%"] == 40   # 长债专属文案（短债标 >1%）
        assert not any(k.startswith("S5") and ">1%" in k for k in funnel)
        assert funnel["P1 综合分位≥45%"] == 23          # 阈值同短债 → 同短债口径计数
        assert funnel["P2 年度分位中位≥55%"] == 18

    def test_spike_between_1pct_and_2pct_passes_long_only(self):
        # 1.5% 单日涨幅：长债高久期合法行情日 → 放行；短债口径判异常 → 剔除
        records, yearly, snapshot, spikes = self._setup()
        spikes[records[0]["code"]] = 0.015
        _, f_long = run_screen(records, spikes, yearly, snapshot,
                               cfg=C.SCREENS["long_bond"])
        assert f_long["S5 无近5年单日涨幅>2%"] == 40
        _, f_short = run_screen(records, spikes, yearly, snapshot)   # 默认短债 cfg
        assert f_short["S5 无近5年单日涨幅>1%"] == 39
        assert f_short["合并基金数"] == 40

    def test_scale_gate_keeps_9yi_sample(self):
        # 006980 式样本（9.04 亿）：S1 维持 ≥5 亿必须保留（≥10 亿会把它挡在门外）
        assert C.SCREENS["long_bond"]["scale_min_yi"] == 5.0
        records, yearly, snapshot, spikes = self._setup(10)
        records[0]["scale_yi"] = 9.04
        _, funnel = run_screen(records, spikes, yearly, snapshot,
                               cfg=C.SCREENS["long_bond"])
        assert funnel["S1 规模合并≥5亿"] == 10

    def test_pool_block_keys_match_short_bond(self):
        # 两池筛选块键集一致（screen/pipeline 逐键取值，缺键即崩）
        assert set(C.SCREENS["long_bond"]) == set(C.SCREENS["short_bond"])


class TestLoadInputsPoolFilter:
    def test_filters_cache_by_pool(self, tmp_path):
        import pickle
        recs = [rec("000001", "中长债基金A", pool="long_bond"),
                rec("000002", "短债基金A", pool="short_bond")]
        cache = tmp_path / "m.pkl"
        cache.write_bytes(pickle.dumps(recs))
        nav_dir = tmp_path / "nav"
        nav_dir.mkdir()
        records, spikes, yearly, navs = load_screen_inputs(
            cache, nav_dir, cfg=C.SCREENS["long_bond"])
        assert [r["code"] for r in records] == ["000001"]
        assert set(navs) == {"000001"}

    def test_empty_pool_raises(self, tmp_path):
        import pickle
        cache = tmp_path / "m.pkl"
        cache.write_bytes(pickle.dumps([rec("000001", "短债基金A", pool="short_bond")]))
        nav_dir = tmp_path / "nav"
        nav_dir.mkdir()
        with pytest.raises(ValueError, match="index"):
            load_screen_inputs(cache, nav_dir, cfg={"pool": "index"})


# ---------------------------------------------------------------- 端到端
class TestRunScreen:
    def _setup(self, n=40):
        """n 只基金：质量随 i 线性变好；每只单份额；快照含 A/C 两类分数。"""
        records, yearly, snapshot, spikes = [], {}, {}, {}
        for i in range(n):
            code = f"{i:06d}"
            name = f"测试短债{i}债券A"
            records.append(rec(code, name, scale=10.0 + i,
                               ret_5y=0.01 + i * 0.005, mdd_5y=0.05 - i * 0.003,
                               ret_3y=0.01 + i * 0.004, mdd_3y=0.04 - i * 0.002))
            yearly[base_name(name)] = yearly_all(0.01 + i * 0.01)
            snapshot[code] = {"code": code, "name": name,
                              "score_capped": 50.0 + i, "star": 3.0}
            spikes[code] = 0.005
        return records, yearly, snapshot, spikes

    def test_funnel_counts_and_selection(self):
        records, yearly, snapshot, spikes = self._setup()
        res, funnel = run_screen(records, spikes, yearly, snapshot)
        # 全部过硬条件；P1 保留池内前55%（分位≥0.45），P2 年度分位中位≥0.55
        assert funnel["合并基金数"] == 40
        assert funnel["S1 规模合并≥5亿"] == 40
        assert funnel["S5 无近5年单日涨幅>1%"] == 40
        # 年度分位 = 本只在池内的位置 (i / 40)，中位即同值 → i≥22.4… 实算如下
        assert funnel["P1 综合分位≥45%"] == 23  # 分位 ≥0.45 → i≥17（0基）→ 后23只
        assert funnel["P2 年度分位中位≥55%"] == 18  # i/40 ≥0.55 → i≥22 → 后18只
        assert len(res) == 18
        # 分数随 i 递增 → 排序应按分数降序
        assert list(res["score"]) == sorted(res["score"], reverse=True)
        assert list(res["rank"]) == list(range(1, len(res) + 1))

    def test_hard_conditions_filter(self):
        records, yearly, snapshot, spikes = self._setup(10)
        records[0]["scale_yi"] = 4.0          # S1 不达标
        records[1]["mgr_career_years"] = 3.0  # S2
        records[2]["mgr_tenure_days"] = 800   # S3
        records[3]["nav_years"] = 3.0         # S4
        spikes[records[4]["code"]] = 0.02     # S5 异常涨幅
        _, funnel = run_screen(records, spikes, yearly, snapshot)
        assert funnel["S1 规模合并≥5亿"] == 9
        assert funnel["S2 经理从业≥5年"] == 8
        assert funnel["S3 现经理任本基金≥3年"] == 7
        assert funnel["S4 成立≥5年"] == 6
        assert funnel["S5 无近5年单日涨幅>1%"] == 5

    def test_spike_checks_all_share_classes(self):
        records, yearly, snapshot, spikes = self._setup(10)
        # 同基金 C 类份额（规模更小、非代表）出现异常涨幅 → 整只基金剔除
        c_code = "900001"
        records.append(rec(c_code, base_name(records[0]["name"]) + "C", scale=1.0))
        spikes[c_code] = 0.03
        _, funnel = run_screen(records, spikes, yearly, snapshot)
        assert funnel["S4 成立≥5年"] == 10
        assert funnel["S5 无近5年单日涨幅>1%"] == 9

    def test_rank_uses_a_class_score(self):
        records, yearly, _, spikes = self._setup(40)
        # 选质量最好的基金（必入选），给它加一个规模更大（成为代表份额）的 C 类
        a_rec = records[39]
        a = a_rec["code"]
        base = base_name(a_rec["name"])
        c = "900002"
        records.append(rec(c, base + "C", scale=999.0,
                           ret_5y=a_rec["ret_5y"], mdd_5y=a_rec["mdd_5y"],
                           ret_3y=a_rec["ret_3y"], mdd_3y=a_rec["mdd_3y"],
                           career=a_rec["mgr_career_years"],
                           tenure=a_rec["mgr_tenure_days"],
                           nav_years=a_rec["nav_years"]))
        snapshot = {r["code"]: {"code": r["code"], "name": r["name"],
                                "score_capped": 50.0 + i, "star": 3.0}
                    for i, r in enumerate(records)}
        snapshot[a]["score_capped"] = 60.0   # A 类分 60
        snapshot[c]["score_capped"] = 90.0   # C 类（代表）分 90 → 排序必须用 A 的 60
        res, _ = run_screen(records, spikes, yearly, snapshot)
        row = res[res["base"] == base].iloc[0]
        assert row["code"] == a              # 按 A 类排序
        assert row["score"] == 60.0
        assert row["rep_code"] == c          # 代表份额仍是规模最大者
        assert row["score_source"] == "A类"

    def test_falls_back_to_rep_share_without_a(self):
        records, yearly, snapshot, spikes = self._setup(40)
        top = records[39]  # 克隆池内最优指标，确保通过 P1/P2
        only_c = rec("900003", "无A类短债C", scale=20.0,
                     ret_5y=top["ret_5y"], mdd_5y=top["mdd_5y"],
                     ret_3y=top["ret_3y"], mdd_3y=top["mdd_3y"])
        records.append(only_c)
        snapshot["900003"] = {"code": "900003", "name": only_c["name"],
                              "score_capped": 88.0, "star": 4.0}
        yearly["无A类短债"] = yearly_all(0.41)  # 高于池内所有(≤0.40) → 分位≈0.98
        spikes["900003"] = 0.004
        res, _ = run_screen(records, spikes, yearly, snapshot)
        row = res[res["base"] == "无A类短债"].iloc[0]
        assert row["code"] == "900003"
        assert row["score_source"] == "代表份额"

    def test_missing_spike_data_blocks(self):
        records, yearly, snapshot, spikes = self._setup(10)
        spikes[records[0]["code"]] = np.nan  # 数据不足 → 不放行 S5
        _, funnel = run_screen(records, spikes, yearly, snapshot)
        assert funnel["S5 无近5年单日涨幅>1%"] == 9


# ---------------------------------------------------------------- 固收+池（docs/10）
class TestFixedPlusPool:
    """SCREENS["fixed_plus"]：S1~S5 同构 + 专属风险预算闸 S6~S8（回撤/波动 ≤8%、反追收益 <5%）。"""

    @staticmethod
    def _setup(n=20):
        records, yearly, snapshot, spikes = {}, {}, {}, {}
        records, yearly, snapshot, spikes = TestRunScreen()._setup(n)
        for i, r in enumerate(records):
            r["pool"] = "fixed_plus"
            # 基夹具 ret_5y 步长 0.5%（i≥9 即 ≥5% 触发 S8）、mdd_5y 步长 −0.3%
            # （i≥14 转负数）——固收+夹具统一压回三闸放行区，单闸测试再各自越线
            r["vol_5y"] = 0.03 + i * 0.001     # 默认全部 ≤8%（S7 放行）
            r["ret_5y"] = 0.01 + i * 0.001     # 默认全部 <5%（S8 放行）
            r["mdd_5y"] = 0.03 - i * 0.001     # 默认全部 ≤8%（S6 放行）
        return records, yearly, snapshot, spikes

    def test_funnel_has_s6_s7_s8_labels(self):
        records, yearly, snapshot, spikes = self._setup()
        _, funnel = run_screen(records, spikes, yearly, snapshot,
                               cfg=C.SCREENS["fixed_plus"])
        assert funnel["S6 近5年最大回撤≤8%"] == 20
        assert funnel["S7 近5年年化波动≤8%"] == 20
        assert funnel["S8 近五年年化<5%"] == 20
        # 顺序：S6/S7/S8 紧跟 S5、在 P1 之前
        keys = list(funnel)
        assert keys.index("S6 近5年最大回撤≤8%") == keys.index("S5 无近5年单日涨幅>5%") + 1
        assert keys.index("S8 近五年年化<5%") < keys.index("P1 综合分位≥45%")

    def test_s6_blocks_high_drawdown(self):
        # mdd_5y 10% > 8% 预算 → 剔除（docs/10 §四 S6：>8% 桶前瞻卡玛崩坏）
        records, yearly, snapshot, spikes = self._setup(10)
        records[0]["mdd_5y"] = 0.10
        _, funnel = run_screen(records, spikes, yearly, snapshot,
                               cfg=C.SCREENS["fixed_plus"])
        assert funnel["S6 近5年最大回撤≤8%"] == 9
        assert funnel["S7 近5年年化波动≤8%"] == 9

    def test_s7_blocks_high_volatility(self):
        # 波动 10% > 8% → 剔除（"名不副实的固收+"，docs/10 §四 S7）
        records, yearly, snapshot, spikes = self._setup(10)
        records[0]["vol_5y"] = 0.10
        _, funnel = run_screen(records, spikes, yearly, snapshot,
                               cfg=C.SCREENS["fixed_plus"])
        assert funnel["S7 近5年年化波动≤8%"] == 9

    def test_s8_blocks_aggressive_return(self):
        # 近五年年化 6% ≥ 5% → 剔除（反追收益闸：高收益桶前瞻回撤最高，docs/10 §三）
        records, yearly, snapshot, spikes = self._setup(10)
        records[0]["ret_5y"] = 0.06
        _, funnel = run_screen(records, spikes, yearly, snapshot,
                               cfg=C.SCREENS["fixed_plus"])
        assert funnel["S8 近五年年化<5%"] == 9
        # 恰好 5% 也剔除（阈值为闭区间下限，"≥5% 剔除"）
        records[0]["ret_5y"] = 0.05
        _, funnel = run_screen(records, spikes, yearly, snapshot,
                               cfg=C.SCREENS["fixed_plus"])
        assert funnel["S8 近五年年化<5%"] == 9

    def test_nan_blocked_by_all_three_gates(self):
        # 数据不足（NaN）不放行——与 S5 同口径（无法验证即不通过）
        records, yearly, snapshot, spikes = self._setup(5)
        records[0]["mdd_5y"] = np.nan
        records[1]["vol_5y"] = np.nan
        records[2]["ret_5y"] = np.nan
        _, funnel = run_screen(records, spikes, yearly, snapshot,
                               cfg=C.SCREENS["fixed_plus"])
        assert funnel["S6 近5年最大回撤≤8%"] == 4
        assert funnel["S7 近5年年化波动≤8%"] == 3
        assert funnel["S8 近五年年化<5%"] == 2

    def test_bond_pools_have_no_s6_s7_s8(self):
        # 两债池 cfg 无三闸键 → 漏斗不含 S6/S7/S8（既有漏斗键名零变化）
        for pool in ("short_bond", "long_bond"):
            records, yearly, snapshot, spikes = TestRunScreen()._setup(10)
            for r in records:
                r["pool"] = pool
            _, funnel = run_screen(records, spikes, yearly, snapshot,
                                   cfg=C.SCREENS[pool])
            assert not any(k[:2] in ("S6", "S7", "S8") for k in funnel), pool
            assert list(funnel)[:6] == ["合并基金数", "S1 规模合并≥5亿",
                                        "S2 经理从业≥5年", "S3 现经理任本基金≥3年",
                                        "S4 成立≥5年",
                                        f"S5 无近5年单日涨幅>"
                                        f"{C.SCREENS[pool]['spike_max_gain'] * 100:g}%"]

    def test_fixed_plus_gate_keys_exist(self):
        # 三闸阈值必须在 cfg 中显式给出（screen 按 .get 取值，缺键=静默跳过=闸门失效）
        cfg = C.SCREENS["fixed_plus"]
        assert cfg["mdd_5y_max"] == 0.08
        assert cfg["vol_5y_max"] == 0.08
        assert cfg["ret_5y_max"] == 0.05
        # S1~S5/P1~P2 与两债池同键（结构同构）
        assert set(C.SCREENS["fixed_plus"]) >= set(C.SCREENS["short_bond"])
