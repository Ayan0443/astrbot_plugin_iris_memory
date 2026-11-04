"""P2U 集成（U2 关系联动 + U3 群友跨群 + U8 自由主动）off 回归。

① prompt：新开关取默认 / 关闭值、或总开关关闭时，主人私聊与群聊 prompt 与 E9 基准逐字节一致
   （沿用 test_p1_off_shadow_regression 的夹具与 P1 数据）。
② 闸门：``lifelike_proactive_freedom=False`` 且关系模式为空（relation off / shadow 时协调器传入的值）时，
   ``gate.check`` 与 73edc2c 冻结副本（_gate_73edc2c.py）对所有组合给出相同结果。
"""

from __future__ import annotations

import dataclasses
import itertools

import pytest

from astrbot_plugin_iris_memory.iris_memory.config.defaults import Defaults
from astrbot_plugin_iris_memory.iris_memory.lifelike import master
from astrbot_plugin_iris_memory.iris_memory.lifelike.initiative import gate as cur

from . import _gate_73edc2c as base
from . import test_p1_off_shadow_regression as p1

# P2U 新开关 / 新语义 → 关闭取值（= 73edc2c 行为）
P2U_OFF = {
    "lifelike_proactive_freedom": False,
    "lifelike_member_groups": "",
    "lifelike_member_require_owner_present": "auto",
    "lifelike_private_l2_groups": "",
}

# 总开关关闭时即便写着建议上线值也必须视为 off
P2U_ON_UNDER_MASTER_OFF = dict(
    p1.P1_ON_UNDER_MASTER_OFF,
    lifelike_proactive_freedom=True,
    lifelike_member_groups="*whitelist",
    lifelike_relation_levels="soften,boundary",
    lifelike_bond_start_stage="s1",
)


def _default(key):
    d = Defaults()
    for f in dataclasses.fields(d):
        sec = getattr(d, f.name)
        if dataclasses.is_dataclass(sec) and hasattr(sec, key):
            return getattr(sec, key)
    raise KeyError(key)


def test_new_switch_defaults_are_off():
    for key, val in P2U_OFF.items():
        assert _default(key) == val, key


def test_freedom_is_master_gated():
    assert master.gate("lifelike_proactive_freedom", True, lambda: False) is False
    assert master.gate("lifelike_proactive_freedom", True, lambda: True) is True


@pytest.mark.asyncio
async def test_all_p2u_off_matches_baseline(tmp_path, monkeypatch):
    got = await p1._render(tmp_path, monkeypatch, dict(p1.P1_OFF, lifelike_enable=True, **P2U_OFF))
    p1._assert_same_as_baseline(got, "all P1/P2U off")


@pytest.mark.asyncio
async def test_master_off_overrides_p2u_on(tmp_path, monkeypatch):
    got = await p1._render(tmp_path, monkeypatch, P2U_ON_UNDER_MASTER_OFF, gated=True)
    p1._assert_same_as_baseline(got, "lifelike_enable=false with P2U on")


@pytest.mark.asyncio
async def test_p2u_shadow_with_wildcard_groups_matches_baseline(tmp_path, monkeypatch):
    """群友认知 shadow + *whitelist：只建档不注入，prompt 不变"""
    got = await p1._render(
        tmp_path, monkeypatch, dict(p1.P1_SHADOW, **dict(P2U_OFF, lifelike_member_groups="*whitelist"))
    )
    p1._assert_same_as_baseline(got, "P1 shadow + member *whitelist")


@pytest.mark.asyncio
async def test_freedom_on_does_not_touch_reply_prompts(tmp_path, monkeypatch):
    """U8 只改主动调度与决策，不改被动回复的 prompt"""
    got = await p1._render(
        tmp_path, monkeypatch, dict(p1.P1_OFF, lifelike_enable=True, **dict(P2U_OFF, lifelike_proactive_freedom=True))
    )
    p1._assert_same_as_baseline(got, "freedom on, rest off")


def _grid():
    T0 = 1_000_000.0
    axes = dict(
        ctype=[cur.CTYPE_FOLLOWUP, cur.CTYPE_GROUP_CARE, "life_share"],
        scope_is_owner_private=[True, False],
        delivery=[cur.DELIVERY_PUSH, cur.DELIVERY_PASSIVE],
        crisis=[False, True],
        care_category=["", "sick"],
        busy_until=[None, T0 + 60],
        budget_exhausted=[False, True],
        dedupe_used=[False, True],
        crisis_sent_24h=[False, True],
        same_ctype_sent_24h=[False, True],
        ctype_cooldown_until=[None, T0 + 60],
        last_push_unreplied=[False, True],
        push_sent_24h=[0, 3],
        last_push_at=[None, T0 - 600],
        care_sent_today=[0, 1],
        care_category_cooldown_until=[None, T0 + 60],
    )
    keys = list(axes)
    for combo in itertools.product(*axes.values()):
        kw = dict(zip(keys, combo))
        kw["last_push_ctype"] = kw["ctype"] if kw["last_push_unreplied"] else ""
        yield T0, kw


def test_gate_freedom_off_equals_73edc2c():
    n = 0
    for now, kw in _grid():
        want = base.check(base.GateContext(now=now, relation_mode="", **kw))
        got = cur.check(cur.GateContext(now=now, relation_mode="", freedom=False, **kw))
        assert tuple(got) == tuple(want), kw
        n += 1
    assert n > 10000
