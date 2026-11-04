from __future__ import annotations

import asyncio
import random
import re
import time
from collections.abc import Awaitable, Callable

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.star import Context

from .activity_gate import (
    REASON_QUIET_HOURS,
    ActivityGateConfig,
    ActivitySnapshot,
    evaluate_activity_gate,
    in_minute_range,
    seconds_until_range_end,
)
from . import freedom as fr
from . import drive as dv
from . import group_freedom as gfm
from .config import ConfigManager
from .decision import (
    INPUT_SAFETY_COOLDOWN_MINUTES,
    DecisionCore,
    DecisionRequest,
)
from .parser import Decision
from .perception import ContextPackager, SlidingWindow, WindowMessage
from .prompts import PRIVATE_SPEAK_HINT_INITIATE, SPEAK_HINTS, SPEAK_SPLIT_NOTE
from .signals import SignalGate
from .state import GroupState, StateManager
from .stats import StatsCollector
from .time_hint import resolve_datetime_reminder, wrap_system_reminder
from ..lifelike.guards.numeric import guard_text as guard_numeric
from ..utils.log_redact import text_for_log
from ..llm_modules import PROACTIVE_REPLY_INITIATE

# 发起评估被 LLM 否决后的重试间隔（秒），仅内存记录
_SKIP_RETRY_SECONDS = 30 * 60
# 接入连续性桥接时：分段上限与重启后过期候选的重新抽样区间（秒）
_MAX_SPEECH_PARTS = 3
_RESTART_RESAMPLE_SECONDS = (30.0, 180.0)
# 活人感 P0-8：账本快照缓存时长（秒）与熔断参数（连续 5 次失败 → 暂停 1 小时）
_FLOOR_CACHE_SECONDS = 600.0
_FLOOR_FAIL_LIMIT = 5
_FLOOR_PAUSE_SECONDS = 3600.0
# 未接入桥接时的独立禁发频道（与 continuity.proactive_bridge.DEFAULT_FORBIDDEN_CHATS 一致）
# 内置禁发列表为空：主人频道等请配置在 continuity.owner_channel_ids / proactive.silent_sessions
_LEGACY_FORBIDDEN_CHATS: frozenset = frozenset()


def _proactive_style_text(group_id: str, care_category: str = "") -> str:
    """P1-4：主动消息的 style 段（固定"中"档，群聊再取群上限）；关闭 / 异常时为空。

    care_category 非空（群→私聊关心候选）→ 淡档、不卖萌、不给示例（不和关心语气打架）。"""
    try:
        from ..style.selector import get_selector

        return get_selector().build_proactive(
            str(group_id), is_group=not str(group_id).startswith("private:"),
            care_category=care_category,
        )
    except Exception as e:
        logger.warning("[lifelike.style_inject] proactive style skipped: %s", type(e).__name__)
        return ""


def _proactive_man_hits(text: str, *, always: bool = False) -> list[str]:
    """P1-4：主动草稿命中 StyleGuard 操纵句（MAN-*）的规则 id；开关关闭 / 异常时为空（= 照常发送）。

    always=True（U8 自由主动）：反情绪勒索拦截是自由模式保留的安全网，不随口吻开关回滚而失效。"""
    try:
        from ..style.selector import proactive_style_enabled

        if not always and not proactive_style_enabled():
            return []
        from ..lifelike.style.guard import proactive_man_hits

        hard, soft = proactive_man_hits(text or "")
        if soft:
            logger.info("[lifelike.style_inject] proactive man log_only rules=%s", ",".join(soft))
        return hard
    except Exception as e:
        logger.warning("[lifelike.style_inject] proactive MAN check skipped: %s", type(e).__name__)
        return []


def _is_probe(text: str) -> bool:
    try:
        from ..lifelike.initiative.coordinator import is_probe

        return bool(is_probe(text))
    except Exception:
        return False


def _strip_private_for_group(group_id: str, text: str) -> str:
    """U9 群内主动：删掉"私聊过 / 你私下跟我说过"类句子（PRIV-02 同一规则）；异常 → 原文。"""
    try:
        from ..lifelike.care.rules import strip_private_mentions

        new, removed = strip_private_mentions(text or "")
        if removed:
            logger.info("[lifelike.group_freedom] scope=%s PRIV-02 sentences_removed=%d", group_id, removed)
        return new
    except Exception as e:
        logger.warning("[lifelike.group_freedom] PRIV-02 check skipped: %s", type(e).__name__)
        return text


def _legacy_chat_variants(chat_id) -> set[str]:
    s = str(chat_id or "").strip()
    if not s:
        return set()
    out = {s}
    if s.startswith("-100") and len(s) > 4:
        out.add(s[4:])
    elif s.startswith("-"):
        out.add(s[1:])
    else:
        out.add("-100" + s)
        out.add("-" + s)
    return out


def _is_silent_or_forbidden(chat_id, silent_sessions) -> bool:
    try:
        from ..continuity.service import is_silent_or_forbidden

        return bool(is_silent_or_forbidden(chat_id, silent_sessions))
    except Exception:
        pass
    ids = _legacy_chat_variants(chat_id)
    if not ids:
        return False
    blocked = {str(x).strip() for x in (silent_sessions or ()) if str(x).strip()}
    if str(chat_id).strip() in blocked:
        return True
    return any(ids & _legacy_chat_variants(c) for c in _LEGACY_FORBIDDEN_CHATS | blocked)


def _topic_matches(topic: str, avoided: list[str]) -> bool:
    """决策话题与「不要主动提起」话题规范化后相同或互相包含（≥2 字）"""
    from ..continuity.models import norm_topic

    t = norm_topic(topic)
    if len(t) < 2:
        return False
    for a in avoided or ():
        n = norm_topic(a)
        if len(n) >= 2 and (n == t or n in t or t in n):
            return True
    return False


def _avoid_block(topics: list[str]) -> str:
    if not topics:
        return ""
    lines = "\n".join(f"- {t}" for t in topics)
    return f"【不要主动提起】\n以下话题对方已拒绝、已完成或已取消，不要主动提起，也不要换个说法绕回去：\n{lines}"


def split_speech_parts(text: str, max_parts: int = _MAX_SPEECH_PARTS) -> list[str]:
    """按空行切分为不超过 max_parts 段；多出的段并入最后一段。"""
    chunks = [c.strip() for c in re.split(r"\n\s*\n", text or "") if c.strip()]
    if len(chunks) <= max_parts:
        return chunks
    return chunks[: max_parts - 1] + ["\n\n".join(chunks[max_parts - 1:])]


def _record_initiate_send(group_id: str, text: str, success: bool, error: str = "") -> None:
    """写入统一运行日志（proactive 类型，主动发起直发结果）。"""
    try:
        from ..core.run_log import get_run_log_manager

        get_run_log_manager().record(
            "proactive",
            f"主动发起{'已发送' if success else '发送失败'}",
            success=success,
            group_id=group_id,
            wake="timer",
            motive="initiate",
            stage="send",
            message=text,
            error=error,
        )
    except Exception:
        pass


class ProactiveEngine:
    """主动发起引擎：按群独立预约，在自然空档中概率评估并直发新话题。

    直发通路不经过 AstrBot 事件管线（after_message_sent 等钩子不会触发），
    因此所有记账（入窗 / 锚点 / pending / 统计）都在此手动完成。
    """

    def __init__(
        self,
        context: Context,
        config: ConfigManager,
        state: StateManager,
        window: SlidingWindow,
        signals: SignalGate,
        decision_core: DecisionCore,
        stats: StatsCollector,
        *,
        llm_manager,
        packager: ContextPackager,
        umo_get: Callable[[str], str | None],
        is_busy: Callable[[str], bool],
        self_id_get: Callable[[], str],
        save_fn: Callable[[], Awaitable[None]],
        on_initiate_sent: Callable[[str, str], Awaitable[None]] | None = None,
        text_transform: Callable[[str], str] | None = None,
        profile_get: Callable[[str, str], Awaitable[str]] | None = None,
        bridge=None,
        group_freedom: "gfm.GroupFreedomHub | None" = None,
        sticker_block: Callable[[str, str], Awaitable[str]] | None = None,
        sticker_take: Callable[[str, str, str], Awaitable[str]] | None = None,
    ) -> None:
        self._context = context
        self._config = config
        self._state = state
        self._window = window
        self._signals = signals
        self._core = decision_core
        self._llm_manager = llm_manager
        self._stats = stats
        self._packager = packager
        self._umo_get = umo_get
        self._is_busy = is_busy
        self._self_id_get = self_id_get
        self._save_fn = save_fn
        self._on_initiate_sent = on_initiate_sent
        self._text_transform = text_transform
        self._profile_get = profile_get
        # U19：主动消息配贴纸（None / 开关 off 时为空操作，与原行为一致）
        self._sticker_block = sticker_block
        self._sticker_take = sticker_take
        # 连续性桥接（iris_memory.continuity.proactive_bridge.ProactiveBridge）；
        # None 时完全保持原有行为（context.send_message 直发）。
        self._bridge = bridge
        if bridge is not None:
            # 私聊暂停只能由本人新私聊消息或管理员重置解除，不按 24h 自动清零
            state.time_reset_private = False
        self._task: asyncio.Task | None = None
        self._wake_event = asyncio.Event()
        self._initiating: set[str] = set()
        self._pool_push_ready: dict[str, bool] = {}
        self._skip_retry_after: dict[str, float] = {}
        self._avoid_cache: dict[str, list[str]] = {}
        # 活人感 P0-8 主动安全底线（开关关闭 / 无桥接时这些状态不会被使用）
        self._floor_cache: dict[str, tuple[float, ActivitySnapshot]] = {}
        self._floor_failures = 0
        self._floor_paused_until = 0.0
        self._floor_logged: dict[str, str] = {}
        # 窗口为空时由账本推得的最后互动时间（scope → ts）
        self._empty_window_hint: dict[str, float] = {}
        # 活人感 U8 自由主动（开关关闭时不会被使用）
        self._freedom_logged: dict[str, str] = {}
        self._breaker_tripped: set[str] = set()
        # 活人感 U9 群聊自由搭话（None / 开关关闭时不会被使用）
        self._gf = group_freedom
        self._gf_logged: dict[str, str] = {}

    def _legacy_target_refusal(self, group_id: str) -> str:
        """未接入桥接时的发送目标硬拦截（禁发频道 / silent_sessions / 私聊白名单）。"""
        scope = str(group_id or "").strip()
        if not scope:
            return "目标会话无效"
        try:
            silent = list(getattr(self._config, "silent_sessions", None) or [])
        except Exception:
            silent = []
        if scope.startswith("private:"):
            uid = scope.split(":", 1)[1].strip()
            try:
                allowed = {str(x).strip() for x in self._config.private_whitelist}
            except Exception:
                allowed = set()
            if not uid or uid not in allowed:
                return "私聊对象不在白名单"
            if _is_silent_or_forbidden(uid, silent):
                return "目标会话禁止发言"
            return ""
        peer = scope.partition("#")[0].strip()
        if scope in {str(x).strip() for x in silent} or _is_silent_or_forbidden(peer, silent):
            return "目标会话禁止发言"
        return ""

    # ---- 活人感 P0-8：主动安全底线 ----

    def _floor_cfg(self) -> ActivityGateConfig:
        try:
            cfg = self._config.activity_gate_config
        except Exception:
            return ActivityGateConfig()
        return cfg if isinstance(cfg, ActivityGateConfig) else ActivityGateConfig()

    def _lifelike_quiet(self, now: float | None = None, cfg: ActivityGateConfig | None = None) -> bool:
        rng = (cfg or self._floor_cfg()).quiet_hours
        if rng is None:
            return False
        t = time.localtime(time.time() if now is None else now)
        return in_minute_range(t.tm_hour * 60 + t.tm_min, rng)

    def _floor_scope(self, group_id: str) -> bool:
        """活动窗口判定 / 空窗判定只作用于接入桥接的主人私聊。"""
        bridge = self._bridge
        if bridge is None or not str(group_id).startswith("private:"):
            return False
        checker = getattr(bridge, "is_owner_private_scope", None)
        if checker is None:
            return False
        try:
            return bool(checker(group_id))
        except Exception:
            return False

    async def _activity_snapshot(
        self, group_id: str, cfg: ActivityGateConfig, now: float
    ) -> ActivitySnapshot | None:
        """读账本快照（10 分钟缓存）；失败返回 None（调用方回退旧行为），连续失败熔断 1 小时。"""
        cached = self._floor_cache.get(group_id)
        if cached is not None and now - cached[0] < _FLOOR_CACHE_SECONDS:
            return cached[1]
        if now < self._floor_paused_until:
            return None
        fetch = getattr(self._bridge, "activity_snapshot", None)
        if fetch is None:
            return None
        try:
            snap = await fetch(group_id, cfg.lookback_days, with_history=cfg.activity_gate)
        except Exception as e:
            self._floor_failures += 1
            if self._floor_failures >= _FLOOR_FAIL_LIMIT:
                self._floor_failures = 0
                self._floor_paused_until = now + _FLOOR_PAUSE_SECONDS
                logger.warning(
                    "[lifelike.proactive] snapshot failed %d times, paused_error 1h (last=%s)",
                    _FLOOR_FAIL_LIMIT, type(e).__name__,
                )
            else:
                logger.warning(
                    "[lifelike.proactive] scope=%s snapshot failed: %s", group_id, type(e).__name__,
                )
            return None
        self._floor_failures = 0
        self._floor_cache[group_id] = (now, snap)
        return snap

    def _last_activity(self, group_id: str, messages, snap: ActivitySnapshot | None) -> float | None:
        vals = [self._state.get_state(group_id).last_human_message_at]
        try:
            self_id = str(self._self_id_get() or "")
        except Exception:
            self_id = ""
        for msg in reversed(messages or ()):
            # 只认对方的消息：她自己发出的（含主动）不算"互动"
            if (self_id and str(msg.sender_id) == self_id) or msg.sender_name == "我":
                continue
            vals.append(msg.timestamp)
            break
        if snap is not None and snap.last_interaction is not None:
            vals.append(float(snap.last_interaction))
        best = max(vals)
        return best if best > 0 else None

    async def _refresh_empty_window_hint(self, group_id: str, now: float) -> None:
        """空窗判定：主人私聊窗口为空时，从水位 + 出站账本求最后互动时间。"""
        cfg = self._floor_cfg()
        # 活人感 U8：自由主动时空窗也交给她判断（最后互动时间作为事实给出）
        allow = cfg.empty_window or self._freedom_on(group_id)
        self._pool_push_ready.pop(group_id, None)
        if not allow and self._initiative_on() and self._pool_empty_window_on():
            # 活人感 P1-8：候选池里有通过闸门的可推送候选（例如群→私聊关心）时，
            # 即使空窗判定开关关闭也允许调度——这时开口有具体理由，不是"无上下文的主动"
            allow = self._floor_scope(group_id) and await self._pool_push_pending(group_id, now)
            if allow:
                self._pool_push_ready[group_id] = True
        if not allow or not self._floor_scope(group_id):
            self._empty_window_hint.pop(group_id, None)
            return
        snap = await self._activity_snapshot(group_id, cfg, now)
        last = self._last_activity(group_id, None, snap)
        if last is None:
            self._empty_window_hint.pop(group_id, None)
        else:
            self._empty_window_hint[group_id] = last

    def _empty_window_allowed(self, group_id: str) -> float | None:
        """窗口为空但空窗判定可用时返回最后互动时间，否则 None（= 现网：窗口为空不调度）。"""
        hint = self._empty_window_hint.get(group_id)
        if hint is None:
            return None
        if (
            not self._floor_cfg().empty_window
            and not self._pool_push_ready.get(group_id)
            and not self._freedom_on(group_id)
        ):
            return None
        return hint

    def _pool_empty_window_on(self) -> bool:
        try:
            return bool(self._config.pool_empty_window)
        except Exception:
            return False

    def _initiative_on(self) -> bool:
        try:
            return bool(self._config.initiative_on)
        except Exception:
            return False

    async def _pool_urgent(self, group_id: str, now: float) -> bool:
        fetch = getattr(self._bridge, "has_urgent_candidate", None)
        if fetch is None:
            return False
        try:
            return bool(await fetch(group_id, now))
        except Exception:
            return False

    async def _pool_push_pending(self, group_id: str, now: float) -> bool:
        fetch = getattr(self._bridge, "has_push_candidate", None)
        if fetch is None:
            return False
        try:
            return bool(await fetch(group_id, now))
        except Exception as e:
            logger.warning("[lifelike.initiative] scope=%s pool check failed: %s", group_id, type(e).__name__)
            return False

    async def _review_pool_speech(
        self, group_id: str, umo: str, provider_id: str, decision: Decision, turn_id: str, text: str
    ) -> tuple[str, str]:
        """活人感 P1-8/P1-12：候选池候选的发送前检查；不通过 → 重新生成一次，仍不通过 → 拦截。
        返回 (文本, 拦截原因)；原因为空表示可以发送。"""
        review = getattr(self._bridge, "review_speech", None)
        if review is None:
            return text, ""
        limit = self._config.proactive_max_message_len
        ok, reason = await review(turn_id, text[:limit])
        if ok:
            return text, ""
        logger.info("[lifelike.initiative] turn=%s review=%s regenerate=1", turn_id, reason)
        again = (await self._generate_speech(group_id, umo, provider_id, decision) or "").strip()
        if not again:
            return text, reason or "regen_failed"
        ok, reason2 = await review(turn_id, again[:limit])
        if ok:
            return again, ""
        logger.info("[lifelike.initiative] turn=%s review=%s blocked=1", turn_id, reason2)
        return again, reason2 or reason or "review_failed"

    def _has_schedule_context(self, group_id: str) -> bool:
        if self._window.get_messages(group_id):
            return True
        return self._empty_window_allowed(group_id) is not None

    async def _has_due_reminder(self, group_id: str, now: float) -> bool:
        """桥接候选里是否有已到点（not_before 已过）且 remind_requested 的事项。失败 → False。"""
        fetch = getattr(self._bridge, "candidates", None)
        if fetch is None:
            return False
        try:
            cands = await fetch(group_id, now)
        except Exception as e:
            logger.warning("[lifelike.proactive] scope=%s reminder check failed: %s", group_id, type(e).__name__)
            return False
        for item in cands or ():
            try:
                if item.get("remind_requested") and item.get("not_before") is not None:
                    return True
            except Exception:
                continue
        return False

    async def _floor_verdict(self, group_id: str, messages) -> tuple[str, float | None]:
        """返回 (推迟原因, 空窗最后互动时间)。任何异常 → ("", None)，即现网行为。"""
        try:
            if self._freedom_on(group_id):
                # 活人感 U8：静音时段 / 活动窗口推迟对主人私聊失效（现在几点作为事实给她）
                return "", (None if messages else self._empty_window_allowed(group_id))
            cfg = self._floor_cfg()
            now = time.time()
            if self._lifelike_quiet(now, cfg):
                return REASON_QUIET_HOURS, None
            hint = None if messages else self._empty_window_allowed(group_id)
            if not cfg.activity_gate or not self._floor_scope(group_id):
                return "", hint
            if await self._has_due_reminder(group_id, now):
                # 主人明确要求的到点提醒不受活动窗口推迟（硬静音时段仍在上面生效；审查 M2）
                return "", hint
            if self._initiative_on() and await self._pool_urgent(group_id, now):
                # 活人感 P1-12：危机关心（源消息 30 分钟内，说明她此刻醒着）同样不受活动窗口推迟
                return "", hint
            snap = await self._activity_snapshot(group_id, cfg, now)
            if snap is None:
                return "", hint
            reason = evaluate_activity_gate(
                cfg,
                now=now,
                last_interaction=self._last_activity(group_id, messages, snap),
                owner_timestamps=snap.owner_timestamps,
            )
            return reason, hint
        except Exception as e:
            logger.warning("[lifelike.proactive] scope=%s verdict failed: %s", group_id, type(e).__name__)
            return "", None

    def _log_floor(self, group_id: str, reason: str) -> None:
        """推迟原因变化时记一行（不含正文）；同一原因不重复刷屏。"""
        prev = self._floor_logged.get(group_id, "")
        if reason == prev:
            return
        self._floor_logged[group_id] = reason
        if reason:
            logger.info("[lifelike.proactive] scope=%s op=defer reason=%s", group_id, reason)
        else:
            logger.info("[lifelike.proactive] scope=%s op=resume reason=%s", group_id, prev)

    # ---- 活人感 U8：主人私聊自由主动 ----

    def _freedom_cfg(self) -> fr.FreedomConfig:
        try:
            cfg = self._config.freedom_config
        except Exception:
            return fr.FreedomConfig()
        return cfg if isinstance(cfg, fr.FreedomConfig) else fr.FreedomConfig()

    def _freedom_on(self, group_id: str) -> bool:
        """自由主动只作用于接入桥接的主人私聊；群聊、无桥接、开关关闭 → False（= 73edc2c）。"""
        try:
            return bool(self._freedom_cfg().enabled) and self._floor_scope(group_id)
        except Exception:
            return False

    def _post_send_gap(self, group_id: str) -> float:
        """发起成功 / 结果不确定后，下一次评估最早的间隔（秒）。"""
        if self._freedom_on(group_id):
            return self._freedom_cfg().min_gap_s
        if self._gf_on(group_id):
            return self._gf_cfg().initiate_eval_s
        return self._config.proactive_min_interval * 60.0

    def _owner_last_at(self, group_id: str, messages, hint: float | None) -> float | None:
        last = self._last_activity(group_id, messages, None)
        if hint:
            last = max(last or 0.0, float(hint))
        return last or None

    def _freedom_block(
        self,
        group_id: str,
        *,
        now: float,
        messages,
        hint: float | None,
        check_cooldown: bool = True,
    ) -> str:
        """自由模式下仍生效的技术安全网（原因只进日志）；熔断首次触发记 ERROR。"""
        cfg = self._freedom_cfg()
        data = self._state.get_state(group_id)
        last_owner = self._owner_last_at(group_id, messages, hint)
        if fr.breaker_count(data.initiate_timestamps, now, last_owner) >= cfg.breaker_24h:
            if group_id not in self._breaker_tripped:
                self._breaker_tripped.add(group_id)
                logger.error(
                    "[lifelike.freedom] scope=%s op=breaker threshold_24h=%d: proactive halted until "
                    "the owner replies or the rolling window drains (possible runaway sending, please check)",
                    group_id, cfg.breaker_24h,
                )
        else:
            self._breaker_tripped.discard(group_id)
        cooldown_until = data.cooldown_until if data.state == GroupState.COOLDOWN else 0.0
        return fr.block_reason(
            cfg,
            now=now,
            cooldown_until=cooldown_until,
            hush_until=float(data.proactive_hush_until or 0.0),
            last_initiate_at=float(data.last_initiate_time or 0.0),
            initiate_timestamps=data.initiate_timestamps,
            last_owner_at=last_owner,
            has_context=bool(messages) or hint is not None,
            check_cooldown=check_cooldown,
        )

    def _log_freedom(self, group_id: str, reason: str) -> None:
        prev = self._freedom_logged.get(group_id, "")
        if reason == prev:
            return
        self._freedom_logged[group_id] = reason
        if reason:
            logger.info("[lifelike.freedom] scope=%s op=hold reason=%s", group_id, reason)
        else:
            logger.info("[lifelike.freedom] scope=%s op=evaluate prev=%s", group_id, prev)

    async def _owner_status(self, group_id: str) -> dict:
        fetch = getattr(self._bridge, "owner_status", None)
        if fetch is None:
            return {}
        try:
            return dict(await fetch(group_id) or {})
        except Exception as e:
            logger.warning("[lifelike.freedom] scope=%s owner_status failed: %s", group_id, type(e).__name__)
            return {}

    async def _relation_cooling(self, group_id: str) -> bool:
        """主人私聊是否处于关系冷静期（relation_mode≠on / 未接线 / 异常 → False = 73edc2c）。"""
        fetch = getattr(self._bridge, "relation_cooling", None)
        if fetch is None:
            return False
        try:
            return bool(await fetch(group_id))
        except Exception as e:
            logger.warning("[lifelike.freedom] scope=%s relation_cooling failed: %s", group_id, type(e).__name__)
            return False

    def _last_free_topic(self, group_id: str) -> str:
        try:
            recent = self._state.get_freedom_recent(group_id)
        except Exception:
            return ""
        return str(recent[-1][1] or "") if recent else ""

    async def _freedom_facts(self, group_id: str, messages, status: dict | None = None) -> str:
        """决策 <facts>：时间与是否回应等客观事实；任何异常 → 只含当前时间的最小事实块。"""
        now = time.time()
        try:
            data = self._state.get_state(group_id)
            hint = None if messages else self._empty_window_allowed(group_id)
            last_owner = self._owner_last_at(group_id, messages, hint)
            if status is None:
                status = await self._owner_status(group_id)
            last_init = float(data.last_initiate_time or 0.0)
            return fr.build_facts(
                now=now,
                last_owner_at=last_owner,
                last_initiate_at=last_init,
                owner_replied_after_initiate=bool(last_init > 0 and last_owner and last_owner > last_init),
                unanswered_streak=int(data.initiate_no_reply_streak or 0),
                initiated_24h=fr.rolling_count(data.initiate_timestamps, now),
                closure_ended_at=status.get("closure_ended_at"),
                busy_until=status.get("busy_until"),
                last_topic=self._last_free_topic(group_id),
                cooling=bool(status.get("cooling")),
            )
        except Exception as e:
            logger.warning("[lifelike.freedom] scope=%s facts failed: %s", group_id, type(e).__name__)
            return f"<facts>\n- 现在：{fr.fmt_now(now)}\n</facts>"

    def note_owner_text(self, group_id: str, text: str, timestamp: float | None = None) -> str:
        """主人私聊新消息正文（只做规则判定，不存正文）：叫停 / 解除自由主动。

        返回 "hush" | "unhush" | ""。开关关闭或非主人私聊时不做任何事。"""
        if not self._freedom_on(group_id):
            return ""
        ts = time.time() if timestamp is None else float(timestamp)
        try:
            # 同一条里既有叫停又有"可以找我"→ 以叫停为准（宁可少打扰）
            if fr.is_hush(text):
                hours = self._freedom_cfg().hush_hours
                self._state.set_proactive_hush(group_id, ts + hours * 3600.0)
                logger.info("[lifelike.freedom] scope=%s op=hush hours=%.1f", group_id, hours)
                return "hush"
            if fr.is_unhush(text):
                if self._state.get_proactive_hush(group_id) > 0:
                    self._state.set_proactive_hush(group_id, 0.0)
                    logger.info("[lifelike.freedom] scope=%s op=unhush", group_id)
                return "unhush"
        except Exception as e:
            logger.warning("[lifelike.freedom] scope=%s hush check failed: %s", group_id, type(e).__name__)
        return ""

    def freedom_status_lines(self, group_id: str) -> list[str]:
        """/iris_reply status 附加行（自由主动开关打开时）：熔断与叫停状态；关闭 → []。"""
        if not self._freedom_on(group_id):
            return []
        try:
            cfg = self._freedom_cfg()
            now = time.time()
            data = self._state.get_state(group_id)
            messages = self._window.get_messages(group_id)
            hint = None if messages else self._empty_window_hint.get(group_id)
            last_owner = self._owner_last_at(group_id, messages, hint)
            count = fr.breaker_count(data.initiate_timestamps, now, last_owner)
            lines = ["  自由主动: 开（由她自己决定什么时候私聊）"]
            lines.append(
                f"  熔断: {'已触发（等主人回话或 24h 窗口退出）' if count >= cfg.breaker_24h else '未触发'}"
                f"（主人上次回话后 {count}/{cfg.breaker_24h} 条）"
            )
            hush_left = float(data.proactive_hush_until or 0.0) - now
            lines.append(f"  主人叫停: {'剩余 %.1f 小时' % (hush_left / 3600) if hush_left > 0 else '无'}")
            return lines
        except Exception as e:
            logger.warning("[lifelike.freedom] scope=%s status failed: %s", group_id, type(e).__name__)
            return []

    async def _content_block(
        self, group_id: str, text: str, decision, *, free: bool, private: bool, force: bool
    ) -> str:
        """发送前的内容检查（只改"发不发这句"，不改频率）；返回拦截原因或 ""。

        - 冷静期试探句（"在吗""是不是生气了"）：对主人私聊所有主动生效（不只候选池路径）；
        - U8 同内容 / 同话题去重：近 24h 自由主动出站里有相似文本（≥0.8）或相同切入角度。"""
        if force:
            return ""
        try:
            if private and self._bridge is not None and _is_probe(text):
                if await self._relation_cooling(group_id):
                    return "cooling_probe"
            if free and fr.is_duplicate(
                text, decision.topic or decision.observation,
                self._state.get_freedom_recent(group_id), time.time(),
            ):
                return "freedom_dedupe"
        except Exception as e:
            logger.warning("[lifelike.freedom] scope=%s content check failed: %s", group_id, type(e).__name__)
        return ""

    def _freedom_note_sent(self, group_id: str, topic: str, text: str) -> None:
        """记一次自由主动出站（去重用；只存规范化文本与话题，不写日志）。"""
        try:
            self._state.note_freedom_sent(
                group_id, ts=time.time(), topic=topic or "", norm_text=fr.normalize_text(text)
            )
        except Exception as e:
            logger.warning("[lifelike.freedom] scope=%s note sent failed: %s", group_id, type(e).__name__)

    # ---- 活人感 U9：白名单群自由搭话（群内主动开话题） ----

    def _gf_on(self, group_id: str) -> bool:
        """群聊自由搭话只作用于平台白名单群；私聊、未注入、开关关闭 → False（= 3c6e6e6）。"""
        gf = self._gf
        if gf is None:
            return False
        try:
            return bool(gf.active(group_id))
        except Exception:
            return False

    def _gf_cfg(self) -> "gfm.GroupFreedomConfig":
        try:
            return self._gf.cfg() if self._gf is not None else gfm.GroupFreedomConfig()
        except Exception:
            return gfm.GroupFreedomConfig()

    def _scan_ids(self) -> set[str]:
        """调度遍历的会话：Iris 白名单；U9 打开时并上已见过消息的白名单群。"""
        ids = self._state.get_whitelist()
        gf = self._gf
        if gf is None:
            return ids
        try:
            if gf.enabled():
                ids |= {g for g in gf.tracker.known_groups() if gf.active(g)}
        except Exception as e:
            logger.warning("[lifelike.group_freedom] scan ids failed: %s", type(e).__name__)
        return ids

    def _drive(self) -> str:
        """聊天欲望档位（off / low / medium / high）；读取失败 → off。"""
        try:
            return dv.normalize(self._config.chat_drive)
        except Exception:
            return "off"

    def _private_eval_s(self) -> float:
        """U8 评估节奏：配置值与档位值中较快的一个（off = 配置值）。"""
        cfg = self._freedom_cfg()
        return float(dv.private_eval_minutes(self._drive(), cfg.eval_minutes)) * 60.0

    def _gf_last_human(self, group_id: str, messages) -> float:
        vals = [self._state.get_state(group_id).last_human_message_at]
        try:
            vals.append(self._gf.tracker.last_human_at(group_id))
        except Exception:
            pass
        try:
            self_id = str(self._self_id_get() or "")
        except Exception:
            self_id = ""
        for msg in reversed(messages or ()):
            if (self_id and str(msg.sender_id) == self_id) or msg.sender_name == "我":
                continue
            vals.append(msg.timestamp)
            break
        return max(vals)

    def _gf_block(self, group_id: str, *, now: float, messages, check_quiet: bool) -> str:
        """U9 群内主动仍生效的技术安全网（原因只进日志）；熔断首次触发记 ERROR。"""
        cfg = self._gf_cfg()
        tracker = self._gf.tracker
        if tracker.breaker_hit(group_id, cfg) and tracker.breaker_first(group_id):
            logger.error(
                "[lifelike.group_freedom] scope=%s op=breaker consecutive=%d: unsolicited group speech "
                "halted until someone in the group speaks (possible runaway sending, please check)",
                group_id, cfg.breaker_consecutive,
            )
        return gfm.block_reason(
            cfg, tracker, group_id,
            now=now,
            hush_until=float(self._state.get_state(group_id).proactive_hush_until or 0.0),
            has_context=bool(messages),
            last_human_at=self._gf_last_human(group_id, messages),
            check_quiet=check_quiet,
        )

    def _log_gf(self, group_id: str, reason: str) -> None:
        prev = self._gf_logged.get(group_id, "")
        if reason == prev:
            return
        self._gf_logged[group_id] = reason
        if reason:
            logger.info("[lifelike.group_freedom] scope=%s op=hold reason=%s", group_id, reason)
        else:
            logger.info("[lifelike.group_freedom] scope=%s op=evaluate prev=%s", group_id, prev)

    def _gf_facts(self, group_id: str, messages) -> str:
        now = time.time()
        try:
            tracker = self._gf.tracker
            last_self = tracker.last_self_at(group_id)
            for msg in reversed(messages or ()):
                if msg.sender_name == "我" or str(msg.sender_id) == str(self._self_id_get() or ""):
                    last_self = max(last_self, float(msg.timestamp))
                    break
            humans, addressed = tracker.response_state(group_id)
            return gfm.build_facts(
                now=now, wake="timer", messages=messages, self_id=str(self._self_id_get() or ""),
                owner_ids=self._gf.owner_ids(), last_self_at=last_self,
                humans_since_self=humans, addressed_since_self=addressed,
            )
        except Exception as e:
            logger.warning("[lifelike.group_freedom] scope=%s facts failed: %s", group_id, type(e).__name__)
            return f"<facts>\n- 现在：{fr.fmt_now(now)}\n</facts>"

    def _gf_note_sent(self, group_id: str, topic: str, text: str) -> None:
        """记一次群内主动（硬底线 / 熔断 / 去重用；只存规范化文本与话题）。"""
        try:
            self._gf.tracker.note_self(group_id, time.time(), unsolicited=True)
        except Exception:
            pass
        self._freedom_note_sent(group_id, topic, text)

    def is_initiating(self, group_id: str) -> bool:
        return group_id in self._initiating

    async def start(self) -> None:
        if not self._config.proactive_enabled:
            logger.info("Iris Reply: proactive engine disabled by config")
            return
        if self._bridge is not None:
            await self._bridge_startup()
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "Iris Reply: proactive engine started (interval=%dmin)",
            self._config.proactive_check_interval,
        )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _bridge_startup(self) -> None:
        """启动时：对账遗留发送、恢复持久化窗口、过期候选重新抽样（不集中补发）。"""
        try:
            await self._bridge.reconcile(force_query=True)
        except Exception as e:
            logger.warning("Iris Reply: bridge reconcile at startup failed: %s", type(e).__name__)
        try:
            await self._bridge.restore_windows(self._window)
        except Exception as e:
            logger.warning("Iris Reply: bridge restore_windows failed: %s", type(e).__name__)
        now = time.time()
        for group_id in self._state.get_whitelist():
            when = self._state.get_next_initiate_check(group_id)
            if 0 < when <= now:
                self._state.set_next_initiate_check(
                    group_id,
                    max(now + random.uniform(*_RESTART_RESAMPLE_SECONDS), when),
                )

    async def _bridge_reconcile(self) -> None:
        if self._bridge is None:
            return
        try:
            await self._bridge.reconcile()
        except Exception as e:
            logger.warning("Iris Reply: bridge reconcile failed: %s", type(e).__name__)

    def _is_paused(self, group_id: str, force: bool) -> bool:
        if force:
            return False
        data = self._state.get_state(group_id)
        if self._freedom_on(group_id) or self._gf_on(group_id):
            # 活人感 U8 / U9：未回复不再暂停；只有叫停（别发了 / 安静点）才暂停
            return float(data.proactive_hush_until or 0.0) > time.time()
        return data.initiate_no_reply_streak >= self._config.proactive_max_streak

    def notify_human_message(self, group_id: str, timestamp: float | None = None) -> None:
        """让旧候选失效，并从这条真实消息之后为单群独立预约。"""
        timestamp = time.time() if timestamp is None else timestamp
        self._state.record_human_message(group_id, timestamp)
        self._schedule_group(group_id, now=timestamp, after_activity=True)
        self._wake_event.set()

    def _schedule_group(
        self,
        group_id: str,
        *,
        now: float | None = None,
        after_activity: bool = False,
        not_before: float = 0.0,
    ) -> float:
        """生成带秒级抖动的每群独立候选时间。"""
        now = time.time() if now is None else now
        if self._freedom_on(group_id):
            # 活人感 U8：固定节奏评估（±20%）；主人刚说完话 → 短静默后再评估（不插话）
            fcfg = self._freedom_cfg()
            if after_activity:
                when = now + fcfg.owner_quiet_s + random.uniform(0.0, 300.0)
            else:
                when = now + self._private_eval_s() * random.uniform(0.8, 1.2)
            when = max(when, not_before)
            self._state.set_next_initiate_check(group_id, when)
            return when
        if self._gf_on(group_id):
            # 活人感 U9：群里安静 initiate_quiet 之后再评估；之后按节奏（±20%）评估
            gcfg = self._gf_cfg()
            if after_activity:
                when = now + gcfg.initiate_quiet_s + random.uniform(0.0, 300.0)
            else:
                when = now + gcfg.initiate_eval_s * random.uniform(0.8, 1.2)
            when = max(when, not_before)
            self._state.set_next_initiate_check(group_id, when)
            return when
        quiet_rng = self._floor_cfg().quiet_hours
        if self._state.is_muted():
            # 静音结束后重新抽 30–90 分钟，避免所有群在开闸时集中发言。
            when = now + self._state.seconds_until_unmuted(now)
            when += random.uniform(30 * 60.0, 90 * 60.0)
        elif quiet_rng is not None and self._lifelike_quiet(now):
            # 活人感 P0-8 硬静音：同样推到静音结束后 30–90 分钟（不补发）
            when = now + seconds_until_range_end(now, quiet_rng)
            when += random.uniform(30 * 60.0, 90 * 60.0)
        elif after_activity:
            earliest = now + self._state.minimum_quiet_seconds()
            jitter = random.uniform(
                0.0,
                max(60.0, self._config.proactive_check_interval * 120.0),
            )
            when = earliest + jitter
        else:
            base = max(60.0, self._config.proactive_check_interval * 60.0)
            when = now + random.uniform(base * 0.6, base * 2.8)
        when = max(when, not_before)
        self._state.set_next_initiate_check(group_id, when)
        return when

    async def _loop(self) -> None:
        while True:
            if not self._config.proactive_enabled:
                await asyncio.sleep(60)
                continue
            try:
                await self._ensure_schedules()
                deadline = self._next_deadline()
                timeout = 60.0 if deadline <= 0 else max(
                    0.05, min(60.0, deadline - time.time())
                )
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=timeout)
                    self._wake_event.clear()
                except asyncio.TimeoutError:
                    pass
                await self._bridge_reconcile()
                await self._scan_due()
            except Exception as e:
                logger.warning("Iris Reply: proactive scan error: %s", e)

    async def _ensure_schedules(self) -> None:
        now = time.time()
        for group_id in self._scan_ids():
            await self._check_pending_timeout(group_id)
            if not self._window.get_messages(group_id):
                # 活人感 P0-8：窗口为空时尝试用账本推得最后互动（开关关闭即跳过，= 现网）
                await self._refresh_empty_window_hint(group_id, now)
                if self._empty_window_allowed(group_id) is None:
                    continue
            if self._state.get_next_initiate_check(group_id) <= 0:
                self._schedule_group(group_id, now=now)

    def _next_deadline(self) -> float:
        deadlines = [
            self._state.get_next_initiate_check(gid)
            for gid in self._scan_ids()
            if self._has_schedule_context(gid)
            and self._state.get_next_initiate_check(gid) > 0
        ]
        return min(deadlines) if deadlines else 0.0

    async def _scan_due(self) -> None:
        now = time.time()
        due = [
            gid
            for gid in self._scan_ids()
            if self._has_schedule_context(gid)
            and 0 < self._state.get_next_initiate_check(gid) <= now
        ]
        random.shuffle(due)
        for group_id in due:
            await self._check_pending_timeout(group_id)
            if self._is_busy(group_id) or group_id in self._initiating:
                self._schedule_group(group_id, now=now)
                continue
            retry_after = self._skip_retry_after.get(group_id, 0)
            if retry_after > now:
                self._schedule_group(
                    group_id,
                    now=now,
                    not_before=retry_after + random.uniform(30.0, 180.0),
                )
                continue
            if not self._state.can_detect(group_id):
                self._schedule_group(group_id, now=now)
                continue
            messages = self._window.get_messages(group_id)
            defer, hint = await self._floor_verdict(group_id, messages)
            self._log_floor(group_id, defer)
            if self._freedom_on(group_id):
                # 活人感 U8：不走概率风险率 / 条数 / 间隔 / 未回复门控，只过技术安全网，交给她决定
                reason = self._freedom_block(group_id, now=now, messages=messages, hint=hint)
                if not reason and not self._config.enabled:
                    reason = "disabled"
                self._log_freedom(group_id, reason)
                self._schedule_group(group_id, now=now)
                if not reason:
                    result = await self.attempt_initiate(group_id)
                    logger.info("Iris Reply: proactive attempt for group %s: %s", group_id, result)
                continue
            if self._gf_on(group_id):
                # 活人感 U9：不走概率风险率 / 条数 / 间隔 / group_initiate_enabled，只过技术安全网，交给她决定
                reason = self._gf_block(group_id, now=now, messages=messages, check_quiet=True)
                if not reason and not self._config.enabled:
                    reason = "disabled"
                self._log_gf(group_id, reason)
                self._schedule_group(group_id, now=now)
                if not reason:
                    result = await self.attempt_initiate(group_id)
                    logger.info("Iris Reply: proactive attempt for group %s: %s", group_id, result)
                continue
            async with self._state.get_lock(group_id):
                if defer or hint is not None:
                    motive = self._signals.evaluate_timer(
                        group_id, messages, last_activity_hint=hint, defer=bool(defer),
                    )
                else:
                    motive = self._signals.evaluate_timer(group_id, messages)
            self._schedule_group(group_id, now=now)
            if motive:
                result = await self.attempt_initiate(group_id)
                logger.info("Iris Reply: proactive attempt for group %s: %s", group_id, result)

    async def _check_pending_timeout(self, group_id: str) -> None:
        data = self._state.get_state(group_id)
        if data.initiate_pending_since <= 0:
            return
        timeout = self._config.proactive_pending_timeout * 60
        if time.time() - data.initiate_pending_since >= timeout:
            async with self._state.get_lock(group_id):
                self._state.record_initiate_unanswered(group_id)
            await self._save_fn()
            logger.info("Iris Reply: initiate unanswered in group %s, streak recorded", group_id)

    async def attempt_initiate(self, group_id: str, force: bool = False) -> str:
        """执行一次主动发起。force 跳过门控（管理命令调试用），但保留互斥护栏。"""
        if group_id in self._initiating:
            return "该群正在发起中"
        if self._is_busy(group_id):
            return "该群有回复进行中，稍后再试"
        if not force and not self._state.is_whitelisted(group_id) and not self._gf_on(group_id):
            return "该群未启用"

        if self._bridge is None:
            refusal = self._legacy_target_refusal(group_id)
            if refusal:
                return f"已拒绝发起：{refusal}"

        umo = self._umo_get(group_id)
        if not umo:
            return "暂无该群的会话标识（等群里有过消息后再试）"

        provider_id = self._config.provider_id
        if not provider_id:
            try:
                provider_id = await self._context.get_current_chat_provider_id(umo)
            except Exception:
                provider_id = None
        if not provider_id:
            return "无法获取 LLM 提供商"

        messages = self._window.get_messages(group_id)
        quiet_minutes = int((time.time() - messages[-1].timestamp) / 60) if messages else 0
        if not messages:
            # 活人感 P0-8：空窗时用账本推得的最后互动计算静默时长（无提示 = 现网的 0）
            hint = self._empty_window_allowed(group_id)
            if hint is not None:
                quiet_minutes = max(0, int((time.time() - hint) / 60))
        context_cutoff = max(
            [m.timestamp for m in messages]
            + [self._state.get_state(group_id).last_human_message_at]
        )

        free = not force and self._freedom_on(group_id)
        gfree = not force and not free and self._gf_on(group_id)
        drive = self._drive() if (free or gfree) else "off"
        free_status: dict = {}
        self._initiating.add(group_id)
        bridge = self._bridge
        turn_id = ""
        turn_reason = "aborted"
        try:
            self._state.record_detect_time(group_id)
            candidates = None
            avoid: list[str] = []
            private = bridge is not None and group_id.startswith("private:")
            self._avoid_cache.pop(group_id, None)
            if bridge is not None:
                if free or gfree:
                    start = await bridge.begin_turn(group_id, force, free=True)
                else:
                    start = await bridge.begin_turn(group_id, force)
                if start.turn is None:
                    return f"已跳过发起：{start.reason}"
                turn_id = start.turn["turn_id"]
                candidates = start.candidates or None
                if private:
                    try:
                        avoid = list(await bridge.avoid_topics(group_id) or [])
                    except Exception as e:
                        logger.warning(
                            "Iris Reply: avoid_topics failed for %s: %s", group_id, type(e).__name__,
                        )
                        avoid = []
                    self._avoid_cache[group_id] = avoid
            if free:
                free_status = await self._owner_status(group_id)
            req = DecisionRequest(
                group_id=group_id,
                wake="timer",
                motive="initiate",
                quiet_minutes=quiet_minutes,
                private=private,
                candidates=candidates,
                avoid_topics=avoid or None,
                freedom_facts=(
                    (await self._freedom_facts(group_id, messages, free_status)) if free
                    else (self._gf_facts(group_id, messages) if gfree else "")
                ),
            )
            if gfree:
                req.group_free = True
            if drive != "off":
                req.chat_drive = drive
                req.speak_wins = True
            if free and dv.private_cooldown_cap(drive):
                req.self_pause_cap = int(dv.private_cooldown_cap(drive))
            elif gfree:
                req.self_pause_cap = self._gf_cfg().self_pause_cap
            outcome = await self._core.decide(req, self._llm_manager, provider_id)

            if outcome.error or outcome.decision is None:
                turn_reason = "decision_error"
                self._stats.record_decision_error(group_id, "initiate")
                self._skip_retry_after[group_id] = time.time() + _SKIP_RETRY_SECONDS
                if outcome.error_kind == "input_content_safety_1026":
                    async with self._state.get_lock(group_id):
                        cleanup = self._core.clear_rejected_dynamic_context(
                            group_id,
                            outcome.dynamic_context_sources,
                        )
                        cooldown = self._state.set_cooldown(
                            group_id,
                            INPUT_SAFETY_COOLDOWN_MINUTES,
                        )
                    await self._save_fn()
                    logger.warning(
                        "Iris Reply: initiate input rejected by provider safety filter "
                        "for group %s (1026, retryable=false, dynamic_sources=%d, "
                        "window_removed=%d, observation_cleared=%s, "
                        "anchor_cleared=%s, cooldown=%dmin)",
                        group_id,
                        cleanup.dynamic_source_count,
                        cleanup.window_removed,
                        cleanup.observation_cleared,
                        cleanup.anchor_cleared,
                        cooldown,
                    )
                return f"决策调用失败: {outcome.error}"

            decision = outcome.decision
            self._stats.record_decision(
                group_id, "initiate",
                system_prompt=outcome.system_prompt,
                user_prompt=outcome.user_prompt,
                response_text=outcome.raw_text,
                decision=decision,
                duration_ms=outcome.duration_ms,
            )
            logger.info(
                "Iris Reply: initiate decision for group %s: speak=%s, drifted=%s, "
                "cooldown=%d, source=%s, why_now=%.100s, topic=%.100s",
                group_id, decision.should_speak, decision.drifted,
                decision.cooldown_minutes, decision.topic_source,
                text_for_log(decision.why_now, 100), text_for_log(decision.topic, 100),
            )

            async with self._state.get_lock(group_id):
                if decision.observation:
                    self._state.set_observation(group_id, decision.observation)
                if decision.cooldown_minutes:
                    minutes = decision.cooldown_minutes
                    if free and dv.private_cooldown_cap(drive):
                        minutes = min(minutes, int(dv.private_cooldown_cap(drive)))
                    elif gfree:
                        minutes = min(minutes, self._gf_cfg().self_pause_cap)
                    self._state.set_cooldown(group_id, minutes)
                if decision.drifted:
                    self._state.close_anchor(group_id)
                    self._state.record_drift(group_id)

            if decision.parse_failed:
                turn_reason = "decision_parse_failed"
                self._skip_retry_after[group_id] = time.time() + _SKIP_RETRY_SECONDS
                await self._save_fn()
                return "决策结果解析失败"

            if not decision.should_speak:
                turn_reason = "llm_declined"
                # LLM 否决：重置概率时钟，避免短时间连续询问模型。
                async with self._state.get_lock(group_id):
                    self._state.reset_initiate_drive(group_id)
                retry = self._private_eval_s() if free else (
                    self._gf_cfg().initiate_eval_s if gfree else _SKIP_RETRY_SECONDS
                )
                self._skip_retry_after[group_id] = time.time() + retry
                await self._save_fn()
                return "LLM 决定暂不发起"

            # 决策明确选中了本回合的有效候选事项时，不做话题回避判定：
            # 例如“面试”已完成、“面试结果”仍是待续候选，跟进候选是合法的。
            chosen_candidate = bool(decision.item_id) and any(
                str(c.get("item_id")) == str(decision.item_id) for c in (candidates or [])
            )
            if avoid and not chosen_candidate and _topic_matches(decision.topic, avoid):
                turn_reason = "avoided_topic"
                self._skip_retry_after[group_id] = time.time() + _SKIP_RETRY_SECONDS
                await self._save_fn()
                return "已跳过发起：avoided_topic"

            guard = self._initiate_guard_reason(
                group_id, context_cutoff, require_quiet=not force
            )
            if guard:
                turn_reason = "guard"
                await self._save_fn()
                return f"已取消发起：{guard}"

            if bridge is not None:
                await bridge.mark_turn(
                    turn_id, "generating", item_id=decision.item_id, llm_calls=1
                )

            # 发言文本走主管线人格生成（决策只给切入角度，不直出消息）
            text = await self._generate_speech(group_id, umo, provider_id, decision)
            if not text:
                text = decision.message.strip()
            if not text:
                turn_reason = "speech_failed"
                self._skip_retry_after[group_id] = time.time() + _SKIP_RETRY_SECONDS
                await self._save_fn()
                return "发言文本生成失败"

            if bridge is not None and str(decision.item_id or "").startswith("cand:"):
                text, blocked = await self._review_pool_speech(
                    group_id, umo, provider_id, decision, turn_id, text
                )
                if blocked:
                    turn_reason = f"blocked_style:{blocked}"
                    self._skip_retry_after[group_id] = time.time() + _SKIP_RETRY_SECONDS
                    await self._save_fn()
                    return f"已拦截发起：{blocked}"
            text = text[: self._config.proactive_max_message_len]
            if gfree:
                # U9：PRIV-02 私聊内容不进群（与群聊被动回复同一规则；整段删空 → 不发）
                stripped = _strip_private_for_group(group_id, text)
                if stripped != text:
                    # U19 审查 11：PRIV-02 删过句子 → 原来配的贴纸未必还合适，放弃
                    sticker_take = getattr(self, "_sticker_take", None)
                    if sticker_take is not None:
                        try:
                            await sticker_take(group_id, umo, "")
                        except Exception:
                            pass
                text = stripped
                if not text.strip():
                    turn_reason = "priv02"
                    self._skip_retry_after[group_id] = time.time() + self._gf_cfg().initiate_eval_s
                    await self._save_fn()
                    return "已取消发起：priv02"
            man_hits = _proactive_man_hits(text, always=free or gfree)
            if not man_hits and (free or gfree) and fr.nag_hit(text):
                man_hits = ["NAG"]  # U8：讨要回复 / 催促（"人呢""怎么还不回"）属于反情绪勒索
            if man_hits:
                # P1-4：操纵句（情绪勒索、讨要关注等）一律不发，只记规则 id
                turn_reason = "style_man"
                self._skip_retry_after[group_id] = time.time() + _SKIP_RETRY_SECONDS
                await self._save_fn()
                logger.info(
                    "[lifelike.style_inject] proactive blocked group=%s rules=%s",
                    group_id, ",".join(man_hits),
                )
                return "已取消发起：style_man"
            content_block = await self._content_block(
                group_id, text, decision, free=free or gfree, private=private, force=force
            )
            if content_block:
                turn_reason = content_block
                retry = self._private_eval_s() if free else (
                    self._gf_cfg().initiate_eval_s if gfree else _SKIP_RETRY_SECONDS
                )
                self._skip_retry_after[group_id] = time.time() + retry
                await self._save_fn()
                logger.info("[lifelike.freedom] scope=%s op=content_block reason=%s", group_id, content_block)
                return f"已取消发起：{content_block}"
            if bridge is not None:
                turn_reason = "send_aborted"
                return await self._send_via_bridge(
                    group_id, umo, turn_id, text, decision, context_cutoff, force
                )
            if self._text_transform is not None:
                # 直发通路不触发 on_decorating_result，消息始终以纯文本发送，
                # 由调用方补齐与管线一致的文本处理（如 Markdown 去除）
                try:
                    text = self._text_transform(text) or text
                except Exception as e:
                    logger.warning(
                        "Iris Reply: text_transform error for group %s: %s",
                        group_id, e,
                    )
            chain = MessageChain().message(text)
            # 紧贴发送动作做最后一次 TOCTOU 校验，尽量缩小竞态窗口。
            guard = self._initiate_guard_reason(
                group_id, context_cutoff, require_quiet=not force
            )
            if guard:
                await self._save_fn()
                return f"已取消发起：{guard}"
            refusal = self._legacy_target_refusal(group_id)
            if refusal:
                return f"已拒绝发起：{refusal}"
            ok = await self._context.send_message(umo, chain)
            if not ok:
                _record_initiate_send(group_id, text, False, "未找到匹配的消息平台")
                return "发送失败：未找到匹配的消息平台"

            _record_initiate_send(group_id, text, True)

            self._window.append(group_id, WindowMessage(
                sender_id=self._self_id_get() or "iris",
                sender_name="我",
                content=text,
                timestamp=time.time(),
            ))
            async with self._state.get_lock(group_id):
                self._state.record_initiate(
                    group_id,
                    topic=decision.topic or decision.observation,
                    bot_message=text,
                    users=decision.watch or None,
                    keywords=decision.watch_keywords or None,
                    reason=decision.why_now or decision.why,
                )
            if free:
                self._freedom_note_sent(group_id, decision.topic or decision.observation, text)
            elif gfree:
                self._gf_note_sent(group_id, decision.topic or decision.observation, text)
            now = time.time()
            self._schedule_group(
                group_id,
                now=now,
                not_before=now + self._post_send_gap(group_id),
            )
            await self._save_fn()
            if self._on_initiate_sent is not None:
                try:
                    await self._on_initiate_sent(group_id, text)
                except Exception as e:
                    logger.warning(
                        "Iris Reply: on_initiate_sent callback error for group %s: %s",
                        group_id, e,
                    )
            return f"已发起: {text[:50]}"
        except Exception as e:
            logger.error("Iris Reply: initiate failed for group %s: %s", group_id, e)
            return f"发起异常: {e}"
        finally:
            if bridge is not None and turn_id:
                # 未走到发送记账的回合（否决/失败/取消/异常）在此收尾；已终结则无操作
                try:
                    await bridge.close_turn(turn_id, "skipped", turn_reason)
                except Exception as e:
                    logger.warning(
                        "Iris Reply: close_turn failed for group %s: %s",
                        group_id, type(e).__name__,
                    )
            self._initiating.discard(group_id)

    def _transform(self, group_id: str, text: str) -> str:
        if self._text_transform is None:
            return text
        try:
            return self._text_transform(text) or text
        except Exception as e:
            logger.warning(
                "Iris Reply: text_transform error for group %s: %s", group_id, e,
            )
            return text

    async def _send_via_bridge(
        self,
        group_id: str,
        umo: str,
        turn_id: str,
        text: str,
        decision: Decision,
        context_cutoff: float,
        force: bool,
    ) -> str:
        """连续性桥接发送：分段（≤3）、每段前复查、带回执发送、按 §5.9 记账。"""
        bridge = self._bridge
        parts = [
            p for p in (self._transform(group_id, raw).strip() for raw in split_speech_parts(text))
            if p
        ]
        if not parts:
            self._skip_retry_after[group_id] = time.time() + _SKIP_RETRY_SECONDS
            await self._save_fn()
            return "发言文本生成失败"
        await bridge.mark_turn(turn_id, "sending", parts_total=len(parts), llm_calls=2)
        stop_reason = ""
        try:
            for idx, part in enumerate(parts):
                guard = self._initiate_guard_reason(
                    group_id, context_cutoff, require_quiet=not force
                )
                if guard:
                    stop_reason = "guard"
                    break
                check = await bridge.presend_check(
                    turn_id, umo, paused=self._is_paused(group_id, force)
                )
                if not check.ok:
                    stop_reason = check.reason
                    break
                outcome = await bridge.send_part(turn_id, umo, part, idx)
                if not outcome.sent:
                    stop_reason = outcome.error or f"send_{outcome.status}"
                    break
        except Exception as e:
            # 已发出的分段仍要记账；异常只终止剩余分段
            stop_reason = stop_reason or "send_exception"
            logger.warning(
                "Iris Reply: bridge send error for group %s: %s", group_id, type(e).__name__,
            )
        result = await bridge.finish_turn(turn_id, stop_reason=stop_reason)
        if not force and self._freedom_on(group_id) and (
            result.parts_sent >= 1 or result.state == "uncertain"
        ):
            # U8 去重：发出或结果不确定都记一次（宁可少重复）
            self._freedom_note_sent(group_id, decision.topic or decision.observation, text)
        elif not force and self._gf_on(group_id) and (
            result.parts_sent >= 1 or result.state == "uncertain"
        ):
            # U9：硬底线 / 熔断 / 去重同样按"发出或不确定"记一次
            self._gf_note_sent(group_id, decision.topic or decision.observation, text)
        return await self._account_bridge_result(group_id, decision, result)

    async def _owner_answered(self, result) -> bool:
        """首段发出后本人已回复（因新消息中止，或水位已越过回合起点）"""
        reason = str(getattr(result, "reason", "") or "")
        if reason == "new_inbound" or reason.startswith("stale"):
            return True
        checker = getattr(self._bridge, "owner_replied_since", None)
        if checker is None:
            return False
        try:
            return bool(await checker(result.turn_id))
        except Exception:
            return False

    async def _account_bridge_result(self, group_id: str, decision: Decision, result) -> str:
        now = time.time()
        if result.parts_sent >= 1:
            # 至少一段真实发出：额度只计 1 次；pending=now，
            # 除非本人已在首段之后回复（已接话，不再等待、不计未回复）
            answered = await self._owner_answered(result)
            sent_text = "\n\n".join(result.sent_texts)
            _record_initiate_send(group_id, sent_text, True)
            self._window.append(group_id, WindowMessage(
                sender_id=self._self_id_get() or "iris",
                sender_name="我",
                content=sent_text,
                timestamp=now,
            ))
            async with self._state.get_lock(group_id):
                self._state.record_initiate(
                    group_id,
                    topic=decision.topic or decision.observation,
                    bot_message=sent_text,
                    users=decision.watch or None,
                    keywords=decision.watch_keywords or None,
                    reason=decision.why_now or decision.why,
                    set_pending=not answered,
                )
            self._schedule_group(
                group_id,
                now=now,
                not_before=now + self._post_send_gap(group_id),
            )
            await self._save_fn()
            if self._on_initiate_sent is not None:
                try:
                    await self._on_initiate_sent(group_id, sent_text)
                except Exception as e:
                    logger.warning(
                        "Iris Reply: on_initiate_sent callback error for group %s: %s",
                        group_id, e,
                    )
            return f"已发起: {sent_text[:50]}"
        if result.state == "uncertain":
            # 结果不确定：bridge 已预占额度（不设 pending）；等对账，min_interval 内不再发起
            _record_initiate_send(group_id, "", False, "发送结果不确定，等待对账")
            self._schedule_group(
                group_id,
                now=now,
                not_before=now + self._post_send_gap(group_id),
            )
            await self._save_fn()
            return "发送结果不确定，等待对账"
        reason = result.reason or result.state
        if result.state == "failed":
            # 确认失败：不扣额度、不计未回复；短期内不重试
            _record_initiate_send(group_id, "", False, reason)
            self._skip_retry_after[group_id] = now + _SKIP_RETRY_SECONDS
            await self._save_fn()
            return f"发送失败：{reason}"
        await self._save_fn()
        return f"已取消发起：{reason}"

    def _initiate_guard_reason(
        self,
        group_id: str,
        context_cutoff: float,
        *,
        require_quiet: bool,
    ) -> str:
        """发送前二次确认现场，防止生成期间群聊已经恢复。"""
        if self._is_busy(group_id):
            return "群内已有回复进行中"
        data = self._state.get_state(group_id)
        messages = self._window.get_messages(group_id)
        latest = max(
            [m.timestamp for m in messages] + [data.last_human_message_at]
        )
        if latest > context_cutoff + 1e-6:
            return "决策期间出现了新消息"
        if not require_quiet:
            return ""
        if self._freedom_on(group_id):
            # 活人感 U8：静音 / 冷却（她本轮刚给的 cooldown）/ 最小静默改为技术安全网
            hint = None if messages else self._empty_window_allowed(group_id)
            reason = self._freedom_block(
                group_id, now=time.time(), messages=messages, hint=hint, check_cooldown=False
            )
            return f"自由主动安全网：{reason}" if reason else ""
        if self._gf_on(group_id):
            # 活人感 U9：静音 / 冷却 / 最小静默改为群聊技术安全网（叫停 / 硬底线 / 熔断）
            reason = self._gf_block(group_id, now=time.time(), messages=messages, check_quiet=False)
            return f"群聊自由搭话安全网：{reason}" if reason else ""
        if self._state.is_muted() or self._lifelike_quiet():
            return "当前进入静音时段"
        if data.state == GroupState.COOLDOWN:
            return "当前处于冷却状态"
        hint = None if messages else self._empty_window_allowed(group_id)
        if not messages and hint is None:
            return "缺少可用群聊上下文"
        quiet = time.time() - max(
            data.last_human_message_at,
            messages[-1].timestamp if messages else 0.0,
            hint or 0.0,
        )
        if quiet < self._state.minimum_quiet_seconds():
            return "群聊尚未达到最小静默时间"
        return ""

    async def _care_style_category(self, decision) -> str:
        """选中的是关心候选（group_care）→ 返回类别（危机为 crisis）；否则 ""。异常 → ""。"""
        item = str(getattr(decision, "item_id", "") or "")
        if not item.startswith("cand:"):
            return ""
        coord = getattr(self._bridge, "initiative", None)
        if coord is None:
            return ""
        try:
            cand = await asyncio.to_thread(coord.pool.get, item)
            if not cand or cand.get("ctype") != "group_care":
                return ""
            from ..lifelike.initiative.coordinator import has_flag, ref_value

            return "crisis" if has_flag(cand, "crisis") else (ref_value(cand, "cat") or "care")
        except Exception as e:
            logger.warning("[lifelike.style_inject] care style lookup skipped: %s", type(e).__name__)
            return ""

    async def _generate_speech(
        self,
        group_id: str,
        umo: str,
        provider_id: str,
        decision: Decision,
    ) -> str:
        """用主管线人格生成发起文本。

        决策层只给出切入角度（topic），具体措辞由 bot 当前人格结合群聊
        上下文产出，保证主动消息与被动回复的语气、风格一致。
        失败时返回空字符串，由调用方回退或重试。
        """
        persona_prompt = ""
        sticker_take = getattr(self, "_sticker_take", None)
        if sticker_take is not None:
            try:
                await sticker_take(group_id, umo, "")  # 清掉上一轮没发出去的暂存贴纸
            except Exception:
                pass
        try:
            personality = await self._context.persona_manager.get_default_persona_v3(umo)
            if personality:
                persona_prompt = personality.get("prompt", "") or ""
        except Exception as e:
            logger.warning(
                "Iris Reply: persona resolve failed for group %s: %s", group_id, e,
            )

        messages = self._window.get_messages(group_id)
        context_block = self._packager.package(group_id, messages, "initiate")
        context_block = guard_numeric(context_block, source="proactive.window", lenient=True)
        bridged = self._bridge is not None
        private = bridged and group_id.startswith("private:")
        # P0-5：决策层给出的话题若含数值化关系表达则丢弃，回落默认话题
        topic_text = guard_numeric(
            decision.topic.strip(), source="proactive.topic"
        ).strip()
        # 隐私审查 #6：why_now 同样可能带出画像 / 记忆里的数值化关系表达
        why_now_text = guard_numeric(decision.why_now or "", source="proactive.why_now")
        if private:
            hint = PRIVATE_SPEAK_HINT_INITIATE.format(
                topic=topic_text or "顺着你们最近的私聊自然地开口",
                why_now=why_now_text or "你们的私聊出现了自然的空档",
                topic_source=decision.topic_source or "recent_context",
            )
        else:
            topic = topic_text or "自由选择一个轻松、大家能接上话的角度"
            hint = SPEAK_HINTS["initiate"].format(
                topic=topic,
                why_now=why_now_text or "当前群聊出现了自然的交流空档",
                topic_source=decision.topic_source or "recent_context",
            )
        if bridged:
            hint = f"{hint}\n{SPEAK_SPLIT_NOTE}"
        prompt = f"{context_block}\n\n{hint}"

        # 直连 llm_generate 不经过主管线，需自行注入当前时间，
        # 否则 LLM 无时间锚点，会从窗口里的旧消息推断时间（如早上说晚上好）。
        system_prompt = persona_prompt or ""
        time_hint = wrap_system_reminder(resolve_datetime_reminder(self._context, umo))
        if time_hint:
            system_prompt = f"{time_hint}\n\n{system_prompt}".strip()

        # 接入桥接时：连续性上下文（与普通回复同一组装逻辑，已含私聊画像）
        continuity = ""
        if bridged:
            continuity = await self._bridge.speech_context(group_id, decision.item_id)
            if continuity:
                system_prompt = f"{system_prompt}\n\n{continuity}".strip()
            avoid_text = _avoid_block(self._avoid_cache.get(group_id) or []) if private else ""
            if avoid_text:
                system_prompt = f"{system_prompt}\n\n{avoid_text}".strip()

        # 直连 generate_direct 不会触发 on_llm_request，画像需要在此手动注入，
        # 否则主动消息只能看到人格，看不到长期记忆。
        if self._profile_get and not continuity:
            try:
                profile_text = await self._profile_get(group_id, umo)
            except Exception as e:
                profile_text = ""
                logger.warning("Iris Reply: profile resolve failed for %s: %s", group_id, e)
            profile_text = guard_numeric(
                profile_text, source="proactive.profile", strict=True
            )
            if profile_text:
                system_prompt = (
                    f"{system_prompt}\n\n【你记得的关于对方的信息】\n{profile_text}"
                ).strip()

        # P1-4：主动消息不经过 llm_request_hook，style 段拼在 system 末尾（每次现拼，不涉及缓存）
        style_text = _proactive_style_text(group_id, await self._care_style_category(decision))
        if style_text:
            system_prompt = f"{system_prompt}\n\n{style_text}".strip()

        # U19：表情包说明（use 开关 off / 无回执平台 → 空）
        sticker_block = getattr(self, "_sticker_block", None)
        if sticker_block is not None:
            try:
                sticker_text = await sticker_block(group_id, umo)
            except Exception as e:
                sticker_text = ""
                logger.warning("[stickers] proactive sticker block skipped: %s", type(e).__name__)
            if sticker_text:
                system_prompt = f"{system_prompt}\n\n{sticker_text}".strip()

        try:
            text = await self._llm_manager.generate_direct(
                prompt=prompt,
                system_prompt=system_prompt or None,
                provider_id=provider_id,
                module=PROACTIVE_REPLY_INITIATE,
            )
        except Exception as e:
            logger.warning(
                "Iris Reply: speech generation failed for group %s: %s", group_id, e,
            )
            return ""
        text = (text or "").strip()
        if text and sticker_take is not None:
            try:
                text = (await sticker_take(group_id, umo, text) or "").strip()
            except Exception as e:
                logger.warning("[stickers] proactive marker skipped: %s", type(e).__name__)
        return text
