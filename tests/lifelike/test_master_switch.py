"""ROADMAP §2.1 总开关 lifelike_enable：false → P1/P2 lifelike_* 子模块开关一律取关闭值；
P0 修复项、style_* / learning_*、参数键不受影响。统一在 Config.get 生效。"""

from pathlib import Path

import pytest

from astrbot_plugin_iris_memory.iris_memory.config import Config
from astrbot_plugin_iris_memory.iris_memory.config.defaults import Defaults, HiddenConfig
from astrbot_plugin_iris_memory.iris_memory.config.hidden_config import HiddenConfigManager
from astrbot_plugin_iris_memory.iris_memory.lifelike import master

P1_ON = {
    "lifelike_world_mode": "on",
    "lifelike_relation_mode": "on",
    "lifelike_bond_mode": "on",
    "lifelike_memory_mode": "on",
    "lifelike_memory_inference_inject": "hedged",
    "lifelike_member_mode": "on",
    "lifelike_dream_contradiction_archive": True,
    "lifelike_initiative_mode": "on",
    "lifelike_care_bridge_mode": "on",
    "lifelike_proactive_freedom": True,
    "lifelike_group_freedom": True,
    "lifelike_chat_drive": "medium",
    "lifelike_counter_mode": "on",
    "lifelike_member_nickname": "on",
    "lifelike_sticker_auto_learn": "on",
    "lifelike_sticker_use": "on",
    "lifelike_sticker_learn_groups": "on",
}


def _cfg(tmp_path: Path, **hidden) -> Config:
    hm = HiddenConfigManager(tmp_path / "hidden_config.json", HiddenConfig())
    c = Config({}, hm, Defaults(), tmp_path)
    for k, v in hidden.items():
        c.set_hidden(k, v)
    return c


def test_gated_keys_exist_in_hidden_config():
    names = set(HiddenConfig.__dataclass_fields__)
    assert master.KEY_ENABLE in names
    assert set(master.GATED_OFF) <= names
    assert set(P1_ON) == set(master.GATED_OFF)


def test_privacy_fixes_not_gated_by_master(tmp_path):
    """R1/R2 是隐私修复：关总开关做功能回滚时不能顺带撤掉"""
    assert set(master.UNGATED_PRIVACY).isdisjoint(master.GATED_OFF)
    c = _cfg(
        tmp_path,
        lifelike_enable=False,
        lifelike_private_l2_filter=True,
        lifelike_group_global_memory_guard=True,
    )
    assert c.get("lifelike_private_l2_filter") is True
    assert c.get("lifelike_group_global_memory_guard") is True
    c.set_hidden("lifelike_private_l2_filter", False)
    assert c.get("lifelike_private_l2_filter") is False


def test_enable_true_passes_values_through(tmp_path):
    c = _cfg(tmp_path, lifelike_enable=True, **P1_ON)
    for k, v in P1_ON.items():
        assert c.get(k) == v


def test_enable_default_is_true(tmp_path):
    c = _cfg(tmp_path)
    assert c.get("lifelike_enable") is True
    # 默认值原样返回（不被总开关改写）
    assert c.get("lifelike_relation_mode") == HiddenConfig().lifelike_relation_mode


def test_enable_false_forces_off(tmp_path):
    c = _cfg(tmp_path, lifelike_enable=False, **P1_ON)
    for k in P1_ON:
        assert c.get(k) == master.GATED_OFF[k], k
        assert c.get(k, "on") == master.GATED_OFF[k]


@pytest.mark.parametrize(
    "key",
    [
        "lifelike_numeric_guard",
        "lifelike_session_extract_mode",
        "lifelike_style_guard_mode",
        "lifelike_strip_bypass_tools",
        "style_anime_register",
        "learning_pattern_rotation",
        "lifelike_budget_cheap_calls_daily",
        "lifelike_push_daily_cap",
        "lifelike_member_groups",
    ],
)
def test_enable_false_leaves_p0_style_and_params(tmp_path, key):
    on = _cfg(tmp_path / "a", lifelike_enable=True)
    off = _cfg(tmp_path / "b", lifelike_enable=False)
    assert on.get(key) == off.get(key)


def test_enable_string_values(tmp_path):
    assert master.enabled(None) is True
    assert master.enabled("false") is False
    assert master.enabled("0") is False
    assert master.enabled("off") is False
    assert master.enabled("true") is True
    assert master.enabled(0) is False


def test_gate_read_failure_keeps_value():
    def boom():
        raise RuntimeError("x")

    assert master.gate("lifelike_relation_mode", "on", boom) == "on"
    assert master.gate("other_key", "v", lambda: False) == "v"
