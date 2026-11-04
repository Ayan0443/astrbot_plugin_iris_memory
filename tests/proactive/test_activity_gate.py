"""活人感 P0-8：主动安全底线（硬静音开关、活动窗口判定、窗口为空时的互动判定）。

纯函数一律用 ``time.gmtime`` 作为本地时间函数 + ``calendar.timegm`` 构造时间戳（假时钟），
结果与运行机器的时区和真实时刻无关。
"""

import calendar
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_iris_memory.iris_memory.proactive import activity_gate as ag
from astrbot_plugin_iris_memory.iris_memory.proactive import proactive as proactive_mod
from astrbot_plugin_iris_memory.iris_memory.proactive.activity_gate import (
    ActivityGateConfig,
    ActivitySnapshot,
    evaluate_activity_gate,
    hourly_active_days,
    in_minute_range,
    is_inactive_hour,
    parse_hhmm_range,
    seconds_until_range_end,
)
from astrbot_plugin_iris_memory.iris_memory.proactive.decision import DecisionCore
from astrbot_plugin_iris_memory.iris_memory.proactive.perception import (
    ContextPackager,
    SlidingWindow,
    WindowMessage,
)
from astrbot_plugin_iris_memory.iris_memory.proactive.proactive import ProactiveEngine
from astrbot_plugin_iris_memory.iris_memory.proactive.signals import SignalGate
from astrbot_plugin_iris_memory.iris_memory.proactive.state import StateManager

GM = time.gmtime
OWNER = "123456789"
SCOPE = f"private:{OWNER}"


def ts(day: int, hour: int, minute: int = 0, sec: int = 0) -> float:
    return float(calendar.timegm((2026, 10, day, hour, minute, sec, 0, 0, 0)))


# ===================================================================== 纯函数


class TestParseRange:
    @pytest.mark.parametrize("text,expected", [
        ("01:00-09:00", (60, 540)),
        ("23:30-07:15", (1410, 435)),
        (" 1:05 - 2:00 ", (65, 120)),
        ("22:00~06:00", (1320, 360)),
    ])
    def test_valid(self, text, expected):
        assert parse_hhmm_range(text) == expected

    @pytest.mark.parametrize("text", ["", "  ", None, 5, "0:00-0:00", "09:00-09:00", "25:00-01:00",
                                      "01:60-02:00", "abc", "01:00", "01:00-"])
    def test_empty_invalid_or_zero_length_is_none(self, text):
        assert parse_hhmm_range(text) is None


class TestInRange:
    def test_same_day_boundaries(self):
        rng = (60, 540)
        assert not in_minute_range(59, rng)
        assert in_minute_range(60, rng)  # 起点包含
        assert in_minute_range(539, rng)
        assert not in_minute_range(540, rng)  # 终点不含

    def test_cross_midnight_boundaries(self):
        rng = (1410, 435)  # 23:30–07:15
        assert not in_minute_range(1409, rng)
        assert in_minute_range(1410, rng)
        assert in_minute_range(0, rng)
        assert in_minute_range(434, rng)
        assert not in_minute_range(435, rng)
        assert not in_minute_range(720, rng)

    def test_none_never_matches(self):
        assert not in_minute_range(100, None)

    def test_seconds_until_end(self):
        rng = parse_hhmm_range("23:30-07:15")
        assert seconds_until_range_end(ts(5, 23, 30), rng, GM) == pytest.approx((7 * 60 + 45) * 60)
        assert seconds_until_range_end(ts(5, 7, 14, 30), rng, GM) == pytest.approx(30)
        assert seconds_until_range_end(ts(5, 12, 0), rng, GM) == 0.0
        assert seconds_until_range_end(ts(5, 1, 0), None, GM) == 0.0


class TestHistogram:
    def test_counts_distinct_days_per_hour_within_lookback(self):
        now = ts(29, 12)
        stamps = [
            ts(28, 22, 1), ts(28, 22, 40),  # 同一天同一小时只算 1 天
            ts(27, 22, 5), ts(27, 9, 0),
            ts(1, 3, 0),  # 超出 7 天回看
            now + 3600,  # 未来时间忽略
            "bad",
        ]
        per_hour, total = hourly_active_days(stamps, now=now, lookback_days=7, localtime=GM)
        assert per_hour[22] == 2 and per_hour[9] == 1 and per_hour[3] == 0
        assert total == 2


class TestInactiveHour:
    def test_history_decides_when_enough_days(self):
        per_hour = [0] * 24
        per_hour[22] = 9
        per_hour[3] = 0
        per_hour[8] = 1
        kw = dict(min_days=7, inactive_ratio=0.1, prior=None)
        assert is_inactive_hour(per_hour, 10, hour=3, minute_of_day=180, **kw) is True
        assert is_inactive_hour(per_hour, 10, hour=22, minute_of_day=1320, **kw) is False
        # 恰好等于阈值不算"几乎不活跃"
        assert is_inactive_hour(per_hour, 10, hour=8, minute_of_day=480, **kw) is False

    def test_falls_back_to_prior_when_insufficient(self):
        per_hour = [0] * 24
        prior = parse_hhmm_range("01:00-08:00")
        kw = dict(min_days=7, inactive_ratio=0.1)
        assert is_inactive_hour(per_hour, 3, hour=3, minute_of_day=180, prior=prior, **kw) is True
        assert is_inactive_hour(per_hour, 3, hour=12, minute_of_day=720, prior=prior, **kw) is False
        # 无先验：数据不足时不推迟
        assert is_inactive_hour(per_hour, 3, hour=3, minute_of_day=180, prior=None, **kw) is False


def _night_owl_history(now: float) -> list[float]:
    """10 个活跃日，每天 21–23 点说话，凌晨从不说话。"""
    out = []
    for d in range(1, 11):
        base = now - d * 86400
        day = time.gmtime(base)
        for h in (21, 22, 23):
            out.append(float(calendar.timegm((day.tm_year, day.tm_mon, day.tm_mday, h, 10, 0, 0, 0, 0))))
    return out


class TestEvaluateGate:
    CFG = ActivityGateConfig(activity_gate=True, recent_hours=6, lookback_days=28, min_days=7,
                             inactive_ratio=0.1, prior_inactive=parse_hhmm_range("01:00-08:00"))

    def test_disabled_never_defers(self):
        now = ts(20, 3)
        cfg = ActivityGateConfig(activity_gate=False)
        assert evaluate_activity_gate(cfg, now=now, last_interaction=None, owner_timestamps=[], localtime=GM) == ""

    def test_inactive_hour_and_no_recent_interaction_defers(self):
        now = ts(20, 3)
        hist = _night_owl_history(now)
        last = now - 7 * 3600
        assert evaluate_activity_gate(self.CFG, now=now, last_interaction=last,
                                      owner_timestamps=hist, localtime=GM) == "inactive_hour"

    def test_recent_interaction_within_six_hours_allows(self):
        now = ts(20, 3)
        hist = _night_owl_history(now)
        last = now - 6 * 3600 + 1  # 5:59:59 前还在聊
        assert evaluate_activity_gate(self.CFG, now=now, last_interaction=last,
                                      owner_timestamps=hist, localtime=GM) == ""
        # 恰好 6 小时：不再算"最近"
        assert evaluate_activity_gate(self.CFG, now=now, last_interaction=now - 6 * 3600,
                                      owner_timestamps=hist, localtime=GM) == "inactive_hour"

    def test_active_hour_allows_even_without_recent_interaction(self):
        now = ts(20, 22)
        hist = _night_owl_history(now)
        assert evaluate_activity_gate(self.CFG, now=now, last_interaction=None,
                                      owner_timestamps=hist, localtime=GM) == ""

    def test_unknown_last_interaction_with_no_history_uses_prior(self):
        assert evaluate_activity_gate(self.CFG, now=ts(20, 3), last_interaction=None,
                                      owner_timestamps=[], localtime=GM) == "inactive_hour"
        assert evaluate_activity_gate(self.CFG, now=ts(20, 8), last_interaction=None,
                                      owner_timestamps=[], localtime=GM) == ""  # 08:00 不在先验内


# ===================================================================== 配置


class TestConfig:
    def test_defaults_without_hidden_values(self, make_config):
        cfg = make_config().activity_gate_config
        assert cfg.quiet_hours is None  # D4：默认不设硬静音
        assert cfg.activity_gate is True and cfg.empty_window is False  # 审查 M5：首发默认关
        assert cfg.recent_hours == 6.0 and cfg.lookback_days == 28 and cfg.min_days == 7
        assert cfg.prior_inactive == (60, 480)

    def test_hidden_overrides_and_clamps(self, make_config):
        cfg = make_config(hidden={
            "lifelike_proactive_quiet_hours": "01:00-09:00",
            "lifelike_proactive_activity_gate": "false",
            "lifelike_activity_recent_hours": "999",
            "lifelike_activity_inactive_ratio": -1,
            "lifelike_activity_prior_inactive": "",
            "lifelike_proactive_empty_window": False,
        }).activity_gate_config
        assert cfg.quiet_hours == (60, 540)
        assert cfg.activity_gate is False and cfg.empty_window is False
        assert cfg.recent_hours == 72.0 and cfg.inactive_ratio == 0.0
        assert cfg.prior_inactive is None

    def test_garbage_falls_back_to_defaults(self, make_config):
        cfg = make_config(hidden={"lifelike_activity_lookback_days": "abc",
                                  "lifelike_proactive_quiet_hours": "nope"}).activity_gate_config
        assert cfg.lookback_days == 28 and cfg.quiet_hours is None

    def test_hidden_config_defaults_registered(self):
        from astrbot_plugin_iris_memory.iris_memory.config.defaults import Defaults

        d = Defaults()
        assert d.get_by_flat_key("lifelike_proactive_quiet_hours") == ""
        assert d.get_by_flat_key("lifelike_proactive_activity_gate") is True
        assert d.get_by_flat_key("lifelike_proactive_empty_window") is False
        assert d.get_by_flat_key("lifelike_activity_recent_hours") == 6.0


# ===================================================================== evaluate_timer


def _all_day_quiet_hours():
    """覆盖当前时刻的硬静音区间（与真实时刻无关）"""
    t = time.localtime()
    cur = t.tm_hour * 60 + t.tm_min
    start, end = (cur - 5) % 1440, (cur + 5) % 1440
    return f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}"


class TestEvaluateTimer:
    def _gate(self, nm_config, hidden=None):
        config = nm_config(cfg={"proactive": {"enabled": True, "proactive_enabled": True}}, hidden=hidden)
        state = StateManager(config)
        return SignalGate(config, state), state

    def _prime(self, state, gid, quiet_s=6 * 3600):
        now = time.time()
        state.record_human_message(gid, now - quiet_s)
        state.get_state(gid).initiate_hazard_at = now - 3600

    def test_quiet_hours_never_fires(self, nm_config, make_msg, monkeypatch):
        gate, state = self._gate(nm_config, {"lifelike_proactive_quiet_hours": _all_day_quiet_hours()})
        gid = SCOPE
        self._prime(state, gid)
        monkeypatch.setattr("random.random", lambda: 0.0)
        msgs = [make_msg(timestamp=time.time() - 6 * 3600)]
        assert gate.lifelike_quiet_now() is True
        assert gate.evaluate_timer(gid, msgs) is None
        assert state.get_state(gid).initiate_hazard_probability == 0.0

    def test_without_quiet_hours_same_input_fires(self, nm_config, make_msg, monkeypatch):
        gate, state = self._gate(nm_config)
        gid = SCOPE
        self._prime(state, gid)
        monkeypatch.setattr("random.random", lambda: 0.0)
        assert gate.lifelike_quiet_now() is False
        assert gate.evaluate_timer(gid, [make_msg(timestamp=time.time() - 6 * 3600)]) == "initiate"

    def test_defer_freezes(self, nm_config, make_msg, monkeypatch):
        gate, state = self._gate(nm_config)
        self._prime(state, SCOPE)
        monkeypatch.setattr("random.random", lambda: 0.0)
        assert gate.evaluate_timer(SCOPE, [make_msg(timestamp=time.time() - 6 * 3600)], defer=True) is None

    def test_empty_window_legacy_vs_hint(self, nm_config, monkeypatch):
        gate, state = self._gate(nm_config)
        state.get_state(SCOPE).initiate_hazard_at = time.time() - 3600
        monkeypatch.setattr("random.random", lambda: 0.0)
        assert gate.evaluate_timer(SCOPE, []) is None  # 现网：窗口为空不触发
        assert gate.evaluate_timer(SCOPE, [], last_activity_hint=time.time() - 30 * 3600) == "initiate"

    def test_config_without_accessor_is_not_quiet(self, nm_config):
        gate, _ = self._gate(nm_config)
        gate._config = SimpleNamespace()
        assert gate.lifelike_quiet_now() is False


# ===================================================================== 引擎接线


class FakeBridge:
    def __init__(self, snap=None, error=None, owner=True, cands=None, cand_error=None):
        self.snap = snap or ActivitySnapshot()
        self.error = error
        self.owner = owner
        self.calls = 0
        self.cands = cands or []
        self.cand_error = cand_error
        self.cand_calls = 0

    async def candidates(self, scope, now=None):
        self.cand_calls += 1
        if self.cand_error is not None:
            raise self.cand_error
        return list(self.cands)

    def is_owner_private_scope(self, scope):
        return self.owner and scope == SCOPE

    async def activity_snapshot(self, scope, lookback_days, *, with_history=True):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.snap


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


EW_ON = {"lifelike_proactive_empty_window": True}


class TestEngineEmptyWindow:
    @pytest.mark.asyncio
    async def test_default_off_keeps_legacy_no_schedule(self, nm_config):
        bridge = FakeBridge(ActivitySnapshot(last_interaction=time.time() - 30 * 3600))
        engine, state, _ = _engine(nm_config, bridge=bridge)
        await engine._ensure_schedules()
        assert state.get_next_initiate_check(SCOPE) == 0
        assert bridge.calls == 0

    @pytest.mark.asyncio
    async def test_empty_window_scheduled_from_ledger(self, nm_config):
        last = time.time() - 30 * 3600
        bridge = FakeBridge(ActivitySnapshot(last_interaction=last))
        engine, state, _ = _engine(nm_config, bridge=bridge, hidden=EW_ON)
        await engine._ensure_schedules()
        assert engine._empty_window_allowed(SCOPE) == pytest.approx(last)
        assert state.get_next_initiate_check(SCOPE) > 0  # 不再因窗口为空丢失调度

    @pytest.mark.asyncio
    async def test_switch_off_keeps_legacy_no_schedule(self, nm_config):
        bridge = FakeBridge(ActivitySnapshot(last_interaction=time.time() - 30 * 3600))
        engine, state, _ = _engine(nm_config, bridge=bridge, hidden={"lifelike_proactive_empty_window": False})
        await engine._ensure_schedules()
        assert engine._empty_window_allowed(SCOPE) is None
        assert state.get_next_initiate_check(SCOPE) == 0
        assert bridge.calls == 0

    @pytest.mark.asyncio
    async def test_no_bridge_keeps_legacy(self, nm_config):
        engine, state, _ = _engine(nm_config, bridge=None)
        await engine._ensure_schedules()
        assert state.get_next_initiate_check(SCOPE) == 0

    @pytest.mark.asyncio
    async def test_no_known_interaction_keeps_legacy(self, nm_config):
        engine, state, _ = _engine(nm_config, bridge=FakeBridge(ActivitySnapshot(last_interaction=None)), hidden=EW_ON)
        await engine._ensure_schedules()
        assert state.get_next_initiate_check(SCOPE) == 0

    @pytest.mark.asyncio
    async def test_scan_passes_hint_and_guard_accepts_empty_window(self, nm_config):
        last = time.time() - 30 * 3600
        engine, state, _ = _engine(
            nm_config, bridge=FakeBridge(ActivitySnapshot(last_interaction=last)),
            hidden={"lifelike_proactive_activity_gate": False, **EW_ON},
        )
        await engine._ensure_schedules()
        state.set_next_initiate_check(SCOPE, time.time() - 1)
        engine._signals.evaluate_timer = Mock(return_value=None)
        await engine._scan_due()
        call = engine._signals.evaluate_timer.call_args
        assert call.args[0] == SCOPE and call.kwargs["last_activity_hint"] == pytest.approx(last)
        assert call.kwargs["defer"] is False
        # 发送前复查：空窗 + 账本最后互动足够久 → 不因"缺少上下文"取消
        assert engine._initiate_guard_reason(SCOPE, time.time(), require_quiet=True) == ""

    def test_guard_legacy_message_when_no_hint(self, nm_config):
        engine, _, _ = _engine(nm_config, bridge=FakeBridge())
        assert engine._initiate_guard_reason(SCOPE, time.time(), require_quiet=True) == "缺少可用群聊上下文"


class TestEngineActivityGate:
    @pytest.mark.asyncio
    async def test_defer_passed_to_evaluate_timer_and_logged_once(self, nm_config, monkeypatch):
        engine, state, window = _engine(nm_config, bridge=FakeBridge())
        window.append(SCOPE, WindowMessage(OWNER, "主人", "x", time.time() - 7 * 3600))
        monkeypatch.setattr(proactive_mod, "evaluate_activity_gate", lambda *a, **k: "inactive_hour")
        log = Mock()
        monkeypatch.setattr(proactive_mod, "logger", log)
        engine._signals.evaluate_timer = Mock(return_value=None)
        for _ in range(2):
            state.set_next_initiate_check(SCOPE, time.time() - 1)
            await engine._scan_due()
        assert engine._signals.evaluate_timer.call_args.kwargs["defer"] is True
        defer_logs = [c for c in log.info.call_args_list if "op=defer" in c.args[0]]
        assert len(defer_logs) == 1 and "reason=%s" in defer_logs[0].args[0]

    @pytest.mark.asyncio
    async def test_last_activity_ignores_bot_own_messages(self, nm_config, monkeypatch):
        engine, state, window = _engine(nm_config, bridge=FakeBridge())
        owner_ts = time.time() - 9 * 3600
        window.append(SCOPE, WindowMessage(OWNER, "主人", "x", owner_ts))
        window.append(SCOPE, WindowMessage("bot", "我", "主动", time.time() - 60))
        seen = {}

        def fake_eval(cfg, *, now, last_interaction, owner_timestamps, **_):
            seen["last"] = last_interaction
            return ""

        monkeypatch.setattr(proactive_mod, "evaluate_activity_gate", fake_eval)
        await engine._floor_verdict(SCOPE, window.get_messages(SCOPE))
        assert seen["last"] == pytest.approx(owner_ts)

    @pytest.mark.asyncio
    async def test_gate_off_or_group_scope_never_queries(self, nm_config):
        bridge = FakeBridge()
        engine, _, window = _engine(nm_config, bridge=bridge, hidden={"lifelike_proactive_activity_gate": False})
        window.append(SCOPE, WindowMessage(OWNER, "主人", "x", time.time() - 7 * 3600))
        assert await engine._floor_verdict(SCOPE, window.get_messages(SCOPE)) == ("", None)
        engine2, _, _ = _engine(nm_config, bridge=bridge)
        assert await engine2._floor_verdict("-100123", [WindowMessage("u", "u", "x", 1.0)]) == ("", None)
        assert bridge.calls == 0

    @pytest.mark.asyncio
    async def test_snapshot_failure_falls_back_and_breaker_trips(self, nm_config, monkeypatch):
        bridge = FakeBridge(error=RuntimeError("db"))
        engine, _, window = _engine(nm_config, bridge=bridge)
        window.append(SCOPE, WindowMessage(OWNER, "主人", "x", time.time() - 7 * 3600))
        log = Mock()
        monkeypatch.setattr(proactive_mod, "logger", log)
        for _ in range(5):
            assert await engine._floor_verdict(SCOPE, window.get_messages(SCOPE)) == ("", None)
        assert bridge.calls == 5
        assert any("paused_error" in c.args[0] for c in log.warning.call_args_list)
        # 熔断期间不再查询，仍是旧行为
        assert await engine._floor_verdict(SCOPE, window.get_messages(SCOPE)) == ("", None)
        assert bridge.calls == 5
        # 日志不含正文
        assert all("x" not in str(c.args[1:]) for c in log.warning.call_args_list)

    @pytest.mark.asyncio
    async def test_snapshot_cached(self, nm_config):
        bridge = FakeBridge()
        engine, _, window = _engine(nm_config, bridge=bridge)
        window.append(SCOPE, WindowMessage(OWNER, "主人", "x", time.time() - 7 * 3600))
        await engine._floor_verdict(SCOPE, window.get_messages(SCOPE))
        await engine._floor_verdict(SCOPE, window.get_messages(SCOPE))
        assert bridge.calls == 1

    @pytest.mark.asyncio
    async def test_quiet_hours_verdict_and_guard_and_schedule(self, nm_config, monkeypatch):
        engine, state, window = _engine(
            nm_config, bridge=FakeBridge(), hidden={"lifelike_proactive_quiet_hours": _all_day_quiet_hours()}
        )
        window.append(SCOPE, WindowMessage(OWNER, "主人", "x", time.time() - 7 * 3600))
        assert (await engine._floor_verdict(SCOPE, window.get_messages(SCOPE)))[0] == "quiet_hours"
        assert engine._initiate_guard_reason(SCOPE, time.time(), require_quiet=True) == "当前进入静音时段"
        monkeypatch.setattr(proactive_mod.random, "uniform", lambda a, b: 1800.0)
        now = time.time()
        when = engine._schedule_group(SCOPE, now=now)
        assert when >= now + 1800.0  # 推到静音结束之后，不补发

    def test_schedule_unchanged_when_quiet_hours_empty(self, nm_config, monkeypatch):
        engine, _, _ = _engine(nm_config, bridge=FakeBridge())
        monkeypatch.setattr(proactive_mod.random, "uniform", lambda a, b: a)
        now = time.time()
        assert engine._schedule_group(SCOPE, now=now) == pytest.approx(now + 300 * 0.6)


def test_module_exports():
    assert ag.REASON_QUIET_HOURS == "quiet_hours" and ag.REASON_INACTIVE_HOUR == "inactive_hour"


class TestDueReminderBypassesActivityGate:
    """审查 M2：主人明确要求的到点提醒不被活动窗口推迟；硬静音仍生效。"""

    def _window(self, window):
        window.append(SCOPE, WindowMessage(OWNER, "主人", "x", time.time() - 8 * 3600))

    @pytest.mark.asyncio
    async def test_due_remind_requested_skips_gate(self, nm_config, monkeypatch):
        bridge = FakeBridge(cands=[{"remind_requested": True, "not_before": time.time() - 60}])
        engine, _, window = _engine(nm_config, bridge=bridge)
        self._window(window)
        monkeypatch.setattr(proactive_mod, "evaluate_activity_gate", lambda *a, **k: "inactive_hour")
        assert await engine._floor_verdict(SCOPE, window.get_messages(SCOPE)) == ("", None)
        assert bridge.calls == 0  # 没去查作息

    @pytest.mark.parametrize("cands", [
        [],
        [{"remind_requested": False, "not_before": 1.0}],
        [{"remind_requested": True, "not_before": None}],  # 没有到点时间的不算"到期提醒"
    ])
    @pytest.mark.asyncio
    async def test_non_reminder_still_deferred(self, nm_config, monkeypatch, cands):
        engine, _, window = _engine(nm_config, bridge=FakeBridge(cands=cands))
        self._window(window)
        monkeypatch.setattr(proactive_mod, "evaluate_activity_gate", lambda *a, **k: "inactive_hour")
        assert (await engine._floor_verdict(SCOPE, window.get_messages(SCOPE)))[0] == "inactive_hour"

    @pytest.mark.asyncio
    async def test_candidate_error_falls_back_to_gate(self, nm_config, monkeypatch):
        engine, _, window = _engine(nm_config, bridge=FakeBridge(cand_error=RuntimeError("db")))
        self._window(window)
        monkeypatch.setattr(proactive_mod, "evaluate_activity_gate", lambda *a, **k: "inactive_hour")
        assert (await engine._floor_verdict(SCOPE, window.get_messages(SCOPE)))[0] == "inactive_hour"

    @pytest.mark.asyncio
    async def test_quiet_hours_still_apply_to_reminders(self, nm_config):
        bridge = FakeBridge(cands=[{"remind_requested": True, "not_before": time.time() - 60}])
        engine, _, window = _engine(
            nm_config, bridge=bridge, hidden={"lifelike_proactive_quiet_hours": _all_day_quiet_hours()}
        )
        self._window(window)
        assert (await engine._floor_verdict(SCOPE, window.get_messages(SCOPE)))[0] == "quiet_hours"
        assert bridge.cand_calls == 0

    @pytest.mark.asyncio
    async def test_gate_off_never_queries_candidates(self, nm_config):
        bridge = FakeBridge(cands=[{"remind_requested": True, "not_before": 1.0}])
        engine, _, window = _engine(nm_config, bridge=bridge, hidden={"lifelike_proactive_activity_gate": False})
        self._window(window)
        await engine._floor_verdict(SCOPE, window.get_messages(SCOPE))
        assert bridge.cand_calls == 0
