"""StyleGuard 审计（log 模式，ROADMAP P0-6 / 设计 §8.5）

开关 ``lifelike_style_guard_mode``（HiddenConfig 平铺键）：

- ``off``：什么都不做（不扫描、不维护内存标记、不写库），与现网一致；
- ``log``（P0 默认）：扫描已定稿的回复，只把命中的规则 id 写进
  continuity.db 的 ``style_audit``（不存原文）并累加 ``counters``；**不改写回复**；
- ``fix`` / ``regen`` 属于 P1-9 / P2-8，本阶段按 ``log`` 处理。

会话历史只以 ``ReplyMarkers``（开头 / 结尾 4 字、问句、反应词、语气词）保存在内存里，
重启即清空，不落盘。任何异常只记日志（类型名，不含正文）并跳过本次审计；
连续 5 次失败熔断 1 小时（ROADMAP §2.3）。
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from typing import Any, Callable, Deque, Dict, List, Optional, Set

from astrbot.api import logger

from . import guard

MODE_OFF = "off"
MODE_LOG = "log"
_KNOWN_MODES = ("off", "log", "fix", "regen")

CHANNEL_PASSIVE = "passive"
CHANNEL_PROACTIVE = "proactive"

_MAX_SCOPES = 256
_HISTORY_LEN = guard.AIW07_WINDOW
_MENTION_TURNS = 6  # 作品提及往回看的对方消息条数
_FAIL_LIMIT = 5
_PAUSE_SECONDS = 3600.0

_LOG = "[lifelike.style]"


def normalize_mode(raw: Any) -> str:
    """开关值归一：未知值视为 off；fix/regen 在 P0 降级为 log"""
    mode = str(raw or "").strip().lower()
    if mode not in _KNOWN_MODES:
        return MODE_OFF
    return MODE_OFF if mode == MODE_OFF else MODE_LOG


class _ScopeState:
    __slots__ = ("markers", "user_mentions")

    def __init__(self) -> None:
        self.markers: Deque[guard.ReplyMarkers] = deque(maxlen=_HISTORY_LEN)
        self.user_mentions: Deque[bool] = deque(maxlen=_MENTION_TURNS)


class StyleAuditor:
    """log 模式风格审计；所有入口故障隔离，不抛出、不影响发送。"""

    def __init__(
        self,
        mode_get: Callable[[], Any],
        store_get: Callable[[], Any],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._mode_get = mode_get
        self._store_get = store_get
        self._clock = clock
        self._scopes: "OrderedDict[str, _ScopeState]" = OrderedDict()
        self._failures = 0
        self._paused_until = 0.0
        self._tasks: Set[asyncio.Task] = set()
        self._warned_mode: Optional[str] = None

    # ------------------------------------------------------------ 状态

    @property
    def mode(self) -> str:
        try:
            raw = self._mode_get()
        except Exception:
            return MODE_OFF
        mode = normalize_mode(raw)
        raw_s = str(raw or "").strip().lower()
        if raw_s in ("fix", "regen") and self._warned_mode != raw_s:
            self._warned_mode = raw_s
            logger.warning(f"{_LOG} mode={raw_s} not available in P0, running as log")
        return mode

    @property
    def paused(self) -> bool:
        return self._clock() < self._paused_until

    def _state(self, scope_key: str) -> _ScopeState:
        st = self._scopes.get(scope_key)
        if st is None:
            st = _ScopeState()
            self._scopes[scope_key] = st
            while len(self._scopes) > _MAX_SCOPES:
                self._scopes.popitem(last=False)
        else:
            self._scopes.move_to_end(scope_key)
        return st

    def _failed(self, where: str, exc: BaseException) -> None:
        self._failures += 1
        logger.warning(f"{_LOG} {where} failed: {type(exc).__name__}")
        if self._failures >= _FAIL_LIMIT and not self.paused:
            self._paused_until = self._clock() + _PAUSE_SECONDS
            self._failures = 0
            logger.warning(f"{_LOG} paused_error: {_FAIL_LIMIT} consecutive failures, paused for 1h")

    # ------------------------------------------------------------ 扫描

    def check(
        self,
        scope_key: str,
        channel: str,
        text: str,
        *,
        user_text: str = "",
    ) -> Optional[List[str]]:
        """同步扫描 + 更新内存标记；返回规则标签（可能为空列表）。

        mode=off、熔断中或文本为空时返回 None（表示未审计）。
        """
        if self.mode == MODE_OFF or self.paused:
            return None
        text = (text or "").strip()
        if not text or not scope_key:
            return None
        try:
            st = self._state(str(scope_key))
            if user_text:
                st.user_mentions.append(bool(guard.WORK_MENTION.search(user_text)))
            ctx: Dict[str, Any] = {
                "user_text": user_text,
                "history": tuple(st.markers),
                "work_mentioned": any(st.user_mentions),
            }
            tags = guard.rule_tags(guard.scan(text, ctx))
            st.markers.append(guard.extract_markers(text))
            return tags
        except Exception as e:  # 规则异常：跳过本次，不影响回复
            self._failed("scan", e)
            return None

    # ------------------------------------------------------------ 记录

    def _write(self, scope_key: str, channel: str, tags: List[str]) -> None:
        store = self._store_get()
        if store is None:
            return
        store.record_style_audit(scope_key, channel, tags, MODE_LOG)

    async def audit(
        self,
        scope_key: str,
        channel: str,
        text: str,
        *,
        user_text: str = "",
    ) -> Optional[List[str]]:
        """扫描并写库（在线程里写）；返回规则标签，未审计返回 None"""
        tags = self.check(scope_key, channel, text, user_text=user_text)
        if tags is None:
            return None
        if tags:
            logger.info(f"{_LOG} scope={scope_key} channel={channel} rules={','.join(tags)}")
        try:
            await asyncio.to_thread(self._write, str(scope_key), channel, tags)
            self._failures = 0
        except Exception as e:
            self._failed("write", e)
        return tags

    def schedule(
        self,
        scope_key: str,
        channel: str,
        text: str,
        *,
        user_text: str = "",
    ) -> None:
        """后台审计（不阻塞发送）；off 时零开销返回"""
        if self.mode == MODE_OFF or self.paused or not (text or "").strip() or not scope_key:
            return
        try:
            task = asyncio.get_running_loop().create_task(
                self.audit(scope_key, channel, text, user_text=user_text)
            )
        except Exception as e:
            self._failed("schedule", e)
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """等待已排队的后台审计（关停 / 测试用）"""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
