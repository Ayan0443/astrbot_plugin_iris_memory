"""活人感 P1-7：``/iris_mem beliefs|forget``（仅主人私聊）。

- ``/iris_mem beliefs [list]`` 查看认知；``confirm <id>`` 主人确认；``retract <id>`` 作废。
- ``/iris_mem forget <关键词>`` 预览数量（不显示原文）→ ``/iris_mem forget confirm <确认码>`` 执行。

群里一律不响应（返回提示，不展示任何条目）。回执不写 L1 / outbound / learning。
"""

from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from ..core import get_logger
from .base import CommandHandler, CommandResult, ParsedArgs

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

logger = get_logger("commands.lifelike")

_PRIVATE_ONLY = "这个指令只能在主人私聊里用。"


def _owner_private(event: "AstrMessageEvent") -> Tuple[Optional[object], str]:
    from ..lifelike.memory.service import get_current

    lm = get_current()
    try:
        if event.get_group_id() or not event.is_private_chat():
            return None, _PRIVATE_ONLY
        sender = str(event.get_sender_id() or "")
    except Exception:
        return None, _PRIVATE_ONLY
    if lm is None:
        return None, "认知模块不可用。"
    if not lm.is_owner_private(f"private:{sender}", sender):
        return None, _PRIVATE_ONLY
    return lm, sender


def _args(args: ParsedArgs) -> List[str]:
    return [str(a) for a in (args.raw_args or [])]


class BeliefsCommandHandler(CommandHandler):
    @property
    def name(self) -> str:
        return "beliefs"

    @property
    def description(self) -> str:
        return "她对你的认知（事实/偏好/推测，可确认或作废）"

    @property
    def sub_commands(self) -> Dict[str, str]:
        return {"list": "查看", "confirm <id>": "确认", "retract <id>": "作废"}

    async def handle(
        self, event: "AstrMessageEvent", args: ParsedArgs, sub_command: Optional[str] = None
    ) -> CommandResult:
        lm, sender = _owner_private(event)
        if lm is None:
            return CommandResult(success=False, message=sender)
        try:
            return CommandResult(success=True, message=await lm.beliefs_command(sender, _args(args)))
        except Exception as e:
            logger.warning(f"[lifelike.beliefs] command failed: {type(e).__name__}")
            return CommandResult(success=False, message="执行失败，详见服务日志")


class ForgetCommandHandler(CommandHandler):
    @property
    def name(self) -> str:
        return "forget"

    @property
    def description(self) -> str:
        return "忘掉某件事（先预览数量，再确认；删除后不再学回来）"

    @property
    def sub_commands(self) -> Dict[str, str]:
        return {"<关键词>": "预览", "confirm <确认码>": "执行删除"}

    async def handle(
        self, event: "AstrMessageEvent", args: ParsedArgs, sub_command: Optional[str] = None
    ) -> CommandResult:
        lm, sender = _owner_private(event)
        if lm is None:
            return CommandResult(success=False, message=sender)
        try:
            return CommandResult(success=True, message=await lm.forget_command(sender, _args(args)))
        except Exception as e:
            logger.warning(f"[lifelike.forget] command failed: {type(e).__name__}")
            return CommandResult(success=False, message="执行失败，详见服务日志")
