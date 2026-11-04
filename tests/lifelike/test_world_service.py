"""ContinuityService 接入生活系统：off 与 3ad413e 一致；shadow 生成/记录不注入；on 注入投影；
被动与主动两个发出后接入点都会核对自述。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from astrbot_plugin_iris_memory.iris_memory.config.defaults import HiddenConfig
from astrbot_plugin_iris_memory.iris_memory.continuity import models as m
from astrbot_plugin_iris_memory.iris_memory.continuity.service import ContinuityService
from astrbot_plugin_iris_memory.iris_memory.lifelike.world import models as wm
from astrbot_plugin_iris_memory.iris_memory.lifelike.world import service as wsvc

from ..continuity.test_bridge import UMO, FakePlatform, make_bridge, make_config  # noqa: F401
from tests.lifelike.v3_util import life_tables, written_tables

OWNER = "100000001"
SCOPE = f"private:{OWNER}"
GROUP = "-1001000000004"
DAY = "2026-11-03"


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Cfg:
    def __init__(self, **values) -> None:
        self.values = {"continuity.extract_debounce_s": 0, "l1_buffer.summary_provider": "deepseek", **values}

    def get(self, key, default=None):
        return self.values.get(key, default)


class LM:
    def __init__(self, claims=None) -> None:
        self.calls: list[dict] = []
        self.claims = claims or []

    async def generate(self, prompt, **kw):
        self.calls.append(kw)
        if kw["module"] == "lifelike_world":
            if "<message>" in prompt:
                return json.dumps({"claims": [{"span": s, "type": t} for s, t in self.claims]}, ensure_ascii=False)
            return "not json"  # DayPlanner → 模板兜底
        return '{"ops":[]}'

    async def get_token_stats(self, module):
        return None


def _svc(tmp_path, cfg, lm, clock) -> ContinuityService:
    return ContinuityService(
        context=SimpleNamespace(get_platform_inst=lambda pid: None),
        config_source=cfg,
        state=SimpleNamespace(get_whitelist=lambda: [], resume_after_owner_message=lambda g: True),
        owner_ids_get=lambda: [OWNER],
        silent_sessions_get=lambda: [],
        data_dir=tmp_path / "data",
        llm_manager=lm,
        clock=clock,
    )


def _tables(svc) -> set[str]:
    return {r[0] for r in svc.store._conn().execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_hidden_defaults():
    h = HiddenConfig()
    assert h.lifelike_enable is True and h.lifelike_world_mode == "shadow"
    assert h.lifelike_world_claim_audit is True and h.lifelike_world_plan_llm is True


@pytest.mark.parametrize(
    "values,mode",
    [
        ({}, "off"),  # 配置读不到：代码回退 off
        ({"lifelike_enable": False, "lifelike_world_mode": "on"}, "off"),  # 总开关优先
        ({"lifelike_enable": True, "lifelike_world_mode": "bogus"}, "off"),
        ({"lifelike_enable": "true", "lifelike_world_mode": "SHADOW"}, "shadow"),
        ({"lifelike_enable": True, "lifelike_world_mode": "on"}, "on"),
    ],
)
def test_mode_resolution(values, mode):
    w = wsvc.WorldService(None, lambda k, d: values.get(k, d))
    assert w.mode() == mode


@pytest.mark.asyncio
async def test_off_matches_legacy(tmp_path):
    clock = Clock(wm.at_hm(DAY, "16:00"))
    lm = LM([("我刚给绿萝换了水", "past_fact")])
    svc = _svc(tmp_path, Cfg(), lm, clock)
    await svc.start()
    try:
        assert not svc.world.running
        assert not written_tables(svc.store, life_tables(svc.store))  # v3 迁移已建表：off 不写行
        before = await svc.assemble(SCOPE, sender_id=OWNER)
        svc.assembler._world_section = None
        assert before == await svc.assemble(SCOPE, sender_id=OWNER)
        await svc.record_reply_sent(SCOPE, "我刚给绿萝换了水", [11], reply_to_msg_id=5)
        await svc._after_proactive_sent(SCOPE, 1, "我刚给绿萝换了水")
        await svc.world.drain()
        assert not [c for c in lm.calls if c["module"] == "lifelike_world"]
        assert not written_tables(svc.store, life_tables(svc.store))  # v3 迁移已建表：off 不写行
        assert "未开启" in await svc.world.today_text()
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_shadow_generates_and_records_without_injection(tmp_path):
    clock = Clock(wm.at_hm(DAY, "16:00"))
    lm = LM([("我刚给绿萝换了水", "past_fact")])
    cfg = Cfg(lifelike_enable=True, lifelike_world_mode="shadow", lifelike_world_plan_llm=False)
    svc = _svc(tmp_path, cfg, lm, clock)
    await svc.start()
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            if svc.world.store._ready and await asyncio.to_thread(svc.world.store.get_day, DAY):
                break
        assert svc.world.running
        day = await asyncio.to_thread(svc.world.store.get_day, DAY)
        assert day and day["generator"] == "template"
        text = await svc.assemble(SCOPE, sender_id=OWNER)
        assert "她的今天" not in text
        await svc.record_reply_sent(SCOPE, "我刚给绿萝换了水", [21], reply_to_msg_id=6)
        await svc.world.drain()
        claims = await asyncio.to_thread(svc.world.store.claims_since, 0)
        assert len(claims) == 1 and claims[0]["applied"] == 0 and claims[0]["scope_key"] == SCOPE
        assert not [e for e in await asyncio.to_thread(svc.world.store.list_events, [DAY]) if e.source == "chat_claim"]
        # 非主人私聊不核对
        await svc.record_reply_sent(GROUP, "我刚给绿萝换了水", [22])
        await svc.world.drain()
        assert len(await asyncio.to_thread(svc.world.store.claims_since, 0)) == 1
        assert "【她的今天" in await svc.world.today_text()
        # 计划生成未调模型（plan_llm=false），核对调了一次
        assert len([c for c in lm.calls if c["module"] == "lifelike_world"]) == 1
    finally:
        await svc.stop()
    assert not svc.world.running


@pytest.mark.asyncio
async def test_on_injects_projection_and_applies(tmp_path):
    clock = Clock(wm.at_hm(DAY, "16:00"))
    lm = LM([("我刚翻了翻海龟图鉴", "past_fact")])
    cfg = Cfg(lifelike_enable=True, lifelike_world_mode="on", lifelike_world_plan_llm=False)
    svc = _svc(tmp_path, cfg, lm, clock)
    await svc.start()
    try:
        await svc.world.tick_once()
        text = await svc.assemble(SCOPE, sender_id=OWNER)
        assert "<她的今天>" in text and "虚拟日常" in text
        assert not any(ch.isdigit() for ch in text.split("<她的今天>")[1].split("</她的今天>")[0])
        group = await svc.assemble(GROUP, sender_id="1")
        assert "<她的今天>\n现在：" in group and "已经做了：" not in group
        await svc._after_proactive_sent(SCOPE, 99, "我刚翻了翻海龟图鉴，挺有意思")
        await svc.world.drain()
        claims = await asyncio.to_thread(svc.world.store.claims_since, 0)
        assert claims[0]["applied"] == 1 and claims[0]["outbound_id"] == 99
        msg = await svc.world.undo(claims[0]["event_id"])
        assert msg == "已撤回。"
        assert "今天已经有计划了" in await svc.world.plan_now()
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_world_failure_falls_back(tmp_path, monkeypatch):
    clock = Clock(wm.at_hm(DAY, "16:00"))
    cfg = Cfg(lifelike_enable=True, lifelike_world_mode="on", lifelike_world_plan_llm=False)
    svc = _svc(tmp_path, cfg, LM(), clock)
    await svc.start()
    try:
        await svc.world.tick_once()

        def boom(*a, **k):
            raise RuntimeError("db")

        monkeypatch.setattr(svc.world.store, "list_events", boom)
        text = await svc.assemble(SCOPE, sender_id=OWNER)
        assert "她的今天" not in text
        for _ in range(wsvc.BREAKER_FAILS):
            await svc.world.tick_once()
        assert svc.world._paused()
        assert await svc.world.render(SCOPE, owner_private=True) == ""
        assert svc.world.schedule_audit(SCOPE, 1, "我刚看完书", "reply") is False
    finally:
        await svc.stop()


# ---------------------------------------------------------------- 主动发送接入点

@pytest.fixture
def bstate():
    from astrbot_plugin_iris_memory.iris_memory.proactive.state import StateManager

    return StateManager(make_config())


async def _send_once(tmp_path, name, bstate, behaviors, hook):
    from astrbot_plugin_iris_memory.iris_memory.continuity.store import ContinuityStore

    clock = Clock(wm.at_hm(DAY, "16:00"))
    store = ContinuityStore(tmp_path / name / "c.db", clock=clock)
    store.open()
    try:
        b = make_bridge(store, bstate, clock, FakePlatform(behaviors), after_sent=hook)
        start = await b.begin_turn(SCOPE)
        assert start.turn is not None
        return await b.send_part(start.turn["turn_id"], UMO, "我刚看完书", 0)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_bridge_after_sent_hook_only_on_sent(tmp_path, bstate):
    calls = []

    async def hook(scope, oid, text):
        calls.append((scope, oid, text))

    out = await _send_once(tmp_path, "a", bstate, [[701]], hook)
    assert out.status == m.OUT_SENT
    assert calls == [(SCOPE, out.outbound_id, "我刚看完书")]
    out2 = await _send_once(tmp_path, "b", bstate, [RuntimeError("net")], hook)
    assert out2.status != m.OUT_SENT and len(calls) == 1

    async def bad_hook(scope, oid, text):
        raise RuntimeError("x")

    out3 = await _send_once(tmp_path, "c", bstate, [[702]], bad_hook)
    assert out3.status == m.OUT_SENT  # 钩子异常不影响发送结果
    out4 = await _send_once(tmp_path, "d", bstate, [[703]], None)  # 不传钩子 = 旧行为
    assert out4.status == m.OUT_SENT
