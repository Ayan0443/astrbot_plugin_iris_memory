"""活人感 U8：主人私聊自由主动（lifelike_proactive_freedom）。

开关打开：频率 / 时段 / 未回复类限制对主人私聊失效，按节奏评估、由 LLM 决定；
保留技术安全网（硬底线、24h 熔断、主人叫停、她自己的 cooldown、主人刚说完话的短静默）。
开关关闭 / 群聊：行为不变。
"""

import calendar
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_iris_memory.iris_memory.lifelike.initiative import gate as g
from astrbot_plugin_iris_memory.iris_memory.proactive import freedom as fr
from astrbot_plugin_iris_memory.iris_memory.proactive import proactive as proactive_mod
from astrbot_plugin_iris_memory.iris_memory.proactive.activity_gate import ActivitySnapshot
from astrbot_plugin_iris_memory.iris_memory.proactive.decision import DecisionCore, DecisionRequest
from astrbot_plugin_iris_memory.iris_memory.proactive.perception import (
    ContextPackager,
    SlidingWindow,
    WindowMessage,
)
from astrbot_plugin_iris_memory.iris_memory.proactive.proactive import ProactiveEngine
from astrbot_plugin_iris_memory.iris_memory.proactive.signals import SignalGate
from astrbot_plugin_iris_memory.iris_memory.proactive.state import GroupState, StateManager

OWNER = "123456789"
SCOPE = f"private:{OWNER}"
GROUP = "-100777"
ON = {"lifelike_proactive_freedom": True}


# ===================================================================== 纯函数


class TestHushPhrases:
    @pytest.mark.parametrize("text", [
        "别发了", "别发了！", "别再发消息了", "不要主动给我发消息", "安静点", "安静一点好吗",
        "闭嘴", "别烦我", "别来烦我了", "先别找我", "让我静静", "今天别主动找我了", "勿扰",
        "好了好了，别发了", "你能不能安静点", "少发点消息", "不用给我发了",
    ])
    def test_hush(self, text):
        assert fr.is_hush(text) is True

    @pytest.mark.parametrize("text", [
        "", "/proactive", "别发表情了", "好安静啊", "为什么不发了？", "今天群里很安静", "我在发消息呢",
        "别发那么长的", "这首歌让我安静下来了，真好听真好听真好听",
    ])
    def test_not_hush(self, text):
        assert fr.is_hush(text) is False

    @pytest.mark.parametrize("text", ["可以找我了", "想发就发吧", "你可以说话了", "随时找我"])
    def test_unhush(self, text):
        assert fr.is_unhush(text) is True
        assert fr.is_hush(text) is False


class TestBlockReason:
    def _kw(self, **over):
        now = 1_000_000.0
        kw = dict(
            now=now, cooldown_until=0.0, hush_until=0.0, last_initiate_at=0.0,
            initiate_timestamps=[], last_owner_at=now - 3600, has_context=True,
        )
        kw.update(over)
        return kw

    def test_all_clear(self):
        assert fr.block_reason(fr.FreedomConfig(enabled=True), **self._kw()) == ""

    def test_reasons(self):
        cfg = fr.FreedomConfig(enabled=True)
        now = 1_000_000.0
        assert fr.block_reason(cfg, **self._kw(cooldown_until=now + 1)) == fr.R_SELF_COOLDOWN
        assert fr.block_reason(cfg, **self._kw(cooldown_until=now + 1), check_cooldown=False) == ""
        assert fr.block_reason(cfg, **self._kw(hush_until=now + 1)) == fr.R_HUSH
        assert fr.block_reason(cfg, **self._kw(last_initiate_at=now - 599)) == fr.R_FLOOR_GAP
        assert fr.block_reason(cfg, **self._kw(last_initiate_at=now - 601)) == ""
        many = [now - 3600 * i for i in range(1, 21)]
        silent = now - 23.5 * 3600  # 主人 23.5 小时没说话
        assert fr.block_reason(cfg, **self._kw(initiate_timestamps=many, last_owner_at=silent)) == fr.R_BREAKER
        assert fr.block_reason(cfg, **self._kw(initiate_timestamps=many[:19], last_owner_at=silent)) == ""
        assert fr.block_reason(cfg, **self._kw(has_context=False)) == fr.R_NO_CONTEXT
        assert fr.block_reason(cfg, **self._kw(last_owner_at=now - 300)) == fr.R_OWNER_RECENT

    def test_no_frequency_limits(self):
        """条数（<熔断）、间隔（>硬底线）、未回复都不在安全网里"""
        cfg = fr.FreedomConfig(enabled=True)
        now = 1_000_000.0
        ts = [now - 700 * i for i in range(1, 12)]  # 11 条，最近一条 700 秒前
        assert fr.block_reason(cfg, **self._kw(initiate_timestamps=ts, last_initiate_at=ts[0])) == ""


class TestFacts:
    def test_facts_are_time_only(self):
        lt = time.gmtime
        now = float(calendar.timegm((2026, 11, 3, 14, 5, 0, 0, 0, 0)))
        text = fr.build_facts(
            now=now, last_owner_at=now - 3 * 3600 - 20 * 60, last_initiate_at=now - 5 * 3600,
            owner_replied_after_initiate=True, unanswered_streak=0, initiated_24h=2,
            closure_ended_at=now - 3 * 3600, busy_until=now + 3600, localtime=lt,
        )
        assert text.startswith("<facts>") and text.endswith("</facts>")
        assert "11月3日 周二 14:05" in text
        assert "约 3 小时 20 分钟前" in text and "约 5 小时前" in text and "之后对方回过你" in text
        assert "过去 24 小时你主动找过对方 2 次" in text
        assert "收尾" in text and "15:05" in text
        for word in ("好感", "亲密", "分", "%", "score"):
            assert word not in text.replace("分钟", "")

    def test_unanswered_streak_and_no_history(self):
        now = 1_000_000.0
        t = fr.build_facts(now=now, last_owner_at=None, last_initiate_at=now - 7200,
                           owner_replied_after_initiate=False, unanswered_streak=3, initiated_24h=3)
        assert "对方之后还没有回" in t and "接连几次" in t and "连续 3 次" not in t and "不清楚" in t
        t2 = fr.build_facts(now=now, last_owner_at=now - 60, last_initiate_at=0.0,
                            owner_replied_after_initiate=False, unanswered_streak=0, initiated_24h=0)
        assert "你最近没有主动找过对方" in t2 and "刚刚" in t2

    @pytest.mark.parametrize("sec,expect", [
        (30, "刚刚"), (25 * 60, "约 25 分钟前"), (2 * 3600 + 15 * 60, "约 2 小时 15 分钟前"),
        (9 * 3600, "约 9 小时前"), (26 * 3600, "约 1 天 2 小时前"), (5 * 86400, "约 5 天前"),
    ])
    def test_fmt_ago(self, sec, expect):
        assert fr.fmt_ago(sec) == expect


class TestConfig:
    def test_defaults_off(self, make_config):
        cfg = make_config().freedom_config
        assert cfg == fr.FreedomConfig()
        assert cfg.enabled is False and cfg.eval_minutes == 20 and cfg.owner_quiet_minutes == 10
        assert cfg.min_gap_minutes == 10 and cfg.breaker_24h == 20 and cfg.hush_hours == 12.0

    def test_overrides_and_clamps(self, make_config):
        cfg = make_config(hidden={
            "lifelike_proactive_freedom": "true",
            "lifelike_freedom_eval_minutes": 1,
            "lifelike_freedom_owner_quiet_minutes": 999,
            "lifelike_freedom_min_gap_minutes": 0,
            "lifelike_freedom_breaker_24h": 1,
            "lifelike_freedom_hush_hours": 500,
        }).freedom_config
        assert cfg.enabled is True and cfg.eval_minutes == 5 and cfg.owner_quiet_minutes == 120
        assert cfg.min_gap_minutes == 5 and cfg.breaker_24h == 3 and cfg.hush_hours == 72.0

    def test_bad_value_falls_back(self, make_config):
        cfg = make_config(hidden={"lifelike_freedom_eval_minutes": "abc", **ON}).freedom_config
        assert cfg.eval_minutes == 20 and cfg.enabled is True

    def test_registered_in_hidden_config(self):
        from astrbot_plugin_iris_memory.iris_memory.config.defaults import Defaults

        d = Defaults()
        assert d.get_by_flat_key("lifelike_proactive_freedom") is False
        assert d.get_by_flat_key("lifelike_freedom_eval_minutes") == 20
        assert d.get_by_flat_key("lifelike_freedom_breaker_24h") == 20


# ===================================================================== 闸门（候选池）


def _ctx(**over):
    kw = dict(now=1_000_000.0, ctype=g.CTYPE_GROUP_CARE, freedom=True)
    kw.update(over)
    return g.GateContext(**kw)


class TestGateFreedom:
    @pytest.mark.parametrize("over", [
        dict(push_sent_24h=9, push_daily_cap=3),
        dict(last_push_at=1_000_000.0 - 60),
        dict(last_push_unreplied=True, last_push_ctype=g.CTYPE_GROUP_CARE),
        dict(same_ctype_sent_24h=True),
        dict(care_sent_today=5, care_sent_week=9),
    ])
    def test_frequency_gates_lifted(self, over):
        assert g.check(_ctx(**over)).ok is True
        assert g.check(_ctx(freedom=False, **over)).ok is False  # 关闭时原闸门照旧

    @pytest.mark.parametrize("over,reason", [
        (dict(dedupe_used=True), g.G_DUP),
        (dict(busy_until=1_000_000.0 + 60), g.G_BUSY),
        (dict(scope_is_owner_private=False), g.G_PRIVACY),
        (dict(delivery=g.DELIVERY_PASSIVE), g.G_DELIVERY),
        (dict(ctype_cooldown_until=1_000_000.0 + 60), g.G_COOLDOWN),
        (dict(care_category_cooldown_until=1_000_000.0 + 60), g.G_CARE_COOLDOWN),
        (dict(budget_exhausted=True), g.G_BUDGET),
        (dict(crisis=True, crisis_sent_24h=True), g.G_CRISIS),  # 同类危机 24h 一次：作为去重保留
    ])
    def test_safety_and_owner_will_kept(self, over, reason):
        res = g.check(_ctx(**over))
        assert res.ok is False and res.reason == reason


    @pytest.mark.parametrize("mode", [g.RELATION_SOFTEN, g.RELATION_PAUSE])
    @pytest.mark.parametrize("freedom", [True, False])
    def test_relation_cooling_never_blocks(self, mode, freedom):
        """P2U 合并（U2 × U8）：冷静期只影响排序与措辞，不拦截任何候选，与 freedom 无关"""
        assert g.check(_ctx(relation_mode=mode, freedom=freedom)).ok is True
        assert g.check(_ctx(relation_mode=mode, freedom=freedom, ctype="life_share")).ok is True


class TestCoordinatorFreedomFlag:
    def test_parse(self):
        from astrbot_plugin_iris_memory.iris_memory.lifelike.initiative.coordinator import InitiativeCoordinator

        def coord(val):
            c = InitiativeCoordinator.__new__(InitiativeCoordinator)
            c._get = lambda k, d=None: val if k == "lifelike_proactive_freedom" else d
            return c

        assert coord(True).freedom() is True
        assert coord("on").freedom() is True
        assert coord(None).freedom() is False
        assert coord("false").freedom() is False


# ===================================================================== 引擎


class FakeBridge:
    def __init__(self, snap=None, owner=True, status=None):
        self.snap = snap or ActivitySnapshot()
        self.owner = owner
        self.status = status or {}
        self.begin_calls = []

    async def candidates(self, scope, now=None):
        return []

    def is_owner_private_scope(self, scope):
        return self.owner and scope == SCOPE

    async def activity_snapshot(self, scope, lookback_days, *, with_history=True):
        return self.snap

    async def owner_status(self, scope):
        return dict(self.status)

    async def begin_turn(self, scope, forced=False, **kw):
        self.begin_calls.append((scope, forced, kw))
        return SimpleNamespace(turn=None, reason="test_stop", candidates=[])


def _all_day_quiet():
    t = time.localtime()
    cur = t.tm_hour * 60 + t.tm_min
    s, e = (cur - 5) % 1440, (cur + 5) % 1440
    return f"{s // 60:02d}:{s % 60:02d}-{e // 60:02d}:{e % 60:02d}"


def _engine(nm_config, *, bridge=None, hidden=None):
    config = nm_config(
        cfg={"proactive": {"enabled": True, "proactive_enabled": True, "provider_id": "p",
                           "private_enabled": True, "private_whitelist": [OWNER]}},
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
    engine = ProactiveEngine(
        context, config, state, window, SignalGate(config, state), core, Mock(),
        llm_manager=SimpleNamespace(generate_direct=AsyncMock()),
        packager=packager, umo_get=lambda _gid: "umo", is_busy=lambda _gid: False,
        self_id_get=lambda: "bot", save_fn=AsyncMock(), bridge=bridge,
    )
    state.add_to_whitelist(SCOPE)
    return engine, state, window


def _prime(state, window, owner_ago=3 * 3600):
    ts = time.time() - owner_ago
    window.append(SCOPE, WindowMessage(OWNER, "主人", "x", ts))
    state.record_human_message(SCOPE, ts)


def _exhaust_legacy_limits(state):
    """现网所有频率 / 未回复类限制同时命中"""
    data = state.get_state(SCOPE)
    now = time.time()
    data.initiate_timestamps = [now - 3600 * i for i in range(1, 11)]  # 10 条（> max_per_day）
    data.initiate_daily_count = 10
    data.last_initiate_time = now - 15 * 60  # < proactive_min_interval
    data.initiate_no_reply_streak = 5  # >= max_streak
    data.initiate_pending_since = now - 15 * 60  # 还在等回复


class TestEngineOff:
    def test_off_is_legacy(self, nm_config, monkeypatch):
        engine, state, _ = _engine(nm_config, bridge=FakeBridge())
        assert engine._freedom_on(SCOPE) is False
        monkeypatch.setattr(proactive_mod.random, "uniform", lambda a, b: a)
        now = time.time()
        assert engine._schedule_group(SCOPE, now=now) == pytest.approx(now + 300 * 0.6)
        state.get_state(SCOPE).initiate_no_reply_streak = 5
        assert engine._is_paused(SCOPE, False) is True
        assert engine.note_owner_text(SCOPE, "别发了") == ""
        assert state.get_proactive_hush(SCOPE) == 0.0
        assert "proactive_hush_until" not in state._serialize_group(state.get_state(SCOPE))

    def test_group_scope_never_free(self, nm_config):
        engine, _, _ = _engine(nm_config, bridge=FakeBridge(), hidden=ON)
        assert engine._freedom_on(SCOPE) is True
        assert engine._freedom_on(GROUP) is False
        assert engine._freedom_on(f"private:999") is False

    def test_no_bridge_never_free(self, nm_config):
        engine, _, _ = _engine(nm_config, bridge=None, hidden=ON)
        assert engine._freedom_on(SCOPE) is False


class TestEngineOn:
    def test_schedule_cadence(self, nm_config, monkeypatch):
        engine, _, _ = _engine(nm_config, bridge=FakeBridge(), hidden=ON)
        now = time.time()
        monkeypatch.setattr(proactive_mod.random, "uniform", lambda a, b: a)
        assert engine._schedule_group(SCOPE, now=now) == pytest.approx(now + 20 * 60 * 0.8)
        assert engine._schedule_group(SCOPE, now=now, after_activity=True) == pytest.approx(now + 600)
        monkeypatch.setattr(proactive_mod.random, "uniform", lambda a, b: b)
        assert engine._schedule_group(SCOPE, now=now) == pytest.approx(now + 20 * 60 * 1.2)
        assert engine._schedule_group(SCOPE, now=now, not_before=now + 9999) == pytest.approx(now + 9999)

    def test_schedule_ignores_quiet_hours(self, nm_config, monkeypatch):
        engine, _, _ = _engine(nm_config, bridge=FakeBridge(),
                               hidden={**ON, "lifelike_proactive_quiet_hours": _all_day_quiet()})
        monkeypatch.setattr(proactive_mod.random, "uniform", lambda a, b: a)
        now = time.time()
        assert engine._schedule_group(SCOPE, now=now) == pytest.approx(now + 960)

    @pytest.mark.asyncio
    async def test_verdict_ignores_quiet_and_activity_gate(self, nm_config, monkeypatch):
        engine, _, window = _engine(nm_config, bridge=FakeBridge(),
                                    hidden={**ON, "lifelike_proactive_quiet_hours": _all_day_quiet()})
        _prime(engine._state, window)
        monkeypatch.setattr(proactive_mod, "evaluate_activity_gate", lambda *a, **k: "inactive_hour")
        assert await engine._floor_verdict(SCOPE, window.get_messages(SCOPE)) == ("", None)

    @pytest.mark.asyncio
    async def test_scan_attempts_despite_legacy_limits(self, nm_config):
        engine, state, window = _engine(nm_config, bridge=FakeBridge(), hidden=ON)
        _prime(state, window)
        _exhaust_legacy_limits(state)
        engine.attempt_initiate = AsyncMock(return_value="ok")
        engine._signals.evaluate_timer = Mock(return_value=None)
        state.set_next_initiate_check(SCOPE, time.time() - 1)
        await engine._scan_due()
        engine.attempt_initiate.assert_awaited_once_with(SCOPE)
        engine._signals.evaluate_timer.assert_not_called()  # 不走概率风险率
        assert engine._is_paused(SCOPE, False) is False  # 未回复不再暂停
        assert engine._initiate_guard_reason(SCOPE, time.time(), require_quiet=True) == ""

    @pytest.mark.asyncio
    async def test_scan_same_limits_off_does_not_attempt(self, nm_config):
        engine, state, window = _engine(nm_config, bridge=FakeBridge())
        _prime(state, window)
        _exhaust_legacy_limits(state)
        engine.attempt_initiate = AsyncMock(return_value="ok")
        state.set_next_initiate_check(SCOPE, time.time() - 1)
        await engine._scan_due()
        engine.attempt_initiate.assert_not_awaited()

    @pytest.mark.parametrize("setup,reason", [
        (lambda st: setattr(st.get_state(SCOPE), "last_initiate_time", time.time() - 300), fr.R_FLOOR_GAP),
        (lambda st: st.set_proactive_hush(SCOPE, time.time() + 3600), fr.R_HUSH),
        (lambda st: st.set_cooldown(SCOPE, 30), fr.R_SELF_COOLDOWN),
        (lambda st: st.record_human_message(SCOPE, time.time() - 120), fr.R_OWNER_RECENT),
    ])
    @pytest.mark.asyncio
    async def test_safety_net_blocks(self, nm_config, setup, reason, monkeypatch):
        engine, state, window = _engine(nm_config, bridge=FakeBridge(), hidden=ON)
        _prime(state, window)
        setup(state)
        log = Mock()
        monkeypatch.setattr(proactive_mod, "logger", log)
        engine.attempt_initiate = AsyncMock(return_value="ok")
        state.set_next_initiate_check(SCOPE, time.time() - 1)
        await engine._scan_due()
        engine.attempt_initiate.assert_not_awaited()
        assert any(c.args[1:] == (SCOPE, reason) for c in log.info.call_args_list)
        assert state.get_next_initiate_check(SCOPE) > time.time()  # 继续按节奏评估

    @pytest.mark.asyncio
    async def test_breaker_logs_error_once(self, nm_config, monkeypatch):
        engine, state, window = _engine(nm_config, bridge=FakeBridge(), hidden=ON)
        _prime(state, window, owner_ago=6 * 3600)
        now = time.time()
        state.get_state(SCOPE).initiate_timestamps = [now - 900 * i for i in range(1, 21)]
        log = Mock()
        monkeypatch.setattr(proactive_mod, "logger", log)
        engine.attempt_initiate = AsyncMock(return_value="ok")
        for _ in range(3):
            state.set_next_initiate_check(SCOPE, time.time() - 1)
            await engine._scan_due()
        engine.attempt_initiate.assert_not_awaited()
        assert len(log.error.call_args_list) == 1
        assert "op=breaker" in log.error.call_args_list[0].args[0]
        # 滚动窗口退下来后恢复，再次触发才会再记
        state.get_state(SCOPE).initiate_timestamps = []
        state.set_next_initiate_check(SCOPE, time.time() - 1)
        await engine._scan_due()
        engine.attempt_initiate.assert_awaited_once()

    def test_guard_ignores_quiet_and_own_cooldown_keeps_floor(self, nm_config):
        engine, state, window = _engine(nm_config, bridge=FakeBridge(),
                                        hidden={**ON, "lifelike_proactive_quiet_hours": _all_day_quiet()})
        _prime(state, window)
        state.set_cooldown(SCOPE, 60)  # 本轮决策给出的 cooldown 不拦截本轮发送
        cutoff = time.time()
        assert engine._initiate_guard_reason(SCOPE, cutoff, require_quiet=True) == ""
        state.get_state(SCOPE).last_initiate_time = time.time() - 60
        assert "floor_gap" in engine._initiate_guard_reason(SCOPE, cutoff, require_quiet=True)

    def test_hush_and_unhush(self, nm_config):
        engine, state, _ = _engine(nm_config, bridge=FakeBridge(), hidden=ON)
        assert engine.note_owner_text(SCOPE, "今天好累") == ""
        assert engine.note_owner_text(SCOPE, "别发了", 1000.0) == "hush"
        assert state.get_proactive_hush(SCOPE) == pytest.approx(1000.0 + 12 * 3600)
        state.set_proactive_hush(SCOPE, time.time() + 3600)
        assert engine._is_paused(SCOPE, False) is True
        assert engine._is_paused(SCOPE, True) is False
        # 持久化往返
        restored = state._deserialize_group(state._serialize_group(state.get_state(SCOPE)))
        assert restored.proactive_hush_until == pytest.approx(state.get_proactive_hush(SCOPE))
        assert engine.note_owner_text(SCOPE, "可以找我了") == "unhush"
        assert state.get_proactive_hush(SCOPE) == 0.0
        assert engine._is_paused(SCOPE, False) is False
        assert engine.note_owner_text(GROUP, "别发了") == ""

    @pytest.mark.asyncio
    async def test_empty_window_allowed_when_free(self, nm_config):
        last = time.time() - 30 * 3600
        bridge = FakeBridge(ActivitySnapshot(last_interaction=last))
        engine, state, _ = _engine(nm_config, bridge=bridge, hidden=ON)
        await engine._ensure_schedules()
        assert engine._empty_window_allowed(SCOPE) == pytest.approx(last)
        assert state.get_next_initiate_check(SCOPE) > 0

    @pytest.mark.asyncio
    async def test_attempt_passes_free_and_post_send_gap(self, nm_config):
        bridge = FakeBridge()
        engine, state, window = _engine(nm_config, bridge=bridge, hidden=ON)
        _prime(state, window)
        await engine.attempt_initiate(SCOPE)
        assert bridge.begin_calls == [(SCOPE, False, {"free": True})]
        assert engine._post_send_gap(SCOPE) == 600.0
        await engine.attempt_initiate(SCOPE, force=True)
        assert bridge.begin_calls[-1] == (SCOPE, True, {})  # 管理命令强制发起走原路径

    @pytest.mark.asyncio
    async def test_attempt_off_legacy_begin_turn(self, nm_config):
        bridge = FakeBridge()
        engine, state, window = _engine(nm_config, bridge=bridge)
        _prime(state, window)
        await engine.attempt_initiate(SCOPE)
        assert bridge.begin_calls == [(SCOPE, False, {})]
        assert engine._post_send_gap(SCOPE) == 360 * 60.0

    @pytest.mark.asyncio
    async def test_facts_from_state_and_bridge(self, nm_config):
        bridge = FakeBridge(status={"busy_until": time.time() + 3600, "closure_ended_at": time.time() - 600})
        engine, state, window = _engine(nm_config, bridge=bridge, hidden=ON)
        _prime(state, window, owner_ago=2 * 3600)
        data = state.get_state(SCOPE)
        data.last_initiate_time = time.time() - 3600
        data.initiate_timestamps = [data.last_initiate_time]
        data.initiate_no_reply_streak = 2
        facts = await engine._freedom_facts(SCOPE, window.get_messages(SCOPE))
        assert "约 2 小时前" in facts and "约 1 小时前" in facts and "对方之后还没有回" in facts
        assert "接连几次" in facts and "过去 24 小时你主动找过对方 1 次" in facts
        assert "在忙" in facts


class TestDecisionPrompt:
    def test_free_prompt_uses_facts(self, nm_config):
        engine, state, window = _engine(nm_config, bridge=FakeBridge(), hidden=ON)
        _prime(state, window)
        core = engine._core
        req = DecisionRequest(group_id=SCOPE, wake="timer", motive="initiate", quiet_minutes=180,
                              private=True, freedom_facts="<facts>\n- 现在：X\n</facts>")
        user, system = core.build_prompt(req)
        assert "<facts>" in user and "由你自己决定" in system and "想对方" in system
        assert "安静了 180 分钟" not in user

    def test_legacy_prompt_unchanged_without_facts(self, nm_config):
        engine, state, window = _engine(nm_config, bridge=FakeBridge(), hidden=ON)
        _prime(state, window)
        req = DecisionRequest(group_id=SCOPE, wake="timer", motive="initiate", quiet_minutes=180, private=True)
        user, system = engine._core.build_prompt(req)
        assert "<facts>" not in user and "安静了 180 分钟" in user and "倾向于不开口" in system


class TestState:
    def test_cooldown_state_enum(self, nm_config):
        _, state, _ = _engine(nm_config, bridge=FakeBridge(), hidden=ON)
        state.set_cooldown(SCOPE, 10)
        assert state.get_state(SCOPE).state == GroupState.COOLDOWN

    def test_deserialize_bad_hush(self, nm_config):
        _, state, _ = _engine(nm_config, bridge=FakeBridge(), hidden=ON)
        assert state._deserialize_group({"proactive_hush_until": "x"}).proactive_hush_until == 0.0


# ===================================================================== 审查修复（p2u review）


class TestHushReviewPhrases:
    """H1：取消限制后"别发了"是唯一刹车——补齐常见说法；L1/6：玩笑与无对象的"不用发了"不误判"""

    @pytest.mark.parametrize("text", [
        "别吵了", "我要睡了别吵", "消停点", "够了别发了", "能不能不要一直发消息", "别一直发消息",
        "不要老是发消息", "你发太多了", "别发消息了我在开会", "你今天能不能别再给我发消息了啊",
        "别发啦求你了", "你可以找我，但今晚别发了", "不用给我发了", "别主动找我了",
    ])
    def test_hush_now_detected(self, text):
        assert fr.is_hush(text) is True

    @pytest.mark.parametrize("text", [
        "不用发了", "安静", "今天安静", "闭嘴吧哈哈", "别烦了哈哈", "闭嘴啦😂", "要不要发个消息给他",
        "特别发愁", "别烦恼", "群里消息太多了", "别吵架了你们", "老师发太多作业了", "别打扰他休息",
        "终于消停了", "我不要发朋友圈", "别发那么长的",
    ])
    def test_not_hush(self, text):
        assert fr.is_hush(text) is False

    @pytest.mark.parametrize("text", ["可以说话吗", "你能说话吗", "可以找我吗", "你可以找我，但今晚别发了"])
    def test_not_unhush(self, text):
        assert fr.is_unhush(text) is False

    def test_hush_wins_over_unhush_in_engine(self, nm_config):
        engine, state, _ = _engine(nm_config, bridge=FakeBridge(), hidden=ON)
        assert engine.note_owner_text(SCOPE, "你可以找我，但今晚别发了", 1000.0) == "hush"
        assert state.get_proactive_hush(SCOPE) > 1000.0
        assert engine.note_owner_text(SCOPE, "可以说话吗") == ""
        assert state.get_proactive_hush(SCOPE) > 1000.0  # 问句不清除叫停


class TestBreakerResetsOnReply:
    def test_owner_reply_resets_breaker(self):
        cfg = fr.FreedomConfig(enabled=True)
        now = 1_000_000.0
        ts = [now - 900 * i for i in range(1, 21)]  # 20 条，最早 5 小时前
        kw = dict(now=now, cooldown_until=0.0, hush_until=0.0, last_initiate_at=0.0,
                  initiate_timestamps=ts, has_context=True)
        assert fr.block_reason(cfg, last_owner_at=now - 6 * 3600, **kw) == fr.R_BREAKER
        assert fr.block_reason(cfg, last_owner_at=now - 3600, **kw) == ""  # 主人回过话 → 不是失控
        assert fr.breaker_count(ts, now, None) == 20


class TestDedupe:
    def test_similar_text_or_same_topic(self):
        now = 1_000_000.0
        recent = [(now - 3600, "问问在干嘛", fr.normalize_text("在干嘛呀？有点想你了"))]
        assert fr.is_duplicate("在干嘛呀，有点想你～", "", recent, now) is True
        assert fr.is_duplicate("今天降温了，记得加衣服", "问问在干嘛", recent, now) is True
        assert fr.is_duplicate("今天降温了，记得加衣服", "天气", recent, now) is False
        old = [(now - 25 * 3600, "问问在干嘛", fr.normalize_text("在干嘛呀？有点想你了"))]
        assert fr.is_duplicate("在干嘛呀？有点想你了", "问问在干嘛", old, now) is False

    def test_state_roundtrip_and_cap(self, nm_config):
        _, state, _ = _engine(nm_config, bridge=FakeBridge(), hidden=ON)
        base = time.time()
        for i in range(40):
            state.note_freedom_sent(SCOPE, ts=base + i, topic=f"t{i}", norm_text=f"x{i}")
        recent = state.get_freedom_recent(SCOPE)
        assert len(recent) == 30 and recent[-1][1] == "t39"
        restored = state._deserialize_group(state._serialize_group(state.get_state(SCOPE)))
        assert restored.freedom_recent == recent
        assert "freedom_recent" not in state._serialize_group(state._deserialize_group({}))

    def test_nag_phrases(self):
        for t in ("人呢？", "怎么还不回我", "你又不理我", "快回我"):
            assert fr.nag_hit(t) is True
        assert fr.nag_hit("今天降温了，记得加衣服") is False


class TestFactsReview:
    def test_last_topic_and_cooling_are_qualitative(self):
        now = 1_000_000.0
        t = fr.build_facts(now=now, last_owner_at=now - 7200, last_initiate_at=now - 3600,
                           owner_replied_after_initiate=False, unanswered_streak=4, initiated_24h=4,
                           last_topic="问他午饭吃了什么", cooling=True)
        assert "问他午饭吃了什么" in t and "不愉快" in t and "接连几次" in t
        assert "连续 4 次" not in t


class CoolingBridge(FakeBridge):
    def __init__(self, cooling=False, **kw):
        super().__init__(**kw)
        self.cooling = cooling
        self.sent = []

    async def relation_cooling(self, scope):
        return self.cooling

    async def owner_status(self, scope):
        return {**self.status, **({"cooling": True} if self.cooling else {})}

    async def begin_turn(self, scope, forced=False, **kw):
        self.begin_calls.append((scope, forced, kw))
        return SimpleNamespace(turn={"turn_id": "t1"}, reason="", candidates=[])

    async def avoid_topics(self, scope):
        return []

    async def mark_turn(self, *a, **kw):
        return None

    async def close_turn(self, *a, **kw):
        return None


def _speak(topic="想他了"):
    from astrbot_plugin_iris_memory.iris_memory.proactive.decision import DecisionOutcome
    from astrbot_plugin_iris_memory.iris_memory.proactive.parser import Decision

    return DecisionOutcome(decision=Decision(action="speak", topic=topic, why_now="想他"),
                           system_prompt="", user_prompt="")


def _wire(engine, speech, topic="想他了"):
    engine._core.decide = AsyncMock(return_value=_speak(topic))
    engine._generate_speech = AsyncMock(return_value=speech)
    engine._send_via_bridge = AsyncMock(return_value="已发起: x")


class TestFreeSendPathReview:
    """审查 1/2/M1/M2：非候选的自由主动也要过 去重 / 冷静期试探句 / 反情绪勒索（不依赖口吻开关）"""

    @pytest.mark.asyncio
    async def test_cooling_probe_blocked_for_non_candidate(self, nm_config):
        bridge = CoolingBridge(cooling=True)
        engine, state, window = _engine(nm_config, bridge=bridge, hidden=ON)
        _prime(state, window)
        _wire(engine, "在吗？")
        assert await engine.attempt_initiate(SCOPE) == "已取消发起：cooling_probe"
        engine._send_via_bridge.assert_not_awaited()
        # 不在冷静期：同一句照常发（不改频率，只看内容）
        bridge.cooling = False
        engine._skip_retry_after.clear()
        assert (await engine.attempt_initiate(SCOPE)).startswith("已发起")

    @pytest.mark.asyncio
    async def test_cooling_fact_reaches_decision(self, nm_config):
        bridge = CoolingBridge(cooling=True)
        engine, state, window = _engine(nm_config, bridge=bridge, hidden=ON)
        _prime(state, window)
        _wire(engine, "今天降温了，记得加衣服")
        await engine.attempt_initiate(SCOPE)
        req = engine._core.decide.await_args.args[0]
        assert "不愉快" in req.freedom_facts

    @pytest.mark.asyncio
    async def test_duplicate_free_message_skipped(self, nm_config):
        engine, state, window = _engine(nm_config, bridge=CoolingBridge(), hidden=ON)
        _prime(state, window)
        state.note_freedom_sent(SCOPE, ts=time.time() - 3600, topic="别的",
                                norm_text=fr.normalize_text("在干嘛呀？有点想你了"))
        _wire(engine, "在干嘛呀，有点想你了～")
        assert await engine.attempt_initiate(SCOPE) == "已取消发起：freedom_dedupe"
        engine._send_via_bridge.assert_not_awaited()
        assert engine._skip_retry_after[SCOPE] >= time.time() + 60  # 按评估节奏再试
        # 换了新内容就照常发
        _wire(engine, "今天降温了，记得加衣服", topic="天气")
        engine._skip_retry_after.clear()
        assert (await engine.attempt_initiate(SCOPE)).startswith("已发起")

    @pytest.mark.asyncio
    async def test_man_and_nag_enforced_even_if_style_off(self, nm_config, monkeypatch):
        from astrbot_plugin_iris_memory.iris_memory.style import selector as S

        monkeypatch.setattr(S, "proactive_style_enabled", lambda: False)
        engine, state, window = _engine(nm_config, bridge=CoolingBridge(), hidden=ON)
        _prime(state, window)
        for speech in ("你都不理我，害我一直等", "人呢？怎么还不回"):
            _wire(engine, speech)
            engine._skip_retry_after.clear()
            assert await engine.attempt_initiate(SCOPE) == "已取消发起：style_man"
        engine._send_via_bridge.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_off_does_not_dedupe_or_nag(self, nm_config, monkeypatch):
        from astrbot_plugin_iris_memory.iris_memory.style import selector as S

        monkeypatch.setattr(S, "proactive_style_enabled", lambda: False)
        engine, state, window = _engine(nm_config, bridge=CoolingBridge(), hidden=None)
        _prime(state, window)
        state.note_freedom_sent(SCOPE, ts=time.time() - 60, topic="x", norm_text=fr.normalize_text("人呢"))
        assert await engine._content_block(SCOPE, "人呢", _speak().decision,
                                           free=False, private=True, force=False) == ""
        assert proactive_mod._proactive_man_hits("你都不理我，害我一直等") == []

    def test_status_lines(self, nm_config):
        engine, state, window = _engine(nm_config, bridge=CoolingBridge(), hidden=ON)
        _prime(state, window)
        state.set_proactive_hush(SCOPE, time.time() + 2 * 3600)
        lines = "\n".join(engine.freedom_status_lines(SCOPE))
        assert "熔断: 未触发" in lines and "剩余 2.0 小时" in lines
        assert "主人叫停主动剩余" in state.get_status_text(SCOPE)
        off, _, _ = _engine(nm_config, bridge=CoolingBridge())
        assert off.freedom_status_lines(SCOPE) == []
