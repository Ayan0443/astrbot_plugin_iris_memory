"""P1 对抗审查（lifelike/p1 @ 5ac02c2）修复的回归测试。

每个用例注释里标了审查条目编号：正确性 #n / 隐私 Pn / 体验 Mn、Ln。
"""

from __future__ import annotations

import asyncio
import json
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_iris_memory.iris_memory.continuity import models as m
from astrbot_plugin_iris_memory.iris_memory.lifelike.care import bridge as care_bridge
from astrbot_plugin_iris_memory.iris_memory.lifelike.initiative import coordinator as coord_mod
from astrbot_plugin_iris_memory.iris_memory.lifelike.initiative import gate as g
from astrbot_plugin_iris_memory.iris_memory.lifelike.initiative.coordinator import InitiativeCoordinator
from astrbot_plugin_iris_memory.iris_memory.lifelike.social import l2_filter
from astrbot_plugin_iris_memory.iris_memory.lifelike.style import guard as style_guard
from astrbot_plugin_iris_memory.iris_memory.style import examples as EX
from astrbot_plugin_iris_memory.iris_memory.style import register as R
from astrbot_plugin_iris_memory.iris_memory.style.selector import StyleSelector, StyleSettings

from .test_care_bridge import Env as CareEnv
from .test_initiative_integration import OWNER, SCOPE, T0, _care_cand, _make


# ---------------------------------------------------------------- #1 / #11 候选结算不依赖进程内存


@pytest.mark.asyncio
async def test_turn_end_after_restart_settles_candidate(tmp_path):
    """#1：回合开始后进程重启（_turns 清空），对账路径结束回合仍要结算候选并记 sent_at"""
    svc = await _make(tmp_path)
    try:
        cid = _care_cand(svc)
        start = await svc.bridge.begin_turn(SCOPE)
        tid = start.turn["turn_id"]
        await svc.bridge.mark_turn(tid, "generating", item_id=f"cand:{cid}", llm_calls=1)
        assert svc.initiative.pool.get(cid)["status"] == "offered"
        # 模拟重启：换一个全新的协调器（内存里没有这个回合）
        fresh = InitiativeCoordinator(
            svc.store, svc._config_get, owner_private=svc.is_owner_scope, clock=svc.test_clock
        )
        fresh.care = svc.care
        await fresh.note_turn_end(tid, m.TURN_UNCERTAIN, "stale_restart")
        assert svc.initiative.pool.get(cid)["status"] == "used"
        dec = fresh.pool.decision_for_turn(tid)
        assert dec is not None and dec["sent_at"] is not None and dec["chosen_ctype"] == "group_care"
        # 已经记过决策：再来一次不重复记
        await fresh.note_turn_end(tid, m.TURN_UNCERTAIN, "stale_restart")
        assert len([d for d in fresh.pool.recent_decisions(SCOPE, 10) if d["turn_id"] == tid]) == 1
        # 下一轮闸门把这次推送算进 24h 计数
        base = fresh._gate_base(SCOPE, svc.test_clock.t + 60)
        assert base["push_24h"] == 1
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_recover_orphans_on_first_offer(tmp_path):
    """#1：候选停在 offered、回合已终结（崩溃遗留）→ 首次建 offer 时按回合终态结算"""
    svc = await _make(tmp_path)
    try:
        sent_cid = _care_cand(svc, key="care:a")
        failed_cid = _care_cand(svc, key="care:b", material="说加班到很晚")
        pool = svc.initiative.pool
        for cid, state in ((sent_cid, m.TURN_SENT), (failed_cid, m.TURN_CANCELLED)):
            turn = svc.store.create_turn(SCOPE, input_msg_id=0, session_version=0, forced=False)
            svc.store.update_turn(turn["turn_id"], state="generating", item_id=f"cand:{cid}")
            pool.set_status(cid, "offered", only_from=("queued",), offered_at=T0)
            svc.store.update_turn(turn["turn_id"], state=state)
        fresh = InitiativeCoordinator(
            svc.store, svc._config_get, owner_private=svc.is_owner_scope, clock=svc.test_clock
        )
        fresh.care = svc.care
        await fresh.merge_candidates(SCOPE, T0 + 60, [])
        assert pool.get(sent_cid)["status"] == "used"
        assert pool.get(failed_cid)["status"] == "queued"
    finally:
        await svc.stop()


def test_turns_registry_pruned():
    """#11：进程内回合登记有 TTL，不会无限增长"""
    clock = SimpleNamespace(t=T0)
    c = InitiativeCoordinator(
        Mock(), lambda k, d=None: d, owner_private=lambda s: True, clock=lambda: clock.t
    )
    c.note_turn_begin(SCOPE, "t1")
    assert "t1" in c._turns
    clock.t += coord_mod.TURN_TTL_S + 1
    c.note_turn_begin(SCOPE, "t2")
    assert "t1" not in c._turns and "t2" in c._turns


# ---------------------------------------------------------------- #4 关心桥 shadow 不写偏好、不注入


@pytest.mark.asyncio
async def test_care_shadow_opt_out_not_applied(tmp_path):
    svc = await _make(tmp_path, lifelike_care_bridge_mode="shadow", lifelike_initiative_mode="off")
    try:
        await svc.note_owner_message(SCOPE, 11, T0 + 5, text="别根据群里的消息关心我了", sender_id=OWNER)
        assert not svc.care.is_off()
        assert SCOPE not in svc.initiative._hints
        text = await svc.assemble(SCOPE, purpose="reply", sender_id=OWNER)
        assert "可以顺口提的" not in text
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_passive_hint_needs_live_module(tmp_path):
    """#4：残留的提示在 initiative/care 都不是 on 时不注入"""
    svc = await _make(tmp_path, lifelike_care_bridge_mode="shadow", lifelike_initiative_mode="shadow")
    try:
        svc.initiative._hints[SCOPE] = ("提示", T0 + 600)
        assert await svc.initiative.passive_section(SCOPE, True) is None
    finally:
        await svc.stop()


# ---------------------------------------------------------------- M4 顺口提只注入一次


@pytest.mark.asyncio
async def test_passive_care_line_injected_once(tmp_path):
    svc = await _make(tmp_path)
    try:
        cid = _care_cand(svc, delivery=g.DELIVERY_PASSIVE)
        first = await svc.assemble(SCOPE, purpose="reply", sender_id=OWNER)
        assert "可以顺口提的" in first
        assert svc.initiative.pool.get(cid)["status"] == "used"
        svc.test_clock.t += 60
        second = await svc.assemble(SCOPE, purpose="reply", sender_id=OWNER)
        assert "可以顺口提的" not in second
    finally:
        await svc.stop()


# ---------------------------------------------------------------- M5 误判收场按类别


@pytest.mark.asyncio
async def test_sick_reply_no_problem_is_not_misread(tmp_path):
    svc = await _make(tmp_path)
    try:
        cid = _care_cand(svc)
        svc.initiative.pool.record_decision(
            ts=T0, scope_key=SCOPE, turn_id="t", mode="on", decision="speak",
            chosen_cand=f"cand:{cid}", chosen_ctype="group_care", sent_at=T0,
        )
        await svc.note_owner_message(SCOPE, 21, T0 + 60, text="没事的，吃了药好多了", sender_id=OWNER)
        dec = svc.initiative.pool.decision_for_turn("t")
        assert dec["replied"] == 1 and dec["reply_tone"] in (None, "")
        assert SCOPE not in svc.initiative._hints
    finally:
        await svc.stop()


# ---------------------------------------------------------------- #8 空窗检查缓存 / P9 独立开关


@pytest.mark.asyncio
async def test_has_push_candidate_cached(tmp_path):
    svc = await _make(tmp_path)
    try:
        _care_cand(svc)
        calls = []
        orig = svc.initiative.merge_candidates

        async def spy(*a, **k):
            calls.append(1)
            return await orig(*a, **k)

        svc.initiative.merge_candidates = spy
        assert await svc.bridge.has_push_candidate(SCOPE)
        assert await svc.bridge.has_push_candidate(SCOPE)
        assert len(calls) == 1
        svc.test_clock.t += 301
        assert await svc.bridge.has_push_candidate(SCOPE)
        assert len(calls) == 2
    finally:
        await svc.stop()


def test_pool_empty_window_switch():
    from astrbot_plugin_iris_memory.iris_memory.proactive.proactive import ProactiveEngine

    e = object.__new__(ProactiveEngine)
    e._config = SimpleNamespace(pool_empty_window=False, initiative_on=True)
    assert not e._pool_empty_window_on()
    e._config = SimpleNamespace(pool_empty_window=True)
    assert e._pool_empty_window_on()
    e._config = SimpleNamespace()
    assert not e._pool_empty_window_on()


def test_pool_empty_window_config_default_and_off():
    from astrbot_plugin_iris_memory.iris_memory.proactive.config import ConfigManager

    cm = object.__new__(ConfigManager)
    cm._hidden_get = lambda k: None
    assert cm.pool_empty_window is True
    cm._hidden_get = lambda k: False if k == "lifelike_pool_empty_window" else None
    assert cm.pool_empty_window is False


# ---------------------------------------------------------------- #6 MAN-08 主动只记日志


def test_proactive_man08_log_only():
    hard, soft = style_guard.proactive_man_hits("你之前答应我今天早点睡的，还记得吗")
    assert hard == [] and soft == ["MAN-08"]
    hard, _ = style_guard.proactive_man_hits("你都不理我")
    assert hard == ["MAN-01"]
    hard, _ = style_guard.proactive_man_hits("昨晚被你吵醒了")
    assert hard == ["MAN-01"]


# ---------------------------------------------------------------- M2 关心消息口吻 / M8 示例冷却


def _settings():
    return StyleSettings(
        register_cap=R.REG_MEDIUM, group_cap=R.REG_LIGHT, cute_cap=R.CUTE_OCCASIONAL, inject_proactive=True,
    )


def test_care_proactive_style_light_no_examples():
    sel = StyleSelector(rng=random.Random(1), token_counter=len)
    text = sel.build_proactive(SCOPE, is_group=False, settings=_settings(), care_category="sick")
    assert "口吻：淡" in text and "这一轮不卖萌" in text
    assert "参考" not in text and "别这样" not in text
    plain = sel.build_proactive(SCOPE, is_group=False, settings=_settings())
    assert "口吻：中" in plain


def test_selector_reuses_oldest_when_cooled_and_no_lonely_anti():
    sel = StyleSelector(rng=random.Random(3), token_counter=len)
    lib = EX.load_examples("")
    for _ in range(6):
        plan = sel.plan("private:x", "今天下雨了", is_group=False, settings=_settings(), library=lib)
        assert plan is not None
        assert plan.examples or plan.anti is None  # 没有正例时不单独给反例
    chat = [ex for ex in lib if ex.scene == plan.scene and "private" in ex.channels]
    if chat:
        assert plan.examples  # 冷却耗尽后重用最早的一条


def test_cute_line_specific_only_for_praised_and_joke():
    sel = StyleSelector(rng=random.Random(5), token_counter=len)
    chat = sel.render(
        sel.plan("private:y", "今天下雨了", is_group=False, settings=_settings(), library=())
    )
    assert "假装委屈" not in chat and "小小得意" not in chat


# ---------------------------------------------------------------- #7 关心桥 rowid 游标


def test_care_cursor_rowid_picks_up_late_posts(tmp_path):
    env = CareEnv(tmp_path)
    try:
        env.clock.t = T0 + 60
        env.post("今天天气不错", at=T0)
        env.run()
        cur = env.care.cs.scan_cursor()
        assert cur is not None and "rowid" in cur
        # 适配器恢复后补采进来的、posted_at 早于上次扫描的主人发言也能扫到
        env.clock.t = T0 + 600
        env.post("我发烧了好难受", at=T0 - 300)
        env.run()
        assert len(env.signals()) == 1
    finally:
        env.store.close()


def test_care_cursor_legacy_timestamp_migrates(tmp_path):
    env = CareEnv(tmp_path)
    try:
        env.store.set_cursor("care_bridge", repr(T0 - 100))
        env.clock.t = T0 + 60
        env.post("旧帖", at=T0 - 200)
        env.post("我发烧了好难受", at=T0)
        env.run()
        assert len(env.signals()) == 1
        assert "rowid" in env.care.cs.scan_cursor()
    finally:
        env.store.close()


# ---------------------------------------------------------------- P8 概括里的人名 / 敏感


def test_llm_gist_with_names_falls_back_to_template(tmp_path):
    env = CareEnv(tmp_path)
    try:
        env.clock.t = T0 + 60
        env.post("今天好烦啊", at=T0)  # 弱关键词：模糊，交给模型
        env.llm_reply = json.dumps(
            [{"i": 0, "about_self": True, "category": "mood_low", "intensity": "轻", "is_joke": False,
              "crisis": False, "gist": "说和小明吵架了"}],
            ensure_ascii=False,
        )
        env.run()
        assert env.llm_calls  # 确实走了模型
        sigs = env.signals()
        assert len(sigs) == 1 and sigs[0]["detector"] == "llm"
        assert "小明" not in (sigs[0]["gist"] or "") and "吵架" not in (sigs[0]["gist"] or "")
        assert env.store.get_counters().get("lifelike.care.gist_rejected") == 1
    finally:
        env.store.close()


# ---------------------------------------------------------------- M3 时间措辞


def test_when_phrase():
    now = T0 + 12 * 3600  # 21:00
    assert care_bridge.when_phrase(now - 600, now) == "刚才"
    assert care_bridge.when_phrase(T0, now) == "今天早些时候"
    assert care_bridge.when_phrase(T0 - 86400 + 14 * 3600, T0) == "昨晚"  # 前一天 23:00 → 今天 09:00
    assert care_bridge.when_phrase(T0 - 86400, T0) == "昨天"
    assert care_bridge.when_phrase(T0 - 3 * 86400, now) == "前两天"


@pytest.mark.asyncio
async def test_focus_text_uses_relative_time(tmp_path):
    svc = await _make(tmp_path)
    try:
        cid = _care_cand(svc)
        cand = svc.initiative.pool.get(cid)
        late = svc.care.focus_text(cand, now=float(cand["created_at"]) + 26 * 3600)
        assert "刚在群里" not in late
        fresh = svc.care.focus_text(cand, now=float(cand["created_at"]) + 60)
        assert "刚在群里" in fresh
    finally:
        await svc.stop()


# ---------------------------------------------------------------- P1 R1 覆盖 L3 与 search_memory


def test_filter_private_graph():
    nodes = [
        {"id": "a", "name": "aming", "group_id": "", "properties": {}},
        {"id": "b", "name": "小明", "group_id": "-1001000000003", "properties": {}},
        {"id": "c", "name": "猫", "group_id": "-1001000000001", "properties": json.dumps({"group_ids": "-1001000000001"})},
        {"id": "d", "name": "混合", "group_id": "", "properties": {"group_ids": "-1001000000001,-1001000000003"}},
    ]
    edges = [
        {"source": "a", "target": "b", "relation_type": "朋友"},
        {"source": "a", "target": "c", "relation_type": "养"},
        {"source": {"id": "a"}, "target": {"id": "d"}, "relation": "x"},
    ]
    kept, kept_edges, stats = l2_filter.filter_private_graph(nodes, edges, ["-1001000000001"])
    assert [n["id"] for n in kept] == ["a", "c"]
    assert kept_edges == [edges[1]]
    assert stats["unauthorized"] == 2


def _cfg(values):
    c = Mock()
    c.get = Mock(side_effect=lambda k, d=None: values.get(k, d))
    return c


def test_private_graph_scrub_switch():
    from astrbot_plugin_iris_memory.iris_memory.core.llm_request_hook import _private_graph_scrub

    nodes = [{"id": "b", "name": "小明", "group_id": "-1001000000003"}]
    on = _cfg({"lifelike_member_groups": "-1001000000001"})
    assert _private_graph_scrub(nodes, [], on, {}) == ([], [])
    off = _cfg({"lifelike_private_l2_filter": False})
    assert _private_graph_scrub(nodes, [], off, None) == (nodes, [])


@pytest.mark.asyncio
async def test_l3_private_drops_unauthorized_group_nodes(monkeypatch):
    """P1：私聊时 L2 被 R1 剔空 → 关键词兜底拿到的未授权群节点也不能注入"""
    from astrbot_plugin_iris_memory.iris_memory.core import llm_request_hook as hook

    cfg = _cfg({
        "l3_kg.enable": True,
        "lifelike_member_groups": "-1001000000001",
        "l3_max_inject_tokens": 600,
    })
    monkeypatch.setattr("astrbot_plugin_iris_memory.iris_memory.config.get_config", lambda: cfg)
    adapter = Mock(get_group_id=Mock(return_value=None))
    monkeypatch.setattr("astrbot_plugin_iris_memory.iris_memory.platform.get_adapter", lambda ev: adapter)
    monkeypatch.setattr(
        "astrbot_plugin_iris_memory.iris_memory.core.persona.resolve_persona", AsyncMock(return_value="default")
    )

    class FakeRetriever:
        def __init__(self, kg):
            pass

        async def retrieve_by_keywords(self, keywords, group_id=None, persona_id=None):
            return [{"id": "b", "name": "小明", "group_id": "-1001000000003"}], []

        async def retrieve_with_expansion(self, **kw):
            return [], []

        def format_for_context(self, nodes, edges, max_tokens=600):
            return ("|".join(n["name"] for n in nodes), [n["id"] for n in nodes])

        async def update_access_count(self, ids):
            return None

    monkeypatch.setattr("astrbot_plugin_iris_memory.iris_memory.l3_kg.GraphRetriever", FakeRetriever)
    cm = Mock(check_component=Mock(return_value="available"), get_available_component=Mock(return_value=object()))
    meta: dict = {}
    text = await hook._collect_l3_knowledge_graph(Mock(), cm, [], user_message="「小明」最近怎么样", meta=meta)
    assert "小明" not in text
    # 群聊路径不受影响
    adapter.get_group_id = Mock(return_value="-1001000000003")
    text2 = await hook._collect_l3_knowledge_graph(Mock(), cm, [], user_message="「小明」最近怎么样", meta={})
    assert "小明" in text2


@pytest.mark.asyncio
async def test_search_memory_tool_private_filtered(monkeypatch):
    from astrbot_plugin_iris_memory.iris_memory.l2_memory import MemoryEntry, MemorySearchResult
    from astrbot_plugin_iris_memory.iris_memory.tools import SearchMemoryTool

    def res(mid, content, **meta):
        return MemorySearchResult(entry=MemoryEntry(id=mid, content=content, metadata=meta), score=0.9, distance=0.1)

    results = [res("m1", "未授权群里小明的事", group_id="-1001000000003"), res("m2", "主人私聊说过喜欢猫")]
    adapter = Mock(get_user_id=Mock(return_value=OWNER), get_group_id=Mock(return_value=None))
    cfg = _cfg({"lifelike_member_groups": "-1001000000001"})
    l2 = Mock(_is_available=True, retrieve=AsyncMock(return_value=results))
    monkeypatch.setattr("astrbot_plugin_iris_memory.iris_memory.platform.get_adapter", lambda ev: adapter)
    monkeypatch.setattr("astrbot_plugin_iris_memory.iris_memory.config.get_config", lambda: cfg)
    monkeypatch.setattr(
        "astrbot_plugin_iris_memory.iris_memory.tools.search_memory.get_component_manager",
        lambda: Mock(get_component=Mock(return_value=l2)),
    )
    monkeypatch.setattr(
        "astrbot_plugin_iris_memory.iris_memory.core.persona.resolve_persona", AsyncMock(return_value="default")
    )
    ctx = Mock()
    out = await SearchMemoryTool().call(ctx, query="小明")
    assert "小明" not in out and "喜欢猫" in out and "m1" not in out
    # 群聊（隔离开启）不做 R1 过滤
    adapter.get_group_id = Mock(return_value="-1001000000003")
    out2 = await SearchMemoryTool().call(ctx, query="小明")
    assert "小明" in out2


# ---------------------------------------------------------------- #9 自述核对的模块日上限串行化


@pytest.mark.asyncio
async def test_claim_auditor_serializes_cap_check():
    from astrbot_plugin_iris_memory.iris_memory.lifelike.world import auditor as aud

    counters = {"n": 0}

    class Budget:
        async def guarded_generate(self, module, prompt, llm, timeout, **kw):
            await asyncio.sleep(0.01)
            counters["n"] += 1
            return "[]"

    a = aud.ClaimAuditor(
        Mock(), AsyncMock(), Budget(),
        counters_get=lambda: {f"lifelike.budget.calls:{aud.MODULE_CLAIM}": counters["n"] + aud.MODULE_CLAIM_DAILY - 1},
        bump=lambda name, n=1: None,
    )
    a._count = lambda *x: None
    await asyncio.gather(*(a._classify("我刚到家") for _ in range(5)))
    assert counters["n"] == 1
