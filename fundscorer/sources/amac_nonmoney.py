"""中国证券投资基金业协会（AMAC）官方：基金管理机构非货币理财公募基金月均规模排名。

接口（JSON，无需登录）：
  GET https://www.amac.org.cn/portal/front/statics/fundIndData/findFundManageOrgans?year=YYYY&quarter=N
  GET https://www.amac.org.cn/portal/front/statics/fundIndData/exportFundManageOrgans?startYear=YYYY&endYear=YYYY  (.xls)

口径（协会官方说明，findExplainAndAttachs?route=fundManage）：
  「1-3季度为本季度各月末规模算术平均，4季度为本年12个月末规模算术平均。
    各月末规模剔除了短期理财债券基金规模和基金中基金持有的自身管理的基金规模。」
  单位：亿元。每个季度只发布 **Top 20**。

⚠ 数据新鲜度：实测最新只到 **2024年3季度**（默认请求返回 curQuarter=2024-3）。
   2024Q4 及 2025-2026 各季均返回 0 行。导出接口 2020Q1–2024Q3 共 19 个季度 × 20 家。
   作为「官方口径校准/回填」可用，不能作为实时数据源。
"""

from __future__ import annotations

import io

import pandas as pd
import requests

_UA = "Mozilla/5.0"
_BASE = "https://www.amac.org.cn/portal/front/statics/fundIndData"
_HEADERS = {"User-Agent": _UA, "Referer": "https://www.amac.org.cn/sjtj/datastatistics/fundindustrydata/"}


def fetch_quarter(year: int | str, quarter: int) -> pd.DataFrame:
    """某季度官方非货月均规模 Top20。返回列: 季度, 排名, 公司, 非货月均规模_亿元"""
    r = requests.get(
        f"{_BASE}/findFundManageOrgans",
        params={"year": str(year), "quarter": str(quarter)},
        headers=_HEADERS,
        timeout=30,
    )
    r.raise_for_status()
    rows = (r.json().get("data") or {}).get("data") or {}
    data = rows.get("amsomcfofmiVOs") or []
    if not data:
        return pd.DataFrame(columns=["季度", "排名", "公司", "非货月均规模_亿元"])
    df = pd.DataFrame(data)
    df = df.rename(columns={
        "ranking": "排名", "companyName": "公司", "fundScale": "非货月均规模_亿元",
    })
    df["季度"] = rows.get("curQuarter")
    df["非货月均规模_亿元"] = pd.to_numeric(df["非货月均规模_亿元"], errors="coerce")
    cols = ["季度", "排名", "公司", "非货月均规模_亿元"]
    return df[[c for c in cols if c in df.columns]]


def fetch_history(start_year: int = 2020, end_year: int | None = None) -> pd.DataFrame:
    """批量导出（.xls，OLE 格式）。返回列同上，含多个季度。"""
    import datetime as _dt

    end_year = end_year or _dt.date.today().year
    r = requests.get(
        f"{_BASE}/exportFundManageOrgans",
        params={"startYear": start_year, "endYear": end_year},
        headers=_HEADERS,
        timeout=40,
    )
    r.raise_for_status()
    df = pd.read_excel(io.BytesIO(r.content), header=1)
    df.columns = ["季度", "排名", "公司", "非货月均规模_亿元"]
    df = df.dropna(subset=["公司"])
    df["排名"] = pd.to_numeric(df["排名"], errors="coerce")
    df["非货月均规模_亿元"] = pd.to_numeric(df["非货月均规模_亿元"], errors="coerce")
    return df.reset_index(drop=True)


def latest_quarter() -> str | None:
    """当前接口发布的最新季度（如 '2024年3季度'）。"""
    hist = fetch_history()
    return hist["季度"].iloc[0] if len(hist) else None


if __name__ == "__main__":
    h = fetch_history()
    print(f"共 {len(h)} 行，{h['季度'].nunique()} 个季度，最新: {latest_quarter()}")
    print(h[h["季度"] == h["季度"].iloc[0]].head(10).to_string(index=False))
