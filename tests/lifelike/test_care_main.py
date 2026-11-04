"""P1-12 main.py 接入：群聊 PRIV-02（删提及私聊的句子）与 /iris_care 只在主人私聊响应"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from astrbot.api.message_components import Plain

from astrbot_plugin_iris_memory.main import IrisMemoryPlugin

OWNER = "100000001"


def _plugin(mode="on"):
    p = object.__new__(IrisMemoryPlugin)
    p.config = SimpleNamespace(get=lambda k, d=None: {"lifelike_care_bridge_mode": mode}.get(k, d))
    return p


class Ev:
    def __init__(self, private=False, sender=OWNER, text=""):
        self._private = private
        self.sender = sender
        self.message_str = text
        self.stopped = False

    def is_private_chat(self):
        return self._private

    def get_sender_id(self):
        return self.sender

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text


def _result(text, llm=True):
    return SimpleNamespace(chain=[Plain(text)], is_llm_result=lambda: llm)


def test_priv02_removes_private_mentions_in_group():
    p = _plugin()
    res = _result("好呀。她私下跟我说过这事。周末去哪玩？")
    ev = Ev()
    assert p._care_priv02(ev, res) is False
    assert res.chain[0].text == "好呀。周末去哪玩？"
    assert not ev.stopped


def test_priv02_keeps_normal_private_chat_offer():
    """审查 #5："好呀我私聊你～"这类约定不删，整条回复照常发"""
    p = _plugin()
    res = _result("好呀我私聊你～")
    ev = Ev()
    assert p._care_priv02(ev, res) is False
    assert res.chain[0].text == "好呀我私聊你～" and not ev.stopped


def test_priv02_shadow_does_not_modify_reply():
    """审查 #2：shadow 只记日志，不改 chain、不拦截"""
    p = _plugin("shadow")
    for text in ("好呀。她私下跟我说过这事。周末去哪玩？", "刚才私聊说的那个我记得。"):
        res = _result(text)
        ev = Ev()
        assert p._care_priv02(ev, res) is False
        assert res.chain[0].text == text and not ev.stopped


def test_priv02_all_removed_stops_send():
    p = _plugin()
    res = _result("刚才私聊说的那个我记得。")
    ev = Ev()
    assert p._care_priv02(ev, res) is True
    assert ev.stopped and res.chain == []


def test_priv02_skips_private_off_and_non_llm():
    text = "刚才私聊说的那个我记得。"
    for plugin, ev, res in (
        (_plugin(), Ev(private=True), _result(text)),
        (_plugin("off"), Ev(), _result(text)),
        (_plugin(), Ev(), _result(text, llm=False)),
    ):
        assert plugin._care_priv02(ev, res) is False
        assert res.chain[0].text == text


async def _collect(gen):
    return [x async for x in gen]


def test_iris_care_only_owner_private():
    p = object.__new__(IrisMemoryPlugin)
    handled = []

    async def fake_handle(care, coord, scope, message, clock=None):
        handled.append((scope, message))
        return "ok"

    svc = SimpleNamespace(
        owner_scope=lambda sid: f"private:{sid}" if sid == OWNER else None, is_open=True, care=object(),
        initiative=object(),
    )
    p._continuity = svc
    import astrbot_plugin_iris_memory.main as main_mod

    orig = main_mod.care_commands.handle
    main_mod.care_commands.handle = fake_handle
    try:
        group = Ev(private=False, text="/iris_care status")
        assert asyncio.run(_collect(p.iris_care_cmd(group))) == []
        assert group.stopped
        stranger = Ev(private=True, sender="555", text="/iris_care status")
        assert asyncio.run(_collect(p.iris_care_cmd(stranger))) == []
        owner = Ev(private=True, text="/iris_care status")
        assert asyncio.run(_collect(p.iris_care_cmd(owner))) == ["ok"]
        assert handled == [(f"private:{OWNER}", "/iris_care status")]
    finally:
        main_mod.care_commands.handle = orig


class GroupEv(Ev):
    def __init__(self, group="-1001", **kw):
        super().__init__(private=False, **kw)
        self.group = group

    def get_group_id(self):
        return self.group


def test_iris_member_and_life_stop_event_in_group():
    """审查 P5：群里发 /iris_member、/iris_life 不响应，并且停止事件（不交给 LLM）"""
    p = object.__new__(IrisMemoryPlugin)
    p._continuity = SimpleNamespace(
        owner_ids=lambda: {OWNER}, owner_scope=lambda sid: f"private:{sid}", is_open=True, world=object(),
    )
    p._session_key_quiet = lambda ev: "group:-1001"
    ev = GroupEv(text="/iris_member show 某人")
    assert asyncio.run(_collect(p.iris_member(ev))) == []
    assert ev.stopped
    ev2 = GroupEv(text="/iris_life today")
    assert asyncio.run(_collect(p.iris_life(ev2))) == []
    assert ev2.stopped
