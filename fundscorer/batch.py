# -*- coding: utf-8 -*-
"""全集构建 + 并行批量抓取（可断点续爬）。

流程：
  build_universe()  从天天基金全量基金列表（含细分类型）筛出债券类全集并分类分池
  crawl()           多线程抓取净值/F10/机构比例，写入 data/cache/（即断点）
  score_all()       从缓存读取，全池分位打分，输出各池榜单
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

from . import config as C
from . import data
from .scorer import POOL_LABEL, collect_metrics, score_pool

UNIVERSE_PATH = Path(__file__).resolve().parent.parent / "data" / "universe.csv"
_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

# 纯债 vs 固收+ 的类型映射（依据 docs/01 分类规则 R0；债券型-混合一级/二级按已确认口径归固收+）
_TYPE_TO_POOL = {
    "债券型-中短债": "short_bond",
    "债券型-长债": "long_bond",
    "债券型-信用债": "long_bond",
    "债券型-利率债": "long_bond",
    "债券型-混合一级": "fixed_plus",
    "债券型-混合二级": "fixed_plus",
    "混合型-偏债": "fixed_plus",
}
BOND_TYPES = set(_TYPE_TO_POOL)


def fetch_fund_list() -> list[list]:
    """全量基金列表 [code, url, name, type, pinyin]。缓存 3 天。"""
    cache = data._load_cache("fund_list_all", 24 * 3)
    if cache is not None:
        return list(cache)
    req = urllib.request.Request("http://fund.eastmoney.com/js/fundcode_search.js", headers=_UA)
    raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", errors="ignore")
    arr = json.loads(re.search(r"\[\[.*\]\]", raw, re.S).group(0))
    data._save_cache("fund_list_all", arr)
    return arr


def build_universe() -> pd.DataFrame:
    """筛出债券类全集（债券型 6 子类 + 混合型-偏债）并分类分池，落盘 universe.csv。

    排除封闭定开债（用户已确认）：名称含'XX个月定开'等定期开放结构的基金，
    多为摊余成本法估值、机构持有100%、封闭期66~88个月，净值近乎零回撤零波动
    是估值会计造成的假象（隐藏利率/流动性风险），且散户买不到，不适合入榜。
    """
    arr = fetch_fund_list()
    rows = []
    skipped_closed = 0
    for code, _url, name, ptype, _py in arr:
        if ptype not in BOND_TYPES:
            continue
        if _is_closed_end(name):
            skipped_closed += 1
            continue
        pool = _TYPE_TO_POOL[ptype]
        rows.append({"code": code, "name": name, "type_text": ptype, "pool": pool})
    df = pd.DataFrame(rows).drop_duplicates("code").reset_index(drop=True)
    df.to_csv(UNIVERSE_PATH, index=False, encoding="utf-8-sig")
    print(f"全集 {len(df)} 只，排除封闭定开 {skipped_closed} 只", flush=True)
    return df


# 封闭定开债：'66个月定开'、'定开'（含'广发鑫惠纯债定开'、'江信一年定开'等无月份前缀写法）、
# '定期开放'、'封闭式'、'X年封闭' 等结构
_CLOSED_END_RE = re.compile(
    r"定开|定期开放|封闭式|封闭运作|\d+\s*年\s*封闭"
)


def _is_closed_end(name: str) -> bool:
    return bool(_CLOSED_END_RE.search(name or ""))


def load_universe() -> pd.DataFrame:
    if UNIVERSE_PATH.exists():
        df = pd.read_csv(UNIVERSE_PATH, dtype={"code": str})
        return _exclude_small_company(df)
    return build_universe()


# ---------------------------------------------------------------- 公司规模排除（R10）
_ROOT = Path(__file__).resolve().parent.parent
_company_scale_cache: pd.Series | None = None


def _load_company_scale() -> pd.Series | None:
    """code → 非货规模(亿元)。数据缺失返回 None（调用方保留该基金，不冤枉排除）。"""
    global _company_scale_cache
    if _company_scale_cache is not None:
        return _company_scale_cache
    import re as _re
    cfg = C.COMPANY_SCALE
    map_p, scale_p = _ROOT / cfg["map_path"], _ROOT / cfg["scale_path"]
    if not map_p.exists() or not scale_p.exists():
        return None
    mp = pd.read_csv(map_p, dtype={"code": str})
    sc = pd.read_csv(scale_p)

    def norm(s: str) -> str:
        s = _re.sub(r"(基金管理|财产管理)?(股份)?(有限)?(责任)?公司$", "", str(s))
        return _re.sub(r"基金$", "", s).strip()

    scale_by_key = dict(zip(sc["归一化公司名"].map(norm), sc["非货规模_亿元"]))
    mp = mp.assign(scale=mp["company"].map(lambda c: scale_by_key.get(norm(c))))
    mp = mp.dropna(subset=["scale"]).drop_duplicates("code")
    _company_scale_cache = mp.set_index("code")["scale"]
    return _company_scale_cache


def _exclude_small_company(df: pd.DataFrame) -> pd.DataFrame:
    """基金公司非货规模 < 门槛（300亿）直接排除（用户：小公司风险大）。
    无映射/无规模数据 → 保留。"""
    scale = _load_company_scale()
    if scale is None:
        print("[公司规模] 数据文件缺失，跳过排除", flush=True)
        return df
    thr = C.COMPANY_SCALE["exclude_below_yi"]
    s = df["code"].map(scale)
    small = s.notna() & (s < thr)
    if small.any():
        names = ", ".join(df.loc[small].head(5)["name"].str[:10])
        print(f"排除基金公司非货规模<{thr}亿: {int(small.sum())} 只（如 {names}...）", flush=True)
    return df[~small].reset_index(drop=True)


def _fetch_one(code: str) -> str | None:
    """抓取单只基金的 nav/f10/inst（写入缓存）。返回 code 或 None（失败）。"""
    try:
        data.fetch_nav(code)
        data.fetch_f10(code)
        data.fetch_inst_ratio(code)
        return code
    except Exception:
        return None


def crawl(codes: list[str], workers: int = 8, log_every: int = 200) -> dict:
    """并行抓取，缓存即断点（已抓过的基金 fetch_* 直接命中缓存，几乎瞬时）。

    返回 {ok, fail, failed_codes}。
    """
    t0 = time.time()
    ok, failed = 0, []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch_one, c): c for c in codes}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            if r is None:
                failed.append(futs[fut])
            else:
                ok += 1
            if i % log_every == 0:
                el = time.time() - t0
                eta = el / i * (len(codes) - i)
                print(f"  进度 {i}/{len(codes)}  成功 {ok}  失败 {len(failed)}  "
                      f"已用 {el/60:.1f}min  预计剩余 {eta/60:.1f}min", flush=True)
    return {"ok": ok, "fail": len(failed), "failed_codes": failed}


def collect_all(uni: pd.DataFrame, retry: int = 1,
                cache_path: Path | None = None) -> list[dict]:
    """对全集逐只计算指标（读缓存，无网络除经理产品/评级外）。失败自动重试。

    cache_path 提供时：命中的 code 直接复用上次计算结果，只对新 code 计算。
    """
    import pickle
    done: dict[str, dict] = {}
    if cache_path and Path(cache_path).exists():
        try:
            done = {r["code"]: r for r in pickle.loads(Path(cache_path).read_bytes())}
            print(f"  命中指标缓存 {len(done)} 只", flush=True)
        except Exception:
            done = {}

    out, failed = [], []
    codes = [c for c in uni["code"].tolist()]
    for i, code in enumerate(codes, 1):
        if code in done:
            out.append(done[code])
            continue
        try:
            out.append(collect_metrics(code))
        except Exception:
            failed.append(code)
        if i % 500 == 0:
            print(f"  指标计算 {i}/{len(codes)}  累计失败 {len(failed)}", flush=True)
    # 重试一次失败的
    for code in failed[:]:
        try:
            out.append(collect_metrics(code))
            failed.remove(code)
        except Exception:
            pass
    # 落盘（供下次增量）
    if cache_path:
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        slim = [{k: v for k, v in r.items() if k != "_nav_df"} for r in out]
        Path(cache_path).write_bytes(pickle.dumps(slim))
    return out


# ---------------------------------------------------------------- 周更：净值固化 + 强制重算
def persist_nav(codes: list[str], nav_dir: Path | None = None,
                workers: int = 8, log_every: int = 300) -> dict:
    """把净值写入 data/nav_full/{code}.pkl（screen/backtest 的离线净值库）。

    nav_full 此前由一次性手工脚本维护（仓库内无写入代码），周更管线必须自带此步骤：
    crawl() 刷新 nav 缓存后调用，把最新全历史净值固化到 nav_full。返回 {ok, fail, failed_codes}。
    """
    nav_dir = Path(nav_dir) if nav_dir else _ROOT / "data" / "nav_full"
    nav_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    ok, failed = 0, []

    def _one(code: str) -> str | None:
        try:
            nav = data.fetch_nav(code)   # 缓存命中（crawl 刚刷新），几乎无网络
            if nav is None or nav.empty:
                return None
            pd.to_pickle(nav, nav_dir / f"{code}.pkl")
            return code
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_one, c): c for c in codes}
        for i, fut in enumerate(as_completed(futs), 1):
            if fut.result() is None:
                failed.append(futs[fut])
            else:
                ok += 1
            if i % log_every == 0:
                print(f"  nav_full 固化 {i}/{len(codes)}  已用 {(time.time()-t0)/60:.1f}min",
                      flush=True)
    return {"ok": ok, "fail": len(failed), "failed_codes": failed}


def refresh_pool_metrics(codes: list[str], cache_path: Path,
                         pool: str, retry: int = 1) -> dict:
    """强制重算指定 code 的指标并**合并**写回 metrics_cache（周更核心步骤）。

    collect_all 对缓存命中的 code 直接复用旧指标——净值刷新后 ret/mdd/任期不会更新
    （陈旧隐患）。本函数对 codes 全部重算；其余池/其余 code 的旧记录原样保留。
    返回 {refreshed, kept, failed, failed_codes}。
    """
    import pickle
    old: dict[str, dict] = {}
    if Path(cache_path).exists():
        try:
            old = {r["code"]: r for r in pickle.loads(Path(cache_path).read_bytes())}
        except Exception:
            old = {}

    t0 = time.time()
    out: dict[str, dict] = {}
    failed: list[str] = []
    for i, code in enumerate(codes, 1):
        try:
            out[code] = collect_metrics(code)
        except Exception:
            failed.append(code)
        if i % 200 == 0:
            print(f"  指标重算 {i}/{len(codes)}  失败 {len(failed)}  "
                  f"已用 {(time.time()-t0)/60:.1f}min", flush=True)
    for code in failed[:retry + 1]:
        try:
            out[code] = collect_metrics(code)
            failed.remove(code)
        except Exception:
            pass

    # 合并：重算成功的替换，失败的沿用旧记录（数据新鲜度降级但不丢基金），无旧记录则缺
    refreshed = len(out)
    for code, rec in out.items():
        old[code] = rec
    kept_fallback = sum(1 for c in failed if c in old)
    slim = [{k: v for k, v in r.items() if k != "_nav_df"} for r in old.values()]
    Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(cache_path) + ".tmp")
    tmp.write_bytes(pickle.dumps(slim))
    tmp.replace(cache_path)   # 原子替换，中断不毁缓存
    return {"refreshed": refreshed, "kept": len(slim), "failed": len(failed),
            "failed_codes": failed, "stale_fallback": kept_fallback,
            "minutes": round((time.time() - t0) / 60, 1)}
