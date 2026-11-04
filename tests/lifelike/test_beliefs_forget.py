"""P1-7：user_beliefs、ForgetService、tombstones、dream 归档、隐私收口 R1（tmp_path 库，不连真实数据）。"""

import json
import re
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_iris_memory.iris_memory.continuity import models as m
from astrbot_plugin_iris_memory.iris_memory.continuity.extractor import ItemExtractor, _Op
from astrbot_plugin_iris_memory.iris_memory.continuity.store import ContinuityStore
from astrbot_plugin_iris_memory.iris_memory.lifelike.budget import CheapBudget
from astrbot_plugin_iris_memory.iris_memory.lifelike.memory import beliefs as bl
from astrbot_plugin_iris_memory.iris_memory.lifelike.memory import session_extractor as se
from astrbot_plugin_iris_memory.iris_memory.lifelike.memory import tombstones as tb
from astrbot_plugin_iris_memory.iris_memory.lifelike.memory.forget import ForgetService
from astrbot_plugin_iris_memory.iris_memory.lifelike.memory.service import LifelikeMemory
from astrbot_plugin_iris_memory.iris_memory.lifelike.social import l2_filter, privacy
from tests.lifelike.v3_util import written_tables

OWNER = "123456789"
SCOPE = f"private:{OWNER}"
BASE = datetime(2026, 11, 3, 20, 0, tzinfo=m.TZ_SHANGHAI).timestamp()
DIGITS = re.compile(r"\d")


class Clock:
    def __init__(self, t=BASE):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    store = ContinuityStore(tmp_path / "continuity.db", clock=clock)
    store.open()
    yield SimpleNamespace(store=store, clock=clock, beliefs=bl.BeliefStore(store, clock))
    store.close()


def owner_row(mid, text):
    return {"msg_id": mid, "_text": text, "sender_id": OWNER}


# ---------------------------------------------------------------- privacy


@pytest.mark.parametrize(
    "text,cat",
    [
        ("我最近确诊了抑郁", "health"),
        ("他住在幸福小区3号楼", "location"),
        ("手机号13812345678", "ident"),
        ("我上初中了", "minor"),
        ("这事别外传啊", "confidential"),
        ("月薪才五千", "finance"),
        ("我跟男朋友分手了", "private_life"),
        ("周末去教会做礼拜", "religion"),
    ],
)
def test_privacy_categories(text, cat):
    assert privacy.sensitive_category(text) == cat


def test_privacy_clean_and_helpers():
    assert privacy.sensitive_category("最近在玩塞尔达，好难") is None
    assert privacy.minor_signal("我今年15岁") and privacy.minor_signal("我还在上高中")
    assert not privacy.minor_signal("高三那年的事好久远了")
    assert privacy.blocked_topic("北京") and privacy.blocked_topic("医院") and not privacy.blocked_topic("塞尔达")
    assert privacy.forget_me_request("别记我的事了") and privacy.forget_me_request("忘了我说的吧")
    assert not privacy.forget_me_request("我记得你说过")


# ---------------------------------------------------------------- band 纯函数


def test_band_functions():
    assert bl.band_ceiling("stated") == "确定"
    assert bl.band_ceiling("observed") == "较确定"
    assert bl.band_ceiling("inferred") == "试探"
    assert bl.band_ceiling("owner_confirmed") == "确定"
    assert bl.initial_band("stated") == "较确定"
    assert bl.band_up("较确定", "确定") == "确定"
    assert bl.band_up("试探", "试探") == "试探"
    assert bl.band_up("确定", "较确定") == "较确定"
    assert bl.band_down("确定") == "较确定"
    assert bl.band_down("试探") is None
    assert bl.normalize_memory_mode("ON") == "on" and bl.normalize_memory_mode("x") == "off"


# ---------------------------------------------------------------- BeliefStore


def test_add_confirm_counter_supersede_expire(env):
    b = env.beliefs
    r = b.add(SCOPE, "preference", "他不吃香菜", "香菜", "stated", f"{SCOPE}:1")
    assert r["band"] == "较确定" and r["status"] == "active" and r["expires_at"] is not None
    # 同一条消息只计一次
    assert b.confirm(r["belief_id"], f"{SCOPE}:1")["band"] == "较确定"
    assert b.confirm(r["belief_id"], f"{SCOPE}:2")["band"] == "确定"
    # 同话题同说法 → confirm，不新增
    same = b.add(SCOPE, "preference", "他不吃香菜", "香菜", "stated", f"{SCOPE}:3")
    assert same["belief_id"] == r["belief_id"]
    # stated 新说法 → 旧条 superseded
    new = b.add(SCOPE, "preference", "他现在吃香菜了", "香菜", "stated", f"{SCOPE}:4")
    assert new["supersedes"] == r["belief_id"]
    assert b.get(r["belief_id"])["status"] == "superseded"
    # 推测：试探 + 需确认 + 14 天过期；反例 → disputed
    inf = b.add(SCOPE, "inference", "他约好的事一般不会临时取消", "守约", "inferred", f"{SCOPE}:5")
    assert inf["band"] == "试探" and inf["needs_confirmation"] == 1
    assert inf["expires_at"] == pytest.approx(BASE + 14 * 86400)
    assert b.counter(inf["belief_id"], f"{SCOPE}:6")["status"] == "disputed"
    # 观察：较确定 → 反例降到试探，仍 active
    hab = b.add(SCOPE, "habit", "他一般深夜才回消息", "深夜回消息", "observed", f"{SCOPE}:7")
    assert b.counter(hab["belief_id"], f"{SCOPE}:8")["band"] == "试探"
    # 过期
    env.clock.t += 15 * 86400
    assert b.expire_due() >= 0
    assert b.get(hab["belief_id"])["status"] == "active"  # habit 90 天
    env.clock.t += 200 * 86400
    b.expire_due()
    assert b.get(new["belief_id"])["status"] == "expired"
    # history 不含正文
    hist = b.history(r["belief_id"])
    assert hist and all("香菜" not in h["snapshot"] for h in hist)


def test_owner_confirm_and_retract(env):
    b = env.beliefs
    inf = b.add(SCOPE, "inference", "他周五晚上一般有空", "周五有空", "inferred", f"{SCOPE}:1")
    c = b.owner_confirm(inf["belief_id"])
    assert c["source"] == "owner_confirmed" and c["band"] == "确定" and c["kind"] == "fact"
    assert c["needs_confirmation"] == 0
    assert b.retract(inf["belief_id"])["status"] == "retracted"


def test_validate_belief_ops_sources_and_gates(env):
    rows = [owner_row(11, "我不吃香菜的"), owner_row(12, "我最近确诊了感冒")]
    raw = [
        {"op": "new", "kind": "preference", "statement": "他不吃香菜", "topic": "香菜", "evidence": "不吃香菜"},
        {"op": "new", "kind": "fact", "statement": "他确诊了感冒", "topic": "身体", "evidence": "确诊了感冒"},
        {"op": "new", "kind": "fact", "statement": "她说的话", "topic": "无关", "evidence": "Xiaobai 说过的话"},
        {"op": "confirm", "belief_id": "b_unknown", "evidence": "不吃香菜"},
        {"op": "new", "kind": "trait", "statement": "他很重视承诺", "topic": "承诺", "evidence": "香菜"},
        {"op": "new", "kind": "inference", "statement": "他可能挑食", "topic": "挑食", "evidence": "香菜"},
        {"op": "new", "kind": "fact", "statement": "超过上限", "topic": "x", "evidence": "香菜"},
    ]
    vr = bl.validate_belief_ops(raw, rows, [], scope_key=SCOPE)
    kinds = [(o.kind, o.source) for o in vr.ops]
    assert ("preference", "stated") in kinds and ("inference", "inferred") in kinds
    assert vr.sensitive == 1
    assert vr.invalid >= 3
    assert all(o.evidence_ref.startswith(SCOPE + ":") for o in vr.ops)
    # 墓碑：命中即跳过
    vr2 = bl.validate_belief_ops(raw[:1], rows, [], scope_key=SCOPE, tombstone_hit=lambda *t: True)
    assert vr2.ops == [] and vr2.tombstoned == 1
    assert bl.validate_belief_ops("bad", rows, [], scope_key=SCOPE).ops == []


def test_render_block_rules():
    now = BASE
    rows = [
        {"status": "active", "subject": "owner", "kind": "preference", "statement": "他不吃香菜", "band": "确定",
         "source": "stated", "expires_at": None, "needs_confirmation": 0},
        {"status": "active", "subject": "owner", "kind": "inference", "statement": "他这周可能挺忙", "band": "试探",
         "source": "inferred", "expires_at": now + 100, "needs_confirmation": 1},
        {"status": "active", "subject": "owner", "kind": "inference", "statement": "他晚上一般在线", "band": "较确定",
         "source": "observed", "expires_at": now + 100, "needs_confirmation": 1},
        {"status": "disputed", "subject": "owner", "kind": "fact", "statement": "他养猫", "band": "试探",
         "source": "stated", "expires_at": None, "needs_confirmation": 0},
        {"status": "active", "subject": "owner", "kind": "fact", "statement": "他过期的事", "band": "确定",
         "source": "stated", "expires_at": now - 1, "needs_confirmation": 0},
        {"status": "active", "subject": "owner", "kind": "boundary", "statement": "他不喜欢被追问", "band": "较确定",
         "source": "stated", "expires_at": None, "needs_confirmation": 0},
    ]
    block = bl.render_beliefs_block(rows, inference_mode="hedged", now=now)
    assert block.startswith("<你记得的她>") and block.endswith("</你记得的她>")
    assert "不吃香菜（她自己说的）" in block and "不喜欢被追问" in block
    assert "晚上一般在线" in block and "没确认过" in block
    assert "这周可能挺忙" not in block  # 试探的推测不注入（D8）
    assert "养猫" not in block and "过期" not in block
    assert not DIGITS.search(block)
    off = bl.render_beliefs_block(rows, inference_mode="off", now=now)
    assert "晚上一般在线" not in off
    assert bl.render_beliefs_block([], now=now) == ""


# ---------------------------------------------------------------- 会话抽取接入


class FakeLLM:
    def __init__(self, out):
        self.out = out
        self.prompts = []

    async def __call__(self, prompt, timeout):
        self.prompts.append(prompt)
        return json.dumps(self.out, ensure_ascii=False)


def _sx(env, llm, memory_mode, shadow_extract=None):
    cfg = {"lifelike_session_extract_mode": "shadow", "lifelike_memory_mode": memory_mode}
    items = ItemExtractor(env.store, FakeLLM({}), clock=env.clock)
    budget = CheapBudget(env.store, lambda k, d=None: cfg.get(k, d), env.clock)
    return se.SessionExtractor(
        env.store, items, llm, budget,
        mode_get=lambda: cfg["lifelike_session_extract_mode"], clock=env.clock, timeout_get=lambda: 10,
        beliefs=env.beliefs, memory_mode_get=lambda: cfg["lifelike_memory_mode"],
        memory_shadow_extract_get=(lambda: shadow_extract) if shadow_extract is not None else None,
    )


async def _seed(env, sx):
    rep = await sx.run_scope(SCOPE, "init")
    assert rep.status == "empty"
    for i, text in enumerate(["我跟你说我是真的不吃香菜，一点都不行", "下周想去看那个新番的剧场版"]):
        env.clock.t += 30
        env.store.record_inbound(SCOPE, 200 + i, OWNER, text, env.clock.t)
        env.store.advance_watermark(SCOPE, 200 + i, env.clock.t)
        env.store.mark_inbound(SCOPE, [200 + i], "skipped", "")


@pytest.mark.asyncio
async def test_session_extract_prompt_unchanged_when_memory_off(env):
    out = {"items": [], "beliefs": [
        {"op": "new", "kind": "preference", "statement": "他不吃香菜", "topic": "香菜", "evidence": "不吃香菜"}]}
    llm = FakeLLM(out)
    sx = _sx(env, llm, "off")
    await _seed(env, sx)
    rep = await sx.run_scope(SCOPE, "manual")
    assert rep.status == "shadow"
    assert se._PROMPT_HEAD in llm.prompts[0] and "<beliefs>" not in llm.prompts[0]
    assert '"beliefs":[]' in llm.prompts[0]
    assert not written_tables(env.store, ["user_beliefs", "belief_history"])  # v3 迁移已建表：off 不写行


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [None, False])
async def test_session_extract_prompt_unchanged_in_shadow_by_default(env, flag):
    """审查 #3：memory=shadow（默认）不改现网 P0-4 抽取 prompt，也不写 beliefs"""
    out = {"items": [], "beliefs": [
        {"op": "new", "kind": "preference", "statement": "她不吃香菜", "topic": "香菜", "evidence": "不吃香菜"}]}
    llm = FakeLLM(out)
    sx = _sx(env, llm, "shadow", flag)
    await _seed(env, sx)
    rep = await sx.run_scope(SCOPE, "manual")
    assert rep.status == "shadow"
    assert se._PROMPT_HEAD in llm.prompts[0] and "<beliefs>" not in llm.prompts[0]
    assert '"beliefs":[]' in llm.prompts[0]
    assert not written_tables(env.store, ["user_beliefs", "belief_history"])


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_mode,flag", [("shadow", True), ("on", None)])
async def test_session_extract_writes_beliefs_in_shadow(env, memory_mode, flag):
    out = {"items": [], "beliefs": [
        {"op": "new", "kind": "preference", "statement": "他不吃香菜", "topic": "香菜", "evidence": "不吃香菜"},
        {"op": "new", "kind": "fact", "statement": "Xiaobai 编的", "topic": "编造", "evidence": "不存在的原文"}]}
    llm = FakeLLM(out)
    sx = _sx(env, llm, memory_mode, flag)
    await _seed(env, sx)
    rep = await sx.run_scope(SCOPE, "manual")
    assert rep.status == "shadow"
    p = llm.prompts[0]
    assert "<beliefs>" in p and "beliefs 保持空数组" not in p and '"beliefs":[{' in p
    rows = env.beliefs.list(SCOPE)
    assert [r["statement"] for r in rows] == ["他不吃香菜"]
    assert env.store.get_counters().get("lifelike.session_extract.beliefs_invalid") == 1


# ---------------------------------------------------------------- forget / tombstones


class FakeL2:
    is_available = True

    def __init__(self):
        self.entries = [
            SimpleNamespace(id="l2a", content="他说过不吃香菜", metadata={"user_id": OWNER}),
            SimpleNamespace(id="l2b", content="别人不吃香菜", metadata={"user_id": "999"}),
            SimpleNamespace(id="l2c", content="他喜欢猫", metadata={"active_users": f"{OWNER},5"}),
        ]
        self.archive = [{"id": "arc1", "content": "香菜相关", "metadata": {}, "user_id": OWNER}]
        self.deleted = []
        self.arch_deleted = []

    async def get_all_entries(self, persona_id=None):
        return self.entries

    async def list_archived_memories(self, limit=50, offset=0, persona_id=None):
        return self.archive

    async def delete_entries(self, ids):
        self.deleted.extend(ids)
        return True

    async def delete_archived_memory(self, mid):
        self.arch_deleted.append(mid)
        return True


class FakeLearning:
    def __init__(self):
        self.deleted = []

    def find_few_shot_ids(self, keyword, user_id):
        return [7, 8] if keyword == "香菜" and user_id == OWNER else []

    def delete_rows(self, table, ids):
        assert table == "few_shot"
        self.deleted.extend(ids)
        return len(ids)


def _item(env, topic):
    return env.store.create_item(
        subject_id=OWNER, subject_type="user", scope_key=SCOPE, topic=topic, summary=topic,
        kind=m.KIND_UNFINISHED_TOPIC, source_kind=m.SOURCE_SHARED_DIALOGUE,
        source_refs=[{"msg_id": 1}], allowed_audiences=[SCOPE], reason="t",
    )


@pytest.mark.asyncio
async def test_forget_preview_purge_and_tombstone(env):
    b = env.beliefs
    keep = b.add(SCOPE, "preference", "他喜欢猫", "猫", "stated", f"{SCOPE}:1")
    gone = b.add(SCOPE, "preference", "他不吃香菜", "香菜", "stated", f"{SCOPE}:2")
    it = _item(env, "香菜火锅的事")
    other = _item(env, "面试结果")
    l2, learn = FakeL2(), FakeLearning()
    fs = ForgetService(env.store, b, l2_get=lambda: l2, learning_get=lambda: learn, clock=env.clock)
    assert await fs.preview(OWNER, "香") is None  # 太短
    pv = await fs.preview(OWNER, "香菜")
    assert pv.counts() == {"beliefs": 1, "items": 1, "l2": 2, "learning": 2}
    assert await fs.purge(pv.token, "someone_else") is None
    out = await fs.purge(pv.token, OWNER)
    assert out == {"beliefs": 1, "items": 1, "l2": 2, "learning": 2}
    assert await fs.purge(pv.token, OWNER) is None  # 确认码一次性
    g = b.get(gone["belief_id"])
    assert g["status"] == "deleted" and g["statement"] == "" and g["topic_key"] == ""
    assert b.get(keep["belief_id"])["status"] == "active"
    row = env.store.get_item(it["item_id"])
    assert row["topic"] == "" and row["summary"] == "" and row["status"] == m.STATUS_CANCELLED
    assert all("香菜" not in json.dumps(h["snapshot"], ensure_ascii=False) for h in env.store.item_history(it["item_id"]))
    assert env.store.get_item(other["item_id"])["topic"] == "面试结果"
    assert l2.deleted == ["l2a"] and l2.arch_deleted == ["arc1"] and learn.deleted == [7, 8]
    # 各层再检索为空
    assert b.search("香菜", SCOPE) == []
    pv2 = await fs.preview(OWNER, "香菜")
    assert pv2.counts()["beliefs"] == 0 and pv2.counts()["items"] == 0
    # 墓碑：表里只有哈希；之后抽取遇到同话题跳过
    rows = tb.read_rows(env.store, "SELECT * FROM tombstones")
    assert rows and all("香菜" not in r["key_hash"] for r in rows)
    assert tb.text_hits_topic_tombstone(env.store, "周末吃香菜火锅")
    assert not tb.text_hits_topic_tombstone(env.store, "周末吃火锅")
    vr = bl.validate_belief_ops(
        [{"op": "new", "kind": "preference", "statement": "他不吃香菜", "topic": "香菜", "evidence": "香菜"}],
        [owner_row(5, "香菜真难吃")], [], scope_key=SCOPE,
        tombstone_hit=lambda *t: tb.text_hits_topic_tombstone(env.store, *t),
    )
    assert vr.tombstoned == 1
    ex = ItemExtractor(env.store, FakeLLM({}), clock=env.clock)
    msg = {"msg_id": 9, "msg_ts": env.clock.t, "_text": "下次去吃香菜火锅", "sender_id": OWNER}
    op = _Op(op="create", item_id=None, kind=m.KIND_PLAN, topic="去吃香菜火锅", summary="", when_text="",
             remind_requested=None, evidence="香菜火锅", msg=msg, when_msg=None)
    assert ex._create(SCOPE, OWNER, op, msg["_text"], env.clock.t) is None
    op2 = _Op(op="create", item_id=None, kind=m.KIND_PLAN, topic="去吃火锅", summary="", when_text="",
              remind_requested=None, evidence="火锅", msg=msg, when_msg=None)
    assert ex._create(SCOPE, OWNER, op2, msg["_text"], env.clock.t) is not None


def test_tombstone_absent_table_is_noop(env):
    assert not written_tables(env.store, ["tombstones"])  # v3 迁移已建表：空表同样是 no-op
    assert tb.text_hits_topic_tombstone(env.store, "任何话题") is False
    assert tb.has_tombstone(env.store, tb.member_hash("1")) is False


@pytest.mark.asyncio
async def test_service_commands_and_injection(env):
    cfg = {"lifelike_memory_mode": "shadow", "lifelike_member_mode": "off"}
    lm = LifelikeMemory(
        env.store, config_get=lambda k, d=None: cfg.get(k, d), owner_ids=lambda: [OWNER],
        observe_chat_ids=lambda: (), clock=env.clock,
    )
    r = lm.beliefs.add(SCOPE, "preference", "他不吃香菜", "香菜", "stated", f"{SCOPE}:1")
    assert await lm.inject_blocks(SCOPE, OWNER, "晚饭吃什么") == []  # shadow 不注入
    cfg["lifelike_memory_mode"] = "on"
    blocks = await lm.inject_blocks(SCOPE, OWNER, "晚饭吃什么")
    assert len(blocks) == 1 and "不吃香菜" in blocks[0] and not DIGITS.search(blocks[0])
    assert await lm.inject_blocks("private:999", "999", "x") == []
    assert await lm.inject_blocks("-100123", OWNER, "x") == []
    listing = await lm.beliefs_command(OWNER, ["list"])
    assert r["belief_id"] in listing
    assert "没找到" in await lm.beliefs_command(OWNER, ["confirm", "b_nope"])
    assert "确定" in await lm.beliefs_command(OWNER, ["confirm", r["belief_id"]])
    pv_text = await lm.forget_command(OWNER, ["香菜"])
    token = re.search(r"forget confirm (\w+)", pv_text).group(1)
    done = await lm.forget_command(OWNER, ["confirm", token])
    assert "不提了" in done
    assert await lm.inject_blocks(SCOPE, OWNER, "x") == []
    cfg["lifelike_memory_mode"] = "off"
    await lm.maintenance()  # off：不扫描、不报错


# ---------------------------------------------------------------- R1 私聊 L2


def _res(content, **meta):
    return SimpleNamespace(entry=SimpleNamespace(content=content, metadata=meta))


def test_private_l2_filter_rules():
    results = [
        _res("私聊记忆", group_id=""),
        _res("授权群的事", group_id="-1001000000001"),
        _res("授权群敏感：他住院了", group_id="-1001000000001"),
        _res("未授权群的事", group_id="-1001000000003"),
        _res("全局知识", group_id="-1001000000003", scope="global"),
        _res("全局敏感：手机号13812345678", group_id="", scope="global"),
    ]
    kept, stats = l2_filter.filter_private_l2(results, ["-1001000000001"])
    assert [r.entry.content for r in kept] == ["私聊记忆", "授权群的事", "全局知识"]
    assert stats == {"unauthorized": 1, "sensitive": 2, "kept": 3}
    # 群 id 写法等价
    kept2, _ = l2_filter.filter_private_l2([_res("x", group_id="1000000001")], ["-1001000000001"])
    assert len(kept2) == 1
    assert l2_filter.parse_chat_ids("-1, -2，-1") == ["-1", "-2"]
    assert l2_filter.parse_chat_ids(None) == []


def test_private_l2_scrub_switch():
    from astrbot_plugin_iris_memory.iris_memory.core.llm_request_hook import _private_l2_scrub

    results = [_res("未授权群的事", group_id="-1001000000003"), _res("私聊", group_id="")]
    cfg_on = Mock()
    cfg_on.get = Mock(side_effect=lambda k, d=None: {"lifelike_member_groups": "-1001000000001"}.get(k, d))
    meta = {}
    assert [r.entry.content for r in _private_l2_scrub(results, cfg_on, meta)] == ["私聊"]
    assert meta["private_filtered"] == 1
    cfg_off = Mock()
    cfg_off.get = Mock(side_effect=lambda k, d=None: False if k == "lifelike_private_l2_filter" else d)
    assert _private_l2_scrub(results, cfg_off, None) is results
    broken = Mock()
    broken.get = Mock(side_effect=RuntimeError("boom"))
    assert _private_l2_scrub(results, broken, None) is results


# ---------------------------------------------------------------- dream 冲突消解归档


def _dream_l2(archived):
    l2 = Mock()
    l2.update_content = AsyncMock(return_value=True)
    l2.update_metadata = AsyncMock()
    l2.delete_entries = AsyncMock()
    l2.archive_superseded = AsyncMock(return_value=archived)
    return l2


def _entries():
    from astrbot_plugin_iris_memory.iris_memory.l2_memory.models import MemoryEntry

    return [MemoryEntry(id="m1", content="旧事实"), MemoryEntry(id="m2", content="新事实")]


@pytest.mark.asyncio
async def test_dream_contradiction_archives_instead_of_delete(monkeypatch):
    from astrbot_plugin_iris_memory.iris_memory.dream import contradiction as cmod

    phase = cmod.ContradictionPhase()
    cfg = Mock()
    cfg.get = Mock(side_effect=lambda k, d=None: d)
    monkeypatch.setattr(cmod, "get_config", lambda: cfg)
    l2 = _dream_l2(1)
    assert await phase._apply_resolution(_entries(), 1, "合并", l2) is True
    l2.archive_superseded.assert_awaited_once_with(["m1"], "m2")
    l2.delete_entries.assert_not_awaited()
    # 归档失败 → 回退旧行为
    l2 = _dream_l2(0)
    assert await phase._apply_resolution(_entries(), 1, "合并", l2) is True
    l2.delete_entries.assert_awaited_once_with(["m1"])
    # 开关关闭 → 旧行为硬删
    cfg.get = Mock(side_effect=lambda k, d=None: False if k == "lifelike_dream_contradiction_archive" else d)
    l2 = _dream_l2(1)
    await phase._apply_resolution(_entries(), 1, "合并", l2)
    l2.archive_superseded.assert_not_awaited()
    l2.delete_entries.assert_awaited_once_with(["m1"])
