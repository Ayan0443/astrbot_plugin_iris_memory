"""P1-2 接入：llm_request_hook 的 gather 并列 _collect_style；<iris:style> 在 learning 之后。

开关关闭时：不建任务、不写 meta、注入文本与改造前逐字节一致。
"""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from astrbot_plugin_iris_memory.iris_memory.core import llm_request_hook as hook
from astrbot_plugin_iris_memory.iris_memory.style import selector as S

_P = "astrbot_plugin_iris_memory.iris_memory.core.llm_request_hook"


class _Part:
    def __init__(self, text):
        self.text = text

    def mark_as_temp(self):
        pass


def _req():
    return SimpleNamespace(extra_user_content_parts=[], system_prompt="", conversation=None)


@pytest.fixture(autouse=True)
def _textpart(monkeypatch):
    import astrbot.core.agent.message as msg_mod

    monkeypatch.setattr(msg_mod, "TextPart", _Part)
    S.get_selector().reset()
    yield
    S.get_selector().reset()


def test_inject_order_style_after_learning():
    req = _req()
    combined = hook._inject_to_extra_user_content_parts(req, "L1", "", "", "", "LEARN", "STYLE")
    assert combined.index("<iris:learning>") < combined.index("<iris:style>\nSTYLE\n</iris:style>")


def test_inject_without_style_identical():
    a, b = _req(), _req()
    old = hook._inject_to_extra_user_content_parts(a, "L1", "P", "", "", "LEARN")
    new = hook._inject_to_extra_user_content_parts(b, "L1", "P", "", "", "LEARN", "")
    assert old == new and "<iris:style>" not in new
    assert hook._inject_to_extra_user_content_parts(_req(), "", "", "", "", "", "") == ""


def _config(values):
    cfg = MagicMock()
    cfg.get.side_effect = lambda key, default=None: values.get(key, default)
    return cfg


async def _run(values, text="今天外面风好大", group_id=""):
    event = MagicMock()
    event.message_str = text
    req = _req()
    adapter = MagicMock()
    adapter.get_group_id.return_value = group_id
    adapter.get_session_id.return_value = "sess-1"
    captured = {}

    def record(*args, **kwargs):
        captured.update(kwargs)

    with ExitStack() as st:
        st.enter_context(patch(f"{_P}._collect_l1_context", new_callable=AsyncMock, return_value="L1"))
        st.enter_context(patch(f"{_P}._collect_user_profile", new_callable=AsyncMock, return_value=""))
        st.enter_context(patch(f"{_P}._collect_l2_memory", new_callable=AsyncMock, return_value=("", [])))
        st.enter_context(patch(f"{_P}._collect_l3_knowledge_graph", new_callable=AsyncMock, return_value=""))
        st.enter_context(patch(f"{_P}._collect_learning", new_callable=AsyncMock, return_value=""))
        st.enter_context(patch(f"{_P}._schedule_related_image_parse"))
        st.enter_context(patch(f"{_P}._log_final_context"))
        st.enter_context(patch(f"{_P}._record_injection_log", side_effect=record))
        st.enter_context(patch("astrbot_plugin_iris_memory.iris_memory.config.get_config", return_value=_config(values)))
        st.enter_context(patch("astrbot_plugin_iris_memory.iris_memory.platform.get_adapter", return_value=adapter))
        await hook.preprocess_llm_request(event, req, MagicMock())
    text = "\n".join(p.text for p in req.extra_user_content_parts)
    return text, captured


@pytest.mark.asyncio
async def test_disabled_no_style_and_no_meta():
    text, cap = await _run({})
    assert "<iris:style>" not in text and "<iris:l1_context>" in text
    assert "style" not in cap["meta"] and cap["style_text"] == ""
    text2, cap2 = await _run({"style_anime_register": "off"})
    assert text2 == text and "style" not in cap2["meta"]


@pytest.mark.asyncio
async def test_enabled_private_injects():
    text, cap = await _run({"style_anime_register": "medium", "style_cute_cap": "occasional"})
    assert "<iris:style>\n## 本轮口吻\n场景：私聊闲聊。口吻：中。" in text
    assert text.index("<iris:l1_context>") < text.index("<iris:style>")
    meta = cap["meta"]["style"]
    assert meta["scene"] == "chat" and meta["register"] == "medium" and "duration_ms" in meta


@pytest.mark.asyncio
async def test_enabled_group_cap_off_skips():
    text, cap = await _run(
        {"style_anime_register": "medium", "style_group_register_cap": "off"}, group_id="g1"
    )
    assert "<iris:style>" not in text
    assert cap["meta"]["style"]["skipped"] == "register_off"


@pytest.mark.asyncio
async def test_collect_style_failure_isolated():
    with patch.object(S.StyleSelector, "build_passive", side_effect=RuntimeError("x")):
        text, cap = await _run({"style_anime_register": "medium"})
    assert "<iris:style>" not in text and "<iris:l1_context>" in text
    assert "error" in cap["meta"]["style"]


@pytest.mark.asyncio
async def test_collect_style_no_scope():
    meta = {}
    adapter = MagicMock()
    adapter.get_group_id.return_value = ""
    adapter.get_session_id.return_value = ""
    with patch("astrbot_plugin_iris_memory.iris_memory.platform.get_adapter", return_value=adapter):
        assert await hook._collect_style(MagicMock(), "hi", meta=meta) == ""
    assert meta["skipped"] == "no_scope"


def test_style_enabled_switch_read_failure():
    with patch.object(S, "style_enabled", side_effect=RuntimeError("x")):
        assert hook._style_enabled() is False


def test_record_injection_log_style_section(monkeypatch):
    recorded = {}

    class _RL:
        def record(self, *a, **kw):
            recorded.update(kw)

    import astrbot_plugin_iris_memory.iris_memory.core.run_log as run_log

    monkeypatch.setattr(run_log, "get_run_log_manager", lambda: _RL())
    base = dict(l1_text="a", profile_text="", l2_text="", l3_text="", learning_text="",
                combined="x", user_message="u", total_duration_ms=1.0)
    hook._record_injection_log(MagicMock(), _req(), meta={}, **base)
    assert "style" not in recorded["sections"] and "style" not in recorded["stage_timings_ms"]
    hook._record_injection_log(MagicMock(), _req(), meta={"style": {"duration_ms": 0.5, "scene": "chat"}},
                               style_text="STYLE", **base)
    assert recorded["sections"]["style"]["injected"] is True
    assert recorded["stage_timings_ms"]["style"] == 0.5


@pytest.mark.asyncio
async def test_learning_inject_paused():
    cfg = _config({"learning.enable": True, "learning_inject_paused": True})
    meta = {}
    cm = MagicMock()
    with patch("astrbot_plugin_iris_memory.iris_memory.config.get_config", return_value=cfg):
        out = await hook._collect_learning(MagicMock(), _req(), cm, meta=meta)
    assert out == "" and meta["skipped"] == "paused"
    cm.check_component.assert_not_called()
