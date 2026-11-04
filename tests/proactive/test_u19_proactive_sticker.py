"""U19：主动消息生成时注入表情包说明、剥离标记（回调为 None 时与原行为一致）。"""

from unittest.mock import AsyncMock

import pytest

from tests.lifelike.test_numeric_guard import _engine, cfg  # noqa: F401


@pytest.mark.asyncio
async def test_generate_speech_without_hooks_unchanged(cfg):  # noqa: F811
    from astrbot_plugin_iris_memory.iris_memory.proactive.parser import Decision

    eng = _engine()
    eng._llm_manager.generate_direct = AsyncMock(return_value="早呀[表情:开心]")
    out = await eng._generate_speech("private:1", "umo", "p", Decision(topic="t"))
    assert out == "早呀[表情:开心]"
    assert "可用表情包" not in eng._llm_manager.generate_direct.await_args.kwargs["system_prompt"]


@pytest.mark.asyncio
async def test_generate_speech_with_hooks(cfg):  # noqa: F811
    from astrbot_plugin_iris_memory.iris_memory.proactive.parser import Decision

    eng = _engine()
    eng._llm_manager.generate_direct = AsyncMock(return_value="早呀[表情:开心]")
    calls = []

    async def block(gid, umo):
        return "【可用表情包】\n- 开心"

    async def take(gid, umo, text):
        calls.append(text)
        return text.replace("[表情:开心]", "")

    eng._sticker_block = block
    eng._sticker_take = take
    out = await eng._generate_speech("private:1", "umo", "p", Decision(topic="t"))
    assert out == "早呀"
    assert "【可用表情包】" in eng._llm_manager.generate_direct.await_args.kwargs["system_prompt"]
    assert calls == ["", "早呀[表情:开心]"]  # 先清暂存，再剥离
