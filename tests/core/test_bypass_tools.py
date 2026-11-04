"""活人感 P0-1：旁路出站工具剥离（send_message_to_user / future_task）。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot.core.agent.tool import FunctionTool, ToolSet

from astrbot_plugin_iris_memory.iris_memory.core import bypass_tools as bt
from astrbot_plugin_iris_memory.iris_memory.core.bypass_tools import (
    BYPASS_TOOL_NAMES,
    BypassToolStripper,
    find_active_proactive_chat,
    strip_bypass_tools,
)


def _tool(name: str) -> FunctionTool:
    return FunctionTool(
        name=name,
        parameters={"type": "object", "properties": {}},
        description=name,
        handler=None,
    )


def _req(*names: str):
    return SimpleNamespace(func_tool=ToolSet([_tool(n) for n in names]))


class TestStripBypassTools:
    def test_removes_both_builtin_tools_and_keeps_others_in_order(self):
        req = _req("save_memory", "send_message_to_user", "web_search", "future_task", "z_tool")
        others = [t for t in req.func_tool.tools if t.name not in BYPASS_TOOL_NAMES]
        tool_set = req.func_tool

        removed = strip_bypass_tools(req)

        assert removed == ["send_message_to_user", "future_task"]
        assert req.func_tool is tool_set  # 原地修改同一个 ToolSet
        assert req.func_tool.names() == ["save_memory", "web_search", "z_tool"]
        assert req.func_tool.tools == others  # 其他工具对象不变

    def test_no_func_tool_is_noop(self):
        req = SimpleNamespace(func_tool=None)
        assert strip_bypass_tools(req) == []
        assert req.func_tool is None
        assert strip_bypass_tools(SimpleNamespace()) == []

    def test_empty_or_unrelated_toolset_untouched(self):
        empty = SimpleNamespace(func_tool=ToolSet())
        assert strip_bypass_tools(empty) == []
        req = _req("save_memory")
        before = list(req.func_tool.tools)
        assert strip_bypass_tools(req) == []
        assert req.func_tool.tools == before

    def test_only_bypass_tools_leaves_empty_toolset(self):
        req = _req("send_message_to_user", "future_task")
        assert strip_bypass_tools(req) == ["send_message_to_user", "future_task"]
        assert req.func_tool.empty()

    def test_non_list_tools_attribute_is_ignored(self):
        req = SimpleNamespace(func_tool=SimpleNamespace(tools=None))
        assert strip_bypass_tools(req) == []


class TestStripper:
    def test_switch_off_keeps_astrbot_default(self):
        stripper = BypassToolStripper(lambda: False)
        req = _req("send_message_to_user", "future_task")
        assert stripper.apply(req) == []
        assert req.func_tool.names() == ["send_message_to_user", "future_task"]

    @pytest.mark.parametrize("value,expected", [
        (True, True), (None, True), ("true", True), ("off", False), ("false", False), ("", False), (0, False),
    ])
    def test_switch_value_coercion(self, value, expected):
        assert BypassToolStripper(lambda: value).enabled() is expected

    def test_switch_read_error_defaults_to_on(self):
        def boom():
            raise RuntimeError("x")

        assert BypassToolStripper(boom).enabled() is True

    def test_logs_info_once_then_debug(self, monkeypatch):
        log = Mock()
        monkeypatch.setattr(bt, "logger", log)
        stripper = BypassToolStripper(lambda: True)
        stripper.apply(_req("send_message_to_user", "future_task"))
        stripper.apply(_req("future_task"))
        assert log.info.call_count == 1
        assert "stripped=2" in log.info.call_args.args[0]
        assert log.debug.call_count == 1
        assert stripper.stripped_total == 2

    def test_exception_falls_back_and_warns(self, monkeypatch):
        log = Mock()
        monkeypatch.setattr(bt, "logger", log)

        def boom(_req):
            raise ValueError("bad")

        monkeypatch.setattr(bt, "strip_bypass_tools", boom)
        req = _req("send_message_to_user")
        assert BypassToolStripper(lambda: True).apply(req) == []
        assert req.func_tool.names() == ["send_message_to_user"]
        log.warning.assert_called_once()
        assert "ValueError" in log.warning.call_args.args[0]


class TestProactiveChatSelfCheck:
    def _ctx(self, *stars):
        return SimpleNamespace(get_all_stars=lambda: list(stars))

    def test_detects_activated_plugin(self):
        star = SimpleNamespace(name="proactive", root_dir_name="astrbot_plugin_proactive_chat", activated=True)
        assert find_active_proactive_chat(self._ctx(star)) is True

    def test_deactivated_or_absent(self):
        star = SimpleNamespace(name="x", root_dir_name="astrbot_plugin_proactive_chat", activated=False)
        other = SimpleNamespace(name="y", root_dir_name="astrbot_plugin_iris_memory", activated=True)
        assert find_active_proactive_chat(self._ctx(star, other)) is False
        assert find_active_proactive_chat(self._ctx()) is False

    def test_query_error_treated_as_inactive(self):
        def boom():
            raise RuntimeError("x")

        assert find_active_proactive_chat(SimpleNamespace(get_all_stars=boom)) is False

    def test_check_warns_once_when_active(self, monkeypatch):
        log = Mock()
        monkeypatch.setattr(bt, "logger", log)
        star = SimpleNamespace(name="astrbot_plugin_proactive_chat", root_dir_name="", activated=True)
        assert BypassToolStripper(lambda: True).check_proactive_chat(self._ctx(star)) is True
        log.warning.assert_called_once()
        log.reset_mock()
        assert BypassToolStripper(lambda: True).check_proactive_chat(self._ctx()) is False
        log.warning.assert_not_called()


@pytest.mark.asyncio
async def test_on_llm_request_strips_before_anything_else():
    from astrbot_plugin_iris_memory.main import IrisMemoryPlugin

    plugin = object.__new__(IrisMemoryPlugin)
    plugin._bypass_stripper = BypassToolStripper(lambda: True)
    # 决策阶段直接终止请求：剥离也必须已经发生
    plugin._handle_reply_decision = AsyncMock(return_value=True)
    req = _req("save_memory", "send_message_to_user", "future_task")

    await plugin.on_llm_request(Mock(), req)

    assert req.func_tool.names() == ["save_memory"]


@pytest.mark.asyncio
async def test_on_llm_request_switch_off_keeps_tools():
    from astrbot_plugin_iris_memory.main import IrisMemoryPlugin

    plugin = object.__new__(IrisMemoryPlugin)
    plugin._bypass_stripper = BypassToolStripper(lambda: False)
    plugin._handle_reply_decision = AsyncMock(return_value=True)
    req = _req("save_memory", "send_message_to_user", "future_task")

    await plugin.on_llm_request(Mock(), req)

    assert req.func_tool.names() == ["save_memory", "send_message_to_user", "future_task"]


def test_hidden_config_default_is_on():
    from astrbot_plugin_iris_memory.iris_memory.config.defaults import Defaults

    assert Defaults().get_by_flat_key("lifelike_strip_bypass_tools") is True


class TestCronSelfCheck:
    """审查 M1：已排好的 active_agent cron 任务不经过 on_llm_request，只能启动时提示。"""

    def _ctx(self, jobs=None, error=None):
        async def list_jobs(job_type=None):
            assert job_type == "active_agent"
            if error is not None:
                raise error
            return jobs or []

        return SimpleNamespace(cron_manager=SimpleNamespace(list_jobs=list_jobs))

    @pytest.mark.asyncio
    async def test_enabled_jobs_warn_with_ids_only(self, monkeypatch):
        log = Mock()
        monkeypatch.setattr(bt, "logger", log)
        jobs = [
            SimpleNamespace(job_id="job-a", name="提醒主人吃药", description="秘密", enabled=True),
            SimpleNamespace(job_id="job-b", name="x", description="y", enabled=False),
        ]
        n = await BypassToolStripper(lambda: True).check_cron_jobs(self._ctx(jobs))
        assert n == 1
        msg = log.warning.call_args.args[0]
        assert "job-a" in msg and "job-b" not in msg
        assert "提醒主人吃药" not in msg and "秘密" not in msg

    @pytest.mark.asyncio
    async def test_no_jobs_info(self, monkeypatch):
        log = Mock()
        monkeypatch.setattr(bt, "logger", log)
        assert await BypassToolStripper(lambda: True).check_cron_jobs(self._ctx([])) == 0
        log.warning.assert_not_called()

    @pytest.mark.asyncio
    async def test_errors_and_missing_manager(self):
        s = BypassToolStripper(lambda: True)
        assert await s.check_cron_jobs(self._ctx(error=RuntimeError("db"))) == -1
        assert await s.check_cron_jobs(SimpleNamespace()) == -1

    @pytest.mark.asyncio
    async def test_switch_off_skips(self):
        ctx = self._ctx(error=AssertionError("must not be called"))
        assert await BypassToolStripper(lambda: False).check_cron_jobs(ctx) == 0
