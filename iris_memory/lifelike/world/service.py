"""WorldService：生活系统门面（设计 §4、§9.10），由 ContinuityService 持有。

开关（HiddenConfig 平铺键）：

- ``lifelike_enable``（总开关，ROADMAP §2.1）：false → 本模块视为 off；
- ``lifelike_world_mode``：``off`` 不建表、不读写、不起后台任务（与改造前一致）；
  ``shadow`` 生成计划、推进状态、核对自述并记录（``life_claims`` applied=0），**不注入 prompt**、
  不补登事件；``on`` 再注入 ``<她的今天>`` 并落实核对结论；
- ``lifelike_world_claim_audit``（默认 true）：false → 不做回复后核对；
- ``lifelike_world_plan_llm``（默认 true）：false → DayPlanner 只用模板，不调模型。

代码层回退值（配置读不到时）为 enable=false / mode=off；HiddenConfig 默认见 defaults.py。

后台：一个低频循环（默认 5 分钟一次 tick；启动时先 catch_up 补结算）。连续 5 次失败
熔断 1 小时（WARN 一次）。所有公共入口异常都回退到"什么都不做"，日志不含正文。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any, Optional

from astrbot.api import logger

from . import models as wm
from .auditor import ClaimAuditor
from .planner import DayPlanner, WorldClock
from .projection import CONFLICT_WINDOW_S, render_projection
from .store import WorldStore

LOG = "[lifelike.world]"
KEY_ENABLE = "lifelike_enable"
KEY_MODE = "lifelike_world_mode"
KEY_CLAIM = "lifelike_world_claim_audit"
KEY_PLAN_LLM = "lifelike_world_plan_llm"
MODES = ("off", "shadow", "on")
TICK_S = 300.0
BREAKER_FAILS = 5
BREAKER_PAUSE_S = 3600.0
MAX_PENDING_AUDITS = 4  # 排队中的自述核对上限（超出直接跳过，核对是旁路）


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "on", "yes"):
            return True
        if v in ("0", "false", "off", "no"):
            return False
    return default


class WorldService:
    def __init__(
        self,
        base_store: Any,
        config_get: Callable[[str, Any], Any],
        *,
        llm: Optional[Callable[[str, float], Awaitable[str]]] = None,
        budget: Any = None,
        usage_probe: Optional[Callable[[str], Awaitable[Optional[int]]]] = None,
        provider_name: Callable[[], Optional[str]] = lambda: None,
        is_owner_scope: Callable[[str], bool] = lambda s: False,
        clock: Callable[[], float] = time.time,
        tick_s: float = TICK_S,
    ) -> None:
        self._base = base_store
        self._get = config_get
        self._clock = clock
        self._is_owner_scope = is_owner_scope
        self._tick_s = float(tick_s)
        self.store = WorldStore(base_store, clock)
        self.planner = DayPlanner(
            self.store, llm, budget,
            counters_get=self._counters,
            llm_enabled=lambda: _as_bool(self._get(KEY_PLAN_LLM, True), True),
            provider_name=provider_name,
            usage_probe=usage_probe,
        )
        self.clock = WorldClock(self.store, self.planner)
        self.auditor = ClaimAuditor(
            self.store, llm, budget,
            counters_get=self._counters,
            bump=lambda name, n: self._base.bump_counter(name, n),
            usage_probe=usage_probe,
        )
        self._task: Optional[asyncio.Task] = None
        self._pending: set[asyncio.Task] = set()
        self._fails = 0
        self._paused_until = 0.0
        self._schema_ok = False

    # ------------------------------------------------------------ mode
    def mode(self) -> str:
        try:
            if not _as_bool(self._get(KEY_ENABLE, False), False):
                return "off"
            v = str(self._get(KEY_MODE, "off") or "").strip().lower()
        except Exception:  # noqa: BLE001
            return "off"
        return v if v in MODES else "off"

    def claim_audit_enabled(self) -> bool:
        try:
            return _as_bool(self._get(KEY_CLAIM, True), True)
        except Exception:  # noqa: BLE001
            return False

    def _counters(self) -> dict[str, int]:
        return self._base.get_counters(wm.local_day(float(self._clock())))

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _paused(self) -> bool:
        return float(self._clock()) < self._paused_until

    def _fail(self, where: str, e: BaseException) -> None:
        self._fails += 1
        logger.warning(f"{LOG} {where} failed: {type(e).__name__} fails={self._fails}")
        if self._fails >= BREAKER_FAILS:
            self._paused_until = float(self._clock()) + BREAKER_PAUSE_S
            self._fails = 0
            logger.warning(f"{LOG} paused_error for {int(BREAKER_PAUSE_S)}s")

    async def _ensure_schema(self) -> bool:
        if self._schema_ok:
            return True
        try:
            await asyncio.to_thread(self.store.ensure_schema)
            self._schema_ok = True
        except Exception as e:  # noqa: BLE001
            self._fail("schema", e)
        return self._schema_ok

    # ------------------------------------------------------------ lifecycle
    async def ensure_started(self) -> None:
        """mode≠off 时启动后台循环（幂等；维护周期也会调用，支持运行时切换）。"""
        if self.mode() == "off" or self.running:
            return
        if not await self._ensure_schema():
            return
        self._task = asyncio.create_task(self._loop(), name="lifelike_world")
        logger.info(f"{LOG} started mode={self.mode()}")

    async def stop(self) -> None:
        task, self._task = self._task, None
        for t in [task, *self._pending]:
            if t is None:
                continue
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._pending.clear()

    async def _loop(self) -> None:
        while True:
            if self.mode() == "off":
                logger.info(f"{LOG} stopped: mode=off")
                return
            await self.tick_once()
            await asyncio.sleep(self._tick_s)

    async def tick_once(self, now: Optional[float] = None) -> list[str]:
        if self.mode() == "off" or self._paused() or not await self._ensure_schema():
            return []
        try:
            changed = await self.clock.tick(float(self._clock() if now is None else now))
            self._fails = 0
            return changed
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self._fail("tick", e)
            return []

    # ------------------------------------------------------------ projection
    async def render(self, scope_key: str, *, owner_private: bool, now: Optional[float] = None) -> str:
        """``<她的今天>``；只有 mode=on 才返回内容（shadow/off 返回 ''，prompt 不变）。"""
        if self.mode() != "on" or self._paused():
            return ""
        try:
            if not await self._ensure_schema():
                return ""
            now = float(self._clock() if now is None else now)
            day = wm.local_day(now)
            events = await asyncio.to_thread(
                self.store.list_events, [wm.shift_day(day, -1), day], (), False, 100
            )
            if not events:
                return ""
            conflicts: list[str] = []
            ids: list[str] = []
            if owner_private:
                rows = await asyncio.to_thread(
                    self.store.claims_since, now - CONFLICT_WINDOW_S, wm.RES_CONFLICT
                )
                pending = [r for r in rows if r.get("applied") and r.get("corrected_at") is None
                           and r.get("scope_key") == scope_key]
                if pending:
                    conflicts = [str(pending[-1]["span"])]
                    ids = [str(pending[-1]["claim_id"])]
            text = render_projection(events, now, owner_private=owner_private, conflicts=conflicts)
            if ids:
                await asyncio.to_thread(self.store.mark_claim_rendered, ids)
            return text
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{LOG} render failed: {type(e).__name__}")
            return ""

    # ------------------------------------------------------------ claim audit
    def schedule_audit(self, scope_key: str, outbound_id: Any, text: str, source: str) -> bool:
        """发出后接入点（被动/主动）：只核对主人私聊；后台执行，不阻塞发送。"""
        try:
            mode = self.mode()
            if mode == "off" or not self.claim_audit_enabled() or self._paused():
                return False
            if not text or outbound_id is None or not self._is_owner_scope(scope_key):
                return False
            oid = int(outbound_id)
        except Exception:  # noqa: BLE001
            return False
        if len(self._pending) >= MAX_PENDING_AUDITS:
            logger.debug(f"{LOG} audit skipped: pending={len(self._pending)}")
            return False
        task = asyncio.create_task(self._audit(scope_key, oid, str(text), source, mode == "on"))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return True

    async def _audit(self, scope_key: str, oid: int, text: str, source: str, apply: bool) -> None:
        try:
            if not await self._ensure_schema():
                return
            await self.auditor.audit(oid, text, scope_key, float(self._clock()), apply=apply)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self._fail(f"audit[{source}]", e)

    async def drain(self) -> None:
        """测试 / 关停用：等待已排队的核对完成"""
        while self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    # ------------------------------------------------------------ owner commands
    async def today_text(self, now: Optional[float] = None) -> str:
        """``/iris_life today``：给主人看的当天记录（不进 prompt、不写 L1）"""
        if self.mode() == "off":
            return "生活系统未开启（lifelike_enable / lifelike_world_mode）。"
        if not await self._ensure_schema():
            return "生活系统暂时不可用。"
        now = float(self._clock() if now is None else now)
        day = wm.local_day(now)
        info = await asyncio.to_thread(self.store.get_day, day)
        events = await asyncio.to_thread(self.store.list_events, [day], (), True, 100)
        claims = await asyncio.to_thread(self.store.claims_since, wm.day_start(day))
        status = {"planned": "打算", "in_progress": "进行中", "done": "做完了", "cancelled": "没做成",
                  "changed": "改了主意"}
        lines = [f"【她的今天 {day}】模式：{self.mode()}"]
        if info:
            lines.append(f"基调：{info.get('outline') or '-'}（{info.get('generator') or '-'}）")
        else:
            lines.append("今天的计划还没生成。")
        for e in events:
            mark = "（已撤回）" if e.retracted else ""
            when = wm.period_text(e.slot_start or e.due_at)
            out = f" → {e.outcome}" if e.outcome else ""
            lines.append(f"- [{e.event_id}] {when} {e.title}：{status.get(e.status, e.status)}{out}{mark}"
                         f"（{e.source}）")
        if claims:
            agg: dict[str, int] = {}
            for c in claims:
                agg[c["resolution"]] = agg.get(c["resolution"], 0) + 1
            lines.append("自述核对：" + "，".join(f"{k} {v}" for k, v in sorted(agg.items())))
        return "\n".join(lines)

    async def undo(self, event_id: str) -> str:
        if self.mode() == "off":
            return "生活系统未开启。"
        if not await self._ensure_schema():
            return "生活系统暂时不可用。"
        ok = await asyncio.to_thread(self.store.retract, event_id.strip(), "owner_cmd", "owner_undo")
        return "已撤回。" if ok else "没找到这个事件，或者已经撤回过了。"

    async def plan_now(self) -> str:
        if self.mode() == "off":
            return "生活系统未开启。"
        if not await self._ensure_schema():
            return "生活系统暂时不可用。"
        now = float(self._clock())
        status = await self.planner.ensure_day(wm.local_day(now), now)
        await self.tick_once(now)
        return "今天已经有计划了。" if status is None else f"已生成今天的计划（{status}）。"
