"""P1-4 主动消息接入 style 段（固定"中"档）+ 草稿命中 MAN-* 不发送。

复用 P0 关闭回归里的真实 ProactiveEngine 夹具（固定时钟 / 随机数 / 记录器）。
"""

from __future__ import annotations

import pytest

from astrbot_plugin_iris_memory.iris_memory.style import selector as S
from tests.lifelike_eval import test_p0_off_regression as reg
from tests.lifelike_eval.test_p0_off_regression import frozen  # noqa: F401  (fixture)


@pytest.fixture
def style_on(monkeypatch):
    for k, v in {
        "style_anime_register": "medium",
        "style_group_register_cap": "light",
        "style_cute_cap": "occasional",
        "style_inject_proactive": True,
    }.items():
        monkeypatch.setitem(reg.ALL_P0_OFF, k, v)
    S.get_selector().reset()
    yield
    S.get_selector().reset()


def _llm_calls(out):
    return [c for c in out["calls"] if c[0] == "llm.generate_direct"]


@pytest.mark.asyncio
async def test_private_speech_gets_medium_style(frozen, style_on):  # noqa: F811
    out = await reg._scenario_initiate(frozen, reg.PRIVATE, bridge=False)
    speech = _llm_calls(out)[1][1]
    assert "<iris:style>" not in speech["system_prompt"]
    tail = speech["system_prompt"].split("\n\n## 本轮口吻\n", 1)[1]
    assert tail.startswith("场景：你主动找对方开口。口吻：中。")
    assert "卖萌：很少" in tail
    # 决策调用不带 style 段
    assert "本轮口吻" not in (_llm_calls(out)[0][1].get("system_prompt") or "")
    assert any(c[0] == "context.send_message" for c in out["calls"])


@pytest.mark.asyncio
async def test_group_speech_capped_light(frozen, style_on):  # noqa: F811
    out = await reg._scenario_initiate(frozen, reg.GROUP, bridge=False)
    assert "场景：你主动在群里开口。口吻：淡。" in _llm_calls(out)[1][1]["system_prompt"]


@pytest.mark.asyncio
async def test_bridge_speech_gets_style(frozen, style_on):  # noqa: F811
    out = await reg._scenario_initiate(frozen, reg.PRIVATE, bridge=True)
    assert "口吻：中。" in _llm_calls(out)[1][1]["system_prompt"]
    assert any(c[0] == "bridge.send_part" for c in out["calls"])


@pytest.mark.asyncio
async def test_switch_off_identical_prompt(frozen, monkeypatch):  # noqa: F811
    base = await reg._scenario_initiate(frozen, reg.PRIVATE, bridge=False)
    frozen["now"] = reg.FIXED_NOW
    monkeypatch.setitem(reg.ALL_P0_OFF, "style_anime_register", "medium")
    monkeypatch.setitem(reg.ALL_P0_OFF, "style_inject_proactive", False)
    off = await reg._scenario_initiate(frozen, reg.PRIVATE, bridge=False)
    assert _llm_calls(off)[1][1] == _llm_calls(base)[1][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("bridge", [False, True])
async def test_man_hit_not_sent(frozen, style_on, monkeypatch, bridge):  # noqa: F811
    monkeypatch.setattr(reg, "SPEECH", "你都不理我，害我一直等")
    out = await reg._scenario_initiate(frozen, reg.PRIVATE, bridge=bridge)
    assert out["result"] == "已取消发起：style_man"
    names = [c[0] for c in out["calls"]]
    assert "context.send_message" not in names and "bridge.send_part" not in names
    if bridge:
        closes = [c for c in out["calls"] if c[0] == "bridge.close_turn"]
        assert closes and "style_man" in str(closes[-1][1])


@pytest.mark.asyncio
async def test_man_hit_sent_when_switch_off(frozen, monkeypatch):  # noqa: F811
    monkeypatch.setattr(reg, "SPEECH", "你都不理我，害我一直等")
    monkeypatch.setitem(reg.ALL_P0_OFF, "style_anime_register", "medium")
    monkeypatch.setitem(reg.ALL_P0_OFF, "style_inject_proactive", False)
    out = await reg._scenario_initiate(frozen, reg.PRIVATE, bridge=False)
    assert any(c[0] == "context.send_message" for c in out["calls"])


def test_helpers_isolate_failures(monkeypatch):
    from astrbot_plugin_iris_memory.iris_memory.proactive import proactive as P

    monkeypatch.setattr(S, "get_selector", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert P._proactive_style_text("private:1") == ""
    monkeypatch.setattr(S, "proactive_style_enabled", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert P._proactive_man_hits("你都不理我") == []
