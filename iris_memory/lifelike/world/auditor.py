"""ClaimAuditor：回复发出之后核对她的自述是否与 life_events 一致（设计 §4.6、§9.10 修订）。

接入点：被动回复（``ContinuityService.record_reply_sent`` 拿到回执后）与主动消息
（``ProactiveBridge.send_part`` 回执为 sent 后）。只核对**主人私聊**里她说出口的话；
后台异步执行，不增加回复延迟，任何失败都只跳过（``life_claims`` 不写）。

流程：
1. 规则快筛 ``has_self_life_cue``（第一人称 + 时间/动作），未命中直接结束；
2. 廉价模型分类（每条至多 1 次，module=``lifelike_world_claim``，本模块日上限 + 全局硬预算），
   片段必须是原文连续子串，类型 ∈ past_fact/ongoing/plan/wish/figurative/hypothetical；
3. 对照事实表：matched / adopted（说出口就认账，补登 chat_claim 事件，每天 ≤3）/
   conflict（计划说成已做、场景冲突、越过身份边界、超过补登上限）/ ignored（愿望、比喻、
   假设、讲对方的事）。

``shadow`` 只写 ``life_claims``（applied=0）与计数，不补登事件、不记 mention、不出提示；
``on`` 时落实（applied=1），conflict 在下一轮投影里提示"自然圆回来"。

输入只有她自己发出的文字，不含主人原文；日志只记 id / 计数 / 原因码。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any, Optional

from astrbot.api import logger

from ..budget import CheapBudget, CheapBudgetExceeded
from . import models as wm
from . import rules
from .store import WorldStore, new_event_id
from ...identity import lazy_re

LOG = "[lifelike.world]"
MODULE_CLAIM = "lifelike_world_claim"
MODULE_CLAIM_DAILY = 20
ADOPT_DAILY_MAX = 3
MATCH_THRESHOLD = 0.5
MAX_CLAIMS = 3
CLAIM_TIMEOUT_S = 30.0
CLAIM_OUTPUT_RESERVE = 300
TEXT_MAX = 600

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_ABOUT_OTHER = lazy_re(r"你|{OWNER}|主人|他|她们|他们", re.IGNORECASE)

LLMGenerate = Callable[[str, float], Awaitable[str]]

_PROMPT_HEAD = """下面是虚拟角色"她"刚发给朋友的一条消息。找出她在讲**她自己**生活经历的片段（不是讲对方的事），并分类：
past_fact（她说自己已经做了/发生了的事）、ongoing（她说自己正在做的事）、plan（她说自己打算/准备做的事）、
wish（愿望，如"好想……"）、figurative（比喻、玩笑、夸张）、hypothetical（假设）。
span 必须是消息原文里连续出现的片段，不超过 30 个字，不要改写。最多 3 条；没有就返回 {"claims":[]}。
只输出 JSON：{"claims":[{"span":"","type":"past_fact|ongoing|plan|wish|figurative|hypothetical"}]}
<message>
"""


def build_claim_prompt(text: str) -> str:
    return _PROMPT_HEAD + str(text or "")[:TEXT_MAX] + "\n</message>"


def parse_claims(raw: str, text: str) -> list[tuple[str, str]]:
    if not raw:
        return []
    s = _FENCE_RE.sub("", str(raw).strip())
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b <= a:
        return []
    try:
        data = json.loads(s[a : b + 1])
    except ValueError:
        return []
    rows = data.get("claims") if isinstance(data, dict) else None
    out: list[tuple[str, str]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        span = " ".join(str(row.get("span") or "").split())
        ctype = str(row.get("type") or "").strip()
        if len(span) < 2 or ctype not in wm.CLAIM_TYPES:
            continue
        if span not in " ".join(str(text).split()):
            continue  # 必须是原文子串
        out.append((span[: wm.SPAN_MAX], ctype))
        if len(out) >= MAX_CLAIMS:
            break
    return out


def _guess_kind(span: str) -> str:
    if re.search(r"看|追|听|翻|读", span):
        return wm.KIND_MEDIA
    if re.search(r"浇|换水|收拾|整理|洗", span):
        return wm.KIND_ROUTINE
    if re.search(r"想了|发呆|琢磨", span):
        return wm.KIND_THOUGHT
    return wm.KIND_ACTIVITY


class ClaimAuditor:
    def __init__(
        self,
        store: WorldStore,
        llm: Optional[LLMGenerate],
        budget: Optional[CheapBudget],
        *,
        counters_get: Callable[[], dict[str, int]],
        bump: Callable[[str, int], Any],
        usage_probe: Optional[Callable[[str], Awaitable[Optional[int]]]] = None,
        canon: Optional[dict] = None,
        timeout_s: float = CLAIM_TIMEOUT_S,
    ) -> None:
        self._store = store
        self._llm = llm
        self._budget = budget
        self._counters_get = counters_get
        self._bump = bump
        self._usage_probe = usage_probe
        self._canon = canon or rules.DEFAULT_CANON
        self._timeout = timeout_s
        # 模块日上限是"先查计数再调用"：串行化，避免多段 / 连续回复并发核对时超额（审查 #9）
        self._llm_lock: Optional[asyncio.Lock] = None

    has_self_life_cue = staticmethod(rules.has_self_life_cue)

    async def _db(self, fn, *a, **kw):
        return await asyncio.to_thread(fn, *a, **kw)

    def _count(self, name: str) -> None:
        try:
            self._bump(f"lifelike.world.claim.{name}", 1)
        except Exception:  # noqa: BLE001
            pass

    async def audit(self, outbound_id: int, text: str, scope_key: str, now: float, *,
                    apply: bool) -> list[wm.ClaimResult]:
        text = str(text or "")
        if not text.strip() or not rules.has_self_life_cue(text):
            return []
        await self._db(self._count, "cue")
        if await self._db(self._store.audited, outbound_id):
            return []
        claims = await self._classify(text)
        if claims is None:
            return []
        if not claims:
            await self._db(self._count, "none")
            return []
        day = wm.local_day(now)
        events = await self._db(self._store.list_events, [wm.shift_day(day, -1), day], (), False, 100)
        results: list[wm.ClaimResult] = []
        for span, ctype in claims:
            res = await self._resolve(span, ctype, events, now, apply)
            cid = await self._db(
                self._store.add_claim, outbound_id, scope_key, span, ctype, res.resolution,
                res.event_id, apply and res.resolution in (wm.RES_ADOPTED, wm.RES_CONFLICT, wm.RES_MATCHED),
                res.reason,
            )
            if cid is None:
                continue
            await self._db(self._count, res.resolution)
            results.append(res)
        logger.info(
            f"{LOG} claim outbound={outbound_id} n={len(results)} apply={int(apply)} "
            f"res={','.join(r.resolution for r in results) or '-'}"
        )
        return results

    async def _classify(self, text: str) -> Optional[list[tuple[str, str]]]:
        if self._llm is None or self._budget is None:
            return None
        if self._llm_lock is None:
            self._llm_lock = asyncio.Lock()
        async with self._llm_lock:
            return await self._classify_locked(text)

    async def _classify_locked(self, text: str) -> Optional[list[tuple[str, str]]]:
        try:
            used = int(self._counters_get().get(f"lifelike.budget.calls:{MODULE_CLAIM}", 0))
        except Exception:  # noqa: BLE001
            used = MODULE_CLAIM_DAILY
        if used >= MODULE_CLAIM_DAILY:
            await self._db(self._count, "skip_cap")
            return None
        try:
            raw = await self._budget.guarded_generate(
                MODULE_CLAIM, build_claim_prompt(text), self._llm, self._timeout,
                usage_probe=self._usage_probe, output_reserve=CLAIM_OUTPUT_RESERVE,
            )
        except CheapBudgetExceeded:
            await self._db(self._count, "skip_budget")
            return None
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{LOG} claim llm failed: {type(e).__name__}")
            await self._db(self._count, "error")
            return None
        return parse_claims(raw, text)

    # ------------------------------------------------------------ 判定
    def _best(self, span: str, events: list[wm.LifeEvent], statuses: tuple[str, ...]) -> Optional[wm.LifeEvent]:
        best, score = None, 0.0
        for ev in events:
            if ev.status not in statuses:
                continue
            s = rules.match_score(span, ev)
            if s > score:
                best, score = ev, s
        return best if score >= MATCH_THRESHOLD else None

    def _scene_conflict(self, span: str, events: list[wm.LifeEvent], now: float) -> bool:
        said = rules.scene_of_span(span)
        if said is None:
            return False
        state = rules.derive_state(events, now, self._canon)
        actual = wm.SCENE_HOME if state.scene == wm.SCENE_SLEEP else state.scene
        said = wm.SCENE_HOME if said == wm.SCENE_SLEEP else said
        out = (wm.SCENE_OUT, wm.SCENE_ROAD)
        return (said in out) != (actual in out)

    async def _resolve(self, span: str, ctype: str, events: list[wm.LifeEvent], now: float,
                       apply: bool) -> wm.ClaimResult:
        if ctype in ("wish", "figurative", "hypothetical"):
            return wm.ClaimResult(span, ctype, wm.RES_IGNORED, reason=ctype)
        if _ABOUT_OTHER.search(span):
            return wm.ClaimResult(span, ctype, wm.RES_IGNORED, reason="not_self")
        day = wm.local_day(now)

        if ctype in ("past_fact", "ongoing"):
            want = (wm.ST_DONE, wm.ST_IN_PROGRESS) if ctype == "past_fact" else (wm.ST_IN_PROGRESS,)
            hit = self._best(span, events, want)
            if hit is not None:
                if apply:
                    await self._db(self._store.note_mention, hit.event_id)
                return wm.ClaimResult(span, ctype, wm.RES_MATCHED, hit.event_id, "match")
            planned = self._best(span, events, (wm.ST_PLANNED,))
            if planned is not None:
                # "答应的事说成做了"：计划里还没到/没做完
                return wm.ClaimResult(span, ctype, wm.RES_CONFLICT, planned.event_id, "plan_as_done")
            cancelled = self._best(span, events, (wm.ST_CANCELLED,))
            if cancelled is not None:
                return wm.ClaimResult(span, ctype, wm.RES_CONFLICT, cancelled.event_id, "cancelled_as_done")
            bad = rules.content_violation(span)
            if bad:
                return wm.ClaimResult(span, ctype, wm.RES_CONFLICT, None, f"content:{bad}")
            if self._scene_conflict(span, events, now):
                return wm.ClaimResult(span, ctype, wm.RES_CONFLICT, None, "scene")
            if await self._db(self._store.adopted_count, day) >= ADOPT_DAILY_MAX:
                return wm.ClaimResult(span, ctype, wm.RES_CONFLICT, None, "adopt_cap")
            title = rules.span_title(span)
            if len(title) < 2:
                return wm.ClaimResult(span, ctype, wm.RES_IGNORED, reason="too_short")
            if not apply:
                return wm.ClaimResult(span, ctype, wm.RES_ADOPTED, None, "shadow")
            ev = wm.LifeEvent(
                event_id=new_event_id(), day=day, kind=_guess_kind(span), title=title,
                status=wm.ST_DONE if ctype == "past_fact" else wm.ST_IN_PROGRESS,
                slot_start=now if ctype == "ongoing" else None,
                due_at=now if ctype == "past_fact" else now + 3600,
                scene=rules.scene_of_span(span) or wm.SCENE_HOME,
                source="chat_claim", source_ref=["claim"],
                settled_at=now if ctype == "past_fact" else None,
            )
            if ev.scene == wm.SCENE_SLEEP:
                ev.scene = wm.SCENE_HOME
            await self._db(self._store.insert_event, ev, actor="claim_extractor", reason="adopted")
            return wm.ClaimResult(span, ctype, wm.RES_ADOPTED, ev.event_id, "adopted")

        # plan
        hit = self._best(span, events, (wm.ST_PLANNED, wm.ST_IN_PROGRESS))
        if hit is not None:
            if apply:
                await self._db(self._store.note_mention, hit.event_id)
            return wm.ClaimResult(span, ctype, wm.RES_MATCHED, hit.event_id, "match")
        target = wm.shift_day(day, 1) if rules.is_tomorrow(span) else day
        due = wm.at_hm(target, "21:00")
        if due <= now:
            due = min(now + 2 * 3600, wm.at_hm(target, "23:30"))
            if due <= now:
                target = wm.shift_day(day, 1)
                due = wm.at_hm(target, "21:00")
        title = rules.span_title(re.sub(r"^我?(?:明天|待会儿?|一会儿?|等下|晚点|打算|准备|要)+", "", span))
        ev = wm.LifeEvent(
            event_id=new_event_id(), day=target, kind=_guess_kind(span), title=title or span[: wm.TITLE_MAX],
            due_at=due, scene=wm.SCENE_HOME, source="chat_claim", source_ref=["claim"],
        )
        day_events = events if target == day else await self._db(self._store.list_events, [target], (), False, 50)
        verdict = rules.validate_proposal(ev, day_events, self._canon)
        if verdict.result == "reject" or verdict.adjusted is None:
            return wm.ClaimResult(span, ctype, wm.RES_CONFLICT, None, verdict.reason)
        if await self._db(self._store.adopted_count, target) >= ADOPT_DAILY_MAX:
            return wm.ClaimResult(span, ctype, wm.RES_CONFLICT, None, "adopt_cap")
        if not apply:
            return wm.ClaimResult(span, ctype, wm.RES_ADOPTED, None, "shadow")
        await self._db(self._store.insert_event, verdict.adjusted, actor="claim_extractor", reason="adopted_plan")
        return wm.ClaimResult(span, ctype, wm.RES_ADOPTED, verdict.adjusted.event_id, "adopted")
