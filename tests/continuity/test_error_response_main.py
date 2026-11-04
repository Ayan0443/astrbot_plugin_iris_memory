"""审查 M4：agent 报错 / 友好报错文案不进 L1 与学习采集（main.on_llm_response 接线）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from astrbot_plugin_iris_memory.iris_memory.extras.error_friendly import (
    ErrorFriendlyMessages,
    ErrorFriendlyProcessor,
)

from .test_integration_main import FakeEvent, plugin  # noqa: F401


def _resp(text, role="assistant"):
    return SimpleNamespace(completion_text=text, usage=None, role=role)


@pytest.mark.asyncio
async def test_err_response_not_pending_for_l1(plugin):  # noqa: F811
    plugin._error_processor = ErrorFriendlyProcessor(plugin.config)
    ev = FakeEvent(text="在吗", msg_id=700)
    await plugin.on_llm_response(ev, _resp(ErrorFriendlyMessages.AGENT_FRIENDLY_MSGS[0], role="err"))
    assert ev.extras.get("iris_pending_assistant") is None
    assert ev.extras.get("iris_pending_learning") is None
    plugin.l1.add_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_normal_response_still_pending(plugin):  # noqa: F811
    plugin._error_processor = ErrorFriendlyProcessor(plugin.config)
    ev = FakeEvent(text="在吗", msg_id=701)
    await plugin.on_llm_response(ev, _resp("在的"))
    assert ev.extras["iris_pending_assistant"]["content"] == "在的"


@pytest.mark.asyncio
async def test_switch_off_keeps_legacy(plugin):  # noqa: F811
    plugin.config.values["lifelike_error_friendly_agent"] = False
    plugin._error_processor = ErrorFriendlyProcessor(plugin.config)
    ev = FakeEvent(text="在吗", msg_id=702)
    await plugin.on_llm_response(ev, _resp("Error occurred during AI execution.", role="err"))
    assert ev.extras["iris_pending_assistant"]["content"].startswith("Error occurred")
