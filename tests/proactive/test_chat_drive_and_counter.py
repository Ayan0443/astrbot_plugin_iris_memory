"""聊天欲望档位（lifelike_chat_drive）与被恶意对待时的反击（lifelike_counter_mode）。"""

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_iris_memory.iris_memory.proactive import drive as dv
from astrbot_plugin_iris_memory.iris_memory.proactive import hostility as hs
from astrbot_plugin_iris_memory.iris_memory.proactive import prompts as P
from astrbot_plugin_iris_memory.iris_memory.proactive.decision import DecisionCore, DecisionRequest
from astrbot_plugin_iris_memory.iris_memory.proactive.perception import ContextPackager, SlidingWindow
from astrbot_plugin_iris_memory.iris_memory.proactive.state import StateManager

import asyncio

from .test_u9_group_freedom import FAST, GID, OWNER, ON, _Ev, _plugin, _Res, _speak_stub

SNAP = Path(__file__).resolve().parents[1] / "lifelike_eval" / "snapshots" / "prompts_3c6e6e6.json"


# ===================================================================== 档位


class TestDrive:
    def test_normalize(self):
        assert dv.normalize(None) == "off" and dv.normalize("MEDIUM") == "medium"
        assert dv.normalize("strong") == "high" and dv.normalize("中等") == "medium" and dv.normalize("?") == "off"

    def test_cadence_and_caps(self):
        assert dv.private_eval_minutes("off", 20) == 20 and dv.private_eval_minutes("medium", 20) == 15
        assert dv.private_eval_minutes("high", 20) == 12 and dv.private_eval_minutes("medium", 10) == 10
        assert dv.group_quiet_minutes("medium", 45) == 30 and dv.group_eval_minutes("medium", 45) == 30
        assert dv.private_cooldown_cap("off") is None and dv.private_cooldown_cap("medium") == 30
        assert dv.group_pause_cap("off") == 15 and dv.group_pause_cap("medium") == 5

    def test_off_prompts_equal_3c6e6e6(self):
        snap = json.loads(SNAP.read_text(encoding="utf-8"))
        assert P.private_free_system("off") == snap["PRIVATE_DECISION_SYSTEM_FREE"]
        assert P.private_free_instruction("off") == snap["PRIVATE_INITIATE_INSTRUCTION_FREE"]
        assert P.private_free_system("bogus") == snap["PRIVATE_DECISION_SYSTEM_FREE"]

    def test_tiers_increase_willingness_text_only(self):
        low, med, high = (P.private_free_system(d) for d in ("low", "medium", "high"))
        assert "挺喜欢" in low and "拿不准自己有没有新鲜的话要说" in low
        assert "常常就会说一句" in med and "不为了刷存在感" in med and "拿不准自己有没有新鲜" not in med
        assert "大多数时候倾向于开口" in high
        for text in (low, med, high, P.group_free_system("medium"), P.group_free_system("high")):
            for word in ("概率强制", "%", "分值", "好感"):
                assert word not in text
            assert "不要催" in text or "不要刷屏" in text  # 防催促 / 刷屏的话仍在
        assert "最多 30" in P.private_free_instruction("medium", 30)

    def test_decision_uses_drive(self, nm_config):
        config = nm_config()
        state = StateManager(config)
        core = DecisionCore(config, state, SlidingWindow(config), ContextPackager(config))
        base = dict(group_id=f"private:{OWNER}", wake="timer", motive="initiate", private=True,
                    freedom_facts="<facts>\n- 现在：x\n</facts>")
        off = core.build_prompt(DecisionRequest(**base))
        med = core.build_prompt(DecisionRequest(**base, chat_drive="medium", self_pause_cap=30))
        snap = json.loads(SNAP.read_text(encoding="utf-8"))
        assert off[1] == snap["PRIVATE_DECISION_SYSTEM_FREE"]
        assert snap["PRIVATE_INITIATE_INSTRUCTION_FREE"] in off[0]
        assert "常常就会说一句" in med[1] and "最多 30" in med[0]


# ===================================================================== 反击：初筛


# 审查 H1 误判回归集：这些都不能触发反击（none 或只是 maybe）
NOT_COUNTER = [
    "我先滚了 晚安", "滚去睡觉", "打滚", "滚滚长江东逝水", "这更新就这？", "这什么玩意儿 好可爱",
    "笑到去死", "累得想去死", "他嘴好贱", "神经病啊这游戏", "这游戏真垃圾", "垃圾分类好麻烦",
    "你配合一下", "死机了", "笨蛋小白", "你这个小笨蛋", "滚动条坏了", "他真是个废物",
]


class TestClassify:
    @pytest.mark.parametrize("text", [
        "你个废物AI，什么都不会", "滚吧，没人想听你说话", "你算什么东西也配插嘴", "你就是个程序而已，装什么",
        "没人会喜欢你的", "你真恶心", "小白你个垃圾", "滚", "人工智障别装了",
    ])
    def test_hostile(self, text):
        assert hs.classify(text) == hs.LEVEL_HOSTILE

    @pytest.mark.parametrize("text", ["你个废物哈哈哈开玩笑的", "你滚啊哈哈哈", "你真恶心[狗头]"])
    def test_joke_marked_is_maybe(self, text):
        assert hs.classify(text) == hs.LEVEL_MAYBE

    @pytest.mark.parametrize("text", ["小白你看这人是不是废物", "@她 你配个图", "你知道吗 我同事真是个废物"])
    def test_second_person_must_be_direct(self, text):
        assert hs.classify(text) != hs.LEVEL_HOSTILE

    def test_short_stupid(self):
        assert hs.classify("小白你好蠢") == hs.LEVEL_HOSTILE
        assert hs.classify("这题好蠢") != hs.LEVEL_HOSTILE

    @pytest.mark.parametrize("text", NOT_COUNTER)
    def test_not_hostile(self, text):
        assert hs.classify(text) != hs.LEVEL_HOSTILE

    def test_rounds_and_group_cap(self):
        assert [hs.round_plan(n, 3) for n in range(5)] == ["reply", "reply", "last", "ignore", "ignore"]
        tr = hs.CounterTracker()
        tr.note(GID, "u9", 1000.0)
        tr.note(GID, "u8", 1100.0)
        assert tr.rounds(GID, "u9", 1200.0) == 1 and tr.group_count(GID, 1200.0) == 2
        assert tr.group_count(GID, 1100.0 + 601) == 0


# 复核 N1：正常句子不得被底线误删（完整底线和全局兜底都不删）
MUST_KEEP = [
    "该上床睡觉啦，晚安。", "你妈妈身体好点了吗？", "我妈的手艺超好。", "你住在哪个城市来着？",
    "你的电话是不是又没电了？", "人肉包子是武侠小说里的梗。", "你可以举报他，平台会处理的。",
    "精神病院的医生说要按时吃药。", "矮子里拔将军嘛。", "我是外地人，不太熟。", "废物利用也挺环保的。",
    "吓得我想去死哈哈。", "他神经病一样笑个不停。", "这就是个垃圾分类问题。", "去死亡谷玩过吗？",
    "明天见，走着瞧谁先起床。", "说话客气点，我又没招你。这么闲不如去干点正事？", "这游戏我滚动屏幕看了半天。",
    "神经科医生说没事。", "垃圾分类好麻烦。", "你这么说我不太舒服，别这样。",
    "睡你的觉吧，明天再说。", "这个bug真是废物代码写的。", "你去死海漂过吗？", "你是不是有病了？要不要看医生。",
]
# 反击回复必须拦的侮辱 / 脏话
MUST_BLOCK_FULL = [
    ("你才是废物", "CTR-INSULT"), ("白痴", "CTR-INSULT"), ("蠢货，闭嘴吧", "CTR-INSULT"), ("你有病吧", "CTR-INSULT"),
    ("滚", "CTR-INSULT"), ("神经病", "CTR-INSULT"), ("你算什么东西", "CTR-INSULT"),
    ("你这种人活该没朋友", "CTR-INSULT"), ("fuck off", "CTR-PROFANITY"), ("你妈的", "CTR-PROFANITY"),
    ("死胖子少说两句", "CTR-IDENTITY"), ("你给我等着", "CTR-THREAT"), ("我知道你住在哪", "CTR-DOX"),
    ("大家一起骂他", "CTR-INCITE"), ("约炮去吧", "CTR-SEXUAL"), ("you are an idiot", "CTR-PROFANITY"),
    ("你也配", "CTR-INSULT"), ("你不配", "CTR-INSULT"), ("你是不是有病", "CTR-INSULT"),
]
# 全局兜底（普通回复）只拦第二人称直接辱骂 / 脏话
MUST_BLOCK_NARROW = ["你才是废物", "你个白痴", "你算什么东西", "你给我滚", "你妈的", "fuck you", "you are an idiot"]


class TestGuard:
    @pytest.mark.parametrize("text", MUST_KEEP)
    def test_must_keep(self, text):
        assert hs.guard_reply(text) == (text, [])
        assert hs.guard_reply(text, fallback=False, narrow=True) == (text, [])

    @pytest.mark.parametrize("bad,rule", MUST_BLOCK_FULL)
    def test_full_blocks(self, bad, rule):
        out, ids = hs.guard_reply(f"你这话挺没意思的。{bad}。")
        assert rule in ids and bad not in out and "没意思" in out

    @pytest.mark.parametrize("bad", MUST_BLOCK_NARROW)
    def test_narrow_blocks_direct_only(self, bad):
        out, ids = hs.guard_reply(f"你这话挺没意思的。{bad}。", fallback=False, narrow=True)
        assert ids and bad not in out

    @pytest.mark.parametrize("text", ["白痴。", "死胖子少说两句。", "大家一起骂他。", "约炮去吧。", "我知道你住在哪。"])
    def test_narrow_skips_other_categories(self, text):
        assert hs.guard_reply(text, fallback=False, narrow=True) == (text, [])

    def test_all_removed(self):
        assert hs.guard_reply("你给我等着！")[0] == hs.FALLBACK_LINE
        assert hs.guard_reply("你个白痴。", fallback=False, narrow=True) == ("你个白痴。", ["CTR-DIRECT-INSULT"])


# ===================================================================== 反击：主插件


CTR = {**ON, "lifelike_counter_mode": "on"}
CTR_FAST = {**FAST, "lifelike_counter_mode": "on"}


class TestCounterMain:
    @pytest.mark.asyncio
    async def test_hostile_by_name_bypasses_debounce(self, nm_config, monkeypatch):
        p = _plugin(nm_config, hidden=CTR)
        calls = _speak_stub(p)
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        ev = _Ev("小白你个废物，闭嘴吧", sender="u9")
        await p.on_message(ev)
        assert ev.get_extra("iris_gf_wait")[3] == "hostile"
        assert ev.get_extra("iris_counter")["plan"] == "reply"
        await p.on_group_freedom_wait(ev)
        asyncio.sleep.assert_not_awaited()
        assert calls[0]["trigger"] == "hostile"
        hint = p._counter_hint(ev)
        assert "简短、有力" in hint and "不说脏话" in hint

    @pytest.mark.asyncio
    async def test_counter_off_is_normal(self, nm_config):
        p = _plugin(nm_config, hidden=ON)
        ev = _Ev("小白你个废物", sender="u9")
        await p.on_message(ev)
        assert ev.get_extra("iris_counter") is None and ev.get_extra("iris_gf_wait")[3] == "name"
        assert p._counter_hint(ev) == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", NOT_COUNTER)
    async def test_misjudge_set_never_counters(self, nm_config, text):
        p = _plugin(nm_config, hidden=CTR)
        p._gf.tracker.note_self(GID, time.time() - 30, unsolicited=True)  # 她刚说过话也不推断成冲她
        ev = _Ev(text, sender="u9")
        await p.on_message(ev)
        info = ev.get_extra("iris_counter") or {}
        assert info.get("level") != hs.LEVEL_HOSTILE
        wait = ev.get_extra("iris_gf_wait")
        assert wait is None or wait[3] != "hostile"
        assert "简短、有力" not in p._counter_hint(ev)

    @pytest.mark.asyncio
    async def test_maybe_addressed_is_neutral(self, nm_config):
        p = _plugin(nm_config, hidden=CTR)
        ev = _Ev("小白就这？", sender="u9", at=True)
        await p.on_message(ev)
        assert ev.get_extra("iris_counter")["plan"] == "neutral"
        assert p._counter_hint(ev) == P.COUNTER_NEUTRAL_HINT
        assert "玩笑" in P.COUNTER_NEUTRAL_HINT

    @pytest.mark.asyncio
    async def test_unaddressed_member_banter_ignored(self, nm_config):
        p = _plugin(nm_config, hidden=CTR)
        p._gf.tracker.note_self(GID, time.time() - 30, unsolicited=True)
        ev = _Ev("你个废物，打得太菜了", sender="u9")  # 群友之间互损，没点她
        await p.on_message(ev)
        assert ev.get_extra("iris_counter") is None and ev.get_extra("iris_gf_wait")[3] == "free"

    @pytest.mark.asyncio
    async def test_defend_owner_requires_reply_and_you(self, nm_config):
        p = _plugin(nm_config, hidden=CTR)
        ev = _Ev("小明就是个废物", sender="u9")  # 只提到名字：不算
        await p.on_message(ev)
        assert ev.get_extra("iris_counter") is None
        ev2 = _Ev("你就是个废物", sender="u9", reply_to=OWNER)
        await p.on_message(ev2)
        info = ev2.get_extra("iris_counter")
        assert info["target"] == "owner" and "护着" in p._counter_hint(ev2)
        assert ev2.get_extra("iris_gf_wait")[3] == "hostile"

    @pytest.mark.asyncio
    async def test_rounds_then_ignore(self, nm_config):
        p = _plugin(nm_config, hidden=CTR)
        for _ in range(2):
            p._counter_tracker.note(GID, "u9", time.time() - 900)
        ev = _Ev("小白你个垃圾", sender="u9", at=True)
        await p.on_message(ev)
        assert ev.get_extra("iris_counter")["plan"] == "last"
        assert "一句话收尾" in p._counter_hint(ev)
        p._counter_tracker.note(GID, "u9", time.time() - 900)
        ev2 = _Ev("小白你个垃圾", sender="u9", at=True)
        await p.on_message(ev2)
        assert ev2.get_extra("iris_counter_ignore") is True and ev2.get_extra("iris_mode") is None

    @pytest.mark.asyncio
    async def test_group_cap(self, nm_config):
        """审查 M5：每群 10 分钟内反击总次数上限（换人刷也挡得住）"""
        p = _plugin(nm_config, hidden=CTR)
        for who in ("a", "b", "c"):
            p._counter_tracker.note(GID, who)
        ev = _Ev("小白你个垃圾", sender="d", at=True)
        await p.on_message(ev)
        assert ev.get_extra("iris_counter_ignore") is True

    @pytest.mark.asyncio
    async def test_hostile_skipped_when_hushed(self, nm_config, monkeypatch):
        p = _plugin(nm_config, hidden=CTR)
        calls = _speak_stub(p)
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        p._state.set_proactive_hush(GID, time.time() + 3600)
        ev = _Ev("小白你个废物", sender="u9")
        await p.on_message(ev)
        await p.on_group_freedom_wait(ev)
        assert calls == []

    @pytest.mark.asyncio
    async def test_hostile_queues_while_busy(self, nm_config, monkeypatch):
        p = _plugin(nm_config, hidden=CTR)
        calls = _speak_stub(p)
        p._reply_in_progress[GID] = time.time()

        async def fake_sleep(s):
            p._reply_in_progress.pop(GID, None)  # 当前回复结束

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        ev = _Ev("小白你个废物", sender="u9")
        await p.on_message(ev)
        await p.on_group_freedom_wait(ev)
        assert calls and calls[0]["trigger"] == "hostile"

    @pytest.mark.asyncio
    async def test_owner_harsh_goes_soft_path(self, nm_config):
        p = _plugin(nm_config, hidden=CTR)
        ev = _Ev("小白你真是个废物", sender=OWNER)
        await p.on_message(ev)
        assert ev.get_extra("iris_counter")["plan"] == "owner"
        assert p._counter_hint(ev) == P.OWNER_HARSH_HINT

    def test_private_owner_hint_only_when_toward_her(self, nm_config):
        p = _plugin(nm_config, hidden=CTR)
        assert p._counter_hint(_Ev("你真是个废物", sender=OWNER, group="")) == P.OWNER_HARSH_HINT
        assert p._counter_hint(_Ev("今天好累", sender=OWNER, group="")) == ""
        assert p._counter_hint(_Ev("我们组长真是个废物", sender=OWNER, group="")) == ""  # 审查 M4：骂第三方
        assert p._counter_hint(_Ev("你知道吗 我同事真是个废物", sender=OWNER, group="")) == ""  # 复核 N2

    def test_guard_modes(self, nm_config):
        p = _plugin(nm_config, hidden=CTR)
        ev = _Ev("x")
        ev.set_extra("iris_counter", {"plan": "reply", "level": "hostile", "target": "her"})
        res = _Res("嘴巴放干净点。你妈的。")
        assert p._counter_guard(ev, res) is False and res.chain[0].text == "嘴巴放干净点。"
        res_b = _Res("约炮去吧。")
        p._counter_guard(ev, res_b)
        assert res_b.chain[0].text == hs.FALLBACK_LINE  # 反击回复删空 → 一句收尾
        # 普通私聊回复：只删第二人称直接辱骂那句；别的类别不查
        ev2 = _Ev("x", group="")
        res2 = _Res("今天辛苦了。你个白痴。")
        p._counter_guard(ev2, res2)
        assert res2.chain[0].text == "今天辛苦了。" and not ev2.stopped
        res3 = _Res("该上床睡觉啦，晚安。")
        p._counter_guard(ev2, res3)
        assert res3.chain[0].text == "该上床睡觉啦，晚安。"
        # 普通回复整段命中：退回原文，绝不静默不发
        res4 = _Res("你个白痴。")
        assert p._counter_guard(ev2, res4) is False and res4.chain[0].text == "你个白痴。" and not ev2.stopped
        # 关闭不变
        p3 = _plugin(nm_config, hidden=ON)
        res5 = _Res("你个白痴。")
        assert p3._counter_guard(_Ev("x"), res5) is False and res5.chain[0].text == "你个白痴。"

    @pytest.mark.parametrize("text", MUST_KEEP)
    def test_normal_replies_never_touched(self, nm_config, text):
        p = _plugin(nm_config, hidden=CTR)
        res = _Res(text)
        p._counter_guard(_Ev("x"), res)
        assert res.chain[0].text == text

    def test_recent_hostile_sender_gets_narrow_only(self, nm_config):
        """最终复核：回击过的人之后的普通消息走全局兜底，不用完整底线、不换收尾句"""
        p = _plugin(nm_config, hidden=CTR)
        p._counter_tracker.note(GID, "u9")
        res = _Res("别冲我来。约炮去吧。")
        p._counter_guard(_Ev("x", sender="u9"), res)
        assert res.chain[0].text == "别冲我来。约炮去吧。"
        res2 = _Res("你个白痴。")
        p._counter_guard(_Ev("x", sender="u9"), res2)
        assert res2.chain[0].text == "你个白痴。"  # 删空退回原文

    def test_full_guard_needs_target(self, nm_config):
        p = _plugin(nm_config, hidden=CTR)
        ev = _Ev("x")
        ev.set_extra("iris_counter", {"plan": "reply", "level": "hostile", "target": "her"})
        assert p._counter_full_guard(ev) is True
        ev2 = _Ev("x")
        ev2.set_extra("iris_counter", {"plan": "neutral", "level": "maybe", "target": "her"})
        assert p._counter_full_guard(ev2) is False

    @pytest.mark.asyncio
    async def test_watchdog_task_reference_kept(self, nm_config):
        p = _plugin(nm_config, hidden=ON)
        p._gf_watchdogs = set()

        async def speak(event):
            p._release_decision_ticket(GID)
            p._reply_in_progress[GID] = 1.0
            return False

        p._handle_reply_decision = speak
        await p._gf_activate(_Ev("xiaobai 在吗"), GID, trigger="name", sender_id="u1", unsolicited=False)
        assert len(p._gf_watchdogs) == 1
        for t in list(p._gf_watchdogs):
            t.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not p._gf_watchdogs

    def test_counter_reply_not_blocked_by_unsolicited_nets(self, nm_config):
        p = _plugin(nm_config, hidden=CTR)
        ev = _Ev("x")
        ev.set_extra("iris_counter", {"plan": "reply"})
        assert p._gf_reply_blocked(ev, _Res("别以为我好欺负，说话客气点")) is False

    def test_counter_reply_counts_into_floor(self, nm_config):
        p = _plugin(nm_config, hidden=CTR)
        ev = _Ev("x")
        ev.set_extra("iris_counter", {"plan": "reply", "level": "hostile", "sender": "u9"})
        p._gf_note_reply_sent(ev, GID, "说话客气点")
        assert p._gf.tracker.floor_left(GID, p._gf.cfg(), time.time()) > 0

    @pytest.mark.asyncio
    async def test_ignore_stops_in_llm_request(self, nm_config):
        p = _plugin(nm_config, hidden=CTR)
        p._bypass_stripper = None
        p._bypass_missing_warned = True
        p._apply_error_friendly_extra = Mock()
        p._handle_reply_decision = AsyncMock(return_value=False)
        ev = _Ev("x", at=True)
        ev.set_extra("iris_counter_ignore", True)
        await p.on_llm_request(ev, SimpleNamespace(extra_user_content_parts=[]))
        assert ev.stopped is True


class TestStyleNoCuteWhenHostile:
    def test_cute_off(self, monkeypatch):
        from astrbot_plugin_iris_memory.iris_memory.style import selector as S

        vals = {"lifelike_counter_mode": "on"}
        monkeypatch.setattr(S, "_cfg_get", lambda k, d: vals.get(k, d))
        assert S._counter_hostile("你个废物") is True
        vals["lifelike_counter_mode"] = "off"
        assert S._counter_hostile("你个废物") is False


class TestReplyRelease:
    @pytest.mark.asyncio
    async def test_exception_releases_claim(self, nm_config, monkeypatch):
        p = _plugin(nm_config, hidden=ON)

        async def boom(event):
            raise RuntimeError("x")

        p._handle_reply_decision = boom
        ev = _Ev("xiaobai 在吗")
        await p._gf_activate(ev, GID, trigger="name", sender_id="u1", unsolicited=False)
        assert GID not in p._triggering and ev.stopped

    @pytest.mark.asyncio
    async def test_watchdog_releases_stuck_reply(self, nm_config, monkeypatch):
        import astrbot_plugin_iris_memory.main as M

        p = _plugin(nm_config, hidden=ON)
        monkeypatch.setattr(M, "_GF_REPLY_TIMEOUT", 0.0)
        p._reply_in_progress[GID] = 123.0
        await p._gf_reply_watchdog(GID, 123.0)
        assert GID not in p._reply_in_progress
        p._reply_in_progress[GID] = 456.0  # 新一轮回复不受旧看门狗影响
        await p._gf_reply_watchdog(GID, 123.0)
        assert p._reply_in_progress[GID] == 456.0
