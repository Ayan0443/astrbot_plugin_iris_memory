"""``/iris_care`` 命令（设计 §7.9.6 第 4、7 条，§9.9）。只在主人私聊响应；回执不进 L1/出站账本/历史。

子命令：status | why [n] | on | off [purge] | mute <类别> | unmute <类别> | proactive [n]
回执里只有类别、状态、消息 id 与计数，不显示原文与概括。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, List

from . import rules as r

HELP = (
    "用法：/iris_care status | why [n] | on | off [purge] | mute <类别> | unmute <类别> | proactive [n]\n"
    "类别：tired（累）sick（身体）overtime（加班）mood_low（情绪）good_news（好事）"
)

_CAT_ALIASES = {
    "累": "tired", "困": "tired", "身体": "sick", "生病": "sick", "病": "sick", "加班": "overtime",
    "情绪": "mood_low", "心情": "mood_low", "好事": "good_news",
}


def parse_args(message: str) -> List[str]:
    parts = str(message or "").strip().split()
    if parts and parts[0].lstrip("/").lower() in ("iris_care", "iris-care"):
        parts = parts[1:]
    return parts


def _int(s: str, default: int) -> int:
    try:
        return max(1, min(20, int(s)))
    except (TypeError, ValueError):
        return default


async def handle(care: Any, coordinator: Any, scope: str, message: str, clock=time.time) -> str:
    if care is None:
        return "关心桥未启用。"
    args = parse_args(message)
    sub = args[0].lower() if args else "status"
    now = float(clock())
    if sub in ("status", "状态"):
        return await asyncio.to_thread(care.status_text, scope, now)
    if sub in ("why", "为什么"):
        n = _int(args[1], 5) if len(args) > 1 else 5
        return await asyncio.to_thread(care.why_text, n)
    if sub in ("off", "关", "关闭"):
        purge = len(args) > 1 and args[1].lower() == "purge"
        n = await asyncio.to_thread(care.set_off, scope, True, "cmd")
        extra = ""
        if purge:
            await asyncio.to_thread(care.cs.purge_text)
            extra = "，已清空已记录的概括"
        return f"关心桥已关闭（撤下待发 {n} 条{extra}）。之后不会再根据群消息关心你。"
    if sub in ("on", "开", "开启"):
        await asyncio.to_thread(care.set_off, scope, False, "cmd")
        return "关心桥已开启（仍受模式开关与每日/每周上限约束）。"
    if sub in ("mute", "unmute"):
        if len(args) < 2:
            return HELP
        cat = _CAT_ALIASES.get(args[1], args[1])
        if cat not in r.CATEGORIES:
            return HELP
        await asyncio.to_thread(care.mute, scope, cat, sub == "mute")
        return f"类别 {cat} 已{'静音' if sub == 'mute' else '取消静音'}。"
    if sub == "clear-hyperbole":
        n = await asyncio.to_thread(care.cs.clear_hyperbole)
        return f"已清空口头夸张表（{n} 条）。"
    if sub == "proactive":
        if coordinator is None:
            return "主动候选池未启用。"
        n = _int(args[1], 5) if len(args) > 1 else 5
        rows = await asyncio.to_thread(coordinator.recent_decisions, scope, n)
        if not rows:
            return "最近没有主动决策记录。"
        lines = []
        for d in rows:
            when = time.strftime("%m-%d %H:%M", time.localtime(float(d["ts"])))
            lines.append(
                f"- {when} mode={d.get('mode')} gate={d.get('gate_result')} decision={d.get('decision') or '-'}"
                f" type={d.get('chosen_ctype') or '-'} outcome={d.get('outcome') or '-'}"
                f" replied={'-' if d.get('replied') is None else d.get('replied')}"
            )
        return "\n".join(lines)
    return HELP
