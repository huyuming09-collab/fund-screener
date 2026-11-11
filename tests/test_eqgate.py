# -*- coding: utf-8 -*-
"""偏股资深经理闸 v1 契约测试（docs/20 §2.1）。

全部用合成数据，不走网络：验证 S1 池过滤、S2 双通道边界、S3/S4 闸、
B 通道分位判定顺序、经理级聚合与漏斗计数。
"""
import pandas as pd
import pytest

from fundscorer import eqgate as G


# ---------------------------------------------------------------- S2 通道边界
def test_channel_boundaries():
    assert G.channel(10.5) == "A"        # >10 直通
    assert G.channel(10.0) == "B"        # 恰 10.0 不算">10" → 走前10%通道
    assert G.channel(6.01) == "B"        # >6 走前10%通道
    assert G.channel(6.0) == "out"       # 恰 6.0 不算">6" → 出闸
    assert G.channel(3.0) == "out"
    assert G.channel(None) == "unknown"
    assert G.channel(float("nan")) == "unknown"


def test_career_years_units():
    # mgr_em 数值=天；'X年' 文本兼容；坏值不猜测
    assert G._career_years(3650) == pytest.approx(10.0)
    assert G._career_years("3650") == pytest.approx(10.0)
    assert G._career_years("12年") == pytest.approx(12.0)
    assert G._career_years("") is None
    assert G._career_years("无数据") is None


# ---------------------------------------------------------------- 池与从业对
def _pool():
    return pd.DataFrame([
        {"code": "000001", "name": "甲成长", "type_text": "股票型"},
        {"code": "000002", "name": "乙均衡", "type_text": "混合型-偏股"},
        {"code": "000003", "name": "丙灵活", "type_text": "混合型-灵活"},
        {"code": "000004", "name": "丁平衡", "type_text": "混合型-平衡"},
        {"code": "000005", "name": "戊短债", "type_text": "债券型-中短债"},  # S1 应滤除
    ])


def test_load_pool_filters_types():
    # load_pool 走网络版的类型过滤逻辑（本地复刻同一谓词）
    arr = [["000001", "u", "甲成长", "股票型", "PY"],
           ["000005", "u", "戊短债", "债券型-中短债", "PY"],
           ["000006", "u", "己QDII", "QDII-混合偏股", "PY"]]
    rows = [{"code": str(c).zfill(6), "name": n, "type_text": t}
            for c, _u, n, t, _py in arr if t in G.POOL_TYPES]
    df = pd.DataFrame(rows)
    assert list(df["code"]) == ["000001"]          # 只剩股票型


def test_career_pairs_dedup_and_join():
    tbl = pd.DataFrame([
        {"姓名": "张三", "所属公司": "甲公司", "现任基金代码": "1", "累计从业时间": 4015},
        {"姓名": "张三", "所属公司": "甲公司", "现任基金代码": "000001", "累计从业时间": 4015},
        {"姓名": "李四", "所属公司": "乙公司", "现任基金代码": 2.0, "累计从业时间": 1825},
        {"姓名": "王五", "所属公司": "丙公司", "现任基金代码": "000009", "累计从业时间": 1000},
    ])
    pairs = G.career_pairs(tbl, ["000001", "000002"])
    # 000009 不在池内 → 剔除；'1' 与 '000001' zfill 后同码去重
    assert set(pairs["code"]) == {"000001", "000002"}
    assert len(pairs) == 2
    zhang = pairs[pairs["mgr"] == "张三"].iloc[0]
    assert zhang["career_y"] == pytest.approx(4015 / 365.0, abs=0.01)


# ---------------------------------------------------------------- S4 + 分位
def test_prep_s4_and_p90():
    pool = pd.DataFrame([
        {"code": f"{i:06d}", "name": f"f{i}", "type_text": "股票型"}
        for i in range(20)
    ])
    # 18 只满 5 年（r5 递增 1%~18%），2 只只有 3 年（S4 剔除）
    navs = {f"{i:06d}": (5.0 if i < 18 else 3.0, (i + 1) / 100.0 if i < 18 else 0.5)
            for i in range(20)}
    pairs = pd.DataFrame([{"mgr": "张三", "company": "甲", "code": "000019",
                           "career_y": 12.0}])       # 只给一只 S4 不足的基金配对
    pool_a, pf, p90 = G.prep(pool, pairs, navs)
    assert int(pool_a["s4"].sum()) == 18             # 2 只 3 年基金被 S4 剔除
    assert p90["股票型"] == pytest.approx(
        pd.Series([(i + 1) / 100.0 for i in range(18)]).quantile(0.90))
    # pair 富化：3 年基金的 s4=False → 不会进闸
    assert bool(pf.iloc[0]["s4"]) is False


def test_f10_targets_selects_direct_and_top10():
    pool = pd.DataFrame([
        {"code": "000001", "name": "a", "type_text": "股票型",
         "ny": 6.0, "r5": 0.05, "s4": True, "pctl": 50.0},
        {"code": "000002", "name": "b", "type_text": "股票型",
         "ny": 6.0, "r5": 0.20, "s4": True, "pctl": 95.0},
        {"code": "000003", "name": "c", "type_text": "股票型",
         "ny": 6.0, "r5": 0.01, "s4": True, "pctl": 5.0},
        {"code": "000004", "name": "d", "type_text": "股票型",
         "ny": 3.0, "r5": None, "s4": False, "pctl": None},
    ])
    pairs = pd.DataFrame([
        {"mgr": "A经理", "company": "甲", "code": "000001", "career_y": 12.0},  # 通道A
        {"mgr": "B经理", "company": "乙", "code": "000002", "career_y": 8.0},   # B 达标
        {"mgr": "C经理", "company": "丙", "code": "000003", "career_y": 8.0},   # B 不达标
        {"mgr": "D经理", "company": "丁", "code": "000004", "career_y": 12.0},  # S4 不足
        {"mgr": "E经理", "company": "戊", "code": "000001", "career_y": 4.0},   # ≤6 出闸
    ])
    _p, pf, p90 = G.prep(pool, pairs, {c: (r["ny"], r["r5"])
                                       for c, r in pool.set_index("code").iterrows()})
    p90["股票型"] = 0.15   # 人工阈值：000002(0.20) 达标、000003(0.01) 不达标
    targets = G.f10_targets(pf, p90)
    assert targets == ["000001", "000002"]   # A∩S4 + B达标∩S4；C/D/E 不抓


# ---------------------------------------------------------------- 闸本体
def _gate_fixture():
    pool = pd.DataFrame([
        {"code": "000001", "name": "甲成长", "type_text": "股票型",
         "ny": 6.0, "r5": 0.20, "s4": True, "pctl": 95.0},
        {"code": "000002", "name": "乙均衡", "type_text": "混合型-偏股",
         "ny": 6.0, "r5": 0.10, "s4": True, "pctl": 80.0},
        {"code": "000003", "name": "丙灵活", "type_text": "混合型-灵活",
         "ny": 6.0, "r5": 0.02, "s4": True, "pctl": 10.0},
    ])
    pairs = pd.DataFrame([
        # 通道A 老将：f10 现任、任期 8 年 → 通过
        {"mgr": "张三", "company": "甲公司", "code": "000001", "career_y": 15.0},
        # 通道A 但任期只有 2 年 → S3 拦下
        {"mgr": "李四", "company": "乙公司", "code": "000002", "career_y": 12.0},
        # 通道B 分位达标(0.10≥p90=0.08)、任期 6 年 → 通过
        {"mgr": "王五", "company": "丙公司", "code": "000002", "career_y": 8.0},
        # 通道B 分位不足(0.02<0.08) → 拦下
        {"mgr": "赵六", "company": "丁公司", "code": "000003", "career_y": 7.0},
        # ≤6 年 → 出闸
        {"mgr": "钱七", "company": "戊公司", "code": "000001", "career_y": 5.0},
        # 通道A 但 f10 里已非现任（不在 managers 列表）→ 拦下
        {"mgr": "孙八", "company": "己公司", "code": "000003", "career_y": 11.0},
    ])
    f10s = {
        "000001": {"managers": [{"name": "张三", "id": "1", "start": "2016-01-01"}]},
        "000002": {"managers": [{"name": "李四", "id": "2", "start": "2024-06-01"},
                                {"name": "王五", "id": "3", "start": "2019-06-01"}]},
        "000003": {"managers": [{"name": "赵六", "id": "4", "start": "2018-01-01"}]},
    }
    p90 = {"股票型": 0.15, "混合型-偏股": 0.08, "混合型-灵活": 0.08}
    bench = {"股票型": 0.05, "混合型-偏股": 0.04,
             "混合型-灵活": 0.03, "混合型-平衡": 0.03}
    return pool, p90, f10s, bench


def test_run_gate_pass_and_funnel():
    pool, _p90, f10s, bench = _gate_fixture()
    pairs = pd.DataFrame([
        {"mgr": "张三", "company": "甲公司", "code": "000001", "career_y": 15.0},
        {"mgr": "李四", "company": "乙公司", "code": "000002", "career_y": 12.0},
        {"mgr": "王五", "company": "丙公司", "code": "000002", "career_y": 8.0},
        {"mgr": "赵六", "company": "丁公司", "code": "000003", "career_y": 7.0},
        {"mgr": "钱七", "company": "戊公司", "code": "000001", "career_y": 5.0},
        {"mgr": "孙八", "company": "己公司", "code": "000003", "career_y": 11.0},
    ])
    pool, pf, p90 = G.prep(pool, pairs,
                           {"000001": (6.0, 0.20), "000002": (6.0, 0.10),
                            "000003": (6.0, 0.02)})
    # prep 的分位要求同类≥10 只，本夹具每类仅 1 只 → 人工给阈值（验证判定逻辑本身）
    p90 = {"股票型": 0.15, "混合型-偏股": 0.08, "混合型-灵活": 0.08}
    funnel, mgrs, funds = G.run_gate(pool, pf, p90, f10s, bench)

    # 通过：张三（A通道）、王五（B通道达标）
    assert set(zip(funds["mgr"], funds["code"])) == {("张三", "000001"),
                                                     ("王五", "000002")}
    assert funnel["通过_pair"] == 2
    assert funnel["通过_基金"] == 2
    assert funnel["通过_经理"] == 2
    # 拦截归类
    assert funnel["S3_非现任或任期不足"] == 2      # 李四(任期2年) + 孙八(非现任)
    assert funnel["B通道_分位不足"] == 1           # 赵六
    assert funnel["S2_出闸_pair_≤6年"] == 1        # 钱七
    # 经理榜聚合：A 通道在前
    assert list(mgrs["mgr"]) == ["张三", "王五"]
    assert list(mgrs["channel"]) == ["A", "B"]
    # 超额 = r5 − 同类基准
    row = funds[funds["mgr"] == "张三"].iloc[0]
    assert row["excess_5y"] == pytest.approx(0.20 - 0.05)


def test_run_gate_empty():
    pool = pd.DataFrame(columns=["code", "name", "type_text", "ny", "r5"])
    pairs = pd.DataFrame(columns=["mgr", "company", "code", "career_y"])
    pool, pf, p90 = G.prep(pool, pairs, {})
    funnel, mgrs, funds = G.run_gate(pool, pf, p90, {}, {})
    assert funnel["通过_pair"] == 0
    assert mgrs.empty and funds.empty


def test_composite_bench_ann_from_between_components():
    # 合成基准年化应落在两成分之间（债 2% < 合成 < 股 8%）
    idx = pd.date_range("2019-01-01", periods=1600, freq="B")
    hs = pd.Series([1.00031 ** i for i in range(1600)], index=idx)   # ~8%/年
    bond = pd.Series([1.00005 ** i for i in range(1600)], index=idx)  # ~1.3%/年
    v = G.composite_bench_ann_from(hs, bond, w=0.7, years=5)
    assert 0.013 < v < 0.08
    # 股权重越高越接近股市收益
    v90 = G.composite_bench_ann_from(hs, bond, w=0.9, years=5)
    assert v90 > v
