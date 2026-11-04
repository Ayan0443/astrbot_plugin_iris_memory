"""P1-13：群友认知（ledger / lens / 命令 / 隐私闸门）。tmp_path 库 + 原创群聊序列，不连真实数据。"""

import re
from datetime import datetime
from types import SimpleNamespace

import pytest
from astrbot_plugin_iris_memory.iris_memory.continuity import models as m
from astrbot_plugin_iris_memory.iris_memory.continuity.store import ContinuityStore
from astrbot_plugin_iris_memory.iris_memory.lifelike.members import ledger as lg
from astrbot_plugin_iris_memory.iris_memory.lifelike.members.lens import MemberLens, MemberResolver, valid_alias
from astrbot_plugin_iris_memory.iris_memory.lifelike.members.store import alias_hash
from astrbot_plugin_iris_memory.iris_memory.lifelike.memory import tombstones as tb
from astrbot_plugin_iris_memory.iris_memory.lifelike.memory.service import LifelikeMemory
from tests.lifelike.v3_util import written_tables

OWNER = "123456789"
SCOPE = f"private:{OWNER}"
G1 = -1001000000001
G2 = -1001000000004
CH = -1001000000002
BASE = datetime(2026, 11, 3, 20, 0, tzinfo=m.TZ_SHANGHAI).timestamp()
DAY = 86400.0
DIGITS = re.compile(r"\d")


class Clock:
    def __init__(self, t=BASE):
        self.t = t

    def __call__(self):
        return self.t


class Env:
    def __init__(self, tmp_path, **cfg):
        self.clock = Clock()
        self.store = ContinuityStore(tmp_path / "continuity.db", clock=self.clock)
        self.store.open()
        self.cfg = {"lifelike_member_mode": "shadow", "lifelike_member_groups": f"{G1}", "lifelike_memory_mode": "off", **cfg}
        self.names = {"u1": ["小林"], "u2": ["阿杰"], "u3": ["小林"], "kid": ["小明"]}
        self.observe = (CH, G1, G2)
        self.lm = LifelikeMemory(
            self.store,
            config_get=lambda k, d=None: self.cfg.get(k, d),
            owner_ids=lambda: [OWNER],
            observe_chat_ids=lambda: self.observe,
            owner_channel_ids=lambda: (CH,),
            profile_names=self._names,
            clock=self.clock,
        )
        self.mid = 1000

    async def _names(self, member_id):
        return self.names.get(member_id, [])

    def post(self, chat, sender, text, ts, kind="other", fwd=0):
        self.mid += 1
        self.store.add_observed_post(
            chat_id=chat, msg_id=self.mid, posted_at=ts, sender_id=sender, author_kind=kind,
            is_forward=fwd, text=text, via="test",
        )

    def owner_here(self, chat=G1, ts=None):
        self.post(chat, OWNER, "主人在群里说话", ts if ts is not None else self.clock.t - DAY, kind="owner")

    async def run(self):
        return await self.lm.ledger.maybe_run(force=True)

    def notes(self, member):
        return self.lm.ledger.members.notes(member)

    def count(self, table):
        if not tb.table_exists(self.store, table):
            return 0
        return tb.read_rows(self.store, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]


@pytest.fixture
def make_env(tmp_path):
    envs = []

    def _make(**cfg):
        e = Env(tmp_path / f"e{len(envs)}", **cfg)
        envs.append(e)
        return e

    yield _make
    for e in envs:
        e.store.close()


GPU_TEXTS = ["这周终于把显卡换了", "新显卡到了，跑游戏好流畅", "显卡驱动又崩了，烦", "二手显卡能买吗"]


def gpu_posts(env, member="u1", chat=G1, days=(3, 2, 1)):
    for i, d in enumerate(days):
        env.post(chat, member, GPU_TEXTS[i % len(GPU_TEXTS)], env.clock.t - d * DAY)


# ---------------------------------------------------------------- 纯函数


def test_extract_terms_and_alias_rules():
    terms = lg.extract_terms("这周终于把显卡换了 rtx4090 真香")
    assert "显卡" in terms and "rtx4090" in terms
    ctx = lg.extract_term_contexts("把显卡换了")
    assert ("把", "换") in ctx["显卡"]
    assert "北京" not in lg.extract_terms("我在北京")
    assert valid_alias("小林") and not valid_alias("猫") and not valid_alias("老师") and not valid_alias("哥")
    assert lg.quotes_post("最近在群里聊过「显卡」", ["最近在群里聊过「显卡」是吧"])
    assert not lg.quotes_post("最近在群里聊过「显卡」", ["显卡好贵"])


def test_resolver_hit_none_ambiguous():
    r = MemberResolver([
        {"member_id": "u1", "alias": "小林"},
        {"member_id": "u3", "alias": "小林"},
        {"member_id": "u2", "alias": "阿杰"},
        {"member_id": "u4", "alias": "猫"},
        {"member_id": "u5", "alias": "#x", "alias_hash": alias_hash("@jay_dev")},
    ])
    assert r.match(["阿杰最近怎么样"]) == ("u2", "hit")
    assert r.match(["小林最近怎么样"]) == (None, "ambiguous")
    assert r.match(["我家猫好可爱"]) == (None, "none")
    assert r.match(["@jay_dev 在吗"]) == ("u5", "hit")


# ---------------------------------------------------------------- ledger


@pytest.mark.asyncio
async def test_ledger_builds_topic_notes_with_bands(make_env):
    env = make_env()
    env.owner_here()
    gpu_posts(env, "u1", days=(3, 2, 1))
    env.post(G1, "u2", "显卡这玩意真贵", env.clock.t - 2 * DAY)  # 只 1 天
    rep = await env.run()
    assert rep.status == "shadow" and rep.posts >= 4
    notes = env.notes("u1")
    texts = {n["text"]: n for n in notes}
    assert list(texts) == ["最近在群里聊过「显卡」"], texts  # 句内窗口碎片被左右邻规则筛掉
    n = next(n for n in notes if "显卡" in n["text"])
    assert n["confidence_band"] == "较确定" and n["detector"] == "rule" and len(n["text"]) <= 30
    assert "确定" != n["confidence_band"]
    assert all(":" in r for r in __import__("json").loads(n["evidence_refs"]))
    assert len([x for x in notes if x["kind"] == "topic"]) <= lg.MAX_TOPICS
    assert env.notes("u2") == []
    aliases = env.lm.ledger.members.aliases("u1")
    assert [a["alias"] for a in aliases] == ["小林"] and aliases[0]["source"] == "profile"
    # 游标推进：再跑一次不重复处理
    rep2 = await env.run()
    assert rep2.posts == 0


@pytest.mark.asyncio
async def test_ledger_authorization(make_env):
    # 主人不在群（60 天内无发言）→ 不产生数据
    env = make_env()
    env.owner_here(ts=env.clock.t - 61 * DAY)
    gpu_posts(env)
    rep = await env.run()
    assert rep.posts == 0 and env.count("member_notes") == 0
    # 未授权群（G2 不在 lifelike_member_groups）→ 不产生数据
    env2 = make_env()
    env2.owner_here(chat=G2)
    gpu_posts(env2, chat=G2)
    await env2.run()
    assert env2.count("member_profiles") == 0
    # 不在采集群 → 不产生数据
    env3 = make_env()
    env3.observe = (CH,)
    env3.owner_here()
    gpu_posts(env3)
    await env3.run()
    assert env3.count("member_profiles") == 0
    # mode=off → 不读不写（v3 迁移已建表，断言不写任何行）
    env4 = make_env(lifelike_member_mode="off")
    env4.owner_here()
    gpu_posts(env4)
    assert await env4.run() is None
    assert not written_tables(env4.store, ["member_profiles", "member_aliases", "member_notes"])


SENSITIVE_POSTS = [
    "我最近确诊了焦虑症", "上周住院做了个手术", "我住在幸福小区3号楼", "我手机号13812345678",
    "我月薪才五千", "跟男朋友分手了好难过", "周末去教会做礼拜", "我是同性恋这事别外传",
    "这事私下说，别告诉别人", "我们公司老板又发疯", "我在华夏银行上班", "投票给某个政党",
    "最近吃药吃得头晕", "我老婆跟我吵架了", "身份证丢了好烦", "房贷压力好大", "我信佛的",
    "我邮箱是abc@example.com", "加我微信号abc123", "@somebody_handle 来看",
]


@pytest.mark.asyncio
async def test_ledger_sensitive_posts_never_stored(make_env):
    env = make_env()
    env.owner_here()
    for d in (3, 2, 1):
        for i, text in enumerate(SENSITIVE_POSTS):
            env.post(G1, f"s{i}", text, env.clock.t - d * DAY + i)
    rep = await env.run()
    assert env.count("member_notes") == 0
    assert rep.sensitive_dropped >= len(SENSITIVE_POSTS)


@pytest.mark.asyncio
async def test_ledger_minor_whole_person_and_forget_me(make_env):
    env = make_env()
    env.owner_here()
    gpu_posts(env, "kid", days=(5, 4, 3))
    gpu_posts(env, "u2", days=(5, 4, 3))
    await env.run()
    assert env.notes("kid") and env.notes("u2")
    env.post(G1, "kid", "我今年15岁，刚上高中", env.clock.t - 1 * DAY)
    env.post(G1, "u2", "别记我的事了啊", env.clock.t - 1 * DAY + 5)
    rep = await env.run()
    assert rep.minors == 1 and rep.forget_requests == 1
    assert env.lm.ledger.members.get_profile("kid") is None and env.notes("kid") == []
    assert env.lm.ledger.members.get_profile("u2") is None
    assert tb.has_tombstone(env.store, tb.member_hash("kid"))
    # 之后的发言不再建档
    gpu_posts(env, "kid", days=(0.5, 0.4))
    env.clock.t += 1
    await env.run()
    assert env.lm.ledger.members.get_profile("kid") is None


@pytest.mark.asyncio
async def test_ledger_skips_forward_command_link(make_env):
    env = make_env()
    env.owner_here()
    for d in (3, 2, 1):
        env.post(G1, "u1", "这周终于把显卡换了", env.clock.t - d * DAY, fwd=1)
        env.post(G1, "u2", "/start 显卡", env.clock.t - d * DAY + 1)
        env.post(G1, "u2", "https://example.com/x", env.clock.t - d * DAY + 2)
        env.post(G1, "", "匿名的显卡帖", env.clock.t - d * DAY + 3)
        env.post(G1, "u9", "频道身份显卡", env.clock.t - d * DAY + 4, kind="unknown")
    await env.run()
    assert env.count("member_profiles") == 0


@pytest.mark.asyncio
async def test_ledger_expiry_and_purge(make_env):
    env = make_env()
    env.owner_here()
    gpu_posts(env)
    await env.run()
    assert env.notes("u1")
    env.clock.t += 31 * DAY
    env.owner_here()
    await env.run()
    assert env.notes("u1") == []  # topic 全部 expired
    assert env.lm.ledger.members.notes("u1", ("expired",))
    env.clock.t += 60 * DAY
    env.owner_here()
    await env.run()
    assert env.lm.ledger.members.get_profile("u1") is None  # 90 天无发言 → 物理删除


# ---------------------------------------------------------------- lens / 注入


async def _built(make_env, mode="on"):
    env = make_env(lifelike_member_mode=mode)
    env.owner_here()
    gpu_posts(env, "u1")
    gpu_posts(env, "u2")
    await env.run()
    return env


@pytest.mark.asyncio
async def test_lens_scope_pull_and_injection(make_env):
    env = await _built(make_env, "on")
    lm = env.lm
    # MG-scope：群聊 / 其他私聊 / 主动路径恒为 ''
    assert await lm.inject_blocks(str(G1), OWNER, "阿杰最近在干嘛") == []
    assert await lm.inject_blocks("private:999", "999", "阿杰最近在干嘛") == []
    assert lm.lens.render(SCOPE, OWNER, [OWNER], ["阿杰最近在干嘛"], purpose="proactive") == ""
    assert lm.lens.render(SCOPE, "999", [OWNER], ["阿杰最近在干嘛"]) == ""
    # MG-pull：没提起 → ''；重名 → ''
    assert await lm.inject_blocks(SCOPE, OWNER, "今天好累") == []
    env.names["u3"] = ["阿杰"]
    lm.ledger.members.touch_profile("u3", G1, env.clock.t)
    lm.ledger.members.upsert_alias("u3", "阿杰", "profile")
    assert await lm.inject_blocks(SCOPE, OWNER, "阿杰最近在干嘛") == []
    blocks = await lm.inject_blocks(SCOPE, OWNER, "小林最近在干嘛")
    assert len(blocks) == 1
    b = blocks[0]
    assert b.startswith("<你在群里对他的印象>") and "小林" in b and "显卡" in b
    assert "不一定准" in b and "群里看到" in b and not DIGITS.search(b)
    assert len(b.split("\n")[2]) <= 120
    # 上一条提到也算（本条没提）
    env.store.record_inbound(SCOPE, 1, OWNER, "说起来小林", env.clock.t - 10)
    env.store.record_inbound(SCOPE, 2, OWNER, "他最近咋样", env.clock.t)
    assert len(await lm.inject_blocks(SCOPE, OWNER, "他最近咋样")) == 1


@pytest.mark.asyncio
async def test_lens_shadow_counts_only_and_r11(make_env):
    env = await _built(make_env, "shadow")
    assert await env.lm.inject_blocks(SCOPE, OWNER, "小林最近在干嘛") == []
    assert env.store.get_counters().get("lifelike.member.would_inject") == 1
    env.cfg["lifelike_member_mode"] = "on"
    assert len(await env.lm.inject_blocks(SCOPE, OWNER, "小林最近在干嘛")) == 1
    # R11：群取消授权 → 旧印象不再注入
    env.cfg["lifelike_member_groups"] = ""
    assert await env.lm.inject_blocks(SCOPE, OWNER, "小林最近在干嘛") == []


@pytest.mark.asyncio
async def test_mg_back_private_text_never_written(make_env):
    env = await _built(make_env, "on")
    before = {t: env.count(t) for t in ("member_profiles", "member_aliases", "member_notes")}
    snapshot = tb.read_rows(env.store, "SELECT * FROM member_notes ORDER BY note_id")
    for text in ("小林最近得了抑郁症，你别跟别人说", "阿杰跟我吵架了", "小林住在幸福小区"):
        await env.lm.inject_blocks(SCOPE, OWNER, text)
    after = {t: env.count(t) for t in ("member_profiles", "member_aliases", "member_notes")}
    assert before == after
    assert tb.read_rows(env.store, "SELECT * FROM member_notes ORDER BY note_id") == snapshot


@pytest.mark.asyncio
async def test_member_commands(make_env):
    env = await _built(make_env, "shadow")
    lm = env.lm
    listing = await lm.member_command(OWNER, ["list"])
    assert "小林" in listing and "阿杰" in listing
    show = await lm.member_command(OWNER, ["show", "小林"])
    assert "显卡" in show
    assert "重名" in await lm.member_command(OWNER, ["show", "不存在的人"])
    assert "记下了" in await lm.member_command(OWNER, ["alias", "u1", "林哥哥"])
    alias_rows = lm.ledger.members.aliases("u1")
    assert any(a["alias"] == "林哥哥" and a["private_only"] == 1 for a in alias_rows)
    assert "不适合" in await lm.member_command(OWNER, ["alias", "u1", "哥"])
    assert "哈希" in await lm.member_command(OWNER, ["alias", "u1", "@lin_dev"])
    assert all("lin_dev" not in a["alias"] for a in lm.ledger.members.aliases("u1"))
    note_id = env.notes("u2")[0]["note_id"]
    assert "删了" in await lm.member_command(OWNER, ["forget-note", note_id])
    assert "删了" in await lm.member_command(OWNER, ["forget", "小林"])
    assert lm.ledger.members.get_profile("u1") is None
    # forget 后同一群友不再产生新条目（墓碑）
    gpu_posts(env, "u1", days=(0.3, 0.2))
    env.clock.t += 1
    await env.run()
    assert lm.ledger.members.get_profile("u1") is None
    # forget-all 两步
    assert "确认" in await lm.member_command(OWNER, ["forget-all"])
    assert "全部" in await lm.member_command(OWNER, ["forget-all", "confirm"])
    assert env.count("member_profiles") == 0
    assert "用法" in await lm.member_command(OWNER, ["bogus"])


def test_lens_render_never_raises():
    lens = MemberLens(SimpleNamespace(aliases=lambda *a: 1 / 0), mode_get=lambda: "on", authorized_groups=lambda: [G1])
    assert lens.render(SCOPE, OWNER, [OWNER], ["小林"]) == ""
