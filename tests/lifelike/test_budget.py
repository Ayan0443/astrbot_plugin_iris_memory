"""CheapBudget：每日硬预算（调用数 / token），计数落 counters，跨天重置。"""

import asyncio
from datetime import datetime

import pytest
from astrbot_plugin_iris_memory.iris_memory.config.defaults import HiddenConfig
from astrbot_plugin_iris_memory.iris_memory.continuity import models as m
from astrbot_plugin_iris_memory.iris_memory.continuity.store import ContinuityStore
from astrbot_plugin_iris_memory.iris_memory.lifelike.budget import (
    C_CALLS,
    C_TOKENS,
    CheapBudget,
    CheapBudgetExceeded,
)

BASE = datetime(2026, 11, 3, 10, 0, tzinfo=m.TZ_SHANGHAI).timestamp()
MOD = "lifelike_test"


class Clock:
    def __init__(self, t=BASE):
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


def _budget(store, clock, **cfg):
    return CheapBudget(store, lambda k, d=None: cfg.get(k, d), clock)


def test_hidden_config_defaults():
    h = HiddenConfig()
    assert h.lifelike_session_extract_mode == "off"
    assert h.lifelike_budget_cheap_calls_daily == 80
    assert h.lifelike_budget_cheap_tokens_daily == 150000


def test_limits_default_and_coercion(store, clock):
    assert _budget(store, clock).limits() == (80, 150000)
    b = _budget(store, clock, lifelike_budget_cheap_calls_daily="5", lifelike_budget_cheap_tokens_daily=-3)
    assert b.limits() == (5, 0)
    b = _budget(store, clock, lifelike_budget_cheap_calls_daily="x", lifelike_budget_cheap_tokens_daily=True)
    assert b.limits() == (80, 150000)

    def boom(k, d=None):
        raise RuntimeError

    assert CheapBudget(store, boom, clock).limits() == (80, 150000)


def test_reserve_counts_and_denies_calls(store, clock):
    b = _budget(store, clock, lifelike_budget_cheap_calls_daily=2)
    b.reserve(MOD, 10)
    b.reserve(MOD, 10)
    with pytest.raises(CheapBudgetExceeded) as ei:
        b.reserve(MOD, 10)
    assert ei.value.reason == "calls"
    c = store.get_counters()
    assert c[C_CALLS] == 2 and c[f"{C_CALLS}:{MOD}"] == 2
    assert c["lifelike.budget.denied"] == 1
    # 跨天重置
    clock.t += 86400
    b.reserve(MOD, 10)
    assert b.usage()["calls"] == 1


def test_reserve_denies_tokens(store, clock):
    b = _budget(store, clock, lifelike_budget_cheap_tokens_daily=100)
    b.reserve(MOD, 60)
    b.charge(MOD, 60)
    with pytest.raises(CheapBudgetExceeded) as ei:
        b.reserve(MOD, 50)
    assert ei.value.reason == "tokens"
    assert store.get_counters()[f"{C_TOKENS}:{MOD}"] == 60


def test_zero_limit_blocks_everything(store, clock):
    b = _budget(store, clock, lifelike_budget_cheap_calls_daily=0)
    with pytest.raises(CheapBudgetExceeded):
        b.reserve(MOD, 1)


def test_reserve_store_failure_denies(store, clock):
    class Broken:
        def get_counters(self, day=None):
            raise OSError("disk")

    with pytest.raises(CheapBudgetExceeded) as ei:
        CheapBudget(Broken(), lambda k, d=None: d, clock).reserve(MOD, 1)
    assert ei.value.reason == "error"


@pytest.mark.asyncio
async def test_guarded_generate_uses_probe_delta(store, clock):
    b = _budget(store, clock)
    total = {"v": 1000}

    async def probe(module):
        assert module == MOD
        return total["v"]

    async def gen(prompt, timeout):
        total["v"] += 321
        return "ok"

    assert await b.guarded_generate(MOD, "x" * 50, gen, 10, usage_probe=probe) == "ok"
    assert b.usage()["tokens"] == 321 and b.usage()["calls"] == 1


@pytest.mark.asyncio
async def test_guarded_generate_estimates_and_charges_on_failure(store, clock):
    b = _budget(store, clock)

    async def gen(prompt, timeout):
        raise asyncio.TimeoutError

    with pytest.raises(asyncio.TimeoutError):
        await b.guarded_generate(MOD, "y" * 40, gen, 10)
    assert b.usage() == {"calls": 1, "tokens": 40, "calls_limit": 80, "tokens_limit": 150000}

    async def gen2(prompt, timeout):
        return "z" * 7

    async def bad_probe(module):
        raise RuntimeError

    await b.guarded_generate(MOD, "y" * 40, gen2, 10, usage_probe=bad_probe)
    assert b.usage()["tokens"] == 40 + 47


@pytest.mark.asyncio
async def test_guarded_generate_denied_does_not_call(store, clock):
    b = _budget(store, clock, lifelike_budget_cheap_tokens_daily=100)
    called = []

    async def gen(prompt, timeout):
        called.append(1)
        return ""

    with pytest.raises(CheapBudgetExceeded):
        await b.guarded_generate(MOD, "p" * 10, gen, 10)  # 10 + 400 输出预留 > 100
    assert not called and b.usage()["calls"] == 0
