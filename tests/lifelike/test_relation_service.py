"""P1-6 / P1-11 接入：库校验、ContinuityService 接线、shadow / on / off、命令、隐私。"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from datetime import datetime
from types import SimpleNamespace

import pytest
from astrbot_plugin_iris_memory.iris_memory.continuity import models as cm
from astrbot_plugin_iris_memory.iris_memory.continuity.service import ContinuityService
from astrbot_plugin_iris_memory.iris_memory.continuity.store import ContinuityStore
from astrbot_plugin_iris_memory.iris_memory.lifelike.relation import models as rm
from astrbot_plugin_iris_memory.iris_memory.lifelike.relation.repo import RelationRepo
from astrbot_plugin_iris_memory.iris_memory.lifelike.relation.schema import RELATION_TABLES
from tests.lifelike.v3_util import written_tables

OWNER = "123456789"
SCOPE = f"private:{OWNER}"
T0 = datetime(2026, 11, 3, 15, 0, tzinfo=cm.TZ_SHANGHAI).timestamp()


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Cfg:
    def __init__(self, **values) -> None:
        self.values = {"continuity.extract_debounce_s": 0, "l1_buffer.summary_provider": "deepseek", **values}

    def get(self, key, default=None):
        return self.values.get(key, default)


class LM:
    def __init__(self, relation_reply: str = '{"hurt": true}', fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.prompts: list[str] = []
        self.relation_reply = relation_reply
        self.fail = fail

    async def generate(self, prompt, **kw):
        self.calls.append(kw)
        if kw["module"] == rm.MODULE:
            self.prompts.append(prompt)
            if self.fail:
                raise ConnectionError("down")
            return self.relation_reply
        return '{"ops":[]}'

    async def get_token_stats(self, module):
        return None

    def rel_calls(self):
        return [c for c in self.calls if c["module"] == rm.MODULE]


def _svc(tmp_path, cfg, lm, clock) -> ContinuityService:
    return ContinuityService(
        context=SimpleNamespace(get_platform_inst=lambda pid: None),
        config_source=cfg,
        state=SimpleNamespace(get_whitelist=lambda: [], resume_after_owner_message=lambda g: True),
        owner_ids_get=lambda: [OWNER],
        silent_sessions_get=lambda: [],
        data_dir=tmp_path / "data",
        llm_manager=lm,
        clock=clock,
    )


def _tables(svc) -> set[str]:
    with svc.store._lock:
        rows = svc.store._conn().execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


async def _idle(svc):
    for _ in range(300):
        await asyncio.sleep(0.01)
        if svc.relation.is_idle():
            return


_mid = [100]


async def say(svc, clock, text, dt=30.0):
    clock.t += dt
    _mid[0] += 1
    await svc.note_owner_message(SCOPE, _mid[0], clock.t, text=text, sender_id=OWNER)
    await _idle(svc)
    return _mid[0]


# ---------------------------------------------------------------- repo


@pytest.fixture
def store(tmp_path):
    s = ContinuityStore(tmp_path / "c.db", clock=Clock())
    s.open()
    yield s
    s.close()


def test_schema_is_idempotent_and_keeps_user_version(store):
    v = store.get_schema_version()
    repo = RelationRepo(store)
    repo.ensure()
    RelationRepo(store).ensure()
    assert store.get_schema_version() == v
    with store._lock:
        names = {r[0] for r in store._conn().execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(RELATION_TABLES) <= names


def test_append_requires_existing_cause(store):
    repo = RelationRepo(store)
    store.advance_watermark(SCOPE, 50, T0)
    ok = repo.make_event(SCOPE, rm.K_WARM, ["msg:50"], T0, "rule")
    assert repo.append(ok)
    assert not repo.append(repo.make_event(SCOPE, rm.K_WARM, [], T0, "rule"))
    assert not repo.append(repo.make_event(SCOPE, rm.K_WARM, ["msg:51"], T0, "rule"))  # 超过水位
    assert not repo.append(repo.make_event(SCOPE, rm.K_WARM, ["item:nope"], T0, "rule"))
    assert not repo.append(repo.make_event(SCOPE, rm.K_WARM, ["grp:-100:5"], T0, "rule"))
    assert repo.append(repo.make_event(SCOPE, rm.K_DISTRESS, ["grp:-100:5"], T0, "rule"))
    assert not repo.append(repo.make_event("group:-100", rm.K_WARM, ["msg:50"], T0, "rule"))
    assert not repo.append(repo.make_event(SCOPE, "love_points", ["msg:50"], T0, "rule"))
    bad = repo.make_event(SCOPE, rm.K_WARM, ["msg:50"], T0, "rule")
    bad.summary = "好感度+5"
    assert not repo.append(bad)
    # 库层 CHECK 也兜底
    with pytest.raises(sqlite3.IntegrityError):
        with store._tx() as db:
            db.execute(
                "INSERT INTO relationship_events(rel_event_id,ts,scope_key,kind,cause_ref,summary,detector)"
                " VALUES ('x',1,'private:1','warm','[]','s','rule')"
            )
    rel = f"rel:{ok.rel_event_id}"
    assert repo.append(repo.make_event(SCOPE, rm.K_REPAIR, [rel], T0, "rule"))


def test_undo_and_transitions_validation(store):
    repo = RelationRepo(store)
    store.advance_watermark(SCOPE, 50, T0)
    e = repo.make_event(SCOPE, rm.K_WARM, ["msg:50"], T0, "rule")
    repo.append(e)
    uid = repo.undo(SCOPE, e.rel_event_id, "cmd:60", T0 + 1)
    assert uid and repo.get_event(e.rel_event_id)["active"] == 0
    assert repo.get_event(uid)["undo_of"] == e.rel_event_id
    assert repo.undo(SCOPE, e.rel_event_id, "cmd:61", T0 + 2) is None  # 已撤回
    assert repo.undo(SCOPE, "nope", "cmd:61", T0 + 2) is None
    t = rm.BondTransition("s1", "s2", "promote", [], actor="bond_folder")
    assert repo.add_transition(SCOPE, t, T0) is None
    t = rm.BondTransition("s1", "s2", "promote", ["re_missing"], actor="bond_folder")
    assert repo.add_transition(SCOPE, t, T0) is None
    t = rm.BondTransition("s1", "s2", "promote", ["cmd:60"], actor="bond_folder")  # 自动转移只认关系事件
    assert repo.add_transition(SCOPE, t, T0) is None
    t = rm.BondTransition("s1", "s5", "set", ["cmd:60"], actor="owner_cmd")
    assert repo.add_transition(SCOPE, t, T0) is None
    t = rm.BondTransition("s1", "s2", "set", ["cmd:60"], actor="owner_cmd")
    assert repo.add_transition(SCOPE, t, T0)
    t = rm.BondTransition("s1", "s2", "promote", [uid], actor="bond_folder")
    assert repo.add_transition(SCOPE, t, T0)
    assert len(repo.transitions(SCOPE)) == 2


# ---------------------------------------------------------------- service


@pytest.mark.asyncio
async def test_off_creates_nothing_and_injects_nothing(tmp_path):
    clock, lm = Clock(), LM()
    svc = _svc(tmp_path, Cfg(lifelike_relation_mode="off"), lm, clock)
    await svc.start()
    try:
        await say(svc, clock, "你真蠢")
        await say(svc, clock, "谢谢你")
        await svc.note_owner_group_message(OWNER, -1001, 5, "我发烧了")
        text = await svc.assemble(SCOPE, sender_id=OWNER)
        assert "你们之间" not in text
        assert not lm.rel_calls()
        assert not written_tables(svc.store, RELATION_TABLES)  # v3 迁移已建表：off 不写行
        assert not any(k.startswith("lifelike.relation") for k in svc.store.get_counters())
        assert "关系模块没有开启" in await svc.relation_command(SCOPE, "iris_rel why", 7)
        svc._last_maintenance = 0.0
        await svc._maintenance()
        assert not written_tables(svc.store, RELATION_TABLES)  # v3 迁移已建表：off 不写行
        assert (await svc.relation.bond_view(SCOPE)).stage == "s1"
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_default_shadow_records_but_never_injects(tmp_path):
    clock, lm = Clock(), LM('{"hurt": true}')
    cfg = Cfg()  # 默认 lifelike_relation_mode=shadow
    svc = _svc(tmp_path, cfg, lm, clock)
    await svc.start()
    try:
        assert svc.relation.mode() == "shadow" and svc.relation.bond_mode() == "shadow"
        baseline = await svc.assemble(SCOPE, sender_id=OWNER)
        await say(svc, clock, "谢谢你，今天多亏你了")
        await say(svc, clock, "闭嘴")
        events = svc.relation.repo.events(SCOPE)
        assert [e["kind"] for e in events] == [rm.K_WARM, rm.K_HURT]
        assert events[1]["detector"] == rm.DETECTOR_LLM
        assert len(svc.relation.repo.modes(SCOPE, live_at=clock.t)) == 1
        assert len(lm.rel_calls()) == 1 and lm.rel_calls()[0]["provider_id"] == "deepseek"
        after = await svc.assemble(SCOPE, sender_id=OWNER)
        assert "你们之间" not in after and after == baseline
        proactive = await svc.assemble(SCOPE, purpose="proactive")
        assert "你们之间" not in proactive
        # 摘要是模板，不含主人原文
        for e in events:
            assert "闭嘴" not in e["summary"] and "谢谢" not in e["summary"]
        c = svc.store.get_counters()
        assert c["lifelike.budget.calls:lifelike_relation"] == 1
        assert c["lifelike.relation.llm_calls"] == 1
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_on_mode_awkward_then_distress_releases(tmp_path):
    clock, lm = Clock(), LM('{"hurt": true}')
    cfg = Cfg(lifelike_relation_mode="on", lifelike_bond_mode="off")
    svc = _svc(tmp_path, cfg, lm, clock)
    await svc.start()
    try:
        assert "你们之间" not in await svc.assemble(SCOPE, sender_id=OWNER)
        await say(svc, clock, "你真蠢")
        text = await svc.assemble(SCOPE, sender_id=OWNER)
        assert "<你们之间>" in text and "有点别扭" in text and "不刻意缩短" in text
        assert not re.search(r"\d", text.split("<你们之间>")[1].split("</你们之间>")[0])
        # 非主人 / 群聊不注入
        assert "你们之间" not in await svc.assemble("group:-1001", sender_id=OWNER)
        assert "你们之间" not in await svc.assemble(SCOPE, sender_id="999")
        # 主人说生病 → 立即解除
        await say(svc, clock, "我发烧了，好难受")
        assert not svc.relation.repo.modes(SCOPE, live_at=clock.t)
        modes = svc.relation.repo.modes(SCOPE)
        assert modes[0]["release_reason"] == rm.REL_OWNER_DISTRESS and modes[0]["release_ref"].startswith("rel:")
        # 解除后仍有未修复的 hurt（72h 内）→ 文字仍"有点别扭"，但每次变化都有原因
        assert all(e["cause_ref"] for e in svc.relation.repo.events(SCOPE))
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_group_distress_releases_cooldown(tmp_path):
    clock, lm = Clock(), LM('{"hurt": true}')
    cfg = Cfg(lifelike_relation_mode="on", **{"continuity.observe_chat_ids": "-1001"})
    svc = _svc(tmp_path, cfg, lm, clock)
    await svc.start()
    try:
        await say(svc, clock, "闭嘴")
        assert svc.relation.repo.modes(SCOPE, live_at=clock.t)
        await svc.note_owner_group_message("999", -1001, 7, "我感冒了")  # 非主人：忽略
        assert svc.relation.repo.modes(SCOPE, live_at=clock.t)
        # 未授权采集的群：主人说生病也不进关系库（审查 P6）
        await svc.note_owner_group_message(OWNER, -1009, 7, "我发烧了")
        assert svc.relation.repo.modes(SCOPE, live_at=clock.t)
        assert not [e for e in svc.relation.repo.events(SCOPE) if e["kind"] == rm.K_DISTRESS]
        await svc.note_owner_group_message(OWNER, -1001, 8, "今天感冒了哈哈，头疼")
        assert not svc.relation.repo.modes(SCOPE, live_at=clock.t)
        md = svc.relation.repo.modes(SCOPE)[0]
        assert md["release_reason"] == rm.REL_OWNER_DISTRESS_GROUP
        dist = [e for e in svc.relation.repo.events(SCOPE) if e["kind"] == rm.K_DISTRESS]
        assert dist and dist[0]["context"] == rm.CTX_GROUP
        # 不落群 id / 消息 id 原值，只存短哈希
        ref = dist[0]["cause_ref"][0]
        assert ref.startswith("grp:h") and "1001" not in ref
        assert "感冒" not in json.dumps(dist, ensure_ascii=False)
        # 群发言原文不进私聊 prompt
        assert "感冒" not in await svc.assemble(SCOPE, sender_id=OWNER)
        # 普通群发言不写事件
        n = len(svc.relation.repo.events(SCOPE))
        await svc.note_owner_group_message(OWNER, -1001, 9, "今天吃火锅")
        assert len(svc.relation.repo.events(SCOPE)) == n
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_repair_made_up_said_once(tmp_path):
    clock, lm = Clock(), LM('{"hurt": true}')
    svc = _svc(tmp_path, Cfg(lifelike_relation_mode="on", lifelike_bond_mode="off"), lm, clock)
    await svc.start()
    try:
        await say(svc, clock, "你真蠢")
        await say(svc, clock, "刚才是我不对，说话太冲了")
        assert not svc.relation.repo.modes(SCOPE, live_at=clock.t)
        text = await svc.assemble(SCOPE, sender_id=OWNER)
        assert "刚和好" in text and "说一次就够" in text
        clock.t += 5
        await svc.record_reply_sent(SCOPE, "嗯，没事啦", [9001], reply_to_msg_id=_mid[0])
        text = await svc.assemble(SCOPE, sender_id=OWNER)
        assert "刚和好" in text and "说一次" not in text
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_confirm_false_budget_cap_and_failures_write_nothing(tmp_path):
    clock = Clock()
    lm = LM('{"hurt": false}')
    svc = _svc(tmp_path, Cfg(), lm, clock)
    await svc.start()
    try:
        await say(svc, clock, "你真蠢")
        assert not [e for e in svc.relation.repo.events(SCOPE) if e["kind"] == rm.K_HURT]
    finally:
        await svc.stop()

    lm = LM('{"hurt": true}')
    svc = _svc(tmp_path / "b", Cfg(lifelike_relation_detector_daily_cap=0), lm, clock)
    await svc.start()
    try:
        await say(svc, clock, "你真蠢")
        assert not lm.rel_calls() and not svc.relation.repo.events(SCOPE)
    finally:
        await svc.stop()

    lm = LM('{"hurt": true}')
    svc = _svc(tmp_path / "c", Cfg(lifelike_budget_cheap_calls_daily=0), lm, clock)
    await svc.start()
    try:
        await say(svc, clock, "你真蠢")
        assert not lm.rel_calls() and not svc.relation.repo.events(SCOPE)
    finally:
        await svc.stop()

    lm = LM(fail=True)
    svc = _svc(tmp_path / "d", Cfg(lifelike_relation_detector_daily_cap=50), lm, clock)
    await svc.start()
    try:
        for _ in range(6):
            await say(svc, clock, "闭嘴")
        assert len(lm.rel_calls()) == 5  # 连续 5 次失败熔断
        assert not svc.relation.repo.events(SCOPE)
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_hurt_during_distress_does_not_start_cooldown(tmp_path):
    clock, lm = Clock(), LM('{"hurt": true}')
    svc = _svc(tmp_path, Cfg(lifelike_relation_mode="on"), lm, clock)
    await svc.start()
    try:
        await say(svc, clock, "今天好累，心情很差")
        await say(svc, clock, "闭嘴")
        kinds = [e["kind"] for e in svc.relation.repo.events(SCOPE)]
        assert rm.K_HURT in kinds
        assert not svc.relation.repo.modes(SCOPE)
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_commands_why_undo_reset_and_bond(tmp_path):
    clock, lm = Clock(), LM('{"hurt": true}')
    cfg = Cfg(lifelike_relation_mode="on", lifelike_bond_mode="on")
    svc = _svc(tmp_path, cfg, lm, clock)
    await svc.start()
    try:
        await say(svc, clock, "你真蠢")
        why = await svc.relation_command(SCOPE, "iris_rel", 900)
        assert "有点别扭" in why and "语气收一点" in why
        hurt = [e for e in svc.relation.repo.events(SCOPE) if e["kind"] == rm.K_HURT][0]
        out = await svc.relation_command(SCOPE, f"/iris_rel undo {hurt['rel_event_id']}", 901)
        assert "已撤回" in out
        assert not svc.relation.repo.modes(SCOPE, live_at=clock.t)
        assert (await svc.relation_command(SCOPE, "iris_rel undo nope", 902)).startswith("没找到")
        await say(svc, clock, "闭嘴")
        assert "1 个" in await svc.relation_command(SCOPE, "iris_rel reset", 903)
        assert not svc.relation.repo.modes(SCOPE, live_at=clock.t)
        bond = await svc.relation_command(SCOPE, "iris_rel bond", 904)
        assert "聊得来的网友" in bond
        for banned in ("门槛", "进度", "还差", "P1", "规则"):
            assert banned not in bond
        assert "熟悉的朋友" in await svc.relation_command(SCOPE, "iris_rel bond set 熟悉的朋友", 905)
        assert (await svc.relation.bond_view(SCOPE)).stage == "s2"
        out = await svc.relation_command(SCOPE, "iris_rel bond set 网友", 906)
        assert "冻结" in out
        bv = await svc.relation.bond_view(SCOPE)
        assert bv.stage == "s1" and bv.frozen and bv.care_stage == "s1"
        assert "解冻" in await svc.relation_command(SCOPE, "iris_rel bond unfreeze", 907)
        assert "冻结" in await svc.relation_command(SCOPE, "iris_rel bond freeze", 908)
        rows = svc.relation.repo.transitions(SCOPE)
        assert all(r["cause_refs"] for r in rows)
        out = await svc.relation_command(SCOPE, f"iris_rel bond undo {rows[-1]['transition_id']}", 909)
        assert "只能撤销" in out
        assert "用法" in await svc.relation_command(SCOPE, "iris_rel nonsense", 910)
        assert "主人私聊" in await svc.relation_command("private:999", "iris_rel", 911)
        # 阶段行进入 prompt（on），无数字、无进度
        text = await svc.assemble(SCOPE, sender_id=OWNER)
        assert "聊得来的网友：" in text
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_owner_distance_request_demotes_and_freezes(tmp_path):
    clock, lm = Clock(), LM()
    cfg = Cfg(lifelike_relation_mode="on", lifelike_bond_mode="on")
    svc = _svc(tmp_path, cfg, lm, clock)
    await svc.start()
    try:
        await svc.relation_command(SCOPE, "iris_rel bond set 亲近", 800)
        assert (await svc.relation.bond_view(SCOPE)).stage == "s3"
        await say(svc, clock, "我们还是保持点距离吧，像以前那样")
        bv = await svc.relation.bond_view(SCOPE)
        assert bv.stage == "s1" and bv.frozen
        await say(svc, clock, "不用保持距离了")
        assert not (await svc.relation.bond_view(SCOPE)).frozen
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_maintenance_daily_bond_shadow_proposals(tmp_path):
    clock, lm = Clock(), LM()
    svc = _svc(tmp_path, Cfg(), lm, clock)  # 默认 shadow
    await svc.start()
    try:
        for d in range(8):
            clock.t = T0 + d * rm.DAY
            await say(svc, clock, "谢谢你" if d % 2 else "帮我记一下这个", dt=0)
        clock.t = T0 + 22 * rm.DAY
        svc._last_maintenance = 0.0
        await svc._maintenance()
        rows = svc.relation.repo.transitions(SCOPE)
        assert len(rows) == 1
        r = rows[0]
        assert (r["kind"], r["status"], r["track"], r["to_stage"]) == ("promote", "proposed", "shadow", "s2")
        # 同一天不重复折叠
        svc._last_maintenance = 0.0
        await svc._maintenance()
        assert len(svc.relation.repo.transitions(SCOPE)) == 1
        # shadow 不影响别的模块，也不注入
        assert (await svc.relation.bond_view(SCOPE)).stage == "s1"
        assert "你们之间" not in await svc.assemble(SCOPE, sender_id=OWNER)
        out = await svc.relation_command(SCOPE, "iris_rel bond", 950)
        assert "shadow 轨迹：熟悉的朋友" in out and "现在：聊得来的网友" in out
        # 主人可以把 shadow 提议批准进正式轨
        out = await svc.relation_command(SCOPE, "iris_rel bond approve " + r["transition_id"], 951)
        assert "已批准" in out
        cfg_on = svc.relation._get
        svc.relation._get = lambda k, d=None: "on" if k in (rm.KEY_MODE, rm.KEY_BOND_MODE) else cfg_on(k, d)
        assert (await svc.relation.bond_view(SCOPE)).stage == "s2"
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_manual_approval_and_promise_kept_derivation(tmp_path):
    clock, lm = Clock(), LM()
    cfg = Cfg(lifelike_relation_mode="on", lifelike_bond_mode="on", lifelike_bond_promote_approval="manual")
    svc = _svc(tmp_path, cfg, lm, clock)
    await svc.start()
    try:
        svc._last_maintenance = 0.0
        await svc._maintenance()  # 初始化 promise 游标
        item = svc.store.create_item(
            subject_id=OWNER, subject_type="user", scope_key=SCOPE, topic="提醒", summary="答应提醒他",
            kind=cm.KIND_BOT_PROMISE, source_kind=cm.SOURCE_SHARED_DIALOGUE, source_refs=[], allowed_audiences=[SCOPE],
            status=cm.STATUS_ACTIVE, dedupe_key="k1",
        )
        for d in range(8):
            clock.t = T0 + d * rm.DAY + 60
            await say(svc, clock, "谢谢你" if d % 2 else "帮我记一下这个", dt=0)
        clock.t = T0 + 9 * rm.DAY
        svc.store.update_item(item["item_id"], expected_revision=item["revision"], reason="test",
                              status=cm.STATUS_DONE)
        clock.t = T0 + 22 * rm.DAY
        svc._last_maintenance = 0.0
        await svc._maintenance()
        kinds = [e["kind"] for e in svc.relation.repo.events(SCOPE)]
        assert rm.K_PROMISE_KEPT in kinds
        rows = svc.relation.repo.transitions(SCOPE)
        assert rows[-1]["status"] == "proposed" and rows[-1]["track"] == "on"
        assert (await svc.relation.bond_view(SCOPE)).stage == "s1"
        assert "待批准" in await svc.relation_command(SCOPE, "iris_rel bond", 960)
        assert "已批准" in await svc.relation_command(SCOPE, "iris_rel bond approve", 961)
        assert (await svc.relation.bond_view(SCOPE)).stage == "s2"
        assert (await svc.relation.bond_view(SCOPE)).allows["life_share_push"] == "yes"
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_start_stage_on_adds_only_the_stage_block(tmp_path):
    """E9 口径：起点阶段 on 与 off 相比，只多 <你们之间> 一段（一行阶段描述）。"""
    clock = Clock()
    outs = {}
    for mode in ("off", "on"):
        svc = _svc(tmp_path / mode, Cfg(lifelike_relation_mode=mode, lifelike_bond_mode=mode), LM(), clock)
        await svc.start()
        try:
            await svc.note_owner_message(SCOPE, 10, clock.t, text="周五要考试", sender_id=OWNER)
            await svc.record_reply_sent(SCOPE, "好，加油", [900], reply_to_msg_id=10)
            outs[mode] = await svc.assemble(SCOPE, sender_id=OWNER)
        finally:
            await svc.stop()
    off_lines, on_lines = outs["off"].splitlines(), outs["on"].splitlines()
    assert off_lines
    extra = [ln for ln in on_lines if ln not in off_lines]
    assert extra == ["<你们之间>", f"聊得来的网友：{rm.STAGE_DESC['s1']}", "</你们之间>"]
    assert [ln for ln in on_lines if ln not in extra] == off_lines


@pytest.mark.asyncio
async def test_long_absence_only_adds_greeting_line_stage_unchanged(tmp_path):
    clock, lm = Clock(), LM()
    svc = _svc(tmp_path, Cfg(lifelike_relation_mode="on", lifelike_bond_mode="on"), lm, clock)
    await svc.start()
    try:
        await svc.relation_command(SCOPE, "iris_rel bond set 熟悉的朋友", 700)
        await say(svc, clock, "晚安")
        clock.t += 50 * rm.DAY
        await say(svc, clock, "好久不见")
        text = await svc.assemble(SCOPE, sender_id=OWNER)
        assert "有阵子没聊了" in text and "别提他没来" in text
        assert "熟悉的朋友：" in text
        svc._last_maintenance = 0.0
        await svc._maintenance()
        assert (await svc.relation.bond_view(SCOPE)).stage == "s2"  # 不因冷落回退
        clock.t += 3 * 3600
        assert "有阵子没聊了" not in await svc.assemble(SCOPE, sender_id=OWNER)
    finally:
        await svc.stop()
