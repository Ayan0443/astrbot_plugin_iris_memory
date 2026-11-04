"""P1-8/P1-12 接入点：ContinuityService / ProactiveBridge / ContextAssembler / 决策 prompt / 引擎发送前检查"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from astrbot_plugin_iris_memory.iris_memory.continuity.service import ContinuityService
from astrbot_plugin_iris_memory.iris_memory.lifelike.initiative import gate as g
from astrbot_plugin_iris_memory.iris_memory.lifelike.initiative.pool import InitiativeCandidate
from astrbot_plugin_iris_memory.iris_memory.proactive.decision import build_candidates_block, candidate_ids
from astrbot_plugin_iris_memory.iris_memory.proactive.parser import parse_decision
from astrbot_plugin_iris_memory.iris_memory.proactive.proactive import ProactiveEngine
from tests.lifelike.v3_util import written_tables

OWNER = "100000001"
SCOPE = f"private:{OWNER}"
GROUP = "-1001000000001"
UMO = f"userbot:FriendMessage:{OWNER}"
T0 = 1_793_667_600.0


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class FakeConfig:
    def __init__(self, **values):
        self.values = {"proactive.silent_sessions": []}
        self.values.update(values)

    def get(self, key, default=None):
        return self.values.get(key, default)


class FakeState:
    def get_whitelist(self):
        return [SCOPE, GROUP]

    def resume_after_owner_message(self, gid):
        return True


async def _make(tmp_path: Path, **values) -> ContinuityService:
    clock = Clock()
    s = ContinuityService(
        context=SimpleNamespace(get_platform_inst=lambda pid: None, get_config=lambda: {}),
        config_source=FakeConfig(**values),
        state=FakeState(),
        owner_ids_get=lambda: [OWNER],
        silent_sessions_get=lambda: [],
        data_dir=tmp_path / "data",
        llm_manager=SimpleNamespace(generate=AsyncMock(return_value='{"ops":[]}')),
        clock=clock,
    )
    s.test_clock = clock
    await s.start()
    return s


@pytest_asyncio.fixture
async def svc(tmp_path):
    s = await _make(tmp_path)
    yield s
    await s.stop()


def _care_cand(svc, delivery=g.DELIVERY_PUSH, key="care:x", material="说发烧了，身体不太舒服"):
    return svc.initiative.pool.enqueue(
        InitiativeCandidate(
            scope_key=SCOPE, ctype=g.CTYPE_GROUP_CARE, dedupe_key=key, why_now=f"她在群里{material}（群里她自己说的）",
            material=material, expires_at=T0 + 86400, delivery=delivery,
            source_ref=["post:-1001000000004:5", "cat:sick", "band:明显"],
        )
    )


@pytest.mark.asyncio
async def test_wired_by_default(svc):
    assert svc.bridge.initiative is svc.initiative
    assert svc.initiative.care is svc.care
    assert [n for n, _ in svc.assembler.lifelike_sources] == ["initiative_focus", "initiative_passive"]


@pytest.mark.asyncio
async def test_bridge_candidates_merge_only_owner_private(svc):
    cid = _care_cand(svc)
    cands = await svc.bridge.candidates(SCOPE)
    assert [c["item_id"] for c in cands] == [f"cand:{cid}"]
    assert await svc.bridge.candidates(GROUP) == []
    assert await svc.bridge.has_push_candidate(SCOPE)
    assert not await svc.bridge.has_push_candidate(GROUP)


@pytest.mark.asyncio
async def test_turn_flow_through_bridge(svc):
    cid = _care_cand(svc)
    start = await svc.bridge.begin_turn(SCOPE)
    assert start.turn is not None and start.candidates[0]["item_id"] == f"cand:{cid}"
    tid = start.turn["turn_id"]
    await svc.bridge.mark_turn(tid, "generating", item_id=f"cand:{cid}", llm_calls=1)
    assert svc.initiative.pool.get(cid)["status"] == "offered"
    ok = await svc.bridge.presend_check(tid, UMO)
    assert ok.ok, ok.reason
    ok_review = await svc.bridge.review_speech(tid, "刚在群里看到你说发烧了，还好吗")
    assert ok_review == (True, "")
    assert (await svc.bridge.review_speech(tid, "还好吗"))[0] is False
    # 候选过期 → 发送前复查拒发
    svc.test_clock.t = T0 + 2 * 86400
    res = await svc.bridge.presend_check(tid, UMO)
    assert not res.ok and res.reason == "focus_item_inactive"
    await svc.bridge.close_turn(tid, "skipped", "llm_declined")
    dec = svc.initiative.pool.recent_decisions(SCOPE, 1)[0]
    assert dec["turn_id"] == tid and dec["decision"] == "none" and dec["chosen_ctype"] == "group_care"


@pytest.mark.asyncio
async def test_presend_rejects_pool_id_without_coordinator(svc):
    cid = _care_cand(svc)
    start = await svc.bridge.begin_turn(SCOPE)
    tid = start.turn["turn_id"]
    await svc.bridge.mark_turn(tid, "generating", item_id=f"cand:{cid}")
    svc.bridge.initiative = None
    res = await svc.bridge.presend_check(tid, UMO)
    assert not res.ok and res.reason == "focus_item_inactive"
    assert await svc.bridge.review_speech(tid, "x") == (True, "")
    assert not await svc.bridge.has_push_candidate(SCOPE)
    await svc.bridge.close_turn(tid, "skipped", "x")


@pytest.mark.asyncio
async def test_off_mode_bridge_unchanged(tmp_path):
    s = await _make(tmp_path, lifelike_initiative_mode="off", lifelike_care_bridge_mode="off")
    try:
        assert await s.bridge.candidates(SCOPE) == []
        start = await s.bridge.begin_turn(SCOPE)
        await s.bridge.close_turn(start.turn["turn_id"], "skipped", "llm_declined")
        await s.note_owner_message(SCOPE, 10, T0, text="我去开会了", sender_id=OWNER)
        text = await s.assemble(SCOPE, purpose="reply", sender_id=OWNER)
        assert "可以顺口提的" not in text
        # v3 迁移已建表：off 时不写任何行
        assert not written_tables(s.store, ["initiative_candidates", "initiative_decisions", "care_signals"])
    finally:
        await s.stop()


@pytest.mark.asyncio
async def test_context_passive_only_in_owner_private(svc):
    _care_cand(svc, delivery=g.DELIVERY_PASSIVE)
    text = await svc.assemble(SCOPE, purpose="reply", sender_id=OWNER)
    assert "可以顺口提的" in text and "在群里" in text
    # 群聊组装永远不含关心桥内容（M30）
    group_text = await svc.assemble(GROUP, purpose="reply", sender_id=OWNER)
    assert "群里" not in group_text and "发烧" not in group_text
    # 主人私聊 scope 但发送者不是主人 → 不注入
    other = await svc.assemble(SCOPE, purpose="reply", sender_id="12345")
    assert "发烧" not in other


@pytest.mark.asyncio
async def test_context_focus_for_proactive(svc):
    cid = _care_cand(svc)
    text = await svc._speech_context(SCOPE, f"cand:{cid}")
    assert "这次想说的事" in text and "在群里看到" in text
    plain = await svc._speech_context(SCOPE, None)
    assert "这次想说的事" not in plain


@pytest.mark.asyncio
async def test_owner_message_hooks(svc):
    _care_cand(svc)
    await svc.note_owner_message(SCOPE, 11, T0 + 5, text="别根据群里的消息关心我了", sender_id=OWNER)
    assert svc.care.is_off()
    assert svc.initiative.pool.open(SCOPE, T0 + 6, ctype=g.CTYPE_GROUP_CARE) == []
    await svc.note_owner_message(SCOPE, 12, T0 + 7, text="我去上课了", sender_id=OWNER)
    assert svc.initiative._busy_until[SCOPE] == pytest.approx(T0 + 7 + 2 * 3600)


def test_decision_prompt_and_parser_compat():
    cand = {
        "item_id": "cand:abc", "candidate_id": "cand:abc", "kind": "group_care", "ctype": "group_care",
        "topic": "她在群里说发烧了（群里她自己说的）", "summary": "说发烧了，身体不太舒服",
    }
    block = build_candidates_block([cand], T0)
    assert "id=cand:abc" in block and "类型:群里看到的近况" in block
    assert candidate_ids([cand]) == {"cand:abc"}
    raw = '{"action":"speak","topic":"关心一下","why_now":"群里说发烧","topic_source":"recent_context","candidate_id":"cand:abc"}'
    d = parse_decision(raw, mode="initiate")
    assert d.item_id == "cand:abc"
    raw2 = '{"action":"speak","topic":"t","why_now":"w","topic_source":"recent_context","item_id":"i1","candidate_id":"cand:x"}'
    assert parse_decision(raw2, mode="initiate").item_id == "i1"


class _ReviewBridge:
    def __init__(self, verdicts):
        self.verdicts = list(verdicts)
        self.calls = []

    async def review_speech(self, turn_id, text):
        self.calls.append(text)
        return self.verdicts.pop(0)


def _engine(bridge, regen="刚在群里看到你说发烧了"):
    e = object.__new__(ProactiveEngine)
    e._bridge = bridge
    e._config = SimpleNamespace(proactive_max_message_len=500, initiative_on=True)
    e._generate_speech = AsyncMock(return_value=regen)
    return e


def test_engine_review_ok_regen_block():
    d = SimpleNamespace(item_id="cand:x")
    e = _engine(_ReviewBridge([(True, "")]))
    assert asyncio.run(e._review_pool_speech("g", "u", "p", d, "t", "原文")) == ("原文", "")
    e = _engine(_ReviewBridge([(False, "no_source_phrase"), (True, "")]))
    assert asyncio.run(e._review_pool_speech("g", "u", "p", d, "t", "原文")) == ("刚在群里看到你说发烧了", "")
    e = _engine(_ReviewBridge([(False, "no_source_phrase"), (False, "no_source_phrase")]))
    assert asyncio.run(e._review_pool_speech("g", "u", "p", d, "t", "原文"))[1] == "no_source_phrase"
    e = _engine(_ReviewBridge([(False, "x")]), regen="")
    assert asyncio.run(e._review_pool_speech("g", "u", "p", d, "t", "原文"))[1] == "x"
    e = _engine(SimpleNamespace())
    assert asyncio.run(e._review_pool_speech("g", "u", "p", d, "t", "原文")) == ("原文", "")


def test_engine_initiative_flag_and_pool_pending():
    e = _engine(SimpleNamespace(has_push_candidate=AsyncMock(return_value=True)))
    assert e._initiative_on()
    assert asyncio.run(e._pool_push_pending("private:1", T0))
    e._bridge = SimpleNamespace(has_push_candidate=AsyncMock(side_effect=RuntimeError("x")))
    assert not asyncio.run(e._pool_push_pending("private:1", T0))
    e._bridge = SimpleNamespace()
    assert not asyncio.run(e._pool_push_pending("private:1", T0))
    e._config = SimpleNamespace()
    assert not e._initiative_on()


def test_engine_pool_urgent():
    e = _engine(SimpleNamespace(has_urgent_candidate=AsyncMock(return_value=True)))
    assert asyncio.run(e._pool_urgent("private:1", T0))
    e._bridge = SimpleNamespace(has_urgent_candidate=AsyncMock(side_effect=RuntimeError("x")))
    assert not asyncio.run(e._pool_urgent("private:1", T0))
    e._bridge = SimpleNamespace()
    assert not asyncio.run(e._pool_urgent("private:1", T0))


@pytest.mark.asyncio
async def test_bridge_urgent_only_for_crisis(svc):
    _care_cand(svc)
    assert not await svc.bridge.has_urgent_candidate(SCOPE)
    svc.initiative.pool.enqueue(
        InitiativeCandidate(
            scope_key=SCOPE, ctype=g.CTYPE_GROUP_CARE, dedupe_key="care:crisis", why_now="w", material="m",
            expires_at=T0 + 86400, source_ref=["cat:mood_low", "band:明显", "flag:crisis"],
        )
    )
    assert await svc.bridge.has_urgent_candidate(SCOPE)
    assert not await svc.bridge.has_urgent_candidate(GROUP)
