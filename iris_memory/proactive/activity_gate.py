"""
Iris Chat Memory - 主动安全底线（活人感 P0-8）

三项能力，各自一个 HiddenConfig 平铺开关，全部只作用于定时主动发起（initiate）；
被动回复、群聊接话不受影响：

1. 硬静音时段 ``lifelike_proactive_quiet_hours``（``HH:MM-HH:MM``，可跨午夜）。
   默认空 = 沿用现网（reply_mute_*；现网为 0:00–0:00 即不静音）。主人 11-02 明确
   表示"无静音时段"（DECISIONS D4），因此默认不设。
2. 活动窗口判定 ``lifelike_proactive_activity_gate``（默认 true，D4）：
   当前小时在主人历史作息里"几乎不活跃"，**且**最近 ``lifelike_activity_recent_hours``
   小时内没有互动 → 推迟本次私聊主动。不是硬静音：主人深夜还在聊时照常。
   "几乎不活跃"= 近 ``lookback_days`` 天里，主人在该小时有消息的天数 / 主人有消息的
   总天数 < ``inactive_ratio``；活跃日不足 ``min_days`` 时用先验时段
   ``lifelike_activity_prior_inactive``（默认 01:00–08:00，空 = 数据不足时不推迟）。
3. 窗口为空时的互动判定 ``lifelike_proactive_empty_window``（默认 false，首发保守）：
   重启后滑动窗口只恢复 24h 内消息，主人 24h 未说话时窗口为空、现网就不再调度。
   开启后改用水位（watermarks.last_msg_ts）+ 出站账本（outbound 已发送的回复）+
   状态里持久化的 last_human_message_at 求最后互动时间，继续按原有风险率调度。

这里只放纯函数（可注入时钟/本地时间函数，便于假时钟测试）；读库在
``ProactiveBridge.activity_snapshot`` 里完成，调度接线在 ``ProactiveEngine``。
作息统计只用时间戳，不读正文；日志前缀 ``[lifelike.proactive]``，不含正文。
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ActivityGateConfig",
    "ActivitySnapshot",
    "REASON_QUIET_HOURS",
    "REASON_INACTIVE_HOUR",
    "parse_hhmm_range",
    "in_minute_range",
    "seconds_until_range_end",
    "hourly_active_days",
    "is_inactive_hour",
    "evaluate_activity_gate",
]

REASON_QUIET_HOURS = "quiet_hours"
REASON_INACTIVE_HOUR = "inactive_hour"

_RANGE_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*[-~–—]\s*(\d{1,2}):(\d{2})\s*$")

LocalTimeFn = Callable[[float], time.struct_time]


def parse_hhmm_range(text: Any) -> tuple[int, int] | None:
    """``"HH:MM-HH:MM"`` → (起始分钟, 结束分钟)；空、格式错误、起止相同 → None（不生效）。"""
    if not isinstance(text, str) or not text.strip():
        return None
    m = _RANGE_RE.match(text)
    if not m:
        return None
    sh, sm, eh, em = (int(x) for x in m.groups())
    if not (0 <= sh <= 23 and 0 <= eh <= 23 and 0 <= sm <= 59 and 0 <= em <= 59):
        return None
    start, end = sh * 60 + sm, eh * 60 + em
    if start == end:
        return None
    return start, end


def in_minute_range(minute_of_day: int, rng: tuple[int, int] | None) -> bool:
    """半开区间 [start, end)，start > end 表示跨午夜。"""
    if rng is None:
        return False
    start, end = rng
    if start < end:
        return start <= minute_of_day < end
    return minute_of_day >= start or minute_of_day < end


def seconds_until_range_end(
    now: float, rng: tuple[int, int] | None, localtime: LocalTimeFn = time.localtime
) -> float:
    """当前处于区间内时返回距区间结束的秒数（按分钟粒度，含当前分钟剩余秒数），否则 0。"""
    if rng is None:
        return 0.0
    t = localtime(now)
    cur = t.tm_hour * 60 + t.tm_min
    if not in_minute_range(cur, rng):
        return 0.0
    end = rng[1]
    minutes = (end - cur) % 1440
    return max(0.0, minutes * 60.0 - t.tm_sec)


def hourly_active_days(
    timestamps: Iterable[float],
    *,
    now: float,
    lookback_days: int,
    localtime: LocalTimeFn = time.localtime,
) -> tuple[list[int], int]:
    """近 lookback_days 天内：每个小时（0–23）主人有消息的天数，以及有消息的总天数。"""
    cutoff = now - max(1, int(lookback_days)) * 86400.0
    per_hour: list[set[tuple[int, int]]] = [set() for _ in range(24)]
    days: set[tuple[int, int]] = set()
    for raw in timestamps:
        try:
            ts = float(raw)
        except (TypeError, ValueError):
            continue
        if ts < cutoff or ts > now + 60.0:
            continue
        t = localtime(ts)
        day = (t.tm_year, t.tm_yday)
        per_hour[t.tm_hour].add(day)
        days.add(day)
    return [len(s) for s in per_hour], len(days)


def is_inactive_hour(
    hour_days: list[int],
    total_days: int,
    *,
    hour: int,
    minute_of_day: int,
    min_days: int,
    inactive_ratio: float,
    prior: tuple[int, int] | None,
) -> bool:
    """当前小时是否"历史上几乎不活跃"。数据不足时退回先验时段（prior 为 None → False）。"""
    if total_days >= max(1, int(min_days)):
        share = hour_days[hour % 24] / float(total_days)
        return share < max(0.0, float(inactive_ratio))
    return in_minute_range(minute_of_day, prior)


@dataclass(frozen=True)
class ActivityGateConfig:
    quiet_hours: tuple[int, int] | None = None
    activity_gate: bool = False
    recent_hours: float = 6.0
    lookback_days: int = 28
    min_days: int = 7
    inactive_ratio: float = 0.1
    prior_inactive: tuple[int, int] | None = None
    empty_window: bool = False


@dataclass
class ActivitySnapshot:
    """读库结果：主人消息时间戳（作息统计用）与最后一次互动时间。"""

    owner_timestamps: list[float] = field(default_factory=list)
    last_interaction: float | None = None


def evaluate_activity_gate(
    cfg: ActivityGateConfig,
    *,
    now: float,
    last_interaction: float | None,
    owner_timestamps: Iterable[float],
    localtime: LocalTimeFn = time.localtime,
) -> str:
    """活动窗口判定：返回推迟原因（``inactive_hour``）或 ""（不推迟）。

    最近 recent_hours 内有互动 → 不推迟（他此刻大概率醒着）；否则当前小时历史上
    几乎不活跃 → 推迟。last_interaction 未知按"最近没有互动"处理。
    """
    if not cfg.activity_gate:
        return ""
    recent_s = max(0.0, float(cfg.recent_hours)) * 3600.0
    if last_interaction is not None and now - float(last_interaction) < recent_s:
        return ""
    hour_days, total_days = hourly_active_days(
        owner_timestamps, now=now, lookback_days=cfg.lookback_days, localtime=localtime
    )
    t = localtime(now)
    inactive = is_inactive_hour(
        hour_days,
        total_days,
        hour=t.tm_hour,
        minute_of_day=t.tm_hour * 60 + t.tm_min,
        min_days=cfg.min_days,
        inactive_ratio=cfg.inactive_ratio,
        prior=cfg.prior_inactive,
    )
    return REASON_INACTIVE_HOUR if inactive else ""
