# -*- coding: utf-8 -*-
"""fundscorer —— 分类型基金评分框架（当前实现债券类：纯债型 / 固收+）。

方法论：docs/01-债券基金评分指南.md (V5.2)
用法：
    from fundscorer import score_funds
    results = score_funds(["110017", "006980"])
"""
from .scorer import classify, collect_metrics, score_pool, save_snapshot

__version__ = "5.0"


def score_funds(codes: list[str]) -> list[dict]:
    """对一批基金代码评分：抓数据 → 指标 → 同池分位打分 → 星级。"""
    collected = []
    errors = {}
    for code in codes:
        try:
            collected.append(collect_metrics(code))
        except Exception as e:      # 单只失败不影响整批
            errors[code] = str(e)
    if len(collected) < 2:
        # 池内少于2只时分位无意义，仍输出绝对指标+质地分
        for f in collected:
            f["warning"] = "同池样本<2，分位分仅供参考"
    results = score_pool(collected)
    if errors:
        for r in results:
            r["_errors"] = errors
    return results
