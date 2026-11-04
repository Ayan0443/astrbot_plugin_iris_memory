"""StyleAuditor（log 模式）与 main.py 接线测试：只记录、不改写；off 零副作用；异常回退。"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from astrbot_plugin_iris_memory.iris_memory.continuity.store import ContinuityStore
from astrbot_plugin_iris_memory.iris_memory.lifelike.style.audit import (
    CHANNEL_PASSIVE,
    CHANNEL_PROACTIVE,
    StyleAuditor,
    normalize_mode,
)


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def store(tmp_path):
    s = ContinuityStore(tmp_path / "continuity.db", clock=Clock())
    s.open()
    yield s
    s.close()


def _auditor(store, mode="log", clock=None):
    box = {"mode": mode}
    a = StyleAuditor(lambda: box["mode"], lambda: store, clock=clock or Clock())
    return a, box


def test_normalize_mode():
    assert normalize_mode("log") == "log"
    assert normalize_mode(" LOG ") == "log"
    assert normalize_mode("off") == "off"
    assert normalize_mode("") == "off"
    assert normalize_mode("weird") == "off"
    assert normalize_mode("fix") == "log"  # P0 只开放到 log
    assert normalize_mode("regen") == "log"


@pytest.mark.asyncio
async def test_log_mode_records_rule_ids_without_text(store):
    a, _ = _auditor(store)
    secret = "有什么需要随时告诉我，亲爱的"
    tags = await a.audit("private:1", CHANNEL_PASSIVE, secret, user_text="今天好累")
    assert "AIW-08" in tags and "BL-PET-NAME" in tags
    rows = store.list_style_audit()
    assert len(rows) == 1
    assert rows[0]["channel"] == "passive" and rows[0]["action"] == "log"
    assert rows[0]["rule_ids"] == tags
    # 库里任何一列都不含原文
    raw = store._conn().execute("SELECT * FROM style_audit").fetchall()
    dumped = json.dumps([tuple(r) for r in raw], ensure_ascii=False)
    assert "随时告诉我" not in dumped and "今天好累" not in dumped
    assert store.get_counters()["style.scanned.passive"] == 1


@pytest.mark.asyncio
async def test_clean_reply_counts_scan_but_writes_no_row(store):
    a, _ = _auditor(store)
    assert await a.audit("private:1", CHANNEL_PROACTIVE, "好，到时候我问你。") == []
    assert store.list_style_audit() == []
    assert store.get_counters()["style.scanned.proactive"] == 1


@pytest.mark.asyncio
async def test_off_mode_has_no_side_effects(store):
    a, _ = _auditor(store, mode="off")
    assert await a.audit("private:1", CHANNEL_PASSIVE, "有什么需要随时告诉我") is None
    a.schedule("private:1", CHANNEL_PASSIVE, "有什么需要随时告诉我")
    await a.drain()
    assert store.list_style_audit() == []
    assert store.get_counters() == {}
    assert a._scopes == {}  # 不维护内存标记


@pytest.mark.asyncio
async def test_history_markers_enable_repeat_rules(store):
    a, _ = _auditor(store)
    for _ in range(2):
        await a.audit("private:1", CHANNEL_PASSIVE, "哈哈哈哈今天也很开心呢")
    tags = await a.audit("private:1", CHANNEL_PASSIVE, "哈哈哈哈今天也很开心呢")
    assert "AIW-07" in tags
    # 另一个会话互不影响
    assert "AIW-07" not in (await a.audit("g2", CHANNEL_PASSIVE, "哈哈哈哈今天也很开心呢"))


@pytest.mark.asyncio
async def test_work_mention_exempts_soft_hints_in_recent_turns(store):
    a, _ = _auditor(store)
    await a.audit("private:1", CHANNEL_PASSIVE, "看过。", user_text="你看过星海物语吗")
    tags = await a.audit("private:1", CHANNEL_PASSIVE, "那个星光怀表的情节挺好的。", user_text="嗯嗯")
    assert "COPY-S01@mention" in tags
    tags2 = await a.audit("g9", CHANNEL_PASSIVE, "那个星光怀表还在吗。", user_text="嗯嗯")
    assert "COPY-S01" in tags2


@pytest.mark.asyncio
async def test_schedule_runs_in_background(store):
    a, _ = _auditor(store)
    a.schedule("private:1", CHANNEL_PASSIVE, "希望对你有帮助")
    await a.drain()
    assert [r["rule_ids"] for r in store.list_style_audit()] == [["AIW-08"]]


@pytest.mark.asyncio
async def test_no_store_is_silent_noop():
    a = StyleAuditor(lambda: "log", lambda: None)
    assert "AIW-08" in (await a.audit("s", CHANNEL_PASSIVE, "希望对你有帮助"))


@pytest.mark.asyncio
async def test_write_failures_trip_breaker_and_log_no_text(caplog):
    clock = Clock()

    class BadStore:
        def record_style_audit(self, *a, **k):
            raise RuntimeError("boom 希望对你有帮助")

    a = StyleAuditor(lambda: "log", lambda: BadStore(), clock=clock)
    caplog.set_level(logging.INFO)
    for _ in range(5):
        await a.audit("s", CHANNEL_PASSIVE, "希望对你有帮助")
    assert a.paused
    assert await a.audit("s", CHANNEL_PASSIVE, "希望对你有帮助") is None
    assert "希望对你有帮助" not in caplog.text
    clock.t += 3601
    assert not a.paused


@pytest.mark.asyncio
async def test_scan_exception_falls_back(monkeypatch, store):
    from astrbot_plugin_iris_memory.iris_memory.lifelike.style import guard

    def boom(*a, **k):
        raise ValueError("x")

    monkeypatch.setattr(guard, "scan", boom)
    a, _ = _auditor(store)
    assert await a.audit("s", CHANNEL_PASSIVE, "希望对你有帮助") is None
    assert store.list_style_audit() == []


@pytest.mark.asyncio
async def test_mode_getter_exception_means_off(store):
    def bad():
        raise RuntimeError

    a = StyleAuditor(bad, lambda: store)
    assert await a.audit("s", CHANNEL_PASSIVE, "希望对你有帮助") is None


@pytest.mark.asyncio
async def test_scope_lru_bounded(store):
    a, _ = _auditor(store)
    for i in range(300):
        a.check(f"s{i}", CHANNEL_PASSIVE, "好的。")
    assert len(a._scopes) == 256


# ---------------------------------------------------------------- main.py 接线


def _plugin(store_or_none, mode="log"):
    from astrbot_plugin_iris_memory.main import IrisMemoryPlugin

    p = object.__new__(IrisMemoryPlugin)
    p.config = SimpleNamespace(get=lambda k, d=None: mode if k == "lifelike_style_guard_mode" else d)
    p._continuity = (
        SimpleNamespace(is_open=True, store=store_or_none) if store_or_none is not None else None
    )
    p._style_auditor = p._build_style_auditor()
    return p


class _Result:
    def __init__(self, text, llm=True):
        from astrbot.api.message_components import Plain

        self.chain = [Plain(text)]
        self._llm = llm

    def is_llm_result(self):
        return self._llm


class _Event:
    def __init__(self, text="今天好累", group=""):
        self.message_str = text
        self._group = group

    def get_group_id(self):
        return self._group

    def is_private_chat(self):
        return not self._group

    def get_sender_id(self):
        return "123456789"


@pytest.mark.asyncio
async def test_main_passive_audit_does_not_modify_reply(store):
    p = _plugin(store)
    res = _Result("有什么需要随时告诉我～～")
    p._style_audit_passive(_Event(), res)
    await p._style_auditor.drain()
    assert res.chain[0].text == "有什么需要随时告诉我～～"  # log 模式不改写
    rows = store.list_style_audit()
    # 隐私审查 #5：只存会话短哈希，不存 uid
    assert rows and rows[0]["scope_key"] == store.style_scope_ref("private:123456789")
    assert "123456789" not in rows[0]["scope_key"]
    assert "AIW-08" in rows[0]["rule_ids"]


@pytest.mark.asyncio
async def test_main_passive_skips_non_llm_results(store):
    p = _plugin(store)
    p._style_audit_passive(_Event(), _Result("希望对你有帮助", llm=False))
    await p._style_auditor.drain()
    assert store.get_counters() == {}


@pytest.mark.asyncio
async def test_main_off_mode_no_records(store):
    p = _plugin(store, mode="off")
    p._style_audit_passive(_Event(), _Result("希望对你有帮助"))
    await p._style_auditor.drain()
    assert store.get_counters() == {}


@pytest.mark.asyncio
async def test_main_proactive_audit_on_initiate_sent(store):
    p = _plugin(store)
    p.component_manager = None
    await p._on_initiate_sent("private:123456789", "在吗？你都不理我")
    await p._style_auditor.drain()
    rows = store.list_style_audit()
    assert rows[0]["channel"] == "proactive" and "MAN-01" in rows[0]["rule_ids"]


@pytest.mark.asyncio
async def test_main_without_auditor_attribute_is_safe():
    from astrbot_plugin_iris_memory.main import IrisMemoryPlugin

    p = object.__new__(IrisMemoryPlugin)
    p._style_audit_passive(_Event(), _Result("希望对你有帮助"))  # 无 _style_auditor 属性也不抛
    p.component_manager = None
    await p._on_initiate_sent("g", "x")


@pytest.mark.asyncio
async def test_main_closed_continuity_skips_write():
    p = _plugin(None)
    assert p._style_audit_store() is None
    p._style_audit_passive(_Event(), _Result("希望对你有帮助"))
    await p._style_auditor.drain()


def test_style_audit_prunes_old_rows_daily(tmp_path):
    clock = {"t": 1_790_000_000.0}
    st = ContinuityStore(tmp_path / "c.db", clock=lambda: clock["t"])
    st.open()
    try:
        st.record_style_audit("private:1", "private", ["AIW-01"])
        clock["t"] += (ContinuityStore.STYLE_AUDIT_RETENTION_DAYS + 1) * 86400.0
        st.record_style_audit("-100", "group", ["AIW-02"])
        rows = st.list_style_audit()
        assert [r["rule_ids"] for r in rows] == [["AIW-02"]]
        assert rows[0]["scope_key"].startswith("h:") and "-100" not in rows[0]["scope_key"]
    finally:
        st.close()
