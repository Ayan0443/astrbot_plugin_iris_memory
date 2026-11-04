"""P1-12 群→私聊关心桥：扫描 → 信号 → 候选池；抑制、合并、模型兜底、误判、关闭、保留期、命令"""

import asyncio
import json

import pytest

from astrbot_plugin_iris_memory.iris_memory.continuity.store import ContinuityStore
from astrbot_plugin_iris_memory.iris_memory.lifelike.budget import CheapBudget
from astrbot_plugin_iris_memory.iris_memory.lifelike.care import commands as care_cmd
from astrbot_plugin_iris_memory.iris_memory.lifelike.care import store as cs
from astrbot_plugin_iris_memory.iris_memory.lifelike.care.bridge import (
    CareBridge,
    next_range_start,
    parse_categories,
    parse_groups,
    parse_llm_output,
)
from astrbot_plugin_iris_memory.iris_memory.lifelike.initiative import gate as g
from astrbot_plugin_iris_memory.iris_memory.lifelike.initiative.coordinator import InitiativeCoordinator

OWNER = "100000001"
SCOPE = f"private:{OWNER}"
GROUP = -1001000000004
GROUP2 = -1001111111111
CHANNEL = -1001000000002
SELF = "30003"
T0 = 1_793_667_600.0  # 2026-11-03 09:00 Asia/Shanghai


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class Env:
    def __init__(self, tmp_path, **cfg):
        self.clock = Clock()
        self.store = ContinuityStore(tmp_path / "c.db", clock=self.clock)
        self.store.open()
        self.conf = {
            "lifelike_initiative_mode": "on",
            "lifelike_care_bridge_mode": "on",
            "lifelike_care_bridge_groups": "*",
            **cfg,
        }
        get = lambda k, d=None: self.conf.get(k, d)  # noqa: E731
        self.budget = CheapBudget(self.store, get, self.clock)
        self.coord = InitiativeCoordinator(
            self.store, get, owner_private=lambda s: s == SCOPE, budget=self.budget, clock=self.clock
        )
        self.llm_calls = []
        self.llm_reply = None
        self.care = CareBridge(
            self.store,
            get,
            pool=self.coord.pool,
            owner_ids=lambda: {OWNER},
            observe_chat_ids=lambda: (GROUP, GROUP2, CHANNEL),
            owner_channel_ids=lambda: (CHANNEL,),
            initiative_mode=self.coord.mode,
            llm_generate=self._llm,
            budget=self.budget,
            clock=self.clock,
        )
        self.coord.care = self.care
        self.mid = 100

    async def _llm(self, prompt, timeout):
        self.llm_calls.append(prompt)
        if isinstance(self.llm_reply, Exception):
            raise self.llm_reply
        return self.llm_reply or "[]"

    def post(self, text, *, kind="owner", sender=OWNER, chat=GROUP, at=None, fwd=0):
        self.mid += 1
        self.store.add_observed_post(
            chat_id=chat, msg_id=self.mid, posted_at=self.clock.t if at is None else at, sender_id=sender,
            author_kind=kind, is_forward=fwd, text=text, via="realtime",
        )
        return self.mid

    def run(self):
        return asyncio.run(self.care.run_incremental(self.clock.t))

    def signals(self):
        cs.ensure_care_schema(self.store)
        with self.store._lock:
            rows = self.store._conn().execute("SELECT * FROM care_signals ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def cands(self):
        return self.coord.pool.open(SCOPE, self.clock.t, ctype=g.CTYPE_GROUP_CARE)


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    # 游标从启用前 1h 开始：首轮在 T0 前 1h 内的帖子都会处理
    yield e
    e.store.close()


def test_sick_owner_post_enqueued_as_push(env):
    env.clock.t = T0 + 60
    env.post("今天发烧了，头好晕", at=T0)
    assert env.run() == 1
    sig = env.signals()[0]
    assert sig["category"] == "sick" and sig["status"] == "queued" and sig["detector"] == "rule"
    assert "发烧" not in sig["msg_ids"] and json.loads(sig["msg_ids"]) == [env.mid]
    c = env.cands()[0]
    assert c["delivery"] == "push" and c["privacy"] == "owner_only" and c["scope_key"] == SCOPE
    assert "群里" in c["why_now"] and "cat:sick" in c["source_ref"]
    # 候选进入决策
    out = asyncio.run(env.coord.merge_candidates(SCOPE, env.clock.t, []))
    assert out and out[0]["ctype"] == "group_care" and out[0]["item_id"].startswith("cand:")


@pytest.mark.parametrize(
    "kw",
    [
        {"kind": "other", "sender": "12345"},  # 群友
        {"kind": "self", "sender": SELF},  # 她自己
        {"kind": "channel", "sender": "", "chat": CHANNEL},  # 频道
        {"fwd": 1},  # 转发
        {"chat": -100777},  # 未授权群
    ],
)
def test_non_owner_sources_never_enqueued(env, kw):
    env.post("我发烧了好难受", at=T0 - 10, **kw)
    env.run()
    assert env.signals() == []
    assert env.cands() == []
    assert env.llm_calls == []


def test_owner_channel_post_not_a_source(env):
    env.post("今天发烧了", chat=CHANNEL, at=T0 - 10)
    env.run()
    assert env.signals() == []


def test_tired_is_passive_and_expires_before_night(env):
    env.clock.t = T0 + 11 * 3600  # 20:00
    env.post("今天好累", at=env.clock.t)
    env.run()
    c = env.coord.pool.open(SCOPE, env.clock.t, delivery="passive_only")[0]
    assert c["delivery"] == "passive_only"
    # 20:00 发的"累"在 01:00 前过期（不在第二天早上问）
    assert c["expires_at"] == pytest.approx(T0 + 16 * 3600)
    assert asyncio.run(env.coord.merge_candidates(SCOPE, env.clock.t, [])) == []


def test_shadow_only_detects(env):
    env.conf["lifelike_care_bridge_mode"] = "shadow"
    env.post("又在加班到凌晨", at=T0 - 10)
    env.run()
    sig = env.signals()[0]
    assert sig["status"] == "detected" and sig["cand_id"] is None
    assert env.cands() == []


def test_mode_off_reads_nothing(env):
    env.conf["lifelike_care_bridge_mode"] = "off"
    env.post("发烧了", at=T0 - 10)
    assert env.run() == 0
    assert asyncio.run(env.care.tick()) == 0


def test_owner_pref_off_and_muted(env):
    env.care.cs.set_pref(off=True)
    env.post("发烧了", at=T0 - 10)
    env.run()
    assert env.signals() == []
    env.care.cs.set_pref(off=False, mute=["sick"])
    env.post("头疼得厉害", at=T0 - 5)
    env.run()
    assert env.signals() == []


def test_category_disabled(env):
    env.conf["lifelike_care_bridge_categories"] = "tired"
    env.post("发烧了", at=T0 - 10)
    env.run()
    assert env.signals() == []


def test_private_known_suppressed(env):
    env.store.record_inbound(SCOPE, 1, OWNER, "我今天发烧了", T0 - 600)
    env.post("发烧了", at=T0 - 10)
    env.run()
    sig = env.signals()[0]
    assert sig["status"] == "suppressed" and sig["suppress_reason"] == "private_known"
    assert env.cands() == []


def test_replied_in_group_suppressed(env):
    env.post("发烧了", at=T0 - 300)
    env.post("多喝热水", kind="self", sender=SELF, at=T0 - 200)
    env.run()
    sig = env.signals()[0]
    assert sig["status"] == "suppressed" and sig["suppress_reason"] == "replied_in_group"
    assert env.cands() == []


def test_urgent_crisis(env):
    env.post("我想自杀", at=T0 - 10)
    env.run()
    assert asyncio.run(env.coord.has_urgent(SCOPE, T0))
    assert not asyncio.run(env.coord.has_urgent(SCOPE, T0 + 3600))
    assert not asyncio.run(env.coord.has_urgent("-1001", T0))


def test_owner_active_in_private_becomes_passive(env):
    env.store.advance_watermark(SCOPE, 5, T0 - 60)
    env.post("发烧了", at=T0 - 10)
    env.run()
    assert env.coord.pool.open(SCOPE, T0, delivery="passive_only")


def test_episode_merge_and_no_duplicate(env):
    env.post("发烧了", at=T0 - 1000)
    env.run()
    env.clock.t = T0 + 3600
    env.post("还在发烧，去医院了", at=T0 + 3000)
    env.run()
    sigs = env.signals()
    assert len(sigs) == 1 and len(json.loads(sigs[0]["msg_ids"])) == 2
    assert len(env.cands()) == 1
    # 再跑一轮：游标已前进，不重复处理
    env.run()
    assert len(env.signals()) == 1


def test_cursor_not_advanced_on_failure(env, monkeypatch):
    env.post("发烧了", at=T0 - 10)

    async def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(env.care, "_process_posts", boom)
    assert env.run() == 0
    assert env.care.cs.scan_cursor() is None
    monkeypatch.undo()
    env.run()
    assert len(env.signals()) == 1


def test_initiative_off_means_passive(env):
    env.conf["lifelike_initiative_mode"] = "off"
    env.post("发烧了", at=T0 - 10)
    env.run()
    assert env.coord.pool.open(SCOPE, T0, delivery="passive_only")


def test_push_flag_false_means_passive(env):
    env.conf["lifelike_care_bridge_push"] = False
    env.post("拿到offer了", at=T0 - 10)
    env.run()
    assert env.coord.pool.open(SCOPE, T0, delivery="passive_only")


class TestModel:
    def test_fuzzy_joke_dropped_and_confirmed_kept(self, env):
        env.post("累死了哈哈", at=T0 - 20)
        env.post("好烦", at=T0 - 10, chat=GROUP2)
        env.llm_reply = json.dumps(
            [
                {"i": 0, "about_self": True, "category": "tired", "intensity": "轻", "is_joke": True, "crisis": False, "gist": "x"},
                {"i": 1, "about_self": True, "category": "mood_low", "intensity": "明显", "is_joke": False,
                 "crisis": False, "gist": "说工作烦得不行"},
            ]
        )
        env.run()
        assert len(env.llm_calls) == 1
        prompt = env.llm_calls[0]
        assert "累死了哈哈" in prompt and "好烦" in prompt
        sigs = env.signals()
        assert len(sigs) == 1 and sigs[0]["detector"] == "llm" and sigs[0]["gist"] == "说工作烦得不行"
        assert env.cands()[0]["delivery"] == "push"

    def test_group_member_text_never_sent_to_model(self, env):
        env.post("好烦", at=T0 - 20)
        env.post("我也好烦，我发烧了", kind="other", sender="999", at=T0 - 15)
        env.llm_reply = "[]"
        env.run()
        assert "发烧" not in env.llm_calls[0]

    def test_model_failure_drops_fuzzy(self, env):
        env.post("好烦", at=T0 - 20)
        env.llm_reply = RuntimeError("down")
        env.run()
        assert env.signals() == []

    def test_daily_cap_and_disabled(self, env):
        env.conf["lifelike_care_llm_daily_cap"] = 0
        env.post("好烦", at=T0 - 20)
        env.run()
        assert env.llm_calls == [] and env.signals() == []
        env.conf["lifelike_care_llm_daily_cap"] = 8
        env.conf["lifelike_care_llm"] = False
        env.post("好烦啊", at=T0 - 10)
        env.run()
        assert env.llm_calls == []

    def test_vague_crisis_without_model_is_normal_mood_low(self, env):
        env.conf["lifelike_care_llm"] = False
        env.post("有点想死", at=T0 - 10)
        env.run()
        sig = env.signals()[0]
        assert sig["category"] == "mood_low" and sig["intensity_band"] == "明显" and sig["crisis"] == 0
        assert "flag:crisis" not in env.cands()[0]["source_ref"]

    def test_model_confirmed_crisis(self, env):
        env.post("有点想死", at=T0 - 10)
        env.llm_reply = json.dumps(
            [{"i": 0, "about_self": True, "category": "mood_low", "intensity": "明显", "is_joke": False, "crisis": True,
              "gist": "说很难受"}]
        )
        env.run()
        assert env.signals()[0]["crisis"] == 1
        assert "flag:crisis" in env.cands()[0]["source_ref"]

    def test_gist_that_copies_source_falls_back(self, env):
        env.post("改了一天的需求真的好烦好烦好烦", at=T0 - 10)
        env.llm_reply = json.dumps(
            [{"i": 0, "about_self": True, "category": "mood_low", "intensity": "明显", "is_joke": False,
              "crisis": False, "gist": "改了一天的需求真的好烦"}]
        )
        env.run()
        assert env.signals()[0]["gist"] == "说好烦，心情不太好"


def test_explicit_crisis_flagged(env, caplog):
    env.post("我想自杀", at=T0 - 10)
    env.run()
    c = env.cands()[0]
    assert "flag:crisis" in c["source_ref"] and c["delivery"] == "push"


def _send_care(env, turn="t1"):
    asyncio.run(env.coord.merge_candidates(SCOPE, env.clock.t, []))
    env.coord.note_turn_begin(SCOPE, turn)
    cid = env.cands()[0]["cand_id"]
    asyncio.run(env.coord.note_chosen(turn, f"cand:{cid}"))
    return cid


class TestDeliveryFlow:
    def test_review_source_phrase_and_quote(self, env):
        env.post("今天发烧了，烧到三十九度了真难受", at=T0 - 10)
        env.run()
        cid = _send_care(env)
        assert asyncio.run(env.coord.review_speech("t1", "你还好吗，多喝水")) == (False, "no_source_phrase")
        quoted = "刚在群里看到你说烧到三十九度了真难受，还好吗"
        assert asyncio.run(env.coord.review_speech("t1", quoted)) == (False, "quotes_source")
        assert asyncio.run(env.coord.review_speech("t1", "刚在群里看到你说发烧了，还好吗？")) == (True, "")
        focus = asyncio.run(env.coord.focus_section(SCOPE, f"cand:{cid}", True))
        assert "在群里看到" in focus and "不卖萌" in focus
        asyncio.run(env.coord.note_turn_end("t1", "sent", ""))
        assert env.signals()[0]["status"] == "used"

    def test_daily_cap_for_group_care(self, env):
        env.post("发烧了", at=T0 - 10)
        env.run()
        _send_care(env)
        asyncio.run(env.coord.note_turn_end("t1", "sent", ""))
        env.clock.t = T0 + 5 * 3600
        env.post("拿到offer了", at=env.clock.t - 10)
        env.run()
        out = asyncio.run(env.coord.merge_candidates(SCOPE, env.clock.t, []))
        assert out == []
        # 同类型 24h 内已发（G-dup）先于关心桥自己的每日上限命中；两者都拦
        assert env.coord._last_offer[SCOPE].gated[0][1] in (g.G_DUP, g.G_CARE_CAP)

    def test_misread_closes_and_builds_hyperbole(self, env):
        for day in range(2):
            env.clock.t = T0 + day * 86400 * 2
            env.post("今天加班到凌晨", at=env.clock.t - 10)
            env.run()
            _send_care(env, f"t{day}")
            asyncio.run(env.coord.note_turn_end(f"t{day}", "sent", ""))
            asyncio.run(env.coord.on_owner_message(SCOPE, env.clock.t + 60, "开玩笑的啦"))
            sig = env.signals()[-1]
            assert sig["status"] == "declined" and sig["suppress_reason"] == "misread"
            dec = env.coord.pool.recent_decisions(SCOPE, 1)[0]
            assert dec["replied"] == 1 and dec["reply_tone"] == "误判"
            hint = asyncio.run(env.coord.passive_section(SCOPE, True))
            assert "说一次就停" in hint
        assert ("overtime", "加班") in env.care.cs.hyperbole(env.clock.t)
        # 之后同一说法只判模糊（模型关掉 → 不入队）
        env.conf["lifelike_care_llm"] = False
        env.clock.t += 86400 * 2
        env.post("今天加班", at=env.clock.t - 10)
        env.run()
        assert env.cands() == []

    def test_opt_out_phrase(self, env):
        env.post("发烧了", at=T0 - 10)
        env.run()
        assert env.cands()
        asyncio.run(env.coord.on_owner_message(SCOPE, T0 + 30, "别根据群里的消息关心我了"))
        assert env.care.is_off()
        assert env.cands() == []
        assert env.signals()[0]["suppress_reason"] == "off"
        assert "不用根据群里" in asyncio.run(env.coord.passive_section(SCOPE, True))
        env.post("头疼", at=T0 + 40)
        env.clock.t = T0 + 50
        env.run()
        assert len(env.signals()) == 1  # 关闭后新入队 = 0

    def test_passive_section_owner_private_only(self, env):
        env.post("今天好累", at=T0 - 10)
        env.run()
        text = asyncio.run(env.coord.passive_section(SCOPE, True))
        assert "可以顺口提的" in text and "在群里" in text and "说清是在群里看到的" in text
        assert asyncio.run(env.coord.passive_section(SCOPE, False)) is None
        assert asyncio.run(env.coord.passive_section("-1001000000004", True)) is None
        # 2 小时后视为已用过，不再反复提供
        env.clock.t = T0 + 3 * 3600
        assert asyncio.run(env.coord.passive_section(SCOPE, True)) is None
        assert env.signals()[0]["status"] == "used"


def test_retention(env):
    env.post("发烧了", at=T0 - 10)
    env.run()
    sid = env.signals()[0]["signal_id"]
    env.care.cs.update(sid, status="expired")
    env.clock.t = T0 + 31 * 86400
    cleared, deleted = env.care.cs.retention(env.clock.t, 30 * 86400, 90 * 86400)
    assert (cleared, deleted) == (1, 0) and env.signals()[0]["gist"] == ""
    env.clock.t = T0 + 91 * 86400
    assert env.care.cs.retention(env.clock.t, 30 * 86400, 90 * 86400)[1] == 1
    assert env.signals() == []


def test_tick_runs_expire_and_retention(env):
    env.post("今天好累", at=T0 - 10)
    asyncio.run(env.care.tick())
    env.clock.t = T0 + 20 * 3600
    asyncio.run(env.care.tick())
    assert env.signals()[0]["status"] == "expired"


def test_authorized_groups(env):
    assert env.care.authorized_groups() == {GROUP, GROUP2}
    env.conf["lifelike_care_bridge_groups"] = f"{GROUP2}, -100555"
    assert env.care.authorized_groups() == {GROUP2}
    env.conf["lifelike_care_bridge_groups"] = ""
    assert env.care.authorized_groups() == set()
    env.conf["lifelike_care_bridge_groups"] = "*"
    env.conf["lifelike_care_include_owner_channel"] = True
    assert CHANNEL in env.care.authorized_groups()
    env.conf["lifelike_care_include_owner_channel"] = False
    env.care._whitelist = lambda: ["telethon:GroupMessage:1000000004"]
    assert env.care.authorized_groups() == {GROUP}


def test_parsers():
    assert parse_groups("*") == (True, set())
    assert parse_groups([GROUP, "x"]) == (False, {GROUP})
    assert parse_categories("sick, foo,tired") == {"sick", "tired"}
    assert parse_categories(["good_news"]) == {"good_news"}
    assert parse_llm_output("nope", 2) == [None, None]
    assert parse_llm_output("[1, {\"i\": 5}]", 1) == [None]
    assert parse_llm_output("{\"a\":1}", 1) == [None]
    assert next_range_start(T0, None) is None
    # 09:00 → 下一次 01:00 是 16 小时后；处于区间内返回自身
    assert next_range_start(T0, (60, 480)) == pytest.approx(T0 + 16 * 3600)
    assert next_range_start(T0 - 6 * 3600, (60, 480)) == T0 - 6 * 3600
    assert next_range_start(T0, (1380, 60)) == pytest.approx(T0 + 14 * 3600)


class TestCommands:
    def test_status_why_off_on_mute(self, env):
        env.post("发烧了", at=T0 - 10)
        env.run()
        run = lambda msg: asyncio.run(care_cmd.handle(env.care, env.coord, SCOPE, msg, clock=env.clock))  # noqa: E731
        st = run("/iris_care status")
        assert "关心桥" in st and "发烧" not in st
        why = run("/iris_care why 3")
        assert "类别=sick" in why and "发烧" not in why
        assert "关闭" in run("/iris_care off purge")
        assert env.care.is_off() and env.signals()[0]["gist"] == ""
        assert "开启" in run("/iris_care on")
        assert not env.care.is_off()
        assert "静音" in run("/iris_care mute 身体")
        assert env.care.cs.pref()["muted"] == ["sick"]
        assert "取消静音" in run("/iris_care unmute sick")
        assert run("/iris_care mute") == care_cmd.HELP
        assert run("/iris_care mute nope") == care_cmd.HELP
        assert run("/iris_care whatever") == care_cmd.HELP
        assert "口头夸张" in run("/iris_care clear-hyperbole")
        assert "最近没有主动决策" in run("/iris_care proactive")
        assert asyncio.run(care_cmd.handle(None, None, SCOPE, "status")) == "关心桥未启用。"
        assert asyncio.run(care_cmd.handle(env.care, None, SCOPE, "proactive")) == "主动候选池未启用。"
        assert care_cmd.parse_args("/iris_care why 2") == ["why", "2"]

    def test_mute_suppresses_only_that_category(self, env):
        env.post("发烧了", at=T0 - 20)
        env.post("拿到offer了", at=T0 - 10, chat=GROUP2)
        env.run()
        assert len(env.cands()) == 2
        env.care.mute(SCOPE, "sick", True)
        left = env.cands()
        assert len(left) == 1 and "cat:good_news" in left[0]["source_ref"]

    def test_proactive_listing(self, env):
        env.post("发烧了", at=T0 - 10)
        env.run()
        _send_care(env)
        asyncio.run(env.coord.note_turn_end("t1", "sent", ""))
        out = asyncio.run(care_cmd.handle(env.care, env.coord, SCOPE, "proactive 3", clock=env.clock))
        assert "type=group_care" in out and "decision=speak" in out


def test_care_schema_independent_of_user_version(env):
    v = env.store.get_schema_version()
    cs.ensure_care_schema(env.store)
    assert env.store.get_schema_version() == v


def test_queued_care_not_offered_when_care_turned_off(env):
    env.post("发烧了", at=T0 - 10)
    env.run()
    assert asyncio.run(env.coord.merge_candidates(SCOPE, T0, []))
    env.conf["lifelike_care_bridge_mode"] = "shadow"
    assert asyncio.run(env.coord.merge_candidates(SCOPE, T0, [])) == []
    env.conf["lifelike_care_bridge_mode"] = "on"
    env.care.cs.set_pref(off=True)
    assert asyncio.run(env.coord.merge_candidates(SCOPE, T0, [])) == []


def test_episode_upgrade_passive_to_push(env):
    env.post("有点郁闷", at=T0 - 1000)
    env.run()
    c = env.cands()[0]
    assert c["delivery"] == "passive_only"  # mood_low 轻 → 顺口提
    env.clock.t = T0 + 600
    env.post("心情不好，一直想哭", at=T0 + 500)
    env.run()
    c = env.cands()[0]
    assert c["delivery"] == "push" and len(env.cands()) == 1
    assert env.signals()[0]["delivery"] == "push"
