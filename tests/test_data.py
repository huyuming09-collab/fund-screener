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
