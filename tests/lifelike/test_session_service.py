"""ContinuityService 接入 SessionExtractor：off 时与现网一致；on 时收尾触发会话抽取。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from astrbot_plugin_iris_memory.iris_memory.continuity import models as m
from astrbot_plugin_iris_memory.iris_memory.continuity.service import ContinuityService
from astrbot_plugin_iris_memory.iris_memory.lifelike.memory import session_extractor as se

OWNER = "123456789"
SCOPE = f"private:{OWNER}"
T0 = 1_793_000_000.0


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Cfg:
    def __init__(self, **values) -> None:
        self.values = {
            "continuity.extract_debounce_s": 0,
            "l1_buffer.summary_provider": "deepseek",
            **values,
        }

    def get(self, key, default=None):
        return self.values.get(key, default)


class LM:
    def __init__(self, session_reply: str = '{"items":[]}') -> None:
        self.calls: list[dict] = []
        self.session_reply = session_reply

    async def generate(self, prompt, **kw):
        self.calls.append(kw)
        if kw["module"] == se.MODULE:
            return self.session_reply
        return '{"ops":[]}'

    async def get_token_stats(self, module):
        n = sum(1 for c in self.calls if c["module"] == module)
        return {"total_input_tokens": 100 * n, "total_output_tokens": 10 * n}


def _svc(tmp_path, cfg: Cfg, lm: LM, clock: Clock) -> ContinuityService:
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


async def _settle(svc, lm, pred, rounds=200):
    for _ in range(rounds):
        await asyncio.sleep(0.01)
        if pred() and svc.worker.is_idle() and svc.session_extractor.is_idle():
            return


@pytest.mark.asyncio
async def test_off_by_default_matches_legacy(tmp_path):
    clock, lm = Clock(), LM()
    svc = _svc(tmp_path, Cfg(), lm, clock)
    await svc.start()
    try:
        assert svc.session_extractor.mode() == "off" and not svc.session_extractor.running
        await svc.note_owner_message(SCOPE, 10, T0, text="刚看完一部电影，挺好看的，主角演得很好", sender_id=OWNER)
        await svc.note_owner_message(SCOPE, 11, T0 + 5, text="晚安", sender_id=OWNER)
        await svc.record_reply_sent(SCOPE, "晚安，好梦", [900], reply_to_msg_id=11)
        await _settle(svc, lm, lambda: svc.store.get_inbound(SCOPE, 11)["extract_status"] != "pending")
        assert not svc.session_extractor.running
        assert svc.store.get_cursor(se.CURSOR_PREFIX + SCOPE) is None
        assert all(c["module"] != se.MODULE for c in lm.calls)
        assert not any(k.startswith("lifelike.") for k in svc.store.get_counters())
        # 旧行为：无时间词的消息被预筛跳过
        assert svc.store.get_inbound(SCOPE, 10)["extract_status"] == m.INBOUND_SKIPPED
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_on_mode_closure_triggers_session_extract(tmp_path, monkeypatch):
    monkeypatch.setattr(se, "SETTLE_S", 0.0)
    monkeypatch.setattr(se, "BUSY_RECHECK_S", 0.02)
    clock = Clock()
    reply = json.dumps({"items": [{"op": "create", "kind": "unfinished_topic", "topic": "看的电影",
                                   "summary": "她今天看了电影，觉得主角演得好", "evidence": "刚看完一部电影"}]},
                       ensure_ascii=False)
    lm = LM(reply)
    svc = _svc(tmp_path, Cfg(lifelike_session_extract_mode="on"), lm, clock)
    await svc.start()
    try:
        assert svc.session_extractor.running
        await svc.note_owner_message(SCOPE, 10, T0, text="刚看完一部电影，挺好看的，主角演得很好", sender_id=OWNER)
        await _settle(svc, lm, lambda: svc.store.get_inbound(SCOPE, 10)["extract_status"] != "pending")
        clock.t += 60
        await svc.note_owner_message(SCOPE, 11, clock.t, text="晚安", sender_id=OWNER)
        await _settle(svc, lm, lambda: any(c["module"] == se.MODULE for c in lm.calls))
        session_calls = [c for c in lm.calls if c["module"] == se.MODULE]
        assert len(session_calls) == 1 and session_calls[0]["provider_id"] == "deepseek"
        topics = [i["topic"] for i in svc.store.list_items(scope_key=SCOPE, statuses=(m.STATUS_ACTIVE,))]
        assert topics == ["看的电影"]
        assert svc.store.get_inbound(SCOPE, 10)["extract_status"] == m.INBOUND_DONE
        c = svc.store.get_counters()
        assert c["lifelike.budget.calls:lifelike_session_extract"] == 1
        assert c["lifelike.budget.tokens:lifelike_session_extract"] == 110  # token_stats 差值
    finally:
        await svc.stop()
    assert not svc.session_extractor.running


@pytest.mark.asyncio
async def test_maintenance_starts_after_runtime_switch(tmp_path):
    clock, lm = Clock(), LM()
    cfg = Cfg()
    svc = _svc(tmp_path, cfg, lm, clock)
    await svc.start()
    try:
        assert not svc.session_extractor.running
        cfg.values["lifelike_session_extract_mode"] = "shadow"
        svc._last_maintenance = 0.0
        clock.t += 10_000
        await svc._maintenance()
        assert svc.session_extractor.running
    finally:
        await svc.stop()
