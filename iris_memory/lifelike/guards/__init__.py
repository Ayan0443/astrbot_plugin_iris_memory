"""活人感 - 注入文本守卫（纯函数 + 开关感知包装）。"""

from .numeric import (
    drop_count,
    emotion_hint,
    filter_lines,
    guard_enabled,
    guard_text,
    scan,
)

__all__ = [
    "drop_count",
    "emotion_hint",
    "filter_lines",
    "guard_enabled",
    "guard_text",
    "scan",
]
