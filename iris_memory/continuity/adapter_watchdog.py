"""
Iris Chat Memory - 适配器失联心跳告警（lifelike P0-2，ROADMAP G3）

Telethon 适配器遇到不可重试的致命错误时，``run()`` 直接退出，AstrBot 只记一条
日志、不重启适配器 → 进程存活但她"静默失联"。本任务定期探测：

- ``platform_getter()`` → 平台实例；有 ``health_snapshot()``（适配器 P0-2 补丁）时读取
  适配器自报状态，``state == "fatal"`` 视为确定失联（不必等阈值）；
- ``inst.get_client()`` 为空或 ``client.is_connected()`` 为假 → 断开。

断开持续超过阈值（HiddenConfig ``lifelike_adapter_down_threshold_s``，默认 300s）→
**恰好一次** ERROR 日志 + ``counters.adapter_down`` + health 文件写 ``down``；恢复后
INFO 一行 + health 文件写 ``ok``（不补发任何消息）。适配器自报 ``stopped``（主人在
WebUI 主动停用）时只记 INFO、health 写 ``stopped``，不报 ERROR。在线时
``adapter_status.json`` 只在状态变化时写，``last_ok`` 至少每 5 分钟刷新一次。

告警渠道只用日志与本机文件（``<data_dir>/health/adapter_status.json`` 与
``<data_dir>/health/last_ok``），供主人自己的监控或 healthcheck 读取。
**不自动重启、不发 Telegram 消息。** 开关 ``lifelike_adapter_watchdog``（默认 true，
每轮现查；关闭时不探测、不写文件）。日志只含状态/原因 id/秒数，不含聊天正文。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from astrbot.api import logger

SWITCH_KEY = "lifelike_adapter_watchdog"
THRESHOLD_KEY = "lifelike_adapter_down_threshold_s"
PERIOD_KEY = "lifelike_adapter_watchdog_period_s"
DEFAULT_THRESHOLD_S = 300
DEFAULT_PERIOD_S = 60
MIN_THRESHOLD_S = 60
MIN_PERIOD_S = 10
COUNTER_NAME = "adapter_down"
HEALTH_DIRNAME = "health"
STATUS_FILENAME = "adapter_status.json"
LAST_OK_FILENAME = "last_ok"

STATE_OK = "ok"
STATE_DOWN = "down"
STATE_PENDING = "pending"  # 已断开但未到阈值
STATE_DISABLED = "disabled"
STATE_STOPPED = "stopped"  # 适配器自报 stopped（WebUI 主动停用 / terminate），不算失联
# 在线时 adapter_status.json 只在状态变化时写；last_ok 至少隔这么久刷新一次（审查：减少写盘）
LAST_OK_MIN_INTERVAL_S = 300

_LOG = "[lifelike.watchdog]"


def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on", "y", "t"}:
            return True
        if v in {"0", "false", "no", "off", "n", "f", ""}:
            return False
    return default


def _to_int(value: Any, default: int, lo: int) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return max(lo, int(float(value)))
    except (TypeError, ValueError, OverflowError):
        return default


class AdapterWatchdog:
    def __init__(
        self,
        platform_getter: Callable[[], Any],
        config_get: Callable[[str, Any], Any],
        health_dir: Path | str | None,
        counter_bump: Callable[[str], Awaitable[Any]] | None = None,
        clock: Callable[[], float] | None = None,
        *,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._platform_getter = platform_getter
        self._config_get = config_get
        self._health_dir = Path(health_dir) if health_dir is not None else None
        self._counter_bump = counter_bump
        self._clock = clock or time.time
        self._sleep = sleep
        self._task: asyncio.Task | None = None
        self._stopped = False
        self._down_since: float | None = None
        self._alerted = False
        self._state = ""
        self._last_ok_written = 0.0

    # ------------------------------------------------------------ config

    def _cfg(self, key: str, default: Any) -> Any:
        try:
            return self._config_get(key, default)
        except Exception:
            return default

    def enabled(self) -> bool:
        return _to_bool(self._cfg(SWITCH_KEY, True), True)

    def threshold_s(self) -> int:
        return _to_int(self._cfg(THRESHOLD_KEY, DEFAULT_THRESHOLD_S), DEFAULT_THRESHOLD_S, MIN_THRESHOLD_S)

    def period_s(self) -> int:
        return _to_int(self._cfg(PERIOD_KEY, DEFAULT_PERIOD_S), DEFAULT_PERIOD_S, MIN_PERIOD_S)

    # ------------------------------------------------------------ lifecycle

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def state(self) -> str:
        return self._state

    def start(self) -> None:
        if self.running:
            return
        self._stopped = False
        self._task = asyncio.create_task(self._loop(), name="iris-lifelike-adapter-watchdog")

    async def stop(self) -> None:
        self._stopped = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _loop(self) -> None:
        while not self._stopped:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"{_LOG} check failed: {type(e).__name__}")
            await self._sleep(float(self.period_s()))

    # ------------------------------------------------------------ probe

    def probe(self) -> tuple[bool, str, dict[str, Any]]:
        """返回 (是否在线, 原因 id, 适配器自报快照)。原因 id 只含 ASCII 标识。"""
        try:
            inst = self._platform_getter()
        except Exception as e:
            return False, f"platform_error:{type(e).__name__}", {}
        if inst is None:
            return False, "no_platform", {}
        snapshot: dict[str, Any] = {}
        snap_fn = getattr(inst, "health_snapshot", None)
        if callable(snap_fn):
            try:
                raw = snap_fn()
                if isinstance(raw, dict):
                    snapshot = raw
            except Exception:
                snapshot = {}
        if snapshot.get("state") == "fatal":
            return False, "adapter_fatal", snapshot
        if snapshot.get("state") == STATE_STOPPED:
            return False, "adapter_stopped", snapshot
        try:
            get_client = getattr(inst, "get_client", None)
            client = get_client() if callable(get_client) else None
        except Exception as e:
            return False, f"client_error:{type(e).__name__}", snapshot
        if client is None:
            return False, "no_client", snapshot
        try:
            is_connected = getattr(client, "is_connected", None)
            connected = bool(is_connected()) if callable(is_connected) else True
        except Exception as e:
            return False, f"is_connected_error:{type(e).__name__}", snapshot
        if not connected:
            return False, "disconnected", snapshot
        return True, "ok", snapshot

    async def check_once(self) -> str:
        """一次检查，返回当前状态（ok/pending/down/stopped/disabled）。"""
        if not self.enabled():
            self._down_since = None
            self._alerted = False
            self._state = STATE_DISABLED
            return STATE_DISABLED
        now = float(self._clock())
        up, reason, snapshot = self.probe()
        inbound_age = self._inbound_age(snapshot, now)

        if up:
            if self._alerted:
                down_for = now - (self._down_since or now)
                logger.info(f"{_LOG} adapter recovered down_for={down_for:.0f}s")
            changed = self._state != STATE_OK
            self._down_since = None
            self._alerted = False
            self._state = STATE_OK
            if changed:
                self._write_status(STATE_OK, now, now, reason, inbound_age)
            if changed or now - self._last_ok_written >= LAST_OK_MIN_INTERVAL_S:
                self._write_last_ok(now)
                self._last_ok_written = now
            return STATE_OK

        if reason == "adapter_stopped":
            # 主人主动停用适配器：INFO 一次 + health 文件 stopped，不报 ERROR（审查 L4）
            if self._state != STATE_STOPPED:
                logger.info(f"{_LOG} adapter stopped (deliberately); not alerting")
                self._write_status(STATE_STOPPED, now, now, reason, inbound_age)
            self._down_since = None
            self._alerted = False
            self._state = STATE_STOPPED
            return STATE_STOPPED

        if self._down_since is None:
            self._down_since = now
            logger.debug(f"{_LOG} adapter looks down reason={reason}")
        down_for = now - self._down_since
        fatal = reason == "adapter_fatal"
        if not self._alerted and (fatal or down_for >= self.threshold_s()):
            self._alerted = True
            fatal_reason = str(snapshot.get("reason") or "") if fatal else ""
            logger.error(
                f"{_LOG} adapter DOWN reason={reason}"
                + (f" adapter_reason={fatal_reason}" if fatal_reason else "")
                + f" down_for={down_for:.0f}s"
                + (f" last_inbound_age={inbound_age:.0f}s" if inbound_age is not None else "")
                + "; no auto-restart, check the adapter / restart the container manually"
            )
            await self._bump_counter()
        if self._alerted:
            self._state = STATE_DOWN
            self._write_status(STATE_DOWN, now, self._down_since, reason, inbound_age)
        else:
            self._state = STATE_PENDING
        return self._state

    @staticmethod
    def _inbound_age(snapshot: dict[str, Any], now: float) -> float | None:
        try:
            ts = snapshot.get("last_inbound_at")
            if ts is None:
                return None
            return max(0.0, now - float(ts))
        except Exception:
            return None

    async def _bump_counter(self) -> None:
        if self._counter_bump is None:
            return
        try:
            await self._counter_bump(COUNTER_NAME)
        except Exception as e:
            logger.warning(f"{_LOG} counter bump failed: {type(e).__name__}")

    # ------------------------------------------------------------ health files

    def _atomic_write(self, name: str, text: str) -> None:
        if self._health_dir is None:
            return
        try:
            self._health_dir.mkdir(parents=True, exist_ok=True)
            path = self._health_dir / name
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        except Exception as e:
            logger.warning(f"{_LOG} health file write failed: {type(e).__name__}")

    def _write_status(
        self, state: str, now: float, since: float, reason: str, inbound_age: float | None
    ) -> None:
        payload = {
            "state": state,
            "since": round(since, 3),
            "checked_at": round(now, 3),
            "reason": reason,
            "last_inbound_age_s": None if inbound_age is None else round(inbound_age, 1),
        }
        self._atomic_write(STATUS_FILENAME, json.dumps(payload, ensure_ascii=True))

    def _write_last_ok(self, now: float) -> None:
        self._atomic_write(LAST_OK_FILENAME, f"{now:.3f}\n")
