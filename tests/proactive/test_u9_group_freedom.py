"""活人感 U9：白名单群自由搭话（lifelike_group_freedom）。

开关打开：白名单群任何人的发言去抖后交给她决定、群内可自己开话题；取消冷却闸门 / 采样 / 频率 /
仅点名 / 质量分 / 静音；保留去重、硬底线、连续无人说话熔断、叫停、MAN、PRIV、频道不发。
开关关闭：所有入口退回原有行为。
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_iris_memory.main import IrisMemoryPlugin
from astrbot_plugin_iris_memory.iris_memory.proactive import group_freedom as gfm
from astrbot_plugin_iris_memory.iris_memory.proactive import proactive as proactive_mod
from astrbot_plugin_iris_memory.iris_memory.proactive.decision import DecisionCore, DecisionRequest
from astrbot_plugin_iris_memory.iris_memory.proactive.parser import parse_decision
from astrbot_plugin_iris_memory.iris_memory.proactive.perception import (
    ContextPackager,
    SlidingWindow,
    WindowMessage,
)
from astrbot_plugin_iris_memory.iris_memory.proactive.proactive import ProactiveEngine
from astrbot_plugin_iris_memory.iris_memory.proactive.signals import SignalGate
from astrbot_plugin_iris_memory.iris_memory.proactive.state import StateManager
from astrbot_plugin_iris_memory.iris_memory.proactive.tickets import DecisionTicketRegistry

OWNER = "100000001"
GID = "-100555"
OTHER = "-100666"
ON = {"lifelike_group_freedom": True}


# ===================================================================== 配置 / 纯函数


class TestConfig:
    def test_defaults_off(self, make_config):
        cfg = make_config().group_freedom_config
        assert cfg == gfm.GroupFreedomConfig()
        assert cfg.enabled is False and cfg.debounce_seconds == 45 and cfg.floor_seconds == 120
        assert cfg.initiate_quiet_minutes == 45 and cfg.initiate_eval_minutes == 45
        assert cfg.breaker_consecutive == 2 and cfg.chat_drive == "off"

    def test_clamps(self, make_config):
        cfg = make_config(hidden={
            **ON, "lifelike_gf_debounce_seconds": 1, "lifelike_gf_floor_seconds": 5,
            "lifelike_gf_breaker_consecutive": 99, "lifelike_gf_initiate_eval_minutes": 1,
        }).group_freedom_config
        assert cfg.enabled and cfg.debounce_seconds == 10 and cfg.floor_seconds == 60
        assert cfg.breaker_consecutive == 10 and cfg.initiate_eval_minutes == 15

    def test_bad_value_falls_back_to_default(self, make_config):
        cfg = make_config(hidden={**ON, "lifelike_gf_floor_seconds": "x"}).group_freedom_config
        assert cfg.enabled is True and cfg.floor_seconds == 120

    def test_registered_and_master_gated(self):
        from astrbot_plugin_iris_memory.iris_memory.config.defaults import Defaults
        from astrbot_plugin_iris_memory.iris_memory.lifelike import master

        d = Defaults()
        assert d.get_by_flat_key("lifelike_group_freedom") is False
        assert d.get_by_flat_key("lifelike_gf_debounce_seconds") == 45
        assert master.gate("lifelike_group_freedom", True, lambda: False) is False


class TestHush:
    @pytest.mark.parametrize("text", ["闭嘴", "别说话了", "你别插嘴", "少说两句", "安静点", "别刷屏了", "别吵"])
    def test_hush(self, text):
        assert gfm.is_group_hush(text) is True

    @pytest.mark.parametrize("text", ["今天好安静", "闭嘴哈哈哈", "他说话好好笑", "我刚说了", "", "/help"])
    def test_not_hush(self, text):
        assert gfm.is_group_hush(text) is False


class TestTrackerAndBlock:
    def test_floor_breaker_reset(self):
        cfg = gfm.GroupFreedomConfig(enabled=True)
        tr = gfm.GroupFreedomTracker()
        now = 10_000.0
        tr.note_self(GID, now - 30, unsolicited=True)
        assert 0 < tr.floor_left(GID, cfg, now) <= 90
        assert gfm.block_reason(cfg, tr, GID, now=now, hush_until=0, has_context=True) == gfm.R_FLOOR
        tr.note_self(GID, now - 200, unsolicited=False)  # 被点名的回复不算主动
        later = now + 200
        assert gfm.block_reason(cfg, tr, GID, now=later, hush_until=0, has_context=True) == ""
        tr.note_self(GID, later, unsolicited=True)
        assert tr.breaker_hit(GID, cfg)  # 连续 2 条主动、中间没人说话
        assert gfm.block_reason(cfg, tr, GID, now=later + 999, hush_until=0, has_context=True) == gfm.R_BREAKER
        tr.note_human(GID, later + 1000)
        assert not tr.breaker_hit(GID, cfg)

    def test_hush_and_quiet(self):
        cfg = gfm.GroupFreedomConfig(enabled=True)
        tr = gfm.GroupFreedomTracker()
        now = 10_000.0
        assert gfm.block_reason(cfg, tr, GID, now=now, hush_until=now + 1, has_context=True) == gfm.R_HUSH
        assert gfm.block_reason(cfg, tr, GID, now=now, hush_until=0, has_context=False) == gfm.R_NO_CONTEXT
        assert gfm.block_reason(
            cfg, tr, GID, now=now, hush_until=0, has_context=True, last_human_at=now - 60, check_quiet=True
        ) == gfm.R_GROUP_ACTIVE

    def test_facts_are_facts_only(self):
        now = time.time()
        msgs = [WindowMessage("u1", "小林", "a", now - 120), WindowMessage(OWNER, "aming", "b", now - 60),
                WindowMessage("bot", "我", "c", now - 30)]
        text = gfm.build_facts(now=now, wake="burst", messages=msgs, self_id="bot", owner_ids=[OWNER],
                               last_self_at=now - 30, humans_since_self=0)
        assert text.startswith("<facts>") and "小林(u1)" in text and "你很熟的人" in text
        assert "之后还没有人说话" in text and "停下来" in text
        for word in ("好感", "亲密", "score", "%", "分值"):
            assert word not in text


# ===================================================================== 决策 prompt / 解析


def _core(nm_config, hidden=None):
    config = nm_config(hidden=hidden)
    state = StateManager(config)
    window = SlidingWindow(config)
    return DecisionCore(config, state, window, ContextPackager(config)), state, window


class TestDecisionPrompt:
    def test_group_free_chime_uses_free_system(self, nm_config):
        core, _, window = _core(nm_config)
        window.append(GID, WindowMessage("u1", "A", "有人看了昨晚的比赛吗", time.time()))
        req = DecisionRequest(group_id=GID, wake="message", motive="chime_in", trigger="free",
                              group_free=True, freedom_facts="<facts>\n- 现在：x\n</facts>", self_pause_cap=5)
        user, system = core.build_prompt(req)
        assert "没有次数或间隔的规定" in system and "<facts>" in user and "最多 5" in user
        assert "不要打断这种对话流" not in system  # 原群聊"不要打断"原则不再适用

    def test_not_free_is_unchanged(self, nm_config):
        core, _, _ = _core(nm_config)
        a = core.build_prompt(DecisionRequest(group_id=GID, wake="message", motive="chime_in", trigger="owner",
                                              trigger_user=OWNER))
        b = core.build_prompt(DecisionRequest(group_id=GID, wake="message", motive="chime_in", trigger="owner",
                                              trigger_user=OWNER, freedom_facts="<facts>x</facts>"))
        assert a == b and "<facts>" not in a[0]

    def test_group_initiate_and_hostile(self, nm_config):
        core, _, _ = _core(nm_config)
        u, s = core.build_prompt(DecisionRequest(group_id=GID, wake="timer", motive="initiate", quiet_minutes=50,
                                                 group_free=True, freedom_facts="<facts>t</facts>"))
        assert "安静了 50 分钟" in u and "<facts>t</facts>" in u
        u2, _ = core.build_prompt(DecisionRequest(group_id=GID, wake="message", motive="chime_in", trigger="hostile",
                                                  trigger_user="u9", group_free=True, freedom_facts="<facts>t</facts>"))
        assert "玩笑" in u2 and "u9" in u2


class TestParserSpeakWins:
    RAW_DRIFT = '{"action":"speak","message":"","drifted":true,"cooldown":0}'
    RAW_CD = '{"action":"speak","message":"","cooldown":5}'

    def test_default_unchanged(self):
        d = parse_decision(self.RAW_DRIFT, mode="chime_in")
        assert d.should_speak is False and d.drifted is True  # 现网曾出现的静默丢弃路径
        assert parse_decision(self.RAW_CD, mode="chime_in").cooldown_minutes == 5

    def test_speak_wins(self):
        d = parse_decision(self.RAW_DRIFT, mode="chime_in", speak_wins=True)
        assert d.should_speak is True and d.drifted is True
        d2 = parse_decision(self.RAW_CD, mode="chime_in", speak_wins=True)
        assert d2.should_speak and d2.cooldown_minutes == 0
        d3 = parse_decision('{"action":"speak","topic":"聊聊天气"}', mode="initiate", speak_wins=True)
        assert d3.should_speak and d3.topic_source == "recent_context"
        assert parse_decision('{"action":"speak","topic":"聊聊天气"}', mode="initiate").should_speak is False
        # 审查 M8：没有切入角度时用 obs 补，仍为空就不说
        d4 = parse_decision('{"action":"speak","obs":"大家在聊周末去哪"}', mode="initiate", speak_wins=True)
        assert d4.should_speak and d4.topic == "大家在聊周末去哪"
        assert parse_decision('{"action":"speak"}', mode="initiate", speak_wins=True).should_speak is False


# ===================================================================== 引擎（群内主动）


class FakeBridge:
    def __init__(self):
        self.begin_calls = []

    async def candidates(self, scope, now=None):
        return []

    def is_owner_private_scope(self, scope):
        return False

    async def begin_turn(self, scope, forced=False, **kw):
        self.begin_calls.append((scope, forced, kw))
        return SimpleNamespace(turn=None, reason="test_stop", candidates=[])


def _engine(nm_config, *, hidden=None, eligible=(GID,), hub=True):
    config = nm_config(
        cfg={"proactive": {"enabled": True, "proactive_enabled": True, "provider_id": "p"}},
        hidden=hidden,
    )
    state = StateManager(config)
    window = SlidingWindow(config)
    packager = ContextPackager(config)
    core = DecisionCore(config, state, window, packager)
    context = SimpleNamespace(
        persona_manager=SimpleNamespace(get_default_persona_v3=AsyncMock(return_value={})),
        send_message=AsyncMock(return_value=True),
        get_config=lambda _umo=None: {},
    )
    gf = gfm.GroupFreedomHub(lambda: config.group_freedom_config, lambda g: g in eligible, lambda: [OWNER]) if hub else None
    engine = ProactiveEngine(
        context, config, state, window, SignalGate(config, state), core, Mock(),
        llm_manager=SimpleNamespace(generate_direct=AsyncMock()),
        packager=packager, umo_get=lambda _gid: "umo", is_busy=lambda _gid: False,
        self_id_get=lambda: "bot", save_fn=AsyncMock(), bridge=FakeBridge(), group_freedom=gf,
    )
    return engine, state, window, gf


def _prime(state, window, gf, ago=3 * 3600):
    ts = time.time() - ago
    window.append(GID, WindowMessage("u1", "A", "x", ts))
    state.record_human_message(GID, ts)
    if gf is not None:
        gf.tracker.note_human(GID, ts)


class TestEngine:
    def test_off_identical(self, nm_config, monkeypatch):
        engine, state, _, _ = _engine(nm_config)
        assert engine._gf_on(GID) is False
        assert engine._scan_ids() == state.get_whitelist()
        monkeypatch.setattr(proactive_mod.random, "uniform", lambda a, b: a)
        now = time.time()
        assert engine._schedule_group(GID, now=now) == pytest.approx(now + 300 * 0.6)

    def test_no_hub_identical(self, nm_config):
        engine, _, _, _ = _engine(nm_config, hidden=ON, hub=False)
        assert engine._gf_on(GID) is False

    def test_scope(self, nm_config):
        engine, state, window, gf = _engine(nm_config, hidden=ON)
        assert engine._gf_on(GID) and not engine._gf_on(OTHER) and not engine._gf_on(f"private:{OWNER}")
        _prime(state, window, gf)
        assert GID in engine._scan_ids()  # 不在 Iris 白名单也会被调度
        assert OTHER not in engine._scan_ids()

    def test_cadence_and_drive(self, nm_config, monkeypatch):
        engine, _, _, _ = _engine(nm_config, hidden=ON)
        monkeypatch.setattr(proactive_mod.random, "uniform", lambda a, b: a)
        now = time.time()
        assert engine._schedule_group(GID, now=now, after_activity=True) == pytest.approx(now + 45 * 60)
        assert engine._schedule_group(GID, now=now) == pytest.approx(now + 45 * 60 * 0.8)
        engine2, _, _, _ = _engine(nm_config, hidden={**ON, "lifelike_chat_drive": "medium"})
        assert engine2._schedule_group(GID, now=now, after_activity=True) == pytest.approx(now + 30 * 60)
        engine3, _, _, _ = _engine(nm_config, hidden={**ON, "lifelike_chat_drive": "high"})
        assert engine3._schedule_group(GID, now=now, after_activity=True) == pytest.approx(now + 18 * 60)

    @pytest.mark.asyncio
    async def test_scan_attempts_without_group_initiate_enabled(self, nm_config):
        engine, state, window, gf = _engine(nm_config, hidden=ON)
        _prime(state, window, gf)
        engine.attempt_initiate = AsyncMock(return_value="ok")
        engine._signals.evaluate_timer = Mock(return_value=None)
        state.set_next_initiate_check(GID, time.time() - 1)
        await engine._scan_due()
        engine.attempt_initiate.assert_awaited_once_with(GID)
        engine._signals.evaluate_timer.assert_not_called()

    @pytest.mark.asyncio
    async def test_scan_off_does_not_attempt(self, nm_config):
        engine, state, window, gf = _engine(nm_config)
        state.add_to_whitelist(GID)
        _prime(state, window, gf)
        engine.attempt_initiate = AsyncMock(return_value="ok")
        state.set_next_initiate_check(GID, time.time() - 1)
        await engine._scan_due()
        engine.attempt_initiate.assert_not_awaited()  # group_initiate_enabled=false 照旧

    @pytest.mark.parametrize("setup,reason", [
        (lambda st, gf: st.set_proactive_hush(GID, time.time() + 3600), gfm.R_HUSH),
        (lambda st, gf: gf.tracker.note_self(GID, time.time() - 30, unsolicited=True), gfm.R_FLOOR),
        (lambda st, gf: gf.tracker.note_human(GID, time.time() - 60), gfm.R_GROUP_ACTIVE),
    ])
    @pytest.mark.asyncio
    async def test_safety_net(self, nm_config, setup, reason, monkeypatch):
        engine, state, window, gf = _engine(nm_config, hidden=ON)
        _prime(state, window, gf)
        setup(state, gf)
        log = Mock()
        monkeypatch.setattr(proactive_mod, "logger", log)
        engine.attempt_initiate = AsyncMock(return_value="ok")
        state.set_next_initiate_check(GID, time.time() - 1)
        await engine._scan_due()
        engine.attempt_initiate.assert_not_awaited()
        assert any(c.args[1:] == (GID, reason) for c in log.info.call_args_list)

    @pytest.mark.asyncio
    async def test_breaker_error_once_until_human(self, nm_config, monkeypatch):
        engine, state, window, gf = _engine(nm_config, hidden=ON)
        _prime(state, window, gf)
        gf.tracker.note_self(GID, time.time() - 3 * 3600, unsolicited=True)
        gf.tracker.note_self(GID, time.time() - 2 * 3600, unsolicited=True)
        log = Mock()
        monkeypatch.setattr(proactive_mod, "logger", log)
        engine.attempt_initiate = AsyncMock(return_value="ok")
        for _ in range(3):
            state.set_next_initiate_check(GID, time.time() - 1)
            await engine._scan_due()
        engine.attempt_initiate.assert_not_awaited()
        assert len(log.error.call_args_list) == 1 and "op=breaker" in log.error.call_args_list[0].args[0]

    @pytest.mark.asyncio
    async def test_attempt_builds_group_free_request(self, nm_config):
        engine, state, window, gf = _engine(nm_config, hidden={**ON, "lifelike_chat_drive": "medium"})
        _prime(state, window, gf)
        seen = {}

        async def decide(req, *_a):
            seen["req"] = req
            return SimpleNamespace(error="x", decision=None, error_kind="", dynamic_context_sources=[])

        engine._core.decide = decide
        engine._bridge.begin_turn = AsyncMock(return_value=SimpleNamespace(turn={"turn_id": "t"}, reason="", candidates=[]))
        engine._bridge.close_turn = AsyncMock()
        await engine.attempt_initiate(GID)
        req = seen["req"]
        assert req.group_free and req.chat_drive == "medium" and req.speak_wins and "<facts>" in req.freedom_facts
        assert req.self_pause_cap == 5

    def test_guard(self, nm_config):
        engine, state, window, gf = _engine(nm_config, hidden=ON)
        _prime(state, window, gf)
        state.set_cooldown(GID, 30)
        assert engine._initiate_guard_reason(GID, time.time(), require_quiet=True) == ""
        gf.tracker.note_self(GID, time.time() - 10, unsolicited=True)
        assert "floor_gap" in engine._initiate_guard_reason(GID, time.time(), require_quiet=True)

    def test_priv02_strip(self):
        out = proactive_mod._strip_private_for_group(GID, "你之前私聊里跟我说过那件事。今天天气不错")
        assert "私聊" not in out


# ===================================================================== 主插件（群消息入口）


class Reply:
    def __init__(self, sender_id):
        self.sender_id = sender_id


class _Ev:
    def __init__(self, text, sender="u1", name="小林", group=GID, at=False, msg_id=None, reply_to=None, post=None):
        self.message_str = text
        self._reply_to = reply_to
        self.is_at_or_wake_command = at
        self.is_wake = at
        self._extra = {}
        self.stopped = False
        self._sender, self._name, self._group = sender, name, group
        self.unified_msg_origin = f"tg:GroupMessage:{group}"
        self.message_obj = SimpleNamespace(
            message_id=msg_id or id(self), raw_message=(SimpleNamespace(post=post, out=False) if post else None)
        )

    def get_extra(self, k, default=None):
        return self._extra.get(k, default)

    def set_extra(self, k, v):
        self._extra[k] = v

    def is_private_chat(self):
        return not self._group

    def get_group_id(self):
        return self._group

    def get_sender_id(self):
        return self._sender

    def get_sender_name(self):
        return self._name

    def get_self_id(self):
        return "bot"

    def get_messages(self):
        return [Reply(self._reply_to)] if self._reply_to else []

    def stop_event(self):
        self.stopped = True


def _plugin(nm_config, hidden=None, eligible=(GID,)):
    from astrbot_plugin_iris_memory.iris_memory.proactive import hostility

    p = object.__new__(IrisMemoryPlugin)
    cfg = nm_config(cfg={"proactive": {"enabled": True, "provider_id": "p", "private_whitelist": [OWNER]}},
                    hidden=hidden)
    p._reply_config = cfg
    p._state = StateManager(cfg)
    p._sliding_window = SlidingWindow(cfg)
    p._signals = SignalGate(cfg, p._state)
    p._decision_tickets = DecisionTicketRegistry(120)
    p._triggering, p._reply_in_progress, p._passive_active = {}, {}, {}
    p._group_umo, p._umo_dirty, p._self_id = {}, False, "bot"
    p._tool_ctx = Mock()
    p._proactive = SimpleNamespace(notify_human_message=Mock(), is_initiating=lambda g: False)
    p._kv_save = AsyncMock()
    p.config = {}
    p.component_manager = None
    p._counter_tracker = hostility.CounterTracker()
    p._gf = gfm.GroupFreedomHub(lambda: cfg.group_freedom_config, lambda g: g in eligible, lambda: [OWNER])
    p._gatekeeper = SimpleNamespace(should_process=Mock(return_value=False), quality_score=lambda t: 1.0)
    p._continuity = None
    p._continuity_on_owner_message = AsyncMock()
    p._prepare_pure_at_request = AsyncMock(return_value=False)
    return p


FAST = {**ON, "lifelike_gf_debounce_seconds": 10}


def _speak_stub(p, speak=True):
    """把决策替换成固定结果：speak=True → 返回 False（决定开口）；False → 记一次不开口并 stop。"""
    calls = []

    async def fake(event):
        calls.append(event.get_extra("iris_decision"))
        p._release_decision_ticket(event.get_group_id())
        if speak:
            return False
        event.stop_event()
        return True

    p._handle_reply_decision = fake
    return calls


class TestMainEntry:
    @pytest.mark.asyncio
    async def test_off_uses_legacy_gate(self, nm_config):
        p = _plugin(nm_config)
        ev = _Ev("大家晚上好")
        await p.on_message(ev)
        p._gatekeeper.should_process.assert_called_once()
        assert ev.get_extra("iris_gf_wait") is None

    @pytest.mark.asyncio
    async def test_any_member_enters_debounce(self, nm_config):
        p = _plugin(nm_config, hidden=ON)
        ev = _Ev("有人玩过这个游戏吗")
        await p.on_message(ev)
        p._gatekeeper.should_process.assert_not_called()  # 静音 / 质量分 / Iris 白名单门控不再适用
        group, seq, sender, mode = ev.get_extra("iris_gf_wait")
        assert group == GID and sender == "u1" and seq == 1 and mode == "free"
        assert len(p._sliding_window.get_messages(GID)) == 1
        assert GID not in p._triggering  # 等去抖结束才认领

    @pytest.mark.asyncio
    async def test_ineligible_group_falls_back(self, nm_config):
        p = _plugin(nm_config, hidden=ON)
        ev = _Ev("hi", group=OTHER)
        await p.on_message(ev)
        p._gatekeeper.should_process.assert_called_once()

    @pytest.mark.asyncio
    async def test_broadcast_post_not_eligible(self, nm_config):
        p = _plugin(nm_config, hidden=ON)
        ev = _Ev("频道新帖", post=True)
        assert p._gf_event_group(ev) == ""

    @pytest.mark.asyncio
    async def test_name_decides_without_debounce(self, nm_config, monkeypatch):
        p = _plugin(nm_config, hidden=ON)
        calls = _speak_stub(p)
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        ev = _Ev("小白你怎么看")
        await p.on_message(ev)
        assert ev.get_extra("iris_gf_wait")[3] == "name"
        await p.on_group_freedom_wait(ev)
        asyncio.sleep.assert_not_awaited()
        assert calls[0]["trigger"] == "name" and not calls[0]["gf_unsolicited"]
        assert ev.is_at_or_wake_command is True and ev.get_extra("iris_gf_predecided") is True

    @pytest.mark.asyncio
    async def test_declined_decision_does_not_wake(self, nm_config, monkeypatch):
        """审查 M3：决策在等待阶段直接做，不开口就不唤醒主管线（不会"正在输入"却不说话）"""
        p = _plugin(nm_config, hidden=FAST)
        _speak_stub(p, speak=False)
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        ev = _Ev("今天好热")
        await p.on_message(ev)
        await p.on_group_freedom_wait(ev)
        assert ev.is_at_or_wake_command is False and ev.stopped is True
        assert GID not in p._triggering

    @pytest.mark.asyncio
    async def test_at_is_passive_and_supersedes_wait(self, nm_config, monkeypatch):
        p = _plugin(nm_config, hidden=FAST)
        calls = _speak_stub(p)
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        e1 = _Ev("先聊点别的")
        await p.on_message(e1)
        ev = _Ev("帮我看看", at=True)
        await p.on_message(ev)
        assert ev.get_extra("iris_mode") == "passive" and ev.get_extra("iris_gf_wait") is None
        await p.on_group_freedom_wait(e1)
        assert calls == []  # 审查 M2：@ 之后旧窗口不再决策一次

    @pytest.mark.asyncio
    async def test_name_supersedes_pending_burst(self, nm_config, monkeypatch):
        p = _plugin(nm_config, hidden=FAST)
        calls = _speak_stub(p)
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        e1 = _Ev("随便说说")
        await p.on_message(e1)
        e2 = _Ev("xiaobai 你觉得呢", sender="u2")
        await p.on_message(e2)
        await p.on_group_freedom_wait(e2)
        await p.on_group_freedom_wait(e1)
        assert [c["trigger"] for c in calls] == ["name"]

    @pytest.mark.asyncio
    async def test_debounce_last_message_wins(self, nm_config, monkeypatch):
        p = _plugin(nm_config, hidden=FAST)
        calls = _speak_stub(p)
        sleeps = []

        async def fake_sleep(s):
            sleeps.append(s)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        e1, e2 = _Ev("第一条"), _Ev("第二条", sender="u2", name="阿杰")
        await p.on_message(e1)
        await p.on_message(e2)
        await p.on_group_freedom_wait(e1)
        assert calls == []  # 被更新的消息接替
        await p.on_group_freedom_wait(e2)
        assert calls[0]["trigger"] == "free" and calls[0]["gf_unsolicited"] is True
        assert sleeps[0] == 10

    @pytest.mark.asyncio
    async def test_floor_waits_not_drops(self, nm_config, monkeypatch):
        p = _plugin(nm_config, hidden=FAST)
        calls = _speak_stub(p)
        sleeps = []

        async def fake_sleep(s):
            sleeps.append(s)
            p._gf.tracker._g(GID).last_unsolicited_at -= s  # 时间向前推

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        p._gf.tracker.note_self(GID, time.time(), unsolicited=True)
        ev = _Ev("那后来呢")
        await p.on_message(ev)
        await p.on_group_freedom_wait(ev)
        assert len(sleeps) >= 2  # 去抖 + 等到硬底线结束
        assert calls[0]["trigger"] == "free"

    @pytest.mark.asyncio
    async def test_media_tail_skips_media_gate(self, nm_config):
        """审查 M1：一阵消息以贴纸结尾时不走媒体门控，交给去抖决策"""
        p = _plugin(nm_config, hidden=ON)
        p._is_media_only_message = lambda e: True
        p._should_reply_to_media = AsyncMock(return_value=False)
        ev = _Ev("[贴纸：猫猫点头]")
        await p.on_message(ev)
        await p.on_all_message(ev)
        p._should_reply_to_media.assert_not_called()
        assert ev.stopped is False and ev.get_extra("iris_gf_wait")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("sender,text,hours", [(OWNER, "安静点", 6.0), ("u3", "小白你安静点", 2.0)])
    async def test_hush(self, nm_config, monkeypatch, sender, text, hours):
        p = _plugin(nm_config, hidden=ON)
        calls = _speak_stub(p)
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        await p.on_message(_Ev(text, sender=sender))
        left = p._state.get_proactive_hush(GID) - time.time()
        assert hours * 3600 - 10 < left <= hours * 3600
        ev = _Ev("今天吃什么")
        await p.on_message(ev)
        assert ev.get_extra("iris_gf_wait") is None  # 叫停期间不评估闲聊
        ev2 = _Ev("xiaobai 你在吗")
        await p.on_message(ev2)
        await p.on_group_freedom_wait(ev2)
        assert calls[-1]["trigger"] == "name"  # 点名照样回应

    @pytest.mark.asyncio
    async def test_member_hush_needs_addressing(self, nm_config):
        """审查 L3：群友的"安静点"只有 @ / 回复她 / 叫名字时才算"""
        p = _plugin(nm_config, hidden=ON)
        p._gf.tracker.note_self(GID, time.time() - 30, unsolicited=True)
        await p.on_message(_Ev("你安静点", sender="u3"))
        assert p._state.get_proactive_hush(GID) == 0.0
        ev = _Ev("你安静点", sender="u3", at=True)  # @ 或回复她（适配器把两者都标为唤醒）
        await p.on_message(ev)
        assert p._state.get_proactive_hush(GID) > time.time()

    @pytest.mark.asyncio
    async def test_owner_unhush(self, nm_config):
        p = _plugin(nm_config, hidden=ON)
        p._state.set_proactive_hush(GID, time.time() + 3600)
        await p.on_message(_Ev("可以说话了", sender=OWNER))
        assert p._state.get_proactive_hush(GID) == 0.0

    def test_self_pause_capped(self, nm_config):
        p = _plugin(nm_config, hidden=ON)
        p._state.set_cooldown(GID, 3)
        assert p._gf_message_block(GID, time.time()) == gfm.R_SELF_PAUSE


class _Res:
    def __init__(self, text):
        from astrbot.api.message_components import Plain

        self.chain = [Plain(text)]

    def is_llm_result(self):
        return True


class TestReplyNets:
    def test_dedupe_blocks_unsolicited(self, nm_config):
        from astrbot_plugin_iris_memory.iris_memory.proactive import freedom as fr

        p = _plugin(nm_config, hidden=ON)
        p._state.note_freedom_sent(GID, ts=time.time() - 60, topic="", norm_text=fr.normalize_text("这个我也想看"))
        ev = _Ev("x")
        ev.set_extra("iris_gf_unsolicited", True)
        res = _Res("这个我也想看！")
        assert p._gf_reply_blocked(ev, res) is True and res.chain == [] and ev.stopped

    def test_nag_blocked_and_addressed_not_checked(self, nm_config):
        p = _plugin(nm_config, hidden=ON)
        ev = _Ev("x")
        ev.set_extra("iris_gf_unsolicited", True)
        assert p._gf_reply_blocked(ev, _Res("怎么都不理我")) is True
        ev2 = _Ev("x")
        assert p._gf_reply_blocked(ev2, _Res("怎么都不理我")) is False


class TestNeutralPrompt:
    @pytest.mark.asyncio
    async def test_unsolicited_prompt_is_neutral(self, nm_config):
        """审查 L1：群友闲聊触发的回复，正文换成中性说明（仍带最后一条）"""
        p = _plugin(nm_config, hidden=ON)
        p._bypass_stripper = None
        p._bypass_missing_warned = True
        p._apply_error_friendly_extra = Mock()
        p._llm_manager = None
        p._inject_continuity = AsyncMock()
        p._inject_sticker_prompt = AsyncMock()
        ev = _Ev("那我也去", sender="u2", name="阿杰")
        ev.set_extra("iris_gf_predecided", True)
        ev.set_extra("iris_gf_unsolicited", True)
        req = SimpleNamespace(extra_user_content_parts=[], prompt="那我也去")
        await p.on_llm_request(ev, req)
        assert "接谁的话由你决定" in req.prompt and "阿杰" in req.prompt and "那我也去" in req.prompt
