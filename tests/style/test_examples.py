"""P1-2 风格示例库（iris_memory/style/style_examples_v1.json + examples.py）

验收②：每条 styled 过 P0-6 规则零命中、过 §12.4 零命中；示例库与评测集 user 字段去重；
反应词开头分布不超过 §5.5 的告警线；每个有示例的场景至少 1 条反例。
"""

from __future__ import annotations

import json
import re
from collections import Counter

from astrbot_plugin_iris_memory.iris_memory.lifelike.guards import numeric
from astrbot_plugin_iris_memory.iris_memory.lifelike.style import guard
from astrbot_plugin_iris_memory.iris_memory.style import examples as EX
from astrbot_plugin_iris_memory.iris_memory.style import register as R
from astrbot_plugin_iris_memory.tools.lifelike_eval import evalset


def _raw():
    with open(EX.SEED_PATH, encoding="utf-8") as f:
        return json.load(f)


def test_seed_loads_without_drops():
    EX.clear_cache()
    data = _raw()
    lib, dropped = EX.parse_library(data)
    assert dropped == 0
    assert len(lib) == len(data["examples"]) >= 25
    assert EX.load_examples("") == lib


def test_styled_zero_guard_hits_and_zero_copy_hits():
    for ex in EX.load_examples(""):
        assert EX.blocking_hits(ex.styled, technical=ex.scene == R.SCENE_HELP) == [], ex.id
        assert EX.copy_hits(ex.styled) == [], ex.id
        assert EX.copy_hits(ex.user) == [], ex.id
        if ex.anti:
            assert EX.copy_hits(ex.anti) == [], ex.id


def test_no_dash_and_no_digits():
    for raw in _raw()["examples"]:
        for key in ("user", "styled", "anti", "anti_reason"):
            text = raw.get(key, "")
            assert "——" not in text, raw["id"]
            assert not re.search(r"\d", text), raw["id"]


def test_all_reviewed_original():
    for raw in _raw()["examples"]:
        assert raw["source"] == "作者原创" and raw["reviewed"] is True


def test_every_scene_has_anti():
    lib = EX.load_examples("")
    scenes = {ex.scene for ex in lib}
    for scene in scenes:
        assert any(ex.anti for ex in lib if ex.scene == scene), scene


def test_scenes_and_channels_known():
    known = set(R._SCENE_TABLE)
    for ex in EX.load_examples(""):
        assert ex.scene in known
        assert set(ex.channels) <= set(EX.CHANNELS)
        if "group" in ex.channels:
            assert not ex.cute  # 群聊不卖萌示例


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", text or "")


def test_user_fields_not_in_evalset():
    eval_inputs = {_norm(t) for c in evalset.load() for t in c.turns}
    for ex in EX.load_examples(""):
        assert _norm(ex.user) not in eval_inputs, ex.id


def test_reaction_word_distribution_under_alarm():
    lib = EX.load_examples("")
    counts = Counter(guard.extract_markers(ex.styled).reaction for ex in lib)
    counts.pop(None, None)
    for word, n in counts.items():
        assert n / len(lib) <= 0.30, word


def test_numeric_guard_keeps_examples(monkeypatch):
    monkeypatch.setattr(numeric, "guard_enabled", lambda: True)
    for ex in EX.load_examples(""):
        assert numeric.guard_text(ex.styled, source="style") == ex.styled


def test_override_path_and_fallback(tmp_path):
    EX.clear_cache()
    good = {
        "examples": [
            {"id": "x1", "scene": "chat", "channel": ["private"], "register": "medium",
             "user": "今天好冷", "styled": "是有点冷，出门多穿一件。", "reviewed": True},
            {"id": "x2", "scene": "chat", "channel": "private", "register": "medium",
             "user": "今天好热", "styled": "人家也觉得好热。", "reviewed": True},  # 人家 → 丢
            {"id": "x3", "scene": "chat", "channel": ["private"], "register": "medium",
             "user": "a", "styled": "b", "reviewed": False},  # 未审校 → 丢
            {"id": "x4", "scene": "chat", "channel": ["private"], "register": "loud",
             "user": "a", "styled": "b", "reviewed": True},  # 档位非法 → 丢
            {"id": "x1", "scene": "chat", "channel": ["private"], "register": "medium",
             "user": "今天好冷", "styled": "是有点冷。", "reviewed": True},  # 重复 id → 丢
            {"id": "x5", "scene": "chat", "channel": ["private"], "register": "light",
             "user": "下雪了", "styled": "下雪了，路上慢点走。", "reviewed": True,
             "anti": "我最喜欢星光怀表了", "anti_reason": "照搬"},  # 反例命中照搬 → 去掉反例
            "not a dict",
        ]
    }
    path = tmp_path / "lib.json"
    path.write_text(json.dumps(good, ensure_ascii=False), encoding="utf-8")
    lib = EX.load_examples(str(path))
    assert [e.id for e in lib] == ["x1", "x5"]
    assert lib[1].anti == "" and lib[0].channels == ("private",)
    # 读不到 → 回退种子库
    seed = EX.load_examples("")
    assert EX.load_examples(str(tmp_path / "missing.json")) == seed
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert EX.load_examples(str(bad)) == seed
    bad.write_text("[]", encoding="utf-8")
    assert EX.load_examples(str(bad)) == seed


def test_seed_unreadable_returns_empty(monkeypatch, tmp_path):
    EX.clear_cache()
    monkeypatch.setattr(EX, "SEED_PATH", tmp_path / "nope.json")
    assert EX.load_examples("") == []
    EX.clear_cache()
