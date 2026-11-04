"""群友常用称呼（lifelike_member_nickname）：规则简称 + 从别人发言里学常用叫法；墓碑不学；off 不变。"""

import pytest

from astrbot_plugin_iris_memory.iris_memory.lifelike.members import nickname as nk
from tests.lifelike.test_members import DAY, G1, make_env  # noqa: F401


class TestRules:
    @pytest.mark.parametrize("display,want", [
        ("小林🌸【摸鱼中】", "小林"),
        ("✨星野✨", "星野"),
        ("桃", ""),
        ("我是一只快乐的咸鱼", "咸鱼"),
        ("Alexandria_W", ""),
        ("Alexandria Wong", "Alex"),
        ("jay_dev", ""),
        ("今天吃什么", ""),
        ("大家好", ""),
        ("好想睡觉", ""),
        ("tom cat", "Tom"),
        ("JackSparrow", "Jack"),
        ("陈晓明 | 产品经理", "陈晓明"),
        ("12345", ""),
        ("🌈🌈", ""),
    ])
    def test_natural_short(self, display, want):
        assert nk.natural_short(display) == want

    def test_candidates_are_name_fragments_only(self):
        c = nk.candidates(["林小雨🍃"])
        assert c == ["林小雨", "小雨"]  # 不生成叠字、不切"林小"这种半截
        assert nk.candidates(["今天吃什么"]) == [] and nk.candidates(["大家好"]) == []
        assert nk.candidates(["好想睡觉"]) == []

    def test_usage_needs_address_context_and_two_speakers(self):
        rows = [
            ("小雨，今天来吗", 10.0, "a"), ("好的，小雨～", 20.0, "b"), ("@小雨 在吗", 30.0, "b"),
            ("外面下小雨了", 40.0, "c"),  # 不是称呼语境
        ]
        usage = nk.count_usage(rows, ["林小雨", "小雨"])
        assert usage["小雨"] == (3, 30.0, 2) and "林小雨" not in usage
        assert nk.choose(usage) == "小雨"
        one = nk.count_usage([("小雨，在吗", 1.0, "a"), ("小雨，来", 2.0, "a")], ["小雨"])
        assert nk.choose(one) is None  # 只有一个人这么叫

    def test_generic_word_dropped(self):
        rows = [("你好，阿杰", float(i), f"s{i}") for i in range(3)]
        rows += [(f"阿杰牌饮料{i}号好喝吗还是不好喝呢我觉得一般", float(i), f"t{i}") for i in range(30)]
        assert "阿杰" not in nk.count_usage(rows, ["阿杰"])

    def test_username_only_when_people_use_it(self):
        assert nk.address_for("jay_dev", None) == ""
        assert nk.address_for("jay_dev", "jay") == "jay"  # 群里确实有 ≥2 个人这么叫才会学到

    def test_address_and_render(self):
        assert nk.address_for("小林🌸【摸鱼中】", None) == "小林"
        assert nk.address_for("小林🌸【摸鱼中】", "小林子") == "小林子"
        table = nk.render_table([("✨✨", ""), ("Alexandria Wong", "Alex")])
        assert "（不用叫名字）" in table and "Alexandria Wong → Alex" in table
        assert "aming 或 小明" in nk.owner_address_line() and "不要用完整的显示名" in nk.NICKNAME_NOTE_GROUP


class TestLedgerLearning:
    @pytest.mark.asyncio
    async def test_learns_from_others_when_on(self, make_env):
        env = make_env(lifelike_member_nickname="on")
        env.owner_here()
        env.names["u1"] = ["林小雨🍃"]
        t = env.clock.t
        env.post(G1, "u1", "我今天加班", t - 3 * DAY)
        env.post(G1, "u2", "小雨，辛苦了", t - 2 * DAY)
        env.post(G1, "u3", "@小雨 晚点打游戏吗", t - DAY)
        env.post(G1, "u1", "小雨是我自己说的不算", t - DAY)  # 本人的自称不计
        env.post(G1, "u2", "外面下小雨了", t - DAY)  # 不是称呼语境
        await env.run()
        assert env.lm.ledger.members.nickname("u1") == "小雨"
        aliases = {a["alias"]: a["source"] for a in env.lm.ledger.members.aliases("u1")}
        assert aliases.get("小雨") == nk.SOURCE_NICK

    @pytest.mark.asyncio
    async def test_off_learns_nothing(self, make_env):
        env = make_env()
        env.owner_here()
        env.names["u1"] = ["林小雨🍃"]
        t = env.clock.t
        env.post(G1, "u1", "我今天加班", t - 3 * DAY)
        env.post(G1, "u2", "小雨，辛苦了", t - 2 * DAY)
        env.post(G1, "u3", "@小雨 晚点打游戏吗", t - DAY)
        await env.run()
        assert env.count("member_nick_usage") == 0

    @pytest.mark.asyncio
    async def test_tombstoned_member_not_learned(self, make_env):
        env = make_env(lifelike_member_nickname="on")
        env.owner_here()
        env.names["u1"] = ["林小雨🍃"]
        env.lm.ledger.forget_member("u1")
        t = env.clock.t
        env.post(G1, "u1", "我今天加班", t - 3 * DAY)
        env.post(G1, "u2", "小雨，辛苦了", t - 2 * DAY)
        env.post(G1, "u3", "@小雨 晚点打游戏吗", t - DAY)
        await env.run()
        assert env.lm.ledger.members.nickname("u1") is None
        assert env.count("member_nick_usage") == 0


class TestMainInjection:
    @pytest.mark.asyncio
    async def test_group_note_on_and_off(self, nm_config):
        from astrbot_plugin_iris_memory.iris_memory.proactive.perception import WindowMessage
        from tests.proactive.test_u9_group_freedom import GID, OWNER, _Ev, _plugin

        p = _plugin(nm_config)
        p._sliding_window.append(GID, WindowMessage("u1", "小林🌸【摸鱼中】", "hi", 1.0))
        p._sliding_window.append(GID, WindowMessage(OWNER, "Aming", "hi", 2.0))
        p._sliding_window.append(GID, WindowMessage("u5", "jay_dev", "hi", 1.5))
        ev = _Ev("大家好", sender="u2", name="Alexandria Wong")
        assert await p._nickname_note(ev) == ""  # off
        p.config = {"lifelike_member_nickname": "on"}
        note = await p._nickname_note(ev)
        assert "小林🌸【摸鱼中】 → 小林" in note and "Alexandria Wong → Alex" in note
        assert "jay_dev → （不用叫名字）" in note  # 用户名不当昵称
        assert "Aming →" not in note  # 主人称呼规则单独写（aming / 小明）
        # 私聊里提到群友：用常用称呼
        pv = _Ev("今天小林🌸【摸鱼中】说要请客", sender=OWNER, group="")
        assert "→ 小林" in await p._nickname_note(pv)


@pytest.fixture
def nm_config():
    from astrbot_plugin_iris_memory.iris_memory.proactive.config import ConfigManager

    def _make(cfg=None, hidden=None):
        base = {"mute_period": {"start_hour": 0, "start_minute": 0, "end_hour": 0, "end_minute": 0}}
        base.update(cfg or {})
        return ConfigManager(base, hidden_get=dict(hidden or {}).get)

    return _make


def test_speakers_column_backfilled(tmp_path):
    """复核 N5：旧开发库里缺 speakers 列时自动补列"""
    from astrbot_plugin_iris_memory.iris_memory.continuity.store import ContinuityStore
    from astrbot_plugin_iris_memory.iris_memory.lifelike.members.store import ensure_members_schema

    st = ContinuityStore(tmp_path / "c.db", clock=lambda: 1.0)
    st.open()
    try:
        with st._tx() as db:
            db.execute("DROP TABLE IF EXISTS member_nick_usage")
            db.execute("CREATE TABLE member_nick_usage(member_id TEXT NOT NULL, nick TEXT NOT NULL, uses INTEGER NOT NULL,"
                       " last_used REAL NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(member_id, nick))")
        ensure_members_schema(st)
        cols = {r[1] for r in st._conn().execute("PRAGMA table_info(member_nick_usage)").fetchall()}
        assert "speakers" in cols
        ensure_members_schema(st)  # 幂等
    finally:
        st.close()
