"""lifelike P0-2：agent 异常路径友好报错（DECISIONS D2）。"""

from __future__ import annotations

import re

import pytest
from astrbot.api.event import MessageChain
from astrbot_plugin_iris_memory.iris_memory.extras.error_friendly import (
    AGENT_SWITCH_KEY,
    PERSONA_ERROR_EXTRA_KEY,
    ErrorFriendlyMessages,
    ErrorFriendlyProcessor,
)

AGENT_ERR = "Error occurred while processing agent request: Connection error."
AI_EXEC_ERR = (
    "Error occurred during AI execution.\nError Type: APITimeoutError\nError Message: Request timed out."
)
LLM_RESP_ERR = "LLM 响应错误: upstream returned 502"


class FakeConfig:
    def __init__(self, **values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


class FakeEvent:
    def __init__(self):
        self.extras: dict = {}
        self.sent: list[str] = []

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    async def send(self, message):
        self.sent.append(message.get_plain_text())


def _proc(**cfg) -> ErrorFriendlyProcessor:
    return ErrorFriendlyProcessor(FakeConfig(**cfg), choose=lambda opts: opts[0])


def _assert_friendly(text: str) -> None:
    assert text in ErrorFriendlyMessages.AGENT_FRIENDLY_MSGS
    assert "Error" not in text and "Exception" not in text and "Traceback" not in text
    assert re.search(r"[一-鿿]", text)
    assert not re.search(r"[A-Za-z]{3,}", text)


@pytest.mark.parametrize(
    "text,kind",
    [
        (AGENT_ERR, "agent_request"),
        (AI_EXEC_ERR, "APITimeoutError"),
        (LLM_RESP_ERR, "llm_response"),
        ("LLM 响应错误：未知错误", "llm_response"),
        ("  " + AGENT_ERR, "agent_request"),
        ("今天聊到 Error occurred while processing agent request 这句英文", None),
        ("你好呀", None),
        ("", None),
    ],
)
def test_agent_error_kind(text, kind):
    assert ErrorFriendlyProcessor.agent_error_kind(text) == kind


def test_all_friendly_messages_are_chinese_without_error_words():
    for msg in ErrorFriendlyMessages.AGENT_FRIENDLY_MSGS:
        _assert_friendly(msg)


@pytest.mark.parametrize("raw", [AGENT_ERR, AI_EXEC_ERR, LLM_RESP_ERR])
def test_process_result_replaces_agent_error(raw):
    result = MessageChain().message(raw)
    _proc().process_result(result)
    _assert_friendly(result.get_plain_text())


def test_process_result_switch_off_keeps_legacy_behavior():
    # lifelike 开关关闭：与旧版逐字一致（AGENT_ERR 不命中旧规则 → 原样发出）
    result = MessageChain().message(AGENT_ERR)
    _proc(**{AGENT_SWITCH_KEY: False}).process_result(result)
    assert result.get_plain_text() == AGENT_ERR

    legacy = "AstrBot 请求失败。\n错误类型: X\n错误信息: y"
    r1 = MessageChain().message(legacy)
    _proc(**{AGENT_SWITCH_KEY: False}).process_result(r1)
    r2 = MessageChain().message(legacy)
    _proc().process_result(r2)
    assert r1.get_plain_text() == r2.get_plain_text() == ErrorFriendlyMessages.DEFAULT_FRIENDLY_MSG


def test_process_result_master_switch_off_disables_agent_path():
    result = MessageChain().message(AGENT_ERR)
    p = _proc(**{"extras.error_friendly.enable": False})
    assert p.should_process(None) is False
    assert p.apply_agent_error_extra(FakeEvent()) is False
    assert p.install_send_guard(FakeEvent()) is False
    assert result.get_plain_text() == AGENT_ERR


def test_normal_reply_untouched():
    result = MessageChain().message("刚刚在想晚饭吃什么")
    _proc().process_result(result)
    assert result.get_plain_text() == "刚刚在想晚饭吃什么"


def test_apply_extra_sets_when_persona_has_none():
    ev = FakeEvent()
    ev.set_extra(PERSONA_ERROR_EXTRA_KEY, None)  # core 解析人格后写入的 None
    assert _proc().apply_agent_error_extra(ev) is True
    _assert_friendly(ev.get_extra(PERSONA_ERROR_EXTRA_KEY))


def test_apply_extra_respects_persona_custom_message():
    ev = FakeEvent()
    ev.set_extra(PERSONA_ERROR_EXTRA_KEY, "人格自己的报错")
    assert _proc().apply_agent_error_extra(ev) is False
    assert ev.get_extra(PERSONA_ERROR_EXTRA_KEY) == "人格自己的报错"


def test_apply_extra_switch_off_noop():
    ev = FakeEvent()
    assert _proc(**{AGENT_SWITCH_KEY: False}).apply_agent_error_extra(ev) is False
    assert PERSONA_ERROR_EXTRA_KEY not in ev.extras


def test_apply_extra_swallows_event_errors():
    class Broken:
        def get_extra(self, *a):
            raise RuntimeError("boom")

        def set_extra(self, *a):
            raise RuntimeError("boom")

    assert _proc().apply_agent_error_extra(Broken()) is False
    assert _proc().apply_agent_error_extra(None) is False


@pytest.mark.asyncio
async def test_send_guard_replaces_direct_agent_error():
    ev = FakeEvent()
    p = _proc()
    assert p.install_send_guard(ev) is True
    assert p.install_send_guard(ev) is False  # 幂等
    await ev.send(MessageChain().message(AGENT_ERR))
    await ev.send(MessageChain().message("正常回复"))
    _assert_friendly(ev.sent[0])
    assert ev.sent[1] == "正常回复"


@pytest.mark.asyncio
async def test_send_guard_hot_switch_off_passthrough():
    cfg = FakeConfig()
    ev = FakeEvent()
    p = ErrorFriendlyProcessor(cfg, choose=lambda o: o[0])
    p.install_send_guard(ev)
    cfg.values[AGENT_SWITCH_KEY] = False
    await ev.send(MessageChain().message(AGENT_ERR))
    assert ev.sent == [AGENT_ERR]


@pytest.mark.asyncio
async def test_send_guard_not_installed_when_off():
    ev = FakeEvent()
    original = ev.send
    assert _proc(**{AGENT_SWITCH_KEY: False}).install_send_guard(ev) is False
    assert ev.send == original


@pytest.mark.asyncio
async def test_core_flow_simulation_extra_then_core_send():
    """模拟 core：解析人格写 None → Iris on_llm_request 写 extra → agent 异常 → core 直发。"""
    ev = FakeEvent()
    p = _proc()
    p.install_send_guard(ev)  # on_self_outgoing（早于 agent 构建）
    ev.set_extra(PERSONA_ERROR_EXTRA_KEY, None)  # core _ensure_persona_and_skills
    p.apply_agent_error_extra(ev)  # Iris on_llm_request
    try:
        raise RuntimeError("Connection error.")
    except Exception as e:  # core internal.py 的 except 分支
        custom = ev.get_extra(PERSONA_ERROR_EXTRA_KEY)
        await ev.send(MessageChain().message(custom or f"Error occurred while processing agent request: {e}"))
    _assert_friendly(ev.sent[0])


def test_choose_failure_falls_back_to_first_message():
    def bad(_):
        raise RuntimeError

    p = ErrorFriendlyProcessor(FakeConfig(), choose=bad)
    assert p.agent_friendly_message() == ErrorFriendlyMessages.AGENT_FRIENDLY_MSGS[0]


def test_default_choose_is_random_from_pool():
    p = ErrorFriendlyProcessor(FakeConfig())
    assert p.agent_friendly_message() in ErrorFriendlyMessages.AGENT_FRIENDLY_MSGS


def test_hidden_config_default_true():
    from astrbot_plugin_iris_memory.iris_memory.config.defaults import Defaults

    d = Defaults()
    assert d.get_by_flat_key(AGENT_SWITCH_KEY) is True
    assert d.get_by_flat_key("lifelike_adapter_watchdog") is True
    assert d.get_by_flat_key("lifelike_adapter_down_threshold_s") == 300


# ---------------------------------------------------------------- 审查 M4：报错不进 L1 / 学习


class _Resp:
    def __init__(self, text, role="assistant"):
        self.completion_text = text
        self.role = role


@pytest.mark.parametrize(
    "resp",
    [
        _Resp("我这边卡了一下，刚才那句没接上。", role="err"),
        _Resp("任意人格自定义报错", role="err"),
        _Resp(ErrorFriendlyMessages.AGENT_FRIENDLY_MSGS[1]),
        _Resp(AI_EXEC_ERR),
        _Resp(AGENT_ERR),
    ],
)
def test_is_error_response_true(resp):
    assert _proc().is_error_response(resp) is True


@pytest.mark.parametrize("resp", [_Resp("今天也辛苦啦"), _Resp(""), None])
def test_is_error_response_false_for_normal(resp):
    assert _proc().is_error_response(resp) is False


def test_is_error_response_switch_off_is_legacy():
    resp = _Resp(ErrorFriendlyMessages.AGENT_FRIENDLY_MSGS[0], role="err")
    assert _proc(**{AGENT_SWITCH_KEY: False}).is_error_response(resp) is False
    assert _proc(**{"extras.error_friendly.enable": False}).is_error_response(resp) is False


def test_is_error_response_swallows_errors():
    class Bad:
        @property
        def role(self):
            raise RuntimeError("boom")

    assert _proc().is_error_response(Bad()) is False


def test_friendly_messages_make_no_followup_promise():
    for msg in ErrorFriendlyMessages.AGENT_FRIENDLY_MSGS:
        for word in ("等下", "稍等", "过一会", "再跟你说", "再回你", "待会"):
            assert word not in msg, msg
