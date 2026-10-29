# -*- coding: utf-8 -*-
"""data.py 纯函数（F10/经理页解析）单测——全部离线，不触发任何抓取。"""
from fundscorer import data as D

# fundf10/jjjl_*.html 的"现任基金经理简介"区结构（2026-10-19 实抓样本裁剪）：
# 每位现任经理一个 jl_intro 块：经理链接 + 姓名 + 上任日期。
JJJL_TWO = '''
<div class="boxitem w790"><h4 class="t"><label class="left">现任基金经理简介</label></h4>
<div class="jl_intro"><a href="//fund.eastmoney.com/manager/30506386.html"><img src="a.jpg"/></a><div class="text">
<p><strong>姓名：</strong><a style='color:#333;' href="//fund.eastmoney.com/manager/30506386.html">张城源</a></p>
<p><strong>上任日期：</strong>2016-12-26</p><p>简介文本……</p></div></div>
<div class="jl_intro"><a href="//fund.eastmoney.com/manager/30199875.html"><img src="b.jpg"/></a><div class="text">
<p><strong>姓名：</strong><a style='color:#333;' href="//fund.eastmoney.com/manager/30199875.html">毛颖</a></p>
<p><strong>上任日期：</strong>2017-01-18</p></div></div>
</div>'''

JJJL_ONE_NO_START = '''
<div class="jl_intro"><a href="//fund.eastmoney.com/manager/30131213.html"><img src="c.jpg"/></a><div class="text">
<p><strong>姓名：</strong><a href="//fund.eastmoney.com/manager/30131213.html">王晓晨</a></p>
<p>（无上任日期字段）</p></div></div>'''


def test_parse_jjl_intro_two_managers():
    """共管基金：每位现任经理一条，姓名/ID/上任日期成对（此前只记第一位）。"""
    out = D._parse_jjl_intro(JJJL_TWO)
    assert [x["name"] for x in out] == ["张城源", "毛颖"]
    assert out[0]["id"] == "30506386" and out[0]["start"] == "2016-12-26"
    assert out[1]["id"] == "30199875" and out[1]["start"] == "2017-01-18"


def test_parse_jjl_intro_single_without_start():
    out = D._parse_jjl_intro(JJJL_ONE_NO_START)
    assert len(out) == 1
    assert out[0]["name"] == "王晓晨" and out[0]["id"] == "30131213"
    assert out[0]["start"] == ""            # 字段缺失 → 空串（下游 tenure=None）


def test_parse_jjl_intro_empty_and_broken():
    assert D._parse_jjl_intro("") == []
    # 无经理链接的块跳过；缺姓名的块跳过（数据不足不猜测）
    broken = ('<div class="jl_intro">无链接块</div>'
              '<div class="jl_intro"><a href="//fund.eastmoney.com/manager/1.html">x</a>'
              '<p><strong>上任日期：</strong>2020-01-01</p></div>')
    assert D._parse_jjl_intro(broken) == []


def test_is_bond_like(monkeypatch):
    # v5.6 经理产品口径（2026-10-20 拍板）：权益产品不参与经理全产品年化中位数
    monkeypatch.setattr(D, "_fund_types", lambda: {
        "001257": "债券型-混合二级", "110007": "混合型-偏债",
        "161725": "混合型-偏股", "001186": "股票型-普通股票"})
    assert D._is_bond_like("001257", "兴业收益增强债券")
    assert D._is_bond_like("110007", "易方达稳健收益混合")
    assert not D._is_bond_like("161725", "某某成长混合")
    assert not D._is_bond_like("001186", "某某股票精选")
    # 类型表未覆盖（含已离任/清盘的历史产品）→ 名称含"债"兜底，不猜测
    assert D._is_bond_like("999991", "某增强回报债券")
    assert not D._is_bond_like("999992", "某均衡配置混合")


def test_fetch_f10_reads_and_writes_same_v3_key(monkeypatch):
    """回归（2026-10-20 评审处置中发现）：fetch_f10 读端曾留在 v2、写端已在 v3——
    旧 v2 缓存 7 天内直接命中 → 永不重抓、managers 永远解析不出来，
    v5.5 共管与 v5.7 团队系数全失效。读写必须同键。"""
    loaded, saved = [], []
    monkeypatch.setattr(D, "_load_cache",
                        lambda key, ttl: (loaded.append(key), None)[1])
    monkeypatch.setattr(D, "_save_cache",
                        lambda key, info: saved.append(key))
    monkeypatch.setattr(D, "_http", lambda *a, **k: "<th>基金简称</th><td>测试债券A</td>")
    D.fetch_f10("000001")
    assert loaded == ["f10_v3_000001"]
    assert saved == ["f10_v3_000001"]


class TestPurchaseStatus:
    """申购状态（2026-10-20 用户要求：暂停申购在卡片标注）。"""

    HTML_LIM = ('<span>交易状态：</span><span class="staticCell">限大额  '
                '(<span>单日累计购买上限5000.00万元</span>)</span>')
    HTML_SUS = '<span>交易状态：</span><span class="staticCell">暂停申购</span>'

    def test_parse_sgzt(self):
        assert D._parse_sgzt(self.HTML_LIM) == "限大额"
        assert D._parse_sgzt(self.HTML_SUS) == "暂停申购"
        assert D._parse_sgzt("<html>没有状态字段</html>") is None

    def test_sgzt_suspended(self):
        assert D.sgzt_suspended("暂停申购") is True
        assert D.sgzt_suspended("暂停交易") is True
        assert D.sgzt_suspended("封闭期") is True
        assert D.sgzt_suspended("未开放") is True
        assert D.sgzt_suspended("限大额") is False   # 限大额仍可小额申购，不标"不能申购"
        assert D.sgzt_suspended("开放申购") is False
        assert D.sgzt_suspended(None) is False

    def test_fetch_readonly_skips_network(self, monkeypatch):
        # --skip-refresh 语义：只读缓存，缓存缺失也不联网
        monkeypatch.setattr(D, "_load_cache", lambda key, h: None)

        def boom(*a, **k):
            raise AssertionError("readonly 模式不得联网")
        monkeypatch.setattr(D, "_http", boom)
        assert D.fetch_purchase_status(["000001"], refresh=False) == {}

    def test_fetch_refresh_cache_hit_and_new(self, monkeypatch):
        saved = {}
        monkeypatch.setattr(D, "_load_cache",
                            lambda key, h: "开放申购" if key == "sgzt_v1_000002" else None)
        monkeypatch.setattr(D, "_save_cache", lambda key, obj: saved.update({key: obj}))
        monkeypatch.setattr(D, "_http", lambda *a, **k: self.HTML_SUS)
        out = D.fetch_purchase_status(["000001", "000002"], refresh=True, sleep_s=0)
        assert out == {"000001": "暂停申购", "000002": "开放申购"}
        assert saved == {"sgzt_v1_000001": "暂停申购"}


# ---- 2026-10-21 经理任期口径修复（用户发现某长债 top1 经理实际任期<2年却过了 S3 闸；
# 当日二改：用户校正"共管期也算真实任期"——任职记录表每行可含多位共管经理，口径 =
# 连续在任（含共管期），与经理侧历任表一致）----
# 任职记录表实抓样本裁剪自 006212（注意 2025-03-05~2025-12-16 行内两人共管）：
JJJL_TABLE_006212 = '''
<table class="w790"><tbody>
<tr><td>2025-12-17</td><td>至今</td><td><a href='http://fund.eastmoney.com/manager/30437326.html'>吴萍萍</a> </td><td>308天</td></tr>
<tr><td>2025-03-05</td><td>2025-12-16</td><td><a href='http://fund.eastmoney.com/manager/30500700.html'>刘长俊</a> <a href='http://fund.eastmoney.com/manager/30437326.html'>吴萍萍</a> </td><td>286天</td></tr>
<tr><td>2023-10-25</td><td>2025-03-04</td><td><a href='http://fund.eastmoney.com/manager/30500700.html'>刘长俊</a> </td><td>1年又131天</td></tr>
<tr><td>2021-01-28</td><td>2023-10-24</td><td><a href='http://fund.eastmoney.com/manager/30698282.html'>车日楠</a> </td><td>2年又269天</td></tr>
<tr><td>2020-04-30</td><td>2021-01-27</td><td><a href='http://fund.eastmoney.com/manager/30437326.html'>吴萍萍</a> <a href='http://fund.eastmoney.com/manager/30698282.html'>车日楠</a> </td><td>272天</td></tr>
<tr><td>2019-04-30</td><td>2020-04-29</td><td><a href='http://fund.eastmoney.com/manager/30437326.html'>吴萍萍</a> </td><td>1年又0天</td></tr>
<tr><td>2018-08-24</td><td>2019-02-28</td><td><a href='http://fund.eastmoney.com/manager/30437326.html'>吴萍萍</a> </td><td>188天</td></tr>
</tbody></table>'''

JJJL_INTRO_STALE = '''
<div class="jl_intro"><a href="//fund.eastmoney.com/manager/30437326.html"><img src="a.jpg"/></a><div class="text">
<p><strong>姓名：</strong><a style='color:#333;' href="//fund.eastmoney.com/manager/30437326.html">吴萍萍</a></p>
<p><strong>上任日期：</strong>2018-08-24</p></div></div>'''


class TestMgrTenureContinuousRun:
    """经理任本基金口径 = **连续在任（含共管期）**（2026-10-21 二改，用户校正）。

    - jl_intro 简介日期是首次上任日，回归接管时不更新 → 不作数（虚增）；
    - 任职表"至今"行起始只覆盖独任期 → 漏共管期（少算）；
    - 正确 = 表内全部区间按经理合并，与"至今"段无缝相接（间隔 ≤7 天）的往期并入。
    例：006212 东方臻选 吴萍萍 = 2018-08~2021-01 离任 + 2025-03 增聘共管 +
    2025-12 转独任 → 真实任期 2025-03-05 起 ≈1.63 年（经理侧历任表同口径）。
    """

    def test_parse_multi_manager_rows(self):
        st = D._parse_manager_stints(JJJL_TABLE_006212)
        # 共管行同时记入两位经理
        assert ["2025-03-05", "2025-12-16"] in st["30437326"]
        assert ["2025-03-05", "2025-12-16"] in st["30500700"]
        assert ["2025-12-17", ""] in st["30437326"]   # "" = 至今（表格自上而下倒序）

    def test_continuous_run_includes_co_mgmt(self):
        st = D._parse_manager_stints(JJJL_TABLE_006212)
        # 吴萍萍：共管段无缝并入 → 2025-03-05（非"至今"行 2025-12-17，更非简介 2018）
        assert D._current_run_start(st["30437326"]) == "2025-03-05"

    def test_old_stint_with_gap_not_merged(self):
        st = D._parse_manager_stints(JJJL_TABLE_006212)
        # 刘长俊：无"至今"行 → 非现任
        assert D._current_run_start(st["30500700"]) is None

    def test_reconcile_overrides_stale_intro(self):
        info = {"manager_id": "30437326", "manager_start": "2025-12-17",
                "managers": [{"name": "吴萍萍", "id": "30437326", "start": "2018-08-24"}],
                "current_stints": {"30437326": "2025-03-05"}}
        assert D._reconcile_mgr_starts(info)["managers"][0]["start"] == "2025-03-05"

    def test_reconcile_fallback_primary_only(self):
        # 旧缓存无 current_stints：回退 主经理 ↔ 独任期起点（保守，可能少共管期）
        info = {"manager_id": "9", "manager_start": "2025-12-17",
                "managers": [{"name": "甲", "id": "9", "start": "2018-08-24"}]}
        assert D._reconcile_mgr_starts(info)["managers"][0]["start"] == "2025-12-17"

    def test_reconcile_noop_without_managers(self):
        info = {"manager_id": "1", "manager_start": "2024-01-01"}
        assert D._reconcile_mgr_starts(info) == info


# ---------------------------------------------------------------- v5.22 指数估值建库
class TestFetchF10Options:
    """指数估值建库用 with_manager=False 省掉 jjjl 页（且不得污染债券池读端）。"""

    JBGK = ('<table><tr><th>基金简称</th><td>测试指数A</td></tr>'
            '<tr><th>基金类型</th><td>指数型-股票</td></tr>'
            '<tr><th>成立日期/规模</th><td>2019年01月01日</td></tr>'
            '<tr><th>净资产规模</th><td>12.34亿元</td></tr>'
            '<tr><th>管理费率</th><td>0.50%</td></tr>'
            '<tr><th>托管费率</th><td>0.10%</td></tr>'
            '<tr><th>销售服务费率</th><td>0.00%</td></tr>'
            '<tr><th>业绩比较基准</th><td>沪深300指数收益率*95%+银行活期存款利率(税后)*5%</td></tr>'
            '<tr><th>基金经理人</th><td><a>张三</a></td></tr></table>')

    def test_skip_manager_avoids_jjjl_request(self, monkeypatch):
        calls, saved = [], {}
        monkeypatch.setattr(D, "_load_cache", lambda key, hours: None)
        monkeypatch.setattr(D, "_save_cache", lambda key, obj: saved.update({key: obj}))

        def fake_http(url, encoding="gbk", referer=None):
            calls.append(url)
            return self.JBGK

        monkeypatch.setattr(D, "_http", fake_http)
        info = D.fetch_f10("999001", with_manager=False)
        assert info["benchmark"].startswith("沪深300指数")
        assert "managers" not in info
        assert len(calls) == 1 and "/jbgk_" in calls[0]
        assert "f10_v3_999001" in saved

    def test_manager_request_refetches_managerless_cache(self, monkeypatch):
        calls = []
        monkeypatch.setattr(D, "_load_cache",
                            lambda key, hours: {"code": "999002", "benchmark": "x"})
        monkeypatch.setattr(D, "_save_cache", lambda key, obj: None)

        def fake_http(url, encoding="gbk", referer=None):
            calls.append(url)
            return self.JBGK

        monkeypatch.setattr(D, "_http", fake_http)
        info = D.fetch_f10("999002", with_manager=True)
        assert "/jjjl_999002" in "".join(calls)      # 缓存缺 managers → 重抓
        assert "managers" in info

    def test_managerless_cache_served_when_not_needed(self, monkeypatch):
        calls = []
        monkeypatch.setattr(D, "_load_cache",
                            lambda key, hours: {"code": "999003", "benchmark": "x"})
        monkeypatch.setattr(D, "_http", lambda *a, **k: calls.append(1) or "")
        info = D.fetch_f10("999003", with_manager=False)
        assert info["benchmark"] == "x" and not calls
