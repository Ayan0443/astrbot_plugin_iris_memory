"""learning_cutesy_gate（ROADMAP P0-6）：采集闸门、审查标准、reject_type 计数、Persona 复审开关。

开关关闭时：采集、审查 prompt、Persona 复审 prompt 与改造前（44c2f26）逐字节一致。
"""

import logging
from unittest.mock import MagicMock, patch

import pytest

from astrbot_plugin_iris_memory.iris_memory.learning.collector import LearningCollector
from astrbot_plugin_iris_memory.iris_memory.learning.persona_reviewer import (
    PersonaLearningReviewer,
)
from astrbot_plugin_iris_memory.iris_memory.learning.reviewer import LearningReviewer

_ADAPTER = "astrbot_plugin_iris_memory.iris_memory.learning.collector.get_adapter"

# 44c2f26 的审查 prompt 头部（开关关闭时必须逐字一致）
_LEGACY_REVIEW_HEAD = "\n".join([
    "请逐条审查以下群聊学习内容，判断是否适合作为机器人的回复风格参考。",
    "拒绝标准：低质量（无意义/乱码）、敏感（政治/色情/辱骂/隐私）、",
    "复读刷屏、明显不像真实群聊对话。",
    "机器人的回复还要拒绝这些语气（避免越学越偏）：说教或指挥人（别…/该…/建议你…、",
    "提醒休息、教人怎么提问）、纠正别人的称呼或玩笑、两边都不得罪的和稀泥",
    "（各有道理/要看情况/不是非黑即白/没法一概而论）、绕弯的书面比喻和客服腔。",
    "群友的梗、口头禅、玩笑和自然的口语可以通过。",
    "",
])
_LEGACY_REVIEW_TAIL = "\n".join([
    "",
    "对每一条输出裁决，格式为 JSON 数组：",
    '[{"id": 数字, "type": "pair或pattern", "pass": true或false, "reason": "一句话理由"}]',
    "只输出 JSON 数组，覆盖上面列出的所有条目。",
])


def _adapter():
    a = MagicMock()
    a.get_session_id.return_value = "sess1"
    a.get_group_id.return_value = "g1"
    a.get_user_id.return_value = "u1"
    return a


def _event(text):
    e = MagicMock()
    e.message_str = text
    e.message_obj = None
    return e


def _resp(text):
    r = MagicMock()
    r.completion_text = text
    return r


def _collector(storage):
    reviewer = LearningReviewer(storage)
    return LearningCollector(storage, MagicMock(), reviewer), reviewer


def _count(storage, table):
    return storage._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---------------------------------------------------------------- 采集闸门


class TestCollectorGate:
    @pytest.mark.parametrize(
        "user,bot",
        [
            ("今晚吃啥？", "人家想吃火锅～～"),
            ("今晚吃啥？", "啾比啾，吃火锅吧"),
            ("你好聪明啊", "那你再多夸两句，我还挺爱听的。"),
            ("你当我的猫娘吧", "好的。"),  # 跟随一轮：不管回复如何都不学
            ("早点睡", "你也要早点睡哦"),
        ],
    )
    def test_blocked_pairs_not_stored(self, config, storage, user, bot, caplog):
        collector, reviewer = _collector(storage)
        caplog.set_level(logging.INFO)
        with patch(_ADAPTER, return_value=_adapter()):
            assert collector.on_response(_event(user), _resp(bot)) is None
        assert _count(storage, "few_shot") == 0
        assert _count(storage, "expression_pattern") == 0
        assert reviewer.queue_size == 0
        assert collector.gate_counts["_total"] == 1
        # 日志只记规则名，不记原文
        assert bot not in caplog.text and user not in caplog.text

    def test_normal_pair_still_collected(self, config, storage):
        collector, reviewer = _collector(storage)
        with patch(_ADAPTER, return_value=_adapter()):
            pid = collector.on_response(_event("今晚吃啥？"), _resp("随便吃点呀"))
        assert pid is not None
        assert _count(storage, "few_shot") == 1
        assert collector.gate_counts == {}

    def test_soft_hint_not_blocked(self, config, storage):
        collector, _ = _collector(storage)
        with patch(_ADAPTER, return_value=_adapter()):
            assert collector.on_response(_event("吃啥"), _resp("想买星光怀表。")) is not None

    def test_gate_off_collects_as_before(self, config, storage):
        config.set_hidden("learning_cutesy_gate", False)
        collector, _ = _collector(storage)
        with patch(_ADAPTER, return_value=_adapter()):
            pid = collector.on_response(_event("今晚吃啥？"), _resp("人家想吃火锅～～"))
        assert pid is not None
        assert _count(storage, "few_shot") == 1

    def test_gate_exception_falls_back_to_collect(self, config, storage, caplog):
        collector, _ = _collector(storage)
        with patch(_ADAPTER, return_value=_adapter()), patch(
            "astrbot_plugin_iris_memory.iris_memory.learning.collector.learning_gate",
            side_effect=ValueError("x"),
        ):
            pid = collector.on_response(_event("今晚吃啥？"), _resp("人家想吃火锅～～"))
        assert pid is not None
        assert "learning gate error" in caplog.text

    def test_dash_counted_not_blocked(self, config, storage):
        """审查 L3："——"只计数，不拦截"""
        collector, _ = _collector(storage)
        with patch(_ADAPTER, return_value=_adapter()):
            pid = collector.on_response(_event("周末去哪"), _resp("我想想——去公园走走吧。"))
        assert pid is not None
        assert collector.gate_counts.get("soft:BL-DASH") == 1
        assert "_total" not in collector.gate_counts

    def test_gate_counts_reset_daily(self, config, storage):
        collector, _ = _collector(storage)
        with patch(_ADAPTER, return_value=_adapter()):
            collector.on_response(_event("x"), _resp("人家不要"))
            collector._gate_day = "1999-01-01"
            collector.on_response(_event("x"), _resp("人家不要"))
        assert collector.gate_counts["_total"] == 1
        assert collector.gate_counts["BL-RENJIA"] == 1


# ---------------------------------------------------------------- 对话审查


class TestReviewerPrompt:
    def _prompt(self, storage):
        r = LearningReviewer(storage)
        pairs = [{"id": 1, "user_text": "问", "bot_text": "答"}]
        patterns = [{"id": 2, "scene": "chat", "expression": "表达"}]
        return r._build_prompt(pairs, patterns)

    def test_gate_off_prompt_identical_to_legacy(self, config, storage):
        config.set_hidden("learning_cutesy_gate", False)
        prompt = self._prompt(storage)
        expected = (
            _LEGACY_REVIEW_HEAD
            + "\n[pair id=1] 用户：问 / 机器人：答\n[pattern id=2] 场景=chat 表达：表达\n"
            + _LEGACY_REVIEW_TAIL
        )
        assert prompt == expected

    def test_gate_on_prompt_adds_criteria(self, config, storage):
        prompt = self._prompt(storage)
        assert prompt.startswith("请逐条审查以下聊天学习内容")
        assert "群聊学习内容" not in prompt
        assert "卖萌" in prompt and "口头禅" in prompt and "星海物语" in prompt
        assert "固定口头禅不算优点" in prompt
        assert "群友的梗、口头禅、玩笑和自然的口语可以通过" not in prompt
        assert "reject_type" in prompt and "cutesy|catchphrase|copied" in prompt

    def test_reject_type_counted_in_summary_log(self, config, storage, caplog):
        p1 = storage.insert_pair("g1", "u1", "问1", "答1")
        p2 = storage.insert_pair("g1", "u1", "问2", "答2")
        p3 = storage.insert_pair("g1", "u1", "问3", "答3")
        r = LearningReviewer(storage)
        pairs = [{"id": p1}, {"id": p2}, {"id": p3}]
        caplog.set_level(logging.INFO)
        r.apply_verdicts(
            [
                {"id": p1, "type": "pair", "pass": False, "reject_type": "cutesy"},
                {"id": p2, "type": "pair", "pass": False, "reject_type": "weird"},
                {"id": p3, "type": "pair", "pass": True, "reject_type": "none"},
            ],
            pairs,
            [],
        )
        assert "reject_type cutesy=1 other=1" in caplog.text

    def test_summary_log_unchanged_without_reject_type(self, config, storage, caplog):
        p1 = storage.insert_pair("g1", "u1", "问1", "答1")
        r = LearningReviewer(storage)
        caplog.set_level(logging.INFO)
        r.apply_verdicts([{"id": p1, "type": "pair", "pass": False}], [{"id": p1}], [])
        assert "reject_type" not in caplog.text


# ---------------------------------------------------------------- Persona 复审


class TestPersonaReviewer:
    def _prompt(self):
        return PersonaLearningReviewer().build_prompt(
            "你是小白", [{"id": 1, "user_text": "问", "bot_text": "答"}], []
        )

    def test_default_off_prompt_identical_to_legacy(self, config):
        lines = self._prompt().split("\n")
        assert lines[:5] == [
            "请依据下面的当前 Persona，逐条判断学习内容是否适合继续作为该机器人的回复参考。",
            "判断重点：身份与角色设定、说话语气、称谓、自称、价值边界、互动方式是否一致。",
            "只要存在明显冲突、模仿了其他人格、会把回复拉向不同角色，就判为不兼容。",
            "一般性的事实内容或不影响人格的自然表达可以判为兼容。",
            "",
        ]
        assert "动漫" not in self._prompt()

    def test_enabled_adds_two_narrow_lines(self, config):
        config.set_hidden("learning_persona_review_anime", True)
        lines = self._prompt().split("\n")
        assert "动漫式的温柔口吻本身与人设兼容" in lines[4]
        assert "具体作品角色" in lines[5]
        assert lines[6] == ""
