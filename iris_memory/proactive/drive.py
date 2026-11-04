"""聊天欲望档位（``lifelike_chat_drive``：off / low / medium / high）。

off（默认）= 3c6e6e6 的决策措辞与节奏；其余档位只在 U8（主人私聊自由主动）/ U9（白名单群自由搭话）
打开时生效，改的是：决策措辞里的"倾向"（只以文字进 prompt，不出现数字分值、不加概率强制）、
评估节奏（取配置值与档位值中较快的一个）、她自己请求冷却 / 暂停的上限，以及"决定开口时 drifted /
cooldown 不再把开口改成不说"。去重、硬底线、熔断、叫停、MAN 拦截不随档位变化。
"""

from __future__ import annotations

from typing import Any

KEY_CHAT_DRIVE = "lifelike_chat_drive"
DRIVES = ("off", "low", "medium", "high")

# 私聊自由主动评估间隔（分钟）；off = 用配置值（默认 20）
_PRIVATE_EVAL = {"low": 18, "medium": 15, "high": 12}
# 私聊她自己请求的冷却上限（分钟）；off = 不额外限制（解析上限 120）
_PRIVATE_COOLDOWN_CAP = {"low": 60, "medium": 30, "high": 15}
# 群内开话题：群安静多久后评估 / 评估节奏（分钟）；off = 用配置值（默认 45 / 45）
_GROUP_QUIET = {"low": 40, "medium": 30, "high": 18}
_GROUP_EVAL = {"low": 40, "medium": 30, "high": 20}
# 群聊她自己请求暂停的上限（分钟）
_GROUP_PAUSE_CAP = {"off": 15, "low": 10, "medium": 5, "high": 3}


def normalize(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    alias = {"strong": "high", "强烈": "high", "高": "high", "中": "medium", "中等": "medium", "低": "low", "关": "off"}
    s = alias.get(s, s)
    return s if s in DRIVES else "off"


def active(drive: str) -> bool:
    return normalize(drive) != "off"


def private_eval_minutes(drive: str, configured: int) -> int:
    v = _PRIVATE_EVAL.get(normalize(drive))
    return min(int(configured), v) if v else int(configured)


def private_cooldown_cap(drive: str) -> int | None:
    return _PRIVATE_COOLDOWN_CAP.get(normalize(drive))


def group_quiet_minutes(drive: str, configured: int) -> int:
    v = _GROUP_QUIET.get(normalize(drive))
    return min(int(configured), v) if v else int(configured)


def group_eval_minutes(drive: str, configured: int) -> int:
    v = _GROUP_EVAL.get(normalize(drive))
    return min(int(configured), v) if v else int(configured)


def group_pause_cap(drive: str) -> int:
    return _GROUP_PAUSE_CAP[normalize(drive)]
