"""DayPlanner + WorldClock + OutcomeWriter（设计 §4.3、§4.5、§10.2）。

Alice 式三段：廉价模型 / 模板 **提议** → ``rules.validate_plan`` **判定**（accept/adjust/reject）
→ 写入事实表；聊天侧只读 ``WorldProjection`` 的投影。

- DayPlanner 每天最多一次（``life_days`` 主键保证幂等）；模型失败、预算耗尽、全部驳回或
  ``lifelike_world_plan_llm=false`` → 模板兜底（"平常的一天"）。
- WorldClock 到点推进：planned→in_progress→done（结算写模板 outcome）；外出类以小概率
  改判"没出门"（不引用真实天气）。每个事件只结算一次（CAS + ``settled_at IS NULL``），
  启动时 ``catch_up`` 补结算，离线超过一天不补生成计划，只记一条模板事件。
- 结算 outcome 只用模板（``llm_outcome`` 暂不实现，省预算）。

日志前缀 ``[lifelike.world]``，只记 id / 计数 / 原因码。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from typing import Optional

from astrbot.api import logger

from ..budget import CheapBudget, CheapBudgetExceeded
from . import models as wm
from . import rules
from .store import WorldStore, new_event_id

LOG = "[lifelike.world]"
MODULE_PLAN = "lifelike_world_plan"
MODULE_PLAN_DAILY = 2  # 本模块每日调用上限（全局硬预算之外再兜一层）
PLAN_HOUR = 4.5  # 04:30 之后才生成当天计划（低峰）
PLAN_TIMEOUT_S = 45.0
PLAN_OUTPUT_RESERVE = 700
OUT_CANCEL_PCT = 15  # 外出类"没出门"的偏差率（%）
MIN_LLM_EVENTS = 2  # 通过判定的事件少于此数 → 模板兜底

LLMGenerate = Callable[[str, float], Awaitable[str]]

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_SLOT_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*[-~—–至到]\s*(\d{1,2}):(\d{2})\s*$")

# ---------------------------------------------------------------- 模板（全部通过 rules.validate_proposal）

# (kind, title, detail, start, end, scene, cost)
_TEMPLATE_DAYS: tuple[tuple[str, tuple[tuple[str, str, str, str, str, str, str], ...]], ...] = (
    ("平常的一天，慢慢悠悠的", (
        ("routine", "给窗台的小盆栽浇水", "顺便看看有没有冒新叶子", "09:30", "10:00", "在家", "轻松"),
        ("media", "看一部讲树懒的纪录片", "听说树懒很少下树", "14:00", "15:30", "在家", "轻松"),
        ("activity", "开着雨声白噪音看书", "", "21:00", "22:30", "在家", "轻松"),
    )),
    ("懒洋洋的一天，想把房间收拾一下", (
        ("routine", "整理房间", "把书桌收拾干净", "10:00", "11:00", "在家", "一般"),
        ("media", "翻海龟的图鉴", "看看不同海龟的壳", "15:00", "16:00", "在家", "轻松"),
        ("thought", "戴着耳机听雨声发呆", "", "22:00", "23:00", "在家", "轻松"),
    )),
    ("安安静静的一天，下午想出去走走", (
        ("routine", "给绿萝换水", "", "09:30", "10:00", "在家", "轻松"),
        ("activity", "出门散会儿步", "在附近走走，看看路边的植物", "16:00", "17:00", "外出", "一般"),
        ("media", "看蜗牛的科普视频", "", "20:30", "22:00", "在家", "轻松"),
    )),
)

# kind → [(outcome, mood_note)]
_OUTCOMES: dict[str, tuple[tuple[str, str], ...]] = {
    wm.KIND_MEDIA: (("看完了，挺治愈的", "心情挺好"), ("看了一半，剩下的留着下次看", "有点意犹未尽"),
                    ("看完了，有点意犹未尽", "意犹未尽")),
    wm.KIND_ROUTINE: (("弄好了", ""), ("做完了，心里舒服多了", "挺舒服")),
    wm.KIND_ACTIVITY: (("弄完了，挺放松的", "挺放松"), ("做了一会儿，比想的轻松", "")),
    wm.KIND_THOUGHT: (("发了一会儿呆，挺安静的", "很安静"),),
    wm.KIND_ERRAND: (("办好了", ""),),
    wm.KIND_SOCIAL: (("做完了", ""),),
}
_OUT_CANCEL = ("最后没出门，在家待着了", "有点懒")


def _h(seed: str) -> int:
    return int(hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8], 16)


def outcome_for(ev: wm.LifeEvent) -> tuple[str, str]:
    choices = _OUTCOMES.get(ev.kind) or _OUTCOMES[wm.KIND_ROUTINE]
    return choices[_h(ev.event_id) % len(choices)]


def should_skip_out(ev: wm.LifeEvent) -> bool:
    return ev.scene in (wm.SCENE_OUT, wm.SCENE_ROAD) and _h("out:" + ev.event_id) % 100 < OUT_CANCEL_PCT


def template_day(day: str, source: str = "template") -> wm.DayPlan:
    outline, rows = _TEMPLATE_DAYS[_h(day) % len(_TEMPLATE_DAYS)]
    events = [
        wm.LifeEvent(
            event_id=new_event_id(), day=day, kind=k, title=t, detail=d,
            slot_start=wm.at_hm(day, s), slot_end=wm.at_hm(day, e), due_at=wm.at_hm(day, e),
            scene=sc, energy_cost=c, source=source,
        )
        for k, t, d, s, e, sc, c in rows
    ]
    return wm.DayPlan(day, outline, events, "template")


# ---------------------------------------------------------------- prompt / 解析

def build_plan_prompt(day: str, canon: dict, carry: list[str], planned: list[str]) -> str:
    items = tuple(canon.get("interests") or ())
    if not items:
        from ...identity import setting, split_names

        items = split_names(setting("lifelike_world_interests", ""))
    interests = "；".join(items)
    lines = [
        f"你在给一个虚拟角色安排她今天（{wm.weekday_text(day)}）的虚拟日常。",
        "她是一个普通的同龄女生，一个人在家的时间多，日常很轻、很慢。",
    ]
    if interests:
        lines.append(f"她喜欢的东西：{interests}。")
    lines += [
        "要求：",
        "- 安排 3 到 5 件她一个人就能做完的小事，普通、低密度、不戏剧化；",
        "- 不写真实具体的地点、店名、品牌、作品名和人名；不写拍照、上班上课、生病、吃东西、天气和新闻；不和任何人一起；",
        "- 时间在 08:30 到 23:30 之间，互不重叠；scene 只能是 在家、外出、路上，出门的事最多一件；",
        "- title 不超过 24 个字，detail 不超过 40 个字，title 和 detail 里不要出现数字；",
        "- outline 用一句话写今天的基调，不超过 20 个字。",
    ]
    if carry:
        lines.append("昨天打算做但没做成的（可以挑一件今天再做，也可以不做）：" + "；".join(carry[:3]))
    if planned:
        lines.append("今天已经定下来的事（不要重复，时间不要撞）：" + "；".join(planned[:3]))
    lines.append(
        '只输出 JSON，不要解释：{"outline":"","events":[{"kind":"routine|activity|errand|thought|media",'
        '"title":"","detail":"","slot":"HH:MM-HH:MM","scene":"在家","energy_cost":"轻松|一般|累"}]}'
    )
    return "\n".join(lines)


def parse_plan(raw: str, day: str, generator: str) -> Optional[wm.DayPlan]:
    if not raw:
        return None
    text = _FENCE_RE.sub("", str(raw).strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        return None
    events: list[wm.LifeEvent] = []
    for row in data["events"][:6]:
        if not isinstance(row, dict):
            continue
        mt = _SLOT_RE.match(str(row.get("slot") or ""))
        if not mt:
            continue
        h1, m1, h2, m2 = (int(x) for x in mt.groups())
        if not (0 <= h1 <= 24 and 0 <= h2 <= 24 and m1 < 60 and m2 < 60):
            continue
        s = wm.day_start(day) + h1 * 3600 + m1 * 60
        e = wm.day_start(day) + h2 * 3600 + m2 * 60
        events.append(
            wm.LifeEvent(
                event_id=new_event_id(), day=day,
                kind=str(row.get("kind") or "").strip(),
                title=str(row.get("title") or ""), detail=str(row.get("detail") or ""),
                slot_start=s, slot_end=e, due_at=e,
                scene=str(row.get("scene") or wm.SCENE_HOME).strip(),
                energy_cost=str(row.get("energy_cost") or "").strip(),
                source="dayplan",
            )
        )
    return wm.DayPlan(day, str(data.get("outline") or ""), events, generator)


# ---------------------------------------------------------------- DayPlanner

class DayPlanner:
    def __init__(
        self,
        store: WorldStore,
        llm: Optional[LLMGenerate],
        budget: Optional[CheapBudget],
        *,
        counters_get: Callable[[], dict[str, int]],
        llm_enabled: Callable[[], bool] = lambda: True,
        provider_name: Callable[[], Optional[str]] = lambda: None,
        usage_probe: Optional[Callable[[str], Awaitable[Optional[int]]]] = None,
        canon: Optional[dict] = None,
        timeout_s: float = PLAN_TIMEOUT_S,
    ) -> None:
        self._store = store
        self._llm = llm
        self._budget = budget
        self._counters_get = counters_get
        self._llm_enabled = llm_enabled
        self._provider_name = provider_name
        self._usage_probe = usage_probe
        self._canon = canon or rules.DEFAULT_CANON
        self._timeout = timeout_s
        self._lock = asyncio.Lock()

    async def _db(self, fn, *a, **kw):
        return await asyncio.to_thread(fn, *a, **kw)

    def _module_calls_today(self) -> int:
        try:
            return int(self._counters_get().get(f"lifelike.budget.calls:{MODULE_PLAN}", 0))
        except Exception:  # noqa: BLE001
            return MODULE_PLAN_DAILY

    async def ensure_day(self, day: str, now: float) -> Optional[str]:
        """幂等：当天已有计划返回 None；否则生成并写入，返回 plan_status。"""
        async with self._lock:
            if await self._db(self._store.get_day, day) is not None:
                return None
            yesterday = wm.shift_day(day, -1)
            prev = await self._db(self._store.list_events, [yesterday], (), False, 50)
            existing = await self._db(self._store.list_events, [day], (), False, 50)
            carry = [e.title for e in prev if e.status in (wm.ST_CANCELLED, wm.ST_CHANGED)]
            fixed = [e.title for e in existing if e.status == wm.ST_PLANNED]
            plan, status, rejected = None, "fallback", []
            raw_plan = await self._propose_llm(day, carry, fixed)
            if raw_plan is not None:
                checked, verdicts = rules.validate_plan(raw_plan, existing, self._canon)
                rejected = [("llm", v.reason) for v in verdicts if v.result == "reject"]
                if len(checked.events) >= MIN_LLM_EVENTS:
                    plan, status = checked, "generated"
                if rejected:
                    logger.info(f"{LOG} plan day={day} rejected={len(rejected)} accepted={len(checked.events)}")
            if plan is None:
                checked, verdicts = rules.validate_plan(template_day(day), existing, self._canon)
                plan = checked
            last = await self._db(self._store.last_planned_day, day)
            ids = await self._db(
                self._store.write_plan, plan, status, actor="planner_llm" if status == "generated" else "template",
                inputs_digest=f"carry={len(carry)} fixed={len(fixed)}", rejected=rejected,
            )
            if last is not None and last < wm.shift_day(day, -1):
                gap = wm.LifeEvent(
                    event_id=new_event_id(), day=day, kind=wm.KIND_THOUGHT,
                    title="前几天过得很平常", outcome="那几天没什么特别的", status=wm.ST_DONE,
                    due_at=now, source="template", settled_at=now,
                )
                await self._db(self._store.insert_event, gap, actor="template", reason="offline_gap")
            logger.info(f"{LOG} plan day={day} status={status} events={len(ids)} generator={plan.generator}")
            return status

    async def _propose_llm(self, day: str, carry: list[str], fixed: list[str]) -> Optional[wm.DayPlan]:
        if self._llm is None or self._budget is None:
            return None
        try:
            if not self._llm_enabled():
                return None
        except Exception:  # noqa: BLE001
            return None
        if await asyncio.to_thread(self._module_calls_today) >= MODULE_PLAN_DAILY:
            logger.info(f"{LOG} plan llm skipped reason=module_cap day={day}")
            return None
        prompt = build_plan_prompt(day, self._canon, carry, fixed)
        try:
            raw = await self._budget.guarded_generate(
                MODULE_PLAN, prompt, self._llm, self._timeout,
                usage_probe=self._usage_probe, output_reserve=PLAN_OUTPUT_RESERVE,
            )
        except CheapBudgetExceeded as e:
            logger.info(f"{LOG} plan llm skipped reason=budget:{e.reason} day={day}")
            return None
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{LOG} plan llm failed day={day}: {type(e).__name__}")
            return None
        pname = ""
        try:
            pname = self._provider_name() or ""
        except Exception:  # noqa: BLE001
            pname = ""
        plan = parse_plan(raw, day, f"llm:{pname or 'background'}")
        if plan is None:
            logger.warning(f"{LOG} plan llm bad_json day={day}")
        return plan


# ---------------------------------------------------------------- WorldClock

class WorldClock:
    """推进状态（由 WorldService 的低频后台循环或启动补结算调用）。"""

    def __init__(self, store: WorldStore, planner: DayPlanner, *, canon: Optional[dict] = None) -> None:
        self._store = store
        self._planner = planner
        self._canon = canon or rules.DEFAULT_CANON
        self._lock = asyncio.Lock()

    async def _db(self, fn, *a, **kw):
        return await asyncio.to_thread(fn, *a, **kw)

    async def tick(self, now: float) -> list[str]:
        async with self._lock:
            day = wm.local_day(now)
            if wm.local_hour(now) >= PLAN_HOUR:
                await self._planner.ensure_day(day, now)
            changed: list[str] = []
            for ev in await self._db(self._store.pending_events, now):
                if await self._advance(ev, now):
                    changed.append(ev.event_id)
            events = await self._db(self._store.list_events, [wm.shift_day(day, -1), day], (), False, 100)
            state = rules.derive_state(events, now, self._canon)
            await self._db(self._store.put_state, state)
            if changed:
                logger.info(f"{LOG} tick changed={len(changed)} scene_awake={int(state.awake)}")
            return changed

    async def catch_up(self, now: float) -> int:
        return len(await self.tick(now))

    async def _advance(self, ev: wm.LifeEvent, now: float) -> bool:
        st = self._store
        if ev.status == wm.ST_PLANNED:
            if should_skip_out(ev):
                out, mood = _OUT_CANCEL
                ok = await self._db(
                    st.transition, ev.event_id, wm.ST_CANCELLED, actor="world_clock", reason="skip_out",
                    expected_from=(wm.ST_PLANNED,), outcome=out, mood_note=mood, settle=True,
                )
                return bool(ok)
            ok = await self._db(
                st.transition, ev.event_id, wm.ST_IN_PROGRESS, actor="world_clock", reason="slot_start",
                expected_from=(wm.ST_PLANNED,),
            )
            if not ok:
                return False
            if ev.due_at is None or now < float(ev.due_at):
                return True
        elif ev.status != wm.ST_IN_PROGRESS:
            return False
        out, mood = outcome_for(ev)
        return bool(
            await self._db(
                st.transition, ev.event_id, wm.ST_DONE, actor="world_clock", reason="due",
                expected_from=(wm.ST_IN_PROGRESS,), outcome=out, mood_note=mood, settle=True,
            )
        )
