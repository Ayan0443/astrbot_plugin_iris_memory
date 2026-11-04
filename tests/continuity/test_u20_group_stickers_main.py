"""U20 main.py：学主人在白名单群里发的贴纸（只学主人本人，不学群友 / 转发 / 频道 / 非资格群），
群里场合短语、按场景计数、群聊档位用主人群里频率；开关 off = c3018c1。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_iris_memory.iris_memory.stickers.understanding import StickerMeta
from tests.stickers.test_library import fake_doc, fake_msg

from .test_integration_main import CHANNEL, GROUP, OWNER, SCOPE, FakeEvent, plugin  # noqa: F401
from .test_u19_stickers_main import _drain, _on, real  # noqa: F401

SELF = "7884366624"


def _groups_on(p, eligible=(GROUP,)):
    _on(p)
    p.config.values["lifelike_sticker_learn_groups"] = "on"
    p._gf_eligible = lambda gid: str(gid).split("#", 1)[0] in set(eligible)
    p._self_id = SELF


def _gsticker(msg_id, doc_id, emoji="😄", sender=OWNER, group_id=GROUP, reply_from=None, mention=False):
    ev = FakeEvent(text=f"[贴纸 {emoji}：x]", msg_id=msg_id, sender=sender, group_id=group_id)
    raw = fake_msg(fake_doc(doc_id, emoji=emoji), chat_id=int(group_id.split("#")[0]), msg_id=msg_id)
    raw.reply_to = None
    if reply_from is not None:
        ents = [SimpleNamespace(user_id=int(OWNER))] if mention else []
        if mention:
            ents = [type("MessageEntityMentionName", (), {"user_id": int(OWNER)})()]
        raw.reply_to = SimpleNamespace(reply_to_msg_id=1, forum_topic=False)
        raw.get_reply_message = AsyncMock(return_value=SimpleNamespace(sender_id=int(reply_from), entities=ents))
    ev.message_obj.raw_message = raw
    meta = StickerMeta(doc_id=doc_id, emoji=emoji, kind="static", mime_type="image/webp")
    return ev, raw, meta


def _gtext(msg_id, text, sender=OWNER, group_id=GROUP):
    return FakeEvent(text=text, msg_id=msg_id, sender=sender, group_id=group_id)


@pytest.mark.asyncio
async def test_off_group_sticker_not_learned(real):
    p, lib, _ = real
    _on(p)  # U19 两个开关开着，但 U20 关
    p._gf_eligible = lambda gid: True
    ev, raw, meta = _gsticker(1, 100)
    await p._sticker_owner_observe(ev, raw, meta, "动漫女孩大笑")
    await _drain(p)
    assert lib.learned_list() == []
    assert lib.owner_activity_scene(f"group:{GROUP}", 7) == (0, 0)
    assert lib.owner_activity(7) == (0, 0)


@pytest.mark.asyncio
async def test_learns_owner_group_sticker_with_occasion(real):
    p, lib, _ = real
    _groups_on(p)
    await p._sticker_owner_observe(_gtext(2, "哈哈哈哈"), None, None, None)
    ev, raw, meta = _gsticker(3, 200, "🤭", reply_from="555")
    await p._sticker_owner_observe(ev, raw, meta, "动漫少女捂嘴偷笑")
    await _drain(p)
    e = lib.learned_by_doc(200)
    assert e is not None and e["learned_in"] == "group" and e["group_occasion"] == "接别人玩笑时"
    assert e["occasion"] == "" and e["group_ok"] is True
    assert lib.owner_activity_scene(f"group:{GROUP}", 7) == (2, 1)
    assert lib.owner_activity(7) == (0, 0)  # 私聊计数不受群影响
    assert lib.group_use_get(200, GROUP)["occasion"] == "接别人玩笑时"
    # 回复她 / 被 @ / 附和
    ev2, raw2, meta2 = _gsticker(4, 201, "👍", reply_from=SELF)
    await p._sticker_owner_observe(ev2, raw2, meta2, "卡通小熊竖起大拇指")
    ev3, raw3, meta3 = _gsticker(5, 202, "😮", reply_from="555", mention=True)
    await p._sticker_owner_observe(ev3, raw3, meta3, "动漫女孩睁大眼惊讶")
    await _drain(p)
    assert lib.learned_by_doc(201)["group_occasion"] == "回她的时候"
    assert lib.learned_by_doc(202)["group_occasion"] == "被@时"
    await p._sticker_owner_observe(_gtext(6, "确实"), None, None, None)
    ev4, raw4, meta4 = _gsticker(7, 203, "👍")
    await p._sticker_owner_observe(ev4, raw4, meta4, "卡通猫点头")
    await _drain(p)
    assert lib.learned_by_doc(203)["group_occasion"] == "附和时"


@pytest.mark.asyncio
async def test_never_learns_members_forwarded_channel_or_ineligible(real):
    p, lib, _ = real
    _groups_on(p)
    ev, raw, meta = _gsticker(10, 300, sender="555")  # 群友
    await p._sticker_owner_observe(ev, raw, meta, "动漫女孩大笑")
    ev2, raw2, meta2 = _gsticker(11, 301)
    raw2.fwd_from = SimpleNamespace(from_id=1)  # 转发
    await p._sticker_owner_observe(ev2, raw2, meta2, "动漫女孩大笑")
    ev3, raw3, meta3 = _gsticker(12, 302, group_id=CHANNEL)  # 频道
    await p._sticker_owner_observe(ev3, raw3, meta3, "动漫女孩大笑")
    ev4, raw4, meta4 = _gsticker(13, 303, group_id="-100777")  # 不在白名单资格里
    await p._sticker_owner_observe(ev4, raw4, meta4, "动漫女孩大笑")
    ev5, raw5, meta5 = _gsticker(14, 304)
    raw5.post = True  # 广播帖子
    await p._sticker_owner_observe(ev5, raw5, meta5, "动漫女孩大笑")
    await _drain(p)
    assert lib.learned_list() == []
    # 只有转发那条是主人在资格群里发的消息（计数一次）
    assert lib.owner_activity_scene(f"group:{GROUP}", 7) == (1, 1)


@pytest.mark.asyncio
async def test_owner_group_use_is_evidence_but_hard_categories_stay_private(real):
    p, lib, _ = real
    _groups_on(p)
    # 私聊学到“干杯”（酒 → 软类别，私聊学时仅私聊）
    pev = FakeEvent(text="[贴纸]", msg_id=20)
    pmeta = StickerMeta(doc_id=400, emoji="🍺", kind="static", mime_type="image/webp")
    await p._sticker_owner_observe(pev, fake_msg(fake_doc(400, emoji="🍺"), msg_id=20), pmeta, "动漫少女举着啤酒杯开心大笑")
    await _drain(p)
    assert lib.learned_by_doc(400)["group_ok"] is False
    ev, raw, meta = _gsticker(21, 400, "🍺")
    await p._sticker_owner_observe(ev, raw, meta, "动漫少女举着啤酒杯开心大笑")
    await _drain(p)
    e = lib.learned_by_doc(400)
    assert e["group_uses"] == 1
    # 主人在这个群亲自用过 → 这个群可用；别的资格群仍按无证据判定（不可用）
    here = {x["label"] for x in lib.allowed_for(GROUP, [OWNER], True, unified=True, include_learned=True)}
    there = {x["label"] for x in lib.allowed_for("-1009998887776", [OWNER], True, unified=True, include_learned=True)}
    assert e["label"] in here and e["label"] not in there
    # 硬类别：主人在群里用过也只在私聊
    ev2, raw2, meta2 = _gsticker(22, 401, "🤬")
    await p._sticker_owner_observe(ev2, raw2, meta2, "卡通小熊举牌「智障」")
    ev3, raw3, meta3 = _gsticker(23, 402, "📷")
    await p._sticker_owner_observe(ev3, raw3, meta3, "真人男子自拍照片，微笑")
    await _drain(p)
    here = {x["label"] for x in lib.allowed_for(GROUP, [OWNER], True, unified=True, include_learned=True)}
    for d in (401, 402):
        x = lib.learned_by_doc(d)
        assert x is None or (x["group_ok"] is False and x["label"] not in here)


@pytest.mark.asyncio
async def test_group_prompt_tier_uses_group_data(real):
    p, lib, _ = real
    _groups_on(p)
    p.config.values["lifelike_chat_drive"] = "medium"
    lib.learn_add(fake_msg(fake_doc(500), chat_id=int(GROUP), msg_id=5), ["附和"], "同意的时候",
                  occasion="附和时", group_ok=True, scene="group", group_key=GROUP)
    lib.learn_add(fake_msg(fake_doc(501), msg_id=6), ["晚安"], "x", occasion="难过时")
    # 私聊数据充足（经常）、群里不足 → 群聊不写主人习惯，也不带私聊画像
    for i in range(30):
        lib.note_owner_message(i % 2 == 0)
    req = SimpleNamespace(system_prompt="base")
    await p._inject_sticker_prompt(FakeEvent(text="hi", sender="1", group_id=GROUP, msg_id=30), req)
    block = req.system_prompt
    assert "主人在群里常在附和时用" in block
    assert "主人自己在群里" not in block and "主人自己平时" not in block and "难过时" not in block
    # 群里数据充足：主人在群里很少发 → 很少
    for _ in range(25):
        lib.note_owner_scene("group:-1009998887776", False)  # 别的群的数据不算
    assert await p._sticker_group_tier(GROUP) == ("经常", False)
    for _ in range(25):
        lib.note_owner_scene(f"group:{GROUP}", False)
    assert await p._sticker_group_tier(GROUP) == ("很少", True)
    # 别的群：没有当前群数据，也看不到 GROUP 里的场合
    oreq = SimpleNamespace(system_prompt="base")
    await p._inject_sticker_prompt(FakeEvent(text="hi", sender="1", group_id="-100555666", msg_id=33), oreq)
    assert "附和时" not in oreq.system_prompt
    req2 = SimpleNamespace(system_prompt="base")
    await p._inject_sticker_prompt(FakeEvent(text="hi", sender="1", group_id=GROUP, msg_id=31), req2)
    assert "主人自己在群里很少发贴纸" in req2.system_prompt
    # 私聊说明不出现群里的场合
    preq = SimpleNamespace(system_prompt="base")
    await p._inject_sticker_prompt(FakeEvent(text="hi", msg_id=32), preq)
    assert "附和时" not in preq.system_prompt and "主人常在难过时用" in preq.system_prompt


@pytest.mark.asyncio
async def test_groups_off_prompt_same_as_u19(real):
    p, lib, _ = real
    _on(p)
    lib.learn_add(fake_msg(fake_doc(600), chat_id=int(GROUP), msg_id=5), ["附和"], "同意的时候",
                  occasion="附和时", group_ok=True, scene="group", group_key=GROUP)
    req = SimpleNamespace(system_prompt="base")
    await p._inject_sticker_prompt(FakeEvent(text="hi", sender="1", group_id=GROUP, msg_id=40), req)
    assert "附和" in req.system_prompt and "群里常在" not in req.system_prompt


@pytest.mark.asyncio
async def test_uncovered_group_uses_cache_only(real, tmp_path):
    p, lib, _ = real
    _groups_on(p)
    calls = []

    class Store:
        def get(self, doc_id):
            calls.append(doc_id)
            return SimpleNamespace(description="动漫女孩大笑") if doc_id == 700 else None

    p._sticker_store = Store()
    p._sticker_understanding = SimpleNamespace(describe=AsyncMock(side_effect=AssertionError("no vision")))
    p._sticker_inbound_allowed = lambda ev: False
    ev, raw, meta = _gsticker(50, 700)
    raw.sticker = fake_doc(700, emoji="😄")
    import astrbot_plugin_iris_memory.main as main_mod

    orig = main_mod.extract_sticker_meta
    main_mod.extract_sticker_meta = lambda r: meta if r is raw else orig(r)
    try:
        await p.on_sticker_understanding(ev)
        await _drain(p)
    finally:
        main_mod.extract_sticker_meta = orig
    assert calls == [700]
    assert lib.learned_by_doc(700) is not None


@pytest.mark.asyncio
async def test_unknown_self_id_reply_not_classified(real):
    p, lib, _ = real
    _groups_on(p)
    p._self_id = ""
    ev, raw, meta = _gsticker(60, 800, "👍", reply_from="555")
    await p._sticker_owner_observe(ev, raw, meta, "卡通小熊竖起大拇指")
    await _drain(p)
    assert lib.learned_by_doc(800)["group_occasion"] == ""
