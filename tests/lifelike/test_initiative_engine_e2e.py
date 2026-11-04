"""P1-8/P1-12 端到端：真实 ProactiveEngine + ProactiveBridge + 候选池（假平台、假 LLM、tmp 库）

group_care 候选 → 决策选中 → 生成缺来源词 → 重生成一次 → 带回执发送 → 候选 used、决策可追溯。
"""

from __future__ import annotations

import pytest

from astrbot_plugin_iris_memory.iris_memory.continuity import models as m
from astrbot_plugin_iris_memory.iris_memory.lifelike.initiative import gate as g
from astrbot_plugin_iris_memory.iris_memory.lifelike.initiative.coordinator import InitiativeCoordinator
from astrbot_plugin_iris_memory.iris_memory.lifelike.initiative.pool import InitiativeCandidate

from ..continuity.test_bridge import (
    DECISION_JSON,
    SCOPE,
    UMO,
    Clock,
    FakePlatform,
    make_config,
    make_engine,
)
from astrbot_plugin_iris_memory.iris_memory.continuity.store import ContinuityStore
from astrbot_plugin_iris_memory.iris_memory.proactive.state import StateManager


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    s = ContinuityStore(tmp_path / "continuity.db", clock=clock)
    s.open()
    yield s
    s.close()


@pytest.fixture
def state():
    return StateManager(make_config())


class _CareStub:
    def mode(self):
        return "on"

    def is_off(self):
        return False

    def source_texts(self, cand):
        return []

    def on_candidate_used(self, *a, **k):
        pass

    def on_candidate_blocked(self, *a, **k):
        pass

    def on_candidates_expired(self, *a, **k):
        pass


def _attach(env, store, clock, **conf):
    cfg = {"lifelike_initiative_mode": "on", **conf}
    coord = InitiativeCoordinator(
        store, lambda k, d=None: cfg.get(k, d), owner_private=env.bridge.is_owner_private_scope, clock=clock
    )
    coord.care = _CareStub()
    env.bridge.initiative = coord
    return coord


def _care(coord, clock):
    return coord.pool.enqueue(
        InitiativeCandidate(
            scope_key=SCOPE, ctype=g.CTYPE_GROUP_CARE, dedupe_key="care:s1",
            why_now="她在群里说发烧了（群里她自己说的）", material="说发烧了，身体不太舒服",
            expires_at=clock() + 86400, source_ref=["cat:sick", "band:明显"],
        )
    )


def _script(env, replies):
    seq = list(replies)

    async def generate_direct(**kw):
        env.prompts.append(kw)
        return seq.pop(0)

    env.engine._llm_manager.generate_direct = generate_direct


@pytest.mark.asyncio
async def test_group_care_regenerated_then_sent(store, state, clock):
    platform = FakePlatform([[31]])
    env = make_engine(store, state, clock, platform, speech="")
    coord = _attach(env, store, clock)
    cid = _care(coord, clock)
    _script(env, [DECISION_JSON.format(item_id=f'"cand:{cid}"'), "你还好吗？", "刚在群里看到你说发烧了，还好吗？"])
    result = await env.engine.attempt_initiate(SCOPE)
    assert result.startswith("已发起"), result
    assert f"cand:{cid}" in env.prompts[0]["prompt"] and "群里看到的近况" in env.prompts[0]["prompt"]
    assert platform.calls == [(UMO, "刚在群里看到你说发烧了，还好吗？")]
    assert coord.pool.get(cid)["status"] == "used"
    dec = coord.pool.recent_decisions(SCOPE, 1)[0]
    assert dec["decision"] == "speak" and dec["chosen_cand"] == f"cand:{cid}" and dec["chosen_ctype"] == "group_care"
    assert dec["sent_at"] is not None
    turn = store.turns_in_state((m.TURN_SENT,))[0]
    assert turn["item_id"] == f"cand:{cid}"
    # 出站账本 item_ids 不写入候选池 id
    rows = store.outbound_for_turn(turn["turn_id"])
    assert rows and rows[0]["item_ids"] == []


@pytest.mark.asyncio
async def test_group_care_without_source_is_blocked(store, state, clock):
    platform = FakePlatform([[31]])
    env = make_engine(store, state, clock, platform, speech="")
    coord = _attach(env, store, clock)
    cid = _care(coord, clock)
    _script(env, [DECISION_JSON.format(item_id=f'"cand:{cid}"'), "你还好吗？", "多喝热水哦"])
    result = await env.engine.attempt_initiate(SCOPE)
    assert "拦截" in result
    assert platform.calls == []
    assert coord.pool.get(cid)["status"] == "blocked"
    assert coord.pool.recent_decisions(SCOPE, 1)[0]["decision"] == "blocked_style"


@pytest.mark.asyncio
async def test_item_path_unchanged_with_coordinator(store, state, clock):
    """协调器在场、但决策选的是普通事项：发送路径与旧行为一致（不做来源检查）"""
    from ..continuity.test_bridge import add_item

    item = add_item(store, clock)
    platform = FakePlatform([[41]])
    env = make_engine(store, state, clock, platform, speech="面试怎么样？", item_id=f'"{item["item_id"]}"')
    coord = _attach(env, store, clock)
    result = await env.engine.attempt_initiate(SCOPE)
    assert result.startswith("已发起")
    assert platform.calls == [(UMO, "面试怎么样？")]
    dec = coord.pool.recent_decisions(SCOPE, 1)[0]
    assert dec["chosen_ctype"] == "followup_item" and dec["decision"] == "speak"
    assert store.get_item(item["item_id"])["ask_count"] == 1
