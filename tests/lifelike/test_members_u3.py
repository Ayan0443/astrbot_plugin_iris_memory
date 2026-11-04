"""U3：群友认知覆盖全部白名单群 + 跨群合并 + 新纳入群回补 + 采集动态并入白名单群。

tmp_path 库 + 原创群聊序列 + 假 Telethon client，不连真实数据、不发任何消息。
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from astrbot_plugin_iris_memory.iris_memory.continuity import channel_ingest as ci
from astrbot_plugin_iris_memory.iris_memory.continuity.service import ContinuityConfig
from astrbot_plugin_iris_memory.iris_memory.lifelike.members import ledger as lg
from astrbot_plugin_iris_memory.iris_memory.lifelike.memory import service as mem_service
from astrbot_plugin_iris_memory.iris_memory.lifelike.memory import tombstones as tb
from astrbot_plugin_iris_memory.iris_memory.lifelike.social import l2_filter
from tests.continuity.test_channel import FakeClient, FakeInst, Msg, no_sleep
from tests.continuity.test_service import FakeConfig, _service
from tests.lifelike.test_members import CH, DAY, G1, G2, OWNER, SCOPE, gpu_posts, make_env  # noqa: F401

G3 = -1001000000003
WL = [OWNER, str(G1), str(G2), str(CH), f"{G3}#513304"]


def wl_env(make_env, wl=WL, **cfg):
    env = make_env(lifelike_member_groups="*whitelist", **cfg)
    env.observe = (CH, G1, G2, G3)
    env.wl = list(wl) if wl is not None else None
    env.lm.ledger._whitelist = lambda: env.wl
    return env


# ---------------------------------------------------------------- 授权群：*whitelist / * / 主人在群条件


def test_whitelist_group_ids_excludes_private_owner_and_channel():
    assert l2_filter.whitelist_group_ids(WL, exclude=[OWNER, CH]) == [G1, G2, G3]
    assert l2_filter.whitelist_group_ids(None) == []
    assert l2_filter.split_group_tokens("*whitelist, -100123") == (False, True, ["-100123"])
    assert l2_filter.split_group_tokens("*") == (True, False, [])


def test_authorized_groups_whitelist_dynamic_and_owner_policy(make_env):
    env = wl_env(make_env)
    # auto + 通配：不要求主人在群；主人频道、私聊 id 不算
    assert env.lm.ledger.authorized_groups() == [G1, G2, G3]
    # 白名单以后新加 / 去掉的群动态生效
    env.wl = [OWNER, str(G1)]
    assert env.lm.ledger.authorized_groups() == [G1]
    env.wl = WL
    # 不在采集群里的白名单群不授权（没有数据来源）
    env.observe = (CH, G1)
    assert env.lm.ledger.authorized_groups() == [G1]
    env.observe = (CH, G1, G2, G3)
    # 白名单未启用（None）→ 通配白名单不扩大范围
    env.wl = None
    assert env.lm.ledger.authorized_groups() == []
    env.wl = WL
    # 强制要求主人在群
    env.cfg["lifelike_member_require_owner_present"] = "true"
    assert env.lm.ledger.authorized_groups() == []
    env.owner_here(chat=G2)
    assert env.lm.ledger.authorized_groups() == [G2]
    # "*" = 全部采集群（去掉主人频道）
    env.cfg["lifelike_member_require_owner_present"] = "false"
    env.cfg["lifelike_member_groups"] = "*"
    assert env.lm.ledger.authorized_groups() == [G1, G2, G3]
    # 显式列表 + auto：保持 73edc2c 行为（要求主人在群）
    env.cfg["lifelike_member_require_owner_present"] = "auto"
    env.cfg["lifelike_member_groups"] = f"{G1}"
    assert env.lm.ledger.authorized_groups() == []


# ---------------------------------------------------------------- 跨群合并


@pytest.mark.asyncio
async def test_cross_group_merge_one_profile_topics_and_sources(make_env):
    env = wl_env(make_env, lifelike_member_mode="on")
    # 同一 Telegram 用户在三个群各说一天显卡 → 一份档案、一条话题、天数跨群累计
    env.post(G1, "u1", "这周终于把显卡换了", env.clock.t - 3 * DAY)
    env.post(G2, "u1", "新显卡到了，跑游戏好流畅", env.clock.t - 2 * DAY)
    env.post(G3, "u1", "显卡驱动又崩了，烦", env.clock.t - 1 * DAY)
    gpu_posts(env, "u2", chat=G2)
    await env.run()
    ms = env.lm.ledger.members
    prof = ms.get_profile("u1")
    assert sorted(json.loads(prof["chat_ids"])) == sorted([str(G1), str(G2), str(G3)])
    notes = env.notes("u1")
    assert [n["text"] for n in notes] == ["最近在群里聊过「显卡」"]
    assert notes[0]["evidence_days"] == 3 and notes[0]["confidence_band"] == "较确定"
    assert {r.split(":")[0] for r in json.loads(notes[0]["evidence_refs"])} == {str(G1), str(G2), str(G3)}
    assert [a["alias"] for a in ms.aliases("u1")] == ["小林"]
    assert tb.read_rows(env.store, "SELECT COUNT(*) AS n FROM member_profiles WHERE member_id='u1'")[0]["n"] == 1
    # 命令：来源群为集合
    show = await env.lm.member_command(OWNER, ["show", "小林"])
    assert "3 个" in show and "跨群合并" in show and "显卡" in show
    listing = await env.lm.member_command(OWNER, ["list"])
    assert "来源群3个" in listing
    # on 模式注入：主人私聊提起才注入；群聊不注入
    blocks = await env.lm.inject_blocks(SCOPE, OWNER, "小林最近怎么样")
    assert len(blocks) == 1 and "显卡" in blocks[0]
    assert await env.lm.inject_blocks(str(G2), OWNER, "小林最近怎么样") == []
    assert await env.lm.inject_blocks(SCOPE, OWNER, "今天好累") == []
    # R11：某来源群移出白名单，只要还有一个来源群授权就还能注入；全部移出 → 不注入
    env.wl = [OWNER, str(G2)]
    assert len(await env.lm.inject_blocks(SCOPE, OWNER, "小林最近怎么样")) == 1
    env.wl = [OWNER]
    assert await env.lm.inject_blocks(SCOPE, OWNER, "小林最近怎么样") == []


@pytest.mark.asyncio
async def test_cross_group_privacy_gates(make_env):
    env = wl_env(make_env)
    gpu_posts(env, "u1", chat=G1, days=(5, 4, 3))
    gpu_posts(env, "kid", chat=G1, days=(5, 4, 3))
    await env.run()
    assert env.notes("u1") and env.notes("kid")
    # 在 G2 自述未成年 → 整人删除（含 G1 来的数据）；在 G3 说"别记我" → 整人删除
    env.post(G2, "kid", "我今年15岁，刚上高中", env.clock.t - DAY)
    env.post(G3, "u1", "别记我的事了啊", env.clock.t - DAY + 5)
    await env.run()
    ms = env.lm.ledger.members
    assert ms.get_profile("kid") is None and ms.get_profile("u1") is None
    # 墓碑跨群生效：之后任何白名单群的发言都不再建档
    gpu_posts(env, "u1", chat=G2, days=(0.5, 0.4))
    gpu_posts(env, "kid", chat=G3, days=(0.5, 0.4))
    env.clock.t += 1
    await env.run()
    assert ms.get_profile("u1") is None and ms.get_profile("kid") is None
    # 敏感发言不落库（任何群）
    env.post(G3, "u5", "我最近确诊了焦虑症", env.clock.t - DAY)
    env.clock.t += 1
    rep = await env.run()
    assert rep.sensitive_dropped >= 1 and ms.get_profile("u5") is None


@pytest.mark.asyncio
async def test_group_theme_word_not_personal_topic(make_env):
    # 某个群里人人都在说的群主题词，不当作跨群合并后某人的个人话题
    env = wl_env(make_env)
    for i, member in enumerate(["a1", "a2", "a3", "a4", "a5"]):
        for d in (3, 2, 1):
            env.post(G3, member, ["今天的番剧好看", "番剧更新了没", "补完番剧了"][d - 1], env.clock.t - d * DAY + i)
    await env.run()
    assert all("番剧" not in n["text"] for m in ["a1", "a2", "a3"] for n in env.notes(m))


# ---------------------------------------------------------------- 新纳入群回补（分批，≤30 天）


@pytest.mark.asyncio
async def test_newly_authorized_group_backfill_batched(make_env, monkeypatch):
    monkeypatch.setattr(lg, "BACKFILL_BATCH", 3)
    env = make_env()  # 显式 G1（73edc2c 配置）
    # G2 早已在采集（rowid 在游标之前），但未授权
    gpu_posts(env, "u2", chat=G2, days=(5, 4, 3))
    env.post(G2, "u2", "显卡太老了", env.clock.t - 40 * DAY)  # 超过 30 天：不回补
    env.owner_here()
    gpu_posts(env, "u1", chat=G1)
    await env.run()
    assert env.lm.ledger.members.get_profile("u2") is None
    state = env.lm.ledger._cursor_state()
    assert state["groups"] == [G1]
    # 切到 *whitelist：G2 纳入，游标之前的帖子分批回补
    env.cfg["lifelike_member_groups"] = "*whitelist"
    env.lm.ledger._whitelist = lambda: WL
    env.clock.t += 1
    rep = await env.run()
    assert rep.posts == 3  # 第一批 = BACKFILL_BATCH（游标之前、30 天内）
    assert env.lm.ledger._cursor_state()["bf"]["groups"]
    env.clock.t += 1
    await env.run()
    env.clock.t += 1
    await env.run()
    state = env.lm.ledger._cursor_state()
    assert state["bf"] is None and set(state["groups"]) >= {G1, G2}
    assert env.lm.ledger.members.get_profile("u2") is not None
    assert [n["text"] for n in env.notes("u2")] == ["最近在群里聊过「显卡」"]
    # 补完后不再重复处理
    env.clock.t += 1
    assert (await env.run()).posts == 0


@pytest.mark.asyncio
async def test_legacy_cursor_format_backfills_idempotently(make_env):
    env = make_env()
    env.owner_here()
    gpu_posts(env, "u1", chat=G1)
    await env.run()
    before = tb.read_rows(env.store, "SELECT note_id,text,evidence_days FROM member_notes ORDER BY note_id")
    # 73edc2c 的旧格式游标：只有 rowid
    env.store.set_cursor(lg.CURSOR, json.dumps({"rowid": env.lm.ledger._cursor()}))
    env.clock.t += 1
    await env.run()
    after = tb.read_rows(env.store, "SELECT note_id,text,evidence_days FROM member_notes ORDER BY note_id")
    assert before == after
    assert env.lm.ledger._cursor_state()["groups"] == [G1]
    assert tb.read_rows(env.store, "SELECT COUNT(*) AS n FROM member_profiles")[0]["n"] == 1


@pytest.mark.asyncio
async def test_mode_off_untouched_with_whitelist(make_env):
    env = wl_env(make_env, lifelike_member_mode="off")
    gpu_posts(env, "u1", chat=G2)
    assert await env.run() is None
    assert env.store.get_cursor(lg.CURSOR) is None


# ---------------------------------------------------------------- 私聊 L2 收口的通配解析


def test_l2_allowed_groups_wildcard_resolution(make_env):
    env = wl_env(make_env)
    cfg = SimpleNamespace(get=lambda k, d=None: {"lifelike_member_groups": "*whitelist"}.get(k, d))
    try:
        l2_filter.set_group_resolver(None)
        assert l2_filter.allowed_groups_from_config(cfg) == []  # 无解析器：通配不扩大
        mem_service.set_current(env.lm)
        assert l2_filter.allowed_groups_from_config(cfg) == [str(G1), str(G2), str(G3)]
        explicit = SimpleNamespace(get=lambda k, d=None: {"lifelike_member_groups": f"{G1}"}.get(k, d))
        assert l2_filter.allowed_groups_from_config(explicit) == [str(G1)]
        pinned = SimpleNamespace(
            get=lambda k, d=None: {"lifelike_private_l2_groups": f"{G2}", "lifelike_member_groups": "*whitelist"}.get(k, d)
        )
        assert l2_filter.allowed_groups_from_config(pinned) == [str(G2)]
    finally:
        mem_service.set_current(None)


# ---------------------------------------------------------------- continuity：observe_chat_ids 的 *whitelist


def test_config_parses_whitelist_token():
    c = ContinuityConfig.from_config({"continuity": {"observe_chat_ids": [str(CH), "*whitelist"]}})
    assert c.observe_whitelist and c.observe_chat_ids == (CH,)
    assert "observe_whitelist" not in c.as_dict()
    plain = ContinuityConfig.from_config({"continuity": {"observe_chat_ids": [str(CH), str(G1)]}})
    assert not plain.observe_whitelist and plain.explicit_observe_ids == (CH, G1)


def test_service_cfg_expands_whitelist(tmp_path):
    cfg = FakeConfig(observe_chat_ids=[str(CH), str(G1), "*whitelist"], owner_channel_ids=[str(CH)])
    svc = _service(tmp_path, cfg)
    wl = {"ids": [OWNER, str(G1), str(G2), str(CH), f"{G3}#1"]}
    svc._context = SimpleNamespace(
        get_platform_inst=lambda pid: None,
        get_config=lambda: {"platform_settings": {"enable_id_white_list": True, "id_whitelist": wl["ids"]}},
    )
    c = svc.cfg
    assert c.observe_chat_ids == (CH, G1, G2, G3)
    assert c.explicit_observe_ids == (CH, G1)
    wl["ids"] = [OWNER, str(G1)]
    assert svc.cfg.observe_chat_ids == (CH, G1)
    # 没有 *whitelist：与 73edc2c 一致，不读白名单
    cfg.set("observe_chat_ids", [str(CH), str(G1)])
    wl["ids"] = [OWNER, str(G2)]
    assert svc.cfg.observe_chat_ids == (CH, G1) and not svc.cfg.observe_whitelist


# ---------------------------------------------------------------- 采集：动态换绑 + 新群历史回补


BASE = 1_800_000_000.0


def hmsg(i, age_days, sender=222):
    return Msg(id=i, date=datetime.fromtimestamp(BASE - age_days * 86400, tz=timezone.utc), sender_id=sender,
               raw_text=f"消息{i}")


class DynClient(FakeClient):
    def __init__(self, history, broadcast=()):
        super().__init__(history)
        self.broadcast = set(broadcast)

    async def get_entity(self, chat):
        return SimpleNamespace(broadcast=chat in self.broadcast)

    async def iter_messages(self, chat, limit=None, min_id=0, reverse=False, offset_id=0):
        self.iter_calls.append({"chat": chat, "limit": limit, "min_id": min_id, "offset_id": offset_id})
        msgs = [x for x in self.history.get(chat, []) if x.id > (min_id or 0) and (not offset_id or x.id < offset_id)]
        msgs.sort(key=lambda x: x.id, reverse=not reverse)
        for x in msgs[: limit or None]:
            yield x


@pytest.mark.asyncio
async def test_ingestor_dynamic_chats_and_history(tmp_path):
    from astrbot_plugin_iris_memory.iris_memory.continuity.store import ContinuityStore

    clock = lambda: BASE  # noqa: E731
    store = ContinuityStore(tmp_path / "c.db", clock=clock)
    store.open()
    try:
        G4 = -1009999
        # G3：近 450 条在 30 天内，再往前是 40 天前的旧消息
        g3 = [hmsg(i, 40) for i in range(1, 51)] + [hmsg(i, 10) for i in range(51, 501)]
        client = DynClient({G1: [hmsg(i, 1) for i in range(1, 4)], G3: g3, G4: [hmsg(1, 1)]}, broadcast={G4})
        wanted = {"ids": [G1]}
        ing = ci.ChannelIngestor(
            store, lambda: FakeInst(client), [G1], {OWNER}, {CH}, clock, sleep=no_sleep,
            builder_factory=lambda ids: ("NewMessage", tuple(ids)),
            chat_ids_get=lambda: wanted["ids"],
            history_chats_get=lambda: set(wanted["ids"]) - {G1},
            history_period_s=0,
        )
        assert await ing.check_binding() is True
        assert client.handlers[-1][1] == ("NewMessage", (G1,))
        # 白名单新增 G3（群）与 G4（广播频道）
        wanted["ids"] = [G1, G3, G4]
        added = await ing.refresh_chats()
        assert added == [G3] and ing.chat_ids == sorted([G1, G3])
        assert len(client.handlers) == 1 and client.handlers[0][1] == ("NewMessage", tuple(sorted([G1, G3])))
        assert len(store.posts_since([G3], 0, 10_000)) == ci.FIRST_RUN_LIMIT
        assert not store.posts_since([G4], 0, 10_000)
        # 历史回补：每步一页（≤200），只存 30 天内；碰到更早的消息即完成
        n1 = await ing.history_tick(force=True)
        assert 0 < n1 <= ci.BACKFILL_PAGE
        while store.get_cursor(ci.history_cursor_name(G3)) != "done":
            await ing.history_tick(force=True)
        ids = {p["msg_id"] for p in store.posts_since([G3], 0, 10_000)}
        assert ids == set(range(51, 501))
        assert all(c["limit"] <= ci.BACKFILL_PAGE for c in client.iter_calls if c["chat"] == G3)
        # 显式群（G1）不做历史回补
        assert store.get_cursor(ci.history_cursor_name(G1)) is None
        # 白名单去掉 G3 → 换绑
        wanted["ids"] = [G1]
        await ing.refresh_chats()
        assert ing.chat_ids == [G1] and client.handlers[0][1] == ("NewMessage", (G1,))
        assert client.sent == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_ingestor_without_dynamic_unchanged(tmp_path):
    from astrbot_plugin_iris_memory.iris_memory.continuity.store import ContinuityStore

    store = ContinuityStore(tmp_path / "c.db", clock=lambda: BASE)
    store.open()
    try:
        client = DynClient({G1: [hmsg(1, 1)]})
        ing = ci.ChannelIngestor(store, lambda: FakeInst(client), [G1], {OWNER}, {CH}, lambda: BASE,
                                 sleep=no_sleep, builder_factory=lambda ids: ("NewMessage", tuple(ids)))
        await ing.check_binding()
        assert await ing.refresh_chats() == [] and await ing.history_tick(force=True) == 0
        assert ing.chat_ids == [G1]
    finally:
        store.close()


# ---------------------------------------------------------------- 审查修复（p2u review 7 / M5 / 8 / 9）


@pytest.mark.asyncio
async def test_transient_whitelist_failure_does_not_rescan(make_env, monkeypatch):
    """白名单暂时读不到：已处理过的群不掉出 known，恢复后不整段重扫"""
    env = wl_env(make_env)
    env.cfg["lifelike_member_groups"] = f"{G1},*whitelist"
    env.owner_here()
    gpu_posts(env, "u1", chat=G1)
    gpu_posts(env, "u2", chat=G2)
    await env.run()
    assert set(env.lm.ledger._cursor_state()["groups"]) >= {G1, G2}
    env.wl = None  # 读白名单失败 → *whitelist 展开为空，只剩显式 G1
    env.clock.t += 1
    await env.run()
    assert set(env.lm.ledger._cursor_state()["groups"]) >= {G1, G2}
    env.wl = WL
    env.clock.t += 1
    rep = await env.run()
    assert rep.posts == 0 and env.lm.ledger._cursor_state()["bf"] is None


@pytest.mark.asyncio
async def test_recompute_honours_minor_and_forget_in_unbackfilled_posts(make_env, monkeypatch):
    """M5：跨群重算话题时读到还没轮到回补的"我才初二""别记我" → 直接墓碑，不建印象"""
    monkeypatch.setattr(lg, "BACKFILL_BATCH", 1)
    env = make_env()
    # G2（未授权）里早于游标的帖子：u2 先聊显卡，再说自己在读初二；u3 说"别记我"
    gpu_posts(env, "u2", chat=G2, days=(6, 5, 4))
    env.post(G2, "u2", "我在读初二，作业好多", env.clock.t - 3.5 * DAY)
    env.post(G2, "u3", "别记我的事", env.clock.t - 3.5 * DAY)
    env.owner_here()
    gpu_posts(env, "u1", chat=G1)
    await env.run()
    assert env.lm.ledger.members.get_profile("u2") is None  # G2 未授权
    # 切到 *whitelist：G2 回补每轮只走 1 条；u2/u3 在 G1 的新发言触发跨群重算
    env.cfg["lifelike_member_groups"] = "*whitelist"
    env.lm.ledger._whitelist = lambda: WL
    gpu_posts(env, "u2", chat=G1, days=(0.5,))
    gpu_posts(env, "u3", chat=G1, days=(0.5,))
    env.clock.t += 1
    rep = await env.run()
    assert rep.minors == 1 and rep.forget_requests == 1
    assert env.lm.ledger.members.get_profile("u2") is None
    assert env.lm.ledger.members.get_profile("u3") is None
    assert tb.has_tombstone(env.store, tb.member_hash("u2"))
    assert tb.has_tombstone(env.store, tb.member_hash("u3"))


class FlakyEntityClient(DynClient):
    def __init__(self, *a, fail=True, **kw):
        super().__init__(*a, **kw)
        self.fail = fail

    async def get_entity(self, chat):
        if self.fail:
            raise ConnectionError("x")
        return await super().get_entity(chat)


@pytest.mark.asyncio
async def test_broadcast_check_failure_defers_instead_of_fail_open(tmp_path):
    from astrbot_plugin_iris_memory.iris_memory.continuity.store import ContinuityStore

    store = ContinuityStore(tmp_path / "c.db", clock=lambda: BASE)
    store.open()
    try:
        G4 = -1009999
        client = FlakyEntityClient({G1: [hmsg(1, 1)], G4: [hmsg(1, 1)]}, broadcast={G4})
        wanted = {"ids": [G1]}
        ing = ci.ChannelIngestor(
            store, lambda: FakeInst(client), [G1], {OWNER}, {CH}, lambda: BASE, sleep=no_sleep,
            builder_factory=lambda ids: ("NewMessage", tuple(ids)),
            chat_ids_get=lambda: wanted["ids"], history_chats_get=lambda: set(wanted["ids"]) - {G1},
            history_period_s=0,
        )
        assert await ing.check_binding() is True
        wanted["ids"] = [G1, G4]
        assert await ing.refresh_chats() == []  # 查不成：不采、不缓存
        assert ing.chat_ids == [G1] and G4 not in ing._verified
        client.fail = False
        assert await ing.refresh_chats() == []  # 下一轮查到是广播频道 → 跳过
        assert G4 in ing._broadcast_skipped and not store.posts_since([G4], 0, 100)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_history_skips_persistently_bad_message(tmp_path, monkeypatch):
    from astrbot_plugin_iris_memory.iris_memory.continuity.store import ContinuityStore

    store = ContinuityStore(tmp_path / "c.db", clock=lambda: BASE)
    store.open()
    try:
        g3 = [hmsg(i, 10) for i in range(1, 301)]
        client = DynClient({G1: [hmsg(1, 1)], G3: g3})
        wanted = {"ids": [G1]}
        ing = ci.ChannelIngestor(
            store, lambda: FakeInst(client), [G1], {OWNER}, {CH}, lambda: BASE, sleep=no_sleep,
            builder_factory=lambda ids: ("NewMessage", tuple(ids)),
            chat_ids_get=lambda: wanted["ids"], history_chats_get=lambda: set(wanted["ids"]) - {G1},
            history_period_s=0,
        )
        assert await ing.check_binding() is True
        wanted["ids"] = [G1, G3]
        await ing.refresh_chats()
        real = store.add_observed_post

        def flaky(**post):
            if post["chat_id"] == G3 and post["msg_id"] == 200:
                raise RuntimeError("bad row")
            return real(**post)

        monkeypatch.setattr(store, "add_observed_post", flaky)
        for _ in range(10):
            if store.get_cursor(ci.history_cursor_name(G3)) == "done":
                break
            await ing.history_tick(force=True)
        assert store.get_cursor(ci.history_cursor_name(G3)) == "done"
        ids = {p["msg_id"] for p in store.posts_since([G3], 0, 10_000)}
        assert 200 not in ids and set(range(1, 200)) <= ids
    finally:
        store.close()
