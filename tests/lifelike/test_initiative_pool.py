"""P1-8 候选池存储、决策记录、打分与协调器（tmp_path 库、可控时钟）"""

import asyncio
import sqlite3

import pytest

from astrbot_plugin_iris_memory.iris_memory.continuity.store import ContinuityStore
from astrbot_plugin_iris_memory.iris_memory.lifelike.initiative import gate as g
from astrbot_plugin_iris_memory.iris_memory.lifelike.initiative.coordinator import (
    InitiativeCoordinator,
    latency_band,
    normalize_mode,
)
from astrbot_plugin_iris_memory.iris_memory.lifelike.initiative.pool import (
    CandidatePool,
    CandidateScorer,
    InitiativeCandidate,
)
from tests.lifelike.v3_util import written_tables

OWNER = "100000001"
SCOPE = f"private:{OWNER}"
T0 = 1_793_667_600.0  # 2026-11-03 09:00 Asia/Shanghai


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    s = ContinuityStore(tmp_path / "c.db", clock=clock)
    s.open()
    yield s
    s.close()


def _tables(store):
    with store._lock:
        return {r[0] for r in store._conn().execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _cand(ctype="owner_post", key="k1", delivery=g.DELIVERY_PUSH, exp=T0 + 3 * 86400, refs=None, scope=SCOPE):
    return InitiativeCandidate(
        scope_key=scope, ctype=ctype, dedupe_key=key, why_now="她频道发了新帖", material="新帖讲了旅行",
        expires_at=exp, delivery=delivery, source_ref=refs or [],
    )


class TestPool:
    def test_schema_does_not_touch_user_version(self, store):
        before = store.get_schema_version()
        pool = CandidatePool(store)
        pool.open(SCOPE, T0)
        assert store.get_schema_version() == before
        assert {"initiative_candidates", "initiative_decisions"} <= _tables(store)

    def test_enqueue_dedupe_open(self, store, clock):
        pool = CandidatePool(store, clock)
        cid = pool.enqueue(_cand())
        assert cid
        assert pool.enqueue(_cand()) is None  # 同 dedupe_key 只入队一次
        rows = pool.open(SCOPE, T0)
        assert [r["cand_id"] for r in rows] == [cid]
        assert pool.get(f"cand:{cid}")["cand_id"] == cid
        assert pool.open(SCOPE, T0, delivery=g.DELIVERY_PASSIVE) == []

    def test_enqueue_rejects_group_scope_and_bad_values(self, store):
        pool = CandidatePool(store)
        with pytest.raises(ValueError):
            pool.enqueue(_cand(scope="-1001000000004"))
        with pytest.raises(ValueError):
            pool.enqueue(_cand(ctype="nonsense"))
        with pytest.raises(ValueError):
            pool.enqueue(_cand(delivery="loud"))
        bad = _cand(ctype=g.CTYPE_GROUP_CARE)
        bad.privacy = g.PRIVACY_TOPIC_ONLY
        with pytest.raises(ValueError):
            pool.enqueue(bad)

    def test_status_expire_suppress(self, store, clock):
        pool = CandidatePool(store, clock)
        a = pool.enqueue(_cand(key="a", exp=T0 + 10))
        b = pool.enqueue(_cand(key="b", ctype=g.CTYPE_GROUP_CARE))
        assert pool.set_status(a, "offered", only_from=("queued",), offered_at=T0)
        assert not pool.set_status(a, "used", only_from=("queued",))
        with pytest.raises(ValueError):
            pool.set_status(a, "weird")
        clock.t = T0 + 20
        expired = pool.expire(clock.t)
        assert [r["cand_id"] for r in expired] == [a]
        assert pool.suppress_open(SCOPE, g.CTYPE_GROUP_CARE) == [b]
        assert pool.get(b)["status"] == "suppressed"
        assert pool.recent_terminal(SCOPE, g.CTYPE_GROUP_CARE, ["suppressed"], 0)[0]["cand_id"] == b

    def test_decisions(self, store, clock):
        pool = CandidatePool(store, clock)
        did = pool.record_decision(scope_key=SCOPE, turn_id="t1", gate_result="ok", ranked=[{"cand_id": "x", "score": 1}],
                                   decision="speak", chosen_ctype="owner_post", sent_at=T0)
        assert pool.last_sent(SCOPE)["id"] == did
        assert pool.unsettled_sent(SCOPE)[0]["ranked"] == [{"cand_id": "x", "score": 1}]
        pool.settle_decision(did, True, "很快", "")
        assert pool.unsettled_sent(SCOPE) == []
        assert pool.response_stats(SCOPE, 0) == {"owner_post": (1, 1)}
        assert pool.recent_decisions(SCOPE, 3)[0]["replied"] == 1


class TestScorer:
    def test_type_order_and_fatigue(self):
        s = CandidateScorer()
        cands = [
            {"cand_id": "a", "ctype": "life_share", "created_at": T0},
            {"cand_id": "b", "ctype": "group_care", "created_at": T0},
            {"cand_id": "c", "ctype": "owner_post", "created_at": T0},
        ]
        ranked = s.rank(cands, T0, k=2)
        assert [c["cand_id"] for c, _ in ranked] == ["b", "c"]
        ranked = s.rank(cands, T0, fatigue={"group_care": 5}, k=3)
        assert ranked[-1][0]["cand_id"] == "b"

    def test_helpers(self):
        assert normalize_mode("ON") == "on"
        assert normalize_mode("bogus") == "off"
        assert normalize_mode(None) == "off"
        assert latency_band(10) == "很快"
        assert latency_band(1200) == "过了一会"
        assert latency_band(7200) == "很久"


def _coord(store, clock, mode="on", **cfg):
    conf = {"lifelike_initiative_mode": mode, **cfg}
    return InitiativeCoordinator(
        store, lambda k, d=None: conf.get(k, d), owner_private=lambda s: s == SCOPE, clock=clock
    )


class TestCoordinator:
    def test_off_touches_nothing(self, tmp_path, clock):
        s = ContinuityStore(tmp_path / "off.db", clock=clock)
        s.open()
        try:
            co = _coord(s, clock, mode="off")
            items = [{"item_id": "i1"}]
            assert asyncio.run(co.merge_candidates(SCOPE, T0, items)) is items
            co.note_turn_begin(SCOPE, "t1")
            asyncio.run(co.note_turn_end("t1", "sent", ""))
            asyncio.run(co.on_owner_message(SCOPE, T0, "我去开会了"))
            assert not written_tables(s, ["initiative_candidates", "initiative_decisions"])  # v3：off 不写行
        finally:
            s.close()

    def test_group_scope_never_gets_pool(self, store, clock):
        co = _coord(store, clock)
        co.pool.enqueue(_cand())
        assert asyncio.run(co.merge_candidates("-1001000000004", T0, [])) == []

    def test_on_merges_and_annotates(self, store, clock):
        co = _coord(store, clock)
        cid = co.pool.enqueue(_cand())
        out = asyncio.run(co.merge_candidates(SCOPE, T0, [{"item_id": "i1", "topic": "面试"}]))
        assert out[0]["candidate_id"] == "item:i1" and out[0]["ctype"] == "followup_item"
        assert out[1]["item_id"] == f"cand:{cid}" and out[1]["kind"] == "owner_post"
        assert "score" not in out[1]

    def test_shadow_returns_items_only_but_records(self, store, clock):
        co = _coord(store, clock, mode="shadow")
        co.pool.enqueue(_cand())
        items = [{"item_id": "i1"}]
        assert asyncio.run(co.merge_candidates(SCOPE, T0, items)) is items
        co.note_turn_begin(SCOPE, "t1")
        asyncio.run(co.note_turn_end("t1", "skipped", "llm_declined"))
        row = co.pool.recent_decisions(SCOPE, 1)[0]
        assert row["mode"] == "shadow" and row["decision"] == "none" and row["gate_result"] == "ok"

    def test_passive_types_not_offered(self, store, clock):
        co = _coord(store, clock)
        co.pool.enqueue(_cand(ctype="life_share"))
        assert asyncio.run(co.merge_candidates(SCOPE, T0, [])) == []

    def test_turn_lifecycle_sent_then_cap_and_gap(self, store, clock):
        co = _coord(store, clock, lifelike_push_daily_cap=1)
        a = co.pool.enqueue(_cand(key="a"))
        b = co.pool.enqueue(_cand(key="b", ctype="moment_followup"))
        asyncio.run(co.merge_candidates(SCOPE, T0, []))
        co.note_turn_begin(SCOPE, "t1")
        asyncio.run(co.note_chosen("t1", f"cand:{a}"))
        assert co.pool.get(a)["status"] == "offered"
        assert co.presend_ok(f"cand:{a}")
        asyncio.run(co.note_turn_end("t1", "sent", ""))
        assert co.pool.get(a)["status"] == "used"
        assert not co.presend_ok(f"cand:{a}")
        dec = co.pool.recent_decisions(SCOPE, 1)[0]
        assert dec["decision"] == "speak" and dec["chosen_cand"] == f"cand:{a}" and dec["sent_at"] == T0
        # 上限 1 → 另一候选被 G-cap 挡住
        clock.t = T0 + 60
        out = asyncio.run(co.merge_candidates(SCOPE, clock.t, []))
        assert out == []
        assert co._last_offer[SCOPE].gated[0][1] == g.G_CAP
        assert b

    def test_gap_and_backoff_and_owner_resume(self, store, clock):
        co = _coord(store, clock)
        a = co.pool.enqueue(_cand(key="a"))
        co.pool.enqueue(_cand(key="b"))
        co.pool.enqueue(_cand(key="c", ctype="moment_followup"))
        asyncio.run(co.merge_candidates(SCOPE, T0, []))
        co.note_turn_begin(SCOPE, "t1")
        asyncio.run(co.note_chosen("t1", f"cand:{a}"))
        asyncio.run(co.note_turn_end("t1", "sent", ""))
        clock.t = T0 + 3600
        asyncio.run(co.merge_candidates(SCOPE, clock.t, []))
        reasons = {c["ctype"]: r for c, r in co._last_offer[SCOPE].gated}
        assert reasons["owner_post"] == g.G_DUP and reasons["moment_followup"] == g.G_GAP
        # 7 小时无回复：同类型 backoff（G-dup 24h 先命中），换类型且间隔翻倍（6h）→ 通过
        clock.t = T0 + 7 * 3600
        out = asyncio.run(co.merge_candidates(SCOPE, clock.t, []))
        assert [c["ctype"] for c in out] == ["moment_followup"]
        assert co.pool.recent_decisions(SCOPE, 1)[0]["replied"] == 0  # 超 6h 结算为未回复

    def test_settle_reply_and_unwilling_cooldown(self, store, clock):
        co = _coord(store, clock)
        a = co.pool.enqueue(_cand(key="a"))
        asyncio.run(co.merge_candidates(SCOPE, T0, []))
        co.note_turn_begin(SCOPE, "t1")
        asyncio.run(co.note_chosen("t1", f"cand:{a}"))
        asyncio.run(co.note_turn_end("t1", "sent", ""))
        asyncio.run(co.on_owner_message(SCOPE, T0 + 120, "不想聊这个"))
        dec = co.pool.recent_decisions(SCOPE, 1)[0]
        assert dec["replied"] == 1 and dec["reply_latency_band"] == "很快" and dec["reply_tone"] == "不想聊"
        co.pool.enqueue(_cand(key="z"))
        clock.t = T0 + 2 * 86400
        asyncio.run(co.merge_candidates(SCOPE, clock.t, []))
        assert co._last_offer[SCOPE].gated[0][1] == g.G_COOLDOWN

    def test_not_sent_requeues_and_blocked(self, store, clock):
        co = _coord(store, clock)
        a = co.pool.enqueue(_cand(key="a"))
        asyncio.run(co.merge_candidates(SCOPE, T0, []))
        co.note_turn_begin(SCOPE, "t1")
        asyncio.run(co.note_chosen("t1", f"cand:{a}"))
        asyncio.run(co.note_turn_end("t1", "cancelled", "new_inbound"))
        assert co.pool.get(a)["status"] == "queued"
        co.note_turn_begin(SCOPE, "t2")
        asyncio.run(co.note_chosen("t2", f"cand:{a}"))
        asyncio.run(co.note_turn_end("t2", "skipped", "blocked_style:no_source_phrase"))
        assert co.pool.get(a)["status"] == "blocked"
        assert co.pool.recent_decisions(SCOPE, 1)[0]["decision"] == "blocked_style"

    def test_uncertain_counts_as_sent(self, store, clock):
        co = _coord(store, clock)
        a = co.pool.enqueue(_cand(key="a"))
        co.note_turn_begin(SCOPE, "t1")
        asyncio.run(co.note_chosen("t1", f"cand:{a}"))
        asyncio.run(co.note_turn_end("t1", "uncertain", ""))
        assert co.pool.get(a)["status"] == "used"

    def test_item_turn_recorded_as_followup(self, store, clock):
        co = _coord(store, clock)
        co.note_turn_begin(SCOPE, "t1")
        asyncio.run(co.note_chosen("t1", "item-123"))
        asyncio.run(co.note_turn_end("t1", "sent", ""))
        row = co.pool.recent_decisions(SCOPE, 1)[0]
        assert row["chosen_ctype"] == "followup_item" and row["gate_result"] == "no_offer"

    def test_busy_blocks_until_back(self, store, clock):
        co = _coord(store, clock)
        co.pool.enqueue(_cand(key="a"))
        asyncio.run(co.on_owner_message(SCOPE, T0, "我去开会了"))
        asyncio.run(co.merge_candidates(SCOPE, T0 + 60, []))
        assert co._last_offer[SCOPE].gated[0][1] == g.G_BUSY
        asyncio.run(co.on_owner_message(SCOPE, T0 + 120, "开完了"))
        assert asyncio.run(co.merge_candidates(SCOPE, T0 + 180, []))

    def test_budget_exhausted_blocks_pool(self, store, clock):
        class B:
            def usage(self):
                return {"calls": 80, "calls_limit": 80, "tokens": 0, "tokens_limit": 10}

        co = _coord(store, clock)
        co._budget = B()
        co.pool.enqueue(_cand(key="a"))
        asyncio.run(co.merge_candidates(SCOPE, T0, []))
        assert co._last_offer[SCOPE].gated[0][1] == g.G_BUDGET

    def test_review_generic_manipulation(self, store, clock):
        co = _coord(store, clock)
        a = co.pool.enqueue(_cand(key="a"))
        co.note_turn_begin(SCOPE, "t1")
        assert asyncio.run(co.review_speech("t1", "随便")) == (True, "")  # 未选 pool 候选
        asyncio.run(co.note_chosen("t1", f"cand:{a}"))
        assert asyncio.run(co.review_speech("t1", "你都不理我")) == (False, "manipulation")
        assert asyncio.run(co.review_speech("t1", "看到你发新帖了")) == (True, "")

    def test_focus_section_generic_and_scope_check(self, store, clock):
        co = _coord(store, clock)
        a = co.pool.enqueue(_cand(key="a"))
        text = asyncio.run(co.focus_section(SCOPE, f"cand:{a}", True))
        assert "这次想说的事" in text and "新帖讲了旅行" in text
        assert asyncio.run(co.focus_section(SCOPE, f"cand:{a}", False)) is None
        assert asyncio.run(co.focus_section(SCOPE, "item-1", True)) is None
        assert asyncio.run(co.focus_section("private:other", f"cand:{a}", True)) is None

    def test_failures_trip_breaker(self, store, clock):
        co = _coord(store, clock)

        def boom(*a, **k):
            raise sqlite3.OperationalError("x")

        co._build_offer = boom
        items = [{"item_id": "i"}]
        for _ in range(5):
            assert asyncio.run(co.merge_candidates(SCOPE, T0, items)) is items
        assert co.paused
