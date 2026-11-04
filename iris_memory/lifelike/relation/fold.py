"""近期基调折叠 + 可恢复反馈的模式规则（纯函数，设计 §5.2、§5.4）。

输入是 relationship_events / relationship_modes 的行（dict），输出文字阶段；
内部只按"最近、未修复、未过期"做规则判定，不产生任何对外可见的数值。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from ...continuity import models as cm
from . import models as rm

__all__ = [
    "active_events",
    "active_modes",
    "unrepaired_negatives",
    "fold_tone",
    "relative_when",
    "plan_mode",
    "releasable",
]

CLOSE_WINDOW_S = 7 * rm.DAY
MADE_UP_WINDOW_S = 24 * rm.HOUR


def active_events(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    out = [e for e in events or () if int(e.get("active", 1) or 0) and e.get("kind") != rm.K_UNDO]
    out.sort(key=lambda e: float(e.get("ts") or 0.0))
    return out


def active_modes(modes: Iterable[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    return [
        md
        for md in modes or ()
        if md.get("released_at") is None and float(md.get("expires_at") or 0.0) > now
    ]


def _repaired(neg: dict[str, Any], events: list[dict[str, Any]]) -> bool:
    nid, nts = neg.get("rel_event_id"), float(neg.get("ts") or 0.0)
    for e in events:
        if e.get("kind") != rm.K_REPAIR:
            continue
        if e.get("supersedes") == nid or float(e.get("ts") or 0.0) > nts:
            return True
    return False


def unrepaired_negatives(
    events: Iterable[dict[str, Any]], now: float, *, window_s: float | None = None, use_decay: bool = True
) -> list[dict[str, Any]]:
    evs = active_events(events)
    out = []
    for e in evs:
        if e.get("kind") not in rm.NEGATIVE_KINDS:
            continue
        ts = float(e.get("ts") or 0.0)
        if ts > now:
            continue
        if window_s is not None and now - ts > window_s:
            continue
        decay = e.get("decay_at")
        if use_decay and decay is not None and float(decay) <= now:
            continue
        if not _repaired(e, evs):
            out.append(e)
    return out


DISTRESS_SET_ASIDE_S = 24 * rm.HOUR


def _set_aside_by_distress(neg: dict[str, Any], events: list[dict[str, Any]]) -> bool:
    """主人难受/生病/危机（私聊或他本人在群里说）→ 之前的别扭放下；难受期间（其后 24h 内）的 hurt
    也不让语气变别扭（§5.4 自动解除第 1 条、§5.7.4"主人难受期间的 hurt 不计"）。只影响近期基调文字。"""
    nts = float(neg.get("ts") or 0.0)
    for e in events:
        if e.get("kind") != rm.K_DISTRESS:
            continue
        dts = float(e.get("ts") or 0.0)
        if dts >= nts or nts - dts <= DISTRESS_SET_ASIDE_S:
            return True
    return False


def relative_when(ts: float, now: float) -> str:
    """相对时间（文字，不含数字）。"""
    if now - ts < rm.HOUR:
        return "刚才"
    d_ts = datetime.fromtimestamp(ts, cm.TZ_SHANGHAI)
    d_now = datetime.fromtimestamp(now, cm.TZ_SHANGHAI)
    days = (d_now.date() - d_ts.date()).days
    if days <= 0:
        h = d_ts.hour
        part = "早上" if h < 9 else "上午" if h < 12 else "中午" if h < 14 else "下午" if h < 18 else "晚上"
        return f"今天{part}"
    if days == 1:
        return "昨天"
    if days == 2:
        return "前天"
    return "这几天"


def _reason(e: dict[str, Any], now: float) -> str:
    kind = str(e.get("kind") or "")
    if kind not in rm.TONE_REASON_KINDS:
        return ""
    summary = rm.SUMMARIES.get(kind, "")
    return f"{relative_when(float(e.get('ts') or now), now)}{summary}" if summary else ""


def fold_tone(
    events: Iterable[dict[str, Any]], modes: Iterable[dict[str, Any]], now: float
) -> rm.RelationView:
    """确定性规则：冷静一下 > 有点别扭 > 刚和好 > 最近挺亲近 > 平常。"""
    evs = [e for e in active_events(events) if float(e.get("ts") or 0.0) <= now]
    live = active_modes(modes, now)
    boundary = any(md.get("kind") == rm.MODE_BOUNDARY for md in live)
    view = rm.RelationView(boundary_active=boundary)

    if any(md.get("kind") == "proactive_pause" for md in live):  # P1 不会产生；为 P2-7 预留
        view.stage = rm.TONE_PAUSE
        view.effects = {"tone": "收一点", "share_life": "少", "proactive": "暂停"}
        return view

    negs = [e for e in unrepaired_negatives(evs, now) if not _set_aside_by_distress(e, evs)]
    soften = [md for md in live if md.get("kind") == rm.MODE_SOFTEN]
    if negs or soften:
        view.stage = rm.TONE_AWKWARD
        view.effects = {"tone": "收一点", "share_life": "少", "joke": "少"}
        src = negs[-1] if negs else None
        if src is None:
            cause = next(
                (e for e in reversed(evs) if e.get("rel_event_id") == soften[-1].get("cause_event_id")), None
            )
            src = cause
        if src is not None:
            r = _reason(src, now)
            if r:
                view.reasons = [r]
                view.reason_ts = float(src.get("ts") or now)
        return view

    repairs = [
        e
        for e in evs
        if e.get("kind") == rm.K_REPAIR and e.get("supersedes") and now - float(e.get("ts") or 0.0) < MADE_UP_WINDOW_S
    ]
    if repairs:
        view.stage = rm.TONE_MADE_UP
        view.repair_ts = float(repairs[-1].get("ts") or now)
        view.reasons = [_reason(repairs[-1], now)]
        view.effects = {"tone": "自然"}
        return view

    warm = [
        e
        for e in evs
        if e.get("kind") in (rm.K_WARM, rm.K_SHARED) and now - float(e.get("ts") or 0.0) <= CLOSE_WINDOW_S
    ]
    warm_days = {cm.local_day(float(e.get("ts") or 0.0)) for e in warm}
    if len(warm_days) >= 2:
        view.stage = rm.TONE_CLOSE
        r = _reason(warm[-1], now)
        view.reasons = [r] if r else []
        view.effects = {"share_life": "多一点", "joke": "多一点"}
        return view
    return view


# ---------------------------------------------------------------- 模式规则


def plan_mode(kind: str, levels: tuple[str, ...], live_modes: list[dict[str, Any]], now: float) -> dict[str, Any] | None:
    """新的负向事件 → 应创建/延长的模式（纯函数）。返回 {'op': 'create'|'extend', ...} 或 None。"""
    mode_kind = {rm.K_HURT: rm.MODE_SOFTEN, rm.K_BOUNDARY_CROSSED: rm.MODE_BOUNDARY}.get(kind)
    if mode_kind is None or mode_kind not in levels:
        return None
    lo, default, hi = rm.MODE_DURATION[mode_kind]
    existing = [md for md in live_modes if md.get("kind") == mode_kind]
    if existing:
        md = existing[-1]
        started = float(md.get("started_at") or now)
        new_exp = min(started + hi, now + default)
        if new_exp <= float(md.get("expires_at") or 0.0):
            return None
        return {"op": "extend", "mode_id": md.get("mode_id"), "expires_at": new_exp}
    return {
        "op": "create",
        "kind": mode_kind,
        "started_at": now,
        "min_until": now + lo,
        "expires_at": now + default,
    }


def releasable(reason: str, live_modes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """自动解除规则（§5.4）：难受/求助、"别生气了"、主人 reset → 全部；repair → 1、3 级。"""
    if reason in (rm.REL_OWNER_DISTRESS, rm.REL_OWNER_DISTRESS_GROUP, rm.REL_ASK, rm.REL_RESET):
        return list(live_modes)
    if reason == rm.REL_REPAIR:
        return [md for md in live_modes if md.get("kind") in (rm.MODE_SOFTEN, "proactive_pause")]
    return []
