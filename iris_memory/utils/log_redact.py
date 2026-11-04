"""
日志去正文（活人感 P0-3，开关 ``lifelike_log_redact``，默认 true）

INFO 及以上级别的日志不记聊天正文，只留长度：

- 适配器入站：``[Telethon] Committing AstrBot event: ... text=%r`` → ``text_len=<n>``
  （适配器在独立插件里，不改它的代码，只在它的插件 logger 上挂过滤器）；
- AstrBot core 入站 ``event_bus``（``[conf] [pid(type)] 名字/id: 正文``）与出站
  ``respond/stage``（``Prepare to send - 名字/id: 正文``），两者都带
  ``extra={"category": "user_chat"}``，正文替换为 ``len=<n>``；
- AstrBot core 语音转文字结果 ``Speech-to-text[ (referenced message)] result: 正文``
  → ``len=<n>``（识别原文就是用户说的话）；
- Iris 自己少数带正文的 INFO 行通过 :func:`text_for_log` 输出。

DEBUG 级不过滤（主人临时开 DEBUG 排障时仍能看到完整内容）。开关关闭时过滤器原样放行，
行为与现网一致；过滤过程异常时原样放行（旧行为）并 WARN 一次（WARN 不含正文）。
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Callable, Iterable, Optional

FLAG_KEY = "lifelike_log_redact"
_FILTER_FLAG = "_iris_lifelike_log_redact"

# 过滤器挂载的 logger：AstrBot core 全局 logger + 适配器 / Iris 插件 logger。
# AstrBot 的插件 logger 名为 ``astrbot.plugin.<metadata.name>``，propagate=False，
# 所以必须逐个挂；logging.getLogger 对尚未创建的名字会先建好对象，之后 AstrBot
# 的 LogManager.GetLogger 拿到的是同一个对象，过滤器不会丢。
DEFAULT_LOGGER_NAMES: tuple[str, ...] = (
    "astrbot",
    "astrbot.plugin.telethon_adapter",
    "astrbot.plugin.astrbot_plugin_iris_memory",
)
PLUGIN_LOGGER_PREFIX = "astrbot.plugin."

_TELETHON_PREFIX = "[Telethon] Committing AstrBot event:"
_CMD_RE = re.compile(r"^/[A-Za-z0-9_]{1,32}")
# core preprocess_stage：logger.info(f"Speech-to-text{suffix} result: " + result)
_STT_RESULT_RE = re.compile(r"^(Speech-to-text[^\n:]*? result: )", re.DOTALL)

# user_chat 行的"头部"（名字/id: ）。按顺序尝试：
#   A. event_bus，名字无空白（或无名字）：[conf] [pid(type)] name/id: 或 [conf] [pid(type)] id:
#   B. event_bus，名字含空白：[conf] [pid(type)] na me/id:
#   C. respond/stage：Prepare to send - name/id:
_USER_CHAT_HEADS = (
    re.compile(r"^(\[[^\]\n]*\] \[[^\]\n]*\] [^\s:]+: )", re.DOTALL),
    re.compile(r"^(\[[^\]\n]*\] \[[^\]\n]*\] [^\n]*?/[^\s/:]+: )", re.DOTALL),
    re.compile(r"^(Prepare to send - [^\n]*?/[^\s/:]*: )", re.DOTALL),
)

_warned_lock = threading.Lock()
_warned = False


def summarize_text(text: object) -> str:
    """正文 → ``len=<n>``；以 ``/命令`` 开头时只额外保留命令名（不含参数）。"""
    s = "" if text is None else str(text)
    out = f"len={len(s)}"
    m = _CMD_RE.match(s.lstrip())
    if m:
        out += f" cmd={m.group(0)}"
    return out


def redact_record(record: logging.LogRecord) -> bool:
    """就地改写一条日志记录；返回是否改写。只处理已知的带正文行。"""
    msg = record.msg
    if not isinstance(msg, str):
        return False
    # 1) 适配器入站：保持 %-格式，只换第 4 个参数
    if msg.startswith(_TELETHON_PREFIX) and "text=%r" in msg:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 4:
            text = args[3]
            record.msg = msg.replace("text=%r", "text_len=%d", 1)
            record.args = tuple(args[:3]) + (len("" if text is None else str(text)),) + tuple(args[4:])
            return True
        return False
    # 2) core 语音转文字结果（无 category，按固定前缀识别）
    if msg.startswith("Speech-to-text"):
        full = record.getMessage()
        m = _STT_RESULT_RE.match(full)
        if m:
            head = m.group(1)
            record.msg = head + f"len={len(full) - len(head)}"
            record.args = ()
            return True
        return False
    # 3) core 入站/出站 user_chat 行
    if getattr(record, "category", None) != "user_chat":
        return False
    full = record.getMessage()
    for pat in _USER_CHAT_HEADS:
        m = pat.match(full)
        if m:
            head = m.group(1)
            record.msg = head + summarize_text(full[len(head):])
            record.args = ()
            return True
    # 认不出头部：整行替换（失败关闭，不泄漏）
    record.msg = f"[user_chat redacted] {summarize_text(full)}"
    record.args = ()
    return True


def _warn_once(exc: BaseException) -> None:
    global _warned
    with _warned_lock:
        if _warned:
            return
        _warned = True
    try:
        logging.getLogger("astrbot.plugin.astrbot_plugin_iris_memory.redact").warning(
            f"[lifelike.log_redact] redact failed, passing record through: {type(exc).__name__}"
        )
    except Exception:
        pass


class ChatTextRedactFilter(logging.Filter):
    """INFO 及以上的聊天正文 → 长度。永远返回 True（只改写，不丢弃）。"""

    def __init__(self, enabled: Callable[[], bool]) -> None:
        super().__init__()
        self._enabled = enabled
        self._local = threading.local()
        self.loggers: list[logging.Logger] = []
        setattr(self, _FILTER_FLAG, True)

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.INFO:
            return True
        if getattr(self._local, "busy", False):
            return True
        self._local.busy = True
        try:
            if not self._is_enabled():
                return True
            redact_record(record)
        except Exception as e:  # 异常 → 原样放行（旧行为）
            _warn_once(e)
        finally:
            self._local.busy = False
        return True

    def _is_enabled(self) -> bool:
        try:
            return _to_bool(self._enabled(), True)
        except Exception:
            return True


def _to_bool(v: object, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return default


def install(
    enabled: Callable[[], bool],
    logger_names: Optional[Iterable[str]] = None,
    include_existing_plugins: bool = True,
) -> ChatTextRedactFilter:
    """在目标 logger 上挂过滤器（幂等：先移除旧的同类过滤器，避免热重载叠加）。

    返回过滤器对象；``filter.loggers`` 为挂载的 logger 列表，供 :func:`uninstall` 精确移除。
    """
    names = list(logger_names if logger_names is not None else DEFAULT_LOGGER_NAMES)
    if include_existing_plugins:
        for name in list(logging.root.manager.loggerDict):
            if isinstance(name, str) and name.startswith(PLUGIN_LOGGER_PREFIX) and name.count(".") == 2:
                names.append(name)
    filt = ChatTextRedactFilter(enabled)
    loggers: list[logging.Logger] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        lg = logging.getLogger(name)
        _remove_from(lg)
        lg.addFilter(filt)
        loggers.append(lg)
    filt.loggers = loggers
    return filt


def uninstall(filt: Optional[ChatTextRedactFilter]) -> int:
    """只移除指定过滤器（热重载时新实例可能先装好，不能误删新实例的过滤器）。"""
    if filt is None:
        return 0
    n = 0
    for lg in list(getattr(filt, "loggers", []) or []):
        if filt in lg.filters:
            lg.removeFilter(filt)
            n += 1
    return n


def _remove_from(lg: logging.Logger) -> int:
    n = 0
    for f in list(lg.filters):
        if getattr(f, _FILTER_FLAG, False):
            lg.removeFilter(f)
            n += 1
    return n


# ---------------------------------------------------------------------------
# Iris 自身日志用的辅助
# ---------------------------------------------------------------------------

_flag_getter: Optional[Callable[[], object]] = None


def set_flag_getter(getter: Optional[Callable[[], object]]) -> None:
    global _flag_getter
    _flag_getter = getter


def redact_enabled() -> bool:
    getter = _flag_getter
    if getter is None:
        try:
            from ..config import get_config
        except Exception:
            return True
        try:
            return _to_bool(get_config().get(FLAG_KEY, True), True)
        except Exception:
            return True
    try:
        return _to_bool(getter(), True)
    except Exception:
        return True


def text_for_log(text: object, max_chars: int = 50) -> str:
    """Iris 自身 INFO 日志里需要提到正文时用：开关开 → ``len=<n>``；关 → 原截断行为。"""
    s = "" if text is None else str(text)
    if redact_enabled():
        return summarize_text(s)
    return s[:max_chars]
