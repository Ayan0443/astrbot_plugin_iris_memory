"""U9 批次（群聊自由搭话 / 聊天欲望档位 / 反击 / 群友称呼 / 表情包统一 / 口吻统一 / 随性标点）off 回归。

① 新开关默认值 = 3c6e6e6 行为；受总开关控制的键在 lifelike_enable=false 时取关闭值；
② 3c6e6e6 的提示词常量逐字节不变（tests/lifelike_eval/snapshots/prompts_3c6e6e6.json 冻结自 3c6e6e6）；
③ 新开关全关、或总开关关闭但写着建议上线值时，被动回复 prompt 与 E9 基准逐字节一致；
④ 开关打开时的新措辞写入快照 prompts_u9_on.json（改措辞需同步更新快照并在报告里说明）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrbot_plugin_iris_memory.iris_memory.continuity.service import ContinuityConfig
from astrbot_plugin_iris_memory.iris_memory.lifelike import master
from astrbot_plugin_iris_memory.iris_memory.lifelike.members import nickname as nk
from astrbot_plugin_iris_memory.iris_memory.proactive import prompts as P

from . import test_p1_off_shadow_regression as p1
from .test_p2u_off_regression import P2U_OFF, P2U_ON_UNDER_MASTER_OFF, _default

SNAP_DIR = Path(__file__).resolve().parent / "snapshots"

U9_OFF = {
    "lifelike_group_freedom": False,
    "lifelike_chat_drive": "off",
    "lifelike_counter_mode": "off",
    "lifelike_member_nickname": "off",
    "style_group_unified": False,
    "style_casual_punctuation": False,
}
U9_DEPLOY = {
    "lifelike_group_freedom": True,
    "lifelike_chat_drive": "medium",
    "lifelike_counter_mode": "on",
    "lifelike_member_nickname": "on",
}


def test_new_switch_defaults_are_off():
    for key, val in U9_OFF.items():
        assert _default(key) == val, key
    assert ContinuityConfig().sticker_scope_unified is False


@pytest.mark.parametrize("key", list(U9_DEPLOY))
def test_master_gated(key):
    assert master.gate(key, U9_DEPLOY[key], lambda: False) == master.GATED_OFF[key]
    assert master.gate(key, U9_DEPLOY[key], lambda: True) == U9_DEPLOY[key]


def test_3c6e6e6_prompt_constants_unchanged():
    snap = json.loads((SNAP_DIR / "prompts_3c6e6e6.json").read_text(encoding="utf-8"))
    for key, want in snap.items():
        got = getattr(P, key)
        if isinstance(got, tuple):
            got = list(got)
        assert got == want, key


@pytest.mark.asyncio
async def test_all_off_matches_baseline(tmp_path, monkeypatch):
    got = await p1._render(tmp_path, monkeypatch, dict(p1.P1_OFF, lifelike_enable=True, **P2U_OFF, **U9_OFF))
    p1._assert_same_as_baseline(got, "all P1/P2U/U9 off")


@pytest.mark.asyncio
async def test_master_off_overrides_u9_on(tmp_path, monkeypatch):
    got = await p1._render(tmp_path, monkeypatch, dict(P2U_ON_UNDER_MASTER_OFF, **U9_DEPLOY), gated=True)
    p1._assert_same_as_baseline(got, "lifelike_enable=false with U9 on")


def _on_texts() -> dict:
    out = {}
    for d in ("low", "medium", "high"):
        out[f"private_free_system[{d}]"] = P.private_free_system(d)
        out[f"private_free_instruction[{d}]"] = P.private_free_instruction(d, 30 if d == "medium" else None)
        out[f"group_free_system[{d}]"] = P.group_free_system(d)
    out["group_free_system[off]"] = P.group_free_system("off")
    for k in ("GROUP_CHIME_INSTRUCTION_FREE", "GROUP_INITIATE_INSTRUCTION_FREE", "GROUP_HOSTILE_INSTRUCTION",
              "COUNTER_SPEAK_HINT", "COUNTER_DEFEND_LINE", "COUNTER_LAST_ROUND_LINE", "OWNER_HARSH_HINT",
              "COUNTER_NEUTRAL_HINT", "GF_NEUTRAL_PROMPT"):
        out[k] = getattr(P, k)
    out["NICKNAME_NOTE_GROUP"] = nk.NICKNAME_NOTE_GROUP
    out["NICKNAME_NOTE_PRIVATE"] = nk.NICKNAME_NOTE_PRIVATE
    return out


def test_on_wording_snapshot():
    want = json.loads((SNAP_DIR / "prompts_u9_on.json").read_text(encoding="utf-8"))
    assert _on_texts() == want
