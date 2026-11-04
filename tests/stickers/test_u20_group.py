"""U20：群适用性的“硬 / 软”类别与主人在群里使用的证据、群里场合短语、schema v3、按场景计数、群里学到的条目。"""

import sqlite3

import pytest

from astrbot_plugin_iris_memory.iris_memory.stickers import learn as L
from astrbot_plugin_iris_memory.iris_memory.stickers import library as lib_mod
from astrbot_plugin_iris_memory.iris_memory.stickers.library import StickerLibrary
from tests.stickers.test_library import GROUP, OWNER, OWNER_SCOPE, Clock, clock, fake_doc, fake_msg, lib  # noqa: F401


# --------------------------------------------------------------------------- 群适用性规则


@pytest.mark.parametrize(
    "desc",
    [
        "真人男子自拍照片",
        "一名中年男子皱眉",
        "卡通小熊上方写着「智障」",
        "小猫竖中指",
        "动漫少女穿比基尼",
        "卡通人物举着国旗",
        "卡通小人拿刀，满脸是血",
        "聊天记录截图",
        "卡通人物在吸毒",
        "",
        # 审查 U20-1：骂人的“死 / 爬 / 急了”
        "文字「去死」",
        "卡通猫，文字「你怎么不去死」",
        "卡通小人，文字「死吧」",
        "卡通小人举牌「爬」",
        "卡通狗写着「急了急了」",
        # 审查 U20-2：真人
        "一个戴眼镜的男生做鬼脸",
        "一张人脸特写，眼睛瞪大",
        "穿校服的女生比耶",
        "一个光头的人在大笑",
        "一个人穿着军装敬礼",
        # 审查 U20-7：政治符号
        "卡通猫举着红旗",
        "动漫角色挥舞党旗",
        # 复核：熊猫头 + 真人信号
        "熊猫头配真实人脸",
        "熊猫头表情包，中年男人的脸",
        "熊猫头表情包，脸是一个男生",
        "熊猫头人脸上带着得意浅笑",
        # U19 审查时的真人样例
        "真人男子自拍照片",
        "一名年轻女性对着镜头",
    ],
)
def test_hard_categories_never_group_even_if_owner_used(desc):
    assert L.group_hard_block(desc) is True
    assert L.group_verdict(desc, owner_used_in_group=True) is False
    assert L.group_verdict(desc, owner_used_in_group=False) is False


@pytest.mark.parametrize(
    "desc",
    [
        "灰色墙面上写着「笑死」",  # 软：夸张的“死”
        "动漫角色死鱼眼",
        "卡通熊累死了趴着",
        "动漫少女举着啤酒杯",  # 软：酒
        "卡通猫叼着香烟",  # 软：烟
        "卡通便便戴着墨镜",  # 软：恶心
        "卡通兔子萌死了",  # 软：萌死 / 可爱死 的夸张用法
        "粉发少女举着啤酒杯",  # 动漫少女风格 + 软类别
    ],
)
def test_soft_categories_open_by_owner_group_use(desc):
    assert L.group_hard_block(desc) is False
    assert L.group_verdict(desc, owner_used_in_group=False) is False
    assert L.group_verdict(desc, owner_used_in_group=True) is True


@pytest.mark.parametrize(
    "desc",
    [
        "白色背景上一行字「绷不住了」的手写体",
        "少女举着啤酒杯",  # “少女”不能单独算卡通
        "一坨便便戴着墨镜",
        "女孩双手托腮",
    ],
)
def test_uncertain_never_opened_by_owner_use(desc):
    assert L.group_verdict(desc, owner_used_in_group=False) is False
    assert L.group_verdict(desc, owner_used_in_group=True) is False


def test_group_ok_unchanged_from_u19():
    assert L.group_ok("灰发猫耳角色歪头吐舌，表情困惑") is True
    assert L.group_ok("动漫少女举着啤酒杯") is False
    assert L.group_ok("真人男子自拍照片") is False


@pytest.mark.parametrize(
    "text,occ",
    [
        ("确实", "附和时"),
        ("+1", "附和时"),
        ("哈哈哈哈", "被逗笑时"),
        ("离谱", "吐槽时"),
        ("晚安", "道晚安时"),
        ("你吃了吗", None),  # 提问在群里不记
        ("今天下雨", None),
    ],
)
def test_classify_group_text(text, occ):
    assert L.classify_group_text(text) == occ


def test_group_occasion_rules():
    assert L.group_occasion(None, "member", mentioned=True) == "被@时"
    assert L.group_occasion("被逗笑时", "member") == "接别人玩笑时"
    assert L.group_occasion("附和时", "member") == "附和时"
    assert L.group_occasion(None, "member") == "接别人话时"
    assert L.group_occasion(None, "bot") == "回她的时候"
    assert L.group_occasion(None, None) == ""
    assert L.group_occasion("吐槽时", None) == "吐槽时"
    for o in L.GROUP_OCCASIONS:
        assert len(o) <= 20


def test_group_prompt_habit_and_no_private_profile():
    entries = [
        {"label": "附和猫", "note": "同意的时候", "occasion": "难过时", "group_occasion": "附和时",
         "group_uses": 2, "source": "auto"},
        {"label": "开心", "note": "高兴", "source": "manual"},
    ]
    grp = L.prompt_block_v2(entries, "经常", private=False, owner_data=True, group_habit=True)
    assert "主人在群里常在附和时用" in grp and "主人自己在群里经常发贴纸" in grp
    assert "其中一些是主人在群里用过的" in grp
    assert "难过时" not in grp and "私聊" not in grp
    # 群里数据不足：不写主人习惯
    grp2 = L.prompt_block_v2(entries, "有时", private=False, owner_data=False, group_habit=True)
    assert "主人自己在群里" not in grp2
    # 私聊说明不出现群里的场合
    priv = L.prompt_block_v2(entries, "有时", private=True, owner_data=True)
    assert "附和时" not in priv and "主人常在难过时用" in priv
    # group_habit 关：与 U19 一致
    old = L.prompt_block_v2(entries, "有时", private=False, owner_data=True)
    assert "群里常在" not in old and "主人自己在群里" not in old
    for b in (grp, grp2, priv):
        assert not any(ch.isdigit() for ch in b)


# --------------------------------------------------------------------------- 库


def test_schema_v3_columns_and_scene_table(lib):
    assert lib.schema_version() == lib_mod.SCHEMA_VERSION == 3
    cols = {r[1] for r in lib._conn().execute("PRAGMA table_info(learned)")}
    assert {"learned_in", "group_occasion", "group_uses", "group_last_used_at", "group_ok_owner"} <= cols
    names = {r[0] for r in lib._conn().execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"owner_activity_scene", "owner_activity", "learned_group_use"} <= names


def test_v2_db_migrates_keeping_learned_rows(tmp_path):
    path = tmp_path / "v2.db"
    db = sqlite3.connect(path)
    for script in (lib_mod._SCHEMA_V1, lib_mod._SCHEMA_V2):
        for stmt in [x.strip() for x in script.split(";") if x.strip()]:
            db.execute(stmt)
    db.execute(
        "INSERT INTO learned(label, doc_id, access_hash, created_at, updated_at, owner_last_used_at)"
        " VALUES('开心', 1, 2, 0, 0, 0)"
    )
    db.execute("PRAGMA user_version=2")
    db.commit()
    db.close()
    lb = StickerLibrary(path, Clock())
    assert lb.schema_version() == 3
    e = lb.learned_get("开心")
    assert e["learned_in"] == "private" and e["group_uses"] == 0 and e["group_occasion"] == ""
    lb.close()


def test_scene_activity_counts_separately(lib):
    lib.note_owner_message(True)
    lib.note_owner_message(False)
    lib.note_owner_scene(f"group:{GROUP}", True)
    lib.note_owner_scene(f"group:{GROUP}", False)
    lib.note_owner_scene(f"group:{GROUP}", False)
    lib.note_owner_scene("group:-100999", True)
    assert lib.owner_activity(7) == (2, 1)
    assert lib.owner_activity_scene("private", 7) == (2, 1)
    assert lib.owner_activity_scene(f"group:{GROUP}", 7) == (3, 1)  # 按群分开
    assert lib.owner_activity_scene("group:-100999", 7) == (1, 1)


def test_group_learn_and_touch_per_group_evidence(lib):
    other = "-1009998887776"
    msg = fake_msg(fake_doc(10), chat_id=int(GROUP), msg_id=10)
    st, e = lib.learn_add(msg, ["附和"], "同意的时候", occasion="附和时", group_ok=True, scene="group",
                          group_key=GROUP, group_ok_owner=True)
    assert st == "added"
    assert e["learned_in"] == "group" and e["group_uses"] == 1 and e["group_occasion"] == "附和时"
    assert e["occasion"] == ""  # 群里的场合不写进私聊场合
    assert lib.group_use_get(10, GROUP) == {"uses": 1, "last_used_at": e["group_last_used_at"], "occasion": "附和时"}
    # 私聊学到的软类别贴纸（无证据不可群用），后来主人在 GROUP 用了
    st2, p = lib.learn_add(fake_msg(fake_doc(11)), ["干杯"], "x", group_ok=False, group_ok_owner=True)
    assert p["group_ok"] is False and p["learned_in"] == "private"
    assert "干杯" not in {x["label"] for x in lib.allowed_for(GROUP, [OWNER], True, unified=True, include_learned=True)}
    t = lib.learn_touch(11, None, "被逗笑时", scene="group", group_key=GROUP, group_ok_owner=True)
    assert t["group_ok"] is False and t["group_uses"] == 1 and t["occasion"] == "" and t["owner_uses"] == 2
    here = {x["label"]: x for x in lib.allowed_for(GROUP, [OWNER], True, unified=True, include_learned=True)}
    assert "干杯" in here and here["干杯"]["group_occasion"] == "被逗笑时" and here["干杯"]["group_uses"] == 1
    # 证据只在用过的那个群生效；别的群看不到“干杯”，也看不到 GROUP 里的场合
    there = {x["label"]: x for x in lib.allowed_for(other, [OWNER], True, unified=True, include_learned=True)}
    assert "干杯" not in there
    assert there["附和"]["group_occasion"] == "" and there["附和"]["group_uses"] == 0
    # 私聊再用不动群里的判定
    t2 = lib.learn_touch(11, None, "道晚安时")
    assert t2["occasion"] == "道晚安时" and lib.group_use_get(11, GROUP)["uses"] == 1
    out = lib.handle_command(["list"], None, OWNER, [OWNER], unified=True, learning=True)
    assert "你在群里常在附和时用" in out and "你在群里常在被逗笑时用" in out
    # 墓碑清掉按群证据
    lib.tombstone_doc(11)
    assert lib.group_use_get(11, GROUP) is None


@pytest.mark.parametrize(
    "desc",
    [
        "粉发少女害羞捂脸",
        "一个粉色头发的女孩开心地笑",
        "双马尾女孩鼓着脸生气",
        "银发猫耳少女歪头",
        "一只猫的脸部特写",  # “特写”不与人同现不算真人
        "熊猫头表情包，写着「我不听」",
    ],
)
def test_anime_girl_style_counts_as_drawn(desc):
    assert L.group_hard_block(desc) is False
    assert L.group_verdict(desc, owner_used_in_group=False) is True


def test_hard_death_vs_soft_cute_death():
    assert L.group_hard_block("卡通兔子可爱死了") is False
    assert L.group_hard_block("卡通兔子写着「死吧」") is True


def test_group_use_rows_follow_learned_deletes(lib, clock):
    """复核 3：自动条目被删（手动接管 / 淘汰 / 未收 / 刷新让位）时，按群证据不留孤儿。"""
    def cnt(doc):
        return lib._conn().execute("SELECT COUNT(*) FROM learned_group_use WHERE doc_id=?", (doc,)).fetchone()[0]

    msg = fake_msg(fake_doc(20), chat_id=int(GROUP), msg_id=20)
    lib.learn_add(msg, ["甲"], "x", scene="group", group_key=GROUP)
    assert cnt(20) == 1
    lib.add_from_message("手动", "", fake_msg(fake_doc(20)), OWNER)  # 手动接管同一张
    assert cnt(20) == 0
    # 容量满且其余都在保护期：新的不收，证据也不留
    lib.learn_add(fake_msg(fake_doc(21), chat_id=int(GROUP)), ["乙"], "x", scene="group", group_key=GROUP, cap=1)
    st, _ = lib.learn_add(fake_msg(fake_doc(22), chat_id=int(GROUP)), ["丙"], "x", scene="group", group_key=GROUP, cap=1)
    assert st == "evicted" and cnt(22) == 0 and cnt(21) == 1
    # 过了保护期被淘汰
    clock.advance(8 * 86400)
    st2, _ = lib.learn_add(fake_msg(fake_doc(23), chat_id=int(GROUP)), ["丁"], "x", scene="group", group_key=GROUP, cap=1)
    assert st2 == "added" and cnt(21) == 0 and cnt(23) == 1
