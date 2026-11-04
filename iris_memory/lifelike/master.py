"""活人感 P1/P2 总开关 ``lifelike_enable``（ROADMAP §2.1）。

``lifelike_enable=false`` 时所有 P1/P2 ``lifelike_*`` 子模块视为 off，行为与 3ad413e 一致。
统一在配置读取入口（``Config.get``）上生效，各模块不必各自判断。

不受总开关控制：P0-1～P0-8 修复项开关、口吻风格层 ``style_*`` / ``learning_*``、
预算 ``lifelike_budget_*``、各模块的参数键（上限、间隔、群列表等，模块 off 时不会被读到）；
R1/R2 隐私收口 ``lifelike_private_l2_filter`` / ``lifelike_group_global_memory_guard``
（隐私修复，只受各自开关控制——关总开关做功能回滚时不能顺带撤掉隐私修复；
"全关 = 3ad413e" 需要把这两个键也单独设为 false）。
"""

from __future__ import annotations

from typing import Any, Callable

KEY_ENABLE = "lifelike_enable"

# 子模块开关 → 总开关关闭时的取值（= 3ad413e 行为）
GATED_OFF: dict[str, Any] = {
    # P1-5 生活系统
    "lifelike_world_mode": "off",
    # P1-6 / P1-11 关系
    "lifelike_relation_mode": "off",
    "lifelike_bond_mode": "off",
    # P1-7 用户认知 / P1-13 群友认知
    "lifelike_memory_mode": "off",
    "lifelike_memory_inference_inject": "off",
    "lifelike_member_mode": "off",
    "lifelike_dream_contradiction_archive": False,
    # P1-8 / P1-12 主动候选池与关心桥
    "lifelike_initiative_mode": "off",
    "lifelike_care_bridge_mode": "off",
    # U8 主人私聊自由主动
    "lifelike_proactive_freedom": False,
    # U9 白名单群自由搭话
    "lifelike_group_freedom": False,
    # 聊天欲望档位 / 被恶意对待时反击
    "lifelike_chat_drive": "off",
    "lifelike_counter_mode": "off",
    "lifelike_member_nickname": "off",
    # U19 表情包自动学习 / 使用意愿
    "lifelike_sticker_auto_learn": "off",
    "lifelike_sticker_use": "off",
    # U20 也学主人在群里的贴纸用法
    "lifelike_sticker_learn_groups": "off",
}

_FALSE = {"0", "false", "no", "off", ""}


def enabled(raw: Any) -> bool:
    """总开关取值解析；None / 无法解析 → True（HiddenConfig 默认值为 true）"""
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    return str(raw).strip().lower() not in _FALSE


def gate(key: str, value: Any, read_enable: Callable[[], Any]) -> Any:
    """``key`` 是受控子模块开关且总开关关闭 → 返回关闭取值；否则原样返回。

    读取总开关失败时不改变取值（不因为异常把模块打开或关闭）。
    """
    if key not in GATED_OFF:
        return value
    try:
        if enabled(read_enable()):
            return value
    except Exception:
        return value
    return GATED_OFF[key]


# 明确不受总开关控制的隐私修复键（测试与文档用）
UNGATED_PRIVACY: tuple[str, ...] = ("lifelike_private_l2_filter", "lifelike_group_global_memory_guard")

__all__ = ["KEY_ENABLE", "GATED_OFF", "UNGATED_PRIVACY", "enabled", "gate"]
