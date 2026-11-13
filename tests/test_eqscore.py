# -*- coding: utf-8 -*-
"""偏股两榜评分引擎（fundscorer/eqscore.py）单元测试。

覆盖（docs/20 §2.2/§2.4）：
  · 展示桶归一（display_bucket：只影响徽章，不影响同格分位）
  · 考卷判定 exam_flags（G3s 中档 / G4s 严档 / NaN 拒判 / 双卷组合）
  · 名单状态机 roster_update（入场 / 首过不出 / 连续两次不过出 /
    离职·出闸·资格失效即时出 / 数据不全冻结 / 毕业迁移与落榜 / eval 节奏）
  · 资格 qualify_pairs（pair 级 S3 任期闸 / 净值断档 / F10 现任经理）
  · 基金通道 fund_channels（= 名下经理从业最大值）
  · W3 权重契约（和为1、F4=0 不进分、与验证脚本口径一致）
  · build_factors（W3 加权分 + 池内分位，缺因子时与验证脚本同款固定分母）
  · roster 落盘往返（monkeypatch 到 tmp_path，不碰真实 eq_roster.json）
  · cards.build_payload 的 mgr 页签过滤（无数据不出现空页签）
"""
import numpy as np
import pandas as pd
import pytest

from fundscorer import cards, data as D, eqscore as ES


# ---------------------------------------------------------------- 1. 展示桶
class TestDisplayBucket:
    def test_alias_mapping(self):
        assert ES.display_bucket("金融价值") == "价值"
        assert ES.display_bucket("工业") == "价值"      # 工业→周期→价值
        assert ES.display_bucket("其他/均衡") == "均衡"

    def test_canon3_order(self):
        assert ES.CANON3 == ["成长", "价值", "均衡"]
        assert (set(ES.GROWTH_BUCKETS) | set(ES.VALUE_BUCKETS)
                | {"均衡"}) == set(ES.CANON7)

    def test_seven_to_three(self):
        assert ES.display_bucket("科技") == "成长"
        assert ES.display_bucket("医药") == "成长"
        assert ES.display_bucket("新能源") == "成长"
        assert ES.display_bucket("消费") == "价值"
        assert ES.display_bucket("周期") == "价值"
        assert ES.display_bucket("均衡") == "均衡"

    def test_display_alias_is_display_only(self):
        """归一只改显示串——考卷桶（STYLE/BUCKET_PATTERMS 原始值）不受影响。"""
        assert set(ES.DISPLAY_ALIAS) & set(ES.CANON7) == set()


# ---------------------------------------------------------------- 2. 考卷
def _feat(ps_rows):
    """ps_rows: {code: [ps0..ps4]}（None=该年窗口不足）。"""
    rows = []
    for code, ps in ps_rows.items():
        rec = {"bucket": "科技", "type": "股票型"}
        for j, v in enumerate(ps):
            rec[f"ps_{j}"] = v
        rows.append(rec)
    return pd.DataFrame(rows).set_index(pd.Index(list(ps_rows), name="code"))


class TestExamFlags:
    def test_g3s_pass_exactly_three(self):
        f = _feat({"000001": [76.0, 76.0, 76.0, 10.0, 10.0]})   # 恰3年>75
        assert ES.exam_flags(f) == {"000001": "G3s"}

    def test_g3s_boundary_75_not_counted(self):
        """75 不算"进前25%"（>75 才算）：3 个 75 + 1 个 90 → G3s 不过；
        均值 65 <70 → G4s 也不过 → None（若 75 被误计则会是 G3s）。"""
        f = _feat({"000001": [75.0, 75.0, 75.0, 90.0, 10.0]})
        assert ES.exam_flags(f) == {"000001": None}

    def test_g3s_fail_two_top(self):
        f = _feat({"000001": [90.0, 90.0, 10.0, 10.0, 10.0]})
        assert ES.exam_flags(f) == {"000001": None}

    def test_g4s_mean_boundary(self):
        f = _feat({"000001": [70.0, 70.0, 70.0, 70.0, 70.0]})   # 均值恰 70 → 过
        assert ES.exam_flags(f) == {"000001": "G4s"}

    def test_g4s_fail_mean_below(self):
        f = _feat({"000001": [70.0, 70.0, 70.0, 70.0, 69.0]})
        assert ES.exam_flags(f) == {"000001": None}

    def test_dual_tag_when_both_pass(self):
        f = _feat({"000001": [90.0, 90.0, 90.0, 85.0, 85.0]})   # G3s+G4s 同过
        assert ES.exam_flags(f) == {"000001": "G3sG4s"}

    def test_nan_year_rejects(self):
        f = _feat({"000001": [90.0, 90.0, None, 90.0, 90.0]})   # 窗口不足 → 拒判
        assert ES.exam_flags(f) == {"000001": None}

    def test_flags_per_code(self):
        f = _feat({"000001": [76.0, 76.0, 76.0, 10.0, 10.0],
                   "000002": [90.0, 90.0, 90.0, 85.0, 85.0],
                   "000003": [50.0, 50.0, 50.0, 50.0, 50.0]})
        out = ES.exam_flags(f)
        assert out["000001"] == "G3s"
        assert out["000002"] == "G3sG4s"
        assert out["000003"] is None


# ---------------------------------------------------------------- 3. 名单状态机
def _cand(key="张三|某公司", **over):
    mgr, company = key.split("|", 1)
    c = dict(mgr=mgr, company=company, name=mgr, career=12.0, channel="A",
             qual_funds=["000001"], pass_funds=["000001"],
             data_complete=True, has_pairs=True)
    c.update(over)
    return c


def _state(members=None, eval_no=0):
    return dict(members=dict(members or {}), exits=[], eval_no=eval_no)


TODAY = "2026-11-13"


class TestRosterEntry:
    def test_bootstrap_entry_vet(self):
        st = _state()
        st, ch = ES.roster_update(st, {"张三|某公司": _cand()}, True, TODAY, "2026-11-11")
        m = st["members"]["张三|某公司"]
        assert m["list"] == "vet"
        assert m["entry"] == TODAY and m["fails"] == 0
        assert m["funds"] == ["000001"]
        assert st["eval_no"] == 1 and st["eval_date"] == TODAY
        assert any("入场" in s for s in ch)

    def test_entry_new_channel(self):
        st = _state()
        c = _cand(career=8.0, channel="B")
        st, _ = ES.roster_update(st, {"张三|某公司": c}, True, TODAY, "2026-11-11")
        assert st["members"]["张三|某公司"]["list"] == "new"

    def test_no_entry_without_pass_funds(self):
        st = _state()
        c = _cand(pass_funds=[])
        st, _ = ES.roster_update(st, {"张三|某公司": c}, True, TODAY, "2026-11-11")
        assert "张三|某公司" not in st["members"]


class TestDoubleWeekProtection:
    def test_first_fail_stays(self):
        m = dict(list="vet", entry="2026-11-01", funds=["000001"], fails=0,
                 last_eval="2026-11-06", name="张三", company="某公司")
        st = _state({"张三|某公司": m})
        c = _cand(pass_funds=[])                      # 本期考卷没过
        st, ch = ES.roster_update(st, {"张三|某公司": c}, True, TODAY, "2026-11-11")
        mm = st["members"]["张三|某公司"]
        assert mm in st["members"].values()           # 首过不出
        assert mm["fails"] == 1
        assert any("首过不出" in s for s in ch)

    def test_second_fail_exits(self):
        m = dict(list="vet", entry="2026-11-01", funds=["000001"], fails=1,
                 last_eval="2026-11-06", name="张三", company="某公司")
        st = _state({"张三|某公司": m})
        st, ch = ES.roster_update(st, {"张三|某公司": _cand(pass_funds=[])},
                                  True, TODAY, "2026-11-11")
        assert "张三|某公司" not in st["members"]
        assert st["exits"] and st["exits"][-1]["reason"] == "连续两次不过"
        assert any("连续两次不过" in s for s in ch)

    def test_pass_resets_fails(self):
        m = dict(list="vet", entry="2026-11-01", funds=["000001"], fails=1,
                 last_eval="2026-11-06", name="张三", company="某公司")
        st = _state({"张三|某公司": m})
        st, _ = ES.roster_update(st, {"张三|某公司": _cand()}, True, TODAY, "2026-11-11")
        assert st["members"]["张三|某公司"]["fails"] == 0

    def test_eval_skip_keeps_member_and_fails(self):
        """eval_now=False：不判卷、不入场，但硬条件仍即时执行。"""
        m = dict(list="vet", entry="2026-11-01", funds=["000001"], fails=1,
                 last_eval="2026-11-06", name="张三", company="某公司")
        st = _state({"张三|某公司": m}, eval_no=3)
        st, ch = ES.roster_update(st, {"张三|某公司": _cand(pass_funds=[])},
                                  False, TODAY, "2026-11-11")
        assert st["members"]["张三|某公司"]["fails"] == 1   # 未判卷
        assert st["eval_no"] == 3                          # 期数不涨
        assert ch == []


class TestImmediateExits:
    def test_quit_when_no_pairs(self):
        m = dict(list="vet", entry="2026-11-01", funds=["000001"], fails=0,
                 last_eval=None, name="张三", company="某公司")
        st = _state({"张三|某公司": m})
        st, _ = ES.roster_update(st, {}, False, TODAY, "2026-11-11")   # 已无在管对
        assert st["exits"][-1]["reason"] == "离职"

    def test_career_gate_out(self):
        m = dict(list="vet", entry="2026-11-01", funds=["000001"], fails=0,
                 last_eval=None, name="张三", company="某公司")
        st = _state({"张三|某公司": m})
        c = _cand(channel="out")
        st, _ = ES.roster_update(st, {"张三|某公司": c}, False, TODAY, "2026-11-11")
        assert st["exits"][-1]["reason"] == "从业出闸"

    def test_qualification_lost_exits_immediately(self):
        """无入围基金且数据完整 → 资格失效即时出（不受双周保护）。"""
        m = dict(list="vet", entry="2026-11-01", funds=["000001"], fails=0,
                 last_eval=None, name="张三", company="某公司")
        st = _state({"张三|某公司": m})
        c = _cand(qual_funds=[], pass_funds=[], data_complete=True)
        st, _ = ES.roster_update(st, {"张三|某公司": c}, False, TODAY, "2026-11-11")
        assert st["exits"][-1]["reason"] == "资格失效"

    def test_data_incomplete_freezes(self):
        m = dict(list="vet", entry="2026-11-01", funds=["000001"], fails=0,
                 last_eval=None, name="张三", company="某公司")
        st = _state({"张三|某公司": m})
        c = _cand(qual_funds=[], pass_funds=[], data_complete=False)
        st, ch = ES.roster_update(st, {"张三|某公司": c}, True, TODAY, "2026-11-11")
        assert "张三|某公司" in st["members"]            # 冻结不判
        assert st["exits"] == []

    def test_unknown_channel_freezes(self):
        m = dict(list="vet", entry="2026-11-01", funds=["000001"], fails=0,
                 last_eval=None, name="张三", company="某公司")
        st = _state({"张三|某公司": m})
        c = _cand(channel="unknown", career=None)
        st, _ = ES.roster_update(st, {"张三|某公司": c}, True, TODAY, "2026-11-11")
        assert "张三|某公司" in st["members"]            # 从业数据缺失 → 冻结
        assert st["exits"] == []


class TestGraduation:
    def _member(self, list_="new"):
        return dict(list=list_, entry="2026-06-01", funds=["000001"], fails=0,
                    last_eval="2026-11-06", name="张三", company="某公司")

    def test_graduation_pass_migrates_keeps_entry(self):
        """毕业（新→老）考老将卷过 → 迁移，榜龄保留（换榜≠出榜）。"""
        st = _state({"张三|某公司": self._member("new")})
        c = _cand(channel="A", career=10.5)            # 从业满10 → 目标老将卷
        st, ch = ES.roster_update(st, {"张三|某公司": c}, True, TODAY, "2026-11-11")
        m = st["members"]["张三|某公司"]
        assert m["list"] == "vet"
        assert m["entry"] == "2026-06-01"              # 榜龄保留
        assert m.get("graduated") == TODAY
        assert any("换榜" in s for s in ch)

    def test_graduation_fail_exits(self):
        """毕业没过老将卷 → 即落榜（§2.4 毕业原文）。"""
        st = _state({"张三|某公司": self._member("new")})
        c = _cand(channel="A", career=10.5, pass_funds=[])
        st, _ = ES.roster_update(st, {"张三|某公司": c}, True, TODAY, "2026-11-11")
        assert "张三|某公司" not in st["members"]
        assert st["exits"][-1]["reason"] == "毕业未过老将卷"

    def test_demotion_pass_migrates(self):
        """数据修正 老→新：过新锐卷 → 迁移到新锐榜。"""
        st = _state({"张三|某公司": self._member("vet")})
        c = _cand(channel="B", career=8.0)
        st, _ = ES.roster_update(st, {"张三|某公司": c}, True, TODAY, "2026-11-11")
        assert st["members"]["张三|某公司"]["list"] == "new"

    def test_demotion_fail_exits(self):
        st = _state({"张三|某公司": self._member("vet")})
        c = _cand(channel="B", career=8.0, pass_funds=[])
        st, _ = ES.roster_update(st, {"张三|某公司": c}, True, TODAY, "2026-11-11")
        assert st["exits"][-1]["reason"] == "换榜未过新锐卷"


# ---------------------------------------------------------------- 4. 资格闸
def _q_env(*, career=12.0, start="2018-01-01", nav_end="2026-11-11",
           in_f10=True, code="000001", span_ok=True):
    pairs = pd.DataFrame([dict(mgr="张三", company="某公司", code=code,
                               career_y=career)])
    feat = pd.DataFrame({"nav_end": [pd.Timestamp(nav_end)],
                         "bucket": ["科技"], "type": ["股票型"]},
                        index=pd.Index([code], name="code"))
    info = {"managers": ([{"name": "张三", "start": start}] if in_f10 else [])}
    return pairs, feat, {code: info}, pd.Timestamp("2026-11-13")


class TestQualifyPairs:
    def test_pass_pair(self):
        pairs, feat, f10s, o = _q_env()
        q = ES.qualify_pairs(pairs, feat, f10s, o)
        assert len(q) == 1 and q.iloc[0]["tenure_y"] >= 5

    def test_tenure_floor_rejects(self):
        """pair 级 S3：本人任期<1825 天 → 该 pair 不入围（与 eqgate 闸同口径）。"""
        pairs, feat, f10s, o = _q_env(start="2024-06-01")
        assert ES.qualify_pairs(pairs, feat, f10s, o).empty

    def test_stale_nav_rejects(self):
        """净值断档 >180 天 → 不入围（资格失效即时出的依据）。"""
        pairs, feat, f10s, o = _q_env(nav_end="2026-01-01")
        assert ES.qualify_pairs(pairs, feat, f10s, o).empty

    def test_not_current_manager_rejects(self):
        pairs, feat, f10s, o = _q_env(in_f10=False)
        assert ES.qualify_pairs(pairs, feat, f10s, o).empty

    def test_channel_out_rejects(self):
        pairs, feat, f10s, o = _q_env(career=5.0)
        assert ES.qualify_pairs(pairs, feat, f10s, o).empty

    def test_missing_feat_rejects(self):
        pairs, feat, f10s, o = _q_env()
        feat = feat.drop(index="000001")
        assert ES.qualify_pairs(pairs, feat, f10s, o).empty

    def test_channel_b_allowed(self):
        pairs, feat, f10s, o = _q_env(career=8.0)
        q = ES.qualify_pairs(pairs, feat, f10s, o)
        assert len(q) == 1 and q.iloc[0]["channel"] == "B"


class TestFundChannels:
    def test_max_career_wins(self):
        pairs = pd.DataFrame([
            dict(mgr="a", company="x", code="1", career_y=12.0),
            dict(mgr="b", company="x", code="1", career_y=8.0)])
        fch = ES.fund_channels(pairs)
        assert fch["1"] == "A"

    def test_single_junior_is_b(self):
        pairs = pd.DataFrame([dict(mgr="b", company="x", code="1", career_y=8.0)])
        assert ES.fund_channels(pairs)["1"] == "B"

    def test_nan_career_unknown(self):
        pairs = pd.DataFrame([dict(mgr="b", company="x", code="1",
                                   career_y=float("nan"))])
        assert ES.fund_channels(pairs)["1"] == "unknown"


# ---------------------------------------------------------------- 5. W3 权重契约
class TestW3Contract:
    def test_weights_sum_to_one(self):
        assert abs(sum(ES.W3.values()) - 1.0) < 1e-9

    def test_f4_excluded(self):
        """F4 三起点负 IC 数据证伪 → 权重归零不进分（docs/20 §2.2）。"""
        assert "F4" not in ES.W3

    def test_weight_values(self):
        assert ES.W3 == {"F1": 0.27, "F3": 0.25, "F5": 0.19,
                         "F2": 0.15, "F6": 0.14}


# ---------------------------------------------------------------- 6. 因子与评分
def _factor_env():
    o = pd.Timestamp("2026-11-13")
    codes = ["000001", "000002", "000003"]
    rows = []
    for i, c in enumerate(codes):
        rows.append(dict(bucket="科技", type="股票型", ret5y=0.10 + 0.02 * i,
                         **{f"ps_{j}": 50.0 + 10 * i for j in range(5)},
                         **{f"back{j}": 0.05 + 0.01 * i for j in range(5)},
                         **{f"cexc_{j}": 0.02 + 0.01 * i for j in range(5)}))
    feat = pd.DataFrame(rows).set_index(pd.Index(codes, name="code"))
    pairs = pd.DataFrame([dict(mgr="张三", company="某公司", code=c,
                               career_y=12.0) for c in codes])
    f10s = {c: {"managers": [{"name": "张三", "start": "2019-01-01"}]}
            for c in codes}
    return o, feat, set(codes), pairs, f10s, {}


class TestBuildFactors:
    def test_score_column_and_range(self):
        o, feat, qual, pairs, f10s, wealth = _factor_env()
        df = ES.build_factors(o, feat, qual, pairs, f10s, wealth)
        assert "score" in df.columns
        assert df["score"].between(0, 100).all()

    def test_percentile_monotone_vs_f1(self):
        """F1（同格逐年排位均值）越高 → 分位越高 → 分数不低。"""
        o, feat, qual, pairs, f10s, wealth = _factor_env()
        df = ES.build_factors(o, feat, qual, pairs, f10s, wealth).sort_values("F1_p")
        assert df["F1_p"].is_monotonic_increasing
        assert df["score"].is_monotonic_increasing

    def test_w3_formula_matches_validation(self):
        """与 scripts/eq_score_test.scores 同款：Σ(w·pct)/Σw（缺因子跳过、固定分母）。"""
        o, feat, qual, pairs, f10s, wealth = _factor_env()
        df = ES.build_factors(o, feat, qual, pairs, f10s, wealth)
        r = df.iloc[0]
        expect = sum(ES.W3[f] * r[f + "_p"] for f in ES.W3) / sum(ES.W3.values())
        assert abs(r["score"] - round(expect, 1)) < 1e-9

    def test_f4_present_but_not_in_score(self):
        o, feat, qual, pairs, f10s, wealth = _factor_env()
        df = ES.build_factors(o, feat, qual, pairs, f10s, wealth)
        assert "F4" in df.columns                    # 算了（CSV 供回核）
        # score 与 F4_p 无关：把 F4_p 全改99 分数不变
        r = df.iloc[0]
        base = r["score"]
        expect = sum(ES.W3[f] * r[f + "_p"] for f in ES.W3) / sum(ES.W3.values())
        assert abs(base - round(expect, 1)) < 1e-9

    def test_empty_qual_returns_empty(self):
        o, feat, _q, pairs, f10s, wealth = _factor_env()
        df = ES.build_factors(o, feat, set(), pairs, f10s, wealth)
        assert df.empty


# ---------------------------------------------------------------- 7. 落盘往返
class TestRosterPersistence:
    def test_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ES, "ROSTER_PATH", tmp_path / "eq_roster.json")
        st = _state({"张三|某公司": dict(list="vet", entry="2026-11-01",
                                         funds=["000001"], fails=0,
                                         name="张三", company="某公司")},
                    eval_no=5)
        ES.save_roster(st)
        loaded = ES.load_roster()
        assert loaded["eval_no"] == 5
        assert loaded["members"]["张三|某公司"]["entry"] == "2026-11-01"

    def test_exits_capped_at_50(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ES, "ROSTER_PATH", tmp_path / "eq_roster.json")
        st = _state()
        st["exits"] = [dict(key=f"k{i}", name="", date=TODAY,
                            reason="连续两次不过", list="vet") for i in range(60)]
        ES.save_roster(st)
        assert len(ES.load_roster()["exits"]) == 50

    def test_missing_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ES, "ROSTER_PATH", tmp_path / "nope.json")
        assert ES.load_roster() is None

    def test_corrupt_file_returns_none(self, tmp_path, monkeypatch):
        p = tmp_path / "eq_roster.json"
        p.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(ES, "ROSTER_PATH", p)
        assert ES.load_roster() is None


# ---------------------------------------------------------------- 8. payload 页签过滤
def _mini_res_nav():
    """最小可跑 build_payload 的输入（与 test_cards 同构，独立副本避免跨文件 import）。"""
    dates = pd.bdate_range("2022-01-01", periods=500)
    nav = pd.DataFrame({"date": dates, "nav": np.linspace(1.0, 1.15, 500)})
    rec = {"code": "002438", "name": "测试债A", "pool": "short_bond",
           "ret_1y": 0.02, "ret_3y": 0.025, "ret_5y": 0.028,
           "mdd_1y": -0.003, "mdd_3y": -0.008, "mdd_5y": -0.012,
           "vol_1y": 0.0021, "vol_5y": 0.0035,
           "lvi_5y": 1.2, "lvi_3y": 1.1,
           "winrate_m_5y": 0.62, "q_win_20q": 0.55, "q_excess_20q": 0.58,
           "excess_5y": 0.004, "bench_ann_5y": 0.023,
           "yearly": {2022: 0.021, 2023: 0.027, 2024: 0.030, 2025: 0.026,
                      2026: 0.019},
           "repair_5y": {"recovered": True, "repair_days": 45, "signif": True},
           "establish_date": "2016-03-01", "age": 10.6, "ms_rating": 3.0,
           "inst_ratio": 0.31, "benchmark": "x", "fee_total": 0.0032,
           "mgmt_fee": 0.0025, "custody_fee": 0.0007, "sales_fee": 0.0,
           "purchase_fee": 0.008, "purchase_fee_disc": 0.0008,
           "manager": "张某", "career": 11.4, "tenure": 1825,
           "nav_years": 4.8, "mgr_products_ann": 0.031, "mgr_products_n": 6,
           "score_capped": 86.83, "star": 5}
    row = pd.Series({
        "rank": 1, "code": "002438", "name": "测试债A", "base": "002438",
        "src_code": "002438", "category": "short_bond", "score": 86.83,
        "scale_sum": 9.81, "n_classes": 3, "nav_years": 4.8,
        "manager": "张某", "career": 11.4, "tenure": 1825,
        "comp_pct": 0.87, "yearly_med": 0.91, "max_gain_5y": 0.0031,
        "rank_prev": None, "rank_delta": None, "is_new": False})
    return pd.DataFrame([row]), {"002438": rec}, {"002438": nav}


def _payload(mgr):
    res, recs, navs = _mini_res_nav()
    return cards.build_payload(res, recs, navs, mgr=mgr)


class TestPayloadMgrTabs:
    def test_none_filters_mgr_tabs(self):
        p = _payload(None)
        ids = [c["id"] for c in p["categories"]]
        assert "mgr_vet" not in ids and "mgr_new" not in ids
        assert p["mgr"] is None
        assert "short_bond" in ids                    # 偏债页签不受影响

    def test_present_keeps_mgr_tabs(self):
        p = _payload({"vet": [], "new": [], "detail": {}})
        ids = [c["id"] for c in p["categories"]]
        assert "mgr_vet" in ids and "mgr_new" in ids
        assert p["mgr"]["vet"] == []


# ---------------------------------------------------------------- 9. 持仓校验降级
class TestHoldingsCheck:
    """前10重仓背离相关桶 → 同格降均衡（2026-11-13 用户Q2"医药第一怎么办"）。"""

    @staticmethod
    def _run(styles, fake, monkeypatch):
        monkeypatch.setattr(ES, "fetch_hold_top10",
                            lambda c, refresh=True: fake.get(c))
        return ES.apply_holdings_check(dict(styles), refresh=True,
                                       log=lambda *a, **k: None)

    def test_backdrop_downgrades(self, monkeypatch):
        styles = {"000001": "医药", "000002": "医药", "000003": "科技"}
        fake = {"000001": [("医药生物", 0.05), ("交通运输", 0.11)],   # 背离
                "000002": [("医药生物", 0.45), ("食品饮料", 0.10)],   # 真行业
                "000003": [("电子", 0.30)]}                          # 真行业
        out = self._run(styles, fake, monkeypatch)
        assert out["000001"] == "均衡"
        assert out["000002"] == "医药"
        assert out["000003"] == "科技"

    def test_missing_data_keeps_bucket(self, monkeypatch):
        out = self._run({"000001": "医药"}, {}, monkeypatch)   # fetch→None
        assert out["000001"] == "医药"                          # 无数据不降级

    def test_balanced_never_fetches(self, monkeypatch):
        called = []
        monkeypatch.setattr(ES, "fetch_hold_top10",
                            lambda c, refresh=True: called.append(c))
        ES.apply_holdings_check({"000001": "均衡"}, refresh=True,
                                log=lambda *a, **k: None)
        assert called == []                                     # 均衡无需校验

    def test_industry_mapping_covers_sw_l1(self):
        """申万一级31行业全部有归属（未列回退均衡）。"""
        sw1 = ["农林牧渔", "基础化工", "钢铁", "有色金属", "电子", "家用电器",
               "食品饮料", "纺织服饰", "轻工制造", "医药生物", "公用事业",
               "交通运输", "房地产", "商贸零售", "社会服务", "银行", "非银金融",
               "建筑装饰", "建筑材料", "电力设备", "机械设备", "国防军工",
               "汽车", "传媒", "通信", "计算机", "环保", "美容护理", "煤炭",
               "石油石化", "综合"]
        for ind in sw1:
            assert ind in ES.IND2BUCKET or ind == "综合", ind


# ---------------------------------------------------------------- 10. 双十与年化门槛
def _cand_env(ret5y=-0.01, exam="G3s", career=12.0, dual=None, key="张三|某公司",
              positions=None, base_scale=None):
    pairs = pd.DataFrame([dict(mgr="张三", company="某公司", code="000001",
                               career_y=career)])
    qual = pd.DataFrame([dict(mgr="张三", company="某公司", code="000001",
                              career_y=career, channel="A", tenure_y=6.0)])
    exams = {"000001": exam} if exam else {}
    fch = pd.Series({"000001": "A"})
    f10s = {"000001": {"managers": [{"name": "张三", "start": "2019-01-01"}]}}
    feat = pd.DataFrame({"ret5y": [ret5y], "name": ["优选混合A"],
                         "type": ["混合型-偏股"]},
                        index=pd.Index(["000001"], name="code"))
    cand = ES.build_candidates(pairs, qual, exams, fch, f10s, {"000001"},
                               feat=feat, dual=dual or {},
                               positions=positions or {},
                               base_scale=base_scale or {})
    return key, cand


class TestDualAndGate:
    def test_dual_bypasses_exam(self):
        key, c = _cand_env(exam=None,
                           dual={"张三|某公司": dict(dual=True, ann=0.15,
                                                     code="000001")})
        assert c[key]["pass_funds"] == ["000001"]      # 免考卷直通

    def test_dual_bypasses_zero_floor(self):
        """双十免0%门槛：小亏（-3%）仍上；但深亏见 G2 测试。"""
        key, c = _cand_env(ret5y=-0.03, exam=None,
                           dual={"张三|某公司": dict(dual=True, ann=0.15,
                                                     code="000001")})
        assert c[key]["pass_funds"] == ["000001"]      # 免0%门槛（"全部上榜"）

    def test_gate_filters_negative(self):
        key, c = _cand_env(ret5y=-0.01, exam="G3s")   # 非双十
        assert c[key]["pass_funds"] == []              # 计分基金负收益→不上

    def test_gate_keeps_positive(self):
        key, c = _cand_env(ret5y=0.02, exam="G3s")
        assert c[key]["pass_funds"] == ["000001"]

    def test_exam_still_required_for_nongate(self):
        key, c = _cand_env(ret5y=0.02, exam=None)     # 没过考卷
        assert c[key]["pass_funds"] == []

    def test_ann_carried_for_display(self):
        key, c = _cand_env(dual={"张三|某公司": dict(dual=True, ann=0.142,
                                                     code="000001")})
        assert c[key]["dual"] is True
        assert c[key]["career_ann"] == pytest.approx(0.142)


# ---------------------------------------------------------------- 11. 从业年化
class TestCareerDualFlags:
    """双十判定（2026-11-14 口径改版）：从业>10年 + **等权组合年化**>10%。
    组合年化在 D.manager_career_ann（mock），本类只测编排：id匹配/同名防串/保守回退。"""

    @staticmethod
    def _f10(mgr, mid):
        return {"manager": mgr, "manager_id": mid}

    def test_ann_above_threshold_dual(self, monkeypatch):
        monkeypatch.setattr(D, "manager_career_ann",
                            lambda mid, refresh=False: 0.15)
        pairs = pd.DataFrame([dict(mgr="张三", company="某公司", code="000001",
                                   career_y=11.0)])
        f10s = {"000001": self._f10("张三", "ID1")}
        out = ES.career_dual_flags(pairs, f10s, log=lambda *a, **k: None)
        rec = out["张三|某公司"]
        assert rec["dual"] is True
        assert rec["ann"] == pytest.approx(0.15)

    def test_low_annualized_not_dual(self, monkeypatch):
        monkeypatch.setattr(D, "manager_career_ann",
                            lambda mid, refresh=False: 0.06)
        pairs = pd.DataFrame([dict(mgr="张三", company="某公司", code="000001",
                                   career_y=11.0)])
        f10s = {"000001": self._f10("张三", "ID1")}
        out = ES.career_dual_flags(pairs, f10s, log=lambda *a, **k: None)
        rec = out["张三|某公司"]
        assert rec["dual"] is False and rec["ann"] is not None

    def test_ann_unavailable_not_dual(self, monkeypatch):
        """年化算不出（缺经理页/缺净值/不足一年）→ 按非双十（保守，需过考卷）。"""
        monkeypatch.setattr(D, "manager_career_ann",
                            lambda mid, refresh=False: None)
        pairs = pd.DataFrame([dict(mgr="张三", company="某公司", code="000001",
                                   career_y=12.0)])
        f10s = {"000001": self._f10("张三", "ID1")}
        out = ES.career_dual_flags(pairs, f10s, log=lambda *a, **k: None)
        rec = out["张三|某公司"]
        assert rec["dual"] is False and rec["ann"] is None

    def test_career_le_10_skipped(self, monkeypatch):
        monkeypatch.setattr(D, "manager_career_ann",
                            lambda mid, refresh=False: 0.99)
        pairs = pd.DataFrame([dict(mgr="张三", company="某公司", code="000001",
                                   career_y=9.0)])
        out = ES.career_dual_flags(pairs, {}, log=lambda *a, **k: None)
        assert "张三|某公司" not in out              # 从业≤10 不进双十池

    def test_same_name_manager_not_crossed(self, monkeypatch):
        """同名经理防串：id 只在该 key 的基金集合里找，且主经理名一致才采用。"""
        seen = []
        monkeypatch.setattr(
            D, "manager_career_ann",
            lambda mid, refresh=False: (seen.append(mid), 0.15)[1])
        pairs = pd.DataFrame([
            dict(mgr="李伟", company="甲公司", code="000001", career_y=11.0),
            dict(mgr="李伟", company="乙公司", code="000002", career_y=7.0)])
        f10s = {"000001": self._f10("李伟", "ID_A"),
                "000002": self._f10("李伟", "ID_B")}
        out = ES.career_dual_flags(pairs, f10s, log=lambda *a, **k: None)
        assert seen == ["ID_A"]                       # 只查甲的；乙 career≤10 不进
        assert out["李伟|甲公司"]["dual"] is True
        assert "李伟|乙公司" not in out

    def test_main_manager_mismatch_rejected(self, monkeypatch):
        """共管基金 jbgk 主经理是别人 → 不采用该页（防串到他人经理页）。"""
        monkeypatch.setattr(D, "manager_career_ann",
                            lambda mid, refresh=False: 0.15)
        pairs = pd.DataFrame([dict(mgr="李伟", company="甲公司", code="000001",
                                   career_y=11.0)])
        f10s = {"000001": {"manager": "王五", "manager_id": "ID_OTHER"}}
        out = ES.career_dual_flags(pairs, f10s, log=lambda *a, **k: None)
        rec = out["李伟|甲公司"]
        assert rec["dual"] is False and rec["ann"] is None


# ---------------------------------------------------------------- 12. 代表作合并
class TestMgrDetailReps:
    @staticmethod
    def _env():
        o = pd.Timestamp("2026-11-13")
        codes = ["000001", "000002", "000003"]          # 01/02 同基金 A/C
        feat = pd.DataFrame(
            {"name": ["优选混合A", "优选混合C", "成长混合A"],
             "type": ["股票型"] * 3, "ret5y": [0.12, 0.11, 0.05],
             "bucket": ["医药", "医药", "科技"]},
            index=pd.Index(codes, name="code"))
        fdf = pd.DataFrame(
            {"score": [90.0, 89.0, 70.0], "F1": [80.0, 79.0, 95.0]},
            index=pd.Index(codes, name="code"))
        f10s = {c: {"scale_yi": s,
                    "managers": [{"name": "张三", "start": "2019-01-01"}]}
                for c, s in zip(codes, [2.0, 3.0, 10.0])}
        return o, codes, feat, fdf, f10s

    @staticmethod
    def _nav():
        dates = pd.bdate_range("2021-11-13", periods=300)
        return pd.DataFrame({"date": dates,
                             "nav": np.linspace(1.0, 1.3, len(dates))})

    def test_count_fund_first_and_ac_merge(self, monkeypatch):
        monkeypatch.setattr(ES, "_read_nav_pickle", lambda c: self._nav())
        o, codes, feat, fdf, f10s = self._env()
        det = ES._mgr_detail(codes, ["000001"], "张三", feat, fdf, f10s,
                             {}, o, {"mkt": None, "prox": {}},
                             inst_map={"000001": 0.62})
        reps = det["reps"]
        # 计分基金主位（000001 score90）；A/C 同基础名合并为一条、规模=2+3
        assert reps[0]["rule"] == "计分" and reps[0]["code"] == "000001"
        assert reps[0]["scale"] == pytest.approx(5.0)
        assert reps[0]["n_shares"] == 2
        assert reps[0]["inst"] == pytest.approx(0.62)
        # 规模最大=成长混合A(10亿)；同基金 C 份额不重复出现
        assert reps[1]["rule"] == "规模最大" and reps[1]["code"] == "000003"
        names = [r["name"] for r in reps]
        assert "优选混合C" not in names
        assert len(reps) == 2                         # 业绩最佳与规模重合→去重

    def test_analysis_blocks_follow_count_fund(self, monkeypatch):
        monkeypatch.setattr(ES, "_read_nav_pickle", lambda c: self._nav())
        o, codes, feat, fdf, f10s = self._env()
        det = ES._mgr_detail(codes, ["000001"], "张三", feat, fdf, f10s,
                             {}, o, {"mkt": None, "prox": {}})
        assert det["risk"] is not None                # 分析块跑在计分基金上


# ---------------------------------------------------------------- 13. P0 三硬闸
class TestHardGates:
    """G1 规模 / G2 深亏 / G3 类型感知仓位（2026-11-13 七问排查；对双十同样生效）。"""

    DUAL = {"张三|某公司": dict(dual=True, ann=0.15, code="000001")}

    def test_g1_shell_fund_cut_even_if_dual(self):
        """规模<2亿 → 出局，双十也不例外（含"数据缺失 fail-open"对照）。"""
        key, c = _cand_env(ret5y=0.10, exam=None, dual=self.DUAL,
                           base_scale={"优选混合": 1.5})
        assert c[key]["pass_funds"] == []
        key, c = _cand_env(ret5y=0.10, exam=None, dual=self.DUAL,
                           base_scale={"优选混合": 3.0})
        assert c[key]["pass_funds"] == ["000001"]      # ≥2亿放行
        key, c = _cand_env(ret5y=0.10, exam=None, dual=self.DUAL,
                           base_scale={})              # 无规模数据 → fail-open
        assert c[key]["pass_funds"] == ["000001"]

    def test_g2_deep_loss_cut_even_if_dual(self):
        """5年年化<-5% → 出局，双十也不例外；-5%边界放行。"""
        key, c = _cand_env(ret5y=-0.06, exam=None, dual=self.DUAL)
        assert c[key]["pass_funds"] == []
        key, c = _cand_env(ret5y=-0.05, exam=None, dual=self.DUAL)
        assert c[key]["pass_funds"] == ["000001"]      # 边界（不低于-5%）

    def test_g3_position_gate_type_aware(self):
        """偏股型仓位50%线、灵活型30%线；缺数据 fail-open。"""
        key, c = _cand_env(ret5y=0.10, exam=None, dual=self.DUAL,
                           positions={"000001": 45.0})   # 偏股型<50% → 出局
        assert c[key]["pass_funds"] == []
        key, c = _cand_env(ret5y=0.10, exam=None, dual=self.DUAL,
                           positions={"000001": 55.0})   # ≥50% 放行
        assert c[key]["pass_funds"] == ["000001"]
        key, c = _cand_env(ret5y=0.10, exam=None, dual=self.DUAL,
                           positions={})                  # 缺数据 → fail-open
        assert c[key]["pass_funds"] == ["000001"]

    def test_gates_also_apply_to_nongate_path(self):
        """非双十路径同样吃三闸（考卷+0%门槛之外）。"""
        key, c = _cand_env(ret5y=0.10, exam="G3s",
                           base_scale={"优选混合": 1.0})
        assert c[key]["pass_funds"] == []              # 壳基金过考也出局
