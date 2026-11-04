"""生活系统规则引擎（设计 §4.3、§4.5、§4.9）：纯函数，无 IO，可单测。

- ``can_transition``：状态机转移表（只能前进，done/cancelled/changed 为终态）；
- ``validate_proposal`` / ``validate_plan``：Alice 式"提议 → 判定（accept/adjust/reject）"；
- ``content_violation``：身份边界与原创边界（不说真实具体地点、不说拍照、不冒充真人
  现实处境、不涉及他人、不滑回参照角色爱好；复用 P0-6 StyleGuard 的照搬清单）；
- ``derive_state``：从事件推出场景 / 精力 / 心情（只产出文字档）；
- ``has_self_life_cue`` / ``rule_claim_type`` / ``match_score``：ClaimAuditor 的规则部分。

词表只在代码里，不进 prompt。
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Iterable, Optional, Sequence

from . import models as wm
from ...identity import lazy_re

# ---------------------------------------------------------------- canon（P1-1 设定集上线前的代码默认）

DEFAULT_CANON: dict[str, Any] = {
    # 作息：00:30 睡，08:00 起；事件只能排在 [08:00, 24:00)
    "sleep_start_h": 0.5,
    "wake_h": 8.0,
    "day_end_h": 24.0,
    "max_events": 5,
    "max_social": 0,  # 虚拟日常不编造"和别人一起"的事（社交经历留给 P2-2 的真实群聊素材）
    "max_out": 2,
    "min_minutes": 10,
    "max_minutes": 240,
    # 角色喜好（只做轻量日常，不做"今天去了哪里"）；为空时读配置 lifelike_world_interests
    "interests": (),
}

# ---------------------------------------------------------------- 状态机

_CREATE_ACTORS = frozenset(
    {"planner_llm", "dayplan", "claim_extractor", "owner_suggest", "carryover", "template", "rule", "owner_cmd"}
)
_TRANSITIONS: dict[tuple[Optional[str], str], frozenset[str]] = {
    (None, wm.ST_PLANNED): _CREATE_ACTORS,
    # 说出口要认账（§4.6 adopted）与离线模板事件可以直接以已发生/进行中登记
    (None, wm.ST_DONE): frozenset({"claim_extractor", "template"}),
    (None, wm.ST_IN_PROGRESS): frozenset({"claim_extractor"}),
    (wm.ST_PLANNED, wm.ST_IN_PROGRESS): frozenset({"world_clock"}),
    (wm.ST_IN_PROGRESS, wm.ST_DONE): frozenset({"world_clock"}),
    (wm.ST_PLANNED, wm.ST_CANCELLED): frozenset({"world_clock", "rule", "owner_cmd"}),
    (wm.ST_IN_PROGRESS, wm.ST_CANCELLED): frozenset({"world_clock", "rule", "owner_cmd"}),
    (wm.ST_PLANNED, wm.ST_CHANGED): frozenset({"claim_extractor", "rule", "owner_cmd"}),
    (wm.ST_IN_PROGRESS, wm.ST_CHANGED): frozenset({"claim_extractor", "rule", "owner_cmd"}),
}


def can_transition(frm: Optional[str], to: str, actor: str) -> bool:
    if frm in wm.TERMINAL:
        return False
    return actor in _TRANSITIONS.get((frm, to), frozenset())


# ---------------------------------------------------------------- 内容边界

_PHOTO = re.compile(r"拍照|拍了|拍下|自拍|照片|相片|摄影|合影|视频通话|发图")
_PLACE_SUFFIX = re.compile(
    r"(?:市|省|县|大道|地铁站|火车站|高铁站|机场|大学|学院|医院|博物馆|美术馆|景区|寺|商场|广场|公园|书店|咖啡馆|电影院)"
)
_PLACE_GENERIC = ("附近", "楼下", "家门口", "小区", "旁边", "街角", "社区", "小")
_JOB = re.compile(r"上班|下班|加班|公司|老板|同事|上课|考试|学校|作业|论文|实习|面试|工资|开会|客户")
_PEOPLE = lazy_re(
    r"{OWNER}|主人|男朋友|女朋友|闺蜜|同学|妈妈|爸爸|家人|室友|朋友|邻居|网友|群友|[你他]|她们|他们|大家",
    re.IGNORECASE,
)
_DRAMA = re.compile(r"生病|发烧|感冒|受伤|住院|哭了|大哭|吵架|分手|事故|搬家|旅行|旅游|出差")
_REALTIME = re.compile(r"下雨了|在下雨|下雪|台风|暴雨|天气预报|气温|新闻|热搜|地震")
_FOOD = re.compile(r"炸鸡|香蕉|甜点|零食|奶茶|蛋糕|外卖|做饭|吃了|好吃|下馆子|火锅|烧烤")
_ASCII_WORD = re.compile(r"[A-Za-z]{3,}")
_DIGIT = re.compile(r"\d")


def _place_hit(text: str) -> bool:
    for mt in _PLACE_SUFFIX.finditer(text):
        before = text[max(0, mt.start() - 4) : mt.start()]
        if any(g in before for g in _PLACE_GENERIC):
            continue
        # 前面至少有 2 个汉字（具体名称）才算具体地点："去公园" 本身不算
        if re.search(r"[一-鿿]{2}$", text[: mt.start()]) and not re.search(
            r"(?:去|到|在|逛|回)$", text[: mt.start()]
        ):
            return True
    return False


def _copy_hit(text: str) -> bool:
    """复用 P0-6 StyleGuard 的禁止照搬清单（硬拦截 + 软提示，物品爱好也在里面）。"""
    try:
        from ..style import guard as sg

        hits = sg._regex_hits(sg.COPY_HARD, text, sg.GROUP_COPY_HARD) + sg._regex_hits(
            sg.COPY_SOFT, text, sg.GROUP_COPY_SOFT
        )
        return bool(hits)
    except Exception:  # noqa: BLE001 - 词表加载失败不阻断（其余规则仍生效）
        return False


def content_violation(text: str) -> Optional[str]:
    """返回违规原因码；合规返回 None。"""
    t = str(text or "")
    if not t.strip():
        return None
    if _PHOTO.search(t):
        return "photo"
    if _place_hit(t):
        return "place"
    if _JOB.search(t):
        return "real_life"
    if _PEOPLE.search(t):
        return "people"
    if _DRAMA.search(t):
        return "drama"
    if _REALTIME.search(t):
        return "realtime"
    if _FOOD.search(t):
        return "food"
    if _ASCII_WORD.search(t):
        return "ascii"
    if _DIGIT.search(t):
        return "digit"
    if _copy_hit(t):
        return "copy"
    return None


# ---------------------------------------------------------------- 提议判定


def _occupying(ev: wm.LifeEvent) -> bool:
    return not ev.retracted and ev.status not in (wm.ST_CANCELLED, wm.ST_CHANGED)


def _overlap(a: wm.LifeEvent, b: wm.LifeEvent) -> bool:
    if None in (a.slot_start, a.slot_end, b.slot_start, b.slot_end):
        return False
    return float(a.slot_start) < float(b.slot_end) and float(b.slot_start) < float(a.slot_end)


def _is_out(ev: wm.LifeEvent) -> bool:
    return ev.scene in (wm.SCENE_OUT, wm.SCENE_ROAD)


def validate_proposal(
    ev: wm.LifeEvent, day_events: Sequence[wm.LifeEvent], canon: Optional[dict] = None
) -> wm.Verdict:
    canon = canon or DEFAULT_CANON
    adjusted: list[str] = []
    e = replace(ev, source_ref=list(ev.source_ref))
    if e.kind not in wm.KINDS:
        return wm.Verdict("reject", "bad_kind")
    if e.scene not in wm.SCENES or e.scene == wm.SCENE_SLEEP:
        return wm.Verdict("reject", "bad_scene")
    if e.energy_cost not in wm.COSTS:
        e.energy_cost = ""
        adjusted.append("cost")
    if e.shareable not in wm.SHAREABLE:
        e.shareable = "owner"
        adjusted.append("shareable")
    e.title = " ".join(str(e.title or "").split())
    e.detail = " ".join(str(e.detail or "").split())
    if not e.title:
        return wm.Verdict("reject", "empty_title")
    if len(e.title) > wm.TITLE_MAX:
        e.title = e.title[: wm.TITLE_MAX]
        adjusted.append("title_len")
    if len(e.detail) > wm.DETAIL_MAX:
        e.detail = e.detail[: wm.DETAIL_MAX]
        adjusted.append("detail_len")
    bad = content_violation(e.title) or content_violation(e.detail)
    if bad:
        return wm.Verdict("reject", f"content:{bad}")
    if e.kind == wm.KIND_SOCIAL and int(canon.get("max_social", 0)) <= 0:
        return wm.Verdict("reject", "social_disabled")

    # 时段：只能在清醒时段 [wake, day_end)
    if e.slot_start is not None and e.slot_end is not None:
        lo = wm.day_start(e.day) + float(canon["wake_h"]) * 3600
        hi = wm.day_start(e.day) + float(canon["day_end_h"]) * 3600
        s, t = float(e.slot_start), float(e.slot_end)
        if t <= s:
            return wm.Verdict("reject", "bad_slot")
        if t <= lo or s >= hi:
            return wm.Verdict("reject", "sleep_window")
        if s < lo or t > hi:
            s, t = max(s, lo), min(t, hi)
            adjusted.append("sleep_clip")
        dur = (t - s) / 60.0
        if dur < float(canon["min_minutes"]):
            return wm.Verdict("reject", "too_short")
        if dur > float(canon["max_minutes"]):
            t = s + float(canon["max_minutes"]) * 60
            adjusted.append("too_long")
        e.slot_start, e.slot_end = s, t
        if e.due_at is None or float(e.due_at) != t:
            e.due_at = t
    elif e.due_at is None:
        return wm.Verdict("reject", "no_due")

    live = [x for x in day_events if _occupying(x) and x.event_id != e.event_id]
    if len(live) >= int(canon["max_events"]):
        return wm.Verdict("reject", "density")
    if e.kind == wm.KIND_SOCIAL and sum(1 for x in live if x.kind == wm.KIND_SOCIAL) >= int(canon["max_social"]):
        return wm.Verdict("reject", "density_social")
    if _is_out(e) and sum(1 for x in live if _is_out(x)) >= int(canon["max_out"]):
        return wm.Verdict("reject", "density_out")
    if any(_overlap(e, x) for x in live):
        return wm.Verdict("reject", "overlap")
    norm = _norm(e.title)
    if any(_norm(x.title) == norm for x in live):
        return wm.Verdict("reject", "duplicate")
    if adjusted:
        return wm.Verdict("adjust", ",".join(adjusted), e)
    return wm.Verdict("accept", "ok", e)


def validate_plan(
    plan: wm.DayPlan, existing: Sequence[wm.LifeEvent], canon: Optional[dict] = None
) -> tuple[wm.DayPlan, list[wm.Verdict]]:
    canon = canon or DEFAULT_CANON
    verdicts: list[wm.Verdict] = []
    accepted: list[wm.LifeEvent] = []
    ordered = sorted(plan.events, key=lambda x: (x.slot_start is None, x.slot_start or 0.0))
    for ev in ordered:
        v = validate_proposal(ev, [*existing, *accepted], canon)
        verdicts.append(v)
        if v.result in ("accept", "adjust") and v.adjusted is not None:
            accepted.append(v.adjusted)
    outline = " ".join(str(plan.outline or "").split())[: wm.OUTLINE_MAX]
    if content_violation(outline):
        outline = "平常的一天"
    return wm.DayPlan(plan.day, outline, accepted, plan.generator), verdicts


# ---------------------------------------------------------------- 状态派生


def is_asleep(ts: float, canon: Optional[dict] = None) -> bool:
    canon = canon or DEFAULT_CANON
    h = wm.local_hour(ts)
    return float(canon["sleep_start_h"]) <= h < float(canon["wake_h"])


def derive_state(events: Iterable[wm.LifeEvent], now: float, canon: Optional[dict] = None) -> wm.LifeState:
    canon = canon or DEFAULT_CANON
    evs = [e for e in events if not e.retracted]
    asleep = is_asleep(now, canon)
    today = wm.local_day(now)
    current = None
    for e in evs:
        if e.status == wm.ST_IN_PROGRESS and e.slot_start is not None and float(e.slot_start) <= now:
            current = e
    scene = wm.SCENE_SLEEP if asleep else (current.scene if current else wm.SCENE_HOME)
    h = wm.local_hour(now)
    tired = sum(
        1 for e in evs if e.day == today and e.status == wm.ST_DONE and e.energy_cost == "累"
    )
    if asleep or h >= 23.5 or h < float(canon["wake_h"]) + 0.5:
        energy = wm.ENERGY_SLEEPY
    elif tired >= 2 or (tired >= 1 and h >= 20):
        energy = wm.ENERGY_TIRED
    elif 9 <= h < 12 and tired == 0:
        energy = wm.ENERGY_GOOD
    else:
        energy = wm.ENERGY_NORMAL
    mood, cause = "平常", None
    recent = [
        e for e in evs
        if e.status == wm.ST_DONE and e.mood_note and e.settled_at is not None and now - float(e.settled_at) <= 3 * 3600
    ]
    if recent:
        last = max(recent, key=lambda e: float(e.settled_at or 0))
        mood, cause = last.mood_note[: wm.MOOD_MAX], last.event_id
    return wm.LifeState(
        scene=scene,
        energy=energy,
        mood=mood,
        mood_cause=cause,
        current_event_id=current.event_id if current else None,
        awake=not asleep,
        updated_at=float(now),
    )


# ---------------------------------------------------------------- ClaimAuditor 规则部分

_SELF_CUE = re.compile(
    r"我(?:刚刚?|才|今天|今早|早上|上午|中午|下午|傍晚|晚上|昨天|昨晚|明天|待会儿?|一会儿?|等下|等会儿?|正在|还在|在家|在外面|"
    r"出门|回来|打算|准备|最近|这两天|周末)"
    r"|我(?:去|看|听|做|整理|浇|换|种|画|写|玩|逛|睡|收拾|翻|读|追|补)(?:了|完|过)"
    r"|我(?:这边|这会儿|现在)(?:在|正)"
)


def has_self_life_cue(text: str) -> bool:
    return bool(_SELF_CUE.search(str(text or "")))


_PLAN_CUE = re.compile(r"明天|待会|一会儿|等下|等会|打算|准备|想要?去|要去|周末|晚点")
_WISH_CUE = re.compile(r"好想|真想|想去|要是|如果|假如|希望|但愿")
_TOMORROW = re.compile(r"明天|明早|明晚")


def is_tomorrow(span: str) -> bool:
    return bool(_TOMORROW.search(span or ""))


def scene_of_span(span: str) -> Optional[str]:
    s = span or ""
    if re.search(r"在外面|出门了?|在路上|外边|还没回(?:家|来)", s):
        return wm.SCENE_OUT
    if re.search(r"在家|窝着|躺着|家里", s):
        return wm.SCENE_HOME
    if re.search(r"睡了|在睡|刚醒|睡醒", s):
        return wm.SCENE_SLEEP
    return None


def _norm(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(text or ""), flags=re.UNICODE)


def _bigrams(text: str) -> set[str]:
    t = _norm(text)
    return {t[i : i + 2] for i in range(len(t) - 1)} if len(t) > 1 else ({t} if t else set())


_STOP_BIGRAMS = frozenset({"我刚", "刚刚", "今天", "了一", "一下", "我今", "我在", "在家", "一会", "下午", "晚上", "上午", "早上"})


def match_score(span: str, ev: wm.LifeEvent) -> float:
    """自述片段与事件的相似度（字二元组覆盖率，0..1）"""
    a = _bigrams(span) - _STOP_BIGRAMS
    b = _bigrams(f"{ev.title}{ev.detail}{ev.outcome}") - _STOP_BIGRAMS
    if not a or not b:
        return 0.0
    return len(a & b) / float(min(len(a), len(b)))


def span_title(span: str) -> str:
    """把她的自述片段收成事件标题（去掉第一人称与时间词）"""
    t = re.sub(r"^我(?:刚刚?|才|今天|今早|早上|上午|中午|下午|傍晚|晚上|正在|还在|在)?", "", str(span or "").strip())
    t = re.sub(r"[，,。！!？?～~…]+$", "", t)
    return " ".join(t.split())[: wm.TITLE_MAX]
