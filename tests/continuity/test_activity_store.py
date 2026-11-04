"""活人感 P0-8：账本只读查询（主人作息时间戳 / 最后互动）与 bridge.activity_snapshot。"""

import time
from types import SimpleNamespace

import pytest

from astrbot_plugin_iris_memory.iris_memory.continuity import models as m
from astrbot_plugin_iris_memory.iris_memory.continuity.proactive_bridge import (
    ProactiveBridge,
    SendPolicy,
)
from astrbot_plugin_iris_memory.iris_memory.continuity.store import ContinuityStore

OWNER = "123456789"
SCOPE = f"private:{OWNER}"


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock(time.time())


@pytest.fixture
def store(tmp_path, clock):
    s = ContinuityStore(tmp_path / "continuity" / "continuity.db", clock=clock)
    s.open()
    yield s
    s.close()


def _sent(store, origin, scope=SCOPE, status=m.OUT_SENT):
    oid = store.add_outbound(scope, origin, "t", m.OUT_SENDING, "bot")
    if status == m.OUT_SENT:
        store.mark_outbound(oid, m.OUT_SENT, [oid + 100])
    elif status != m.OUT_SENDING:
        store.mark_outbound(oid, status)
    return oid


class TestStoreQueries:
    def test_last_interaction_empty_is_none(self, store):
        assert store.last_interaction_ts(SCOPE) is None

    def test_last_interaction_uses_watermark_and_sent_replies(self, store, clock):
        now = clock.t
        store.advance_watermark(SCOPE, 10, now - 3600)
        assert store.last_interaction_ts(SCOPE) == pytest.approx(now - 3600)
        clock.t = now - 600
        _sent(store, "reply")
        assert store.last_interaction_ts(SCOPE) == pytest.approx(now - 600)

    def test_proactive_failed_and_other_scopes_do_not_count(self, store, clock):
        now = clock.t
        store.advance_watermark(SCOPE, 10, now - 7200)
        clock.t = now - 60
        _sent(store, "proactive")  # 她自己主动发的不算互动
        _sent(store, "reply", status=m.OUT_FAILED)
        _sent(store, "reply", scope="private:1")
        assert store.last_interaction_ts(SCOPE) == pytest.approx(now - 7200)

    def test_activity_timestamps_filters_sender_and_since(self, store, clock):
        now = clock.t
        store.record_inbound(SCOPE, 1, OWNER, "a", now - 40 * 86400)
        store.record_inbound(SCOPE, 2, OWNER, "b", now - 86400)
        store.record_inbound(SCOPE, 3, "someone", "c", now - 3600)
        clock.t = now - 1800
        _sent(store, "reply")
        _sent(store, "proactive")
        clock.t = now
        stamps = sorted(store.activity_timestamps(SCOPE, now - 28 * 86400, OWNER))
        assert stamps == [pytest.approx(now - 86400), pytest.approx(now - 1800)]
        assert len(store.activity_timestamps(SCOPE, now - 28 * 86400)) == 3


class TestBridgeSnapshot:
    def _bridge(self, store, clock):
        return ProactiveBridge(
            store,
            SimpleNamespace(),
            policy_get=lambda: SendPolicy(owner_ids=frozenset({OWNER})),
            clock=clock,
        )

    @pytest.mark.asyncio
    async def test_snapshot(self, store, clock):
        now = clock.t
        store.advance_watermark(SCOPE, 5, now - 7200)
        store.record_inbound(SCOPE, 5, OWNER, "a", now - 7200)
        bridge = self._bridge(store, clock)
        snap = await bridge.activity_snapshot(SCOPE, 28)
        assert snap.last_interaction == pytest.approx(now - 7200)
        assert snap.owner_timestamps == [pytest.approx(now - 7200)]
        light = await bridge.activity_snapshot(SCOPE, 28, with_history=False)
        assert light.owner_timestamps == [] and light.last_interaction == pytest.approx(now - 7200)

    def test_owner_scope_check(self, store, clock):
        bridge = self._bridge(store, clock)
        assert bridge.is_owner_private_scope(SCOPE) is True
        assert bridge.is_owner_private_scope("private:1") is False
        assert bridge.is_owner_private_scope("-1001") is False
