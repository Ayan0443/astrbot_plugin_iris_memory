"""main.py 接线：主人群发言只用于解除冷静期；/iris_rel 只在主人私聊响应。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from astrbot_plugin_iris_memory.iris_memory.continuity.service import ContinuityService
from astrbot_plugin_iris_memory.iris_memory.lifelike.relation import models as rm
from astrbot_plugin_iris_memory.main import IrisMemoryPlugin

from .test_relation_service import LM, OWNER, SCOPE, Cfg, Clock
from ..continuity.test_integration_main import FakeEvent
from tests.lifelike.v3_util import written_tables


def _plugin(tmp_path, cfg, lm, clock):
    svc = ContinuityService(
        context=SimpleNamespace(get_platform_inst=lambda pid: None),
        config_source=cfg,
        state=SimpleNamespace(get_whitelist=lambda: [], resume_after_owner_message=lambda g: True),
        owner_ids_get=lambda: [OWNER],
        silent_sessions_get=lambda: [],
        data_dir=tmp_path / "data",
        llm_manager=lm,
        clock=clock,
    )
    p = object.__new__(IrisMemoryPlugin)
    p._continuity = svc
    p._state = SimpleNamespace(resume_after_owner_message=lambda g: False)
    return p, svc


async def _drain(gen):
    return [r async for r in gen]


@pytest.mark.asyncio
async def test_group_message_from_owner_releases_modes(tmp_path):
    clock, lm = Clock(), LM('{"hurt": true}')
    cfg = Cfg(lifelike_relation_mode="on", **{"continuity.observe_chat_ids": "-1001000000001"})
    p, svc = _plugin(tmp_path, cfg, lm, clock)
    await svc.start()
    try:
        await p._continuity_on_owner_message(FakeEvent(text="闭嘴", sender=OWNER, msg_id=300))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if svc.relation.is_idle() and svc.relation.repo.modes(SCOPE):
                break
        assert svc.relation.repo.modes(SCOPE, live_at=clock.t)
        await p._continuity_on_owner_message(FakeEvent(text="感冒了好难受", group_id="-1001000000001", sender=OWNER, msg_id=301))
        assert not svc.relation.repo.modes(SCOPE, live_at=clock.t)
        md = svc.relation.repo.modes(SCOPE)[0]
        assert md["release_reason"] == rm.REL_OWNER_DISTRESS_GROUP
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_group_hook_is_noop_when_off(tmp_path):
    clock, lm = Clock(), LM()
    p, svc = _plugin(tmp_path, Cfg(lifelike_relation_mode="off"), lm, clock)
    await svc.start()
    try:
        await p._continuity_on_owner_message(FakeEvent(text="感冒了好难受", group_id="-1001000000001", sender=OWNER, msg_id=301))
        assert not written_tables(svc.store, ["relationship_events"])  # v3：off 不写行
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_iris_rel_command_owner_private_only(tmp_path):
    clock, lm = Clock(), LM()
    p, svc = _plugin(tmp_path, Cfg(), lm, clock)
    await svc.start()
    try:
        ev = FakeEvent(text="/iris_rel why", sender=OWNER, msg_id=400)
        ev.plain_result = lambda t: t
        out = await _drain(p.iris_rel(ev))
        assert out and "近期基调" in out[0] and "shadow" in out[0]
        ev = FakeEvent(text="/iris_rel why", group_id="-1001000000001", sender=OWNER, msg_id=401)
        ev.plain_result = lambda t: t
        assert "私聊" in (await _drain(p.iris_rel(ev)))[0]
        ev = FakeEvent(text="/iris_rel why", sender="999", msg_id=402)
        ev.plain_result = lambda t: t
        assert "主人私聊" in (await _drain(p.iris_rel(ev)))[0]
    finally:
        await svc.stop()
