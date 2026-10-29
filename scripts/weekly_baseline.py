# -*- coding: utf-8 -*-
"""周更基准快照：把「上一期周五」的产物固化下来，供周六文案做名次对比。

背景（用户 2026-10-29 定）：周中可能为了抓数据跑一次管线，会把 rank_history/top20 写到当天，
导致周五产物的 `rank_prev` 指向"周中"而不是"上周五"。规则是
**周更文案的名次变化一律对比上一周周五**，所以：

  python scripts/weekly_baseline.py save            # 周更成功后调用（wrapper 内），存当期快照
  python scripts/weekly_baseline.py list            # 看有哪些基准
  python scripts/weekly_baseline.py restore 2026-10-24   # 周中跑完管线后回滚产物与 rank_history

快照位置：data/snapshots/weekly/<asof>/（.gitignore 已排除 data/snapshots/）
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
BASE = DATA / "snapshots" / "weekly"
TOP20 = DATA / "top20.json"
DATAJS = ROOT / "web" / "data.js"
HISTORY = DATA / "rank_history.json"


def _asof() -> str:
    d = json.loads(TOP20.read_text(encoding="utf-8"))
    return str(d.get("asof") or "").strip()


def save() -> int:
    asof = _asof()
    if not asof:
        print("[错误] top20.json 缺 asof")
        return 1
    out = BASE / asof
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(TOP20, out / "top20.json")
    if DATAJS.exists():
        shutil.copy2(DATAJS, out / "data.js")
    print(f"[ok] 基准已存：{out}（asof {asof}）")
    return 0


def list_() -> int:
    if not BASE.exists():
        print("（无基准）")
        return 0
    for p in sorted(BASE.iterdir()):
        if p.is_dir():
            n = len(list(p.glob("*.json"))) + len(list(p.glob("*.js")))
            print(f"{p.name}  （{n} 个文件）")
    return 0


def restore(date: str) -> int:
    src = BASE / date
    if not (src / "top20.json").exists():
        print(f"[错误] 基准不存在：{src}")
        return 1
    shutil.copy2(src / "top20.json", TOP20)
    if (src / "data.js").exists():
        shutil.copy2(src / "data.js", DATAJS)
    # rank_history 去掉基准日之后的所有条目（周中跑出来的那些）
    if HISTORY.exists():
        h = json.loads(HISTORY.read_text(encoding="utf-8"))
        keep = {k: v for k, v in h.items() if k <= date}
        dropped = sorted(set(h) - set(keep))
        HISTORY.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")
        print(f"[ok] 已回滚产物到 {date}；rank_history 删除 {dropped or '无'}")
    else:
        print(f"[ok] 已回滚产物到 {date}")
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "save":
        return save()
    if cmd == "list":
        return list_()
    if cmd == "restore" and len(argv) > 2:
        return restore(argv[2])
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
