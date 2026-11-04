"""P1-3 learning 反自我强化（口吻附录 §5.5）

- 表达模式：加权随机 + 冷却（同一模式连续若干轮不重复）、语气词结尾配额、纯反应词不注入；
- 衰减：假时钟 30 天，已通过模式数不因本改动减少（15 天清空问题）；
- few-shot：按本轮场景过滤（淡档场景不注入浓档 / 卖萌样例）+ 冷却；
- 抽取：反应词开头不收、句尾语气词剥掉后不足 4 字不收；开关 true 恢复旧规则。
"""

from __future__ import annotations

import time
from collections import Counter
from unittest.mock import MagicMock, patch

import pytest

from astrbot_plugin_iris_memory.iris_memory.learning import expression, injector
from astrbot_plugin_iris_memory.iris_memory.learning import collector as collector_mod
from astrbot_plugin_iris_memory.iris_memory.learning.injector import build_learning_context
from astrbot_plugin_iris_memory.iris_memory.learning.jargon import JargonLearner

_ADAPTER = "astrbot_plugin_iris_memory.iris_memory.learning.injector.get_adapter"


def _adapter(group="g1"):
    a = MagicMock()
    # 私聊时分组键回落到会话 id；测试数据统一写在 "g1" 下
    a.get_session_id.return_value = "sess1" if group else "g1"
    a.get_group_id.return_value = group
    a.get_user_id.return_value = "u1"
    return a


def _event(text="今天外面风好大"):
    e = MagicMock()
    e.message_str = text
    return e


@pytest.fixture(autouse=True)
def _reset():
    injector.reset_rotation_state(seed=11)
    yield
    injector.reset_rotation_state()


def _seed_patterns(storage, n=20, tone_every=4):
    ids = []
    for i in range(n):
        expr = f"表达模式第{i}号句子" + ("呀" if i % tone_every == 0 else "")
        pid = storage.insert_pattern("g1", "chat", expr)
        storage.update_status("expression_pattern", [pid], "approved")
        ids.append(pid)
    return ids


# ---------------------------------------------------------------- 抽取


class TestExtraction:
    def test_legacy_default_unchanged(self):
        assert expression.extract_expressions("今天天气真好呀。") == ["今天天气真好呀"]
        assert expression.extract_expressions("哈哈哈，比惨大会开起来了。") == ["哈哈哈"]

    def test_reaction_opening_dropped(self):
        assert expression.extract_expressions("哈哈哈，比惨大会开起来了。", legacy=False) == ["比惨大会开起来了"]
        assert expression.extract_expressions("嗯……啊，好吧。", legacy=False) == []
        assert expression.extract_expressions("好耶，", legacy=False) == []

    def test_tail_particle_stripped(self):
        assert expression.extract_expressions("今天天气真好呀。", legacy=False) == ["今天天气真好"]
        assert expression.extract_expressions("好的呀。", legacy=False) == []
        assert expression.extract_expressions("没事啦啦。", legacy=False) == []

    def test_reaction_short_sentence_dropped(self):
        assert expression.extract_expressions("哇好厉害！", legacy=False) == []
        assert expression.extract_expressions("诶，真抢到了？", legacy=False) == ["真抢到了"]
        assert expression.extract_expressions("嗯嗯。", legacy=False) == []

    def test_opening_address_kept(self):
        assert expression.extract_expressions("宝子，这个问题问得好。", legacy=False) == ["宝子"]
        assert expression.extract_expressions("我倒是觉得可以。", legacy=False) == ["我倒是觉得可以"]

    def test_switch_reading(self, config):
        assert collector_mod._tail_particle_rule_enabled() is False  # 默认新规则
        config.set_hidden("learning_expression_tail_particle_rule", True)
        assert collector_mod._tail_particle_rule_enabled() is True

    def test_switch_read_failure_is_legacy(self):
        with patch("astrbot_plugin_iris_memory.iris_memory.config.get_config", side_effect=RuntimeError):
            assert collector_mod._tail_particle_rule_enabled() is True

    def test_collector_uses_new_rule(self, config, storage):
        from astrbot_plugin_iris_memory.iris_memory.learning.collector import LearningCollector

        reviewer = MagicMock()
        col = LearningCollector(storage, JargonLearner(storage), reviewer)
        resp = MagicMock()
        resp.completion_text = "哈哈哈，今天天气真好呀。"
        with patch.object(collector_mod, "get_adapter", return_value=_adapter()), \
                patch.object(collector_mod, "cutesy_gate_enabled", return_value=False):
            col.on_response(_event("天气怎么样"), resp)
        exprs = [r["expression"] for r in storage._db.execute("SELECT expression FROM expression_pattern").fetchall()]
        assert exprs == ["今天天气真好"]


# ---------------------------------------------------------------- 表达模式轮换


class TestPatternRotation:
    @pytest.mark.asyncio
    async def test_no_repeat_in_consecutive_rounds_and_tone_quota(self, config, storage):
        _seed_patterns(storage, 30)
        rounds = []
        with patch(_ADAPTER, return_value=_adapter()):
            for _ in range(6):
                meta = {}
                text = await build_learning_context(_event(), storage, JargonLearner(storage), meta)
                lines = [ln for ln in text.splitlines() if ln.startswith("- [chat]")]
                rounds.append({ln for ln in lines})
                assert sum(1 for ln in lines if ln.endswith("呀")) <= 1
                assert meta["pattern_count"] == len(lines) >= 4
        for i in range(len(rounds) - 1):
            for j in range(i + 1, min(i + 4, len(rounds))):
                assert not (rounds[i] & rounds[j])

    @pytest.mark.asyncio
    async def test_hits_still_recorded(self, config, storage):
        _seed_patterns(storage, 20)
        with patch(_ADAPTER, return_value=_adapter()):
            await build_learning_context(_event(), storage, JargonLearner(storage), {})
        total = storage._db.execute("SELECT SUM(hit_count) AS s FROM expression_pattern").fetchone()["s"]
        assert total == 5

    @pytest.mark.asyncio
    async def test_reaction_only_patterns_not_injected(self, config, storage):
        for expr in ("哈哈哈", "嗯……啊", "这个说法挺好"):
            pid = storage.insert_pattern("g1", "chat", expr)
            storage.update_status("expression_pattern", [pid], "approved")
        with patch(_ADAPTER, return_value=_adapter()):
            text = await build_learning_context(_event(), storage, JargonLearner(storage), {})
        assert "这个说法挺好" in text and "哈哈哈" not in text and "嗯……啊" not in text

    @pytest.mark.asyncio
    async def test_rotation_off_is_top_n(self, config, storage):
        ids = _seed_patterns(storage, 8, tone_every=99)
        storage._db.execute("UPDATE expression_pattern SET hit_count=id")
        storage._db.commit()
        config.set_hidden("learning_pattern_rotation", False)
        with patch(_ADAPTER, return_value=_adapter()):
            for _ in range(3):
                meta = {}
                await build_learning_context(_event(), storage, JargonLearner(storage), meta)
        rows = {r["id"]: r["hit_count"] for r in storage._db.execute("SELECT id,hit_count FROM expression_pattern")}
        top5 = sorted(ids, reverse=True)[:5]
        assert all(rows[i] == i + 3 for i in top5)
        assert all(rows[i] == i for i in ids if i not in top5)

    @pytest.mark.asyncio
    async def test_top5_share_drops_vs_legacy(self, config, storage):
        ids = _seed_patterns(storage, 30, tone_every=99)
        storage._db.execute("UPDATE expression_pattern SET hit_count=CASE WHEN id<=5 THEN 30 ELSE 1 END")
        storage._db.commit()
        top5 = set(ids[:5])
        counts = Counter()
        with patch(_ADAPTER, return_value=_adapter()):
            for _ in range(40):
                text = await build_learning_context(_event(), storage, JargonLearner(storage), {})
                counts.update(ln for ln in text.splitlines() if ln.startswith("- [chat]"))
        exprs = {r["id"]: r["expression"] for r in storage._db.execute("SELECT id,expression FROM expression_pattern")}
        top_lines = {f"- [chat] {exprs[i]}" for i in top5}
        share = sum(n for ln, n in counts.items() if ln in top_lines) / sum(counts.values())
        assert share < 0.5  # 旧排序下恒为 1.0

    @pytest.mark.asyncio
    async def test_small_pool_still_injects_every_round(self, config, storage):
        _seed_patterns(storage, 3, tone_every=99)
        with patch(_ADAPTER, return_value=_adapter()):
            for _ in range(3):
                meta = {}
                await build_learning_context(_event(), storage, JargonLearner(storage), meta)
                assert meta["pattern_count"] == 3

    @pytest.mark.asyncio
    async def test_rotation_failure_falls_back(self, config, storage):
        _seed_patterns(storage, 3, tone_every=99)
        with patch(_ADAPTER, return_value=_adapter()), \
                patch.object(injector, "_rotate_patterns", side_effect=RuntimeError), \
                patch.object(injector, "_rotate_few_shots", side_effect=RuntimeError):
            meta = {}
            await build_learning_context(_event(), storage, JargonLearner(storage), meta)
        assert meta["pattern_count"] == 3

    def test_rotation_state_bounded(self):
        for i in range(injector._ROTATION_MAX_KEYS + 5):
            injector._rounds_for(injector._pattern_rounds, (f"g{i}", "p"), 3)
        assert len(injector._pattern_rounds) == injector._ROTATION_MAX_KEYS


# ---------------------------------------------------------------- 衰减（15 天清空问题）


class TestDecay:
    def _simulate(self, storage, monkeypatch, *, rotation: bool) -> int:
        clock = {"now": 1_800_000_000.0}
        monkeypatch.setattr(time, "time", lambda: clock["now"])
        _seed_patterns(storage, 40, tone_every=99)
        for _day in range(30):
            for _ in range(2):
                if rotation:
                    picked = injector._rotate_patterns(storage, "g1", "default", 5)
                else:
                    picked = storage.get_approved_patterns("g1", 5, "default")
                storage.record_pattern_hit([int(p["id"]) for p in picked])
            clock["now"] += 86400
            expression.decay(storage, 15, 300, rotation=rotation)
        return storage._db.execute(
            "SELECT COUNT(*) AS c FROM expression_pattern WHERE status='approved'"
        ).fetchone()["c"]

    def test_30_days_rotation_keeps_all(self, storage, monkeypatch):
        assert self._simulate(storage, monkeypatch, rotation=True) == 40

    def test_30_days_legacy_baseline_shrinks(self, tmp_path, monkeypatch):
        from astrbot_plugin_iris_memory.iris_memory.learning import LearningStorage

        s = LearningStorage(tmp_path / "legacy.db")
        s.init_schema()
        try:
            assert self._simulate(s, monkeypatch, rotation=False) < 40
        finally:
            s.close()

    def test_disuse_window_still_applies_eventually(self, storage, monkeypatch):
        clock = {"now": 1_800_000_000.0}
        monkeypatch.setattr(time, "time", lambda: clock["now"])
        _seed_patterns(storage, 3, tone_every=99)
        clock["now"] += 61 * 86400
        assert expression.decay(storage, 15, 300, rotation=True) == 3

    def test_overflow_still_enforced(self, storage):
        _seed_patterns(storage, 10, tone_every=99)
        assert storage.decay_patterns(15, 6, disuse_days=60) == 4

    @pytest.mark.asyncio
    async def test_component_passes_rotation(self, config):
        from astrbot_plugin_iris_memory.iris_memory.learning.component import LearningComponent

        comp = LearningComponent()
        await comp.initialize()
        try:
            with patch.object(expression, "decay") as dec:
                await comp.run_decay()
            assert dec.call_args.kwargs == {"rotation": True}
            config.set_hidden("learning_pattern_rotation", False)
            with patch.object(expression, "decay") as dec:
                await comp.run_decay()
            assert dec.call_args.kwargs == {"rotation": False}
        finally:
            await comp.shutdown()


# ---------------------------------------------------------------- few-shot 按场景


def _seed_few_shots(storage):
    rows = [
        ("今天外面风好大", "风大就别出门啦。"),
        ("你好聪明啊", "被你发现了，再多夸两句。"),  # 卖萌
        ("python 怎么读文件", "用 open 打开再 read。"),
        ("docker 时区不对怎么办", "挂载宿主机的 localtime。"),
        ("哈哈哈笑死我了", "哈哈，你也太好笑了。"),
    ]
    for u, b in rows:
        pid = storage.insert_pair("g1", "u1", u, b)
        storage.update_status("few_shot", [pid], "approved")


class TestFewShotScene:
    @pytest.mark.asyncio
    async def test_tech_turn_only_gets_light_samples(self, config, storage):
        _seed_few_shots(storage)
        with patch(_ADAPTER, return_value=_adapter(group="")):
            text = await build_learning_context(_event("git 提交错了分支怎么撤回？"), storage, JargonLearner(storage), {})
        assert "用 open 打开再 read" in text or "挂载宿主机" in text
        assert "风大就别出门" not in text and "再多夸两句" not in text and "太好笑" not in text

    @pytest.mark.asyncio
    async def test_chat_turn_gets_any_but_cute_only_when_allowed(self, config, storage):
        _seed_few_shots(storage)
        config.set_hidden("learning_few_shot_max", 5)
        with patch(_ADAPTER, return_value=_adapter(group="")):
            meta = {}
            await build_learning_context(_event("今天外面风好大"), storage, JargonLearner(storage), meta)
        assert meta["few_shot_count"] == 5
        injector.reset_rotation_state(seed=3)
        with patch(_ADAPTER, return_value=_adapter(group="")):
            text = await build_learning_context(_event("我先睡了"), storage, JargonLearner(storage), {})
        assert "再多夸两句" not in text  # 不卖萌场景不注入卖萌样例

    @pytest.mark.asyncio
    async def test_few_shot_cooldown(self, config, storage):
        for i in range(9):
            pid = storage.insert_pair("g1", "u1", f"今天天气第{i}天", f"嗯，第{i}天也不错。")
            storage.update_status("few_shot", [pid], "approved")
        seen = []
        with patch(_ADAPTER, return_value=_adapter(group="")):
            for _ in range(3):
                text = await build_learning_context(_event("今天外面风好大"), storage, JargonLearner(storage), {})
                seen.append({ln for ln in text.splitlines() if ln.startswith("  回复：")})
        assert not (seen[0] & seen[1]) and not (seen[1] & seen[2]) and not (seen[0] & seen[2])

    @pytest.mark.asyncio
    async def test_filter_off_is_latest_n(self, config, storage):
        _seed_few_shots(storage)
        config.set_hidden("learning_few_shot_scene_filter", False)
        with patch(_ADAPTER, return_value=_adapter(group="")):
            text = await build_learning_context(_event("git 提交错了分支怎么撤回？"), storage, JargonLearner(storage), {})
        # 旧行为：最新 3 条，不看场景
        assert "太好笑" in text and "挂载宿主机" in text and "用 open 打开再 read" in text
