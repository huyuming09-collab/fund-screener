"""基金公司管理规模（总规模 / 非货币规模）数据源。

数据源：东方财富（天天基金）基金公司页
  - 总规模   GET https://fund.eastmoney.com/Company/home/gspmlist?fundType=0
             HTML 表，215 家公司，含"全部管理规模(亿元)"与"更新日期"（近实时，日级更新）
  - 货币型   GET https://fund.eastmoney.com/Company/home/HistoryScaleTable?year=YYYY
             HTML 表，按年（当年 = 最新已披露季度），含 货币型 规模(亿元)

口径：
  非货规模(亿元) = 总规模(近实时) - 货币型规模(季末)
  对没有货币基金的公司（历史表中货币型为 '-'）则 非货 = 总规模。

实测精度（2026-06-30，对比财联社/东财公布的官方非货 Top10）：
  pearson 0.99920，中位误差 0.30%，最大误差 3.17%（华夏，因其 ETF 联接口径差异）。

关联 key：公司全称，如 "易方达基金管理有限公司"。
  与基金数据里的简称（如 "易方达基金"）关联时用 normalize_company_name()。
"""

from __future__ import annotations

import re
from io import StringIO

import pandas as pd
import requests

_UA = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
_HEADERS = {"User-Agent": _UA}
_GSPM = "https://fund.eastmoney.com/Company/home/gspmlist"
_HIST = "https://fund.eastmoney.com/Company/home/HistoryScaleTable"

# 去掉这些后缀后做归一化匹配（易方达基金管理有限公司 / 易方达基金 / 易方达 -> 易方达）
_SUFFIX = re.compile(
    r"(基金管理(股份)?有限公司|资产管理(股份)?有限公司|有限责任公司|股份有限公司|有限公司|基金管理|基金|证券)$"
)


def normalize_company_name(name: str) -> str:
    """把"易方达基金管理有限公司"、"易方达基金"、"易方达"都归一成"易方达"。"""
    if not isinstance(name, str):
        return ""
    s = name.strip()
    prev = None
    while s and s != prev:
        prev = s
        s = _SUFFIX.sub("", s)
    return s.strip() or name.strip()


def _read_table(url: str, **params) -> pd.DataFrame:
    """抓取东财 HTML 表；必须 decode('utf-8','replace')，否则列名乱码。"""
    resp = requests.get(url, params=params, headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    html = resp.content.decode("utf-8", "replace")
    tables = pd.read_html(StringIO(html))
    if not tables:
        raise ValueError(f"no table parsed from {url}")
    df = tables[0]
    # 该表偶发多出一个空列（部分年份 10 列）
    if len(df.columns) == 10:
        df = df.drop(columns=[df.columns[3]])
    return df


def fetch_total_scale() -> pd.DataFrame:
    """公司 -> 总规模(亿元, 近实时)。返回列: 公司, 总规模_亿元, 更新日期, 基金数, 经理数, 成立时间"""
    df = _read_table(_GSPM, fundType="0")
    df.columns = ["序号", "公司", "相关", "成立时间", "评级", "_scale", "基金数", "经理数"]
    df["总规模_亿元"] = pd.to_numeric(
        df["_scale"].astype(str).str.replace(",", "", regex=False).str.split().str[0],
        errors="coerce",
    )
    df["更新日期"] = df["_scale"].astype(str).str.split().str[1]
    df["基金数"] = pd.to_numeric(df["基金数"], errors="coerce")
    df["经理数"] = pd.to_numeric(df["经理数"], errors="coerce")
    return df.drop(columns=["_scale"])


def fetch_money_scale(year: int | str | None = None) -> pd.DataFrame:
    """公司 -> 货币型规模(亿元, 季末)。year 默认取当前年（= 最新已披露季度）。

    返回列: 公司, 总规模_亿元, 货币型_亿元, 股票型_亿元, 混合型_亿元, 债券型_亿元, 指数型_亿元, QDII_亿元
    """
    if year is None:
        import datetime as _dt

        year = _dt.date.today().year
    df = _read_table(_HIST, year=str(year))
    df.columns = [
        "序号", "公司", "总规模_亿元", "股票型_亿元", "混合型_亿元",
        "债券型_亿元", "指数型_亿元", "QDII_亿元", "货币型_亿元",
    ]
    df = df.dropna(subset=["公司"])
    for c in df.columns[2:]:
        # 源站用 '-' 表示该类别规模为 0 / 无
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.drop(columns=["序号"])


def get_company_scale(backfill_years: tuple[int, ...] = ()) -> pd.DataFrame:
    """总规模 + 货币型 -> 非货规模。

    非货规模_亿元 = 总规模_亿元 - 货币型_亿元（无货币基金的公司，货币型为空 -> 非货=总规模）

    参数 backfill_years: 若当年历史表缺失，按序用这些年份的货币型补齐（如 (2025, 2024)）。
    """
    total = fetch_total_scale()
    money = fetch_money_scale()

    out = total[["公司", "总规模_亿元", "更新日期", "基金数", "经理数", "成立时间"]].copy()
    out = out.merge(money[["公司", "货币型_亿元"]], on="公司", how="left")

    for y in backfill_years:
        miss = out["货币型_亿元"].isna()
        if not miss.any():
            break
        alt = fetch_money_scale(y)[["公司", "货币型_亿元"]].rename(
            columns={"货币型_亿元": "_alt"}
        )
        tmp = out.loc[miss, ["公司"]].merge(alt, on="公司", how="left")
        out.loc[miss, "货币型_亿元"] = tmp["_alt"].values

    # 总规模为 '-' 的行：52 家，几乎全是证券公司（页内不报基金规模），
    # 另含华宸未来、施罗德(中国) 两家迷你基金公司 —— 直接剔除。
    out = out[out["总规模_亿元"].notna()].copy()

    # 货币型为空 = 没有货币基金（已逐家核实：睿远/泉果/中庚/朱雀/贝莱德等页内确无货币基金表）
    money_filled = out["货币型_亿元"].fillna(0.0)
    out["非货规模_亿元"] = out["总规模_亿元"] - money_filled
    out["货币型_亿元"] = money_filled

    out = out.sort_values("非货规模_亿元", ascending=False).reset_index(drop=True)
    out["归一化公司名"] = out["公司"].map(normalize_company_name)
    return out


def lookup(df: pd.DataFrame, company: str) -> dict | None:
    """按全称或简称查一家公司；优先精确匹配，再按归一化名匹配。"""
    hit = df[df["公司"] == company]
    if hit.empty:
        key = normalize_company_name(company)
        hit = df[df["归一化公司名"] == key]
    return hit.iloc[0].to_dict() if not hit.empty else None


if __name__ == "__main__":
    df = get_company_scale(backfill_years=(2025, 2024))
    print(f"共 {len(df)} 家，可算非货 {df['非货规模_亿元'].notna().sum()} 家")
    print(df.head(15)[["公司", "总规模_亿元", "货币型_亿元", "非货规模_亿元", "更新日期"]].to_string(index=False))
