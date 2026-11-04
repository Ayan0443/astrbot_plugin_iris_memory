"""
Iris Chat Memory - 主动联系桥接（DESIGN.md §5）

ProactiveEngine 通过可选的 ``bridge`` 参数接入；bridge 为 None 时引擎行为与原来完全一致。

职责：
- 候选事项（§5.3）与回合账本（§5.4）：``begin_turn`` / ``mark_turn`` / ``close_turn`` /
  ``finish_turn``；同一 scope 同时只允许一个进行中的回合，存在未结算 uncertain 回合时跳过。
- 发送目标硬拦截 + 发送前复查（§5.7）：``check_target`` / ``presend_check``。
- 分段发送（§5.8）：``send_part``（经 OutboundLedger，先落盘 sending 再发）。
- 记账（§5.9）：sent/partial → 额度由引擎 record_initiate 计 1 次；全部确认失败 →
  不扣额度；存在 uncertain 且无 sent → ``state.reserve_initiate``（预占，不设 pending）。
- 对账（§5.10）：``reconcile``（启动与每次扫描前，无待对账行时只做两次轻量查询）。
- 暂停恢复（§5.2）：``note_owner_message`` / ``resume_if_advanced``。
- 窗口持久化（§5.11）：``save_windows`` / ``restore_windows``（丢弃 >24h）。

日志格式：``[continuity] turn=<id> scope=<k> wm=<msg_id> item=<id> decision=<...> reason=<...>
llm=<n> sent=<ids>``；不打印 prompt / 正文 / 画像。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

from . import models as m
from .outbound import (
    OutboundLedger,
    SendOutcome,
    match_sent_message,
    message_timestamp,
    parse_umo,
    peer_of_umo,
    resolve_platform,
)

__all__ = [
    "ProactiveBridge",
    "SendPolicy",
    "TurnStart",
    "PresendResult",
    "TurnResult",
    "DEFAULT_FORBIDDEN_CHATS",
    "split_speech_parts",
]

# 永不作为发送目标的会话（频道）；配置可追加，不可移除
# 内置禁发列表为空：主人频道等请配置在 continuity.owner_channel_ids / proactive.silent_sessions
DEFAULT_FORBIDDEN_CHATS: frozenset = frozenset()

CANDIDATE_LIMIT = 3
MENTION_COOLDOWN_S = 6 * 3600
MAX_ASK_COUNT = 2
WINDOW_MAX_AGE_S = 24 * 3600
RECONCILE_MATCH_SLACK_S = 60.0
RECONCILE_FAIL_AFTER_S = 600.0
RECONCILE_FETCH_LIMIT = 20
# 对账跳过最近 60s 内有更新的进行中回合 / sending 行（可能正被本进程收尾）
RECONCILE_GRACE_S = 60.0
MAX_PARTS = 3
# 「不要主动提起」：最近 30 天内被拒绝 / 已完成 / 已取消的同受众事项
AVOID_LOOKBACK_S = 30 * 86400
AVOID_LIMIT = 8
_AVOID_STATUSES = (m.STATUS_DECLINED, m.STATUS_DONE, m.STATUS_CANCELLED)

_IN_FLIGHT = (m.TURN_DECIDING, m.TURN_GENERATING, m.TURN_SENDING)
PUSH_CHECK_TTL_S = 300.0  # 活人感 P1-8：has_push_candidate 结果缓存
_TERMINAL = (
    m.TURN_SENT,
    m.TURN_PARTIAL,
    m.TURN_FAILED,
    m.TURN_UNCERTAIN,
    m.TURN_SKIPPED,
    m.TURN_CANCELLED,
)

# 发送前复查的原因
REASON_TURN_CLOSED = "turn_closed"
REASON_NEW_INBOUND = "new_inbound"
REASON_FOCUS_INACTIVE = "focus_item_inactive"
REASON_PAUSED = "paused"
REASON_SILENT = "target_silent_session"
REASON_FORBIDDEN = "target_forbidden_chat"
REASON_NOT_OWNER = "target_private_not_whitelisted"
REASON_GROUP_NOT_ALLOWED = "target_group_not_whitelisted"
REASON_UMO_MISMATCH = "target_umo_mismatch"
REASON_BAD_SCOPE = "target_invalid_scope"


def _quota_cursor(turn_id: str) -> str:
    return f"quota_reservation:{turn_id}"


def _chat_variants(chat_id: Any) -> set[str]:
    """同一 Telegram 会话的几种写法（-100xxx / xxx / -xxx），用于禁发集合比较"""
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


def split_speech_parts(text: str, max_parts: int = MAX_PARTS) -> list[str]:
    """按空行切分为 ≤max_parts 段；多出的段并入最后一段"""
    import re

    chunks = [c.strip() for c in re.split(r"\n\s*\n", str(text or "")) if c.strip()]
    if len(chunks) <= max_parts:
        return chunks
    return chunks[: max_parts - 1] + ["\n\n".join(chunks[max_parts - 1 :])]


@dataclass(frozen=True)
class SendPolicy:
    """发送目标策略（每次调用时经 policy_get 现取，配置热更新即生效）"""

    owner_ids: frozenset = frozenset()  # proactive.private_whitelist
    silent_sessions: frozenset = frozenset()  # proactive.silent_sessions
    forbidden_chats: frozenset = DEFAULT_FORBIDDEN_CHATS
    group_whitelist: frozenset | None = None  # None → 用 StateManager 白名单


@dataclass
class TurnStart:
    turn: dict[str, Any] | None
    reason: str = ""
    candidates: list[dict[str, Any]] = field(default_factory=list)

    @property
    def skipped(self) -> bool:
        return self.turn is None


@dataclass
class PresendResult:
    ok: bool
    reason: str = ""


@dataclass
class TurnResult:
    turn_id: str
    state: str
    reason: str = ""
    sent_texts: list[str] = field(default_factory=list)
    sent_ids: list[int] = field(default_factory=list)
    uncertain: int = 0
    quota_state: str = "none"

    @property
    def parts_sent(self) -> int:
        return len(self.sent_texts)


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        return await value
    return value


def _resolve(value: Any, default: Any) -> Any:
    try:
        return value() if callable(value) else (default if value is None else value)
    except Exception:
        return default


class ProactiveBridge:
    def __init__(
        self,
        store: Any,
        state: Any,
        *,
        policy_get: Callable[[], SendPolicy],
        context: Any = None,
        umo_get: Callable[[str], str | None] | None = None,
        speech_context_fn: Callable[[str, str | None], Awaitable[str]] | None = None,
        client_getter: Callable[[str], Any] | None = None,
        clock: Callable[[], float] = time.time,
        closure_cooldown_minutes: Any = 120,
        topics_enabled: Any = True,
        save_fn: Callable[[], Awaitable[None]] | None = None,
        send_timeout_s: float = 60.0,
        reconcile_query_interval_s: float = 60.0,
        after_sent: Callable[[str, Any, str], Awaitable[None]] | None = None,
    ) -> None:
        self._store = store
        self._state = state
        self._policy_get = policy_get
        self._context = context
        self._umo_get = umo_get
        self._speech_context_fn = speech_context_fn
        self._client_getter = client_getter
        self._clock = clock
        self._closure_cooldown_minutes = closure_cooldown_minutes
        self._topics_enabled = topics_enabled
        self._save_fn = save_fn
        # 活人感 P1-5：主动消息回执为 sent 后的接入点（ClaimAuditor）；None = 旧行为
        self._after_sent = after_sent
        self._ledger = OutboundLedger(store, clock=clock, send_timeout_s=send_timeout_s)
        self._reconcile_query_interval_s = float(reconcile_query_interval_s)
        self._active: dict[str, str] = {}  # scope_key -> turn_id（本进程进行中的回合）
        self._last_query: dict[str, float] = {}
        self._window_digest: dict[str, str] = {}
        self._reconcile_lock = asyncio.Lock()
        # 活人感 P1-8：主动候选池协调器（lifelike.initiative.coordinator.InitiativeCoordinator）。
        # None 或 lifelike_initiative_mode=off 时桥接行为与改造前完全一致。
        self.initiative: Any = None
        self._push_check_cache: dict[str, tuple[float, bool]] = {}
        # 活人感 U9：群聊自由搭话时额外允许的群（平台 id_whitelist 群；频道 / silent / 禁发仍在前面硬拦截）。
        # None 或返回 False = 旧行为（只认 StateManager 白名单）。
        self.group_allow: Callable[[str], bool] | None = None

    @property
    def ledger(self) -> OutboundLedger:
        return self._ledger

    def _now(self) -> float:
        return float(self._clock())

    async def _db(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    def _log(self, turn: dict[str, Any] | None, **fields: Any) -> None:
        t = turn or {}
        parts = [
            f"turn={t.get('turn_id', '-')}",
            f"scope={t.get('scope_key', fields.pop('scope', '-'))}",
            f"wm={t.get('input_msg_id', '-')}",
            f"item={t.get('item_id') or '-'}",
        ]
        parts += [f"{k}={v}" for k, v in fields.items()]
        logger.info("[continuity] " + " ".join(parts))

    async def _save(self) -> None:
        if self._save_fn is None:
            return
        try:
            await self._save_fn()
        except Exception as e:
            logger.warning(f"[continuity] bridge save_fn failed: {type(e).__name__}")

    # ------------------------------------------------------------ candidates

    async def candidates(self, scope_key: str, now: float | None = None) -> list[dict[str, Any]]:
        """§5.3：≤3 项可作为主动话题的事项（失败返回 []）"""
        if not bool(_resolve(self._topics_enabled, True)):
            return []
        now = self._now() if now is None else float(now)
        try:
            items = await self._db(self._store.list_items, None, (m.STATUS_ACTIVE,), scope_key, 50)
        except Exception as e:
            logger.warning(f"[continuity] candidates scope={scope_key} failed: {type(e).__name__}")
            return []
        out = []
        for it in items:
            if it.get("status") != m.STATUS_ACTIVE or not it.get("proactive_allowed"):
                continue
            if scope_key not in (it.get("allowed_audiences") or []):
                continue
            nb = it.get("not_before")
            if nb is not None and now < nb:
                continue
            exp = it.get("expires_at")
            if exp is not None and now >= exp:
                continue
            last = it.get("last_mentioned_at")
            if last is not None and now - last < MENTION_COOLDOWN_S:
                continue
            if int(it.get("ask_count") or 0) >= MAX_ASK_COUNT:
                continue
            out.append(it)
        out.sort(
            key=lambda it: (
                -int(it.get("remind_requested") or 0),
                -int(it.get("importance") or 1),
                it.get("not_before") if it.get("not_before") is not None else float("-inf"),
            )
        )
        out = out[:CANDIDATE_LIMIT]
        coord = self.initiative
        if coord is not None:
            # 活人感 P1-8：合并候选池（失败 → 只用 items 候选，= 旧行为）
            try:
                out = await coord.merge_candidates(scope_key, now, out)
            except Exception as e:
                logger.warning(f"[lifelike.initiative] merge scope={scope_key} failed: {type(e).__name__}")
        return out

    @staticmethod
    def _is_pool_id(item_id: Any) -> bool:
        return isinstance(item_id, str) and item_id.startswith("cand:")

    async def _note_turn_end(self, turn_id: str, state: str, reason: str) -> None:
        coord = self.initiative
        if coord is None:
            return
        try:
            await coord.note_turn_end(turn_id, state, reason)
        except Exception as e:
            logger.warning(f"[lifelike.initiative] note_turn_end failed: {type(e).__name__}")

    async def review_speech(self, turn_id: str, text: str) -> tuple[bool, str]:
        """活人感 P1-8/P1-12：pool 候选生成文本的发送前检查（来源标注等）；未接入 → 通过"""
        coord = self.initiative
        if coord is None:
            return True, ""
        try:
            return await coord.review_speech(turn_id, text)
        except Exception as e:
            logger.warning(f"[lifelike.initiative] review turn={turn_id} failed: {type(e).__name__}")
            return False, "review_error"

    async def has_urgent_candidate(self, scope_key: str, now: float | None = None) -> bool:
        """活人感 P1-12：危机关心（源消息 30 分钟内）——活动窗口推迟对它不生效"""
        coord = self.initiative
        if coord is None or not self._is_owner_private(scope_key):
            return False
        try:
            return bool(await coord.has_urgent(scope_key, self._now() if now is None else float(now)))
        except Exception:
            return False

    async def has_push_candidate(self, scope_key: str, now: float | None = None) -> bool:
        """活人感 P1-8：候选池里是否有通过闸门、可推送的候选（空窗时允许调度用）。

        调度主循环每轮都会问：结果按 scope 缓存 PUSH_CHECK_TTL_S（开回合时清掉），
        避免每分钟都跑一遍候选池过期 / 结算 / 闸门查询（审查 #8）。"""
        coord = self.initiative
        if coord is None or coord.mode() != "on" or not self._is_owner_private(scope_key):
            return False
        now = self._now() if now is None else float(now)
        hit = self._push_check_cache.get(scope_key)
        if hit is not None and 0 <= now - hit[0] < PUSH_CHECK_TTL_S:
            return hit[1]
        try:
            merged = await coord.merge_candidates(scope_key, now, [])
        except Exception:
            return False
        ok = any(self._is_pool_id(c.get("item_id")) for c in merged)
        self._push_check_cache[scope_key] = (now, ok)
        return ok

    async def avoid_topics(self, scope_key: str, now: float | None = None) -> list[str]:
        """最近 30 天内同受众的 declined/done/cancelled 事项话题（去重，只有话题文本）"""
        now = self._now() if now is None else float(now)
        try:
            items = await self._db(self._store.list_items, None, _AVOID_STATUSES, scope_key, 100)
        except Exception as e:
            logger.warning(f"[continuity] avoid_topics scope={scope_key} failed: {type(e).__name__}")
            return []
        items = sorted(items, key=lambda it: -float(it.get("updated_at") or 0))
        # 与当前仍有效的事项话题重叠的不列入（避免“不要提面试”与“面试结果”候选自相矛盾）
        try:
            active = await self._db(self._store.list_items, scope_key, ("active",), scope_key, 100)
        except Exception:
            active = []
        active_keys = [k for k in (m.norm_topic(str(a.get("topic") or "")) for a in active) if len(k) >= 2]
        out: list[str] = []
        seen: set[str] = set()
        for it in items:
            if it.get("status") not in _AVOID_STATUSES:
                continue
            if scope_key not in (it.get("allowed_audiences") or []):
                continue
            if now - float(it.get("updated_at") or 0) > AVOID_LOOKBACK_S:
                continue
            topic = m.clip(" ".join(str(it.get("topic") or "").split()), 40)
            key = m.norm_topic(topic)
            if not key or key in seen:
                continue
            if any(key == k or key in k or k in key for k in active_keys):
                continue
            seen.add(key)
            out.append(topic)
            if len(out) >= AVOID_LIMIT:
                break
        return out

    # ------------------------------------------------------------ turns

    async def begin_turn(self, scope_key: str, forced: bool = False, *, free: bool = False) -> TurnStart:
        """§5.4：开回合；跳过时 turn=None 并给出 reason（已记录日志）

        free=True（活人感 U8 主人私聊自由主动）：不执行收尾冷却（收尾作为事实交给她判断）。"""
        if scope_key in self._active:
            self._log(None, scope=scope_key, decision="skip", reason="turn_active")
            return TurnStart(None, "turn_active")
        self._active[scope_key] = ""  # 同步占位，防止并发 tick 重入
        try:
            now = self._now()
            in_flight = await self._db(self._store.turns_in_state, _IN_FLIGHT, scope_key)
            if in_flight:
                self._active.pop(scope_key, None)
                self._log(None, scope=scope_key, decision="skip", reason="turn_active")
                return TurnStart(None, "turn_active")
            pending = await self._db(self._store.turns_in_state, (m.TURN_UNCERTAIN,), scope_key)
            if pending:
                self._active.pop(scope_key, None)
                self._log(None, scope=scope_key, decision="skip", reason="uncertain_pending")
                return TurnStart(None, "uncertain_pending")
            cands = await self.candidates(scope_key, now)
            if not forced and not (free and self._is_owner_private(scope_key)):
                closure = await self._db(self._store.get_closure, scope_key)
                cooldown_s = max(0, int(_resolve(self._closure_cooldown_minutes, 120) or 0)) * 60
                if (
                    closure
                    and closure.get("state") == "ended"
                    and closure.get("ended_at") is not None
                    and now - float(closure["ended_at"]) < cooldown_s
                    and not any(c.get("remind_requested") for c in cands)
                ):
                    self._active.pop(scope_key, None)
                    self._log(None, scope=scope_key, decision="skip", reason="closure_cooldown")
                    return TurnStart(None, "closure_cooldown")
            wm = await self._db(self._store.get_watermark, scope_key) or {}
            turn = await self._db(
                self._store.create_turn,
                scope_key,
                input_msg_id=int(wm.get("last_msg_id") or 0),
                session_version=int(wm.get("session_version") or 0),
                forced=bool(forced),
            )
            self._active[scope_key] = turn["turn_id"]
            self._push_check_cache.pop(scope_key, None)
            if self.initiative is not None:
                try:
                    self.initiative.note_turn_begin(scope_key, turn["turn_id"])
                except Exception as e:
                    logger.warning(f"[lifelike.initiative] note_turn_begin failed: {type(e).__name__}")
            self._log(turn, decision="begin", candidates=len(cands), forced=int(bool(forced)))
            return TurnStart(turn, "", cands)
        except Exception as e:
            self._active.pop(scope_key, None)
            logger.warning(f"[continuity] begin_turn scope={scope_key} failed: {type(e).__name__}")
            return TurnStart(None, "bridge_error")

    async def mark_turn(self, turn_id: str, state: str, **fields: Any) -> dict[str, Any] | None:
        if self.initiative is not None and fields.get("item_id"):
            try:
                await self.initiative.note_chosen(turn_id, fields.get("item_id"))
            except Exception as e:
                logger.warning(f"[lifelike.initiative] note_chosen failed: {type(e).__name__}")
        try:
            return await self._db(self._store.update_turn, turn_id, state=state, **fields)
        except Exception as e:
            logger.warning(f"[continuity] mark_turn turn={turn_id} failed: {type(e).__name__}")
            return None

    def _release_active(self, turn: dict[str, Any]) -> None:
        if self._active.get(turn.get("scope_key")) == turn.get("turn_id"):
            self._active.pop(turn["scope_key"], None)

    async def close_turn(self, turn_id: str, state: str, reason: str = "") -> dict[str, Any] | None:
        """结束未发送的回合（skipped/cancelled/failed）；已终结的回合不改动（幂等）"""
        try:
            turn = await self._db(self._store.get_turn, turn_id)
            if turn is None:
                return None
        except Exception as e:
            logger.warning(f"[continuity] close_turn turn={turn_id} failed: {type(e).__name__}")
            return None
        try:
            if turn["state"] not in _IN_FLIGHT:
                return turn
            # 已经有出站行的回合（发送中被取消/异常）必须走 finish_turn；
            # 引擎此时未记账，sent 段由此计入额度（不设 pending）
            rows = await self._db(self._store.outbound_for_turn, turn_id)
            if rows:
                res = await self.finish_turn(turn_id, stop_reason=reason, charge_state=True)
                return await self._db(self._store.get_turn, res.turn_id)
            turn = await self._db(self._store.update_turn, turn_id, state=state, reason=reason)
            self._log(turn, decision=state, reason=reason or "-", llm=turn.get("llm_calls", 0))
            await self._note_turn_end(turn_id, state, reason)
            return turn
        except Exception as e:
            logger.warning(f"[continuity] close_turn turn={turn_id} failed: {type(e).__name__}")
            return None
        finally:
            # 终态落盘之后才释放进程内占位（对账据此跳过进行中的回合）
            self._release_active(turn)

    # ------------------------------------------------------------ context

    async def speech_context(self, scope_key: str, focus_item_id: str | None) -> str:
        if self._speech_context_fn is None:
            return ""
        try:
            return str(await _maybe_await(self._speech_context_fn(scope_key, focus_item_id)) or "")
        except Exception as e:
            logger.warning(f"[continuity] speech_context scope={scope_key} failed: {type(e).__name__}")
            return ""

    # ------------------------------------------------------------ target guard

    def _policy(self) -> SendPolicy:
        try:
            pol = self._policy_get()
            if isinstance(pol, SendPolicy):
                return pol
        except Exception as e:
            logger.warning(f"[continuity] policy_get failed: {type(e).__name__}")
        # 策略不可用 → 最保守：没有主人、没有群白名单（全部拒发）
        return SendPolicy(group_whitelist=frozenset())

    def check_target(self, scope_key: str, umo: str | None) -> str:
        """发送目标硬拦截；返回拒绝原因，允许时返回 ''"""
        pol = self._policy()
        scope_key = str(scope_key or "").strip()
        if not scope_key:
            return REASON_BAD_SCOPE
        forbidden: set[str] = set()
        for c in set(DEFAULT_FORBIDDEN_CHATS) | {str(x) for x in pol.forbidden_chats}:
            forbidden |= _chat_variants(c)
        silent = {str(s).strip() for s in pol.silent_sessions}
        parsed = parse_umo(umo)
        if parsed is None:
            return REASON_UMO_MISMATCH
        _, mtype, session_id = parsed
        peer = session_id.partition("#")[0].strip()
        if scope_key in silent or peer in silent or str(umo) in silent:
            return REASON_SILENT
        if _chat_variants(peer) & forbidden or _chat_variants(scope_key) & forbidden:
            return REASON_FORBIDDEN
        if scope_key.startswith("private:"):
            uid = scope_key.split(":", 1)[1].strip()
            if not uid or uid not in {str(o).strip() for o in pol.owner_ids}:
                return REASON_NOT_OWNER
            if mtype != "FriendMessage" or session_id != uid:
                return REASON_UMO_MISMATCH
            return ""
        allowed = pol.group_whitelist
        if allowed is None:
            try:
                allowed = frozenset(self._state.get_whitelist())
            except Exception:
                allowed = frozenset()
        if scope_key not in {str(g) for g in allowed} and not self._group_allowed_extra(scope_key):
            return REASON_GROUP_NOT_ALLOWED
        if mtype != "GroupMessage" or peer != scope_key:
            return REASON_UMO_MISMATCH
        return ""

    def _group_allowed_extra(self, scope_key: str) -> bool:
        fn = self.group_allow
        if fn is None:
            return False
        try:
            return bool(fn(scope_key))
        except Exception:
            return False

    async def presend_check(self, turn_id: str, umo: str | None, *, paused: bool = False) -> PresendResult:
        """§5.7：每段发送前复查（回合仍进行中、目标允许、水位未变、焦点仍 active、未暂停）"""
        try:
            turn = await self._db(self._store.get_turn, turn_id)
            if turn is None or turn["state"] not in _IN_FLIGHT:
                return PresendResult(False, REASON_TURN_CLOSED)
            scope = turn["scope_key"]
            reason = self.check_target(scope, umo)
            if reason:
                self._log(turn, decision="refuse", reason=reason)
                return PresendResult(False, reason)
            wm = await self._db(self._store.get_watermark, scope) or {}
            if int(wm.get("last_msg_id") or 0) != int(turn["input_msg_id"] or 0) or int(
                wm.get("session_version") or 0
            ) != int(turn["session_version"] or 0):
                return PresendResult(False, REASON_NEW_INBOUND)
            if self._is_pool_id(turn.get("item_id")):
                # 活人感 P1-8：焦点是候选池条目 → 复查其仍未结、未过期（协调器缺失 → 拒发）
                coord = self.initiative
                ok = coord is not None and await self._db(coord.presend_ok, turn["item_id"])
                if not ok:
                    return PresendResult(False, REASON_FOCUS_INACTIVE)
            elif turn.get("item_id"):
                item = await self._db(self._store.get_item, turn["item_id"])
                if item is None or item.get("status") != m.STATUS_ACTIVE:
                    return PresendResult(False, REASON_FOCUS_INACTIVE)
            if paused:
                return PresendResult(False, REASON_PAUSED)
            return PresendResult(True)
        except Exception as e:
            logger.warning(f"[continuity] presend_check turn={turn_id} failed: {type(e).__name__}")
            return PresendResult(False, "presend_error")

    # ------------------------------------------------------------ send

    def _platform_for(self, umo: str) -> Any:
        parsed = parse_umo(umo)
        if parsed is None:
            return None
        return resolve_platform(self._context, parsed[0])

    async def send_part(self, turn_id: str, umo: str, text: str, idx: int) -> SendOutcome:
        """发送一段：草稿落盘 → 目标再校验 → sending 落盘 → send_with_receipt"""
        turn = await self._db(self._store.get_turn, turn_id)
        if turn is None:
            return SendOutcome(None, m.OUT_CANCELLED, error=REASON_TURN_CLOSED)
        scope = turn["scope_key"]
        item_ids = (turn["item_id"],) if turn.get("item_id") and not self._is_pool_id(turn["item_id"]) else ()
        try:
            oid = await self._ledger.create_draft(scope, text, turn_id=turn_id, part_index=idx, item_ids=item_ids)
        except Exception as e:
            logger.warning(f"[continuity] send_part turn={turn_id} draft failed: {type(e).__name__}")
            return SendOutcome(None, m.OUT_FAILED, error="persist_failed")
        # 纵深防御：即便调用方跳过 presend_check，禁发目标也绝不发送
        reason = self.check_target(scope, umo)
        if reason:
            await self._ledger.mark(oid, m.OUT_CANCELLED, error=reason)
            self._log(turn, decision="refuse", reason=reason)
            return SendOutcome(oid, m.OUT_CANCELLED, error=reason)
        if turn["state"] != m.TURN_SENDING:
            await self.mark_turn(turn_id, m.TURN_SENDING)
        outcome = await self._ledger.deliver(oid, scope, self._platform_for(umo), umo, text)
        self._log(turn, decision="send", part=idx, status=outcome.status, sent=outcome.tg_msg_ids or "-")
        if self._after_sent is not None and outcome.status == m.OUT_SENT:
            try:
                await self._after_sent(scope, oid, text)
            except Exception as e:
                logger.warning(f"[continuity] after_sent hook failed: {type(e).__name__}")
        return outcome

    # ------------------------------------------------------------ accounting

    async def _note_item_mention(self, item_id: str | None, ids: Sequence[int], when: float) -> None:
        if not item_id:
            return
        for _ in range(2):
            try:
                item = await self._db(self._store.get_item, item_id)
                if item is None:
                    return
                await self._db(
                    self._store.update_item,
                    item_id,
                    expected_revision=item["revision"],
                    reason="proactive_mention",
                    source_msg_id=str(ids[0]) if ids else None,
                    last_mentioned_at=when,
                    ask_count=int(item.get("ask_count") or 0) + 1,
                    last_outbound_msg_ids=list(ids),
                )
                return
            except Exception as e:
                if type(e).__name__ == "RevisionConflict":
                    continue
                logger.warning(f"[continuity] item mention update item={item_id} failed: {type(e).__name__}")
                return

    async def _reserve(self, turn: dict[str, Any], *, cursor: bool) -> None:
        try:
            async with self._state.get_lock(turn["scope_key"]):
                ts = self._state.reserve_initiate(turn["scope_key"])
            if cursor:
                await self._db(self._store.set_cursor, _quota_cursor(turn["turn_id"]), repr(float(ts)))
        except Exception as e:
            logger.warning(f"[continuity] reserve quota turn={turn['turn_id']} failed: {type(e).__name__}")
        await self._save()

    async def finish_turn(
        self, turn_id: str, *, stop_reason: str = "", charge_state: bool = False
    ) -> TurnResult:
        """§5.9：根据出站行结算回合（幂等：已终结回合只返回结果）

        有 sent 段时额度由调用方（引擎 record_initiate）计 1 次；charge_state=True
        （崩溃遗留回合，引擎未记账）时改由此处 reserve_initiate 计入额度（不设 pending）。
        """
        turn = await self._db(self._store.get_turn, turn_id)
        if turn is None:
            return TurnResult(turn_id, m.TURN_FAILED, "turn_missing")
        try:
            return await self._finish_turn_inner(turn, stop_reason, charge_state)
        finally:
            # 终态 update_turn 落盘之后才释放 _active，避免对账在收尾途中抢先结算
            self._release_active(turn)

    async def _finish_turn_inner(
        self, turn: dict[str, Any], stop_reason: str, charge_state: bool
    ) -> TurnResult:
        turn_id = turn["turn_id"]
        rows = await self._db(self._store.outbound_for_turn, turn_id)
        for r in rows:
            if r["status"] == m.OUT_DRAFT:
                await self._ledger.mark(r["id"], m.OUT_CANCELLED, error=stop_reason or "not_sent")
                r["status"] = m.OUT_CANCELLED
        sent = [r for r in rows if r["status"] == m.OUT_SENT]
        uncertain = [r for r in rows if r["status"] in (m.OUT_UNCERTAIN, m.OUT_SENDING)]
        sent_ids = [i for r in sent for i in (r.get("tg_msg_ids") or [])]
        result = TurnResult(
            turn_id,
            turn["state"],
            stop_reason,
            [r["text"] for r in sent],
            sent_ids,
            len(uncertain),
            turn.get("quota_state") or "none",
        )
        if turn["state"] not in _IN_FLIGHT:
            return result
        parts_total = max(int(turn.get("parts_total") or 0), len(rows))
        if sent:
            complete = len(sent) == parts_total and not uncertain and not stop_reason
            result.state = m.TURN_SENT if complete else m.TURN_PARTIAL
            result.quota_state = "charged"
            if charge_state:
                await self._reserve(turn, cursor=False)
            await self._note_item_mention(turn.get("item_id"), sent_ids, self._now())
        elif uncertain:
            result.state = m.TURN_UNCERTAIN
            result.quota_state = "reserved"
            await self._reserve(turn, cursor=True)
        else:
            attempted = any(r["status"] == m.OUT_FAILED for r in rows)
            result.state = m.TURN_FAILED if attempted else m.TURN_CANCELLED
            result.quota_state = "none"
            if not result.reason:
                result.reason = next((r.get("error") for r in rows if r.get("error")), "") or "not_sent"
        try:
            await self._db(
                self._store.update_turn,
                turn_id,
                state=result.state,
                reason=result.reason,
                quota_state=result.quota_state,
                parts_sent=len(sent),
                parts_total=parts_total,
            )
        except Exception as e:
            logger.warning(f"[continuity] finish_turn turn={turn_id} update failed: {type(e).__name__}")
        self._log(
            turn,
            decision=result.state,
            reason=result.reason or "-",
            llm=turn.get("llm_calls", 0),
            sent=sent_ids or "-",
        )
        await self._note_turn_end(turn_id, result.state, result.reason)
        return result

    async def owner_replied_since(self, turn_id: str) -> bool:
        """回合开始后本人是否已有新私聊消息（水位 / 会话版本推进）"""
        try:
            turn = await self._db(self._store.get_turn, turn_id)
            if turn is None:
                return False
            wm = await self._db(self._store.get_watermark, turn["scope_key"]) or {}
        except Exception as e:
            logger.warning(f"[continuity] owner_replied_since turn={turn_id} failed: {type(e).__name__}")
            return False
        return int(wm.get("last_msg_id") or 0) > int(turn.get("input_msg_id") or 0) or int(
            wm.get("session_version") or 0
        ) != int(turn.get("session_version") or 0)

    # ------------------------------------------------------------ reconcile

    async def _client_for(self, scope_key: str, client_getter: Any) -> Any:
        getter = client_getter or self._client_getter
        if getter is not None:
            return await _maybe_await(getter(scope_key))
        umo = self._umo_get(scope_key) if self._umo_get else None
        platform = self._platform_for(umo) if umo else None
        get_client = getattr(platform, "get_client", None)
        return get_client() if callable(get_client) else None

    def _peer_for(self, scope_key: str) -> int | None:
        if scope_key.startswith("private:"):
            try:
                return int(scope_key.split(":", 1)[1])
            except ValueError:
                return None
        if self._umo_get is not None:
            try:
                peer = peer_of_umo(self._umo_get(scope_key))
                if peer is not None:
                    return peer
            except Exception:
                pass
        try:
            return int(scope_key)
        except ValueError:
            return None

    async def reconcile(self, client_getter: Any = None, *, force_query: bool = False) -> dict[str, int]:
        """§5.10：sending → uncertain；uncertain 经 get_messages 匹配 → sent / failed / 保持"""
        stats = {"stale": 0, "sent": 0, "failed": 0, "kept": 0}
        if self._reconcile_lock.locked():
            return stats
        async with self._reconcile_lock:
            try:
                await self._reconcile_inner(client_getter, force_query, stats)
            except Exception as e:
                logger.warning(f"[continuity] reconcile failed: {type(e).__name__}")
        if any(stats.values()):
            logger.info(
                "[continuity] reconcile stale={stale} sent={sent} failed={failed} kept={kept}".format(**stats)
            )
        return stats

    async def _reconcile_inner(self, client_getter: Any, force_query: bool, stats: dict[str, int]) -> None:
        active = {t for t in self._active.values() if t}
        now = self._now()
        # 启动对账（force_query）时本进程尚无进行中回合，不需要宽限
        grace = 0.0 if force_query else RECONCILE_GRACE_S

        def _recent(row: dict[str, Any]) -> bool:
            return grace > 0 and now - float(row.get("updated_at") or 0) < grace

        # 1) 上次进程遗留的进行中回合 / 出站
        for t in await self._db(self._store.turns_in_state, _IN_FLIGHT):
            if t["turn_id"] in active or _recent(t):
                continue
            rows = await self._db(self._store.outbound_for_turn, t["turn_id"])
            for r in rows:
                if r["status"] == m.OUT_SENDING:
                    await self._ledger.mark(r["id"], m.OUT_UNCERTAIN, error="stale_sending")
                    stats["stale"] += 1
            await self.finish_turn(t["turn_id"], stop_reason="stale_restart", charge_state=True)
        for r in await self._db(self._store.outbound_in_state, (m.OUT_SENDING,)):
            if r.get("origin") != "proactive" or r.get("turn_id") in active or _recent(r):
                continue
            await self._ledger.mark(r["id"], m.OUT_UNCERTAIN, error="stale_sending")
            stats["stale"] += 1

        # 2) uncertain 出站对账
        pending = [
            r
            for r in await self._db(self._store.outbound_in_state, (m.OUT_UNCERTAIN,))
            if r.get("origin") == "proactive" and r.get("turn_id") not in active
        ]
        touched: set[str] = set()
        matched_ts: dict[str, float] = {}  # turn_id -> 对账匹配到的消息发送时间（最新一段）
        by_scope: dict[str, list[dict[str, Any]]] = {}
        for r in pending:
            by_scope.setdefault(r["scope_key"], []).append(r)
        for scope, rows in by_scope.items():
            if not force_query and now - self._last_query.get(scope, float("-inf")) < self._reconcile_query_interval_s:
                stats["kept"] += len(rows)
                continue
            self._last_query[scope] = now
            messages = None
            try:
                client = await self._client_for(scope, client_getter)
                peer = self._peer_for(scope)
                if client is not None and peer is not None:
                    messages = list(await client.get_messages(peer, limit=RECONCILE_FETCH_LIMIT) or [])
            except Exception as e:
                logger.warning(f"[continuity] reconcile query scope={scope} failed: {type(e).__name__}")
                messages = None
            if messages is None:
                stats["kept"] += len(rows)
                continue
            for r in rows:
                turn = await self._db(self._store.get_turn, r["turn_id"]) if r.get("turn_id") else None
                base = float(turn["created_at"]) if turn else float(r["created_at"])
                bound = []
                for msg in messages:
                    mid = getattr(msg, "id", None)
                    if isinstance(mid, int):
                        owner = await self._db(self._store.outbound_by_tg_id, scope, mid)
                        if owner is not None and int(owner["id"]) != int(r["id"]):
                            bound.append(mid)
                mid = match_sent_message(messages, r["text"], base - RECONCILE_MATCH_SLACK_S, bound)
                if mid is not None:
                    await self._ledger.mark(r["id"], m.OUT_SENT, [mid], error="reconciled")
                    stats["sent"] += 1
                    msg_ts = next(
                        (message_timestamp(x) for x in messages if getattr(x, "id", None) == mid),
                        None,
                    )
                    if r.get("turn_id") and msg_ts is not None:
                        matched_ts[r["turn_id"]] = max(matched_ts.get(r["turn_id"], 0.0), float(msg_ts))
                elif now - float(r["created_at"]) > RECONCILE_FAIL_AFTER_S:
                    await self._ledger.mark(r["id"], m.OUT_FAILED, error="reconcile_no_match")
                    stats["failed"] += 1
                else:
                    stats["kept"] += 1
                    continue
                if r.get("turn_id"):
                    touched.add(r["turn_id"])

        # 3) 结算 uncertain 回合（含出站已全部有结论的回合）
        for t in await self._db(self._store.turns_in_state, (m.TURN_UNCERTAIN,)):
            if t["turn_id"] not in active:
                touched.add(t["turn_id"])
        for turn_id in touched:
            await self._settle_turn(turn_id, matched_ts.get(turn_id))

    async def _settle_turn(self, turn_id: str, sent_ts: float | None = None) -> None:
        turn = await self._db(self._store.get_turn, turn_id)
        if turn is None:
            return
        rows = await self._db(self._store.outbound_for_turn, turn_id)
        sent = [r for r in rows if r["status"] == m.OUT_SENT]
        unresolved = [r for r in rows if r["status"] in (m.OUT_UNCERTAIN, m.OUT_SENDING)]
        sent_ids = [i for r in sent for i in (r.get("tg_msg_ids") or [])]
        if turn["state"] != m.TURN_UNCERTAIN:
            if int(turn.get("parts_sent") or 0) != len(sent):
                await self.mark_turn(turn_id, turn["state"], parts_sent=len(sent))
            return
        scope = turn["scope_key"]
        if sent:
            # 预占的那次额度转为正式计费
            await self._db(self._store.set_cursor, _quota_cursor(turn_id), "")
            complete = len(sent) == int(turn.get("parts_total") or len(rows)) and not unresolved
            settled = await self.mark_turn(
                turn_id,
                m.TURN_SENT if complete else m.TURN_PARTIAL,
                quota_state="charged",
                parts_sent=len(sent),
                reason="reconciled",
            )
            await self._note_item_mention(turn.get("item_id"), sent_ids, self._now())
            pending = "-"
            if settled is not None:
                # 回合已离开 uncertain（终态已落盘），之后不会再进入此分支 → 接话记账只做一次
                pending = await self._account_reconciled_pending(turn, sent, sent_ts)
            self._log(turn, decision="reconciled_sent", sent=sent_ids, pending=pending)
            await self._save()
            return
        if unresolved:
            return
        raw = await self._db(self._store.get_cursor, _quota_cursor(turn_id))
        released = False
        if raw:
            try:
                async with self._state.get_lock(scope):
                    released = self._state.release_initiate_reservation(scope, float(raw))
            except Exception as e:
                logger.warning(f"[continuity] release quota turn={turn_id} failed: {type(e).__name__}")
            await self._db(self._store.set_cursor, _quota_cursor(turn_id), "")
        await self.mark_turn(
            turn_id,
            m.TURN_FAILED,
            quota_state="released",
            reason="reconcile_no_match",
        )
        self._log(turn, decision="reconciled_failed", released=int(released))
        await self._save()

    async def _account_reconciled_pending(
        self, turn: dict[str, Any], sent: list[dict[str, Any]], sent_ts: float | None
    ) -> str:
        """对账确认送达的回合补做接话记账：窗口内 → pending=发送时间；已超时 → 计一次未回复；
        本人已有新消息 → 两者都不做。返回动作名（日志用）。"""
        scope = turn["scope_key"]
        if sent_ts is None:
            known = [float(r["sent_at"]) for r in sent if r.get("sent_at") is not None]
            sent_ts = max(known) if known else float(turn.get("created_at") or self._now())
        if await self.owner_replied_since(turn["turn_id"]):
            return "owner_replied"
        try:
            timeout_s = float(self._state._config.proactive_pending_timeout) * 60.0
        except Exception:
            timeout_s = 30 * 60.0
        try:
            async with self._state.get_lock(scope):
                if self._now() - float(sent_ts) < timeout_s:
                    self._state.mark_initiate_pending(scope, float(sent_ts))
                    return "pending"
                self._state.record_initiate_unanswered(scope, force=True)
                return "unanswered"
        except Exception as e:
            logger.warning(f"[continuity] reconcile pending turn={turn['turn_id']} failed: {type(e).__name__}")
            return "error"

    # ------------------------------------------------------------ owner resume

    def _is_owner_private(self, scope_key: str) -> bool:
        if not str(scope_key).startswith("private:"):
            return False
        uid = str(scope_key).split(":", 1)[1].strip()
        return uid in {str(o).strip() for o in self._policy().owner_ids}

    def resume_if_advanced(self, scope_key: str, advanced: bool) -> bool:
        """水位真实推进的主人私聊消息 → 恢复主动（清 streak / pending）；重放不恢复"""
        if not advanced or not self._is_owner_private(scope_key):
            return False
        try:
            changed = self._state.resume_after_owner_message(scope_key)
        except Exception as e:
            logger.warning(f"[continuity] resume scope={scope_key} failed: {type(e).__name__}")
            return False
        if changed:
            logger.info(f"[continuity] scope={scope_key} proactive resumed by owner message")
        return True

    async def note_owner_message(self, scope_key: str, msg_id: int, msg_ts: float) -> bool:
        """推进水位并按需恢复；返回 advanced。服务层已自行推进水位时改用 resume_if_advanced"""
        try:
            advanced, _ = await self._db(self._store.advance_watermark, scope_key, int(msg_id), float(msg_ts))
        except Exception as e:
            logger.warning(f"[continuity] advance_watermark scope={scope_key} failed: {type(e).__name__}")
            return False
        self.resume_if_advanced(scope_key, advanced)
        return bool(advanced)

    async def owner_status(self, scope_key: str) -> dict[str, Any]:
        """活人感 U8：自由主动决策用的事实（收尾时间、主人说在忙到何时）；失败或非主人私聊 → {}"""
        if not self._is_owner_private(scope_key):
            return {}
        out: dict[str, Any] = {}
        try:
            closure = await self._db(self._store.get_closure, scope_key)
            if closure and closure.get("state") == "ended" and closure.get("ended_at") is not None:
                out["closure_ended_at"] = float(closure["ended_at"])
        except Exception as e:
            logger.warning(f"[continuity] owner_status scope={scope_key} closure failed: {type(e).__name__}")
        coord = self.initiative
        busy = getattr(coord, "_busy_until", None) if coord is not None else None
        if isinstance(busy, dict) and busy.get(scope_key) is not None:
            try:
                out["busy_until"] = float(busy[scope_key])
            except (TypeError, ValueError):
                pass
        if await self.relation_cooling(scope_key):
            out["cooling"] = True  # 只给定性事实（"上次聊天有点不愉快"），不带分值
        return out

    async def relation_cooling(self, scope_key: str) -> bool:
        """主人私聊是否处于关系冷静期（只读；relation_mode≠on / 未接线 / 异常 → False = 73edc2c）"""
        if not self._is_owner_private(scope_key):
            return False
        coord = self.initiative
        view_fn = getattr(coord, "relation_view", None) if coord is not None else None
        if view_fn is None:
            return False
        try:
            rel = await asyncio.to_thread(view_fn, scope_key, self._now())
        except Exception as e:
            logger.warning(f"[continuity] relation_cooling scope={scope_key} failed: {type(e).__name__}")
            return False
        return bool(getattr(rel, "cooling", False)) if rel is not None else False

    # ------------------------------------------------------------ activity（活人感 P0-8）

    def is_owner_private_scope(self, scope_key: str) -> bool:
        """主人私聊 scope（private:<白名单用户>）；活动窗口判定 / 空窗判定只作用于它"""
        try:
            return self._is_owner_private(scope_key)
        except Exception:
            return False

    async def activity_snapshot(self, scope_key: str, lookback_days: int, *, with_history: bool = True):
        """读主人作息时间戳与最后互动时间（只读时间戳）。失败向上抛，由调用方回退旧行为。"""
        from ..proactive.activity_gate import ActivitySnapshot

        now = self._now()
        last = await self._db(self._store.last_interaction_ts, scope_key)
        stamps: list[float] = []
        if with_history:
            owner = str(scope_key).split(":", 1)[1].strip() if ":" in str(scope_key) else None
            stamps = await self._db(
                self._store.activity_timestamps,
                scope_key,
                now - max(1, int(lookback_days)) * 86400.0,
                owner or None,
            )
        return ActivitySnapshot(owner_timestamps=list(stamps), last_interaction=last)

    # ------------------------------------------------------------ windows

    async def save_windows(self, sliding_window: Any, whitelist: Iterable[str]) -> int:
        """把白名单会话的 SlidingWindow 落盘（只写有变化的；丢弃 >24h 消息）"""
        cutoff = self._now() - WINDOW_MAX_AGE_S
        writes: dict[str, list[dict[str, Any]]] = {}
        for scope in list(whitelist or ()):
            try:
                msgs = sliding_window.get_messages(scope)
            except Exception:
                continue
            payload = [
                {
                    "sender_id": str(x.sender_id),
                    "sender_name": str(x.sender_name),
                    "content": str(x.content),
                    "timestamp": float(x.timestamp),
                }
                for x in msgs
                if float(x.timestamp) >= cutoff
            ]
            digest = hashlib.sha1(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()
            if self._window_digest.get(scope) == digest:
                continue
            writes[scope] = payload
            self._window_digest[scope] = digest
        saved = 0
        for scope, payload in writes.items():
            try:
                await self._db(self._store.save_window, scope, payload)
                saved += 1
            except Exception as e:
                self._window_digest.pop(scope, None)
                logger.warning(f"[continuity] save_window scope={scope} failed: {type(e).__name__}")
        return saved

    async def restore_windows(self, sliding_window: Any) -> int:
        """启动时恢复窗口：只追加比内存中最新一条更新、且不超过 24h 的消息（可重复调用）"""
        from ..proactive.perception import WindowMessage

        try:
            snapshots = await self._db(self._store.load_windows)
        except Exception as e:
            logger.warning(f"[continuity] load_windows failed: {type(e).__name__}")
            return 0
        cutoff = self._now() - WINDOW_MAX_AGE_S
        restored = 0
        for scope, payload in snapshots.items():
            try:
                existing = sliding_window.get_messages(scope)
                latest = max((x.timestamp for x in existing), default=float("-inf"))
                for d in payload:
                    ts = float(d.get("timestamp") or 0)
                    if ts < cutoff or ts <= latest:
                        continue
                    sliding_window.append(
                        scope,
                        WindowMessage(
                            sender_id=str(d.get("sender_id") or ""),
                            sender_name=str(d.get("sender_name") or ""),
                            content=str(d.get("content") or ""),
                            timestamp=ts,
                        ),
                    )
                    latest = ts
                    restored += 1
            except Exception as e:
                logger.warning(f"[continuity] restore_window scope={scope} failed: {type(e).__name__}")
        if restored:
            logger.info(f"[continuity] restored {restored} window messages from {len(snapshots)} scopes")
        return restored
