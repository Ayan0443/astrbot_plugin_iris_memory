"""
Iris Chat Memory - 旁路出站收口（活人感 P0-1）

AstrBot 主管线在 ``build_main_agent`` 里自动给主 Agent 挂两个能"自己往外发消息"的
内置工具（core/astr_main_agent.py）：

- ``future_task``：``provider_settings.add_cron_tools`` 为真时挂载，LLM 可以排一个
  定时任务，到点由 AstrBot cron 直接唤醒 Agent 发消息；
- ``send_message_to_user``：平台 ``support_proactive_message`` 为真时挂载，LLM 可以
  在一次回复之外主动往任意会话发消息。

这两条路径都不经过 Iris 主动账本（outbound / proactive_turns）、静默会话、滚动额度与
静音判定。``on_llm_request`` 钩子在 ``build_main_agent`` 之后、``agent_runner.reset``
之前执行，此时从 ``req.func_tool`` 移除即可让本轮 Agent 看不到这两个工具（模型即便
硬调也只会得到 "tool not found"）。

开关：HiddenConfig ``lifelike_strip_bypass_tools``（默认 true；修复项，不受
``lifelike_enable`` 控制）。日志前缀 ``[lifelike.outbound]``，只记工具名与计数。
"""

from __future__ import annotations

from typing import Any, Iterable

from astrbot.api import logger

__all__ = [
    "BYPASS_TOOL_NAMES",
    "PROACTIVE_CHAT_PLUGIN",
    "strip_bypass_tools",
    "find_active_proactive_chat",
    "BypassToolStripper",
]

# 绕过 Iris 账本的内置出站工具（AstrBot 4.x：astrbot/core/tools/{message,cron}_tools.py）
BYPASS_TOOL_NAMES: tuple[str, ...] = ("send_message_to_user", "future_task")

# 同容器内的第二套主动插件（只检测，不修改）
PROACTIVE_CHAT_PLUGIN = "astrbot_plugin_proactive_chat"


def _tool_name(tool: Any) -> str:
    return str(getattr(tool, "name", "") or "")


def strip_bypass_tools(req: Any, names: Iterable[str] = BYPASS_TOOL_NAMES) -> list[str]:
    """从 ``req.func_tool`` 中移除旁路出站工具，返回实际移除的工具名（按出现顺序）。

    - ``func_tool`` 为空 / 不存在：不做任何事，返回 []；
    - 只按工具名移除，其他工具的顺序与对象保持不变；
    - 原地修改同一个 ToolSet（agent_runner.reset 之后读取的就是它）。
    """
    tool_set = getattr(req, "func_tool", None)
    if tool_set is None:
        return []
    tools = getattr(tool_set, "tools", None)
    if not isinstance(tools, list) or not tools:
        return []
    targets = {str(n) for n in names}
    removed = [_tool_name(t) for t in tools if _tool_name(t) in targets]
    if not removed:
        return []
    kept = [t for t in tools if _tool_name(t) not in targets]
    # 与 ToolSet.remove_tool 相同的写法：整体替换列表
    tool_set.tools = kept
    return removed


def find_active_proactive_chat(context: Any) -> bool:
    """proactive_chat 插件当前是否处于启用（activated）状态；查询失败按未启用处理。"""
    try:
        stars = context.get_all_stars() or []
    except Exception:
        return False
    for star in stars:
        try:
            root = str(getattr(star, "root_dir_name", "") or "")
            name = str(getattr(star, "name", "") or "")
            if PROACTIVE_CHAT_PLUGIN not in (root, name):
                continue
            if bool(getattr(star, "activated", False)):
                return True
        except Exception:
            continue
    return False


class BypassToolStripper:
    """``on_llm_request`` 用的有状态包装：读取开关、只在首次剥离时打 INFO。"""

    def __init__(self, enabled_get) -> None:
        self._enabled_get = enabled_get
        self._logged_info = False
        self.stripped_total = 0

    def enabled(self) -> bool:
        try:
            value = self._enabled_get()
        except Exception:
            return True
        if isinstance(value, str):
            return value.strip().lower() not in ("false", "0", "no", "off", "")
        return True if value is None else bool(value)

    def apply(self, req: Any) -> list[str]:
        """按开关剥离；异常时不改请求（回到 AstrBot 默认行为）并记 WARN。"""
        if not self.enabled():
            return []
        try:
            removed = strip_bypass_tools(req)
        except Exception as e:
            logger.warning(f"[lifelike.outbound] strip failed: {type(e).__name__}")
            return []
        if removed:
            self.stripped_total += 1
            msg = f"[lifelike.outbound] stripped={len(removed)} tools={','.join(removed)}"
            if not self._logged_info:
                self._logged_info = True
                logger.info(msg + " (further strips logged at debug)")
            else:
                logger.debug(msg)
        return removed

    async def check_cron_jobs(self, context: Any) -> int:
        """启动自检（审查 M1）：AstrBot cron 唤醒 Agent 时不经过 on_llm_request，剥离不到。

        已排好的 active_agent 任务到点仍会绕过 Iris 账本直接发消息，所以存在已启用的
        这类任务时 WARN 一次，只列 job_id（不打印名称 / 描述 / 正文），交给主人决定是否
        在 WebUI 停用。只读，不修改任何任务。返回已启用任务数；查询失败返回 -1。
        """
        if not self.enabled():
            return 0
        mgr = getattr(context, "cron_manager", None)
        lister = getattr(mgr, "list_jobs", None)
        if lister is None:
            return -1
        try:
            jobs = await lister("active_agent")
        except Exception as e:
            logger.debug(f"[lifelike.outbound] cron job check failed: {type(e).__name__}")
            return -1
        active = [j for j in (jobs or []) if bool(getattr(j, "enabled", False))]
        if active:
            ids = ",".join(str(getattr(j, "job_id", "?"))[:36] for j in active[:10])
            logger.warning(
                f"[lifelike.outbound] {len(active)} enabled active_agent cron job(s) bypass the "
                f"Iris ledger when they fire (job_ids={ids}); review/disable them in AstrBot WebUI"
            )
        else:
            logger.info("[lifelike.outbound] cron self-check: enabled active_agent jobs=0")
        return len(active)

    def check_proactive_chat(self, context: Any) -> bool:
        """启动自检：proactive_chat 仍启用时 WARN 一次（不修改该插件）。"""
        active = find_active_proactive_chat(context)
        if active:
            logger.warning(
                f"[lifelike.outbound] plugin {PROACTIVE_CHAT_PLUGIN} is activated; "
                "it sends proactive messages outside the Iris ledger. "
                "Disable it in AstrBot plugin management (files can stay)."
            )
        return active
