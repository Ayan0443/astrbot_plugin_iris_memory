"""P0-5 NumericLeakGuard + 好感度残留隔离 + 情绪推测标注 单元测试"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import astrbot_plugin_iris_memory.iris_memory.config as config_mod
from astrbot_plugin_iris_memory.iris_memory.lifelike.guards import numeric as ng
from astrbot_plugin_iris_memory.iris_memory.lifelike.guards.numeric import (
    EMOTION_HEDGE_SUFFIX,
    emotion_hint,
    filter_lines,
    guard_enabled,
    guard_text,
    scan,
)


class FakeConfig:
    def __init__(self, **values) -> None:
        self.values = dict(values)

    def get(self, key, default=None):
        return self.values.get(key, default)


@pytest.fixture
def cfg(monkeypatch):
    """可变假配置；默认空（= 各开关取默认值）。"""
    fake = FakeConfig()
    monkeypatch.setattr(config_mod, "get_config", lambda: fake)
    return fake


# ---------------------------------------------------------------- 样例集

SHOULD_BLOCK = [
    "当前好感度：100",
    "好感度 85/100",
    "她对你的好感度已经90%了",
    "亲密度Lv3",
    "亲密度 72",
    "心情值 60",
    "精力值还剩 30%",
    "信任值=88",
    "好感+5",
    "+10好感",
    "favorability: 100",
    "affection level 7",
    "Affection: 80",
    "关系等级 Lv.4",
    "对你的好感大概80分",
    "她对你的喜欢有100分",
    "你们的亲密程度到了 95%",
    "信任感 9/10",
    "好感度百分之八十",
    "羁绊值满级",
    "trust score 80%",
    "好感度满分",
    "好感度上升了5点",
    "关系升到3级了",
]

SHOULD_PASS = [
    "3点20分见",
    "5分钟后到",
    "下午3点开会",
    "明天早上8点半出门",
    "考了90分",
    "电量还剩20%",
    "这件衣服99元",
    "CPU 占用 80%",
    "进度 3/10",
    "比分 2:1",
    "第3章写完了",
    "评分8.5分的电影",
    "我们关系很好",
    "心情不好",
    "她今天跑了5公里",
    "等我10分钟",
    "12:30 吃饭",
    "2026-11-03 交论文",
    "这部电影我打9分",
    "折扣是七折，省了30%",
    "没关系，95%的人都会这样",
    "他今年23岁",
    "带宽100MB",
    "30分钟后提醒我喝水",
    "一起看了3集",
    "体重掉了2斤",
    "我们认识3年了，关系一直很好",
    "她说 10:00 有个面试",
    "电池健康度 87%",
    "这周加班了3天",
    "你对我好感度多少",  # 非 strict：只有词，没有数值
    "满意度调查发了 5 份",
]


@pytest.mark.parametrize("text", SHOULD_BLOCK)
def test_blocks_numeric_relation(text):
    assert scan(text), text


@pytest.mark.parametrize("text", SHOULD_PASS)
def test_passes_normal_numbers(text):
    assert scan(text) == [], text


def test_sample_counts_meet_acceptance():
    assert len(SHOULD_BLOCK) >= 20
    assert len(SHOULD_PASS) >= 30


def test_empty_text():
    assert scan("") == []
    assert filter_lines("") == ("", [], 0)


def test_strict_word_only():
    assert scan("自定义: 好感度 很高") == []
    assert scan("自定义: 好感度 很高", strict=True) == ["metric_word_strict"]
    assert scan("favorability", strict=True) == ["metric_word_strict"]
    assert scan("兴趣: 摄影", strict=True) == []


def test_rule_ids():
    assert scan("好感+5") == ["metric_delta"]
    assert scan("好感度+5") == ["metric_number"]
    assert scan("考了90分。好感度100") == ["metric_number"]
    assert scan("对你的好感大概80分") == ["score_with_relation"]
    # 多句：两类规则都命中后提前结束
    assert scan("好感度 80。信任感 9/10。再来一句") == ["metric_number", "score_with_relation"]
    # 空白句被跳过
    assert scan("\n\n考了90分") == []


def test_time_then_metric_number_still_blocked():
    # 时间被剔除后，指标词旁边仍有数字 → 命中
    assert scan("3点20分的时候好感度是100") == ["metric_number"]
    # 只有时间 → 不命中
    assert scan("好感度这件事3点20分再聊") == []
    assert scan("好感度的话题晚8点再说") == []


# ---------------------------------------------------------------- filter_lines


def test_filter_lines_drops_only_hit_lines():
    text = "【发送者】ID: 1\n兴趣: 摄影\nfavorability: 100\n3点20分见"
    cleaned, rules, dropped = filter_lines(text, strict=True)
    assert cleaned == "【发送者】ID: 1\n兴趣: 摄影\n3点20分见"
    assert rules == ["metric_word_strict", "metric_number"]
    assert dropped == 1


def test_filter_lines_no_hit_returns_same_object():
    text = "考了90分\n下午3点见"
    cleaned, rules, dropped = filter_lines(text)
    assert cleaned is text and rules == [] and dropped == 0


def test_filter_lines_header_only_left_is_emptied():
    cleaned, rules, dropped = filter_lines("【画像摘要】\n好感度：100\n")
    assert cleaned == "" and dropped == 1 and rules


def test_filter_lines_dedups_rules():
    _, rules, dropped = filter_lines("好感度 1\n好感度 2\n普通")
    assert rules == ["metric_number"] and dropped == 2


# ---------------------------------------------------------------- 开关与回退


def test_guard_enabled_default_true_without_config(monkeypatch):
    def boom():
        raise RuntimeError("not init")

    monkeypatch.setattr(config_mod, "get_config", boom)
    assert guard_enabled() is True


@pytest.mark.parametrize(
    "value,expected",
    [
        (False, False), (True, True), (0, False), (1, True),
        ("false", False), ("OFF", False), ("0", False), ("no", False),
        ("true", True), ("on", True), ("yes", True), ("garbage", True), (None, True), ([], True),
    ],
)
def test_guard_enabled_parsing(cfg, value, expected):
    cfg.values["lifelike_numeric_guard"] = value
    assert guard_enabled() is expected


def test_guard_text_off_returns_identical(cfg):
    cfg.values["lifelike_numeric_guard"] = False
    text = "好感度：100\n兴趣: 摄影"
    assert guard_text(text, source="profile", strict=True) is text


def test_guard_text_empty(cfg):
    assert guard_text("", source="x") == ""


def test_guard_text_drops_counts_and_logs_without_body(cfg, monkeypatch):
    seen = []
    monkeypatch.setattr(ng.logger, "warning", lambda msg, *a, **k: seen.append(msg))
    before = ng.drop_count()
    out = guard_text("秘密内容：好感度 100\n兴趣: 摄影", source="profile")
    assert out == "兴趣: 摄影"
    assert ng.drop_count() == before + 1
    assert len(seen) == 1
    assert "[lifelike.numeric]" in seen[0] and "source=profile" in seen[0]
    assert "秘密内容" not in seen[0] and "100" not in seen[0]


def test_guard_text_exception_falls_back(cfg, monkeypatch):
    seen = []
    monkeypatch.setattr(ng.logger, "warning", lambda msg, *a, **k: seen.append(msg))

    def boom(*a, **k):
        raise ValueError("x")

    monkeypatch.setattr(ng, "_filter", boom)
    text = "好感度 100"
    assert guard_text(text, source="l1_context") is text
    assert seen and "guard failed" in seen[0]


# ---------------------------------------------------------------- 审查 M3 / L6：误报反例


# 技术 / 考试 / 日常话题：任何模式都不应命中
M3_FALSE_POSITIVES = [
    "关系型数据库里 80% 的查询走索引",
    "把进程 CPU affinity 绑到 4 核",
    "这个 bonding 网卡 2 级缓存",
    "Moodle 考试我拿了 90分",
    "因果关系里 70% 的样本",
    "零信任架构覆盖了 90% 的服务",
]


@pytest.mark.parametrize("text", M3_FALSE_POSITIVES)
def test_review_false_positives_no_longer_hit(text):
    assert scan(text) == [], text


# 弱规则仍会命中，但 lenient（原始对话段）只观测、不删行
LENIENT_KEEP = [
    "我们关系很好，周末打游戏赢了 3 级",
    "她 trust 这个库，评分 4.5/5",
]


@pytest.mark.parametrize("text", LENIENT_KEEP)
def test_lenient_keeps_weak_hits(text):
    assert scan(text) == ["score_with_relation"]
    cleaned, rules, dropped = filter_lines(text, lenient=True)
    assert cleaned is text and rules == [] and dropped == 0
    # 非 lenient（系统生成段）仍删
    assert filter_lines(text)[2] == 1


def test_lenient_still_drops_strong_rules():
    text = "今天聊了很多\n当前好感度：100\n好感+5\n明天见"
    cleaned, rules, dropped = filter_lines(text, lenient=True)
    assert cleaned == "今天聊了很多\n明天见" and dropped == 2
    assert set(rules) == {"metric_number", "metric_delta"}


def test_guard_text_lenient_logs_observed_at_debug_only(cfg, monkeypatch):
    warns, debugs = [], []
    monkeypatch.setattr(ng.logger, "warning", lambda msg, *a, **k: warns.append(msg))
    monkeypatch.setattr(ng.logger, "debug", lambda msg, *a, **k: debugs.append(msg))
    before = ng.drop_count()
    text = "主人: 我们关系很好，周末打游戏赢了 3 级"
    assert guard_text(text, source="l1_context", lenient=True) is text
    assert ng.drop_count() == before and warns == []
    assert debugs and "observed" in debugs[0] and "关系" not in debugs[0]


def test_english_terms_adjacent_to_chinese_still_hit():
    assert scan("她的favorability是100") == ["metric_number"]
    assert scan("affection level 7") == ["metric_number"]


@pytest.mark.parametrize("text", [
    "兴趣: galgame 刷好感度",
    "性格: affectionate, 爱笑",
    "最近在玩恋爱游戏，研究好感度系统",
])
def test_strict_ignores_word_in_interest_lines(text):
    """审查 L6：strict 只认字段名形态"""
    assert scan(text, strict=True) == [], text


@pytest.mark.parametrize("text", [
    "好感度: 很高",
    "自定义: 好感度 很高",
    "亲密度上升",
    "favorability",
    "Affection: high",
])
def test_strict_field_forms_still_hit(text):
    assert "metric_word_strict" in scan(text, strict=True), text


def test_request_hook_uses_lenient_for_raw_sections():
    import inspect

    from astrbot_plugin_iris_memory.iris_memory.core import llm_request_hook

    src = inspect.getsource(llm_request_hook)
    assert 'lenient=name in ("l1_context", "learning")' in src


# ---------------------------------------------------------------- 情绪推测标注


def test_emotion_raw_when_guard_off(cfg):
    cfg.values["lifelike_numeric_guard"] = False
    assert emotion_hint("焦虑") == ("", "焦虑")


def test_emotion_auto_default_drops(cfg):
    assert emotion_hint("焦虑") is None


def test_emotion_auto_hedged_when_memory_on(cfg):
    cfg.values["lifelike_memory_mode"] = "on"
    assert emotion_hint("焦虑") == (EMOTION_HEDGE_SUFFIX, "可能焦虑")


def test_emotion_hedged_does_not_double(cfg):
    cfg.values["lifelike_emotion_hint"] = "hedged"
    assert emotion_hint(" 似乎有点累 ") == (EMOTION_HEDGE_SUFFIX, "似乎有点累")
    assert "可能" in EMOTION_HEDGE_SUFFIX or "推测" in EMOTION_HEDGE_SUFFIX


def test_emotion_hedged_numeric_value_dropped(cfg):
    cfg.values["lifelike_emotion_hint"] = "hedged"
    assert emotion_hint("心情值 60") is None


def test_emotion_explicit_modes(cfg):
    cfg.values["lifelike_emotion_hint"] = "off"
    assert emotion_hint("焦虑") is None
    cfg.values["lifelike_emotion_hint"] = "raw"
    assert emotion_hint("焦虑") == ("", "焦虑")
    cfg.values["lifelike_emotion_hint"] = "weird"  # 未知 → auto
    assert emotion_hint("焦虑") is None
    cfg.values["lifelike_emotion_hint"] = None
    assert emotion_hint("焦虑") is None


def test_emotion_empty(cfg):
    assert emotion_hint("") is None
    assert emotion_hint(None) is None


def test_emotion_exception_falls_back_raw(cfg, monkeypatch):
    def boom():
        raise ValueError("x")

    monkeypatch.setattr(ng, "_emotion_mode", boom)
    assert emotion_hint("焦虑") == ("", "焦虑")


# ---------------------------------------------------------------- 接入：画像注入


def _profiles(**over):
    user = dict(
        user_id="42", user_name="小雨", historical_names=[], personality_tags=["安静"],
        interests=["摄影"], occupation="", language_style="", communication_style="",
        emotional_baseline="焦虑", favorability=100.0, bot_relationship="",
        important_dates=[], important_events=[], taboo_topics=[],
        custom_fields={"favorability": 100, "宠物": "猫"},
    )
    user.update(over)
    group = SimpleNamespace(interests=[], atmosphere_tags=[], long_term_tags=[],
                            blacklist_topics=[], custom_fields={})
    return group, SimpleNamespace(**user)


def test_profile_format_guard_on_drops_emotion(cfg):
    from astrbot_plugin_iris_memory.iris_memory.core.llm_request_hook import (
        _format_profiles_for_injection,
    )

    group, user = _profiles()
    text = _format_profiles_for_injection(group, user)
    assert "情感" not in text and "焦虑" not in text
    assert "兴趣: 摄影" in text


def test_profile_format_guard_on_hedged(cfg):
    from astrbot_plugin_iris_memory.iris_memory.core.llm_request_hook import (
        _format_profiles_for_injection,
    )

    cfg.values["lifelike_emotion_hint"] = "hedged"
    group, user = _profiles()
    text = _format_profiles_for_injection(group, user)
    assert f"情感{EMOTION_HEDGE_SUFFIX}: 可能焦虑" in text


def test_profile_format_guard_off_legacy(cfg):
    from astrbot_plugin_iris_memory.iris_memory.core.llm_request_hook import (
        _format_profiles_for_injection,
    )

    cfg.values["lifelike_numeric_guard"] = False
    group, user = _profiles()
    text = _format_profiles_for_injection(group, user)
    assert "情感: 焦虑" in text
    assert "favorability: 100" in text  # 旧行为：custom_fields 原样


def _req():
    return SimpleNamespace(extra_user_content_parts=[])


def test_inject_parts_drops_profile_residue(cfg):
    from astrbot_plugin_iris_memory.iris_memory.core.llm_request_hook import (
        _inject_to_extra_user_content_parts,
    )

    req = _req()
    combined = _inject_to_extra_user_content_parts(
        req,
        l1_text="[小雨] 3点20分见\n[小雨] 你对我好感度+5了吗",
        profile_text="【发送者】ID: 42\n兴趣: 摄影, favorability: 100",
        l2_text="她考了90分",
        l3_text="",
        learning_text="",
    )
    assert "3点20分见" in combined and "她考了90分" in combined and "兴趣" not in combined
    assert "favorability" not in combined and "好感度+5" not in combined
    assert "<iris:profile>\n【发送者】ID: 42\n</iris:profile>" in combined
    assert req.extra_user_content_parts[0].text == combined


def test_inject_parts_section_fully_dropped(cfg):
    from astrbot_plugin_iris_memory.iris_memory.core.llm_request_hook import (
        _inject_to_extra_user_content_parts,
    )

    req = _req()
    assert _inject_to_extra_user_content_parts(req, "", "好感度：100", "", "", "") == ""
    assert req.extra_user_content_parts == []


def test_inject_parts_guard_off_identical(cfg):
    from astrbot_plugin_iris_memory.iris_memory.core.llm_request_hook import (
        _inject_to_extra_user_content_parts,
    )

    cfg.values["lifelike_numeric_guard"] = False
    req = _req()
    combined = _inject_to_extra_user_content_parts(
        req, "a", "favorability: 100", "b", "c", "d"
    )
    assert combined == (
        "<iris:l1_context>\na\n</iris:l1_context>\n\n"
        "<iris:profile>\nfavorability: 100\n</iris:profile>\n\n"
        "<iris:l2_memory>\nb\n</iris:l2_memory>\n\n"
        "<iris:l3_kg>\nc\n</iris:l3_kg>\n\n"
        "<iris:learning>\nd\n</iris:learning>"
    )


# ---------------------------------------------------------------- 接入：get_profile 工具


def test_get_profile_tool_guard(cfg):
    from astrbot_plugin_iris_memory.iris_memory.tools.get_profile import GetProfileTool

    _, user = _profiles(interests=["好感度 100 的游戏"])
    on = GetProfileTool()._format_user_profile(user)
    assert "情感基线" not in on and "好感度" not in on and "小雨" in on

    cfg.values["lifelike_numeric_guard"] = False
    off = GetProfileTool()._format_user_profile(user)
    assert "**情感基线**: 焦虑" in off and "好感度 100" in off


# ---------------------------------------------------------------- 接入：连续性上下文


@pytest.mark.asyncio
async def test_continuity_profile_section_guarded(cfg):
    from astrbot_plugin_iris_memory.iris_memory.continuity.context import ContextAssembler

    async def profile_text(_sk):
        return "兴趣：摄影\n好感度：100"

    a = ContextAssembler(None, lambda: {"42"}, config={}, profile_text=profile_text)
    out = await a.assemble("private:42", purpose="proactive")
    assert "兴趣：摄影" in out and "好感度" not in out

    cfg.values["lifelike_numeric_guard"] = False
    out = await a.assemble("private:42", purpose="proactive")
    assert "好感度：100" in out


@pytest.mark.asyncio
async def test_continuity_private_only_guarded(cfg):
    from astrbot_plugin_iris_memory.iris_memory.continuity.service import ContinuityService

    async def private_section(_sk):
        return "【相处方式】\n好感度 100"

    async def profile_section(_sk):
        return "【画像摘要】\n兴趣：摄影"

    svc = object.__new__(ContinuityService)
    svc.assembler = SimpleNamespace(
        is_owner_private=lambda *a: True,
        _private_section=private_section,
        _profile_section=profile_section,
    )
    out = await svc._private_only("private:42", "proactive", None)
    assert "好感度" not in out and "兴趣：摄影" in out and "相处方式" not in out


# ---------------------------------------------------------------- 接入：主动发起


def _engine(topic_profile="兴趣：摄影\n好感度：100"):
    from astrbot_plugin_iris_memory.iris_memory.proactive.proactive import ProactiveEngine

    eng = object.__new__(ProactiveEngine)
    persona_manager = SimpleNamespace(get_default_persona_v3=AsyncMock(return_value={"prompt": "P"}))
    eng._context = SimpleNamespace(persona_manager=persona_manager, get_config=lambda umo=None: {})
    eng._window = SimpleNamespace(get_messages=lambda gid: [])
    eng._packager = SimpleNamespace(package=lambda gid, msgs, mode: "[群] 3点20分见\n[群] 好感度+5")
    eng._bridge = None
    eng._avoid_cache = {}
    eng._profile_get = AsyncMock(return_value=topic_profile)
    eng._llm_manager = SimpleNamespace(generate_direct=AsyncMock(return_value=" 嗨 "))
    return eng


@pytest.mark.asyncio
async def test_generate_speech_guarded(cfg):
    from astrbot_plugin_iris_memory.iris_memory.proactive.parser import Decision

    eng = _engine()
    out = await eng._generate_speech("-100", "umo", "p", Decision(topic="问问她对你的好感度 90 分的事"))
    assert out == "嗨"
    kw = eng._llm_manager.generate_direct.await_args.kwargs
    assert "好感" not in kw["prompt"] and "3点20分见" in kw["prompt"]
    assert "自由选择一个轻松" in kw["prompt"]
    assert "兴趣：摄影" in kw["system_prompt"] and "好感度" not in kw["system_prompt"]


@pytest.mark.asyncio
async def test_generate_speech_guard_off_legacy(cfg):
    from astrbot_plugin_iris_memory.iris_memory.proactive.parser import Decision

    cfg.values["lifelike_numeric_guard"] = False
    eng = _engine()
    await eng._generate_speech("-100", "umo", "p", Decision(topic="好感度 90 分"))
    kw = eng._llm_manager.generate_direct.await_args.kwargs
    assert "好感度+5" in kw["prompt"] and "好感度 90 分" in kw["prompt"]
    assert "好感度：100" in kw["system_prompt"]


@pytest.mark.asyncio
async def test_generate_speech_why_now_guarded(cfg):
    """隐私审查 #6：why_now 也过数值守卫"""
    from astrbot_plugin_iris_memory.iris_memory.proactive.parser import Decision

    eng = _engine()
    await eng._generate_speech("-100", "umo", "p", Decision(topic="", why_now="她对你好感度很高（90）"))
    kw = eng._llm_manager.generate_direct.await_args.kwargs
    assert "90" not in kw["prompt"] and "当前群聊出现了自然的交流空档" in kw["prompt"]
    cfg.values["lifelike_numeric_guard"] = False
    await eng._generate_speech("-100", "umo", "p", Decision(topic="", why_now="她对你好感度很高（90）"))
    assert "好感度很高（90）" in eng._llm_manager.generate_direct.await_args.kwargs["prompt"]


# ---------------------------------------------------------------- 好感度残留隔离：画像分析 prompt


def test_slim_profile_strips_favorability(cfg):
    from astrbot_plugin_iris_memory.iris_memory.profile.analyzer import _slim_profile_dict

    prof = {"favorability": 100.0, "interests": ["摄影"], "version": 3}
    assert _slim_profile_dict(prof) == {"interests": ["摄影"]}
    cfg.values["lifelike_numeric_guard"] = False
    assert _slim_profile_dict(prof) == {"favorability": 100.0, "interests": ["摄影"]}


def test_numeric_guard_on_helper_fallback(monkeypatch):
    from astrbot_plugin_iris_memory.iris_memory.profile import analyzer as an

    def boom():
        raise ValueError("x")

    monkeypatch.setattr(ng, "guard_enabled", boom)
    assert an._numeric_guard_on() is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "guard,fav,expect_key",
    [(True, False, False), (False, False, True), (True, True, True)],
)
async def test_batch_prompt_favorability_field(cfg, monkeypatch, guard, fav, expect_key):
    from astrbot_plugin_iris_memory.iris_memory.profile import analyzer as an

    cfg.values["lifelike_numeric_guard"] = guard
    cfg.values["profile.favorability_enable"] = fav
    monkeypatch.setattr(an, "get_config", lambda: cfg)
    llm = SimpleNamespace(generate_direct=AsyncMock(return_value='{"users": {}}'))
    analyzer = an.ProfileAnalyzer(llm)
    await analyzer.analyze_profiles_batch(
        group=None,
        users=[{"id": "42", "tier": "mid", "messages": ["hi"], "profile": {"favorability": 100.0}}],
    )
    prompt = llm.generate_direct.await_args.kwargs["prompt"]
    assert ('"favorability_delta": 0' in prompt) is expect_key
    if expect_key and not guard:
        assert '"emotional_baseline": "", "favorability_delta": 0,\n' in prompt
    assert ('"favorability": 100.0' in prompt) is (not guard)
