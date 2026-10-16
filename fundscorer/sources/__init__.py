"""可编程外部数据源封装（均经实际抓取验证）。"""

from .company_scale import (  # noqa: F401
    fetch_money_scale,
    fetch_total_scale,
    get_company_scale,
    lookup,
    normalize_company_name,
)

__all__ = [
    "fetch_total_scale",
    "fetch_money_scale",
    "get_company_scale",
    "lookup",
    "normalize_company_name",
]
