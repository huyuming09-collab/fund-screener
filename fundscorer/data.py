# -*- coding: utf-8 -*-
"""数据层：天天基金/AKShare 抓取 + 本地缓存。

数据源优先级（见 docs/03 调研）：AKShare 主源，天天基金 F10 直连兜底。
所有抓取结果写入 data/cache/，按 TTL 复用，避免重复请求给数据源压力。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}


def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.pkl"


def _load_cache(key: str, max_age_hours: float):
    p = _cache_path(key)
    if p.exists() and (time.time() - p.stat().st_mtime) < max_age_hours * 3600:
        try:
            return pd.read_pickle(p)
        except Exception:
            pass
    return None


def _save_cache(key: str, obj) -> None:
    try:
        pd.to_pickle(obj, _cache_path(key))
    except Exception:
        pass


def _http(url: str, encoding: str = "gbk", referer: str | None = None) -> str:
    """GET + 轻量重试（仅网络类异常重试 2 次，退避 1s/3s；4xx 不重试）。"""
    headers = dict(_UA)
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    last: Exception | None = None
    for i in range(3):
        try:
            return urllib.request.urlopen(req, timeout=20).read().decode(
                encoding, errors="ignore")
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:        # 请求错误重试无意义
                raise
            last = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
        if i < 2:
            time.sleep(1 if i == 0 else 3)
    raise last  # type: ignore[misc]


# ---------------------------------------------------------------- 1. 净值序列（复权）
def _neutralize_glitches(df: pd.DataFrame, code: str = "") -> pd.DataFrame:
    """清洗复权净值中的单日异常坏点。

    天天基金 equityReturn 偶尔混入 +134%/+193% 等坏点，一个坏点即可把整只基金的
    年化/回撤/波动全部毁掉。债券基金单日不可能超 ±NAV_GLITCH_RET，超限则视当日
    收益为坏点、中和为 0，再重建累计净值。对缓存数据同样生效。
    """
    from . import config as C
    df = df.sort_values("date").reset_index(drop=True)
    if len(df) < C.GLITCH_MIN_POINTS:
        return df
    ret = df["nav"].pct_change()
    glitch = ret.abs() > C.NAV_GLITCH_RET
    n = int(glitch.sum())
    if n:
        ret = ret.mask(glitch, 0.0)          # 坏点当日收益中和为 0
        base = df["nav"].iloc[0]
        df = df.copy()
        df["nav"] = base * (1 + ret.fillna(0)).cumprod()
        print(f"    [清洗] {code} 命中 {n} 个单日坏点，已中和", flush=True)
    return df


def fetch_nav(code: str) -> pd.DataFrame:
    """返回列 [date, ret] 的日频【复权】收益率序列（已含分红再投资）。

    数据源：天天基金 pingzhongdata 的 Data_netWorthTrend.equityReturn（复权日增长率）。
    注意：不能用"单位净值"直接算收益——高分红债基会严重低估（如 110017 单位净值口径
    5年年化仅 0.5%，复权口径 4.24%）。缓存 20 小时。
    """
    cached = _load_cache(f"nav_{code}", 20)
    if cached is not None and "nav" in cached.columns:
        return _neutralize_glitches(cached, code)
    raw = _http(f"http://fund.eastmoney.com/pingzhongdata/{code}.js", encoding="utf-8")
    m = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", raw)
    if not m:
        raise ValueError(f"{code} 未取到净值序列")
    import json as _json
    arr = _json.loads(m.group(1))
    df = pd.DataFrame(arr)
    if df.empty or "equityReturn" not in df.columns:
        raise ValueError(f"{code} 净值序列为空")
    df["date"] = pd.to_datetime(df["x"], unit="ms")
    df["nav"] = (1 + df["equityReturn"] / 100).cumprod()   # 复权净值
    df = df[["date", "nav"]].dropna().sort_values("date").reset_index(drop=True)
    df = _neutralize_glitches(df, code)
    _save_cache(f"nav_{code}", df)
    return df


# ---------------------------------------------------------------- 2. F10 基本概况
def _parse_purchase_fee(html: str) -> tuple[float | None, float | None]:
    """F10“最高申购费率”行 → (原价, 天天基金优惠价)。

    行结构（实测 002438/003547/519724 一致）：
      <th>最高申购费率</th><td><span划线>0.80%（前端）</span><br>
      <span>天天基金优惠费率：<span红字>0.08%（前端）</span></span></td>
    优惠行可能缺失（仅原价）；无申购费显示 0%。
    """
    m = re.search(r"最高申购费率</th>\s*<td[^>]*>(.*?)</td>", html, re.S)
    if not m:
        return None, None
    block = m.group(1)
    nums = re.findall(r"([\d.]+)\s*%", block)
    if not nums:
        return None, None
    orig = float(nums[0]) / 100
    disc = float(nums[1]) / 100 if ("优惠费率" in block and len(nums) > 1) else None
    return orig, disc


def fetch_f10(code: str) -> dict:
    """基金基本概况：类型、成立日期、规模、费率（含申购原价/优惠价）、经理、基准。

    缓存 7 天；key 含 v2：旧缓存字典缺 purchase_fee 字段，换 key 强制一次性重抓。
    """
    cached = _load_cache(f"f10_v2_{code}", 24 * 7)
    if cached is not None:
        return cached.to_dict() if isinstance(cached, pd.Series) else cached
    html = _http(f"https://fundf10.eastmoney.com/jbgk_{code}.html", encoding="utf-8")

    def grab(pat, default=""):
        m = re.search(pat, html)
        return m.group(1).strip() if m else default

    _pf, _pd = _parse_purchase_fee(html)
    info = {
        "code": code,
        "name": grab(r"基金简称</th><td>([^<]+)<"),
        "type_text": grab(r"基金类型</th>\s*<td[^>]*>([^<]+)<"),
        "establish_date": _parse_cn_date(grab(r"成立日期/规模</th>\s*<td[^>]*>([\d年月日\-]+)")),
        "scale_yi": _parse_yi(grab(r"净资产规模</th>\s*<td[^>]*>([^<]+)<")),
        "mgmt_fee": _parse_pct(grab(r"管理费率</th>\s*<td[^>]*>([\d.<>%—\-]+)")),
        "custody_fee": _parse_pct(grab(r"托管费率</th>\s*<td[^>]*>([\d.<>%—\-]+)")),
        "sales_fee": _parse_pct(grab(r"销售服务费率</th>\s*<td[^>]*>([\d.<>%—\-]+)")),
        "purchase_fee": _pf,          # 申购原价（划线价）
        "purchase_fee_disc": _pd,     # 天天基金优惠价（无优惠行 → None）
        "manager": grab(r"基金经理人</th>\s*<td[^>]*>\s*<a[^>]*>([^<]+)<"),
        "manager_id": grab(r"fund\.eastmoney\.com/manager/(\d+)\.html"),
        "benchmark": grab(r"业绩比较基准</th>\s*<td[^>]*>([^<]+)<"),
    }
    # 经理任职起始（F10 基金经理页第一行"至今"）
    try:
        jjjl = _http(f"https://fundf10.eastmoney.com/jjjl_{code}.html", encoding="utf-8")
        m = re.search(r"<td>([\d-]{10})</td><td>至今</td>", jjjl)
        info["manager_start"] = m.group(1) if m else ""
    except Exception:
        info["manager_start"] = ""
    _save_cache(f"f10_v2_{code}", info)
    return info


def _parse_tenure_days(text: str) -> int | None:
    """'15年又63天' / '6年又140天' / '181天' → 总天数。"""
    if not text:
        return None
    total = 0
    m_y = re.search(r"(\d+)年", text)
    m_d = re.search(r"(\d+)天", text)
    if m_y:
        total += int(m_y.group(1)) * 365
    if m_d:
        total += int(m_d.group(1))
    return total if (m_y or m_d) else None


def _parse_cn_date(text: str) -> str:
    """'2008年03月19日' → '2008-03-19'；已是 YYYY-MM-DD 则原样返回。"""
    text = (text or "").strip()
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.match(r"(\d{4}-\d{2}-\d{2})", text)
    return m.group(1) if m else ""


def _parse_yi(text: str) -> float | None:
    m = re.search(r"([\d.]+)", text or "")
    return float(m.group(1)) if m else None


def _parse_pct(text: str) -> float | None:
    if not text or "—" in text or "-" == text.strip():
        return None
    m = re.search(r"([\d.]+)", text)
    return float(m.group(1)) / 100 if m else None


# ---------------------------------------------------------------- 3. 机构持有比例
def fetch_inst_ratio(code: str) -> float | None:
    """最新一期机构持有比例（0~1）。缓存 7 天。"""
    cached = _load_cache(f"inst_{code}", 24 * 7)
    if cached is not None:
        return float(cached)
    try:
        raw = _http(f"https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=cyrjg&code={code}",
                    encoding="utf-8",
                    referer=f"https://fundf10.eastmoney.com/cyrjg_{code}.html")
        m = re.search(r"机构持有比例</th>.*?<td[^>]*>([\d.]+)%</td>", raw, re.S)
        val = float(m.group(1)) / 100 if m else None
    except Exception:
        val = None
    if val is not None:
        _save_cache(f"inst_{code}", val)
    return val


# ---------------------------------------------------------------- 4. 基金经理全产品年化
_MGR_TABLE_CACHE: pd.DataFrame | None = None


def _manager_em_table() -> pd.DataFrame:
    """全市场经理表（累计从业时间）。缓存 3 天。"""
    global _MGR_TABLE_CACHE
    if _MGR_TABLE_CACHE is not None:
        return _MGR_TABLE_CACHE
    cached = _load_cache("mgr_em_all", 24 * 3)
    if cached is not None:
        _MGR_TABLE_CACHE = cached
        return cached
    import akshare as ak
    df = ak.fund_manager_em()
    _save_cache("mgr_em_all", df)
    _MGR_TABLE_CACHE = df
    return df


def fetch_manager_career_years(manager_name: str, code: str | None = None) -> float | None:
    """经理累计从业年限（年）。

    重名消歧：经理表重名极多（如 '刘杰' 61 人），仅按姓名取首行会张冠李戴
    （实测全市场 114 条取错、短债池 26 条，5 只基金 S2 判定翻转）。
    传入 code 时优先按'现任基金代码'精确定位；代码未命中且姓名唯一仍可用；
    代码未命中且重名 → 返回 None（数据不足不猜测）。
    """
    df = _manager_em_table()
    if df is None or df.empty or not manager_name:
        return None
    sub = df[df["姓名"] == manager_name]
    if sub.empty:
        return None
    if code is not None:
        try:
            hit = sub[pd.to_numeric(sub["现任基金代码"], errors="coerce") == int(code)]
        except (TypeError, ValueError):
            hit = sub.iloc[0:0]
        if not hit.empty:
            sub = hit
        elif len(sub) > 1:
            return None
    text = str(sub.iloc[0]["累计从业时间"])
    m = re.search(r"(\d+)", text)
    if "年" in text and m:
        return float(m.group(1))
    m2 = re.search(r"(\d+)", text)
    return round(int(m2.group(1)) / 365, 1) if m2 else None


def fetch_manager_products_ann(manager_id: str) -> list[float]:
    """经理职业生涯全部产品（含已离任）的任职年化收益列表。
    剔除：任职 <2 年的产品（年化无意义）、货币基金（与目标池无可比性）。
    调用方对列表取中位数（scorer.collect_metrics），理由见 docs/01 §5.5。缓存 7 天。"""
    cached = _load_cache(f"mgrprod_{manager_id}", 24 * 7)
    if cached is not None:
        return list(cached)
    anns: list[float] = []
    if not manager_id:
        return anns
    try:
        html = _http(f"https://fund.eastmoney.com/manager/{manager_id}.html", encoding="utf-8")
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S)
        for r in rows:
            cells = [re.sub(r"<[^>]+>", "", c).strip()
                     for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)]
            if len(cells) < 8 or not re.match(r"^\d{6}$", cells[0]):
                continue
            days = _parse_tenure_days(cells[6]) if len(cells) > 6 else None
            ret_m = re.match(r"(-?[\d.]+)%", cells[7]) if len(cells) > 7 else None
            if not (days and ret_m):
                continue
            ret = float(ret_m.group(1)) / 100
            if days < 730:      # 任职 <2 年不计（§5.5）
                continue
            if "货币" in cells[1]:
                continue
            try:
                anns.append((1 + ret) ** (365 / days) - 1)
            except OverflowError:
                continue
    except Exception:
        pass
    _save_cache(f"mgrprod_{manager_id}", anns)
    return anns


# ---------------------------------------------------------------- 5. 晨星评级
_RATING_CACHE: pd.DataFrame | None = None


def fetch_rating(code: str) -> float | None:
    """晨星评级（1~5）。全表缓存 3 天。"""
    global _RATING_CACHE
    if _RATING_CACHE is None:
        cached = _load_cache("rating_all", 24 * 3)
        if cached is not None:
            _RATING_CACHE = cached
        else:
            import akshare as ak
            _RATING_CACHE = ak.fund_rating_all()
            _save_cache("rating_all", _RATING_CACHE)
    row = _RATING_CACHE[_RATING_CACHE["代码"] == code]
    if row.empty:
        return None
    val = row.iloc[0]["晨星评级"]
    try:
        v = float(val)
        return v if 1 <= v <= 5 else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 6. 中债综合财富指数（基准）
def fetch_bond_index(short: bool = False) -> pd.DataFrame:
    """中债财富指数日线，列 [date, value]。缓存 20 小时。

    short=False：中债新综合财富(总值)，全久期基准（纯债·中长期 / 固收+ 池用）。
    short=True ：中债新综合财富【1年以下】分段，短端基准（纯债·短债池用）。
                 短债久期短、收益天然低于综合指数（2.14% vs 4.19%），共用综合指数
                 会让短债池 99% 基金 LVI 为负、无法拉开差距。
    """
    key = "bond_index_wealth_short" if short else "bond_index_wealth"
    cached = _load_cache(key, 20)
    if cached is not None:
        return cached
    import akshare as ak
    if short:
        df = ak.bond_index_general_cbond(index_category="新综合指数",
                                         indicator="财富", period="1年以下")
    else:
        df = ak.bond_composite_index_cbond(indicator="财富", period="总值")
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    _save_cache(key, df)
    return df
