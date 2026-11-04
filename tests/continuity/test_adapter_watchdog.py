"""lifelike P0-2：适配器失联心跳告警（只写日志与本机 health 文件）。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from astrbot_plugin_iris_memory.iris_memory.continuity import adapter_watchdog as aw
from astrbot_plugin_iris_memory.iris_memory.continuity.adapter_watchdog import AdapterWatchdog

T0 = 1_793_000_000.0


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Client:
    def __init__(self, connected: bool = True) -> None:
        self.connected = connected

    def is_connected(self) -> bool:
        return self.connected


class Logs:
    def __init__(self):
        self.records: list[tuple[str, str]] = []

    def __getattr__(self, level):
        return lambda msg, *a, **k: self.records.append((level, str(msg)))

    def count(self, level: str) -> int:
        return sum(1 for lv, _ in self.records if lv == level)


@pytest.fixture
def logs(monkeypatch):
    lg = Logs()
    monkeypatch.setattr(aw, "logger", lg)
    return lg


def _wd(tmp_path, inst_ref, cfg=None, counters=None, clock=None):
    cfg = cfg if cfg is not None else {}
    counters = counters if counters is not None else []

    async def bump(name):
        counters.append(name)

    return AdapterWatchdog(
        lambda: inst_ref["inst"],
        lambda k, d: cfg.get(k, d),
        tmp_path / "health",
        bump,
        clock or Clock(),
    )


def _status(tmp_path):
    return json.loads((tmp_path / "health" / aw.STATUS_FILENAME).read_text())


@pytest.mark.asyncio
async def test_disconnect_alerts_exactly_once_after_threshold_then_recovers(tmp_path, logs):
    client = Client(True)
    ref = {"inst": SimpleNamespace(get_client=lambda: client)}
    clock = Clock()
    counters: list[str] = []
    wd = _wd(tmp_path, ref, {aw.THRESHOLD_KEY: 120}, counters, clock)

    assert await wd.check_once() == aw.STATE_OK
    assert _status(tmp_path)["state"] == "ok"
    assert (tmp_path / "health" / aw.LAST_OK_FILENAME).read_text().strip() == f"{T0:.3f}"

    client.connected = False
    clock.t += 60
    assert await wd.check_once() == aw.STATE_PENDING
    clock.t += 119
    assert await wd.check_once() == aw.STATE_PENDING
    assert logs.count("error") == 0
    assert _status(tmp_path)["state"] == "ok"  # 未到阈值不写 down

    clock.t += 1  # 断开满 120s
    assert await wd.check_once() == aw.STATE_DOWN
    for _ in range(5):
        clock.t += 60
        assert await wd.check_once() == aw.STATE_DOWN
    assert logs.count("error") == 1
    assert counters == ["adapter_down"]
    st = _status(tmp_path)
    assert st["state"] == "down" and st["reason"] == "disconnected"
    assert st["since"] == pytest.approx(T0 + 60)

    client.connected = True
    clock.t += 60
    assert await wd.check_once() == aw.STATE_OK
    assert _status(tmp_path)["state"] == "ok"
    assert any(lv == "info" and "recovered" in msg for lv, msg in logs.records)
    # 恢复后再断开会重新计时、再次告警一次
    client.connected = False
    clock.t += 1
    await wd.check_once()
    clock.t += 120
    await wd.check_once()
    assert logs.count("error") == 2


@pytest.mark.asyncio
async def test_brief_disconnect_below_threshold_never_alerts(tmp_path, logs):
    client = Client(True)
    ref = {"inst": SimpleNamespace(get_client=lambda: client)}
    clock = Clock()
    wd = _wd(tmp_path, ref, {}, None, clock)
    await wd.check_once()
    client.connected = False
    clock.t += 200
    await wd.check_once()
    client.connected = True
    clock.t += 60
    assert await wd.check_once() == aw.STATE_OK
    assert logs.count("error") == 0
    assert not any("recovered" in m for _, m in logs.records)


@pytest.mark.asyncio
async def test_adapter_fatal_snapshot_alerts_immediately(tmp_path, logs):
    inst = SimpleNamespace(
        get_client=lambda: None,
        health_snapshot=lambda: {"state": "fatal", "reason": "AuthKeyUnregisteredError", "last_inbound_at": T0 - 30},
    )
    wd = _wd(tmp_path, {"inst": inst})
    assert await wd.check_once() == aw.STATE_DOWN
    assert logs.count("error") == 1
    err = [m for lv, m in logs.records if lv == "error"][0]
    assert "adapter_fatal" in err and "AuthKeyUnregisteredError" in err and "last_inbound_age=30s" in err
    assert _status(tmp_path)["last_inbound_age_s"] == 30.0


@pytest.mark.parametrize(
    "inst,reason",
    [
        (None, "no_platform"),
        (SimpleNamespace(get_client=lambda: None), "no_client"),
        (SimpleNamespace(), "no_client"),
    ],
)
def test_probe_reasons(tmp_path, inst, reason):
    wd = _wd(tmp_path, {"inst": inst})
    up, why, _ = wd.probe()
    assert up is False and why == reason


def test_probe_exceptions_become_reason_ids(tmp_path):
    def boom():
        raise RuntimeError("secret text")

    wd = AdapterWatchdog(boom, lambda k, d: d, None)
    assert wd.probe()[:2] == (False, "platform_error:RuntimeError")

    inst = SimpleNamespace(get_client=boom)
    wd = _wd(tmp_path, {"inst": inst})
    assert wd.probe()[:2] == (False, "client_error:RuntimeError")

    inst = SimpleNamespace(get_client=lambda: SimpleNamespace(is_connected=boom))
    wd = _wd(tmp_path, {"inst": inst})
    assert wd.probe()[:2] == (False, "is_connected_error:RuntimeError")

    # health_snapshot 抛错 → 忽略快照，按 client 判断
    inst = SimpleNamespace(get_client=lambda: Client(True), health_snapshot=boom)
    wd = _wd(tmp_path, {"inst": inst})
    assert wd.probe()[:2] == (True, "ok")


@pytest.mark.asyncio
async def test_switch_off_no_probe_no_files(tmp_path, logs):
    calls = []

    def getter():
        calls.append(1)
        return None

    wd = AdapterWatchdog(getter, lambda k, d: False if k == aw.SWITCH_KEY else d, tmp_path / "health")
    assert wd.enabled() is False
    assert await wd.check_once() == aw.STATE_DISABLED
    assert calls == []
    assert not (tmp_path / "health").exists()
    assert logs.records == []


def test_config_parsing_and_floors(tmp_path):
    wd = _wd(tmp_path, {"inst": None}, {aw.THRESHOLD_KEY: "5", aw.PERIOD_KEY: 1, aw.SWITCH_KEY: "off"})
    assert wd.threshold_s() == aw.MIN_THRESHOLD_S
    assert wd.period_s() == aw.MIN_PERIOD_S
    assert wd.enabled() is False
    wd = _wd(tmp_path, {"inst": None}, {aw.THRESHOLD_KEY: "bad", aw.PERIOD_KEY: None, aw.SWITCH_KEY: "yes"})
    assert wd.threshold_s() == aw.DEFAULT_THRESHOLD_S
    assert wd.period_s() == aw.DEFAULT_PERIOD_S
    assert wd.enabled() is True

    def broken(k, d):
        raise RuntimeError

    wd = AdapterWatchdog(lambda: None, broken, None)
    assert wd.enabled() is True and wd.threshold_s() == aw.DEFAULT_THRESHOLD_S


@pytest.mark.asyncio
async def test_counter_and_file_failures_are_swallowed(tmp_path, logs):
    async def bad_bump(name):
        raise RuntimeError

    blocker = tmp_path / "file"
    blocker.write_text("x")
    wd = AdapterWatchdog(
        lambda: None, lambda k, d: d, blocker / "health", bad_bump, Clock()
    )
    wd._down_since = T0 - 1000
    assert await wd.check_once() == aw.STATE_DOWN
    assert logs.count("error") == 1
    assert logs.count("warning") >= 2  # counter + file


@pytest.mark.asyncio
async def test_loop_start_stop(tmp_path, logs):
    import asyncio

    ticks = []

    async def fake_sleep(s):
        ticks.append(s)
        await asyncio.sleep(0)

    wd = AdapterWatchdog(
        lambda: SimpleNamespace(get_client=lambda: Client(True)),
        lambda k, d: d,
        tmp_path / "health",
        sleep=fake_sleep,
    )
    wd.start()
    wd.start()  # 幂等
    for _ in range(20):
        await asyncio.sleep(0)
    assert wd.running
    await wd.stop()
    assert not wd.running
    assert ticks and ticks[0] == float(aw.DEFAULT_PERIOD_S)
    assert wd.state == aw.STATE_OK


def test_no_message_content_in_status_file(tmp_path):
    # 状态文件只含固定键
    import asyncio

    wd = _wd(tmp_path, {"inst": SimpleNamespace(get_client=lambda: Client(True))})
    asyncio.run(wd.check_once())
    assert set(_status(tmp_path)) == {"state", "since", "checked_at", "reason", "last_inbound_age_s"}


@pytest.mark.asyncio
async def test_adapter_stopped_is_info_not_error(tmp_path, logs):
    """审查 L4：主人主动停用适配器（自报 stopped）→ INFO 一次 + health=stopped，不报 ERROR。"""
    inst = SimpleNamespace(get_client=lambda: None, health_snapshot=lambda: {"state": "stopped"})
    ref = {"inst": inst}
    clock = Clock()
    counters: list[str] = []
    wd = _wd(tmp_path, ref, {}, counters, clock)
    for _ in range(10):
        assert await wd.check_once() == aw.STATE_STOPPED
        clock.t += 600
    assert logs.count("error") == 0 and counters == []
    assert sum(1 for lv, m in logs.records if lv == "info" and "stopped" in m) == 1
    assert _status(tmp_path)["state"] == "stopped"


@pytest.mark.asyncio
async def test_ok_writes_throttled(tmp_path, logs, monkeypatch):
    """在线时 status 只在状态变化时写；last_ok 至少隔 LAST_OK_MIN_INTERVAL_S 刷新。"""
    client = Client(True)
    ref = {"inst": SimpleNamespace(get_client=lambda: client)}
    clock = Clock()
    wd = _wd(tmp_path, ref, {}, None, clock)
    writes: list[str] = []
    real = wd._atomic_write

    def spy(name, text):
        writes.append(name)
        real(name, text)

    monkeypatch.setattr(wd, "_atomic_write", spy)
    await wd.check_once()
    assert writes == [aw.STATUS_FILENAME, aw.LAST_OK_FILENAME]
    for _ in range(4):
        clock.t += 60
        await wd.check_once()
    assert writes == [aw.STATUS_FILENAME, aw.LAST_OK_FILENAME]
    clock.t += 60  # 距上次 last_ok 满 300s
    await wd.check_once()
    assert writes == [aw.STATUS_FILENAME, aw.LAST_OK_FILENAME, aw.LAST_OK_FILENAME]
