"""主动候选池协调器（设计 §7.1～§7.7；ROADMAP P1-8）

不新开调度器：挂在现有 ``ProactiveBridge`` 上（``bridge.initiative``），由桥接在这些点调用：

- ``merge_candidates``：``ProactiveBridge.candidates()`` 在 items 候选之后合并候选池
  （逐候选硬闸门 → 内部打分取前 k；分数不进 prompt）；
- ``note_turn_begin`` / ``note_chosen`` / ``note_turn_end``：回合开始、决策选中、回合结束 →
  写 ``initiative_decisions``（理由记录）、推进候选状态；
- ``on_owner_message``：主人私聊新消息 → 归因结算（6h 内首条）、G-busy、关心桥一句话关闭/误判收场；
- ``focus_section`` / ``passive_section``：连续性上下文里的"这次想说的事"与"可以顺口提的"；
- ``review_speech``：pool 候选生成后的发送前检查（来源标注、操纵句、复述原句）。

开关 ``lifelike_initiative_mode``：off = 桥接行为与 3ad413e 完全一致（不读写任何新表）；
shadow = 计算闸门/排序并记 ``initiative_decisions``（mode=shadow），不把候选交给决策；
on = 合并候选。任何异常 → 回退为只用 items 候选，日志不含正文，连续 5 次失败熔断 1 小时。

关系联动（U2 / P1-11；``relation`` 为 RelationService，relation_mode≠on 时读到空视图 = 73edc2c）：
- 闸门拿到真实的 ``relation_mode``（冷静期 1/2 级），但按 U8 **不据此拦截、不减少次数**；
- 阶段≥"熟悉的朋友"（bond_mode=on）时 ``life_share`` 从 ``passive_only_types`` 里放出来可推送（§5.7.5）；
- 冷静期内：``life_share`` 排到最后（"不主动分享生活"只影响选哪件事说）；发送前检查拦"试探句"
  （"在吗""是不是生气了"，设计 §5.5）——只改内容，不改次数；
- 关心桥推送范围不读阶段（U4：关心桥保持主动私聊；§5.7.5 的 s1"只顺口提"被 U4 覆盖）。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from astrbot.api import logger

from ...continuity import models as m
from ..care import rules as care_rules
from ..guards.numeric import guard_text as guard_numeric
from . import gate as g
from .pool import (
    CAND_PREFIX,
    ITEM_PREFIX,
    OPEN_STATUSES,
    ST_BLOCKED,
    ST_OFFERED,
    ST_QUEUED,
    ST_USED,
    CandidatePool,
    CandidateScorer,
)

KEY_MODE = "lifelike_initiative_mode"
KEY_CAP = "lifelike_push_daily_cap"
KEY_GAP = "lifelike_push_min_gap_hours"
KEY_PASSIVE_TYPES = "lifelike_passive_only_types"
KEY_FREEDOM = "lifelike_proactive_freedom"

DEFAULT_MODE = "on"
DEFAULT_CAP = 3
DEFAULT_GAP_H = 3.0
DEFAULT_PASSIVE_TYPES = "life_share,group_topic"

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ON = "on"

CANDIDATE_LIMIT = 3
UNREPLIED_S = 6 * 3600.0
CTYPE_COOLDOWN_S = 7 * 86400.0  # reply_tone=不想聊 → 该类型冷却 7 天
CARE_UNANSWERED_COOLDOWN_S = 7 * 86400.0  # 连续 2 次关心无回应 → group_care 冷却 7 天
CARE_UNWILLING_COOLDOWN_S = 14 * 86400.0  # 关心被回"不想聊" → 该类别冷却 14 天
CARE_CATEGORY_COOLDOWN_S = 24 * 3600.0
HINT_TTL_S = 15 * 60.0
TURN_TTL_S = 6 * 3600.0  # 进程内回合登记的最长保留（防泄漏；结算以库为准）
URGENT_WINDOW_S = 30 * 60.0
FAIL_LIMIT = 5
PAUSE_S = 3600.0

TONE_UNWILLING = "不想聊"
TONE_MISREAD = "误判"

_LOG = "[lifelike.initiative]"

CTYPE_LIFE_SHARE = "life_share"
# 冷静期主动消息里的"试探句"（设计 §5.5"冷静期内禁止试探性主动消息"）
_PROBE_RE = re.compile(
    r"(?:你|您)?(?:是不是|还在|是否)?生(?:我的?)?气了?[吗嘛么？?]|还(?:在)?生气|别生(?:我的?)?气|"
    r"(?:^|[，。！？\s])在吗[？?。！!～~]*\s*$|在不在[？?]|理(?:一下)?我(?:一下)?(?:嘛|好不好)|是不是讨厌我"
)


def is_probe(text: str) -> bool:
    return bool(_PROBE_RE.search(str(text or "")))

CTYPE_LABELS = {
    "followup_item": "待续事项",
    "group_care": "群里看到的近况",
    "life_share": "自己的日常",
    "moment_followup": "共同经历的后续",
    "owner_post": "她频道的新帖",
    "group_topic": "群里的话题",
    "confirm_inference": "想确认的事",
    "next_opening": "上次留下的话头",
}


def normalize_mode(raw: Any) -> str:
    s = str(raw if raw is not None else "").strip().lower()
    return s if s in (MODE_OFF, MODE_SHADOW, MODE_ON) else MODE_OFF


def ref_value(cand: Dict[str, Any], key: str) -> str:
    for r in cand.get("source_ref") or []:
        s = str(r)
        if s.startswith(key + ":"):
            return s[len(key) + 1:]
    return ""


def has_flag(cand: Dict[str, Any], flag: str) -> bool:
    return f"flag:{flag}" in [str(r) for r in cand.get("source_ref") or []]


def latency_band(seconds: float) -> str:
    if seconds < 600:
        return "很快"
    if seconds < 3600:
        return "过了一会"
    return "很久"


@dataclass
class Offer:
    now: float
    ranked: List[Tuple[Dict[str, Any], float]] = field(default_factory=list)
    gated: List[Tuple[Dict[str, Any], str]] = field(default_factory=list)

    def summary(self) -> List[Dict[str, Any]]:
        out = [{"cand_id": c["cand_id"], "ctype": c["ctype"], "score": s} for c, s in self.ranked]
        out += [{"cand_id": c["cand_id"], "ctype": c["ctype"], "gate": r} for c, r in self.gated]
        return out

    def gate_result(self) -> str:
        if self.ranked:
            return g.G_OK
        if self.gated:
            return self.gated[0][1]
        return "no_pool"


class InitiativeCoordinator:
    def __init__(
        self,
        store: Any,
        config_get: Callable[[str, Any], Any],
        *,
        owner_private: Callable[[str], bool],
        budget: Any = None,
        care: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._get = config_get
        self._owner_private = owner_private
        self._budget = budget
        self.care = care
        self._clock = clock
        self.pool = CandidatePool(store, clock)
        self._scorer = CandidateScorer()
        self._last_offer: Dict[str, Offer] = {}
        self._turns: Dict[str, Dict[str, Any]] = {}
        self._busy_until: Dict[str, float] = {}
        self._hints: Dict[str, Tuple[str, float]] = {}
        self._fails = 0
        self._paused_until = 0.0
        self._recovered = False
        self.relation: Any = None  # RelationService（可选）；由 ContinuityService 接线

    # ------------------------------------------------------------ config

    def _cfg(self, key: str, default: Any) -> Any:
        try:
            val = self._get(key, default)
        except Exception:
            return default
        return default if val is None else val

    def mode(self) -> str:
        return normalize_mode(self._cfg(KEY_MODE, DEFAULT_MODE))

    def push_cap(self) -> int:
        try:
            return max(0, min(10, int(self._cfg(KEY_CAP, DEFAULT_CAP))))
        except (TypeError, ValueError):
            return DEFAULT_CAP

    def min_gap_s(self) -> float:
        try:
            return max(0.0, min(24.0, float(self._cfg(KEY_GAP, DEFAULT_GAP_H)))) * 3600.0
        except (TypeError, ValueError):
            return DEFAULT_GAP_H * 3600.0

    def freedom(self) -> bool:
        """活人感 U8：主人私聊自由主动（读取失败 → False = 旧闸门）"""
        raw = self._cfg(KEY_FREEDOM, False)
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")

    def passive_types(self) -> set:
        raw = self._cfg(KEY_PASSIVE_TYPES, DEFAULT_PASSIVE_TYPES)
        if isinstance(raw, (list, tuple, set)):
            items = raw
        else:
            items = str(raw or "").replace("，", ",").split(",")
        return {str(x).strip() for x in items if str(x).strip()} - {"group_care", "followup_item"}

    def _now(self) -> float:
        return float(self._clock())

    def relation_view(self, scope_key: str, now: float) -> Any:
        """关系状态（同步，在线程里调用）；未接线 / 异常 → None（= 73edc2c：relation_mode 恒为空）"""
        rel = self.relation
        if rel is None:
            return None
        try:
            return rel.gate_view_sync(scope_key, now)
        except Exception as e:
            logger.warning(f"{_LOG} relation view failed: {type(e).__name__}")
            return None

    def _is_owner_private(self, scope_key: str) -> bool:
        try:
            return bool(self._owner_private(scope_key))
        except Exception:
            return False

    @property
    def paused(self) -> bool:
        return self._now() < self._paused_until

    def _failed(self, where: str, exc: BaseException) -> None:
        self._fails += 1
        logger.warning(f"{_LOG} {where} failed: {type(exc).__name__}")
        if self._fails >= FAIL_LIMIT:
            self._fails = 0
            self._paused_until = self._now() + PAUSE_S
            logger.warning(f"{_LOG} paused_error: {FAIL_LIMIT} consecutive failures, pause 1h")

    def _ok(self) -> None:
        self._fails = 0

    def _care_on(self) -> bool:
        care = self.care
        if care is None:
            return False
        try:
            return care.mode() != MODE_OFF
        except Exception:
            return False

    def _care_mode_on(self) -> bool:
        """关心桥模式为 on（shadow 只记录：不写偏好、不注入提示）"""
        care = self.care
        if care is None:
            return False
        try:
            return care.mode() == MODE_ON
        except Exception:
            return False

    def _care_live(self) -> bool:
        care = self.care
        if care is None:
            return False
        try:
            return care.mode() == MODE_ON and not care.is_off()
        except Exception:
            return False

    # ------------------------------------------------------------ candidates

    async def merge_candidates(
        self, scope_key: str, now: float, items: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        mode = self.mode()
        if mode == MODE_OFF or self.paused or not self._is_owner_private(scope_key):
            return items
        try:
            offer = await asyncio.to_thread(self._build_offer, scope_key, float(now))
            self._ok()
        except Exception as e:
            self._failed("merge", e)
            return items
        self._last_offer[scope_key] = offer
        if mode != MODE_ON:
            return items
        out = [
            dict(it, candidate_id=f"{ITEM_PREFIX}{it.get('item_id')}", ctype=g.CTYPE_FOLLOWUP)
            for it in items
        ]
        for cand, _score in offer.ranked:
            if len(out) >= CANDIDATE_LIMIT:
                break
            out.append(self.as_decision_candidate(cand))
        return out

    async def has_urgent(self, scope_key: str, now: float) -> bool:
        """危机候选（源消息 30 分钟内）且通过闸门 → True：活动窗口推迟对它不生效（设计 §7.9.3 第 4 条）"""
        if self.mode() != MODE_ON or self.paused or not self._is_owner_private(scope_key):
            return False
        try:
            offer = await asyncio.to_thread(self._build_offer, scope_key, float(now))
        except Exception as e:
            self._failed("urgent", e)
            return False
        for cand, _score in offer.ranked:
            if has_flag(cand, "crisis") and float(now) - float(cand.get("created_at") or 0) <= URGENT_WINDOW_S:
                return True
        return False

    @staticmethod
    def as_decision_candidate(cand: Dict[str, Any]) -> Dict[str, Any]:
        """决策 prompt 用的候选 dict（只含文字：类型、理由、素材；不含分数与门槛）"""
        cid = f"{CAND_PREFIX}{cand['cand_id']}"
        why = guard_numeric(str(cand.get("why_now") or ""), source="initiative.why_now")
        material = guard_numeric(str(cand.get("material") or ""), source="initiative.material")
        return {
            "item_id": cid,
            "candidate_id": cid,
            "kind": str(cand.get("ctype") or ""),
            "ctype": str(cand.get("ctype") or ""),
            "topic": why,
            "summary": material,
            "why_now": why,
            "delivery": str(cand.get("delivery") or ""),
        }

    def _owner_last_ts(self, scope_key: str) -> Optional[float]:
        wm = self._store.get_watermark(scope_key) or {}
        ts = wm.get("last_msg_ts")
        return float(ts) if ts is not None else None

    def _budget_exhausted(self) -> bool:
        if self._budget is None:
            return False
        try:
            u = self._budget.usage()
            return u["calls"] >= u["calls_limit"] or u["tokens"] >= u["tokens_limit"]
        except Exception:
            return False

    def _settle_stale(self, scope_key: str, now: float) -> None:
        for d in self.pool.unsettled_sent(scope_key):
            if now - float(d["sent_at"]) >= UNREPLIED_S:
                self.pool.settle_decision(int(d["id"]), False)

    def _build_offer(self, scope_key: str, now: float) -> Offer:
        if not self._recovered and self.mode() == MODE_ON:
            self._recovered = True
            try:
                self.recover_orphans()
            except Exception as e:
                logger.warning(f"{_LOG} recover orphans failed: {type(e).__name__}")
        expired = self.pool.expire(now)
        if expired and self.care is not None:
            self.care.on_candidates_expired([r["cand_id"] for r in expired])
        self._settle_stale(scope_key, now)
        offer = Offer(now=now)
        cands = self.pool.open(scope_key, now)
        if not cands:
            return offer
        base = self._gate_base(scope_key, now)
        rel = self.relation_view(scope_key, now)
        base["relation_mode"] = str(getattr(rel, "mode", "") or "") if rel is not None else ""
        cooling = bool(getattr(rel, "cooling", False)) if rel is not None else False
        passive_types = self.passive_types()
        if rel is not None and (getattr(rel, "allows", None) or {}).get("life_share_push") == "yes":
            passive_types.discard(CTYPE_LIFE_SHARE)  # P1-11：熟悉的朋友起 life_share 可推送
        care_live = self._care_live()
        passed: List[Dict[str, Any]] = []
        for c in cands:
            if c.get("ctype") == g.CTYPE_GROUP_CARE and not care_live:
                continue  # 关心桥被关掉（模式 off/shadow 或主人关闭）：已入队的关心不再推送
            delivery = c.get("delivery")
            if c.get("ctype") in passive_types:
                delivery = g.DELIVERY_PASSIVE
            if delivery != g.DELIVERY_PUSH:
                continue  # passive_only 只作顺口提素材，不进推送决策（也不算被闸门拦）
            ctx = self._gate_ctx(base, c, now)
            res = g.check(ctx)
            if res.ok:
                passed.append(c)
            else:
                offer.gated.append((c, res.reason))
        if passed:
            fatigue: Dict[str, int] = {}
            for d in base["sent_72h"]:
                ct = str(d.get("chosen_ctype") or "")
                fatigue[ct] = fatigue.get(ct, 0) + 1
            response = self.pool.response_stats(scope_key, now - 30 * 86400.0)
            if cooling:
                # 冷静期"不主动分享生活"：只把 life_share 排到最后，不减少可说的事（U8）
                ranked = self._scorer.rank(passed, now, response, fatigue, len(passed))
                ranked.sort(key=lambda cs: cs[0].get("ctype") == CTYPE_LIFE_SHARE)
                offer.ranked = ranked[:CANDIDATE_LIMIT]
            else:
                offer.ranked = self._scorer.rank(passed, now, response, fatigue, CANDIDATE_LIMIT)
        if rel is not None and (cooling or getattr(rel, "stage", "s1") != "s1"):
            logger.debug(
                f"{_LOG} relation link mode={base['relation_mode'] or '-'} tone={getattr(rel, 'tone', '-')}"
                f" stage={getattr(rel, 'stage', '-')} ranked={len(offer.ranked)}"
            )
        return offer

    def _gate_base(self, scope_key: str, now: float) -> Dict[str, Any]:
        sent_week = self.pool.sent_since(scope_key, now - 7 * 86400.0)
        sent_24h = [d for d in sent_week if float(d["sent_at"]) >= now - 86400.0]
        sent_72h = [d for d in sent_week if float(d["sent_at"]) >= now - 3 * 86400.0]
        last = sent_week[0] if sent_week else self.pool.last_sent(scope_key)
        owner_ts = self._owner_last_ts(scope_key)
        unreplied = False
        if last is not None:
            spoke_after = owner_ts is not None and owner_ts > float(last["sent_at"])
            if not spoke_after:
                unreplied = last.get("replied") == 0 or (
                    last.get("settled_at") is None and now - float(last["sent_at"]) >= UNREPLIED_S
                )
        # 类型冷却：reply_tone=不想聊
        cooldown: Dict[str, float] = {}
        for d in sent_week:
            if d.get("reply_tone") == TONE_UNWILLING:
                ct = str(d.get("chosen_ctype") or "")
                cooldown[ct] = max(cooldown.get(ct, 0.0), float(d["sent_at"]) + CTYPE_COOLDOWN_S)
        care = [d for d in sent_week if d.get("chosen_ctype") == g.CTYPE_GROUP_CARE]
        today = m.local_day(now)
        care_today = sum(1 for d in care if m.local_day(float(d["sent_at"])) == today)
        # 连续 2 次关心无回应（误判不算）→ group_care 冷却 7 天
        settled_care = [d for d in care if d.get("settled_at") is not None and d.get("reply_tone") != TONE_MISREAD]
        freedom = self.freedom()
        if not freedom and len(settled_care) >= 2 and all(d.get("replied") == 0 for d in settled_care[:2]):
            cooldown[g.CTYPE_GROUP_CARE] = max(
                cooldown.get(g.CTYPE_GROUP_CARE, 0.0), float(settled_care[0]["sent_at"]) + CARE_UNANSWERED_COOLDOWN_S
            )
        # 关心类别冷却：24h 内同类别关心过；"不想聊" → 14 天
        cat_cooldown: Dict[str, float] = {}
        older = self.pool.sent_since(scope_key, now - CARE_UNWILLING_COOLDOWN_S)
        cat_of: Dict[str, str] = {}
        crisis_24h = False
        for d in older:
            if d.get("chosen_ctype") != g.CTYPE_GROUP_CARE:
                continue
            cid = str(d.get("chosen_cand") or "")
            cand = self.pool.get(cid) if cid else None
            if cand is None:
                continue
            cat = ref_value(cand, "cat")
            cat_of[cid] = cat
            sent_at = float(d["sent_at"])
            if has_flag(cand, "crisis") and now - sent_at < 86400.0:
                crisis_24h = True
            until = sent_at + CARE_CATEGORY_COOLDOWN_S
            if d.get("reply_tone") == TONE_UNWILLING:
                until = sent_at + CARE_UNWILLING_COOLDOWN_S
            if cat:
                cat_cooldown[cat] = max(cat_cooldown.get(cat, 0.0), until)
        return {
            "sent_24h": sent_24h,
            "sent_72h": sent_72h,
            "push_24h": len(sent_24h),
            "last_at": float(last["sent_at"]) if last is not None else None,
            "last_ctype": str(last.get("chosen_ctype") or "") if last is not None else "",
            "unreplied": unreplied,
            "ctype_24h": {str(d.get("chosen_ctype") or "") for d in sent_24h},
            "cooldown": cooldown,
            "care_today": care_today,
            "care_week": len(care),
            "cat_cooldown": cat_cooldown,
            "crisis_24h": crisis_24h,
            "busy_until": self._busy_until.get(scope_key),
            "budget_exhausted": self._budget_exhausted(),
            "owner_private": self._is_owner_private(scope_key),
            "freedom": freedom,
        }

    def _gate_ctx(self, base: Dict[str, Any], c: Dict[str, Any], now: float) -> g.GateContext:
        ctype = str(c.get("ctype") or "")
        cat = ref_value(c, "cat")
        return g.GateContext(
            now=now,
            ctype=ctype,
            delivery=str(c.get("delivery") or ""),
            privacy=str(c.get("privacy") or ""),
            scope_is_owner_private=bool(base["owner_private"]),
            privacy_ok=True,
            not_before=float(c.get("not_before") or 0.0),
            expires_at=float(c.get("expires_at") or 0.0),
            crisis=has_flag(c, "crisis"),
            care_category=cat,
            care_intensity=ref_value(c, "band"),
            push_sent_24h=int(base["push_24h"]),
            push_daily_cap=self.push_cap(),
            last_push_at=base["last_at"],
            min_gap_s=self.min_gap_s(),
            last_push_unreplied=bool(base["unreplied"]),
            last_push_ctype=str(base["last_ctype"]),
            dedupe_used=str(c.get("status")) not in OPEN_STATUSES,
            same_ctype_sent_24h=ctype in base["ctype_24h"],
            ctype_cooldown_until=base["cooldown"].get(ctype),
            busy_until=base["busy_until"],
            relation_mode=str(base.get("relation_mode") or ""),
            budget_exhausted=bool(base["budget_exhausted"]),
            care_sent_today=int(base["care_today"]),
            care_sent_week=int(base["care_week"]),
            care_category_cooldown_until=base["cat_cooldown"].get(cat) if cat else None,
            crisis_sent_24h=bool(base["crisis_24h"]),
            freedom=bool(base.get("freedom", False)),
        )

    # ------------------------------------------------------------ turn lifecycle

    def note_turn_begin(self, scope_key: str, turn_id: str) -> None:
        offer = self._last_offer.pop(scope_key, None)
        self._prune_turns()
        if self.mode() == MODE_OFF or not self._is_owner_private(scope_key):
            return
        self._turns[turn_id] = {
            "scope": scope_key, "offer": offer, "mode": self.mode(), "chosen": None, "begun": self._now(),
        }

    def _prune_turns(self) -> None:
        """丢掉超过 TURN_TTL_S 仍未结束的进程内回合登记（候选状态由 recover_orphans 按库结算）"""
        cutoff = self._now() - TURN_TTL_S
        for tid in [k for k, v in self._turns.items() if float(v.get("begun") or 0.0) < cutoff]:
            self._turns.pop(tid, None)

    async def note_chosen(self, turn_id: str, item_id: Any) -> None:
        t = self._turns.get(turn_id)
        if t is None or not item_id:
            return
        t["chosen"] = str(item_id)
        if t["mode"] != MODE_ON or not str(item_id).startswith(CAND_PREFIX):
            return
        try:
            await asyncio.to_thread(
                self.pool.set_status, str(item_id), ST_OFFERED, only_from=(ST_QUEUED,), offered_at=self._now()
            )
        except Exception as e:
            self._failed("note_chosen", e)

    def chosen_cand(self, turn_id: str) -> Optional[str]:
        t = self._turns.get(turn_id)
        if t is None:
            return None
        ch = t.get("chosen") or ""
        return ch if ch.startswith(CAND_PREFIX) else None

    async def note_turn_end(self, turn_id: str, state: str, reason: str = "") -> None:
        t = self._turns.pop(turn_id, None)
        if t is None:
            # 进程重启 / 对账路径：内存里没有登记 → 按 proactive_turns.item_id 从库里还原
            try:
                t = await asyncio.to_thread(self._turn_from_store, turn_id)
            except Exception as e:
                self._failed("note_turn_end_restore", e)
                return
            if t is None:
                return
        try:
            await asyncio.to_thread(self._record_turn, turn_id, t, str(state or ""), str(reason or ""))
            self._ok()
        except Exception as e:
            self._failed("note_turn_end", e)

    def _turn_from_store(self, turn_id: str) -> Optional[Dict[str, Any]]:
        """库里还原回合登记：只还原选中了候选池条目（cand:）且尚未记过决策的回合"""
        mode = self.mode()
        if mode == MODE_OFF:
            return None
        turn = self._store.get_turn(turn_id)
        if turn is None:
            return None
        item = str(turn.get("item_id") or "")
        scope = str(turn.get("scope_key") or "")
        if not item.startswith(CAND_PREFIX) or not self._is_owner_private(scope):
            return None
        if self.pool.decision_for_turn(turn_id) is not None:
            return None
        return {"scope": scope, "offer": None, "mode": MODE_ON, "chosen": item, "restored": True}

    def recover_orphans(self) -> int:
        """候选停在 offered 但对应回合已终结（崩溃 / 收尾异常遗留）→ 按回合终态结算。

        同步（在线程里调用）；返回结算条数。回合仍在进行中的不动。"""
        if self.mode() != MODE_ON:
            return 0
        n = 0
        for row in self.pool.offered_push():
            cid = f"{CAND_PREFIX}{row['cand_id']}"
            turn = self.pool.latest_turn_for_item(cid)
            if turn is None:
                continue
            state = str(turn.get("state") or "")
            if state in (m.TURN_DECIDING, m.TURN_GENERATING, m.TURN_SENDING):
                continue
            if self.pool.decision_for_turn(turn["turn_id"]) is not None:
                # 决策已记但候选状态没跟上：直接按终态改状态
                if state in (m.TURN_SENT, m.TURN_PARTIAL, m.TURN_UNCERTAIN):
                    self.pool.set_status(cid, ST_USED, only_from=OPEN_STATUSES)
                else:
                    self.pool.set_status(cid, ST_QUEUED, only_from=(ST_OFFERED,))
                n += 1
                continue
            t = {"scope": turn["scope_key"], "offer": None, "mode": MODE_ON, "chosen": cid, "restored": True}
            self._record_turn(turn["turn_id"], t, state, str(turn.get("reason") or ""))
            n += 1
        if n:
            logger.info(f"{_LOG} recovered orphan candidates n={n}")
        return n

    def _record_turn(self, turn_id: str, t: Dict[str, Any], state: str, reason: str) -> None:
        now = self._now()
        offer: Optional[Offer] = t.get("offer")
        chosen = t.get("chosen") or None
        cand = self.pool.get(chosen) if chosen and chosen.startswith(CAND_PREFIX) else None
        # uncertain 也按"已发出"处理（宁可少发，不重复发同一素材）
        sent = state in (m.TURN_SENT, m.TURN_PARTIAL, m.TURN_UNCERTAIN)
        if sent:
            decision = "speak"
        elif reason == "llm_declined":
            decision = "none"
        elif reason.startswith("blocked_style"):
            decision = "blocked_style"
        elif reason in ("decision_error", "decision_parse_failed"):
            decision = "error"
        else:
            decision = "cancel"
        if cand is not None:
            ctype = str(cand.get("ctype") or "")
        elif chosen and not chosen.startswith(CAND_PREFIX):
            ctype = g.CTYPE_FOLLOWUP
        else:
            ctype = ""
        self.pool.record_decision(
            ts=now,
            scope_key=t["scope"],
            turn_id=turn_id,
            mode=t["mode"],
            gate_result=offer.gate_result() if offer is not None else "no_offer",
            ranked=offer.summary() if offer is not None else [],
            decision=decision,
            outcome=f"{state}:{reason}" if reason else state,
            chosen_cand=(f"{CAND_PREFIX}{cand['cand_id']}" if cand is not None else chosen),
            chosen_ctype=ctype,
            why_now=str(cand.get("why_now") or "") if cand is not None else "",
            sent_at=now if sent else None,
        )
        if t["mode"] != MODE_ON or cand is None:
            return
        cid = cand["cand_id"]
        if sent:
            self.pool.set_status(cid, ST_USED, only_from=OPEN_STATUSES)
            if self.care is not None:
                self.care.on_candidate_used(cid, now)
        elif decision == "blocked_style":
            self.pool.set_status(cid, ST_BLOCKED, only_from=OPEN_STATUSES)
            if self.care is not None:
                self.care.on_candidate_blocked(cid)
        else:
            self.pool.set_status(cid, ST_QUEUED, only_from=(ST_OFFERED,))
        logger.info(
            f"{_LOG} turn={turn_id} cand={cid} ctype={ctype} decision={decision} state={state}"
        )

    def presend_ok(self, item_id: str) -> bool:
        """发送前复查：pool 候选仍未结且未过期（同步，供 presend_check 在线程里调用）"""
        cand = self.pool.get(item_id)
        if cand is None:
            return False
        return str(cand.get("status")) in OPEN_STATUSES and self._now() < float(cand.get("expires_at") or 0)

    # ------------------------------------------------------------ speech review

    async def review_speech(self, turn_id: str, text: str) -> Tuple[bool, str]:
        """pool 候选生成文本的发送前检查；(ok, reason)。异常 → 视为不通过（宁可不发）。"""
        cid = self.chosen_cand(turn_id)
        if cid is None:
            return True, ""
        try:
            return await asyncio.to_thread(self._review, cid, str(text or ""))
        except Exception as e:
            self._failed("review", e)
            return False, "review_error"

    def _review(self, cid: str, text: str) -> Tuple[bool, str]:
        from ..style import guard as style_guard

        cand = self.pool.get(cid)
        if cand is None:
            return False, "candidate_missing"
        hits, _soft = style_guard.proactive_man_hits(text)
        if hits:
            return False, "manipulation"
        if is_probe(text):
            rel = self.relation_view(str(cand.get("scope_key") or ""), self._now())
            if rel is not None and getattr(rel, "cooling", False):
                return False, "cooling_probe"
        if cand.get("ctype") == g.CTYPE_GROUP_CARE:
            if not care_rules.has_source_phrase(text):
                return False, "no_source_phrase"
            care = self.care
            sources = care.source_texts(cand) if care is not None else []
            for src in sources:
                if care_rules.longest_common_run(text, src) > care_rules.QUOTE_MAX_OVERLAP:
                    return False, "quotes_source"
        return True, ""

    # ------------------------------------------------------------ owner message

    async def on_owner_message(self, scope_key: str, msg_ts: float, text: str) -> None:
        if not self._is_owner_private(scope_key):
            return
        body = str(text or "").strip()
        if body.startswith("/"):
            return
        ts = float(msg_ts)
        mode = self.mode()
        if mode == MODE_OFF and not self._care_mode_on():
            return
        if mode != MODE_OFF:
            hours = care_rules.busy_hours(body)
            if hours is not None:
                self._busy_until[scope_key] = ts + hours * 3600.0
            else:
                self._busy_until.pop(scope_key, None)
        care = self.care
        if care is not None and self._care_on() and care_rules.is_opt_out(body):
            if not self._care_mode_on():
                # shadow：只记计数，不写偏好、不设提示（与旧行为一致）
                logger.info(f"{_LOG} care opt_out phrase seen mode=shadow (not applied)")
            else:
                await self._apply_opt_out(scope_key, ts)
        if mode == MODE_OFF:
            return
        try:
            await asyncio.to_thread(self._settle_reply, scope_key, ts, body)
            self._ok()
        except Exception as e:
            self._failed("settle", e)

    async def _apply_opt_out(self, scope_key: str, ts: float) -> None:
        care = self.care
        if care is not None:
            try:
                await asyncio.to_thread(care.set_off, scope_key, True, "owner_phrase")
                self._hints[scope_key] = (
                    "她刚说以后不用根据群里的消息关心她了。轻松回一句好就行，不用解释、不追问。",
                    ts + HINT_TTL_S,
                )
            except Exception as e:
                self._failed("care_opt_out", e)

    def _settle_reply(self, scope_key: str, ts: float, body: str) -> None:
        rows = self.pool.unsettled_sent(scope_key)
        if not rows:
            return
        for d in rows:
            sent_at = float(d["sent_at"])
            if ts < sent_at:
                continue
            if ts - sent_at > UNREPLIED_S:
                self.pool.settle_decision(int(d["id"]), False)
                continue
            tone = ""
            if care_rules.is_unwilling_reply(body):
                tone = TONE_UNWILLING
            cid = str(d.get("chosen_cand") or "")
            cat = ""
            if d.get("chosen_ctype") == g.CTYPE_GROUP_CARE and cid:
                cand = self.pool.get(cid)
                cat = ref_value(cand, "cat") if cand is not None else ""
            if (
                d.get("chosen_ctype") == g.CTYPE_GROUP_CARE
                and self.care is not None
                and care_rules.is_misread_reply(body, cat)
            ):
                tone = TONE_MISREAD
                self.care.on_misread(cid)
                self._hints[scope_key] = (
                    "她刚说群里那句只是随口说的/开玩笑的。轻松接一句（比如“那就好，是我想多了”），"
                    "说一次就停：不道歉连发、不追问“真的没事吗”、不说教。",
                    ts + HINT_TTL_S,
                )
            self.pool.settle_decision(int(d["id"]), True, latency_band(ts - sent_at), tone)
            logger.info(
                f"{_LOG} settle dec={d['id']} ctype={d.get('chosen_ctype') or '-'} replied=1"
                f" band={latency_band(ts - sent_at)} tone={tone or '-'}"
            )

    # ------------------------------------------------------------ context sections

    async def focus_section(self, scope_key: str, focus_id: Optional[str], owner_private: bool) -> Optional[str]:
        if not owner_private or not focus_id or not str(focus_id).startswith(CAND_PREFIX):
            return None
        if self.mode() != MODE_ON:
            return None
        cand = await asyncio.to_thread(self.pool.get, str(focus_id))
        if cand is None or cand.get("scope_key") != scope_key:
            return None
        if cand.get("ctype") == g.CTYPE_GROUP_CARE:
            if self.care is None:
                return None
            return self.care.focus_text(cand)
        label = CTYPE_LABELS.get(str(cand.get("ctype")), "想说的事")
        return (
            f"【这次想说的事】（{label}）{cand.get('why_now') or ''}：{cand.get('material') or ''}\n"
            "只围绕这一件事，自然地说一两句；不要解释为什么现在说。"
        )

    async def passive_section(self, scope_key: str, owner_private: bool) -> Optional[str]:
        if not owner_private or not self._is_owner_private(scope_key):
            return None
        now = self._now()
        lines: List[str] = []
        hint = self._hints.get(scope_key)
        if hint is not None:
            if now >= hint[1]:
                self._hints.pop(scope_key, None)
            elif self.mode() == MODE_ON or self._care_mode_on():
                # 提示只在相关模块为 on 时注入（shadow 不改 prompt）
                lines.append(f"- {hint[0]}")
        care = self.care
        if care is not None and self._care_on() and care.mode() == MODE_ON:
            try:
                line = await asyncio.to_thread(self._passive_care_line, scope_key, now)
            except Exception as e:
                self._failed("passive", e)
                line = None
            if line:
                lines.append(f"- {line}")
        if not lines:
            return None
        return "【可以顺口提的】\n" + "\n".join(lines)

    def _passive_care_line(self, scope_key: str, now: float) -> Optional[str]:
        if self.care is None or self.care.is_off():
            return None
        rows = self.pool.open(scope_key, now, delivery=g.DELIVERY_PASSIVE, ctype=g.CTYPE_GROUP_CARE)
        for c in rows:
            offered = c.get("offered_at")
            if offered is not None:
                # 已经在一轮回复里作为素材出现过：视为用过，不再反复提供（说一次就停）
                self.pool.set_status(c["cand_id"], ST_USED, only_from=OPEN_STATUSES)
                self.care.on_candidate_used(c["cand_id"], now, passive=True)
                continue
            line = self.care.passive_line(c, now)
            if not line:
                continue
            # 只注入这一次：标 offered 后立即视为用过
            self.pool.set_status(c["cand_id"], ST_OFFERED, only_from=(ST_QUEUED,), offered_at=now)
            self.pool.set_status(c["cand_id"], ST_USED, only_from=OPEN_STATUSES)
            self.care.on_candidate_used(c["cand_id"], now, passive=True)
            return line
        return None

    # ------------------------------------------------------------ status

    def recent_decisions(self, scope_key: str, n: int = 5) -> List[Dict[str, Any]]:
        return self.pool.recent_decisions(scope_key, n)


__all__ = ["InitiativeCoordinator", "normalize_mode", "KEY_MODE", "CTYPE_LABELS"]
