"""生活系统数据结构（设计 §4.2）。

只放常量与 dataclass，无 IO。对外（prompt）只暴露文字档，不暴露任何序号或分值。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from ...continuity import models as cm

TZ = cm.TZ_SHANGHAI

# ---------------------------------------------------------------- 枚举
KIND_ROUTINE = "routine"
KIND_ACTIVITY = "activity"
KIND_ERRAND = "errand"
KIND_SOCIAL = "social"
KIND_THOUGHT = "thought"
KIND_MEDIA = "media"
KINDS = frozenset({KIND_ROUTINE, KIND_ACTIVITY, KIND_ERRAND, KIND_SOCIAL, KIND_THOUGHT, KIND_MEDIA})

ST_PLANNED = "planned"
ST_IN_PROGRESS = "in_progress"
ST_DONE = "done"
ST_CANCELLED = "cancelled"
ST_CHANGED = "changed"
STATUSES = frozenset({ST_PLANNED, ST_IN_PROGRESS, ST_DONE, ST_CANCELLED, ST_CHANGED})
TERMINAL = frozenset({ST_DONE, ST_CANCELLED, ST_CHANGED})

SCENE_HOME = "在家"
SCENE_OUT = "外出"
SCENE_ROAD = "路上"
SCENE_SLEEP = "睡觉"
SCENES = frozenset({SCENE_HOME, SCENE_OUT, SCENE_ROAD, SCENE_SLEEP})

ENERGY_GOOD = "精神很好"
ENERGY_NORMAL = "正常"
ENERGY_TIRED = "有点累"
ENERGY_SLEEPY = "很困"
ENERGIES = (ENERGY_GOOD, ENERGY_NORMAL, ENERGY_TIRED, ENERGY_SLEEPY)

COSTS = frozenset({"", "轻松", "一般", "累"})

SOURCES = frozenset(
    {"dayplan", "chat_claim", "owner_suggest", "social", "carryover", "template", "owner_cmd", "rule"}
)
SHAREABLE = frozenset({"owner", "group", "none"})

# 事件日志 op
OPS = frozenset(
    {"propose", "accept", "adjust", "reject", "start", "settle", "cancel", "change", "mention", "retract"}
)

# 生活声明（ClaimAuditor）
CLAIM_TYPES = frozenset({"past_fact", "ongoing", "plan", "wish", "figurative", "hypothetical"})
RES_MATCHED = "matched"
RES_ADOPTED = "adopted"
RES_DOWNGRADED = "downgraded"
RES_CONFLICT = "conflict"
RES_IGNORED = "ignored"
RESOLUTIONS = frozenset({RES_MATCHED, RES_ADOPTED, RES_DOWNGRADED, RES_CONFLICT, RES_IGNORED})

# 长度上限（设计 §4.2）
TITLE_MAX = 24
DETAIL_MAX = 80
OUTCOME_MAX = 60
MOOD_MAX = 16
SPAN_MAX = 60
OUTLINE_MAX = 40


@dataclass
class LifeEvent:
    event_id: str
    day: str
    kind: str
    title: str
    detail: str = ""
    slot_start: Optional[float] = None
    slot_end: Optional[float] = None
    due_at: Optional[float] = None
    status: str = ST_PLANNED
    outcome: str = ""
    mood_note: str = ""
    energy_cost: str = ""
    scene: str = SCENE_HOME
    source: str = "dayplan"
    source_ref: list[str] = field(default_factory=list)
    parent_id: Optional[str] = None
    shareable: str = "owner"
    revision: int = 1
    retracted: bool = False
    mention_count: int = 0
    last_mentioned_at: Optional[float] = None
    settled_at: Optional[float] = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "LifeEvent":
        ref = row.get("source_ref")
        if isinstance(ref, str):
            try:
                ref = json.loads(ref or "[]")
            except ValueError:
                ref = []
        return cls(
            event_id=str(row["event_id"]),
            day=str(row["day"]),
            kind=str(row["kind"]),
            title=str(row["title"]),
            detail=str(row.get("detail") or ""),
            slot_start=row.get("slot_start"),
            slot_end=row.get("slot_end"),
            due_at=row.get("due_at"),
            status=str(row["status"]),
            outcome=str(row.get("outcome") or ""),
            mood_note=str(row.get("mood_note") or ""),
            energy_cost=str(row.get("energy_cost") or ""),
            scene=str(row.get("scene") or SCENE_HOME),
            source=str(row.get("source") or "dayplan"),
            source_ref=list(ref or []),
            parent_id=row.get("parent_id"),
            shareable=str(row.get("shareable") or "owner"),
            revision=int(row.get("revision") or 1),
            retracted=bool(row.get("retracted") or 0),
            mention_count=int(row.get("mention_count") or 0),
            last_mentioned_at=row.get("last_mentioned_at"),
            settled_at=row.get("settled_at"),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LifeState:
    scene: str = SCENE_HOME
    energy: str = ENERGY_NORMAL
    mood: str = "平常"
    mood_cause: Optional[str] = None
    current_event_id: Optional[str] = None
    awake: bool = True
    updated_at: float = 0.0


@dataclass
class DayPlan:
    day: str
    outline: str
    events: list[LifeEvent]
    generator: str  # 'llm:<provider>' / 'template'


@dataclass
class Verdict:
    result: str  # accept | adjust | reject
    reason: str
    adjusted: Optional[LifeEvent] = None


@dataclass
class ClaimResult:
    span: str
    claim_type: str
    resolution: str
    event_id: Optional[str] = None
    reason: str = ""


# ---------------------------------------------------------------- 时间工具


def local_day(ts: float) -> str:
    return cm.local_day(ts)


def day_start(day: str) -> float:
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ)
    return d.timestamp()


def at_hm(day: str, hm: str) -> float:
    """'YYYY-MM-DD' + 'HH:MM' → 时间戳（Asia/Shanghai）；'24:00' 允许"""
    h, mi = (int(x) for x in hm.split(":", 1))
    return day_start(day) + h * 3600 + mi * 60


def shift_day(day: str, n: int) -> str:
    d = datetime.strptime(day, "%Y-%m-%d") + timedelta(days=n)
    return d.strftime("%Y-%m-%d")


def local_hour(ts: float) -> float:
    dt = datetime.fromtimestamp(float(ts), TZ)
    return dt.hour + dt.minute / 60.0


def weekday_text(day: str) -> str:
    return "周" + "一二三四五六日"[datetime.strptime(day, "%Y-%m-%d").weekday()]


def period_text(ts: Optional[float]) -> str:
    """时间戳 → 时段文字（不进数字）"""
    if ts is None:
        return ""
    h = local_hour(ts)
    if h < 5:
        return "深夜"
    if h < 9:
        return "早上"
    if h < 11.5:
        return "上午"
    if h < 13.5:
        return "中午"
    if h < 17.5:
        return "下午"
    if h < 19:
        return "傍晚"
    if h < 23:
        return "晚上"
    return "深夜"
