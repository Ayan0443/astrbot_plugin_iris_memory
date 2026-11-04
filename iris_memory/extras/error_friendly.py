"""错误消息友好化处理器

在消息发送前拦截框架错误消息，替换为友好提示。
（自 v2 processing/message_processor.py 摘出，独立保留）

lifelike P0-2（DECISIONS D2）：额外覆盖 AstrBot agent 异常路径。core 在 agent
失败时用 ``event.send()`` 直发 ``"Error occurred while processing agent request: {e}"``，
这条路不经过 ``on_decorating_result``，所以旧的替换覆盖不到。三层兜底：

1. ``apply_agent_error_extra``：在 ``on_llm_request`` 里给事件写入 core 自己认的
   ``persona_custom_error_message`` extra（人格未配置时才写；不改人格数据）。
   core 三条 agent 报错路径都会优先使用它。
2. ``process_result``：管线结果里出现 agent 报错文本（``LLM 响应错误:`` /
   ``Error occurred during AI execution``）时替换。
3. ``install_send_guard``：包一层事件实例的 ``send``，直发的 agent 报错文本被替换
   （覆盖 on_llm_request 之前就抛出的异常）。

开关：``extras.error_friendly.enable``（旧开关）且 ``lifelike_error_friendly_agent``
（HiddenConfig，默认 true）。后者关闭时行为与旧版一致。日志只记异常类名/路径，
不记报错原文与聊天正文。
"""

import random
import re
from typing import Any, Callable, Final, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

# core: astrbot/core/persona_error_reply.py PERSONA_CUSTOM_ERROR_MESSAGE_EXTRA_KEY
PERSONA_ERROR_EXTRA_KEY: Final[str] = "persona_custom_error_message"
AGENT_SWITCH_KEY: Final[str] = "lifelike_error_friendly_agent"
_GUARD_MARK: Final[str] = "_iris_error_send_guard"
_LOG_PREFIX: Final[str] = "[lifelike.error_friendly]"
_EXC_TYPE_RE = re.compile(r"Error Type:\s*([A-Za-z_][\w.]*)")


class ErrorFriendlyMessages:
    """错误消息友好化配置"""

    ERROR_PATTERNS: Final[tuple] = (
        "AstrBot 请求失败",
        "错误类型:",
        "错误信息:",
        "请在平台日志查看",
    )

    DEFAULT_FRIENDLY_MSG: Final[str] = "呜...遇到了一点问题，请稍后再试试吧~"
    NETWORK_ERROR_MSG: Final[str] = "网络好像不太稳定呢，稍后再试试？"
    RATE_LIMIT_MSG: Final[str] = "我需要休息一下，请稍后再来找我~"
    BAD_REQUEST_MSG: Final[str] = "请求出了点问题，稍后再试试吧~"

    # AstrBot core agent 异常路径的报错前缀（core internal.py /
    # astr_agent_run_util.py / tool_loop_agent_runner.py）
    AGENT_ERROR_PREFIXES: Final[tuple] = (
        ("agent_request", "Error occurred while processing agent request"),
        ("ai_execution", "Error occurred during AI execution"),
        ("llm_response", "LLM 响应错误:"),
        ("llm_response", "LLM 响应错误："),
    )

    # 中文、口吻自然、不暴露异常（ROADMAP P0-2）。
    # 审查 M4：不写"等下再跟你说"这类承诺——没有任何机制会兑现它，进了上下文还可能被顺着编。
    AGENT_FRIENDLY_MSGS: Final[tuple] = (
        "我这边卡了一下，刚才那句没接上。",
        "刚才走神了，这句没接住。",
        "我这边网有点卡，刚才没说出来。",
    )


class ErrorFriendlyProcessor:
    """错误消息友好化处理器

    在消息发送前拦截框架错误消息，替换为友好提示。

    Args:
        config: 插件配置对象（iris_memory.config.Config）
    """

    def __init__(
        self,
        config: Any,
        *,
        choose: Optional[Callable[[tuple], str]] = None,
    ) -> None:
        self._config = config
        self._choose = choose or random.choice

    def should_process(self, event: AstrMessageEvent) -> bool:
        """检查是否需要处理该事件"""
        return self._is_enabled()

    def process_result(self, result: Any) -> None:
        """处理消息结果，替换错误消息"""
        if not result:
            return

        text = self._get_result_plain_text(result)
        if not text:
            return

        if self._agent_enabled():
            kind = self.agent_error_kind(text)
            if kind is not None:
                result.chain.clear()
                result.message(self.agent_friendly_message())
                logger.warning(f"{_LOG_PREFIX} agent error replaced path=result kind={kind}")
                return

        if self._is_framework_error(text):
            friendly_msg = self._get_friendly_error_message(text)
            result.chain.clear()
            result.message(friendly_msg)

    def _is_enabled(self) -> bool:
        """检查错误消息友好化功能是否启用"""
        try:
            if hasattr(self._config, "get"):
                return bool(self._config.get("extras.error_friendly.enable", True))
            return True
        except Exception:
            return True

    def _agent_enabled(self) -> bool:
        """agent 异常路径覆盖：旧开关 + lifelike_error_friendly_agent 同时为真"""
        if not self._is_enabled():
            return False
        try:
            if hasattr(self._config, "get"):
                return bool(self._config.get(AGENT_SWITCH_KEY, True))
            return True
        except Exception:
            return True

    # ------------------------------------------------------------------ agent 路径

    @staticmethod
    def agent_error_kind(text: str) -> Optional[str]:
        """识别 core agent 报错文本；返回日志用的类别（异常类名或路径 id），否则 None"""
        if not text:
            return None
        stripped = text.lstrip()
        for marker_id, prefix in ErrorFriendlyMessages.AGENT_ERROR_PREFIXES:
            if stripped.startswith(prefix):
                m = _EXC_TYPE_RE.search(stripped)
                return m.group(1) if m else marker_id
        return None

    def agent_friendly_message(self) -> str:
        try:
            return self._choose(ErrorFriendlyMessages.AGENT_FRIENDLY_MSGS)
        except Exception:
            return ErrorFriendlyMessages.AGENT_FRIENDLY_MSGS[0]

    def is_error_response(self, resp: Any) -> bool:
        """agent 报错响应（core ``role="err"`` / 友好文案 / 原始报错前缀）不应进 L1 与学习采集。

        只在 agent 覆盖开关打开时返回 True；关闭时恒 False（= 旧行为：报错照常入 L1）。
        """
        try:
            if not self._agent_enabled() or resp is None:
                return False
            if str(getattr(resp, "role", "") or "") == "err":
                return True
            text = str(getattr(resp, "completion_text", "") or "").strip()
            if not text:
                return False
            if text in ErrorFriendlyMessages.AGENT_FRIENDLY_MSGS:
                return True
            return self.agent_error_kind(text) is not None
        except Exception as e:
            logger.warning(f"{_LOG_PREFIX} error response check failed: {type(e).__name__}")
            return False

    def apply_agent_error_extra(self, event: Any) -> bool:
        """人格未配置 custom_error_message 时，为本事件写入友好报错（core 原生读取）。

        必须在 core 构建人格之后调用（on_llm_request），否则会被 core 以 None 覆盖。
        返回是否写入。任何异常都吞掉（回退旧行为）。
        """
        try:
            if not self._agent_enabled():
                return False
            if event is None or not hasattr(event, "get_extra") or not hasattr(event, "set_extra"):
                return False
            existing = event.get_extra(PERSONA_ERROR_EXTRA_KEY)
            if isinstance(existing, str) and existing.strip():
                return False  # 尊重人格自己配置的报错文案
            event.set_extra(PERSONA_ERROR_EXTRA_KEY, self.agent_friendly_message())
            return True
        except Exception as e:
            logger.warning(f"{_LOG_PREFIX} set error extra failed: {type(e).__name__}")
            return False

    def install_send_guard(self, event: Any) -> bool:
        """包装事件实例的 send：直发的 agent 报错文本替换为友好文案。

        幂等；开关在每次发送时现查（关闭后包装层直接透传）。返回是否新安装。
        """
        try:
            if not self._agent_enabled() or event is None:
                return False
            original = getattr(event, "send", None)
            if original is None or getattr(original, _GUARD_MARK, False):
                return False
            processor = self

            async def guarded_send(message: Any, *args: Any, **kwargs: Any) -> Any:
                try:
                    if processor._agent_enabled():
                        text = processor._get_result_plain_text(message)
                        kind = processor.agent_error_kind(text)
                        if kind is not None:
                            message.chain.clear()
                            message.message(processor.agent_friendly_message())
                            logger.warning(
                                f"{_LOG_PREFIX} agent error replaced path=send kind={kind}"
                            )
                except Exception as e:
                    logger.warning(f"{_LOG_PREFIX} send guard failed: {type(e).__name__}")
                return await original(message, *args, **kwargs)

            setattr(guarded_send, _GUARD_MARK, True)
            event.send = guarded_send
            return True
        except Exception as e:
            logger.warning(f"{_LOG_PREFIX} install send guard failed: {type(e).__name__}")
            return False

    def _get_result_plain_text(self, result: Any) -> str:
        """获取消息结果的纯文本内容"""
        if hasattr(result, "get_plain_text"):
            return result.get_plain_text() or ""
        return ""

    def _is_framework_error(self, text: str) -> bool:
        """检测是否为 AstrBot 框架错误消息"""
        text_lower = text.lower()

        match_count = sum(
            1 for pattern in ErrorFriendlyMessages.ERROR_PATTERNS
            if pattern.lower() in text_lower
        )
        if match_count >= 2:
            return True

        if "400" in text or "bad request" in text_lower:
            if any(keyword in text_lower for keyword in ["请求", "request", "error", "failed"]):
                return True

        error_keywords = [
            "error", "failed", "exception", "traceback",
            "请求失败", "错误", "异常"
        ]
        framework_indicators = [
            "platform", "api", "http", "status code",
            "平台", "框架"
        ]

        has_error_keyword = any(kw in text_lower for kw in error_keywords)
        has_framework_indicator = any(ind in text_lower for ind in framework_indicators)

        return has_error_keyword and has_framework_indicator

    def _get_friendly_error_message(self, text: str) -> str:
        """根据错误内容返回合适的友好消息"""
        text_lower = text.lower()

        if "400" in text or "bad request" in text_lower:
            return ErrorFriendlyMessages.BAD_REQUEST_MSG

        if any(kw in text_lower for kw in ["network", "timeout", "连接", "网络"]):
            return ErrorFriendlyMessages.NETWORK_ERROR_MSG

        if any(kw in text_lower for kw in ["rate", "limit", "限流", "频率", "频繁"]):
            return ErrorFriendlyMessages.RATE_LIMIT_MSG

        return ErrorFriendlyMessages.DEFAULT_FRIENDLY_MSG
