"""活人感廉价模型硬预算（ROADMAP §2.3a、设计 §11.4）。

所有 ``lifelike_*`` 模块的廉价模型调用共用一份**每日**预算：

- ``lifelike_budget_cheap_calls_daily``（默认 80）：调用次数上限；
- ``lifelike_budget_cheap_tokens_daily``（默认 150000）：输入+输出 token 上限。

计数落在连续性库 ``counters`` 表（按 Asia/Shanghai 自然日，重启不丢）：
``lifelike.budget.calls`` / ``lifelike.budget.tokens`` 为合计，
``…:<module>`` 为模块维度（module 与 Iris token_stats 的 module 名一致，
例如 ``lifelike_session_extract``）。

调用前 ``reserve`` 先占一次调用并按估算 token 判断是否超额；调用后 ``charge``
记实际 token（拿不到实际用量时用估算值）。超额时抛 ``CheapBudgetExceeded``，
调用方按设计 §11.3 降级（规则兜底 / 维持旧行为），不阻塞聊天。

日志只记模块名、计数与原因，不记 prompt 或正文。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

from astrbot.api import logger

from ..continuity import models as m

__all__ = [
    "CheapBudget",
    "CheapBudgetExceeded",
    "DEFAULT_CALLS_DAILY",
    "DEFAULT_TOKENS_DAILY",
    "estimate_tokens",
]

DEFAULT_CALLS_DAILY = 80
DEFAULT_TOKENS_DAILY = 150000
KEY_CALLS = "lifelike_budget_cheap_calls_daily"
KEY_TOKENS = "lifelike_budget_cheap_tokens_daily"

C_CALLS = "lifelike.budget.calls"
C_TOKENS = "lifelike.budget.tokens"
C_DENIED = "lifelike.budget.denied"

# 预留给输出的 token 估算（会话抽取 JSON 通常 <300）
DEFAULT_OUTPUT_RESERVE = 400


class CheapBudgetExceeded(RuntimeError):
    """预算耗尽（调用前判定，未发出 Provider 请求）。args[0] 为原因码 calls|tokens|error。"""

    @property
    def reason(self) -> str:
        return str(self.args[0]) if self.args else "budget"


def estimate_tokens(text: str) -> int:
    """保守估算：按字符数计（中文约 1 字 ≈ 1 token，英文偏高估），不依赖 tiktoken 下载。"""
    return max(1, len(text or ""))


def _to_limit(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, n)


class CheapBudget:
    """同步计数 + 异步 guarded_generate 包装。线程安全（store 调用在 to_thread 中）。"""

    def __init__(
        self,
        store: Any,
        config_get: Callable[[str, Any], Any],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._get = config_get
        self._clock = clock
        self._lock = threading.Lock()
        self._warned: set[tuple[str, str]] = set()  # (day, reason)：每天每类只 WARN 一次

    # ------------------------------------------------------------ limits / usage
    def limits(self) -> tuple[int, int]:
        try:
            calls = _to_limit(self._get(KEY_CALLS, DEFAULT_CALLS_DAILY), DEFAULT_CALLS_DAILY)
            tokens = _to_limit(self._get(KEY_TOKENS, DEFAULT_TOKENS_DAILY), DEFAULT_TOKENS_DAILY)
        except Exception:  # noqa: BLE001 - 配置读取失败按默认值
            calls, tokens = DEFAULT_CALLS_DAILY, DEFAULT_TOKENS_DAILY
        return calls, tokens

    def _day(self) -> str:
        return m.local_day(float(self._clock()))

    def usage(self) -> dict[str, int]:
        counters = self._store.get_counters(self._day())
        calls_limit, tokens_limit = self.limits()
        return {
            "calls": int(counters.get(C_CALLS, 0)),
            "tokens": int(counters.get(C_TOKENS, 0)),
            "calls_limit": calls_limit,
            "tokens_limit": tokens_limit,
        }

    # ------------------------------------------------------------ sync primitives
    def reserve(self, module: str, est_tokens: int) -> None:
        """占用一次调用；超额抛 CheapBudgetExceeded（不计数）。计数失败按超额处理（宁可不调）。"""
        day = self._day()
        calls_limit, tokens_limit = self.limits()
        with self._lock:
            try:
                counters = self._store.get_counters(day)
                used_calls = int(counters.get(C_CALLS, 0))
                used_tokens = int(counters.get(C_TOKENS, 0))
                reason = ""
                if used_calls >= calls_limit:
                    reason = "calls"
                elif used_tokens + max(0, int(est_tokens)) > tokens_limit:
                    reason = "tokens"
                if reason:
                    self._store.bump_counter(C_DENIED, 1, day)
                    self._store.bump_counter(f"{C_DENIED}:{module}", 1, day)
                else:
                    self._store.bump_counter(C_CALLS, 1, day)
                    self._store.bump_counter(f"{C_CALLS}:{module}", 1, day)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[lifelike.budget] reserve failed module={module}: {type(e).__name__}")
                raise CheapBudgetExceeded("error") from e
        if reason:
            if (day, reason) not in self._warned:
                self._warned.add((day, reason))
                logger.warning(
                    f"[lifelike.budget] exhausted reason={reason} module={module} day={day}"
                    f" calls={used_calls}/{calls_limit} tokens={used_tokens}/{tokens_limit}"
                )
            raise CheapBudgetExceeded(reason)

    def charge(self, module: str, tokens: int) -> None:
        tokens = max(0, int(tokens))
        if not tokens:
            return
        day = self._day()
        try:
            with self._lock:
                self._store.bump_counter(C_TOKENS, tokens, day)
                self._store.bump_counter(f"{C_TOKENS}:{module}", tokens, day)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[lifelike.budget] charge failed module={module}: {type(e).__name__}")

    # ------------------------------------------------------------ async wrapper
    async def guarded_generate(
        self,
        module: str,
        prompt: str,
        generate: Callable[[str, float], Awaitable[str]],
        timeout: float,
        *,
        usage_probe: Callable[[str], Awaitable[int | None]] | None = None,
        output_reserve: int = DEFAULT_OUTPUT_RESERVE,
    ) -> str:
        """预算内调用一次 ``generate(prompt, timeout)``；超额抛 CheapBudgetExceeded。

        usage_probe(module) 返回该模块累计 token（Iris token_stats），用调用前后差值作为
        实际用量；拿不到（None / 差值 ≤0）时用估算值。调用失败同样按估算输入计费
        （Provider 可能已计费），保证上限是硬的。
        """
        est_in = estimate_tokens(prompt)
        await asyncio.to_thread(self.reserve, module, est_in + max(0, int(output_reserve)))
        before = await self._probe(usage_probe, module)
        raw: str | None = None
        try:
            raw = await generate(prompt, timeout)
            return raw
        finally:
            after = await self._probe(usage_probe, module)
            actual = (after - before) if (before is not None and after is not None) else 0
            if actual <= 0:
                actual = est_in + (estimate_tokens(raw) if raw else 0)
            await asyncio.to_thread(self.charge, module, actual)

    @staticmethod
    async def _probe(
        probe: Callable[[str], Awaitable[int | None]] | None, module: str
    ) -> int | None:
        if probe is None:
            return None
        try:
            val = await probe(module)
            return None if val is None else int(val)
        except Exception:  # noqa: BLE001
            return None
