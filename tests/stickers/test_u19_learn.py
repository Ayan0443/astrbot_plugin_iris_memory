"""U19 表情包自动学习：标签推导、群里适用性、场合概括、档位、新 prompt、库的 learned 表 / 墓碑 / 容量淘汰 / 命令。"""

import sqlite3

import pytest

from astrbot_plugin_iris_memory.iris_memory.stickers import learn as L
from astrbot_plugin_iris_memory.iris_memory.stickers import library as lib_mod
from astrbot_plugin_iris_memory.iris_memory.stickers.library import (
    StickerLibrary,
    marker_raw_labels,
    take_tiezhi_marker,
)
from tests.stickers.test_library import GROUP, OWNER, OWNER_SCOPE, Clock, clock, fake_doc, fake_msg, lib  # noqa: F401


# --------------------------------------------------------------------------- 纯函数


@pytest.mark.parametrize(
    "emoji,desc,tag",
    [
        ("🤨", "绿发双马尾动漫女孩侧目皱眉，头顶有蓝色问号", "疑惑"),
        ("😄", "灰色墙面上用蓝色喷漆写着「有點好笑」", "偷笑"),
        ("💤", "金发动漫女孩闭眼躺在枕上微张着嘴，神态安详地熟睡", "晚安"),
        ("", "一只小猫抱着枕头睡觉，旁边写着「晚安」", "晚安"),
        ("🙄", "金发少女微侧头半眯眼显露无奈神情", "无语"),
        ("🌟", "大耳狗流泪撇嘴，神情委屈难过", "委屈"),
        ("😮", "白发少女张嘴睁大眼显出惊讶神情", "震惊"),
        ("👍", "小熊竖起大拇指", "赞同"),
        ("🤗", "两只兔子拥抱在一起", "抱抱"),
        ("😠", "女生微皱眉双手在胸前抬起", "生气"),
        ("🤭", "黑发少女用双手捂嘴轻笑，眼睛微眯流露调皮神色", "偷笑"),
    ],
)
def test_derive_tag(emoji, desc, tag):
    assert L.derive_tag(emoji, desc) == tag


def test_derive_tag_unknown_returns_none():
    assert L.derive_tag("🚬", "金发女孩站着手持冒烟细长物，神情平静") is None
    assert L.derive_tag("", "") is None


@pytest.mark.parametrize(
    "desc",
    [
        "粉发少女满脸血迹张嘴大笑",
        "女孩穿西装伸手微笑，图上有「全体出动操你妈」",
        "浅绿发角色流汗惊恐地掀起衣服指着身上红印",
        "聊天软件群聊截图，群名某某",
        "少女上方写着「我对日本人打了」",
        # 审查 1 复现样例
        "小猫竖中指",
        "动漫女孩比中指",
        "卡通小熊上方写着「智障」",
        "猫猫头写着「弱智」",
        "卡通人物旁边有「脑残」字样",
        "动漫女孩写着「废物」",
        "卡通小人举牌「sb」",
        "真人男子自拍照片",
        "一名男子抽烟喝酒",
        "动漫少女举着啤酒杯",
        "卡通猫叼着香烟",
        "一名年轻女性坐在木台上托下巴",
        "动漫少女穿比基尼",
        "卡通人物举着国旗",
        "动漫角色双手合十拜佛祖",
        "卡通小人拿刀",
        "",
        "一个东西",
    ],
)
def test_group_unsafe_descriptions_private_only(desc):
    assert L.group_ok(desc) is False


def test_group_ok_normal():
    for d in (
        "灰发猫耳角色歪头吐舌，表情困惑，旁边有一个蓝色问号",
        "一只浅色虎斑猫抬眼张望",
        "熊猫头表情包，写着「我不听」",
        "动漫女孩傻笑",
    ):
        assert L.group_ok(d) is True, d


def test_group_unsafe_reuses_hostility_words():
    assert L._HOSTILE_RES, "应复用反击模块词表"
    assert L.group_ok("卡通小熊举牌「狗东西」") is False
    assert L.group_ok("动漫角色写着「人渣」") is False


def test_label_candidates_distinguish_same_emotion():
    c = L.label_candidates("开心", "一只橘猫张嘴大笑")
    assert c[0] == "开心" and c[1] == "开心猫"
    assert all(not ch.isdigit() for x in c for ch in x)
    assert L.label_candidates("无语", "金发少女白眼")[1] == "无语金毛"


@pytest.mark.parametrize(
    "text,occ",
    [
        ("晚安啦", "道晚安时"),
        ("哈哈哈哈笑死", "被逗笑时"),
        ("这也太离谱了吧", "吐槽时"),
        ("谢谢你", "道谢时"),
        ("今天好累", "累了的时候"),
        ("好的", "答应时"),
        ("你吃饭了吗", "提问时"),
        ("这是什么", None),  # 不是对她的问题
        ("怎么办", None),
        ("/sticker list", None),
        ("今天吃了面", None),
    ],
)
def test_classify_owner_text(text, occ):
    assert L.classify_owner_text(text) == occ


def test_occasions_are_short_fixed_phrases():
    for o in L.OCCASIONS:
        assert len(o) <= 20
    assert L.classify_bot_text("你也太厉害了吧") == "被夸时"
    assert L.classify_bot_text("今天天气不错") is None


def test_dont_learn_detection():
    for t in ("别学这个", "这个别用", "这张表情别再用了", "不要学这个"):
        assert L.is_dont_learn(t), t
    for t in ("别学了，去睡觉吧明天还要上班呢真的", "学习好累", "/sticker del 开心"):
        assert not L.is_dont_learn(t), t


def test_parse_label_json():
    assert L.parse_label_json('{"label": "摸鱼", "note": "偷懒的时候"}') == ("摸鱼", "偷懒的时候")
    assert L.parse_label_json('好的：{"label":"开心","note":""}') == ("开心", L.TAG_NOTES["开心"])
    assert L.parse_label_json('{"label": "abc", "note": "x"}') is None
    assert L.parse_label_json("没有 JSON") is None
    assert L.parse_label_json('{"label": "太长太长太长的标签", "note": "x"}') is None
    prompt = L.label_prompt("😀", "一只猫")
    assert "一只猫" in prompt and "JSON" in prompt


@pytest.mark.parametrize(
    "msgs,stk,drive,tier,owner",
    [
        (0, 0, "off", "有时", False),  # 数据不足：drive 给起点
        (0, 0, "low", "很少", False),
        (0, 0, "medium", "有时", False),
        (0, 0, "high", "经常", False),
        (100, 2, "off", "很少", True),
        (100, 2, "medium", "很少", True),  # 主人很少用 → 很少，drive 不往上抬
        (100, 2, "high", "很少", True),
        (100, 15, "off", "有时", True),
        (100, 40, "low", "经常", True),
    ],
)
def test_sticker_tier(msgs, stk, drive, tier, owner):
    assert L.sticker_tier(msgs, stk, drive) == (tier, owner)


def test_prompt_block_v2_wording_no_digits():
    entries = [
        {"label": "开心", "note": "高兴", "source": "manual"},
        {"label": "晚安猫", "note": "道晚安、要睡觉的时候", "occasion": "道晚安时", "source": "auto",
         "owner_last_used_at": 5.0},
    ]
    for tier in L.TIERS:
        block = L.prompt_block_v2(entries, tier, private=True, owner_data=True)
        assert "【可用表情包】" in block and "[表情:标签]" in block
        assert "主人也常用这些贴纸" in block and "不用每条都发" in block
        assert "道晚安" in block and "被夸时" in block
        assert "晚安猫：道晚安、要睡觉的时候（主人常在道晚安时用）" in block
        assert tier in block
        assert "难过" in block and "技术" in block and "回击" in block  # 什么时候不发
        assert not any(ch.isdigit() for ch in block)
    assert L.prompt_block_v2([], "有时") == ""


def test_prompt_block_v2_no_fabrication_and_group_has_no_private_profile():
    entries = [
        {"label": "开心", "note": "高兴", "source": "manual"},
        {"label": "晚安猫", "note": "道晚安的时候", "occasion": "难过时", "source": "auto"},
    ]
    nodata = L.prompt_block_v2(entries, "有时", private=True, owner_data=False)
    assert "主人自己平时" not in nodata  # 没有主人数据不编造主人习惯
    grp = L.prompt_block_v2(entries, "经常", private=False, owner_data=True)
    assert "主人常在" not in grp and "主人自己平时" not in grp and "私聊" not in grp
    assert "主人也常用" not in grp
    assert "晚安猫：道晚安的时候" in grp
    manual_only = L.prompt_block_v2(entries[:1], "有时", private=True, owner_data=True)
    assert "主人也常用这些贴纸" not in manual_only


def test_fuzzy_label():
    assert L.fuzzy_label("开心的", ["开心", "无语"]) == "开心"
    assert L.fuzzy_label("晚安", ["晚安猫", "开心"]) == "晚安猫"
    assert L.fuzzy_label("开心", ["开心猫", "开心狗"]) is None  # 不唯一
    assert L.fuzzy_label("哈", ["哈哈"]) is None
    # 审查 5：反义 / 疑问不认
    assert L.fuzzy_label("开心不起来", ["开心"]) is None
    assert L.fuzzy_label("生气吗", ["生气"]) is None
    assert L.fuzzy_label("不开心", ["开心"]) is None
    assert L.fuzzy_label("开心果", ["开心"]) is None


def test_tiezhi_marker_and_raw_labels():
    assert take_tiezhi_marker("好呀\n[贴纸：开心]", ["开心"]) == ("好呀", "开心")
    assert take_tiezhi_marker("好呀\n【贴纸:开心】  ", ["开心"]) == ("好呀", "开心")
    # 审查 10：同一行末尾 / 转述语境 / 不在库里 / 不在末尾：原样
    assert take_tiezhi_marker("好呀[贴纸：开心]", ["开心"]) == ("好呀[贴纸：开心]", None)
    retell = "你刚发的是\n[贴纸：开心]"
    assert take_tiezhi_marker(retell, ["开心"]) == (retell, None)
    retell2 = "哈哈你发的这个：\n[贴纸：开心]"
    assert take_tiezhi_marker(retell2, ["开心"]) == (retell2, None)
    assert take_tiezhi_marker("好呀\n[贴纸：别的]", ["开心"]) == ("好呀\n[贴纸：别的]", None)
    assert take_tiezhi_marker("[贴纸：开心]\n好呀", ["开心"]) == ("[贴纸：开心]\n好呀", None)
    assert take_tiezhi_marker("[贴纸：开心]", ["开心"]) == ("[贴纸：开心]", None)
    assert marker_raw_labels("嗯[表情:开心的]") == ["开心的"]
    assert marker_raw_labels("没有标记") == []


# --------------------------------------------------------------------------- 库


def test_schema_v2_adds_tables_only(lib):
    assert lib.schema_version() == lib_mod.SCHEMA_VERSION >= 2
    names = {r[0] for r in lib._conn().execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"library", "sends", "learned", "learn_tombstones", "learn_meta", "owner_activity"} <= names


def test_v1_db_migrates_and_keeps_rows(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    for stmt in [x.strip() for x in lib_mod._SCHEMA_V1.split(";") if x.strip()]:
        db.execute(stmt)
    db.execute(
        "INSERT INTO library(label, doc_id, access_hash, created_at, updated_at) VALUES('开心', 1, 2, 0, 0)"
    )
    db.execute("PRAGMA user_version=1")
    db.commit()
    db.close()
    lb = StickerLibrary(path, Clock())
    assert lb.schema_version() == lib_mod.SCHEMA_VERSION
    assert [e["label"] for e in lb.list()] == ["开心"]
    lb.close()


def _learn(lib, doc_id, labels=("开心",), **kw):
    return lib.learn_add(fake_msg(fake_doc(doc_id), msg_id=doc_id), list(labels), "开心的时候", **kw)


def test_learn_add_touch_and_dedupe(lib, clock):
    st, e = _learn(lib, 10, occasion="被夸时")
    assert st == "added" and e["label"] == "开心" and e["source"] == "auto" and e["occasion"] == "被夸时"
    assert e["owner_uses"] == 1
    clock.advance(100)
    st2, e2 = _learn(lib, 10, occasion="道晚安时")
    assert st2 == "touched" and e2["owner_uses"] == 2 and e2["occasion"] == "道晚安时"
    assert e2["owner_last_used_at"] == clock.t
    # 同一情绪另一张：标签加区分
    st3, e3 = _learn(lib, 11, labels=("开心", "开心猫"))
    assert st3 == "added" and e3["label"] == "开心猫"
    assert len(lib.learned_list()) == 2


def test_learn_skips_manual_doc_and_label_collision(lib):
    lib.add_from_message("开心", "", fake_msg(fake_doc(20)), OWNER)
    assert _learn(lib, 20)[0] == "manual"
    st, e = _learn(lib, 21, labels=("开心", "开心二"))
    assert st == "added" and e["label"] == "开心二"
    assert _learn(lib, 22, labels=("开心",))[0] == "no_label"


def test_tombstone_blocks_relearn(lib):
    _learn(lib, 30)
    assert lib.tombstone_doc(30) == "开心"
    assert lib.learned_list() == []
    assert _learn(lib, 30)[0] == "tombstoned"
    # 手动的贴纸“这个别用”→ 停用
    lib.add_from_message("困惑", "", fake_msg(fake_doc(31)), OWNER)
    assert lib.disable_manual_doc(31) == 1 and lib.get("困惑")["enabled"] is False


def test_capacity_evicts_auto_only_by_owner_recency(lib, clock):
    lib.add_from_message("手动", "", fake_msg(fake_doc(99)), OWNER)
    for i in range(3):
        clock.advance(10)
        _learn(lib, 40 + i, labels=(f"标签{'甲乙丙丁'[i]}",), cap=3)
    clock.advance(10)
    lib.learn_touch(40)  # 最早学的那张主人刚又用过
    clock.advance(10)
    # 审查 6：其余都在 7 天保护期内 → 新的这张不收，如实返回 evicted
    assert _learn(lib, 49, labels=("标签己",), cap=3) == ("evicted", None)
    assert {e["label"] for e in lib.learned_list()} == {"标签甲", "标签乙", "标签丙"}
    clock.advance(8 * 86400)
    lib.learn_touch(40)
    st, e = _learn(lib, 50, labels=("标签戊",), cap=3)
    assert st == "added" and e["label"] == "标签戊"  # 刚学的不会被立刻淘汰
    labels = {e["label"] for e in lib.learned_list()}
    assert labels == {"标签甲", "标签丙", "标签戊"}  # 淘汰了最久没用的“乙”
    assert lib.get("手动") is not None  # 手动永不淘汰


def test_allowed_for_include_learned_and_group_ok(lib):
    lib.add_from_message("公开", "", fake_msg(fake_doc(60)), OWNER, default_scope="all")
    _learn(lib, 61, labels=("开心",))
    _learn(lib, 62, labels=("偷笑",), group_ok=False)
    owners = [OWNER]
    # 不带 include_learned：与 676cd8a 一致
    assert [e["label"] for e in lib.allowed_for(OWNER_SCOPE, owners, True, unified=True)] == ["公开"]
    priv = lib.allowed_for(OWNER_SCOPE, owners, True, unified=True, include_learned=True)
    assert sorted(e["label"] for e in priv) == ["偷笑", "公开", "开心"]
    grp = lib.allowed_for(GROUP, owners, True, unified=True, include_learned=True)
    assert sorted(e["label"] for e in grp) == ["公开", "开心"]  # 不适合群的只在私聊
    assert lib.allowed_for(GROUP, owners, False, unified=True, include_learned=True) == []
    assert lib.allowed_for("-1001000000002", owners, True, unified=True, include_learned=True, blocked_scopes=["-1001000000002"]) == []


def test_record_send_updates_only_its_own_table(lib):
    _learn(lib, 70)
    lib.record_send(OWNER_SCOPE, "开心", [1], source="auto", doc_id=70)
    assert lib.learned_get("开心")["use_count"] == 1
    lib.record_send(OWNER_SCOPE, "开心", [2])  # 手动（无来源）不碰 learned
    assert lib.learned_get("开心")["use_count"] == 1


def test_review2_manual_add_vs_learned_no_cross_table(lib):
    """审查 2 复现：先学 A→开心、B→开心猫，再 /sticker add 开心 回复 B，模拟 file_reference 过期刷新。"""
    _learn(lib, 1, labels=("开心",))
    _learn(lib, 2, labels=("开心", "开心猫"))
    assert lib.learned_by_doc(2)["label"] == "开心猫"
    lib.add_from_message("开心", "", fake_msg(fake_doc(2, ref=b"m"), msg_id=200), OWNER)
    # B 改由手动管理：自动条目删掉；A 的自动条目让出“开心”改名，内容不变
    assert lib.learned_by_doc(2) is None
    a = lib.learned_by_doc(1)
    assert a is not None and a["label"] == "开心二" and a["file_reference"] == b"ref-old"
    assert lib.get("开心")["doc_id"] == 2
    # 手动“开心”的引用刷新：只改 library，不会撞 learned 的 UNIQUE
    e = lib.refresh_document("开心", fake_doc(2, ref=b"fresh"))
    assert e["file_reference"] == b"fresh" and lib.learned_by_doc(1)["file_reference"] == b"ref-old"
    # 刷新成另一个 doc_id（即使与自动条目相同）也不会失败
    e2 = lib.refresh_document("开心", fake_doc(1, ref=b"x"))
    assert e2["doc_id"] == 1
    # 发送计数各记各的
    lib.record_send(OWNER_SCOPE, "开心", [9])
    assert lib.get("开心")["use_count"] == 1 and lib.learned_by_doc(1)["use_count"] == 0
    # 自动条目刷新按 doc_id
    lib.refresh_document("开心二", fake_doc(1, ref=b"auto-new"), source="auto", old_doc_id=1)
    assert lib.learned_by_doc(1)["file_reference"] == b"auto-new"
    assert lib.get("开心")["file_reference"] == b"x"
    # /sticker del 手动“开心”：不会让别的自动条目以“开心”再现（自动条目保持改名后的标签）
    assert lib.remove("开心")
    labels = [x["label"] for x in lib.allowed_for(OWNER_SCOPE, [OWNER], True, unified=True, include_learned=True)]
    assert labels == ["开心二"]


@pytest.mark.asyncio
async def test_review2_auto_send_refresh_does_not_touch_manual(lib):
    from tests.stickers.test_library import FakeClient

    _learn(lib, 5, labels=("晚安",))
    lib.add_from_message("开心", "", fake_msg(fake_doc(6)), OWNER)
    entry = lib.allowed_for(OWNER_SCOPE, [OWNER], True, unified=True, include_learned=True)
    auto = next(e for e in entry if e["label"] == "晚安")
    from telethon import errors as tg_errors

    client = FakeClient(
        fail_first=tg_errors.FileReferenceExpiredError(request=None),
        refetch=fake_msg(fake_doc(5, ref=b"new-ref")),
    )
    ids = await lib.send(client, int(OWNER), auto, scope_key=OWNER_SCOPE)
    assert ids
    assert lib.learned_by_doc(5)["file_reference"] == b"new-ref"
    assert lib.learned_by_doc(5)["use_count"] == 1
    assert lib.get("开心")["use_count"] == 0 and lib.get("开心")["file_reference"] == b"ref-old"


def test_should_send_cooldown_override(lib, clock):
    lib.record_send(OWNER_SCOPE, "x", [1])
    clock.advance(61)
    assert lib.should_send(OWNER_SCOPE) is False  # 默认 120 秒
    assert lib.should_send(OWNER_SCOPE, None, 60.0) is True


def test_owner_activity_counts(lib, clock):
    for _ in range(3):
        lib.note_owner_message(False)
    lib.note_owner_message(True)
    clock.advance(8 * 86400)
    lib.note_owner_message(True)
    assert lib.owner_activity(7) == (1, 1)
    assert lib.owner_activity(10) == (5, 2)


def test_commands_with_learning(lib):
    lib.add_from_message("开心", "高兴", fake_msg(fake_doc(80)), OWNER)
    _learn(lib, 81, labels=("晚安",), occasion="道晚安时")
    _learn(lib, 82, labels=("偷笑",), group_ok=False)
    # 开关 off：list 与原来一致（不显示自动条目）
    off = lib.handle_command(["list"], None, OWNER, [OWNER], unified=True)
    assert "晚安" not in off and "自动" not in off
    out = lib.handle_command(["list"], None, OWNER, [OWNER], unified=True, learning=True)
    assert "开心" in out and "手动" in out
    assert "晚安" in out and "自动" in out and "道晚安时" in out
    assert "偷笑" in out and "仅私聊" in out
    assert "开启中" in out
    assert "暂停" in lib.handle_command(["learn", "off"], None, OWNER, [OWNER], learning=True) or lib.learn_paused()
    assert lib.learn_paused() is True
    lib.handle_command(["learn", "on"], None, OWNER, [OWNER], learning=True)
    assert lib.learn_paused() is False
    msg = lib.handle_command(["del", "晚安"], None, OWNER, [OWNER], learning=True)
    assert "不会再学" in msg and lib.is_tombstoned(81)
    assert lib.handle_command(["del", "开心"], None, OWNER, [OWNER], learning=True) == "已删除「开心」"
    assert "learn" in lib.handle_command(["help"], None, OWNER, [OWNER], learning=True)
    assert "learn" not in lib.handle_command(["help"], None, OWNER, [OWNER])
    # 非主人 / 群里照旧拒绝
    assert "只有主人" in lib.handle_command(["learn", "off"], None, "1", [OWNER], learning=True)
