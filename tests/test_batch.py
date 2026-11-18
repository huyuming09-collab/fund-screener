# -*- coding: utf-8 -*-
"""batch.py 单测（2026-11-18 权益仓位<30% 路由固收+）。"""
import pandas as pd

from fundscorer import batch, data


def test_avg_pos10_reads_g3_cache(tmp_path, monkeypatch):
    """仓位读 G3 的 pos_*.pkl 缓存（list[dict] 格式）：均值跳过 None；
    无缓存 → None（不入路由，不放水）。"""
    import pickle
    monkeypatch.setattr(data, "CACHE_DIR", tmp_path)
    with open(tmp_path / "pos_000001.pkl", "wb") as fh:
        pickle.dump([dict(d="2026-09-30", gp=20.0),
                     dict(d="2026-06-30", gp=None),
                     dict(d="2026-03-31", gp=40.0)], fh)
    assert batch._avg_pos10("000001") == 30.0        # (20+40)/2，None 跳过
    assert batch._avg_pos10("000002") is None        # 无缓存
    (tmp_path / "pos_000003.pkl").write_bytes(b"broken")
    assert batch._avg_pos10("000003") is None        # 损坏 → None（不炸）


def test_equity_route_types_mirror_pool():
    """路由类型集=股混池类型（镜像 eqgate.POOL_TYPES，不能含债券类型——债类本就在全集）。"""
    from fundscorer.eqgate import POOL_TYPES
    assert set(batch._EQUITY_ROUTE_TYPES) == set(POOL_TYPES)
    assert not (set(batch._EQUITY_ROUTE_TYPES) & set(batch.BOND_TYPES))
