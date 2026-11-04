"""P1 集成验收 ④：P1 开关关闭 / 总开关关闭 / shadow 时，prompt 与 3ad413e 逐字节一致。

复用 E9 夹具（真实 ``on_llm_request`` + 真实 ContinuityService），基准仍是 E9 快照
（44c2f26 生成；3ad413e 在 P0 全关口径下与之逐字节一致，见 test_p0_off_regression）。

为了让"一致"有意义，每个用例都先往 continuity.db 里塞好 P1 数据（用户认知），
并做变异检查：把某个模块切到 on，快照必须漂移。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrbot_plugin_iris_memory.iris_memory.lifelike import master
from astrbot_plugin_iris_memory.iris_memory.lifelike.memory.beliefs import BeliefStore

from . import test_e9_prompt_snapshot as e9
from .test_p0_off_regression import ALL_P0_OFF

E9_SNAPSHOT = Path(__file__).resolve().parent / "snapshots" / "e9_prompt_snapshot.json"

# P1 子模块开关 → 关闭取值（= 3ad413e 行为）
P1_OFF = {
    "lifelike_world_mode": "off",
    "lifelike_world_claim_audit": False,
    "lifelike_world_plan_llm": False,
    "lifelike_relation_mode": "off",
    "lifelike_bond_mode": "off",
    "lifelike_memory_mode": "off",
    "lifelike_memory_inference_inject": "off",
    "lifelike_member_mode": "off",
    "lifelike_dream_contradiction_archive": False,
    "lifelike_private_l2_filter": False,
    "lifelike_group_global_memory_guard": False,
    "lifelike_initiative_mode": "off",
    "lifelike_care_bridge_mode": "off",
    "lifelike_care_bridge_push": False,
    "lifelike_care_llm": False,
}

# 所有 off/shadow/on 三态的 P1 模块切到 shadow；没有 shadow 档的布尔开关保持关闭
P1_SHADOW = dict(
    P1_OFF,
    lifelike_enable=True,
    lifelike_world_mode="shadow",
    lifelike_world_claim_audit=True,
    lifelike_relation_mode="shadow",
    lifelike_bond_mode="shadow",
    lifelike_memory_mode="shadow",
    lifelike_memory_inference_inject="hedged",
    lifelike_member_mode="shadow",
    lifelike_initiative_mode="shadow",
    lifelike_care_bridge_mode="shadow",
)

# 总开关关闭时各子模块即便写着 on 也必须视为 off
P1_ON_UNDER_MASTER_OFF = {
    "lifelike_enable": False,
    "lifelike_world_mode": "on",
    "lifelike_relation_mode": "on",
    "lifelike_bond_mode": "on",
    "lifelike_memory_mode": "on",
    "lifelike_memory_inference_inject": "hedged",
    "lifelike_member_mode": "on",
    "lifelike_dream_contradiction_archive": True,
    # R1/R2 隐私修复不受总开关控制：全关口径需单独设为 false
    "lifelike_private_l2_filter": False,
    "lifelike_group_global_memory_guard": False,
    "lifelike_initiative_mode": "on",
    "lifelike_care_bridge_mode": "on",
}


def _gated_get(self, key, default=None):
    """与 Config.get 相同的总开关语义（FakeConfig 没有经过 Config.get）"""
    val = self.values.get(key, default)
    return master.gate(key, val, lambda: self.values.get(master.KEY_ENABLE))


async def _render(tmp_path, monkeypatch, switches: dict, *, gated: bool = False) -> dict:
    merged = dict(ALL_P0_OFF)
    merged.update(switches)
    monkeypatch.setattr(e9, "P0_BASELINE_SWITCHES", merged)
    monkeypatch.setattr(e9, "CONT_SWITCHES", merged)
    if gated:
        monkeypatch.setattr(e9.FakeConfig, "get", _gated_get)
    orig_seed = e9._seed

    def _seed(svc):
        orig_seed(svc)
        # P1 数据：一条主人亲口说的偏好（memory on 时会注入）
        BeliefStore(svc.store, clock=lambda: e9.FIXED_NOW).add(
            e9.SCOPE, "preference", "他不吃香菜", "香菜", "stated", f"{e9.SCOPE}:1"
        )

    monkeypatch.setattr(e9, "_seed", _seed)
    return await e9._render_all(tmp_path, monkeypatch)


def _assert_same_as_baseline(got: dict, label: str) -> None:
    want = json.loads(E9_SNAPSHOT.read_text(encoding="utf-8"))
    assert sorted(got) == sorted(want)
    for cid in sorted(want):
        assert json.dumps(got[cid], ensure_ascii=False, sort_keys=True) == json.dumps(
            want[cid], ensure_ascii=False, sort_keys=True
        ), f"prompt drift ({label}): {cid}"


def _drifted(got: dict) -> list[str]:
    want = json.loads(E9_SNAPSHOT.read_text(encoding="utf-8"))
    return [
        cid
        for cid in sorted(want)
        if json.dumps(got.get(cid), ensure_ascii=False, sort_keys=True)
        != json.dumps(want[cid], ensure_ascii=False, sort_keys=True)
    ]


@pytest.mark.asyncio
async def test_all_p1_off_matches_3ad413e(tmp_path, monkeypatch):
    got = await _render(tmp_path, monkeypatch, dict(P1_OFF, lifelike_enable=True))
    _assert_same_as_baseline(got, "all P1 off")


@pytest.mark.asyncio
async def test_master_switch_off_overrides_p1_on(tmp_path, monkeypatch):
    got = await _render(tmp_path, monkeypatch, P1_ON_UNDER_MASTER_OFF, gated=True)
    _assert_same_as_baseline(got, "lifelike_enable=false")


@pytest.mark.asyncio
async def test_all_p1_shadow_matches_3ad413e(tmp_path, monkeypatch):
    got = await _render(tmp_path, monkeypatch, P1_SHADOW)
    _assert_same_as_baseline(got, "all P1 shadow")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,needle",
    [
        ("lifelike_relation_mode", "你们之间"),
        ("lifelike_memory_mode", "不吃香菜"),
    ],
)
async def test_mutation_on_drifts(tmp_path, monkeypatch, key, needle):
    """变异检查：同样的数据，某个模块切到 on，主人私聊的 prompt 必须变化（说明上面的一致不是空测）。"""
    got = await _render(tmp_path, monkeypatch, dict(P1_SHADOW, **{key: "on"}))
    drift = _drifted(got)
    assert drift, f"{key}=on produced no drift"
    joined = "\n".join("\n".join(got[c]["parts"]) for c in drift)
    assert needle in joined
    # 群聊用例不受主人私聊注入影响
    assert "E9-07" not in drift
