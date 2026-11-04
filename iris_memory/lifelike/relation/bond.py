"""BondFolder 的纯函数部分：关系渐进阶段的折叠（设计 §5.7.3、§5.7.4）。

- 阶段只由 relationship_events 折叠；每次升降返回一条带非空 ``cause_refs``（关系事件 id）的
  ``BondTransition``，没有原因事件就没有阶段变化。
- 一次至多升/降一级；门槛只在本文件常量里（不进 prompt、不进命令回执、模型不可见）。
- 回退只有三种：伤害未修复（D1）、主人要求（由服务层在收到消息/命令时直接写）、误判撤销（U1）。
  长期不聊、没回主动、宣泄、和群友聊得多都**不**回退。
- shadow 轨：shadow 期的 proposed 行构成一条假想轨迹，便于观察多周轨迹；正式轨只认 applied。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ...continuity import models as cm
from . import models as rm
from .fold import active_events, unrepaired_negatives

__all__ = ["BondState", "bond_state", "evaluate", "dedup_positive", "care_stage", "PROMOTE_RULES"]

WEEK = 7 * rm.DAY

# 晋升门槛（初值，设计 §5.7.3）。只在代码里。
MIN_STAY = {"s1": 21 * rm.DAY, "s2": 45 * rm.DAY, "s3": 90 * rm.DAY}
PROMOTE_RULES = {"s1": "P1", "s2": "P2", "s3": "P3"}
VETO_NEG_WINDOW = 14 * rm.DAY
UNDO_VETO_WINDOW = 30 * rm.DAY
DEMOTE_WINDOW = 14 * rm.DAY
DEMOTE_REPAIR_WINDOW = 7 * rm.DAY
DISTRESS_WINDOW = 24 * rm.HOUR
DEDUP_WINDOW = 24 * rm.HOUR
CARE_LOOKBACK = 90 * rm.DAY
MAX_CAUSES = 30

_STAGE_KINDS = frozenset({"promote", "demote", "owner_request", "undo", "set", "init"})


@dataclass
class BondState:
    stage: str
    entered_at: float | None = None
    frozen: bool = False
    last_change: dict[str, Any] | None = None
    pending: list[dict[str, Any]] = field(default_factory=list)
    effective: list[dict[str, Any]] = field(default_factory=list)


def _effective(row: dict[str, Any], track: str) -> bool:
    status, rtrack = row.get("status"), row.get("track") or rm.MODE_ON
    if status == "applied" and rtrack == rm.MODE_ON:
        return True
    return track == rm.MODE_SHADOW and rtrack == rm.MODE_SHADOW and status == "proposed"


def bond_state(transitions: Iterable[dict[str, Any]], start_stage: str, track: str = rm.MODE_ON) -> BondState:
    rows = sorted(transitions or (), key=lambda r: float(r.get("created_at") or 0.0))
    st = BondState(stage=start_stage)
    for r in rows:
        if not _effective(r, track):
            continue
        st.effective.append(r)
        kind = r.get("kind")
        if kind == "freeze":
            st.frozen = True
            continue
        if kind == "unfreeze":
            st.frozen = False
            continue
        to = str(r.get("to_stage") or st.stage)
        if to not in rm.STAGE_CODES:
            continue
        if kind == "owner_request":
            st.frozen = True
        st.stage = to
        st.entered_at = float(r.get("created_at") or 0.0)
        st.last_change = r
    if track == rm.MODE_ON:
        referenced = {str(r.get("ref_id") or "") for r in rows if r.get("ref_id")}
        last_at = float(st.effective[-1].get("created_at") or 0.0) if st.effective else 0.0
        st.pending = [
            r
            for r in rows
            if r.get("status") == "proposed"
            and (r.get("track") or rm.MODE_ON) == rm.MODE_ON
            and str(r.get("transition_id")) not in referenced
            and float(r.get("created_at") or 0.0) >= last_at
            and r.get("from_stage") == st.stage
        ]
    return st


def dedup_positive(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """正向证据：同一种类 24 小时内只计 1 条（§5.5 防刷）。"""
    last: dict[str, float] = {}
    out = []
    for e in active_events(events):
        kind = e.get("kind")
        if kind not in rm.POSITIVE_KINDS:
            continue
        ts = float(e.get("ts") or 0.0)
        if kind in last and ts - last[kind] < DEDUP_WINDOW:
            continue
        last[kind] = ts
        out.append(e)
    return out


def _window(evs: list[dict[str, Any]], now: float, span: float) -> list[dict[str, Any]]:
    return [e for e in evs if 0.0 <= now - float(e.get("ts") or 0.0) <= span]


def _ids(evs: list[dict[str, Any]]) -> list[str]:
    return [str(e["rel_event_id"]) for e in evs[-MAX_CAUSES:]]


def _evidence(cur: str, events: list[dict[str, Any]], now: float) -> list[str] | None:
    """晋升证据要求（不含停留与否决）；满足返回所依据的事件 id，否则 None。"""
    pos = dedup_positive(events)
    if cur == "s1":
        w = _window(pos, now, 30 * rm.DAY)
        days = {cm.local_day(float(e["ts"])) for e in w}
        kinds = {e["kind"] for e in w}
        return _ids(w) if len(days) >= 6 and len(kinds) >= 2 else None
    if cur == "s2":
        w = _window(pos, now, 8 * WEEK)
        weeks = {int((now - float(e["ts"])) // WEEK) for e in w}
        deep = [e for e in w if e["kind"] in (rm.K_CONFIDED, rm.K_SUPPORT)]
        rely = [e for e in w if e["kind"] in (rm.K_RELIED, rm.K_PROMISE_KEPT)]
        return _ids(w) if len(weeks) >= 4 and len(deep) >= 2 and len(rely) >= 1 else None
    if cur == "s3":
        w = _window(pos, now, 12 * WEEK)
        weeks = {int((now - float(e["ts"])) // WEEK) for e in w}
        shared = [e for e in w if e["kind"] == rm.K_SHARED]
        if len(weeks) < 8 or len(shared) < 3:
            return None
        evs = active_events(events)
        neg_ids = {e["rel_event_id"] for e in evs if e.get("kind") == rm.K_HURT}
        made_up = any(e.get("kind") == rm.K_REPAIR and e.get("supersedes") in neg_ids for e in evs)
        no_recent_hurt = not any(
            e.get("kind") == rm.K_HURT and now - float(e.get("ts") or 0.0) <= 90 * rm.DAY for e in evs
        )
        return _ids(w) if (made_up or no_recent_hurt) else None
    return None


def _in_distress(ts: float, distress_ts: list[float]) -> bool:
    return any(abs(ts - d) <= DISTRESS_WINDOW for d in distress_ts)


def evaluate(
    events: Iterable[dict[str, Any]],
    transitions: Iterable[dict[str, Any]],
    now: float,
    *,
    start_stage: str = rm.DEFAULT_BOND_START,
    track: str = rm.MODE_ON,
    modes_active: bool = False,
) -> rm.BondTransition | None:
    """一次至多升/降一级；无变化返回 None（纯函数）。"""
    all_rows = list(transitions or ())
    st = bond_state(all_rows, start_stage, track)
    if track == rm.MODE_ON and st.pending:
        return None  # manual：等主人处理
    evs = [e for e in active_events(events) if float(e.get("ts") or 0.0) <= now]
    active_ids = {str(e.get("rel_event_id")) for e in evs}
    cur = st.stage

    # U1 误判撤销：上一次晋升依据的事件被 undo，且剩余证据不再满足
    lc = st.last_change
    if lc is not None and lc.get("kind") == "promote":
        refs = [str(r) for r in (lc.get("cause_refs") or []) if str(r).startswith("re_")]
        gone = [r for r in refs if r not in active_ids]
        if gone:
            frm = str(lc.get("from_stage") or start_stage)
            at = float(lc.get("created_at") or now)
            before = [e for e in evs if float(e.get("ts") or 0.0) <= at]
            if _evidence(frm, before, at) is None:
                return rm.BondTransition(cur, frm, "undo", gone[:MAX_CAUSES], rule_id="U1")

    # D1 伤害未修复：14 天内 ≥2 条经确认的伤害（模型复核），7 天内没有 repair；
    # 主人难受窗口内的伤害不计；只计本阶段进入之后的伤害（同一批伤害不会连降）
    if rm.stage_index(cur) > rm.stage_index(start_stage):
        distress = [float(e["ts"]) for e in evs if e.get("kind") == rm.K_DISTRESS]
        since = st.entered_at or 0.0
        hurts = [
            e
            for e in evs
            if e.get("kind") in rm.NEGATIVE_KINDS
            and e.get("detector") == rm.DETECTOR_LLM
            and now - float(e["ts"]) <= DEMOTE_WINDOW
            and float(e["ts"]) > since
            and not _in_distress(float(e["ts"]), distress)
        ]
        repaired_recently = any(
            e.get("kind") == rm.K_REPAIR and now - float(e["ts"]) <= DEMOTE_REPAIR_WINDOW for e in evs
        )
        if len(hurts) >= 2 and not repaired_recently:
            to = rm.STAGE_CODES[rm.stage_index(cur) - 1]
            return rm.BondTransition(cur, to, "demote", _ids(hurts), rule_id="D1")

    # 晋升
    if st.frozen or cur not in MIN_STAY:
        return None
    if modes_active or unrepaired_negatives(evs, now, window_s=VETO_NEG_WINDOW, use_decay=False):
        return None
    if cur in ("s2", "s3"):
        if any(
            r.get("kind") == "undo" and now - float(r.get("created_at") or 0.0) <= UNDO_VETO_WINDOW
            for r in st.effective
        ):
            return None
    entered = st.entered_at
    if entered is None:
        if not evs:
            return None
        entered = float(evs[0]["ts"])
    stay = MIN_STAY[cur]
    if lc is not None and lc.get("kind") == "demote" and lc.get("rule_id") == "D1":
        lc_at = float(lc.get("created_at") or 0.0)
        if any(e.get("kind") == rm.K_REPAIR and float(e["ts"]) > lc_at for e in evs):
            stay /= 2.0
    if now - entered < stay:
        return None
    causes = _evidence(cur, evs, now)
    if not causes:
        return None
    to = rm.STAGE_CODES[rm.stage_index(cur) + 1]
    return rm.BondTransition(cur, to, "promote", causes, rule_id=PROMOTE_RULES[cur])


def care_stage(transitions: Iterable[dict[str, Any]], start_stage: str, now: float) -> str:
    """关心桥 delivery 用：近 90 天内正式生效过的最高阶段；主人要求的降阶例外（按要求执行）。"""
    rows = sorted(transitions or (), key=lambda r: float(r.get("created_at") or 0.0))
    st = bond_state(rows, start_stage, rm.MODE_ON)
    best = st.stage
    eff = st.effective
    last_req = max((i for i, r in enumerate(eff) if r.get("kind") == "owner_request"), default=-1)
    for r in eff[last_req + 1 :]:  # 主人要求的降阶之前的阶段不算（按主人要求执行）
        if now - float(r.get("created_at") or 0.0) > CARE_LOOKBACK:
            continue
        for code in (r.get("to_stage"), r.get("from_stage")):
            if code in rm.STAGE_CODES and rm.stage_index(code) > rm.stage_index(best):
                best = code
    return best
