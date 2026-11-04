"""关系事件溯源 / 文字基调 / 关系渐进：常量、数据结构与开关解析（设计 §5、§5.7）。

全部是纯数据，不读写库、不调模型。阶段与基调只有**文字**；内部门槛只在
``bond.py`` 的代码常量里，不进 prompt、不进命令回执。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------- 开关

KEY_MODE = "lifelike_relation_mode"
KEY_LEVELS = "lifelike_relation_levels"
KEY_DAILY_CAP = "lifelike_relation_detector_daily_cap"
KEY_BOND_MODE = "lifelike_bond_mode"
KEY_BOND_START = "lifelike_bond_start_stage"
KEY_BOND_APPROVAL = "lifelike_bond_promote_approval"

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ON = "on"
MODES = (MODE_OFF, MODE_SHADOW, MODE_ON)
_MODE_RANK = {MODE_OFF: 0, MODE_SHADOW: 1, MODE_ON: 2}

DEFAULT_MODE = MODE_SHADOW
DEFAULT_BOND_MODE = MODE_SHADOW
DEFAULT_LEVELS = "soften,boundary"
DEFAULT_DAILY_CAP = 10
DEFAULT_BOND_START = "s1"
DEFAULT_APPROVAL = "notify"

# P1 只开放 1、2 级（拍板 D8：冷静期最高只到 2 级）；3、4 级即使写进配置也忽略
LEVELS_ALLOWED = ("soften", "boundary")

MODULE = "lifelike_relation"  # 廉价模型 token_stats / 预算模块名
LOG = "[lifelike.relation]"
LOG_BOND = "[lifelike.bond]"


def normalize_mode(value: Any, default: str = MODE_OFF) -> str:
    v = str(value if value is not None else "").strip().lower()
    return v if v in MODES else default


def min_mode(a: str, b: str) -> str:
    return a if _MODE_RANK.get(a, 0) <= _MODE_RANK.get(b, 0) else b


def parse_levels(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple, set)):
        parts = [str(v) for v in value]
    else:
        parts = str(value if value is not None else DEFAULT_LEVELS).split(",")
    out = []
    for p in parts:
        p = p.strip().lower()
        if p in LEVELS_ALLOWED and p not in out:
            out.append(p)
    return tuple(out)


def parse_cap(value: Any) -> int:
    if isinstance(value, bool):
        return DEFAULT_DAILY_CAP
    try:
        return max(0, min(50, int(value)))
    except (TypeError, ValueError):
        return DEFAULT_DAILY_CAP


# ---------------------------------------------------------------- 关系事件

K_WARM = "warm"
K_SHARED = "shared_moment"
K_SUPPORT = "support_given"
K_HURT = "hurt"
K_BOUNDARY_CROSSED = "boundary_crossed"
K_BOUNDARY_STATED = "boundary_stated"
K_REPAIR = "repair"
K_OWNER_REQUEST = "owner_request"
K_OWNER_FEEDBACK = "owner_feedback"
K_CONFIDED = "confided"
K_RELIED = "relied_on"
K_PROMISE_KEPT = "promise_kept"
# 主人难受/生病/危机：不是关系变化本身，但它是"立即解除所有模式"的原因事件，也用于
# §5.7.4"主人难受期间的 hurt 不计"
K_DISTRESS = "owner_distress"
K_UNDO = "undo"

ALL_KINDS = frozenset(
    {
        K_WARM, K_SHARED, K_SUPPORT, K_HURT, K_BOUNDARY_CROSSED, K_BOUNDARY_STATED,
        K_REPAIR, K_OWNER_REQUEST, K_OWNER_FEEDBACK, K_CONFIDED, K_RELIED,
        K_PROMISE_KEPT, K_DISTRESS, K_UNDO,
    }
)
POSITIVE_KINDS = frozenset({K_WARM, K_SHARED, K_SUPPORT, K_REPAIR, K_CONFIDED, K_RELIED, K_PROMISE_KEPT})
NEGATIVE_KINDS = frozenset({K_HURT, K_BOUNDARY_CROSSED})

DETECTOR_RULE = "rule"
DETECTOR_LLM = "llm_classifier"
DETECTOR_CMD = "owner_cmd"
DETECTOR_DERIVED = "derived"
DETECTORS = frozenset({DETECTOR_RULE, DETECTOR_LLM, DETECTOR_CMD, DETECTOR_DERIVED})

# context 列取值（文字枚举，不含正文）
CTX_ASK = "ask"  # "别生气了 / 你还在生气吗"
CTX_DISTANCE = "distance"  # 主人要求保持距离（§5.7.4 第 2 种回退）
CTX_UNFREEZE = "unfreeze"  # 主人说"不用保持距离了"
CTX_RESET = "reset"  # /iris_rel reset
CTX_GROUP = "group"  # 主人在群里说生病/情绪差/危机（只存 id，不存原文）

HOUR = 3600.0
DAY = 86400.0
# 只影响近期基调折叠；关系渐进折叠读全部 active 事件
DECAY_S = {
    K_HURT: 72 * HOUR,
    K_BOUNDARY_CROSSED: 72 * HOUR,
    K_WARM: 7 * DAY,
    K_SHARED: 7 * DAY,
    K_REPAIR: 7 * DAY,
}
SUMMARY_MAX = 40

# 她视角的模板原因（不引用主人原文，不含数字）。只有 TONE_REASON_KINDS 会进 prompt
SUMMARIES = {
    K_WARM: "他说了让你挺开心的话",
    K_SHARED: "你们一起经历了一件事",
    K_SUPPORT: "你陪他度过了难受的时候",
    K_HURT: "他有句话让你有点不舒服",
    K_BOUNDARY_CROSSED: "他追问了你不太想聊的事",
    K_BOUNDARY_STATED: "你说过有件事不太想聊",
    K_REPAIR: "他刚才解释了",
    K_OWNER_REQUEST: "他提了相处方式上的要求",
    K_OWNER_FEEDBACK: "他对你说话方式提了意见",
    K_CONFIDED: "他跟你说了心里的事",
    K_RELIED: "他托你帮忙",
    K_PROMISE_KEPT: "你答应他的事做到了",
    K_DISTRESS: "他那会儿状态不太好",
    K_UNDO: "撤回了一条记录",
}
# 晋升证据种类不可见（§5.7.1 第 5 条）：基调原因只用这几类
TONE_REASON_KINDS = frozenset({K_WARM, K_SHARED, K_HURT, K_BOUNDARY_CROSSED, K_REPAIR})


@dataclass
class RelationshipEvent:
    rel_event_id: str
    ts: float
    scope_key: str
    kind: str
    cause_ref: list[str]
    summary: str
    detector: str
    decay_at: float | None = None
    supersedes: str | None = None
    active: bool = True
    undo_of: str | None = None
    context: str = ""

    def as_row(self) -> dict[str, Any]:
        return {
            "rel_event_id": self.rel_event_id,
            "ts": self.ts,
            "scope_key": self.scope_key,
            "kind": self.kind,
            "cause_ref": list(self.cause_ref),
            "summary": self.summary,
            "detector": self.detector,
            "decay_at": self.decay_at,
            "supersedes": self.supersedes,
            "active": 1 if self.active else 0,
            "undo_of": self.undo_of,
            "context": self.context,
        }


# ---------------------------------------------------------------- 模式（可恢复反馈）

MODE_SOFTEN = "soften"
MODE_BOUNDARY = "boundary"
# (最短, 默认, 最长)；boundary 是一次性 + avoid 72h
MODE_DURATION = {
    MODE_SOFTEN: (30 * 60.0, 12 * HOUR, 24 * HOUR),
    MODE_BOUNDARY: (0.0, 72 * HOUR, 72 * HOUR),
}
REL_OWNER_DISTRESS = "owner_distress"
REL_OWNER_DISTRESS_GROUP = "owner_distress_group"
REL_REPAIR = "repair"
REL_ASK = "owner_ask"
REL_RESET = "owner_reset"
REL_UNDO = "undo"
REL_EXPIRED = "expired"


# ---------------------------------------------------------------- 近期基调

TONE_NORMAL = "平常"
TONE_CLOSE = "最近挺亲近"
TONE_AWKWARD = "有点别扭"
TONE_PAUSE = "冷静一下"
TONE_MADE_UP = "刚和好"
TONE_CODES = {
    TONE_NORMAL: "normal",
    TONE_CLOSE: "close",
    TONE_AWKWARD: "awkward",
    TONE_PAUSE: "pause",
    TONE_MADE_UP: "made_up",
}


@dataclass
class RelationView:
    stage: str = TONE_NORMAL
    reasons: list[str] = field(default_factory=list)
    effects: dict[str, str] = field(default_factory=dict)
    reason_ts: float | None = None
    boundary_active: bool = False
    repair_ts: float | None = None

    @property
    def code(self) -> str:
        return TONE_CODES.get(self.stage, "normal")


# ---------------------------------------------------------------- 关系渐进阶段

STAGE_CODES = ("s1", "s2", "s3", "s4")
STAGE_NAMES = {
    "s1": "聊得来的网友",
    "s2": "熟悉的朋友",
    "s3": "亲近、信任的朋友",
    "s4": "很要好的老朋友",
}
STAGE_DESC = {
    "s1": "认识有一阵了，聊得来；会开玩笑，但心事说得不多，偶尔还会客气一下",
    "s2": "已经很熟，不用客气；愿意聊自己这几天的小事，玩笑和撒娇更放得开",
    "s3": "他说的话你愿意往好处想；你也愿意把自己的小烦恼说给他听；有不同意见会直接说，不怕他不高兴",
    "s4": "彼此很放心，默契多、梗多；关心是自然的，不需要理由；吵过也能很快和好",
}
_NAME_TO_CODE = {v: k for k, v in STAGE_NAMES.items()}


def stage_index(code: str) -> int:
    try:
        return STAGE_CODES.index(code)
    except ValueError:
        return 0


def parse_stage(value: Any, default: str = DEFAULT_BOND_START) -> str:
    v = str(value if value is not None else "").strip()
    if v.lower() in STAGE_CODES:
        return v.lower()
    if v in _NAME_TO_CODE:
        return _NAME_TO_CODE[v]
    alias = (("老朋友", "s4"), ("要好", "s4"), ("亲近", "s3"), ("信任", "s3"), ("熟悉", "s2"), ("朋友", "s2"), ("网友", "s1"))
    for k, code in alias:
        if v and k in v:
            return code
    return default


def normalize_approval(value: Any) -> str:
    v = str(value or "").strip().lower()
    return v if v in ("notify", "manual") else DEFAULT_APPROVAL


def bond_allows(stage: str, care_stage: str | None = None) -> dict[str, str]:
    """各阶段在表达面的差异（§5.7.5），只给其他模块读的文字/枚举档位，无数字。"""
    i = stage_index(stage)
    ci = stage_index(care_stage or stage)
    return {
        "cute_extra": "two_cells_occasional" if i >= 1 else "none",
        "owner_style_cap": "occasional" if i >= 2 else "rare",
        "care_push": "push" if ci >= 1 else "mention_only",
        "life_share_push": "yes" if i >= 1 else "no",
        "share_worries": "会说" if i >= 2 else "少",
    }


# 趋同（P2-1 owner_style）档位上限随阶段：起点两阶段 rare，"亲近、信任的朋友"起 occasional；封顶 occasional
OWNER_STYLE_LEVELS = ("off", "rare", "occasional")


def owner_style_cap(stage: str, configured: Any) -> str:
    """实际趋同档位 = min(配置档位, 阶段允许档)；只返回文字档位（off/rare/occasional），不含数字。"""
    conf = str(configured or "").strip().lower()
    if conf not in OWNER_STYLE_LEVELS:
        conf = "rare"
    allowed = bond_allows(stage).get("owner_style_cap", "rare")
    return conf if OWNER_STYLE_LEVELS.index(conf) <= OWNER_STYLE_LEVELS.index(allowed) else allowed


@dataclass(frozen=True)
class RelationGateView:
    """给主动候选池闸门 / P1-11 联动点读的关系状态（只有文字枚举，无数字）。

    U8：冷静期**不减少**主动次数，只影响语气与内容（排序、发送前措辞检查、prompt 文字）。
    ``mode`` 为空 = 与 73edc2c 相同（relation_mode≠on、异常、无状态时都为空）。
    """

    mode: str = ""  # "" | "soften" | "boundary"
    tone: str = "normal"  # RelationView.code
    stage: str = "s1"  # 关系渐进阶段码（bond_mode≠on 时恒为 s1）
    allows: Mapping[str, str] = field(default_factory=dict)

    @property
    def cooling(self) -> bool:
        return self.mode in (MODE_SOFTEN, MODE_BOUNDARY) or self.tone in ("awkward", "pause")


@dataclass(frozen=True)
class BondView:
    stage: str  # 阶段码 s1..s4（注入时映射为文字）
    frozen: bool = False
    reasons: tuple[str, ...] = ()
    allows: Mapping[str, str] = field(default_factory=dict)
    care_stage: str = "s1"

    @property
    def name(self) -> str:
        return STAGE_NAMES.get(self.stage, STAGE_NAMES["s1"])


@dataclass
class BondTransition:
    from_stage: str
    to_stage: str
    kind: str  # promote|demote|owner_request|undo|freeze|unfreeze|approve|reject
    cause_refs: list[str]
    rule_id: str = ""
    status: str = "applied"
    actor: str = "bond_folder"
    track: str = MODE_ON
    ref_id: str = ""
    transition_id: str = ""
    created_at: float = 0.0


ConfigGet = Callable[[str, Any], Any]
