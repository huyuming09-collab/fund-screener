"""content_digest.py 单测：素材包结构、数字一致性、环比标记、本地落盘（合成数据，无网络）。

脚本在 scripts/ 下且非包，用 importlib 按路径加载。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "content_digest", _ROOT / "scripts" / "content_digest.py")
CD = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CD)

DISCLAIMER = "本内容为历史数据分析结论，不构成投资建议。"


def _fund(rank, code, name, category, *, score, delta=None, new=False, prev=None,
          scale=None, ret_1y=None):
    return {
        "rank": rank, "code": code, "name": name, "category": category,
        "score": score, "rank_prev": prev, "rank_delta": delta, "is_new": new,
        "screening": {"scale_sum": scale, "comp_pct": 0.7, "yearly_med": 0.6,
                      "max_gain_5y": 0.003},
        "metrics": {"ret_1y": ret_1y, "mdd_5y": 0.0123, "sharpe_5y": 3.21},
    }


def _payload(*, has_prev_long: bool = False) -> dict:
    return {
        "asof": "2026-10-17", "generated_at": "2026-10-17 11:31:45",
        "model_version": "v5.2", "category": "short_bond", "top_n": 20,
        "n": 20, "n_screened": 23, "has_prev": True,
        "disclaimer": DISCLAIMER,
        "categories": [
            {"id": "short_bond", "name": "短债", "enabled": True},
            {"id": "long_bond", "name": "中长债", "enabled": True},
            {"id": "fixed_plus", "name": "固收+", "enabled": False},
        ],
        "pools": {
            "short_bond": {"n": 3, "n_screened": 23, "top_n": 20, "has_prev": True,
                           "funnel": {"合并基金数": 482, "P2 年度分位中位≥55%": 23},
                           "screen": {"scale_min_yi": 5, "comp_pct_min": 0.45,
                                      "yearly_median_min": 0.55, "spike_max_gain": 0.01}},
            "long_bond": {"n": 2, "n_screened": 76, "top_n": 20, "has_prev": has_prev_long,
                          "funnel": {"合并基金数": 945, "P2 年度分位中位≥55%": 76},
                          "screen": {"scale_min_yi": 5, "comp_pct_min": 0.45,
                                     "yearly_median_min": 0.55, "spike_max_gain": 0.02}},
        },
        "funnel": {"合并基金数": 482, "P2 年度分位中位≥55%": 23},
        "funds": [
            _fund(1, "002438", "甲短债A", "short_bond", score=86.9, delta=2, prev=3,
                  scale=9.81, ret_1y=0.0274),
            _fund(2, "011961", "乙短债A", "short_bond", score=85.7, new=True, prev=None,
                  scale=12.0, ret_1y=0.0311),
            _fund(3, "006980", "丙短债A", "short_bond", score=84.0, delta=-1, prev=2,
                  scale=9.04, ret_1y=0.02),
            _fund(1, "003102", "丁长债A", "long_bond", score=92.1, prev=None,
                  scale=99.88, ret_1y=0.0436),
            _fund(2, "006980", "国寿安保泰恒纯债债券", "long_bond", score=87.0, prev=None,
                  scale=9.04, ret_1y=0.04),
        ],
    }


class TestBuildDigest:
    def test_four_files_generated(self):
        files = CD.build_digest(_payload(), "2026-W42", ["006980"], 10)
        assert set(files) == {"要点.md", "小红书骨架.md", "公众号骨架.md", "数据切片.json"}

    def test_digest_contains_numbers_and_disclaimer(self):
        text = CD.build_digest(_payload(), "2026-W42", ["006980"], 10)["要点.md"]
        assert "2026-10-17" in text and DISCLAIMER in text
        assert "合并基金数 482" in text and "合并基金数 945" in text   # 双池漏斗都在
        assert "甲短债A" in text and "丁长债A" in text                  # 双池榜单都在
        assert "86.9" in text and "9.81" in text                       # 分数与规模原值
        assert "短债榜**第 3 名**" in text                              # 锚点两池都报
        assert "中长债榜**第 2 名**" in text

    def test_delta_marks(self):
        text = CD.build_digest(_payload(), "2026-W42", [], 10)["要点.md"]
        assert "↑2" in text and "↓1" in text and "新增" in text

    def test_first_period_pool_has_no_delta(self):
        first = CD.build_digest(_payload(), "2026-W42", [], 10)["要点.md"]
        later = CD.build_digest(_payload(has_prev_long=True), "2026-W42", [], 10)["要点.md"]
        # 长债 has_prev=false → 该池表格环比列全为「首期—」且变动段声明首期
        assert "首期—" in first
        assert "无上一期历史" in first and "不显示环比" in first
        # 下期起 has_prev=true → 首期标记消失
        assert "首期—" not in later
        assert "名次零变动" in later

    def test_slim_json_serializable_and_matches_input(self):
        raw = CD.build_digest(_payload(), "2026-W42", ["006980"], 10)["数据切片.json"]
        data = json.loads(raw)
        assert data["week"] == "2026-W42" and data["asof"] == "2026-10-17"
        assert len(data["funds"]) == 5
        assert data["disclaimer"] == DISCLAIMER
        top1 = next(f for f in data["funds"] if f["name"] == "丁长债A")
        assert top1["score"] == 92.1 and top1["scale_sum"] == 99.88

    def test_disabled_pool_not_in_tables(self):
        text = CD.build_digest(_payload(), "2026-W42", [], 10)["要点.md"]
        assert "固收+" not in text.split("## 二")[1]   # 榜单表不为禁用池生成

    def test_watch_outside_rank_flagged(self):
        text = CD.build_digest(_payload(), "2026-W42", ["110017"], 10)["要点.md"]
        assert "110017" in text and "本期未入榜" in text


class TestMain:
    def test_writes_to_local_archive(self, tmp_path):
        payload = tmp_path / "top20.json"
        payload.write_text(json.dumps(_payload(), ensure_ascii=False))
        out = tmp_path / "榜单"
        rc = CD.main(["--payload", str(payload), "--out", str(out), "--week", "2026-W42",
                      "--watch", "006980"])
        assert rc == 0
        week_dir = out / "2026-W42"
        assert (week_dir / "要点.md").exists()
        assert (week_dir / "小红书骨架.md").exists()
        assert (week_dir / "公众号骨架.md").exists()
        assert (week_dir / "数据切片.json").exists()
        assert DISCLAIMER in (week_dir / "公众号骨架.md").read_text()

    def test_missing_payload_returns_error(self, tmp_path):
        rc = CD.main(["--payload", str(tmp_path / "nope.json"), "--out", str(tmp_path)])
        assert rc == 1

    def test_week_defaults_to_asof_iso_week(self, tmp_path):
        payload = tmp_path / "top20.json"
        payload.write_text(json.dumps(_payload(), ensure_ascii=False))
        out = tmp_path / "榜单"
        assert CD.main(["--payload", str(payload), "--out", str(out)]) == 0
        # 2026-10-17 落在 2026 年第 42 周
        assert (out / "2026-W42" / "要点.md").exists()
