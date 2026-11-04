"""U19 main.py 接线：使用意愿 prompt / 档位、[贴纸:标签] 与近似标签、论坛话题、60 秒底线、
主动消息配贴纸、只学主人私聊贴纸（不学群）、场合概括、别学这个、/sticker learn；两个开关 off = 676cd8a。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_iris_memory.iris_memory.stickers.library import StickerLibrary
from astrbot_plugin_iris_memory.iris_memory.stickers.understanding import StickerMeta
from tests.stickers.test_library import fake_doc, fake_msg

from .test_integration_main import GROUP, OWNER, SCOPE, FakeEvent, FakeResult, plugin  # noqa: F401


class FakeClient:
    def __init__(self):
        self.sent = []
        self.next_id = 7000

    async def send_file(self, peer, file=None, **kw):
        self.sent.append((peer, kw.get("reply_to")))
        self.next_id += 1
        return SimpleNamespace(id=self.next_id)


@pytest.fixture
def real(plugin, tmp_path):  # noqa: F811
    lib = StickerLibrary(tmp_path / "stk" / "stickers_library.db")
    plugin._sticker_library = lib
    plugin._sticker_understanding = None
    client = FakeClient()
    inst = plugin.context.get_platform_inst("userbot")
    inst.get_client = lambda: client
    plugin.config.values.update(
        {
            "continuity.sticker_send_enable": True,
            "continuity.sticker_groups_enable": True,
            "continuity.sticker_scope_unified": True,
        }
    )
    lib.add_from_message("开心", "", fake_msg(fake_doc(1)), OWNER, default_scope="all")
    yield plugin, lib, client
    lib.close()


def _on(p, use=True, learn=True):
    if use:
        p.config.values["lifelike_sticker_use"] = "on"
    if learn:
        p.config.values["lifelike_sticker_auto_learn"] = "on"


def _resp(text):
    return SimpleNamespace(completion_text=text, usage=None)


async def _drain(p):
    for t in list(getattr(p, "_sticker_learn_tasks", None) or ()):
        await t


# --------------------------------------------------------------------------- off 回归


@pytest.mark.asyncio
async def test_off_prompt_and_markers_unchanged(real):
    p, lib, client = real
    req = SimpleNamespace(system_prompt="base")
    await p._inject_sticker_prompt(FakeEvent(text="hi", msg_id=1), req)
    assert "主人给你准备了下面这些表情包" in req.system_prompt  # 旧措辞
    assert "主人也常用" not in req.system_prompt
    # [贴纸:开心] 不认、原样
    ev = FakeEvent(text="hi", msg_id=2)
    resp = _resp("好呀[贴纸：开心]")
    await p.on_llm_response(ev, resp)
    assert resp.completion_text == "好呀[贴纸：开心]"
    assert ev.extras.get("iris_sticker_pending") is None
    # 论坛话题不发
    ev2 = FakeEvent(text="hi", sender="123", group_id=GROUP + "#7", msg_id=3)
    assert (await p._sticker_entries(ev2)) == (None, [])


@pytest.mark.asyncio
async def test_off_owner_sticker_not_learned_or_counted(real):
    p, lib, _ = real
    ev = FakeEvent(text="[贴纸 😄：一只猫大笑]", msg_id=10)
    raw = fake_msg(fake_doc(500, emoji="😄"), msg_id=10)
    meta = StickerMeta(doc_id=500, emoji="😄", kind="static", mime_type="image/webp")
    await p._sticker_owner_observe(ev, raw, meta, "一只猫大笑")
    await _drain(p)
    assert lib.learned_list() == []
    assert lib.owner_activity(7) == (0, 0)


# --------------------------------------------------------------------------- 使用意愿


@pytest.mark.asyncio
async def test_use_on_prompt_lists_occasions_and_tier(real):
    p, lib, _ = real
    _on(p)
    p.config.values["lifelike_chat_drive"] = "medium"
    lib.learn_add(fake_msg(fake_doc(2), msg_id=2), ["晚安"], "道晚安、要睡觉的时候", occasion="道晚安时")
    req = SimpleNamespace(system_prompt="base")
    await p._inject_sticker_prompt(FakeEvent(text="hi", msg_id=20), req)
    block = req.system_prompt
    assert "主人也常用这些贴纸" in block and "合适的时候就发一张" in block
    assert "主人自己平时" not in block  # 没有主人数据，不编造主人习惯
    assert "晚安：道晚安、要睡觉的时候（主人常在道晚安时用）" in block
    assert not any(ch.isdigit() for ch in block[len("base"):])
    # 群里：不出现私聊画像
    greq = SimpleNamespace(system_prompt="base")
    await p._inject_sticker_prompt(FakeEvent(text="hi", sender="1", group_id=GROUP, msg_id=21), greq)
    assert "晚安" in greq.system_prompt and "主人常在" not in greq.system_prompt
    assert "主人也常用" not in greq.system_prompt
    # 反击回复：不给表情说明
    creq = SimpleNamespace(system_prompt="base")
    cev = FakeEvent(text="hi", sender="1", group_id=GROUP, msg_id=22)
    cev.extras["iris_counter"] = {"level": "hostile"}
    await p._inject_sticker_prompt(cev, creq)
    assert creq.system_prompt == "base"


@pytest.mark.asyncio
async def test_tier_follows_owner_sticker_ratio(real):
    p, lib, _ = real
    _on(p)
    p.config.values["lifelike_chat_drive"] = "off"
    for i in range(30):
        lib.note_owner_message(i % 2 == 0)
    assert await p._sticker_tier() == ("经常", True)
    p.config.values["lifelike_chat_drive"] = "high"
    for _ in range(200):
        lib.note_owner_message(False)
    assert await p._sticker_tier() == ("很少", True)  # 主人很少用 → 很少，drive 不往上抬


@pytest.mark.asyncio
async def test_use_on_accepts_tiezhi_and_fuzzy_label(real):
    p, lib, client = real
    _on(p)
    ev = FakeEvent(text="hi", msg_id=30)
    resp = _resp("好呀\n[贴纸：开心]")
    await p.on_llm_response(ev, resp)
    assert resp.completion_text == "好呀"
    assert ev.extras.get("iris_sticker_pending") == "开心"
    evr = FakeEvent(text="hi", msg_id=32)
    retell = _resp("你刚发的是\n[贴纸：开心]")
    await p.on_llm_response(evr, retell)
    assert retell.completion_text == "你刚发的是\n[贴纸：开心]" and evr.extras.get("iris_sticker_pending") is None
    evn = FakeEvent(text="hi", msg_id=33)
    neg = _resp("唉[表情:开心不起来]")
    await p.on_llm_response(evn, neg)
    assert evn.extras.get("iris_sticker_pending") is None
    ev2 = FakeEvent(text="hi", msg_id=31)
    resp2 = _resp("好呀[表情:开心的]")
    await p.on_llm_response(ev2, resp2)
    assert resp2.completion_text == "好呀" and ev2.extras.get("iris_sticker_pending") == "开心"


@pytest.mark.asyncio
async def test_use_on_sends_after_receipt_with_60s_floor(real):
    p, lib, client = real
    _on(p)
    ev = FakeEvent(text="hi", msg_id=40)
    await p.on_llm_response(ev, _resp("好呀[表情:开心]"))
    ev.result = FakeResult("好呀")
    ev.extras["telethon_sent_message_ids"] = [9001]
    await p.on_message_sent(ev)
    assert client.sent == [(int(OWNER), None)]
    # 60 秒内第二张不发（防故障底线）
    ev2 = FakeEvent(text="hi", msg_id=41)
    await p.on_llm_response(ev2, _resp("再来[表情:开心]"))
    ev2.result = FakeResult("再来")
    ev2.extras["telethon_sent_message_ids"] = [9002]
    await p.on_message_sent(ev2)
    assert len(client.sent) == 1
    # 超过 60 秒（但不到旧的 120 秒）可以发
    lib._conn().execute("UPDATE sends SET created_at=created_at-61")
    ev3 = FakeEvent(text="hi", msg_id=42)
    await p.on_llm_response(ev3, _resp("又来[表情:开心]"))
    ev3.result = FakeResult("又来")
    ev3.extras["telethon_sent_message_ids"] = [9003]
    await p.on_message_sent(ev3)
    assert len(client.sent) == 2


@pytest.mark.asyncio
async def test_use_on_forum_topic_sends_into_topic(real):
    p, lib, client = real
    _on(p)
    gid = GROUP + "#77"
    # 不是论坛话题（拿不到 forum 标识）的线程：不发
    ev0 = FakeEvent(text="hi", sender="123", group_id=gid, msg_id=49)
    await p.on_llm_response(ev0, _resp("哈哈[表情:开心]"))
    assert ev0.extras.get("iris_sticker_pending") is None
    ev = FakeEvent(text="hi", sender="123", group_id=gid, msg_id=50)
    ev.message_obj.raw_message.reply_to = SimpleNamespace(forum_topic=True, reply_to_msg_id=77)
    await p.on_llm_response(ev, _resp("哈哈[表情:开心]"))
    assert ev.extras.get("iris_sticker_pending") == "开心"
    ev.result = FakeResult("哈哈")
    ev.extras["telethon_sent_message_ids"] = [9100]
    await p.on_message_sent(ev)
    assert client.sent == [(int(GROUP), 77)]
    # 60 秒底线按群计（去掉 #话题后缀）：同群另一个话题 / 主会话都不发
    ev2 = FakeEvent(text="hi", sender="123", group_id=GROUP, msg_id=51)
    await p.on_llm_response(ev2, _resp("嘿[表情:开心]"))
    ev2.result = FakeResult("嘿")
    ev2.extras["telethon_sent_message_ids"] = [9101]
    await p.on_message_sent(ev2)
    assert len(client.sent) == 1


@pytest.mark.asyncio
async def test_private_streak_floor(real):
    p, lib, client = real
    _on(p)

    async def reply(i, text):
        lib._conn().execute("UPDATE sends SET created_at=created_at-100")
        ev = FakeEvent(text="hi", msg_id=300 + i)
        await p.on_llm_response(ev, _resp(text))
        ev.result = FakeResult("x")
        ev.extras["telethon_sent_message_ids"] = [8000 + i]
        await p.on_message_sent(ev)

    await reply(1, "a[表情:开心]")
    await reply(2, "b[表情:开心]")
    await reply(3, "c[表情:开心]")  # 连续两条带贴纸后，这条不发
    assert len(client.sent) == 2
    await reply(4, "d[表情:开心]")
    assert len(client.sent) == 3
    await reply(5, "e")  # 不带贴纸的回复清零
    await reply(6, "f[表情:开心]")
    await reply(7, "g[表情:开心]")
    assert len(client.sent) == 5


@pytest.mark.asyncio
async def test_proactive_hooks(real):
    p, lib, client = real
    umo = f"userbot:FriendMessage:{OWNER}"
    assert await p._proactive_sticker_block(GROUP + "#5", umo) == ""
    # off：不注入、不剥离
    assert await p._proactive_sticker_block(SCOPE, umo) == ""
    assert await p._proactive_sticker_take(SCOPE, umo, "早[表情:开心]") == "早[表情:开心]"
    _on(p)
    block = await p._proactive_sticker_block(SCOPE, umo)
    assert "开心" in block
    clean = await p._proactive_sticker_take(SCOPE, umo, "早呀[表情:开心]")
    assert clean == "早呀"
    await p._on_initiate_sent(SCOPE, clean)
    assert client.sent == [(int(OWNER), None)]
    # 没发出去（新一轮生成会清掉暂存）
    await p._proactive_sticker_take(SCOPE, umo, "在吗[表情:开心]")
    await p._proactive_sticker_take(SCOPE, umo, "")
    lib._conn().execute("UPDATE sends SET created_at=created_at-100")
    await p._on_initiate_sent(SCOPE, "在吗")
    assert len(client.sent) == 1


# --------------------------------------------------------------------------- 自动学习


def _sticker_event(msg_id, doc_id, emoji="😄", sender=OWNER, group_id=""):
    ev = FakeEvent(text=f"[贴纸 {emoji}：x]", msg_id=msg_id, sender=sender, group_id=group_id)
    raw = fake_msg(fake_doc(doc_id, emoji=emoji), msg_id=msg_id)
    meta = StickerMeta(doc_id=doc_id, emoji=emoji, kind="static", mime_type="image/webp")
    return ev, raw, meta


@pytest.mark.asyncio
async def test_learns_owner_private_sticker_with_occasion(real):
    p, lib, _ = real
    _on(p)
    # 主人先说了一句（只记成场合短语），再发贴纸
    await p._sticker_owner_observe(FakeEvent(text="哈哈哈笑死我了", msg_id=60), None, None, None)
    ev, raw, meta = _sticker_event(61, 600, "🤭")
    await p._sticker_owner_observe(ev, raw, meta, "动漫黑发少女用双手捂嘴轻笑")
    await _drain(p)
    e = lib.learned_by_doc(600)
    assert e is not None and e["label"] == "偷笑" and e["occasion"] == "被逗笑时"
    assert e["group_ok"] is True
    assert e["group_ok"] is True
    assert lib.owner_activity(7) == (2, 1)
    # 再用一次：次数 +1，不重复加
    ev2, raw2, meta2 = _sticker_event(62, 600, "🤭")
    await p._sticker_owner_observe(ev2, raw2, meta2, "动漫黑发少女用双手捂嘴轻笑")
    await _drain(p)
    assert len(lib.learned_list()) == 1 and lib.learned_by_doc(600)["owner_uses"] == 2


@pytest.mark.asyncio
async def test_occasion_from_next_owner_text_and_bot_praise(real):
    p, lib, _ = real
    _on(p)
    ev, raw, meta = _sticker_event(70, 700, "💤")
    await p._sticker_owner_observe(ev, raw, meta, "女孩闭眼熟睡，旁边写着「晚安」")
    await _drain(p)
    assert lib.learned_by_doc(700)["occasion"] == ""
    await p._sticker_owner_observe(FakeEvent(text="我去睡了", msg_id=71), None, None, None)
    assert lib.learned_by_doc(700)["occasion"] == "道晚安时"
    # 她夸了主人 → 主人发贴纸 → “被夸时”
    sent = FakeEvent(text="hi", msg_id=72)
    sent.result = FakeResult("你也太厉害了吧")
    sent.extras["telethon_sent_message_ids"] = [1]
    p._sticker_note_bot_reply(sent)
    ev2, raw2, meta2 = _sticker_event(73, 701, "☺️")
    await p._sticker_owner_observe(ev2, raw2, meta2, "角色脸颊泛红捂脸害羞")
    await _drain(p)
    assert lib.learned_by_doc(701)["occasion"] == "被夸时"
    # 判不出场合的一句会清空之前的场合；超过 60 秒的场合不再算
    await p._sticker_owner_observe(FakeEvent(text="哈哈哈", msg_id=74), None, None, None)
    await p._sticker_owner_observe(FakeEvent(text="今天吃了面", msg_id=75), None, None, None)
    ev3, raw3, meta3 = _sticker_event(76, 702, "🤨")
    await p._sticker_owner_observe(ev3, raw3, meta3, "动漫女孩皱眉，头顶有问号")
    await _drain(p)
    assert lib.learned_by_doc(702)["occasion"] == ""
    p._stk_ctx()[SCOPE]["last"] = (0.0, "被逗笑时")
    ev4, raw4, meta4 = _sticker_event(77, 703, "🤨")
    await p._sticker_owner_observe(ev4, raw4, meta4, "动漫女孩皱眉，头顶有问号")
    await _drain(p)
    assert lib.learned_by_doc(703)["occasion"] == ""


@pytest.mark.asyncio
async def test_forwarded_sticker_not_learned(real):
    p, lib, _ = real
    _on(p)
    ev, raw, meta = _sticker_event(78, 704, "😄")
    raw.fwd_from = SimpleNamespace(from_id=1)
    await p._sticker_owner_observe(ev, raw, meta, "一只猫大笑")
    await _drain(p)
    assert lib.learned_by_doc(704) is None


@pytest.mark.asyncio
async def test_never_learns_group_or_non_owner(real):
    p, lib, _ = real
    _on(p)
    ev, raw, meta = _sticker_event(80, 800, sender="123", group_id=GROUP)
    await p._sticker_owner_observe(ev, raw, meta, "一只猫大笑")
    ev2, raw2, meta2 = _sticker_event(81, 801, sender=OWNER, group_id=GROUP)  # 主人在群里发的也不学
    await p._sticker_owner_observe(ev2, raw2, meta2, "一只猫大笑")
    ev3, raw3, meta3 = _sticker_event(82, 802, sender="555")  # 别人私聊
    await p._sticker_owner_observe(ev3, raw3, meta3, "一只猫大笑")
    await _drain(p)
    assert lib.learned_list() == []
    assert lib.owner_activity(7) == (0, 0)


@pytest.mark.asyncio
async def test_unsafe_sticker_private_only_and_pause(real):
    p, lib, _ = real
    _on(p)
    ev, raw, meta = _sticker_event(90, 900, "😄")
    await p._sticker_owner_observe(ev, raw, meta, "动漫少女满脸血迹张嘴大笑")
    await _drain(p)
    e = lib.learned_by_doc(900)
    assert e is not None and e["group_ok"] is False
    _, grp = await p._sticker_entries(FakeEvent(text="hi", sender="1", group_id=GROUP, msg_id=91))
    assert e["label"] not in [x["label"] for x in grp]
    _, priv = await p._sticker_entries(FakeEvent(text="hi", msg_id=92))
    assert e["label"] in [x["label"] for x in priv]
    # 暂停后不学新的
    lib.set_learn_paused(True)
    ev2, raw2, meta2 = _sticker_event(93, 901, "🤨")
    await p._sticker_owner_observe(ev2, raw2, meta2, "女孩皱眉，头顶有问号")
    await _drain(p)
    assert lib.learned_by_doc(901) is None


@pytest.mark.asyncio
async def test_learn_off_hides_learned_entries(real):
    p, lib, _ = real
    _on(p)
    lib.learn_add(fake_msg(fake_doc(3), msg_id=3), ["晚安"], "x")
    _, entries = await p._sticker_entries(FakeEvent(text="hi", msg_id=95))
    assert "晚安" in [e["label"] for e in entries]
    p.config.values["lifelike_sticker_auto_learn"] = "off"
    _, entries = await p._sticker_entries(FakeEvent(text="hi", msg_id=96))
    assert [e["label"] for e in entries] == ["开心"]


@pytest.mark.asyncio
async def test_dont_learn_reply_tombstones(real):
    p, lib, _ = real
    _on(p)
    ev, raw, meta = _sticker_event(100, 1000, "🤨")
    await p._sticker_owner_observe(ev, raw, meta, "女孩皱眉，头顶有问号")
    await _drain(p)
    assert lib.learned_by_doc(1000) is not None
    say = FakeEvent(text="别学这个", msg_id=101)
    say.message_obj.raw_message.reply_to = SimpleNamespace(reply_to_msg_id=100)
    say.message_obj.raw_message.get_reply_message = AsyncMock(return_value=fake_msg(fake_doc(1000)))
    await p._sticker_owner_observe(say, say.message_obj.raw_message, None, None)
    assert lib.learned_by_doc(1000) is None and lib.is_tombstoned(1000)
    assert say.extras.get("iris_sticker_dont_learn") == "done"
    ev2, raw2, meta2 = _sticker_event(102, 1000, "🤨")
    await p._sticker_owner_observe(ev2, raw2, meta2, "女孩皱眉，头顶有问号")
    await _drain(p)
    assert lib.learned_by_doc(1000) is None
    # 没学过的贴纸说“别学这个”：记墓碑，提示用“以后不学它”
    say2 = FakeEvent(text="这个别用", msg_id=103)
    say2.message_obj.raw_message.reply_to = SimpleNamespace(reply_to_msg_id=99)
    say2.message_obj.raw_message.get_reply_message = AsyncMock(return_value=fake_msg(fake_doc(1001)))
    await p._sticker_owner_observe(say2, say2.message_obj.raw_message, None, None)
    assert say2.extras.get("iris_sticker_dont_learn") == "never" and lib.is_tombstoned(1001)


@pytest.mark.asyncio
async def test_label_llm_fallback_uses_budget(real, monkeypatch):
    p, lib, _ = real
    _on(p)
    calls = []

    async def gen(prompt, module=None, provider_id=None, timeout=None):
        calls.append(module)
        assert "画面描述" in prompt
        return '{"label": "摸鱼", "note": "偷懒的时候"}'

    p._llm_manager = SimpleNamespace(generate=gen)
    ev, raw, meta = _sticker_event(110, 1100, "🚬")
    await p._sticker_owner_observe(ev, raw, meta, "金发女孩站着手持冒烟细长物，神情平静")
    await _drain(p)
    e = lib.learned_by_doc(1100)
    assert e is not None and e["label"] == "摸鱼" and e["note"] == "偷懒的时候"
    assert calls == ["lifelike_sticker_learn"]
    counters = p._continuity.store.get_counters()
    assert int(counters.get("lifelike.budget.calls:lifelike_sticker_learn", 0)) == 1


@pytest.mark.asyncio
async def test_sticker_cmd_learning_listing(real):
    p, lib, _ = real
    lib.learn_add(fake_msg(fake_doc(4), msg_id=4), ["晚安"], "道晚安的时候", occasion="道晚安时")

    ev = FakeEvent(text="/sticker list", msg_id=120)
    ev.plain_result = lambda t: t
    outs = [r async for r in p.sticker_cmd(ev)]
    assert "晚安" not in outs[0]  # 开关 off：与原来一致
    _on(p)
    outs = [r async for r in p.sticker_cmd(ev)]
    assert "晚安" in outs[0] and "自动" in outs[0] and "道晚安时" in outs[0]
