"""生活系统存储 / DayPlanner / WorldClock：建表幂等、append-only、CAS 结算只一次、计划生成与兜底。"""

from __future__ import annotations

import json
import sqlite3

import pytest
from astrbot_plugin_iris_memory.iris_memory.continuity import store as cstore
from astrbot_plugin_iris_memory.iris_memory.continuity.store import ContinuityStore
from astrbot_plugin_iris_memory.iris_memory.lifelike.budget import CheapBudget
from astrbot_plugin_iris_memory.iris_memory.lifelike.world import models as wm
from astrbot_plugin_iris_memory.iris_memory.lifelike.world import planner as pl
from astrbot_plugin_iris_memory.iris_memory.lifelike.world.store import WorldStore, ensure_world_schema

DAY = "2026-11-03"
NEXT = "2026-11-04"


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return Clock(wm.at_hm(DAY, "05:00"))


@pytest.fixture
def base(tmp_path, clock):
    s = ContinuityStore(tmp_path / "c.db", clock=clock)
    s.open()
    yield s
    s.close()


@pytest.fixture
def ws(base, clock):
    w = WorldStore(base, clock)
    w.ensure_schema()
    return w


def _ev(eid, day=DAY, s="10:00", e="11:00", **kw):
    base = dict(event_id=eid, day=day, kind="routine", title="给绿萝换水", slot_start=wm.at_hm(day, s),
                slot_end=wm.at_hm(day, e), due_at=wm.at_hm(day, e), scene="在家")
    base.update(kw)
    return wm.LifeEvent(**base)


# ---------------------------------------------------------------- store

def test_schema_idempotent_and_user_version_unchanged(base):
    before = base.get_schema_version()
    ensure_world_schema(base)
    ensure_world_schema(base)
    assert base.get_schema_version() == before == cstore.SCHEMA_VERSION
    names = {r[0] for r in base._conn().execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"life_days", "life_events", "life_event_log", "life_state", "life_claims"} <= names


def test_log_append_only(ws, base):
    ws.insert_event(_ev("a"), actor="dayplan", reason="t")
    with pytest.raises(sqlite3.DatabaseError):
        with base._tx() as db:
            db.execute("UPDATE life_event_log SET reason='x'")
    with pytest.raises(sqlite3.DatabaseError):
        with base._tx() as db:
            db.execute("DELETE FROM life_event_log")
    ops = [r["op"] for r in ws.log_for("a")]
    assert ops == ["propose", "accept"]


def test_transition_cas_and_settle_once(ws, clock):
    ws.insert_event(_ev("a"), actor="dayplan", reason="t")
    assert not ws.transition("a", "done", actor="world_clock", reason="x", expected_from=("planned",))
    assert ws.transition("a", "in_progress", actor="world_clock", reason="x", expected_from=("planned",))
    assert not ws.transition("a", "in_progress", actor="world_clock", reason="x", expected_from=("planned",))
    assert ws.transition("a", "done", actor="world_clock", reason="due", expected_from=("in_progress",),
                         outcome="弄好了", settle=True)
    assert not ws.transition("a", "done", actor="world_clock", reason="due", expected_from=("in_progress", "done"),
                             settle=True)
    e = ws.get_event("a")
    assert e.status == "done" and e.outcome == "弄好了" and e.settled_at is not None and e.revision == 3
    assert [r["op"] for r in ws.log_for("a")].count("settle") == 1
    assert not ws.transition("missing", "done", actor="world_clock", reason="x", expected_from=("in_progress",))


def test_illegal_create_rejected(ws):
    with pytest.raises(ValueError):
        ws.insert_event(_ev("a", status="done"), actor="planner_llm", reason="t")


def test_retract_keeps_row_and_status(ws):
    ws.insert_event(_ev("a"), actor="dayplan", reason="t")
    assert ws.retract("a", "owner_cmd", "undo")
    assert not ws.retract("a", "owner_cmd", "undo")
    assert ws.list_events([DAY]) == []
    e = ws.list_events([DAY], include_retracted=True)[0]
    assert e.retracted and e.status == "planned" and e.shareable == "none"


def test_claims_unique_and_render_mark(ws):
    cid = ws.add_claim(7, "private:1", "我刚看完书", "past_fact", "conflict", None, True)
    assert cid and ws.add_claim(7, "private:1", "我刚看完书", "past_fact", "conflict", None, True) is None
    assert ws.audited(7) and not ws.audited(8)
    ws.mark_claim_rendered([cid])
    assert ws.claims_since(0, "conflict")[0]["corrected_at"] is not None


# ---------------------------------------------------------------- DayPlanner

class LLM:
    def __init__(self, reply="", fail=False):
        self.reply, self.fail, self.calls = reply, fail, 0

    async def __call__(self, prompt, timeout):
        self.calls += 1
        self.prompt = prompt
        if self.fail:
            raise TimeoutError()
        return self.reply


def _planner(ws, base, clock, llm=None, enabled=True, budget_cfg=None):
    cfg = budget_cfg or {}
    budget = CheapBudget(base, lambda k, d: cfg.get(k, d), clock)
    return pl.DayPlanner(ws, llm, budget, counters_get=lambda: base.get_counters(wm.local_day(clock())),
                         llm_enabled=lambda: enabled, provider_name=lambda: "deepseek")


GOOD = json.dumps({
    "outline": "慢悠悠的一天",
    "events": [
        {"kind": "routine", "title": "给小盆栽浇水", "detail": "", "slot": "09:00-09:30", "scene": "在家", "energy_cost": "轻松"},
        {"kind": "media", "title": "看海龟的纪录片", "detail": "", "slot": "14:00-15:00", "scene": "在家", "energy_cost": "轻松"},
        {"kind": "activity", "title": "去中山公园拍照", "detail": "", "slot": "16:00-17:00", "scene": "外出", "energy_cost": "一般"},
        {"kind": "thought", "title": "听雨声发呆", "detail": "", "slot": "22:00-23:00", "scene": "在家", "energy_cost": ""},
    ],
}, ensure_ascii=False)


@pytest.mark.asyncio
async def test_planner_llm_plan_validated(ws, base, clock):
    llm = LLM("```json\n" + GOOD + "\n```")
    p = _planner(ws, base, clock, llm)
    assert await p.ensure_day(DAY, clock()) == "generated"
    assert await p.ensure_day(DAY, clock()) is None  # 幂等
    assert llm.calls == 1
    day = ws.get_day(DAY)
    assert day["generator"] == "llm:deepseek" and day["plan_status"] == "generated"
    titles = [e.title for e in ws.list_events([DAY])]
    assert titles == ["给小盆栽浇水", "看海龟的纪录片", "听雨声发呆"]
    assert all(e.status == "planned" and e.source == "dayplan" for e in ws.list_events([DAY]))
    rejects = [r for r in ws.all_log() if r["op"] == "reject"]
    assert len(rejects) == 1 and rejects[0]["reason"].startswith("content:")
    assert "去中山公园" not in json.dumps(ws.all_log(), ensure_ascii=False)
    counters = base.get_counters(DAY)
    assert counters["lifelike.budget.calls:lifelike_world_plan"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reply,fail", [("not json", False), ("", True), ('{"events":[]}', False)])
async def test_planner_falls_back_to_template(ws, base, clock, reply, fail):
    llm = LLM(reply, fail)
    p = _planner(ws, base, clock, llm)
    assert await p.ensure_day(DAY, clock()) == "fallback"
    assert ws.get_day(DAY)["generator"] == "template"
    assert len(ws.list_events([DAY])) == 3


@pytest.mark.asyncio
async def test_planner_template_only_and_budget(ws, base, clock):
    llm = LLM(GOOD)
    await _planner(ws, base, clock, llm, enabled=False).ensure_day(DAY, clock())
    assert llm.calls == 0
    await _planner(ws, base, clock, llm, budget_cfg={"lifelike_budget_cheap_calls_daily": 0}).ensure_day(NEXT, clock())
    assert llm.calls == 0 and ws.get_day(NEXT)["generator"] == "template"


@pytest.mark.asyncio
async def test_planner_module_cap(ws, base, clock):
    base.bump_counter("lifelike.budget.calls:lifelike_world_plan", pl.MODULE_PLAN_DAILY, DAY)
    llm = LLM(GOOD)
    await _planner(ws, base, clock, llm).ensure_day(DAY, clock())
    assert llm.calls == 0


def test_prompt_contains_no_owner_and_constraints():
    canon = dict(pl.rules.DEFAULT_CANON, interests=("拼图", "听雨"))
    prompt = pl.build_plan_prompt(DAY, canon, ["出门散会儿步"], ["整理房间"])
    assert "周二" in prompt and "拼图；听雨" in prompt and "不写拍照" in prompt
    assert "出门散会儿步" in prompt and "整理房间" in prompt


def test_parse_plan_bad_rows():
    assert pl.parse_plan("", DAY, "x") is None
    assert pl.parse_plan("[1,2]", DAY, "x") is None
    assert pl.parse_plan('{"events": 3}', DAY, "x") is None
    plan = pl.parse_plan('{"events":[1,{"slot":"bad"},{"slot":"25:00-26:00"},{"kind":"media","title":"看书","slot":"10:00~11:00"}]}', DAY, "x")
    assert len(plan.events) == 1 and plan.events[0].slot_start == wm.at_hm(DAY, "10:00")


# ---------------------------------------------------------------- WorldClock

@pytest.mark.asyncio
async def test_clock_progression_and_restart_settles_once(ws, base, clock):
    clock.t = wm.at_hm(DAY, "04:00")
    p = _planner(ws, base, clock)
    wc = pl.WorldClock(ws, p)
    await wc.tick(clock.t)
    assert ws.get_day(DAY) is None  # 04:30 之前不生成
    clock.t = wm.at_hm(DAY, "05:00")
    await wc.tick(clock.t)
    evs = ws.list_events([DAY])
    assert len(evs) == 3 and all(e.status == "planned" for e in evs)
    first = evs[0]
    clock.t = float(first.slot_start) + 60
    changed = await wc.tick(clock.t)
    assert first.event_id in changed
    st = ws.get_event(first.event_id).status
    assert st in ("in_progress", "cancelled")
    state = ws.get_state()
    assert state["awake"] == "1"
    # 模拟离线到深夜：新实例（重启）补结算，所有事件到终态，每个只结算一次
    clock.t = wm.at_hm(DAY, "23:50")
    wc2 = pl.WorldClock(ws, _planner(ws, base, clock))
    await wc2.catch_up(clock.t)
    await wc2.catch_up(clock.t)
    await pl.WorldClock(ws, _planner(ws, base, clock)).catch_up(clock.t)
    for e in ws.list_events([DAY]):
        assert e.status in ("done", "cancelled") and e.settled_at is not None and e.outcome
        ops = [r["op"] for r in ws.log_for(e.event_id)]
        assert ops.count("settle") + ops.count("cancel") == 1
    assert ws.get_state()["energy"] == "很困"


@pytest.mark.asyncio
async def test_skip_out_cancels_with_reason(ws, base, clock, monkeypatch):
    monkeypatch.setattr(pl, "should_skip_out", lambda ev: ev.scene == "外出")
    ws.insert_event(_ev("o", title="出门散会儿步", scene="外出"), actor="dayplan", reason="t")
    with base._tx() as db:
        db.execute("INSERT INTO life_days(day,plan_status,created_at,updated_at) VALUES (?,?,0,0)", (DAY, "generated"))
    wc = pl.WorldClock(ws, _planner(ws, base, clock))
    await wc.tick(wm.at_hm(DAY, "10:30"))
    e = ws.get_event("o")
    assert e.status == "cancelled" and "没出门" in e.outcome and e.settled_at is not None


@pytest.mark.asyncio
async def test_offline_gap_no_backfill(ws, base, clock):
    p = _planner(ws, base, clock)
    await p.ensure_day(DAY, clock())
    later = "2026-11-06"
    clock.t = wm.at_hm(later, "09:00")
    await pl.WorldClock(ws, p).tick(clock.t)
    assert ws.get_day("2026-11-04") is None and ws.get_day("2026-11-05") is None
    assert ws.get_day(later) is not None
    gap = [e for e in ws.list_events([later]) if e.source == "template" and e.status == "done"]
    assert len(gap) == 1 and gap[0].outcome == "那几天没什么特别的"
    # 旧日子的事件也被补结算
    assert all(e.status in ("done", "cancelled") for e in ws.list_events([DAY]))


def test_outcome_deterministic():
    e = _ev("abc", kind="media")
    assert pl.outcome_for(e) == pl.outcome_for(e)
    assert pl.outcome_for(_ev("x", kind="unknown"))[0] in {o for o, _ in pl._OUTCOMES["routine"]}
    assert not pl.should_skip_out(_ev("y", scene="在家"))


def test_prompt_interests_from_config_or_omitted():
    from astrbot_plugin_iris_memory.iris_memory import identity

    identity.set_source(lambda k, d="": {"lifelike_world_interests": "折纸,看星星"}.get(k, d))
    prompt = pl.build_plan_prompt(DAY, pl.rules.DEFAULT_CANON, [], [])
    assert "她喜欢的东西：折纸；看星星。" in prompt
    identity.set_source(None)
    prompt = pl.build_plan_prompt(DAY, pl.rules.DEFAULT_CANON, [], [])
    assert "她喜欢的东西" not in prompt
