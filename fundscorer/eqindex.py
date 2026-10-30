# -*- coding: utf-8 -*-
"""指数估值模块（债指之外的指数型基金 · v1.0）。

与 ``indexfund``（债券指数基金）并列、互不影响：
  · 指数卡单元 = **指数**（不是基金）；卡片只放估值；
  · 详情页 = 估值档位/历史分位/走势 + 同指数 5 只低费率场外基金（含 A/C）；
  · 估值数据 = 雪球基金·指数估值（PE/PB/股息率/ROE），分位由本模块按**近 5 年周频
    自算**（不直接用第三方口径），档位阈值见 config.EQ_INDEX["tiers"]；
  · 基金→指数 = F10 业绩比较基准解析 + 固定目录别名匹配（最长匹配，歧义拒绝）。

数据流：
  ingest()  全量基金列表 → F10(基准/费率/规模) → 目录匹配 → data/eq_index_funds.pkl
  build()   读建库缓存 + 估值缓存 → 分位/档位/基金清单 → payload["eq_index"]
  CLI       python -m fundscorer.eqindex --ingest | --build | --check
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

from . import batch as B
from . import config as C
from . import data as D

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
FUND_CACHE = DATA_DIR / "eq_index_funds.pkl"
CSV_COMPANY_MAP = DATA_DIR / "fund_company_map.csv"
CSV_COMPANY_SCALE = DATA_DIR / "company_scale_nonmoney.csv"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
REFERER = "https://danjuanfunds.com/djmodule/value-center"
BASE = "https://danjuanfunds.com/djapi/index_eva/"

GROUP_ORDER = ["宽基", "行业", "策略", "海外"]


# ---------------------------------------------------------------- 1. 指数目录（63）
def _cat(code, name, typ, group, metric, aliases, why="", exclude=None):
    return {"code": code, "name": name, "type": typ, "group": group,
            "metric": metric, "aliases": list(aliases),
            "exclude": list(exclude or []), "why": why}


_WHY_PE = "成分分散、盈利口径稳定，以 PE-TTM 分位为主口径；PB 分位并列展示。"
_WHY_PB = "盈利周期波动大/资产驱动，PB 比 PE 更贴近资产端估值；PE 分位并列展示。"
_WHY_GROWTH = "成长/盈利驱动型指数，以 PE-TTM 分位为主口径；PB 并列展示。"
_WHY_DIV = "红利/价值策略成分偏金融与高分红资产，以 PB 分位为主口径（股息率现值并列），避免 PE 被周期利润扭曲。"
_WHY_STRAT = "价值/低波/基本面类策略指数，盈利波动大，以 PB 分位为主口径；PE 分位并列校正。"
_WHY_OVERSEA = "海外/跨境指数，PE-TTM 分位为主口径；净值与估值日期可能滞后一个交易日。"

_BROAD_EXCL = ["价值", "成长", "红利", "低波", "增强", "质量", "等权", "行业中性"]

CATALOG = [
    # ---- 宽基（A 股）----
    _cat("SH000300", "沪深300", "宽基", "宽基", "pe",
         ["沪深300指数", "沪深300"], _WHY_PE, ["价值", "成长", "红利", "低波", "非银", "医药", "消费", "信息", "金融", "地产", "ESG", "质量", "A500", "200"]),
    _cat("SH000905", "中证500", "宽基", "宽基", "pe",
         ["中证500指数", "中证小盘500指数", "中证500"], _WHY_PE, ["低波动", "质量", "成长", "价值", "红利", "行业中性", "等权", "2000"]),
    _cat("SH000852", "中证1000", "宽基", "宽基", "pe",
         ["中证1000指数", "中证1000"], _WHY_PE, ["价值", "成长", "红利", "低波", "质量"]),
    _cat("SH000016", "上证50", "宽基", "宽基", "pe",
         ["上证50指数", "上证50"], _WHY_PE, ["价值", "成长", "红利", "低波", "A50", "科创"]),
    _cat("SH000010", "上证180", "宽基", "宽基", "pe",
         ["上证180指数", "上证180"], _WHY_PE, ["价值", "成长"]),
    _cat("SH000903", "中证100", "宽基", "宽基", "pe",
         ["中证A100指数", "中证100指数", "中证A100", "中证100"], _WHY_PE, ["价值", "成长"]),
    _cat("SZ399006", "创业板", "宽基", "宽基", "pe",
         ["创业板指数", "创业板指"], _WHY_GROWTH, ["50", "200", "综", "中盘", "大盘", "小盘", "低波", "价值", "成长"]),
    _cat("SH000688", "科创50", "宽基", "宽基", "pe",
         ["科创50指数", "上证科创板50成份指数", "科创板50指数", "科创50"], _WHY_GROWTH, ["100"]),
    _cat("SZ399330", "深证100", "宽基", "宽基", "pe",
         ["深证100指数", "深证100"], _WHY_PE, ["价值", "成长", "红利"]),
    _cat("SZ399001", "深证成指", "宽基", "宽基", "pe",
         ["深证成份指数", "深证成指"], _WHY_PE),
    _cat("SZ399317", "国证A指", "宽基", "宽基", "pe",
         ["国证A股指数", "国证A指"], _WHY_PE),
    _cat("SH000170", "50AH优选", "宽基", "宽基", "pe",
         ["50AH优选指数", "中证50AH优选指数", "50AH优选"], _WHY_PE),
    _cat("SZ399550", "央视50", "宽基", "宽基", "pe",
         ["央视财经50指数", "央视50指数", "央视50"], _WHY_PE),
    _cat("CSI716567", "MSCI中国", "宽基", "宽基", "pe",
         ["MSCI中国A股国际通指数", "MSCI中国A股指数", "MSCI中国A股国际通", "MSCI中国A股"], _WHY_PE),
    # ---- 海外 ----
    _cat("SP500", "标普500", "宽基", "海外", "pe",
         ["标普500指数", "标普500"], _WHY_OVERSEA),
    _cat("NDX", "纳指100", "宽基", "海外", "pe",
         ["纳斯达克100指数", "纳斯达克100", "纳指100指数", "纳指100", "NASDAQ100"], _WHY_OVERSEA),
    _cat("GDAXI", "德国DAX", "宽基", "海外", "pe",
         ["德国DAX指数", "DAX指数", "德国DAX"], _WHY_OVERSEA),
    _cat("HKHSI", "恒生指数", "宽基", "海外", "pe",
         ["恒生指数"], _WHY_OVERSEA),
    _cat("HKHSCEI", "国企指数", "宽基", "海外", "pe",
         ["恒生中国企业指数", "国企指数", "H股指数"], _WHY_OVERSEA),
    _cat("HSFML25", "香港大盘", "宽基", "海外", "pe",
         ["恒生香港大盘指数", "恒生香港上市大型股指数", "香港大盘"], _WHY_OVERSEA),
    _cat("SPHCMSHP", "香港中小", "宽基", "海外", "pe",
         ["标普香港上市中国中小盘指数", "香港中小盘指数", "香港中小"], _WHY_OVERSEA),
    _cat("935600", "MSCI印度", "宽基", "海外", "pe",
         ["MSCI印度指数", "MSCI印度", "印度指数"], _WHY_OVERSEA),
    _cat("HKHSTECH", "恒生科技", "策略", "海外", "pe",
         ["恒生科技指数", "恒生科技"], _WHY_OVERSEA),
    _cat("HKHSSCNE", "新经济", "行业", "海外", "pe",
         ["恒生沪深港新经济指数", "恒生新经济指数", "新经济指数", "新经济"], _WHY_OVERSEA),
    _cat("CSIH30533", "中概互联50", "行业", "海外", "pe",
         ["中证海外中国互联网50指数", "中国互联网50指数", "中概互联50"], _WHY_OVERSEA),
    _cat("CSIH11136", "中国互联", "行业", "海外", "pe",
         ["中证海外中国互联网指数", "中国互联网指数", "中国互联"], _WHY_OVERSEA),
    _cat("SPACEVCP", "标普价值", "策略", "海外", "pb",
         ["标普价值指数", "标普价值"], _WHY_PB),
    _cat("SPCQVCP", "标普质量", "策略", "海外", "pe",
         ["标普质量指数", "标普质量"], _WHY_PE),
    _cat("CSPSADRP", "标普红利", "策略", "海外", "pb",
         ["标普红利指数", "标普中国A股红利机会指数", "标普红利"], _WHY_DIV),
    # ---- 行业 ----
    _cat("SH000932", "主要消费", "行业", "行业", "pe",
         ["中证主要消费指数", "主要消费指数", "中证800消费指数"], _WHY_GROWTH),
    _cat("SZ399997", "中证白酒", "行业", "行业", "pe",
         ["中证白酒指数", "白酒指数"], _WHY_GROWTH),
    _cat("CSIH30094", "消费红利", "策略", "行业", "pb",
         ["中证主要消费红利指数", "消费红利指数", "消费红利"], _WHY_DIV),
    _cat("SZ399396", "国证食品", "行业", "行业", "pe",
         ["国证食品饮料指数", "国证食品指数", "国证食品"], _WHY_GROWTH),
    _cat("SZ399975", "证券公司", "行业", "行业", "pb",
         ["中证全指证券公司指数", "证券公司指数"], _WHY_PB),
    _cat("SZ399989", "中证医疗", "行业", "行业", "pe",
         ["中证医疗指数"], _WHY_GROWTH),
    _cat("SH000991", "全指医药", "行业", "行业", "pe",
         ["中证全指医药卫生指数", "全指医药卫生指数", "全指医药"], _WHY_GROWTH),
    _cat("SH000978", "医药100", "行业", "行业", "pe",
         ["中证医药100指数", "医药100指数"], _WHY_GROWTH),
    _cat("SZ399986", "中证银行", "行业", "行业", "pb",
         ["中证银行指数"], _WHY_PB),
    _cat("SZ399998", "中证煤炭", "行业", "行业", "pb",
         ["中证煤炭指数"], _WHY_PB),
    _cat("SH000827", "中证环保", "行业", "行业", "pe",
         ["中证环保产业指数", "中证环保指数", "中证环保"], _WHY_GROWTH),
    _cat("SZ399417", "新能源车", "行业", "行业", "pe",
         ["国证新能源车指数", "国证新能源车", "新能源车指数"], _WHY_GROWTH),
    _cat("SZ399971", "中证传媒", "行业", "行业", "pe",
         ["中证传媒指数"], _WHY_GROWTH),
    _cat("SZ399610", "TMT50", "行业", "行业", "pe",
         ["TMT50指数", "中证TMT50指数", "TMT50"], _WHY_GROWTH),
    _cat("CSI931079", "5G通讯", "策略", "行业", "pe",
         ["中证5G通信主题指数", "5G通信主题指数", "5G通讯指数", "5G通信指数", "5G通信主题"], _WHY_GROWTH),
    _cat("CSI931087", "科技龙头", "行业", "行业", "pe",
         ["中证科技龙头指数", "科技龙头指数"], _WHY_GROWTH),
    _cat("CSI930652", "中证电子", "行业", "行业", "pe",
         ["中证电子指数"], _WHY_GROWTH),
    _cat("SH000993", "全指信息", "行业", "行业", "pe",
         ["中证全指信息技术指数", "全指信息技术指数", "全指信息"], _WHY_GROWTH),
    _cat("SZ399967", "中证军工", "行业", "行业", "pe",
         ["中证军工指数"], _WHY_GROWTH),
    _cat("SZ399812", "养老产业", "策略", "行业", "pe",
         ["中证养老产业指数", "养老产业指数"], _WHY_GROWTH),
    _cat("SZ399393", "国证地产", "行业", "行业", "pb",
         ["国证房地产指数", "国证地产指数", "国证房地产", "国证地产"], _WHY_PB),
    _cat("CSI931142", "东证竞争", "策略", "行业", "pe",
         ["中证东方红竞争力指数", "东证竞争指数", "东证竞争"], _WHY_PE),
    _cat("SH000989", "全指可选", "行业", "行业", "pe",
         ["中证全指可选消费指数", "全指可选消费指数", "全指可选"], _WHY_GROWTH),
    # ---- 策略 ----
    _cat("SH000922", "中证红利", "策略", "策略", "pb",
         ["中证红利指数"], _WHY_DIV, ["低波", "成长", "价值", "质量"]),
    _cat("SH000015", "上证红利", "策略", "策略", "pb",
         ["上证红利指数"], _WHY_DIV),
    _cat("SZ399324", "深证红利", "策略", "策略", "pb",
         ["深证红利指数"], _WHY_DIV),
    _cat("CSIH30269", "红利低波", "策略", "策略", "pb",
         ["中证红利低波动指数", "中证红利低波动", "红利低波动指数", "红利低波"], _WHY_DIV, ["100"]),
    _cat("CSI931157", "红利成长LV", "策略", "策略", "pb",
         ["中证沪港深红利成长低波动指数", "沪港深红利成长低波动指数", "红利成长LV"], _WHY_DIV),
    _cat("CSI930740", "300红利LV", "策略", "策略", "pb",
         ["沪深300红利低波动指数", "300红利低波动指数", "300红利LV"], _WHY_DIV),
    _cat("SH000919", "300价值", "策略", "策略", "pb",
         ["沪深300价值指数", "300价值指数", "300价值"], _WHY_STRAT),
    _cat("SH000925", "基本面50", "策略", "策略", "pb",
         ["中证锐联基本面50指数", "基本面50指数", "基本面50"], _WHY_STRAT),
    _cat("SZ399701", "基本面60", "策略", "策略", "pb",
         ["国证基本面60指数", "基本面60指数", "基本面60"], _WHY_STRAT),
    _cat("SZ399702", "基本面120", "策略", "策略", "pb",
         ["国证基本面120指数", "基本面120指数", "基本面120"], _WHY_STRAT),
    _cat("CSI930782", "500低波", "策略", "策略", "pb",
         ["中证500行业中性低波动指数", "中证500低波动指数", "500低波动指数", "500低波"], _WHY_STRAT),
    # ---- v3 基础版：主流宽基/行业/海外（红利/价值/低波暂不上线，见 _DEFER）----
    _cat("SH000510", "中证A500", "宽基", "宽基", "pe",
         ["中证A500指数", "中证A500"], _WHY_PE),
    _cat("SH899050", "北证50", "宽基", "宽基", "pe",
         ["北证50成份指数", "北证50成分指数", "北证50"], _WHY_PE),
    _cat("SH000001", "上证综指", "宽基", "宽基", "pe",
         ["上证综合指数", "上证综指", "上证指数"], _WHY_PE),
    _cat("SH000698", "科创100", "宽基", "宽基", "pe",
         ["上证科创板100指数", "科创100指数", "科创100"], _WHY_GROWTH),
    _cat("SH000699", "科创200", "宽基", "宽基", "pe",
         ["上证科创板200指数", "科创200指数", "科创200"], _WHY_GROWTH),
    _cat("H30184", "半导体", "行业", "行业", "pe",
         ["中证全指半导体产品与设备指数", "中证全指半导体", "半导体产品与设备指数"], _WHY_GROWTH),
    _cat("CSI931151", "光伏产业", "行业", "行业", "pe",
         ["中证光伏产业指数", "光伏产业指数"], _WHY_GROWTH),
    _cat("SZ399976", "新能源汽车", "行业", "行业", "pe",
         ["中证新能源汽车指数", "中证新能源汽车", "新能源汽车指数"], _WHY_GROWTH),
    _cat("CSI930713", "人工智能", "行业", "行业", "pe",
         ["中证人工智能主题指数", "中证人工智能主题", "人工智能主题指数"], _WHY_GROWTH),
    _cat("H30590", "机器人", "行业", "行业", "pe",
         ["中证机器人指数", "机器人指数"], _WHY_GROWTH),
    _cat("SZ399965", "800地产", "行业", "行业", "pb",
         ["中证800地产指数", "800地产指数", "800地产"], _WHY_PB),
]

CATALOG_BY_CODE = {c["code"]: c for c in CATALOG}

# v1.2：四分区导航上线。策略类（价值/低波/基本面/质量）留在指数型「策略」tab；
# 纯红利/价值/红利低波等留给未来的「红利类」页签（占位中），继续 _DEFER。
_MAINSTREAM = {
    # 宽基
    "SH000300", "SH000016", "SH000510", "SH000905", "SH000852", "SZ399006",
    "SH000688", "SH899050", "SH000001", "SH000698", "SH000699",
    # 行业/主题
    "SZ399997", "SH000932", "SH000991", "SZ399989", "SZ399986", "SZ399975",
    "H30184", "CSI931151", "SZ399976", "SZ399967", "CSI930652", "CSI930713",
    "H30590", "CSI931079", "CSI931087", "SZ399971", "SH000827", "SZ399965",
    # 策略（价值/低波/基本面/质量，非红利）
    "SH000919", "SH000925", "SZ399701", "SZ399702", "CSI930782", "SPCQVCP",
    # 海外
    "HKHSTECH", "NDX", "SP500", "HKHSI",
}
# 纯红利/价值：留待「红利类」页签，暂不上线
_DEFER = {
    "SH000922", "SH000015", "SZ399324", "CSIH30269", "CSI931157", "CSI930740",
    "CSIH30094", "CSPSADRP", "SPACEVCP",
}


def _cs_code(code: str) -> str | None:
    """雪球代码 → 中证官网代码；海外/港交所代码返回 None。"""
    if re.fullmatch(r"H?\d{5,6}", str(code or "")):
        return str(code)
    for pre in ("CSIH", "CSI", "SH", "SZ"):
        if code.startswith(pre) and len(code) > len(pre):
            c = code[len(pre):]
            return c if (c.isdigit() or (c[0] == "H" and c[1:].isdigit())) else None
    return None


for _c in CATALOG:
    _c["mainstream"] = _c["code"] in _MAINSTREAM
    _c["defer"] = _c["code"] in _DEFER
    _c["cs"] = _cs_code(_c["code"])
    # 行业/策略默认展示 PB（金融/周期/价值更稳），宽基/成长默认 PE
    _c["default_metric"] = "pb" if _c.get("type") in ("行业", "策略") else "pe"
CATALOG_BY_CODE = {c["code"]: c for c in CATALOG}


# ---------------------------------------------------------------- 2. 基准解析 / 目录匹配
# 现金腿识别：不要用裸「银行」——会误杀「中证银行指数」这类银行行业指数
_CASH_WORDS = ("活期", "定期", "存款", "税后", "金融机构人民币")
_WEIGHT_HEAD = re.compile(r"^\s*\d+(?:\.\d+)?\s*%\s*[*×xX]?\s*")
_WEIGHT_TAIL = re.compile(r"\s*[*×xX]\s*\d+(?:\.\d+)?\s*%\s*$")
_PAREN = re.compile(r"[（(][^）)]*[）)]")


def _norm(text: str) -> str:
    s = str(text or "").strip()
    s = s.replace("（", "(").replace("）", ")").replace("，", ",")
    s = re.sub(r"\s+", "", s)
    return s


def clean_component(comp: str) -> str:
    """单个基准成分 → 指数名（去权重/收益率/汇率折算括号/人民币计价前缀）。"""
    s = _norm(comp)
    s = s.replace("×", "*").replace("＋", "+")
    s = _WEIGHT_HEAD.sub("", s)
    s = _WEIGHT_TAIL.sub("", s)
    s = s.replace("人民币计价的", "").replace("人民币计价", "")
    s = _PAREN.sub("", s)
    s = s.replace("价格指数", "指数")              # 深证红利价格指数 → 深证红利指数
    s = re.sub(r"(?:净)?(?:总收益|全收益)指数$", "指数", s)
    s = re.sub(r"(?:总收益|全收益|净价|全价)?收益率$", "", s)
    s = re.sub(r"收益率$", "", s)
    return s.strip("*")


def parse_benchmark(bench: str) -> list[str]:
    """业绩比较基准 → 候选指数名列表（现金/存款腿剔除，权重剥离）。

    多成分基准保留全部非现金成分，供``match_benchmark``逐段匹配取最长别名。
    """
    raw = _norm(bench).replace("×", "*").replace("＋", "+")
    if not raw:
        return []
    comps = [clean_component(x) for x in re.split(r"\+", raw)]
    comps = [x for x in comps if len(x) >= 3]
    if not comps:
        return []
    non_cash = [x for x in comps if not any(w in x for w in _CASH_WORDS)]
    # 债券/货币腿（如中证综合债）不是股票指数，剔除后仍有成分才用；否则保留非现金列表
    eq_like = [x for x in non_cash if "债" not in x and "货币" not in x]
    return eq_like or non_cash


def match_catalog(text: str) -> tuple[str | None, str | None]:
    """单个指数名 → (目录代码, 命中的别名)。最长别名匹配；不同指数同分→歧义拒绝。"""
    s = _norm(text)
    if not s:
        return None, None
    hits: dict[str, tuple[int, str]] = {}
    for c in CATALOG:
        if any(x in s for x in c["exclude"]):
            continue
        for alias in c["aliases"]:
            a = _norm(alias)
            if a and a in s:
                cur = hits.get(c["code"])
                if cur is None or len(a) > cur[0]:
                    hits[c["code"]] = (len(a), alias)
    if not hits:
        return None, None
    best = max(v[0] for v in hits.values())
    top = [k for k, v in hits.items() if v[0] == best]
    if len(top) > 1:
        return None, None
    code = top[0]
    return code, hits[code][1]


def match_benchmark(bench: str) -> tuple[str | None, str | None, str | None]:
    """业绩基准 → (目录代码, 成分名, 命中别名)。多成分取最长命中。"""
    best = (None, None, None, 0)
    for comp in parse_benchmark(bench):
        code, alias = match_catalog(comp)
        if code and len(alias or "") > best[3]:
            best = (code, comp, alias, len(alias or ""))
    return best[0], best[1], best[2]


# ---------------------------------------------------------------- 3. 分位 / 档位
def percentile_of(values: list[float], dates: list, current: float | None = None,
                  window_years: int | None = None, min_weeks: int | None = None,
                  min_weeks_for_tier: int | None = None) -> dict:
    """窗口内当前值分位（0~100，值越大越贵）。返回区间/周数/样本充足性。"""
    cfg = C.EQ_INDEX
    window_years = window_years or cfg["window_years"]
    min_weeks = int(min_weeks or cfg["min_weeks"])
    min_tier = int(min_weeks_for_tier or cfg["min_weeks_for_tier"])
    out = {"pct": None, "weeks": 0, "window_start": None, "end": None,
           "min": None, "median": None, "max": None,
           "sufficient": False, "enough_for_tier": False}
    if not values or dates is None or len(dates) == 0:
        return out
    s = pd.Series(values, index=pd.to_datetime(dates), dtype=float).dropna()
    s = s.sort_index()
    if s.empty:
        return out
    end = s.index.max()
    start = end - pd.DateOffset(years=window_years)
    w = s[(s.index >= start) & (s > 0)]
    if w.empty:
        return out
    cur = float(w.iloc[-1]) if current is None else float(current)
    out.update({
        "pct": round(float((w <= cur).mean() * 100.0), 2),
        "weeks": int(len(w)),
        "window_start": w.index.min().strftime("%Y-%m-%d"),
        "end": w.index.max().strftime("%Y-%m-%d"),
        "min": round(float(w.min()), 4),
        "median": round(float(w.median()), 4),
        "max": round(float(w.max()), 4),
        "sufficient": len(w) >= min_weeks,
        "enough_for_tier": len(w) >= min_tier,
    })
    return out


def tier_of(pct: float | None) -> str | None:
    """分位 → 五档（分位越低越便宜）。阈值语义：<= 上界。"""
    if pct is None:
        return None
    t = C.EQ_INDEX["tiers"]
    if pct <= t["极低估"]:
        return "极低估"
    if pct <= t["低估"]:
        return "低估"
    if pct <= t["合理偏低"]:
        return "合理偏低"
    if pct <= t["合理偏高"]:
        return "合理偏高"
    return "高估"


def _monthly_series(values: list[float], dates: list, points: int = 40,
                    window_years: int | None = None) -> list[list]:
    if not values:
        return []
    s = pd.Series(values, index=pd.to_datetime(dates), dtype=float).dropna()
    s = s[s > 0].sort_index()
    if s.empty:
        return []
    if window_years:                              # 走势只画估值窗口内（与分位口径一致）
        s = s[s.index >= s.index.max() - pd.DateOffset(years=window_years)]
    g = s.groupby([s.index.year, s.index.month]).last()
    items = list(g.items())
    if len(items) > points:                       # 等距抽稀，保留首尾与整段跨度
        step = (len(items) - 1) / (points - 1)
        items = [items[round(i * step)] for i in range(points)]
    return [[f"{y}-{m:02d}", round(float(v), 4)] for (y, m), v in items]


# ---------------------------------------------------------------- 4. 估值抓取（雪球）
def _json_get(url: str, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": REFERER})
    last: Exception | None = None
    for i in range(2):
        try:
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except Exception as e:                     # noqa: BLE001 - 网络/解析统一重试
            last = e
            if i == 0:
                time.sleep(1.0)
    raise last  # type: ignore[misc]


def _hist_pairs(arr: list[dict], key: str) -> tuple[list, list]:
    vals, dates = [], []
    for row in arr or []:
        v = row.get(key)
        ts = row.get("ts")
        if v is None or ts is None:
            continue
        try:
            vals.append(float(v))
            dates.append(pd.to_datetime(int(ts), unit="ms"))
        except (TypeError, ValueError):
            continue
    return vals, dates


def fetch_valuation_remote(code: str) -> dict:
    """拉取单指数估值（detail + PE/PB 全历史）。返回原始结构，供缓存。"""
    detail = (_json_get(BASE + "detail/" + code) or {}).get("data") or {}
    pe = (_json_get(BASE + "pe_history/" + code + "?day=all") or {}).get("data") or {}
    pb = (_json_get(BASE + "pb_history/" + code + "?day=all") or {}).get("data") or {}
    return {"fetched_at": str(pd.Timestamp.now()),
            "detail": detail,
            "pe_hist": pe.get("index_eva_pe_growths") or [],
            "pb_hist": pb.get("index_eva_pb_growths") or []}


def load_valuation(code: str, refresh: bool) -> dict | None:
    """估值缓存（20h）；refresh=False 无网络、可用过期缓存；失败沿用旧缓存。"""
    key = f"eqidx_v1_{code}"
    cached = D._load_cache(key, C.EQ_INDEX["cache_hours"])
    if cached is not None:
        return cached
    if not refresh:
        try:
            p = D._cache_path(key)
            return pd.read_pickle(p) if p.exists() else None
        except Exception:                          # noqa: BLE001
            return None
    try:
        raw = fetch_valuation_remote(code)
        D._save_cache(key, raw)
        return raw
    except Exception:                              # noqa: BLE001 - 网络失败回退旧缓存
        try:
            p = D._cache_path(key)
            return pd.read_pickle(p) if p.exists() else None
        except Exception:                          # noqa: BLE001
            return None


def build_index_valuation(entry: dict, raw: dict) -> dict | None:
    """原始估值 → 主口径/双分位/档位/区间/走势。两指标都不可用 → None。"""
    if not raw:
        return None
    detail = raw.get("detail") or {}
    pe_vals, pe_dates = _hist_pairs(raw.get("pe_hist"), "pe")
    pb_vals, pb_dates = _hist_pairs(raw.get("pb_hist"), "pb")
    pe_cur = detail.get("pe")
    pb_cur = detail.get("pb")
    pe = percentile_of(pe_vals, pe_dates,
                       current=pe_cur if (pe_cur is not None and float(pe_cur) > 0) else None)
    pb = percentile_of(pb_vals, pb_dates,
                       current=pb_cur if (pb_cur is not None and float(pb_cur) > 0) else None)
    metric = entry["metric"]
    why = entry["why"]
    fallback_note = ""
    if metric == "pe" and (pe["pct"] is None or not pe["enough_for_tier"]):
        if pb["pct"] is not None and pb["enough_for_tier"]:
            metric, fallback_note = "pb", "因 PE 缺失/失真，改用 PB 分位。"
    if metric == "pb" and (pb["pct"] is None or not pb["enough_for_tier"]):
        if pe["pct"] is not None and pe["enough_for_tier"]:
            metric, fallback_note = "pe", "因 PB 缺失/失真，改用 PE 分位。"
    prim, sec = (pe, pb) if metric == "pe" else (pb, pe)
    if prim["pct"] is None or not prim["enough_for_tier"]:
        return None
    pct = prim["pct"]
    gap = None
    if pe["pct"] is not None and pb["pct"] is not None:
        gap = round(abs(pe["pct"] - pb["pct"]), 1)
    out = {
        "code": entry["code"], "name": entry["name"],
        "type": entry["type"], "group": entry["group"],
        "metric": metric,
        "metric_label": "PB 分位" if metric == "pb" else "PE-TTM 分位",
        "why": why + (" " + fallback_note if fallback_note else ""),
        "tier": tier_of(pct), "percentile": pct,
        "window_years": C.EQ_INDEX["window_years"],
        "weeks": prim["weeks"], "window_start": prim["window_start"],
        "sufficient": prim["sufficient"],
        "pe": _round(pe_cur, 4), "pe_pct": pe["pct"],
        "pb": _round(pb_cur, 4), "pb_pct": pb["pct"],
        "div_yield": _round(detail.get("yeild"), 4),
        "roe": _round(detail.get("roe"), 4),
        "pe_pb_gap": gap,
        "range": {"min": prim["min"], "median": prim["median"], "max": prim["max"]},
        "valuation_date": prim["end"] or detail.get("date"),
        "series": _monthly_series(pe_vals if metric == "pe" else pb_vals,
                                  pe_dates if metric == "pe" else pb_dates,
                                  window_years=C.EQ_INDEX["window_years"]),
    }
    return out


def _round(v, nd):
    try:
        return None if v is None or pd.isna(v) else round(float(v), nd)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 4b. 中证官方 PE + 指数点位（v3 基础版）
CS_BASE = "https://www.csindex.com.cn/csindex-home"
CS_HEADERS = {"User-Agent": UA, "Referer": "https://www.csindex.com.cn/"}


def _cs_json(url: str, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(url, headers=CS_HEADERS)
    last: Exception | None = None
    for i in range(2):
        try:
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except Exception as e:                     # noqa: BLE001
            last = e
            if i == 0:
                time.sleep(0.8)
    raise last  # type: ignore[misc]


def load_cs(code: str, refresh: bool) -> dict | None:
    """中证官网 PE(TTM) 日频 + 指数收盘日频（缓存 20h，失败沿用旧缓存）。"""
    key = f"eqcs_v1_{code}"
    cached = D._load_cache(key, C.EQ_INDEX["cache_hours"])
    if cached is not None:
        return cached
    if not refresh:
        try:
            p = D._cache_path(key)
            return pd.read_pickle(p) if p.exists() else None
        except Exception:                          # noqa: BLE001
            return None
    try:
        info = _cs_json(f"{CS_BASE}/indexInfo/index-basic-info/{code}").get("data") or {}
        pe = _cs_json(f"{CS_BASE}/perf/indexCsiDsPe?indexCode={code}"
                      f"&startDate=20050101&endDate=20261231").get("data") or []
        pr = _cs_json(f"{CS_BASE}/perf/index-perf?indexCode={code}"
                      f"&startDate=20050101&endDate=20261231").get("data") or []
        raw = {"publish": str(info.get("publishDate") or ""),
               "pe": [[str(x.get("tradeDate")), x.get("peg")] for x in pe
                      if x.get("peg") is not None],
               "close": [[str(x.get("tradeDate")), x.get("close")] for x in pr
                         if x.get("close") is not None]}
        if not raw["pe"] and not raw["close"]:
            return None
        D._save_cache(key, raw)
        return raw
    except Exception:                              # noqa: BLE001
        try:
            p = D._cache_path(key)
            return pd.read_pickle(p) if p.exists() else None
        except Exception:                          # noqa: BLE001
            return None


def _pairs_series(pairs: list) -> pd.Series:
    if not pairs:
        return pd.Series(dtype=float)
    data = {}
    for d, v in pairs:
        try:
            data[pd.Timestamp(str(d))] = float(v)
        except (TypeError, ValueError):
            continue
    if not data:
        return pd.Series(dtype=float)
    s = pd.Series(data, dtype=float)
    s = s[s > 0].sort_index()
    return s.resample("W-FRI").last().dropna()


def _win_stats(s: pd.Series, cur: float | None, years: float | None) -> dict | None:
    if s is None or s.empty or cur is None:
        return None
    end = s.index.max()
    w = s if years is None else s[s.index >= end - pd.DateOffset(years=int(years))]
    if len(w) < 8:
        return None
    q = w.quantile([0.2, 0.4, 0.5, 0.6, 0.8]).tolist()
    mean, std = float(w.mean()), float(w.std(ddof=0) or 0)
    return {
        "pct": round(float((w <= cur).mean() * 100), 2),
        "weeks": int(len(w)),
        "min": round(float(w.min()), 4), "max": round(float(w.max()), 4),
        "mean": round(mean, 4), "std": round(std, 4),
        "z": round((cur - mean) / std, 2) if std else None,
        "q20": round(q[0], 4), "q40": round(q[1], 4), "q50": round(q[2], 4),
        "q60": round(q[3], 4), "q80": round(q[4], 4),
        "danger": round(q[4], 4), "median": round(q[2], 4), "opportunity": round(q[0], 4),
        "start": w.index.min().strftime("%Y-%m-%d"),
        "end": w.index.max().strftime("%Y-%m-%d"),
    }


def _monthly_pairs(s: pd.Series, points: int = 132) -> list:
    if s is None or s.empty:
        return []
    g = s.groupby([s.index.year, s.index.month]).last()
    items = list(g.items())
    if len(items) > points:
        items = items[-points:]
    return [[f"{y}-{m:02d}-01", round(float(v), 4)] for (y, m), v in items]


def _tier_of(pct: float | None) -> str | None:
    """五档（20/40/60/80；估值分位越低越便宜）。"""
    if pct is None:
        return None
    t = C.EQ_INDEX["tiers"]
    if pct <= t["极低估"]:
        return "极低估"
    if pct <= t["低估"]:
        return "低估"
    if pct <= t["合理偏低"]:
        return "合理偏低"
    if pct <= t["合理偏高"]:
        return "合理偏高"
    return "高估"


# 定投倍数（v1.2 删除：用户要求去掉“几倍定投”内容，档位只做展示）

# 无中证点位数据的指数（深交所/海外）：用对应场外基金净值做“点位代理”，只用于画图/近1年
_CLOSE_PROXY = {"SZ399006": "110026", "HKHSTECH": "012348", "NDX": "006479",
                "SP500": "161125", "HKHSI": "000071"}


def build_index_valuation_v2(entry: dict, refresh: bool) -> dict | None:
    """v3 基础版：中证 PE/点位为主、雪球 PB 为辅；10Y 为主窗口，3/5/全历史可切。"""
    cs = load_cs(entry["cs"], refresh) if entry.get("cs") else None
    dj = None
    try:
        dj = load_valuation(entry["code"], refresh)
    except Exception:                              # noqa: BLE001
        dj = None
    pe_pairs = (cs or {}).get("pe") or []
    close_pairs = (cs or {}).get("close") or []
    pe_s = _pairs_series(pe_pairs)
    if pe_s.empty and dj:
        vals, dates = _hist_pairs(dj.get("pe_hist"), "pe")
        pe_s = _pairs_series([[pd.Timestamp(d).strftime("%Y%m%d"), v]
                              for d, v in zip(dates, vals)])
    pb_s = pd.Series(dtype=float)
    if dj:
        vals, dates = _hist_pairs(dj.get("pb_hist"), "pb")
        pb_s = _pairs_series([[pd.Timestamp(d).strftime("%Y%m%d"), v]
                              for d, v in zip(dates, vals)])
        if len(pb_s) < 26:                         # 雪球未覆盖时只回 1 个占位点，视为无 PB
            pb_s = pd.Series(dtype=float)
    close_s = _pairs_series(close_pairs)
    close_proxy = False
    if close_s.empty and entry["code"] in _CLOSE_PROXY:
        try:
            nav = D.fetch_nav(_CLOSE_PROXY[entry["code"]])
            if nav is not None and len(nav):
                n = nav.copy()
                n["date"] = pd.to_datetime(n["date"])
                n = n.set_index("date")["nav"].sort_index().resample("W-FRI").last().dropna()
                close_s = n
                close_proxy = True
        except Exception:                          # noqa: BLE001
            pass
    # 只保留指数发布日之后（避免中证回填历史）
    pub = (cs or {}).get("publish") or ""
    if pub:
        try:
            start = pd.Timestamp(pub)
            pe_s = pe_s[pe_s.index >= start]
            pb_s = pb_s[pb_s.index >= start]
            close_s = close_s[close_s.index >= start]
        except Exception:                          # noqa: BLE001
            pass
    if pe_s.empty and pb_s.empty:
        return None
    cur_pe = float(pe_s.iloc[-1]) if not pe_s.empty else None
    cur_pb = float(pb_s.iloc[-1]) if not pb_s.empty else None
    windows = {}
    for label, years in (("3y", 3), ("5y", 5), ("10y", 10), ("all", None)):
        pe_w = _win_stats(pe_s, cur_pe, years)
        pb_w = _win_stats(pb_s, cur_pb, years)
        pcts = [x["pct"] for x in (pe_w, pb_w) if x and x["pct"] is not None]
        comp = round(sum(pcts) / len(pcts), 2) if pcts else None
        windows[label] = {"label": {"3y": "近3年", "5y": "近5年",
                                    "10y": "近10年", "all": "上市以来"}[label],
                          "comp_pct": comp, "tier": _tier_of(comp),
                          "pe": pe_w, "pb": pb_w}
    base = windows["10y"] if windows["10y"]["comp_pct"] is not None else next(
        (windows[k] for k in ("5y", "3y", "all") if windows[k]["comp_pct"] is not None), None)
    if base is None:
        return None
    pct = base["comp_pct"]
    tier = _tier_of(pct)
    default_metric = "pb" if (not pb_s.empty and entry.get("default_metric") == "pb") else "pe"
    if default_metric == "pb" and windows["10y"]["pb"] is None:
        default_metric = "pe"
    if default_metric == "pe" and windows["10y"]["pe"] is None:
        default_metric = "pb"
    method_label = ("PE+PB" if (not pe_s.empty and not pb_s.empty) else "PE") + " · 分位"
    weeks10 = max((windows["10y"][m] or {}).get("weeks", 0)
                  for m in ("pe", "pb") if windows["10y"].get(m))
    history_label = ("近10年" if weeks10 >= 468 else "近5年" if weeks10 >= 260
                     else "近3年" if weeks10 >= 156 else "上市以来")
    if entry["group"] == "宽基":
        why = "宽基指数用 PE/PB 综合分位（越低越便宜），PE 反映盈利定价、PB 反映资产端。"
    elif entry["group"] == "行业":
        why = "行业指数用 PE/PB 综合分位：PB 对盈利波动更稳，PE 并列校正；只作历史位置参考。"
    else:
        why = "海外/跨境指数用 PE/PB 分位；净值与估值日期可能滞后，汇率折算会带来偏差。"
    ret_1y = None
    if len(close_s) > 53:
        try:
            ret_1y = round(float(close_s.iloc[-1] / close_s.iloc[-53] - 1), 4)
        except Exception:                          # noqa: BLE001
            ret_1y = None
    return {
        "code": entry["code"], "name": entry["name"], "type": entry["type"],
        "group": entry["group"], "cs": entry.get("cs"),
        "tier": tier, "percentile": pct,
        "windows": windows,
        "pe": _round(cur_pe, 2), "pb": _round(cur_pb, 4),
        "div_yield": _round(((dj or {}).get("detail") or {}).get("yeild"), 4),
        "default_metric": default_metric,
        "method_label": method_label,
        "history_label": history_label,
        "why": why,
        "ret_1y": ret_1y,
        "close_proxy": close_proxy,
        "valuation_date": base.get("pe", {}).get("end") if base.get("pe") else (
            base.get("pb", {}).get("end") if base.get("pb") else None),
        "series": {"pe": _monthly_pairs(pe_s), "pb": _monthly_pairs(pb_s),
                   "close": _monthly_pairs(close_s)},
    }


# ---------------------------------------------------------------- 5. 基金建库（F10）
_CLS_TAIL = re.compile(r"([A-Z])(?:\(后端\))?$")


def fund_class(name: str) -> str:
    m = _CLS_TAIL.search(_norm(name))
    return m.group(1) if m else ""


def eligible_fund(name: str, ptype: str) -> bool:
    cfg = C.EQ_INDEX
    if ptype not in cfg["types"]:
        return False
    nm = str(name or "")
    if any(k in nm for k in cfg["exclude_name_keywords"]):
        return False
    if cfg["exclude_onsite_etf"] and ("ETF" in nm and "ETF联接" not in nm):
        return False
    return True


def _parse_yi(text) -> float | None:
    m = re.search(r"([\d.]+)\s*亿", str(text or ""))
    return float(m.group(1)) if m else None


def _xq_fund(code: str) -> dict:
    """雪球基金基础信息 + 费率明细（管理/托管/销售服务 + 买入首档）。

    2026-10-29 实测：单只 2 个轻量请求、8 线程约 16 只/s，无 514 限频；
    天天基金 F10 在并发下会 ``HTTP 514 Frequency Capped``（首版建库踩坑），故改为主源。
    """
    import akshare as ak
    b = ak.fund_individual_basic_info_xq(symbol=code)
    items = {str(r["item"]): r["value"] for _, r in b.iterrows()}
    d = ak.fund_individual_detail_info_xq(symbol=code)
    fees: dict[str, float] = {}
    purchase = None
    if d is not None and len(d):
        for _, r in d.iterrows():
            kind = str(r.get("费用类型") or "")
            nm = str(r.get("条件或名称") or "")
            try:
                val = float(r.get("费用"))
            except (TypeError, ValueError):
                continue
            if kind == "其他费用":
                if "管理费" in nm:
                    fees["mgmt_fee"] = round(val / 100, 6)
                elif "托管费" in nm:
                    fees["custody_fee"] = round(val / 100, 6)
                elif "销售服务费" in nm:
                    fees["sales_fee"] = round(val / 100, 6)
            elif kind == "买入规则" and purchase is None:
                purchase = round(val / 100, 6)
    return {"code": code, "name": str(items.get("基金名称") or ""),
            "benchmark": str(items.get("业绩比较基准") or ""),
            "scale_yi": _parse_yi(items.get("最新规模")),
            "company": str(items.get("基金公司") or ""),
            "establish_date": str(items.get("成立时间") or ""),
            "mgmt_fee": fees.get("mgmt_fee"), "custody_fee": fees.get("custody_fee"),
            "sales_fee": fees.get("sales_fee"), "purchase_fee": purchase,
            "purchase_fee_disc": None, "source": "xueqiu"}


def _f10_fund(code: str) -> dict:
    """F10 兜底（雪球接口缺失时）：基准/规模/费率，无销售服务费时按 0 补。"""
    info = D.fetch_f10(code, with_manager=False, max_age_hours=C.EQ_INDEX["fund_cache_days"] * 24)
    return {"code": code, "name": info.get("name") or "",
            "benchmark": info.get("benchmark") or "",
            "scale_yi": info.get("scale_yi"), "company": "",
            "establish_date": info.get("establish_date") or "",
            "mgmt_fee": info.get("mgmt_fee"), "custody_fee": info.get("custody_fee"),
            "sales_fee": info.get("sales_fee") or 0.0,
            "purchase_fee": info.get("purchase_fee"),
            "purchase_fee_disc": info.get("purchase_fee_disc"), "source": "f10"}


def ingest(log=print, limit: int | None = None, force: bool = False) -> dict:
    """全量建库：基金列表 → 雪球基础信息/费率 → 目录匹配 → data/eq_index_funds.pkl。

    单只基金结果另存 ``eqfund_v1_{code}``（默认 45 天），中断后重跑基本秒回；
    F10 只作兜底（并发限频 514）。
    """
    cfg = C.EQ_INDEX
    t0 = time.time()
    arr = B.fetch_fund_list()
    uni = [r for r in arr if eligible_fund(r[2], r[3])]
    if limit:
        uni = uni[:int(limit)]
    log(f"[ingest] 指数型份额 {sum(1 for r in arr if r[3] in cfg['types'])}"
        f" → 场外非增强 {len(uni)}")
    max_age = cfg["fund_cache_days"] * 24

    def work(row):
        code, _url, name, ptype, _py = row
        key = f"eqfund_v1_{code}"
        rec = None if force else D._load_cache(key, max_age)
        if rec is None:
            try:
                rec = _xq_fund(code)
            except Exception:                      # noqa: BLE001 - 兜底 F10
                try:
                    rec = _f10_fund(code)
                except Exception as e:             # noqa: BLE001
                    return {"code": code, "name": name, "type": ptype,
                            "error": f"{type(e).__name__}: {str(e)[:60]}"}
            D._save_cache(key, rec)
        bench = rec.get("benchmark") or ""
        idx_code, comp, alias = match_benchmark(bench)
        return {**rec, "type": ptype, "name": rec.get("name") or name,
                "benchmark": bench, "idx_code": idx_code, "idx_comp": comp,
                "idx_alias": alias, "cls": fund_class(rec.get("name") or name),
                "fee_total": _fee_total(rec),
                "_fetched_at": str(pd.Timestamp.now())}

    out: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=int(cfg.get("workers") or 8)) as ex:
        futs = {ex.submit(work, r): r[0] for r in uni}
        for f in as_completed(futs):
            out.append(f.result())
            done += 1
            if done % 200 == 0:
                log(f"[ingest] {done}/{len(uni)} …")
    matched = [r for r in out if r.get("idx_code")]
    unmatched = [r for r in out if not r.get("idx_code") and r.get("benchmark")]
    err = [r for r in out if r.get("error")]
    payload = {"fetched_at": str(pd.Timestamp.now()), "universe_n": len(uni),
               "funds": out}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(FUND_CACHE) + ".tmp")
    pd.to_pickle(payload, tmp)
    tmp.replace(FUND_CACHE)
    cnt: dict[str, int] = {}
    for r in matched:
        cnt[r["idx_code"]] = cnt.get(r["idx_code"], 0) + 1
    log(f"[ingest] 完成：入库 {len(out)}，匹配 {len(matched)} 只 / {len(cnt)} 个指数，"
        f"未匹配基准 {len(unmatched)}，失败 {len(err)}，耗时 {(time.time() - t0) / 60:.1f}min")
    for r in sorted(unmatched, key=lambda x: x.get("benchmark") or "")[:10]:
        log(f"[ingest] 未匹配：{r['code']} {r['name'][:18]} ← {r.get('benchmark', '')[:60]}")
    return {"ok": len(out), "matched": len(matched), "indices": len(cnt),
            "unmatched": len(unmatched), "failed": len(err)}


def _fee_total(info: dict) -> float | None:
    vals = [info.get(k) for k in ("mgmt_fee", "custody_fee", "sales_fee")]
    vals = [float(v) for v in vals if v is not None]
    return round(sum(vals), 6) if vals else None


# ---------------------------------------------------------------- 6. 基金清单
def _load_fund_cache() -> dict:
    if not FUND_CACHE.exists():
        return {}
    try:
        return pd.read_pickle(FUND_CACHE)
    except Exception:                              # noqa: BLE001
        return {}


def fund_returns(refresh: bool) -> dict[str, dict]:
    """近 1/3 年收益与申购优惠费（天天基金指数基金列表，缓存 24h）。"""
    key = "eq_fund_returns"
    cached = D._load_cache(key, 24)
    if cached is not None:
        return cached
    if not refresh:
        try:
            p = D._cache_path(key)
            return pd.read_pickle(p) if p.exists() else {}
        except Exception:                          # noqa: BLE001
            return {}
    out: dict[str, dict] = {}
    try:
        import akshare as ak
        for sym in ("全部", "沪深指数", "行业主题", "股票指数", "大盘指数"):
            try:
                df = ak.fund_info_index_em(symbol=sym, indicator="全部")
            except Exception:                      # noqa: BLE001
                continue
            for _, r in df.iterrows():
                code = str(r.get("基金代码") or "").zfill(6)
                if not code:
                    continue
                row = out.setdefault(code, {})
                for src, dst in (("近1年", "ret_1y"), ("近3年", "ret_3y"),
                                 ("手续费", "purchase_fee")):
                    v = r.get(src)
                    try:
                        if v is not None and not pd.isna(v):
                            row[dst] = float(v) / 100 if src != "手续费" else float(v) / 100
                    except (TypeError, ValueError):
                        continue
    except Exception:                              # noqa: BLE001
        pass
    D._save_cache(key, out)
    return out


def _company_scale() -> tuple[dict[str, str], dict[str, float]]:
    code2co: dict[str, str] = {}
    co2scale: dict[str, float] = {}
    try:
        m = pd.read_csv(CSV_COMPANY_MAP, dtype={"code": str})
        for _, r in m.iterrows():
            code2co[str(r["code"]).zfill(6)] = str(r["company"])
    except Exception:                              # noqa: BLE001
        pass
    try:
        s = pd.read_csv(CSV_COMPANY_SCALE)
        for _, r in s.iterrows():
            co2scale[str(r["公司"])] = float(r["非货规模_亿元"])
    except Exception:                              # noqa: BLE001
        pass
    return code2co, co2scale


def select_funds(recs: list[dict], status: dict[str, str],
                 rets: dict[str, dict], code2co: dict[str, str],
                 co2scale: dict[str, float], n: int) -> list[dict]:
    """同指数基金 → 综合费率升序、暂停申购沉底、截 n。"""
    rows = []
    for r in recs:
        fee = r.get("fee_total")
        if fee is None:
            continue
        st = status.get(r["code"])
        rows.append({
            "code": r["code"], "name": r.get("name") or "", "cls": r.get("cls") or "",
            "fee_total": round(float(fee), 6),
            "mgmt_fee": r.get("mgmt_fee"), "custody_fee": r.get("custody_fee"),
            # 雪球费率明细缺项=0（A 类通常无销售服务费）；显示「0」而非「—」
            "sales_fee": r.get("sales_fee") if r.get("sales_fee") is not None else 0.0,
            "purchase_fee": r.get("purchase_fee"),
            "purchase_fee_disc": r.get("purchase_fee_disc"),
            "scale_yi": r.get("scale_yi"),
            "company": code2co.get(r["code"]),
            "company_scale": _round(co2scale.get(code2co.get(r["code"], "")), 2),
            "ret_1y": (rets.get(r["code"]) or {}).get("ret_1y"),
            "ret_3y": (rets.get(r["code"]) or {}).get("ret_3y"),
            "purchase_status": st,
            "suspended": D.sgzt_suspended(st),
        })
    rows.sort(key=lambda x: (x["suspended"], x["fee_total"],
                             -(x["company_scale"] or 0), -(x["scale_yi"] or 0)))
    return rows[:n]


# ---------------------------------------------------------------- 7. 汇总 payload
def build(log=print, refresh: bool = True, budget_s: float | None = None) -> dict | None:
    """组装 payload["eq_index"]。任何异常/缺数据只降级，不抛出到管线。"""
    cfg = C.EQ_INDEX
    if not cfg.get("enabled"):
        return None
    t0 = time.time()
    cache = _load_fund_cache()
    funds = cache.get("funds") or []
    if not funds:
        log("[7][指数估值] 未建库（先运行 python -m fundscorer.eqindex --ingest）→ 跳过")
        return None
    mapped = [r for r in funds if r.get("idx_code")]
    by_idx: dict[str, list[dict]] = {}
    for r in mapped:
        by_idx.setdefault(r["idx_code"], []).append(r)
    rets = fund_returns(refresh)
    code2co, co2scale = _company_scale()

    # 申购状态：每指数按费率取前 N 只预取，再重排截 5（refresh=False 仅读缓存）
    pre = cfg["status_prefetch_n"]
    pre_codes: list[str] = []
    for recs in by_idx.values():
        recs2 = sorted([r for r in recs if r.get("fee_total") is not None],
                       key=lambda x: x["fee_total"])[:pre]
        pre_codes.extend(r["code"] for r in recs2)
    status = D.fetch_purchase_status(sorted(set(pre_codes)), refresh=refresh) if pre_codes else {}

    deadline = time.time() + float(budget_s or cfg["budget_s"])
    indices: list[dict] = []
    missing: list[dict] = []
    for entry in CATALOG:
        # v1.3：不再按主流/延后名单裁剪——目录全量进 payload（含红利/价值/低波等），
        # 「重要与否」交给前端默认 Top N + 查看更多（用户 2026-10-30 拍板）
        recs = by_idx.get(entry["code"]) or []
        # v1.3：无匹配基金的指数也展示（fund_count=0，详情提示无场外基金）
        if time.time() > deadline:
            missing.append({"code": entry["code"], "name": entry["name"],
                            "reason": "估值抓取预算超时"})
            continue
        val = build_index_valuation_v2(entry, refresh)
        if not val:
            missing.append({"code": entry["code"], "name": entry["name"],
                            "reason": "估值数据缺失/样本不足"})
            continue
        val["fund_count"] = len(recs)
        val["mainstream"] = bool(entry.get("mainstream"))
        val["funds"] = select_funds(recs, status, rets, code2co, co2scale,
                                    cfg["fund_list_n"])
        fees = [f["fee_total"] for f in val["funds"]]
        val["min_fee_total"] = min(fees) if fees else None
        indices.append(val)

    indices.sort(key=lambda x: (GROUP_ORDER.index(x["group"])
                                if x["group"] in GROUP_ORDER else 99,
                                x["percentile"] if x["percentile"] is not None else 999))
    asof = max((x.get("valuation_date") or "") for x in indices) if indices else ""
    tier_cnt: dict[str, int] = {}
    for x in indices:
        tier_cnt[x["tier"]] = tier_cnt.get(x["tier"], 0) + 1
    log(f"[7][指数估值] 目录 {len(CATALOG)} / 展示 {len(indices)} / 缺 {len(missing)}"
        f"（默认每组前 {cfg['hot_per_group']} 只，其余进「查看更多」）；"
        f"基金 {sum(x['fund_count'] for x in indices)} 只；档位 "
        + "、".join(f"{k}{v}" for k, v in tier_cnt.items())
        + f"；耗时 {time.time() - t0:.1f}s")
    return {"version": cfg["version"], "asof": asof,
            "source": "中证指数官网 PE/点位 + 雪球基金 PB（分位为程序自算）",
            "catalog_n": len(CATALOG),
            "mainstream_n": sum(1 for c in CATALOG if c.get("mainstream") and not c.get("defer")),
            "hot_per_group": cfg["hot_per_group"],
            "funds_n": len(mapped),
            "indices": indices, "missing": missing}


# ---------------------------------------------------------------- 8. CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="指数估值模块（债指之外的指数型基金）")
    ap.add_argument("--ingest", action="store_true", help="全量建库（基金→指数映射）")
    ap.add_argument("--build", action="store_true", help="只组装 payload（读缓存）")
    ap.add_argument("--check", action="store_true", help="打印覆盖/未匹配统计")
    ap.add_argument("--limit", type=int, default=None, help="建库只跑前 N 只（调试）")
    ap.add_argument("--force", action="store_true", help="忽略建库缓存强制重抓")
    args = ap.parse_args(argv)

    if args.ingest:
        ingest(log=print, limit=args.limit, force=args.force)
    if args.build or not (args.ingest or args.check):
        out = build(log=print, refresh=True)
        print(json.dumps({"indices": len(out["indices"]) if out else 0,
                          "funds_n": out["funds_n"] if out else 0,
                          "missing": len(out["missing"]) if out else 0},
                         ensure_ascii=False))
    if args.check:
        cache = _load_fund_cache()
        funds = cache.get("funds") or []
        mapped = [r for r in funds if r.get("idx_code")]
        cnt: dict[str, int] = {}
        for r in mapped:
            cnt[r["idx_code"]] = cnt.get(r["idx_code"], 0) + 1
        print(f"建库时间 {cache.get('fetched_at')}；总 {len(funds)}；匹配 {len(mapped)}；"
              f"指数 {len(cnt)}/{len(CATALOG)}")
        for e in CATALOG:
            n = cnt.get(e["code"], 0)
            if n == 0:
                print(f"  无基金：{e['code']} {e['name']}")
        unmatched = [r for r in funds if not r.get("idx_code") and r.get("benchmark")]
        for r in unmatched[:20]:
            print(f"  未匹配：{r['code']} {r.get('name', '')[:16]} ← {r.get('benchmark', '')[:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
