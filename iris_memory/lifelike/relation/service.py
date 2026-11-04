"""RelationService：关系事件溯源 + 可恢复反馈（1、2 级）+ 关系渐进的编排（P1-6、P1-11）。

模式（HiddenConfig 平铺键）：

- ``lifelike_relation_mode``：``off`` 不读写任何状态、不建表、不调模型（与 3ad413e 一致）；
  ``shadow``（默认）记录事件 / 模式 / 阶段并写日志，**不注入 prompt、不改变任何行为**；
  ``on`` 在主人私聊注入 ``<你们之间>``。
- ``lifelike_bond_mode``：同上三档，实际取值 = min(bond_mode, relation_mode)。shadow 下每日折叠只写
  ``bond_transitions(status=proposed, track=shadow)``。
- ``lifelike_relation_levels``：P1 只认 ``soften``、``boundary``（拍板 D8）。
- ``lifelike_relation_detector_daily_cap``：本模块廉价模型每日调用上限（另受全局硬预算约束）。
- ``lifelike_bond_start_stage`` / ``lifelike_bond_promote_approval``。

不变量：
- 关系 / 阶段的每次变化都有原因事件（RelationRepo 校验）；
- 冷静期只影响"怎么说"：被动回复照常、不刻意缩短；主人难受/生病/危机（私聊或他本人在群里说）
  立即解除所有模式；
- U8（11-03 用户拍板）：关系状态**不**减少主动次数（不冷落）。``gate_view_sync`` 给主动候选池的
  只是文字枚举，用于排序、发送前措辞检查与 prompt 文字，闸门不据此拦截；
- 只作用于主人私聊 ``private:<owner>``；群消息只用于"立即解除"，不存原文、不进任何 prompt；
- 日志只记 id / 枚举 / 计数，不记正文；任何异常 → 不写、不注入（等于旧行为）。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from astrbot.api import logger

from ...continuity import models as cm
from ..budget import CheapBudget, CheapBudgetExceeded
from . import bond as bf
from . import detector as det
from . import fold as fd
from . import models as rm
from . import narrator
from .repo import RelationRepo, valid_scope

__all__ = ["RelationService"]

LLMGenerate = Callable[[str, float], Awaitable[str]]

C_LLM_CALLS = "lifelike.relation.llm_calls"
C_EVENTS = "lifelike.relation.events"
C_RELEASE = "lifelike.relation.release"
REUNION_GAP_S = 45 * rm.DAY
REUNION_SHOW_S = 2 * rm.HOUR
SAME_KIND_COOLDOWN_S = rm.HOUR  # 同类事件 1 小时内只落一条（存储层防刷；折叠另有 24h 去重）
GROUP_DISTRESS_COOLDOWN_S = 6 * rm.HOUR
EVENTS_LOOKBACK_S = 120 * rm.DAY
BREAKER_FAILS = 5
BREAKER_PAUSE_S = 3600.0
MAX_TASKS = 2
DEFAULT_TIMEOUT_S = 30.0
OUTPUT_RESERVE = 80
CURSOR_REUNION = "lifelike.relation.reunion:"
CURSOR_PROMISE = "lifelike.relation.promise:"
CURSOR_BOND_DAY = "lifelike.bond.last_eval:"


class RelationService:
    def __init__(
        self,
        store: Any,
        config_get: Callable[[str, Any], Any],
        *,
        clock: Callable[[], float] = time.time,
        llm_generate: LLMGenerate | None = None,
        budget: CheapBudget | None = None,
        usage_probe: Callable[[str], Awaitable[int | None]] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._store = store
        self._get = config_get
        self._clock = clock
        self._llm = llm_generate
        self._budget = budget
        self._usage_probe = usage_probe
        self._timeout = float(timeout_s)
        self.repo = RelationRepo(store)
        self._tasks: set[asyncio.Task] = set()
        self._fails = 0
        self._paused_until = 0.0
        self._warned_budget_day = ""
        # 模块日上限"查计数 → 占额度"串行化，避免连续消息并发确认时超额（审查 #9）
        self._confirm_lock: asyncio.Lock | None = None
        # 进程内缓存：scope → (阶段码, 是否冷静期, 写入时间)；给同步的卖萌档联动读（不落库、不读库）
        self._link_cache: dict[str, tuple[str, bool, float]] = {}

    # ------------------------------------------------------------ switches
    def _cfg(self, key: str, default: Any) -> Any:
        try:
            v = self._get(key, default)
        except Exception:
            return default
        return default if v is None else v

    def mode(self) -> str:
        return rm.normalize_mode(self._cfg(rm.KEY_MODE, rm.DEFAULT_MODE), rm.MODE_OFF)

    def bond_mode(self) -> str:
        bm = rm.normalize_mode(self._cfg(rm.KEY_BOND_MODE, rm.DEFAULT_BOND_MODE), rm.MODE_OFF)
        return rm.min_mode(bm, self.mode())

    def levels(self) -> tuple[str, ...]:
        return rm.parse_levels(self._cfg(rm.KEY_LEVELS, rm.DEFAULT_LEVELS))

    def daily_cap(self) -> int:
        return rm.parse_cap(self._cfg(rm.KEY_DAILY_CAP, rm.DEFAULT_DAILY_CAP))

    def start_stage(self) -> str:
        return rm.parse_stage(self._cfg(rm.KEY_BOND_START, rm.DEFAULT_BOND_START))

    def approval(self) -> str:
        return rm.normalize_approval(self._cfg(rm.KEY_BOND_APPROVAL, rm.DEFAULT_APPROVAL))

    def _track(self) -> str:
        return rm.MODE_ON if self.bond_mode() == rm.MODE_ON else rm.MODE_SHADOW

    # ------------------------------------------------------------ owner private message
    async def on_owner_message(
        self, scope_key: str, msg_id: int, msg_ts: float, text: str, prev_ts: float | None = None
    ) -> None:
        if self.mode() == rm.MODE_OFF or not valid_scope(scope_key):
            return
        try:
            needs = await asyncio.to_thread(self._on_owner_sync, scope_key, int(msg_id), float(msg_ts), text, prev_ts)
        except Exception as e:
            logger.warning(f"{rm.LOG} owner message failed: {type(e).__name__}")
            return
        if needs:
            self._spawn_confirm(scope_key, int(msg_id), float(msg_ts), text, needs)

    def _recent_same_kind(self, scope_key: str, kind: str, ts: float) -> bool:
        rows = self.repo.events(scope_key, ts - SAME_KIND_COOLDOWN_S, include_inactive=False)
        return any(r.get("kind") == kind for r in rows)

    def _append(self, ev: rm.RelationshipEvent) -> bool:
        ok = self.repo.append(ev)
        if ok:
            try:
                self._store.bump_counter(f"{C_EVENTS}:{ev.kind}")
            except Exception:
                pass
            logger.info(
                f"{rm.LOG} event kind={ev.kind} id={ev.rel_event_id} detector={ev.detector} mode={self.mode()}"
            )
        return ok

    def _release(self, scope_key: str, reason: str, ref: str, now: float) -> int:
        live = self.repo.modes(scope_key, live_at=now)
        targets = fd.releasable(reason, live)
        n = self.repo.release_modes([str(md["mode_id"]) for md in targets], reason, ref, now)
        if n:
            try:
                self._store.bump_counter(f"{C_RELEASE}:{reason}", n)
            except Exception:
                pass
            logger.info(f"{rm.LOG} release reason={reason} modes={n}")
        return n

    def _on_owner_sync(
        self, scope_key: str, mid: int, ts: float, text: str, prev_ts: float | None
    ) -> set[str]:
        self.repo.ensure()
        now = float(self._clock())
        if prev_ts is not None and ts - float(prev_ts) >= REUNION_GAP_S:
            self._store.set_cursor(CURSOR_REUNION + scope_key, f"{ts:.3f}")
        live = self.repo.modes(scope_key, live_at=now)
        sig = det.quick_signals(text, boundary_active=any(md.get("kind") == rm.MODE_BOUNDARY for md in live))
        if not sig.any():
            return set()
        ref = f"msg:{mid}"

        if sig.distress:  # 最高优先级：立即解除所有模式
            # 不落"危机/健康"等细分类别（敏感类别不落库），只记"主人状态不好"这一事实
            ev = self.repo.make_event(scope_key, rm.K_DISTRESS, [ref], ts, rm.DETECTOR_RULE)
            if self._append(ev):
                self._release(scope_key, rm.REL_OWNER_DISTRESS, f"rel:{ev.rel_event_id}", now)

        if rm.K_REPAIR in sig.kinds:
            recent = self.repo.events(scope_key, ts - 14 * rm.DAY, include_inactive=False)
            negs = fd.unrepaired_negatives(recent, ts + 1.0)
            live = self.repo.modes(scope_key, live_at=now)
            target = negs[-1]["rel_event_id"] if negs else None
            if target is None:
                soft = [md for md in live if md.get("kind") == rm.MODE_SOFTEN]
                target = soft[-1]["cause_event_id"] if soft else None
            if target is not None or not self._recent_same_kind(scope_key, rm.K_REPAIR, ts):
                ev = self.repo.make_event(
                    scope_key, rm.K_REPAIR, [ref], ts, rm.DETECTOR_RULE,
                    supersedes=target, context=rm.CTX_ASK if sig.ask else "",
                )
                if self._append(ev):
                    reason = rm.REL_ASK if sig.ask else rm.REL_REPAIR
                    self._release(scope_key, reason, f"rel:{ev.rel_event_id}", now)

        for kind in (rm.K_WARM, rm.K_RELIED, rm.K_OWNER_FEEDBACK):
            if kind in sig.kinds and not self._recent_same_kind(scope_key, kind, ts):
                self._append(self.repo.make_event(scope_key, kind, [ref], ts, rm.DETECTOR_RULE))

        if rm.K_OWNER_REQUEST in sig.kinds:
            ctx = rm.CTX_DISTANCE if sig.distance else rm.CTX_UNFREEZE if sig.unfreeze else ""
            ev = self.repo.make_event(scope_key, rm.K_OWNER_REQUEST, [ref], ts, rm.DETECTOR_RULE, context=ctx)
            if self._append(ev) and ctx and self.bond_mode() != rm.MODE_OFF:
                self._bond_owner_request(scope_key, ev.rel_event_id, ctx, now)
        return sig.needs_confirm

    # ------------------------------------------------------------ cheap-model confirm
    def _spawn_confirm(self, scope_key: str, mid: int, ts: float, text: str, kinds: set[str]) -> None:
        self._tasks = {t for t in self._tasks if not t.done()}
        if len(self._tasks) >= MAX_TASKS:
            logger.info(f"{rm.LOG} confirm skipped reason=busy")
            return
        task = asyncio.create_task(self._confirm(scope_key, mid, ts, text, set(kinds)))
        self._tasks.add(task)

    def _llm_allowed(self, now: float) -> str:
        if self._llm is None or self._budget is None:
            return "no_llm"
        if now < self._paused_until:
            return "paused"
        cap = self.daily_cap()
        if cap <= 0:
            return "cap"
        used = int(self._store.get_counters(cm.local_day(now)).get(C_LLM_CALLS, 0))
        return "cap" if used >= cap else ""

    async def _confirm(self, scope_key: str, mid: int, ts: float, text: str, kinds: set[str]) -> None:
        now = float(self._clock())
        if self._confirm_lock is None:
            self._confirm_lock = asyncio.Lock()
        reserved = False
        try:
            async with self._confirm_lock:
                try:
                    blocked = await asyncio.to_thread(self._llm_allowed, now)
                except Exception as e:
                    logger.warning(f"{rm.LOG} confirm precheck failed: {type(e).__name__}")
                    return
                if blocked:
                    logger.info(f"{rm.LOG} confirm skipped reason={blocked} kinds={','.join(sorted(kinds))}")
                    return
                # 先占模块额度（锁内），全局预算拒绝时退还
                await asyncio.to_thread(self._store.bump_counter, C_LLM_CALLS, 1, cm.local_day(now))
                reserved = True
            bots = await asyncio.to_thread(self.repo.bot_lines_recent, scope_key, det.BOT_LINES_MAX)
            prompt = det.build_confirm_prompt(kinds, text, bots)
            raw = await self._budget.guarded_generate(  # type: ignore[union-attr]
                rm.MODULE, prompt, self._llm, self._timeout,  # type: ignore[arg-type]
                usage_probe=self._usage_probe, output_reserve=OUTPUT_RESERVE,
            )
        except CheapBudgetExceeded as e:
            day = cm.local_day(now)
            if reserved:
                try:
                    await asyncio.to_thread(self._store.bump_counter, C_LLM_CALLS, -1, day)
                except Exception as e2:
                    logger.warning(f"{rm.LOG} confirm refund failed: {type(e2).__name__}")
            if self._warned_budget_day != day:
                self._warned_budget_day = day
                logger.info(f"{rm.LOG} confirm skipped reason=budget_{e.reason}")
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._fails += 1
            if self._fails >= BREAKER_FAILS:
                self._paused_until = now + BREAKER_PAUSE_S
                self._fails = 0
                logger.warning(f"{rm.LOG} breaker open pause_s={int(BREAKER_PAUSE_S)}")
            else:
                logger.warning(f"{rm.LOG} confirm failed: {type(e).__name__}")
            return
        self._fails = 0
        confirmed = det.parse_confirm(raw, kinds)
        logger.info(
            f"{rm.LOG} confirm kinds={','.join(sorted(kinds))} confirmed={','.join(sorted(confirmed)) or '-'}"
        )
        if confirmed:
            try:
                await asyncio.to_thread(self._write_confirmed, scope_key, mid, ts, confirmed)
            except Exception as e:
                logger.warning(f"{rm.LOG} confirmed write failed: {type(e).__name__}")

    def _write_confirmed(self, scope_key: str, mid: int, ts: float, kinds: set[str]) -> None:
        now = float(self._clock())
        ref = f"msg:{mid}"
        recent = self.repo.events(scope_key, ts - bf.DISTRESS_WINDOW, include_inactive=False)
        distressed = any(r.get("kind") == rm.K_DISTRESS for r in recent)
        for kind in sorted(kinds):
            if kind == rm.K_CONFIDED and self._recent_same_kind(scope_key, kind, ts):
                continue
            ev = self.repo.make_event(scope_key, kind, [ref], ts, rm.DETECTOR_LLM)
            if not self._append(ev):
                continue
            if kind in rm.NEGATIVE_KINDS and not distressed:
                # 主人难受时不进入冷静期（§5.4 自动解除第 1 条优先）
                live = self.repo.modes(scope_key, live_at=now)
                plan = fd.plan_mode(kind, self.levels(), live, now)
                if plan and plan["op"] == "create":
                    mode_id = self.repo.create_mode(scope_key, plan, ev.rel_event_id)
                    logger.info(f"{rm.LOG} mode op=create kind={plan['kind']} id={mode_id} mode={self.mode()}")
                elif plan and plan["op"] == "extend":
                    self.repo.extend_mode(str(plan["mode_id"]), float(plan["expires_at"]))
                    logger.info(f"{rm.LOG} mode op=extend id={plan['mode_id']}")

    # ------------------------------------------------------------ owner group message
    async def on_owner_group_message(self, scope_key: str, chat_id: Any, msg_id: Any, text: str) -> None:
        """主人本人在群里说生病 / 情绪差 / 危机 → 立即解除所有模式（不存原文）。"""
        if self.mode() == rm.MODE_OFF or not valid_scope(scope_key):
            return
        try:
            distress, crisis = det.group_distress(text)
            if not distress:
                return
            await asyncio.to_thread(self._group_sync, scope_key, int(chat_id), int(msg_id), crisis)
        except Exception as e:
            logger.warning(f"{rm.LOG} owner group message failed: {type(e).__name__}")

    def _group_sync(self, scope_key: str, chat_id: int, msg_id: int, crisis: bool) -> None:
        now = float(self._clock())
        live = self.repo.modes(scope_key, live_at=now)
        recent = self.repo.events(scope_key, now - GROUP_DISTRESS_COOLDOWN_S, include_inactive=False)
        dup = any(r.get("kind") == rm.K_DISTRESS and r.get("context") == rm.CTX_GROUP for r in recent)
        if dup and not live and not crisis:
            return
        # 不落群 id / 消息 id 原值：只存不可逆的短哈希（审查 P6）
        digest = hashlib.sha256(f"{chat_id}:{msg_id}".encode()).hexdigest()[:12]
        ev = self.repo.make_event(
            scope_key, rm.K_DISTRESS, [f"grp:h{digest}"], now, rm.DETECTOR_RULE, context=rm.CTX_GROUP
        )
        if self._append(ev):
            self._release(scope_key, rm.REL_OWNER_DISTRESS_GROUP, f"rel:{ev.rel_event_id}", now)

    # ------------------------------------------------------------ prompt section
    async def render(self, scope_key: str, purpose: str = "reply") -> str | None:
        """on：返回 ``<你们之间>`` 段；shadow：只写日志返回 None；off：None（不读库）。"""
        mode = self.mode()
        if mode == rm.MODE_OFF or not valid_scope(scope_key):
            return None
        try:
            text, view, bond = await asyncio.to_thread(self._render_sync, scope_key, mode, purpose)
        except Exception as e:
            logger.warning(f"{rm.LOG} render failed: {type(e).__name__}")
            return None
        lines = max(0, text.count("\n") - 1) if text else 0
        if mode == rm.MODE_SHADOW:
            msg = (
                f"{rm.LOG} shadow purpose={purpose} tone={view.code} bond={bond.stage if bond else '-'}"
                f" boundary={int(view.boundary_active)} lines={lines} chars={len(text)}"
            )
            if view.code != "normal" or view.boundary_active:
                logger.info(msg)
            else:
                logger.debug(msg)
            return None
        return text or None

    def _render_sync(
        self, scope_key: str, mode: str, purpose: str = "reply"
    ) -> tuple[str, rm.RelationView, rm.BondView | None]:
        now = float(self._clock())
        events = self.repo.events(scope_key, now - 14 * rm.DAY)
        modes = self.repo.modes(scope_key)
        view = fd.fold_tone(events, modes, now)
        explained = False
        if view.stage == rm.TONE_MADE_UP and view.repair_ts is not None:
            explained = self.repo.bot_sent_since(scope_key, view.repair_ts)
        reunion = False
        raw = self._store.get_cursor(CURSOR_REUNION + scope_key)
        if raw:
            try:
                reunion = 0.0 <= now - float(raw) <= REUNION_SHOW_S
            except ValueError:
                reunion = False
        bond: rm.BondView | None = None
        bmode = self.bond_mode()
        if bmode != rm.MODE_OFF:
            bond = self._bond_view_sync(scope_key, track=self._track(), now=now)
        text = narrator.render(view, bond, explained=explained, reunion=reunion, purpose=purpose)
        if mode == rm.MODE_ON:
            self._remember_link(scope_key, view, bond if self.bond_mode() == rm.MODE_ON else None, now)
        return text, view, bond

    # ------------------------------------------------------------ 主动闸门 / 联动点（P1-8、P1-11、U2）
    def _remember_link(self, scope_key: str, view: rm.RelationView, bond: rm.BondView | None, now: float) -> None:
        cooling = view.code in ("awkward", "pause") or view.boundary_active
        self._link_cache[scope_key] = (bond.stage if bond is not None else "s1", cooling, now)

    def gate_view_sync(self, scope_key: str, now: float | None = None) -> rm.RelationGateView:
        """主动候选池（在线程里同步调用）读的关系状态。relation_mode≠on / 异常 → 空视图（= 73edc2c）。

        冷静期模式只取 P1 允许的 1、2 级；阶段只在 bond_mode=on 时读真实值，否则恒为起点 s1。"""
        empty = rm.RelationGateView(allows=rm.bond_allows("s1"))
        if self.mode() != rm.MODE_ON or not valid_scope(scope_key):
            return empty
        try:
            now = float(self._clock() if now is None else now)
            events = self.repo.events(scope_key, now - 14 * rm.DAY)
            modes = self.repo.modes(scope_key)
            view = fd.fold_tone(events, modes, now)
            live = fd.active_modes(modes, now)
            kinds = {str(md.get("kind") or "") for md in live}
            mode = ""
            if rm.MODE_SOFTEN in kinds:
                mode = rm.MODE_SOFTEN
            elif rm.MODE_BOUNDARY in kinds:
                mode = rm.MODE_BOUNDARY
            bond = None
            if self.bond_mode() == rm.MODE_ON:
                bond = self._bond_view_sync(scope_key, track=rm.MODE_ON, now=now)
            self._remember_link(scope_key, view, bond, now)
            stage = bond.stage if bond is not None else "s1"
            allows = bond.allows if bond is not None else rm.bond_allows("s1")
            return rm.RelationGateView(mode=mode, tone=view.code, stage=stage, allows=dict(allows))
        except Exception as e:
            logger.warning(f"{rm.LOG} gate view failed: {type(e).__name__}")
            return empty

    def cute_relaxed(self, scope_key: str) -> bool:
        """P1-11 卖萌两格放宽（同步、只读进程内缓存）：relation/bond 都为 on、阶段≥"熟悉的朋友"且不在冷静期。

        缓存由每轮主人私聊的 ``<你们之间>`` 组装与主动闸门刷新；没有缓存（如刚重启）→ False（= 起点行为）。"""
        try:
            if self.mode() != rm.MODE_ON or self.bond_mode() != rm.MODE_ON:
                return False
            hit = self._link_cache.get(scope_key)
            if hit is None:
                return False
            stage, cooling, _ts = hit
            return not cooling and rm.bond_allows(stage).get("cute_extra") == "two_cells_occasional"
        except Exception:
            return False

    async def owner_style_cap(self, scope_key: str, configured: Any) -> str:
        """P2-1 趋同档位联动（只给文字档位）：min(配置, 阶段允许)；bond_mode≠on → 起点档。"""
        bv = await self.bond_view(scope_key)
        return rm.owner_style_cap(bv.stage, configured)

    # ------------------------------------------------------------ bond
    def _bond_view_sync(self, scope_key: str, *, track: str, now: float) -> rm.BondView:
        rows = self.repo.transitions(scope_key)
        start = self.start_stage()
        st = bf.bond_state(rows, start, track)
        reasons: list[str] = []
        if st.last_change is not None:
            for ref in list(st.last_change.get("cause_refs") or [])[-2:]:
                ev = self.repo.get_event(str(ref))
                if ev and ev.get("kind") in rm.TONE_REASON_KINDS:
                    reasons.append(rm.SUMMARIES.get(str(ev.get("kind")), ""))
        care = bf.care_stage(rows, start, now) if track == rm.MODE_ON else st.stage
        return rm.BondView(
            stage=st.stage,
            frozen=st.frozen,
            reasons=tuple(r for r in reasons if r),
            allows=rm.bond_allows(st.stage, care),
            care_stage=care,
        )

    async def bond_view(self, scope_key: str) -> rm.BondView:
        """给其他模块（P1-2 卖萌档、P1-12 关心桥、P2-1 趋同）读的文字档位。

        只有 bond_mode=on 才返回真实阶段；off / shadow / 异常一律返回起点 s1 的档位（= 现网行为）。
        """
        default = rm.BondView(stage="s1", allows=rm.bond_allows("s1"), care_stage="s1")
        if self.bond_mode() != rm.MODE_ON or not valid_scope(scope_key):
            return default
        try:
            return await asyncio.to_thread(
                self._bond_view_sync, scope_key, track=rm.MODE_ON, now=float(self._clock())
            )
        except Exception as e:
            logger.warning(f"{rm.LOG_BOND} view failed: {type(e).__name__}")
            return default

    def _write_transition(self, scope_key: str, t: rm.BondTransition, now: float) -> str | None:
        tid = self.repo.add_transition(scope_key, t, now)
        if tid:
            logger.info(
                f"{rm.LOG_BOND} op={t.kind} from={t.from_stage} to={t.to_stage} rule={t.rule_id or '-'}"
                f" causes={len(t.cause_refs)} status={t.status} track={t.track} id={tid}"
            )
        return tid

    def _bond_owner_request(self, scope_key: str, rel_event_id: str, ctx: str, now: float) -> None:
        track = self._track()
        rows = self.repo.transitions(scope_key)
        start = self.start_stage()
        st = bf.bond_state(rows, start, track)
        status = "applied" if track == rm.MODE_ON else "proposed"
        if ctx == rm.CTX_DISTANCE:
            t = rm.BondTransition(
                st.stage, start, "owner_request", [rel_event_id], rule_id="R2",
                status=status, actor="bond_folder", track=track,
            )
        elif ctx == rm.CTX_UNFREEZE and st.frozen:
            t = rm.BondTransition(
                st.stage, st.stage, "unfreeze", [rel_event_id], rule_id="R2u",
                status=status, actor="bond_folder", track=track,
            )
        else:
            return
        self._write_transition(scope_key, t, now)

    def bond_evaluate_sync(self, scope_key: str, now: float, *, only_undo: bool = False) -> str | None:
        bmode = self.bond_mode()
        if bmode == rm.MODE_OFF:
            return None
        track = self._track()
        events = self.repo.events(scope_key, now - EVENTS_LOOKBACK_S)
        rows = self.repo.transitions(scope_key)
        live = self.repo.modes(scope_key, live_at=now)
        try:
            t = bf.evaluate(
                events, rows, now, start_stage=self.start_stage(), track=track, modes_active=bool(live)
            )
        except Exception as e:  # 折叠失败 → 维持当前阶段
            logger.warning(f"{rm.LOG_BOND} evaluate failed: {type(e).__name__}")
            return None
        if t is None or (only_undo and t.kind != "undo"):
            return None
        t.track = track
        t.actor = "bond_folder"
        if track == rm.MODE_SHADOW:
            t.status = "proposed"
        elif t.kind in ("promote", "demote") and self.approval() == "manual":
            t.status = "proposed"
        else:
            t.status = "applied"
        return self._write_transition(scope_key, t, now)

    # ------------------------------------------------------------ maintenance
    async def maintenance(self, scopes: Iterable[str], now: float | None = None) -> None:
        if self.mode() == rm.MODE_OFF:
            return
        now = float(self._clock() if now is None else now)
        try:
            await asyncio.to_thread(self._maintenance_sync, [s for s in scopes if valid_scope(s)], now)
        except Exception as e:
            logger.warning(f"{rm.LOG} maintenance failed: {type(e).__name__}")

    def _maintenance_sync(self, scopes: list[str], now: float) -> None:
        self.repo.ensure()
        n = self.repo.expire_modes(now)
        if n:
            logger.info(f"{rm.LOG} release reason={rm.REL_EXPIRED} modes={n}")
        for scope in scopes:
            self._derive_promises(scope, now)
            if self.bond_mode() == rm.MODE_OFF:
                continue
            key = CURSOR_BOND_DAY + scope
            day = cm.local_day(now)
            if self._store.get_cursor(key) == day:
                continue
            self._store.set_cursor(key, day)
            self.bond_evaluate_sync(scope, now)

    def _derive_promises(self, scope_key: str, now: float) -> None:
        """promise_kept 由 items(bot_promise→done) 派生（detector=derived）。首次启用只从现在起算。"""
        key = CURSOR_PROMISE + scope_key
        raw = self._store.get_cursor(key)
        if raw is None:
            self._store.set_cursor(key, f"{now:.3f}")
            return
        try:
            since = float(raw)
        except ValueError:
            since = now
        last = since
        for row in self.repo.promises_done_since(scope_key, since):
            ref = f"item:{row['item_id']}"
            ts = float(row.get("updated_at") or now)
            last = max(last, ts)
            if self.repo.derived_exists(scope_key, ref):
                continue
            self._append(self.repo.make_event(scope_key, rm.K_PROMISE_KEPT, [ref], ts, rm.DETECTOR_DERIVED))
        if last > since:
            self._store.set_cursor(key, f"{last:.3f}")

    # ------------------------------------------------------------ lifecycle
    def is_idle(self) -> bool:
        return all(t.done() for t in self._tasks)

    async def stop(self) -> None:
        tasks = [t for t in self._tasks if not t.done()]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except BaseException:
                pass
        self._tasks.clear()
