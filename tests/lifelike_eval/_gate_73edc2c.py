# 冻结副本：73edc2c 的 iris_memory/lifelike/initiative/gate.py（P2U off 回归基准，勿改）
"""主动候选硬闸门（纯函数，设计 §7.3、§7.5、§7.9.4；ROADMAP P1-8 验收①）

``check(ctx)`` 只看传入的 ``GateContext``，不做 I/O、不读时钟、不记日志。
全局类闸门（静音时段/活动窗口、连续未回复暂停、收尾冷却、最小静默）仍由现有
``ProactiveEngine`` / ``ProactiveBridge`` 执行；这里只做**逐候选**的闸门。
任何数字门槛只在代码与 HiddenConfig 里，不进 prompt。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple, Optional

CTYPE_FOLLOWUP = "followup_item"
CTYPE_GROUP_CARE = "group_care"

DELIVERY_PUSH = "push"
DELIVERY_PASSIVE = "passive_only"

PRIVACY_OWNER_ONLY = "owner_only"
PRIVACY_TOPIC_ONLY = "topic_only"

G_OK = "ok"
G_PRIVACY = "G-privacy"
G_DELIVERY = "G-delivery"
G_MODE = "G-mode"
G_BUSY = "G-busy"
G_BUDGET = "G-budget"
G_DUP = "G-dup"
G_COOLDOWN = "G-cooldown"
G_BACKOFF = "G-backoff"
G_CAP = "G-cap"
G_GAP = "G-gap"
G_CARE_CAP = "G-care-cap"
G_CARE_COOLDOWN = "G-care-cooldown"
G_CRISIS = "G-crisis"
G_EXPIRED = "G-expired"
G_NOT_YET = "G-not-before"

RELATION_PAUSE = "proactive_pause"
RELATION_SOFTEN = "soften"

ALL_REASONS = (
    G_PRIVACY, G_DELIVERY, G_EXPIRED, G_NOT_YET, G_MODE, G_BUSY, G_BUDGET, G_DUP, G_COOLDOWN,
    G_BACKOFF, G_CAP, G_GAP, G_CARE_CAP, G_CARE_COOLDOWN, G_CRISIS,
)


class GateResult(NamedTuple):
    ok: bool
    reason: str


@dataclass(frozen=True)
class GateContext:
    now: float
    ctype: str
    delivery: str = DELIVERY_PUSH
    privacy: str = PRIVACY_OWNER_ONLY
    scope_is_owner_private: bool = True
    privacy_ok: bool = True  # topic_only 素材的脱敏校验结果
    not_before: float = 0.0
    expires_at: float = float("inf")
    crisis: bool = False
    care_category: str = ""
    care_intensity: str = ""
    # 全局 push 上限（设计 §7.3 G-cap）
    push_sent_24h: int = 0
    push_daily_cap: int = 3
    last_push_at: Optional[float] = None
    min_gap_s: float = 3 * 3600.0
    # 未回应退避（设计 §7.5）
    last_push_unreplied: bool = False
    last_push_ctype: str = ""
    # 去重 / 冷却
    dedupe_used: bool = False
    same_ctype_sent_24h: bool = False
    ctype_cooldown_until: Optional[float] = None
    # 主人状态 / 关系模式 / 预算
    busy_until: Optional[float] = None
    relation_mode: str = ""
    budget_exhausted: bool = False
    # 关心桥（设计 §7.9.4）
    care_sent_today: int = 0
    care_sent_week: int = 0
    care_daily_cap: int = 1
    care_weekly_cap: int = 3
    care_category_cooldown_until: Optional[float] = None
    crisis_sent_24h: bool = False


def care_release(ctx: GateContext) -> bool:
    """他不舒服时照常关心：group_care 中的危机、sick、mood_low(明显) 越过关系模式（§5.4、§7.9.4）"""
    if ctx.ctype != CTYPE_GROUP_CARE:
        return False
    if ctx.crisis or ctx.care_category == "sick":
        return True
    return ctx.care_category == "mood_low" and ctx.care_intensity == "明显"


def check(ctx: GateContext) -> GateResult:
    """逐候选硬闸门；返回第一个不通过的闸门 id，全部通过为 (True, 'ok')"""
    # G-privacy：只允许主人私聊；group_care 必须 owner_only；topic_only 须通过脱敏校验
    if not ctx.scope_is_owner_private:
        return GateResult(False, G_PRIVACY)
    if ctx.ctype == CTYPE_GROUP_CARE and ctx.privacy != PRIVACY_OWNER_ONLY:
        return GateResult(False, G_PRIVACY)
    if ctx.privacy == PRIVACY_TOPIC_ONLY and not ctx.privacy_ok:
        return GateResult(False, G_PRIVACY)
    # 低打扰：passive_only 永不推送
    if ctx.delivery != DELIVERY_PUSH:
        return GateResult(False, G_DELIVERY)
    if ctx.now >= ctx.expires_at:
        return GateResult(False, G_EXPIRED)
    if ctx.now < ctx.not_before:
        return GateResult(False, G_NOT_YET)
    released = care_release(ctx)
    # G-mode：冷静期（P1-6 上线前 relation_mode 恒为空）
    if ctx.relation_mode == RELATION_PAUSE and not released:
        return GateResult(False, G_MODE)
    if ctx.relation_mode == RELATION_SOFTEN and not (ctx.ctype == CTYPE_FOLLOWUP or released):
        return GateResult(False, G_MODE)
    # G-busy：主人说过在忙（危机除外）
    if ctx.busy_until is not None and ctx.now < ctx.busy_until and not ctx.crisis:
        return GateResult(False, G_BUSY)
    # G-budget：当日廉价模型预算耗尽 → 只允许 followup_item（危机除外）
    if ctx.budget_exhausted and ctx.ctype != CTYPE_FOLLOWUP and not ctx.crisis:
        return GateResult(False, G_BUDGET)
    # G-dup：同素材已用过/被拒；同类型 24h 内已发（危机豁免同类型限制，不豁免同素材）
    if ctx.dedupe_used:
        return GateResult(False, G_DUP)
    if ctx.crisis:
        if ctx.crisis_sent_24h:
            return GateResult(False, G_CRISIS)
        return GateResult(True, G_OK)  # 危机：唯一可越过每日上限与间隔的情形（隐私闸门已在上面）
    if ctx.same_ctype_sent_24h:
        return GateResult(False, G_DUP)
    if ctx.ctype_cooldown_until is not None and ctx.now < ctx.ctype_cooldown_until:
        return GateResult(False, G_COOLDOWN)
    # 未回应退避：上一条主动 6h 无回复 → 下一次必须换类型，且间隔翻倍
    if ctx.last_push_unreplied and ctx.last_push_ctype and ctx.last_push_ctype == ctx.ctype:
        return GateResult(False, G_BACKOFF)
    # G-cap：push 每天上限 + 两次之间最小间隔
    if ctx.push_sent_24h >= max(0, int(ctx.push_daily_cap)):
        return GateResult(False, G_CAP)
    if ctx.last_push_at is not None:
        gap = ctx.min_gap_s * (2.0 if ctx.last_push_unreplied else 1.0)
        if ctx.now - ctx.last_push_at < gap:
            return GateResult(False, G_GAP)
    # 关心桥自己的限频
    if ctx.ctype == CTYPE_GROUP_CARE:
        if ctx.care_sent_today >= ctx.care_daily_cap or ctx.care_sent_week >= ctx.care_weekly_cap:
            return GateResult(False, G_CARE_CAP)
        if ctx.care_category_cooldown_until is not None and ctx.now < ctx.care_category_cooldown_until:
            return GateResult(False, G_CARE_COOLDOWN)
    return GateResult(True, G_OK)
