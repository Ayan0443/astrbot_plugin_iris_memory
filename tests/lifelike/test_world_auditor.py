"""ClaimAuditor 与 WorldProjection：自述核对的四类结论、shadow 不落实、投影文字化与群聊边界。"""

from __future__ import annotations

import json

import pytest
from astrbot_plugin_iris_memory.iris_memory.continuity.store import ContinuityStore
from astrbot_plugin_iris_memory.iris_memory.lifelike.budget import CheapBudget
from astrbot_plugin_iris_memory.iris_memory.lifelike.guards import numeric
from astrbot_plugin_iris_memory.iris_memory.lifelike.world import auditor as au
from astrbot_plugin_iris_memory.iris_memory.lifelike.world import models as wm
from astrbot_plugin_iris_memory.iris_memory.lifelike.world.projection import render_projection
from astrbot_plugin_iris_memory.iris_memory.lifelike.world.store import WorldStore

DAY = "2026-11-03"
SCOPE = "private:1"


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock(wm.at_hm(DAY, "16:00"))


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


class LLM:
    def __init__(self, claims=None, fail=False, raw=None):
        self.claims, self.fail, self.raw, self.calls = claims or [], fail, raw, 0

    async def __call__(self, prompt, timeout):
        self.calls += 1
        self.prompt = prompt
        if self.fail:
            raise ConnectionError()
        if self.raw is not None:
            return self.raw
        return json.dumps({"claims": [{"span": s, "type": t} for s, t in self.claims]}, ensure_ascii=False)


def _auditor(ws, base, clock, llm):
    budget = CheapBudget(base, lambda k, d: d, clock)
    return au.ClaimAuditor(ws, llm, budget, counters_get=lambda: base.get_counters(DAY),
                           bump=lambda n, k: base.bump_counter(n, k))


def _ev(eid, title, _hint, s="14:00", e="15:00", **kw):
    d = dict(event_id=eid, day=DAY, kind="media", title=title, slot_start=wm.at_hm(DAY, s),
             slot_end=wm.at_hm(DAY, e), due_at=wm.at_hm(DAY, e), scene="在家")
    d.update(kw)
    ev = wm.LifeEvent(**d)
    return ev


def _seed(ws, base, ev, final):
    """按合法路径把事件推到 final 状态"""
    ws.insert_event(ev, actor="dayplan", reason="seed")
    if final in ("in_progress", "done"):
        ws.transition(ev.event_id, "in_progress", actor="world_clock", reason="s", expected_from=("planned",))
    if final == "done":
        ws.transition(ev.event_id, "done", actor="world_clock", reason="d", expected_from=("in_progress",),
                      outcome="看完了，挺治愈的", mood_note="心情挺好", settle=True)
    if final == "cancelled":
        ws.transition(ev.event_id, "cancelled", actor="world_clock", reason="c", expected_from=("planned",),
                      outcome="最后没出门，在家待着了", settle=True)


@pytest.mark.asyncio
async def test_no_cue_no_llm(ws, base, clock):
    llm = LLM()
    assert await _auditor(ws, base, clock, llm).audit(1, "这个配置项要改成 true", SCOPE, clock(), apply=True) == []
    assert llm.calls == 0 and ws.claims_since(0) == []


@pytest.mark.asyncio
async def test_matched_on_records_mention(ws, base, clock):
    _seed(ws, base, _ev("a", "看一部讲树懒的纪录片", "done"), "done")
    text = "我下午看了树懒的纪录片，好治愈"
    llm = LLM([("我下午看了树懒的纪录片", "past_fact")])
    res = await _auditor(ws, base, clock, llm).audit(1, text, SCOPE, clock(), apply=True)
    assert [r.resolution for r in res] == ["matched"] and res[0].event_id == "a"
    assert ws.get_event("a").mention_count == 1
    assert "我下午看了树懒的纪录片" in llm.prompt
    row = ws.claims_since(0)[0]
    assert row["applied"] == 1 and row["resolution"] == "matched"
    assert base.get_counters(DAY)["lifelike.world.claim.cue"] == 1
    # 同一出站不重复核对
    assert await _auditor(ws, base, clock, llm).audit(1, text, SCOPE, clock(), apply=True) == []
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_shadow_records_but_does_not_apply(ws, base, clock):
    _seed(ws, base, _ev("a", "看一部讲树懒的纪录片", "done"), "done")
    text = "我刚给绿萝换了水，我下午看了树懒的纪录片"
    llm = LLM([("我刚给绿萝换了水", "past_fact"), ("我下午看了树懒的纪录片", "past_fact")])
    res = await _auditor(ws, base, clock, llm).audit(2, text, SCOPE, clock(), apply=False)
    assert [r.resolution for r in res] == ["adopted", "matched"]
    assert ws.get_event("a").mention_count == 0
    assert [e.source for e in ws.list_events([DAY])] == ["dayplan"]  # 没补登
    assert all(r["applied"] == 0 for r in ws.claims_since(0))


@pytest.mark.asyncio
async def test_adopted_on_inserts_chat_claim_and_cap(ws, base, clock):
    a = _auditor(ws, base, clock, None)
    spans = ["我刚给绿萝换了水", "我刚整理了书桌", "我刚翻了翻海龟图鉴", "我刚听了会儿雨声"]
    results = []
    for i, sp in enumerate(spans):
        a._llm = LLM([(sp, "past_fact")])
        results += await a.audit(10 + i, sp + "。", SCOPE, clock(), apply=True)
    assert [r.resolution for r in results] == ["adopted", "adopted", "adopted", "conflict"]
    assert results[-1].reason == "adopt_cap"
    claimed = [e for e in ws.list_events([DAY]) if e.source == "chat_claim"]
    assert len(claimed) == 3 and all(e.status == "done" and e.settled_at for e in claimed)
    assert claimed[0].title == "给绿萝换了水"
    ops = [r["op"] for r in ws.log_for(claimed[0].event_id)]
    assert ops == ["propose", "accept"]


@pytest.mark.asyncio
async def test_conflicts(ws, base, clock):
    _seed(ws, base, _ev("p", "看海龟的纪录片", "planned", s="20:00", e="21:00"), "planned")
    _seed(ws, base, _ev("c", "出门散会儿步", "cancelled", s="10:00", e="11:00", scene="外出", kind="activity"), "cancelled")
    a = _auditor(ws, base, clock, None)
    cases = [
        ("我看完了海龟的纪录片", "past_fact", "plan_as_done"),
        ("我刚出门散了会儿步", "past_fact", "cancelled_as_done"),
        ("我刚在中山公园拍了照", "past_fact", "content:photo"),
        ("我还在外面晃悠", "ongoing", "scene"),
    ]
    for i, (span, t, reason) in enumerate(cases):
        a._llm = LLM([(span, t)])
        res = await a.audit(20 + i, span, SCOPE, clock(), apply=True)
        assert [(r.resolution, r.reason) for r in res] == [("conflict", reason)], span
    assert not [e for e in ws.list_events([DAY]) if e.source == "chat_claim"]


@pytest.mark.asyncio
async def test_ignored_and_bad_spans(ws, base, clock):
    text = "我明天好想去海边，我刚帮你查了一下，我刚变成了一只树懒"
    llm = LLM([("好想去海边", "wish"), ("我刚帮你查了一下", "past_fact"), ("我刚变成了一只树懒", "figurative"),
               ("我刚去了月球", "past_fact"), ("我", "past_fact"), ("我刚帮你查了一下", "bogus")])
    res = await _auditor(ws, base, clock, llm).audit(3, text, SCOPE, clock(), apply=True)
    assert [(r.resolution, r.reason) for r in res] == [("ignored", "wish"), ("ignored", "not_self"), ("ignored", "figurative")]


@pytest.mark.asyncio
async def test_plan_tomorrow_creates_planned_event(ws, base, clock):
    text = "我明天打算整理房间"
    llm = LLM([("我明天打算整理房间", "plan")])
    res = await _auditor(ws, base, clock, llm).audit(4, text, SCOPE, clock(), apply=True)
    assert res[0].resolution == "adopted"
    ev = ws.get_event(res[0].event_id)
    assert ev.day == "2026-11-04" and ev.status == "planned" and ev.title == "整理房间" and ev.source == "chat_claim"
    # 已有计划再说一遍 → matched
    llm2 = LLM([("我明天打算整理房间", "plan")])
    clock.t = wm.at_hm("2026-11-04", "09:00")
    res2 = await _auditor(ws, base, clock, llm2).audit(5, text, SCOPE, clock(), apply=True)
    assert res2[0].resolution == "matched"
    llm3 = LLM([("我待会要去上班", "plan")])
    res3 = await _auditor(ws, base, clock, llm3).audit(6, "我待会要去上班", SCOPE, clock(), apply=True)
    assert res3[0].resolution == "conflict" and res3[0].reason == "content:real_life"


@pytest.mark.asyncio
async def test_plan_late_evening_rolls_to_next_day(ws, base, clock):
    clock.t = wm.at_hm(DAY, "23:40")
    llm = LLM([("我待会去收拾书桌", "plan")])
    res = await _auditor(ws, base, clock, llm).audit(7, "我待会去收拾书桌", SCOPE, clock(), apply=True)
    assert ws.get_event(res[0].event_id).day == "2026-11-04"


@pytest.mark.asyncio
async def test_failures_write_nothing(ws, base, clock):
    a = _auditor(ws, base, clock, LLM(fail=True))
    assert await a.audit(8, "我刚看完书", SCOPE, clock(), apply=True) == []
    a._llm = LLM(raw="not json")
    assert await a.audit(9, "我刚看完书", SCOPE, clock(), apply=True) == []
    base.bump_counter("lifelike.budget.calls:lifelike_world_claim", au.MODULE_CLAIM_DAILY, DAY)
    llm = LLM([("我刚看完书", "past_fact")])
    a._llm = llm
    assert await a.audit(11, "我刚看完书", SCOPE, clock(), apply=True) == []
    assert llm.calls == 0 and ws.claims_since(0) == []
    assert await au.ClaimAuditor(ws, None, None, counters_get=dict, bump=lambda n, k: None).audit(
        12, "我刚看完书", SCOPE, clock(), apply=True) == []


def test_parse_claims_edge():
    assert au.parse_claims("", "x") == []
    assert au.parse_claims("[]", "x") == []
    assert au.parse_claims('{"claims": 1}', "x") == []
    many = json.dumps({"claims": [{"span": "我刚看书", "type": "past_fact"}] * 5}, ensure_ascii=False)
    assert len(au.parse_claims(many, "我刚看书")) == au.MAX_CLAIMS


# ---------------------------------------------------------------- projection

def _proj_events():
    done = _ev("d", "看一部讲树懒的纪录片", "done", s="13:00", e="14:00", status="done", outcome="看完了，挺治愈的",
               mood_note="心情挺好", settled_at=wm.at_hm(DAY, "14:00"))
    cur = _ev("c", "给绿萝换水", "in_progress", s="15:30", e="16:30", status="in_progress", kind="routine")
    nxt = _ev("n", "开着雨声白噪音看书", "planned", s="21:00", e="22:00", status="planned", kind="activity")
    skip = _ev("k", "出门散会儿步", "cancelled", s="10:00", e="11:00", status="cancelled", scene="外出",
               outcome="最后没出门，在家待着了", settled_at=wm.at_hm(DAY, "10:00"))
    gone = _ev("r", "去中山公园", "done", status="done", retracted=True, settled_at=wm.at_hm(DAY, "12:00"))
    return [done, cur, nxt, skip, gone]


def test_projection_owner_private():
    now = wm.at_hm(DAY, "16:00")
    text = render_projection(_proj_events(), now, owner_private=True, conflicts=["我刚出门了"])
    assert text.startswith("<她的今天>") and text.endswith("</她的今天>")
    assert "现在：在家" in text and "正在给绿萝换水" in text and "心情挺好" in text
    assert "已经做了：中午看一部讲树懒的纪录片，看完了，挺治愈的" in text
    assert "打算：晚上开着雨声白噪音看书（还没开始）" in text
    assert "没做成：本来想出门散会儿步" in text
    assert "『我刚出门了』" in text and "虚拟日常" in text
    assert "中山公园" not in text
    assert not any(ch.isdigit() for ch in text)
    assert numeric.scan(text, strict=True) == []


def test_projection_group_only_scene_and_sleep():
    now = wm.at_hm(DAY, "16:00")
    g = render_projection(_proj_events(), now, owner_private=False)
    assert g.startswith("<她的今天>\n现在：在家。\n") and g.endswith("</她的今天>")
    assert "虚拟" in g and "被认真问就承认" in g  # 群里也带精简尾句（审查 P4）
    night = render_projection(_proj_events(), wm.at_hm("2026-11-04", "03:00"), owner_private=True)
    assert "本来在睡觉" in night and "照常认真回应" in night
    # 不给"回得短"的许可、不提被吵醒（审查 P3）
    assert "回得短" not in night and "吵醒了" not in night
    assert "昨天" in night


def test_projection_deprioritizes_mentioned():
    now = wm.at_hm(DAY, "16:00")
    evs = _proj_events()
    older = _ev("o", "翻海龟的图鉴", "done", s="09:00", e="10:00", status="done", outcome="弄好了",
                settled_at=wm.at_hm(DAY, "10:00"))
    evs[0].mention_count, evs[0].last_mentioned_at = 2, now - 600
    text = render_projection([*evs, older], now, owner_private=True)
    assert "翻海龟的图鉴" in text.split("已经做了：")[1].split("。")[0]
