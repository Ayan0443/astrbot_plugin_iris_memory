"""生活系统规则引擎（纯函数）：状态机、内容边界、提议判定、状态派生、自述线索。"""

from __future__ import annotations

from datetime import datetime

import pytest
from astrbot_plugin_iris_memory.iris_memory.continuity import models as cm
from astrbot_plugin_iris_memory.iris_memory.lifelike.world import models as wm
from astrbot_plugin_iris_memory.iris_memory.lifelike.world import planner, rules

DAY = "2026-11-03"


def ts(hm: str, day: str = DAY) -> float:
    return wm.at_hm(day, hm)


def ev(eid="e1", title="给绿萝换水", s="10:00", e="11:00", **kw) -> wm.LifeEvent:
    base = dict(event_id=eid, day=DAY, kind="routine", title=title, slot_start=ts(s), slot_end=ts(e),
                due_at=ts(e), scene="在家")
    base.update(kw)
    return wm.LifeEvent(**base)


# ---------------------------------------------------------------- 状态机

@pytest.mark.parametrize(
    "frm,to,actor,ok",
    [
        (None, "planned", "planner_llm", True),
        (None, "planned", "world_clock", False),
        (None, "done", "claim_extractor", True),
        (None, "done", "planner_llm", False),
        (None, "in_progress", "claim_extractor", True),
        ("planned", "in_progress", "world_clock", True),
        ("planned", "in_progress", "claim_extractor", False),
        ("planned", "done", "world_clock", False),  # 必须先 in_progress
        ("in_progress", "done", "world_clock", True),
        ("in_progress", "done", "claim_extractor", False),  # 聊天里说做完了不能改状态
        ("planned", "cancelled", "rule", True),
        ("in_progress", "cancelled", "owner_cmd", True),
        ("planned", "changed", "claim_extractor", True),
        ("in_progress", "changed", "world_clock", False),
        ("done", "cancelled", "owner_cmd", False),
        ("cancelled", "planned", "rule", False),
        ("changed", "done", "world_clock", False),
    ],
)
def test_can_transition(frm, to, actor, ok):
    assert rules.can_transition(frm, to, actor) is ok


# ---------------------------------------------------------------- 内容边界

@pytest.mark.parametrize(
    "text,reason",
    [
        ("在窗边拍照", "photo"),
        ("给小盆栽拍了张", "photo"),
        ("去中山公园散步", "place"),
        ("去上海市逛逛", "place"),
        ("下午去上班", "real_life"),
        ("准备考试", "real_life"),
        ("和朋友出去玩", "people"),
        ("给你挑礼物", "people"),
        ("和aming聊天", "people"),
        ("感冒了在家躺着", "drama"),
        ("看台风的新闻", "realtime"),
        ("外面下雨了", "realtime"),
        ("吃了炸鸡", "food"),
        ("做饭", "food"),
        ("玩Minecraft", "ascii"),
        ("看了3集番", "digit"),
        ("啾比啾地打招呼", "copy"),
    ],
)
def test_content_violation_hits(text, reason):
    assert rules.content_violation(text) == reason


@pytest.mark.parametrize(
    "text",
    ["给窗台的小盆栽浇水", "去附近的公园散步", "去公园走走", "开着雨声白噪音看书", "看一部讲树懒的纪录片",
     "在附近走走，看看路边的植物", "", "翻海龟的图鉴"],
)
def test_content_ok(text):
    assert rules.content_violation(text) is None


# ---------------------------------------------------------------- 提议判定

def test_validate_accept():
    v = rules.validate_proposal(ev(), [])
    assert v.result == "accept" and v.adjusted is not None


def test_validate_adjust_title_and_sleep_clip_and_long():
    long_title = "慢慢地" * 10
    v = rules.validate_proposal(ev(title=long_title, s="07:00", e="09:00"), [])
    assert v.result == "adjust"
    assert len(v.adjusted.title) == wm.TITLE_MAX
    assert v.adjusted.slot_start == ts("08:00") and v.adjusted.due_at == ts("09:00")
    v2 = rules.validate_proposal(ev(s="09:00", e="23:00"), [])
    assert v2.result == "adjust" and "too_long" in v2.reason
    v3 = rules.validate_proposal(ev(energy_cost="超级累", shareable="all", detail="慢" * 100), [])
    assert v3.result == "adjust"
    assert v3.adjusted.energy_cost == "" and v3.adjusted.shareable == "owner"
    assert len(v3.adjusted.detail) == wm.DETAIL_MAX


@pytest.mark.parametrize(
    "kw,reason",
    [
        (dict(kind="party"), "bad_kind"),
        (dict(scene="睡觉"), "bad_scene"),
        (dict(scene="公司"), "bad_scene"),
        (dict(title="  "), "empty_title"),
        (dict(title="去中山公园"), "content:place"),
        (dict(kind="social", title="出去走走"), "social_disabled"),
        (dict(s="02:00", e="05:00"), "sleep_window"),
        (dict(s="11:00", e="10:00"), "bad_slot"),
        (dict(s="10:00", e="10:05"), "too_short"),
        (dict(slot_start=None, slot_end=None, due_at=None), "no_due"),
    ],
)
def test_validate_reject(kw, reason):
    assert rules.validate_proposal(ev(**kw), []).reason == reason


def test_validate_overlap_density_duplicate_out():
    a = ev("a", s="10:00", e="11:00")
    assert rules.validate_proposal(ev("b", title="看书", s="10:30", e="11:30"), [a]).reason == "overlap"
    assert rules.validate_proposal(ev("b", s="12:00", e="13:00"), [a]).reason == "duplicate"
    # 撤回 / 取消的事件不占时段
    cancelled = ev("a", status="cancelled")
    retracted = ev("a", retracted=True)
    assert rules.validate_proposal(ev("b", title="看书", s="10:30", e="11:30"), [cancelled, retracted]).result == "accept"
    many = [ev(f"x{i}", title=f"事情{'甲乙丙丁戊'[i]}", s=f"{9 + 2 * i:02d}:00", e=f"{10 + 2 * i:02d}:00") for i in range(5)]
    assert rules.validate_proposal(ev("y", title="看书", s="20:30", e="21:00"), many).reason == "density"
    outs = [ev("o1", title="出门散步", scene="外出"), ev("o2", title="出门走走", s="12:00", e="13:00", scene="路上")]
    assert rules.validate_proposal(ev("o3", title="出门看看", s="15:00", e="16:00", scene="外出"), outs).reason == "density_out"
    canon = dict(rules.DEFAULT_CANON, max_social=1)
    soc = ev("s1", kind="social", title="出去走走", s="15:00", e="16:00")
    assert rules.validate_proposal(soc, [], canon).result == "accept"
    assert rules.validate_proposal(ev("s2", kind="social", title="再走走", s="17:00", e="18:00"),
                                   [soc], canon).reason == "density_social"


def test_validate_no_slot_with_due_ok():
    v = rules.validate_proposal(ev(slot_start=None, slot_end=None, due_at=ts("21:00")), [])
    assert v.result == "accept"


def test_validate_plan_and_outline():
    plan = wm.DayPlan(DAY, "去中山公园拍照的一天", [ev("a"), ev("b", title="去中山公园"), ev("c", title="看书", s="10:30", e="12:00")], "llm:x")
    out, verdicts = rules.validate_plan(plan, [])
    assert [e.event_id for e in out.events] == ["a"]
    assert [v.result for v in verdicts].count("reject") == 2
    assert out.outline == "平常的一天"


def test_templates_all_pass_rules():
    for i in range(len(planner._TEMPLATE_DAYS)):
        day = None
        for d in range(1, 60):
            cand = f"2026-12-{d:02d}" if d <= 31 else f"2027-01-{d - 31:02d}"
            if planner._h(cand) % len(planner._TEMPLATE_DAYS) == i:
                day = cand
                break
        assert day
        plan = planner.template_day(day)
        out, verdicts = rules.validate_plan(plan, [])
        assert len(out.events) == len(plan.events), [v.reason for v in verdicts]
        assert all(v.result == "accept" for v in verdicts)
    for rows in planner._OUTCOMES.values():
        for outcome, mood in rows:
            assert rules.content_violation(outcome) is None and rules.content_violation(mood) is None


# ---------------------------------------------------------------- 状态派生

def test_derive_state_sleep_and_current_and_energy():
    s = rules.derive_state([], ts("03:00"))
    assert s.scene == "睡觉" and not s.awake and s.energy == "很困"
    cur = ev("c", status="in_progress", scene="外出", s="15:00", e="16:00")
    s2 = rules.derive_state([cur], ts("15:30"))
    assert s2.scene == "外出" and s2.current_event_id == "c" and s2.energy == "正常"
    s3 = rules.derive_state([], ts("10:00"))
    assert s3.energy == "精神很好" and s3.mood == "平常" and s3.mood_cause is None
    tired = [ev(f"t{i}", status="done", energy_cost="累", settled_at=ts("12:00")) for i in range(2)]
    assert rules.derive_state(tired, ts("14:00")).energy == "有点累"
    one = [ev("t", status="done", energy_cost="累", settled_at=ts("12:00"))]
    assert rules.derive_state(one, ts("21:00")).energy == "有点累"
    assert rules.derive_state([], ts("23:45")).energy == "很困"


def test_derive_state_mood_needs_cause_and_ignores_retracted():
    done = ev("d", status="done", mood_note="意犹未尽", settled_at=ts("14:00"))
    s = rules.derive_state([done], ts("15:00"))
    assert s.mood == "意犹未尽" and s.mood_cause == "d"
    assert rules.derive_state([done], ts("19:00")).mood == "平常"  # 超过 3 小时
    gone = ev("d", status="done", mood_note="意犹未尽", settled_at=ts("14:00"), retracted=True)
    assert rules.derive_state([gone], ts("15:00")).mood == "平常"


# ---------------------------------------------------------------- 自述线索 / 匹配

@pytest.mark.parametrize(
    "text,ok",
    [
        ("我刚给绿萝换了水", True),
        ("我今天看了一部纪录片", True),
        ("我明天打算整理房间", True),
        ("我看完了那本书", True),
        ("我这会儿在听雨声", True),
        ("这个函数的返回值不对", False),
        ("你今天累不累呀", False),
        ("我觉得你说得对", False),
        ("", False),
    ],
)
def test_has_self_life_cue(text, ok):
    assert rules.has_self_life_cue(text) is ok


def test_match_score_and_helpers():
    e = ev(title="看一部讲树懒的纪录片", outcome="看完了，挺治愈的")
    assert rules.match_score("我下午看了树懒的纪录片", e) >= 0.5
    assert rules.match_score("我刚整理了房间", e) < 0.5
    assert rules.match_score("", e) == 0.0
    assert rules.span_title("我刚给绿萝换了水。") == "给绿萝换了水"
    assert rules.scene_of_span("我还在外面呢") == "外出"
    assert rules.scene_of_span("我在家窝着") == "在家"
    assert rules.scene_of_span("我刚睡醒") == "睡觉"
    assert rules.scene_of_span("我看了书") is None
    assert rules.is_tomorrow("我明天去整理书架") and not rules.is_tomorrow("我待会去")


def test_period_text_has_no_digits():
    for h in range(24):
        t = datetime(2026, 11, 3, h, 0, tzinfo=cm.TZ_SHANGHAI).timestamp()
        p = wm.period_text(t)
        assert p and not any(ch.isdigit() for ch in p)
    assert wm.period_text(None) == ""
    assert wm.weekday_text(DAY) == "周二"
