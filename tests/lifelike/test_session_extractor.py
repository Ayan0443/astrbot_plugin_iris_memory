"""SessionExtractor（P0-4）：tmp_path 库 + 可控时钟 + 假 LLM，不连真实数据目录。"""

import asyncio
import json
from datetime import datetime

import pytest
from astrbot_plugin_iris_memory.iris_memory.continuity import models as m
from astrbot_plugin_iris_memory.iris_memory.continuity.extractor import ItemExtractor
from astrbot_plugin_iris_memory.iris_memory.continuity.store import ContinuityStore
from astrbot_plugin_iris_memory.iris_memory.lifelike.budget import CheapBudget
from astrbot_plugin_iris_memory.iris_memory.lifelike.memory import session_extractor as se
from astrbot_plugin_iris_memory.iris_memory.lifelike.memory.session_extractor import (
    SessionExtractor,
    build_session_prompt,
    normalize_mode,
    parse_session_json,
)

OWNER = "123456789"
SCOPE = f"private:{OWNER}"
BASE = datetime(2026, 11, 3, 20, 0, tzinfo=m.TZ_SHANGHAI).timestamp()
CUR = se.CURSOR_PREFIX + SCOPE


class Clock:
    def __init__(self, t=BASE):
        self.t = t

    def __call__(self):
        return self.t


class FakeLLM:
    def __init__(self, *script):
        self.script = list(script)
        self.prompts = []

    async def __call__(self, prompt, timeout):
        self.prompts.append(prompt)
        if not self.script:
            raise AssertionError("unexpected LLM call")
        out = self.script.pop(0)
        if callable(out) and not isinstance(out, Exception):
            out = out(prompt)
        if isinstance(out, Exception):
            raise out
        if isinstance(out, dict):
            return json.dumps(out, ensure_ascii=False)
        return out

    @property
    def calls(self):
        return len(self.prompts)


def items(*ops):
    return {"items": list(ops), "beliefs": [], "moments": [], "rel_signals": []}


def create(topic, evidence, kind="unfinished_topic", **kw):
    return {"op": "create", "item_id": "", "kind": kind, "topic": topic, "summary": "",
            "when_text": kw.get("when_text", ""), "remind_requested": False, "evidence": evidence}


class Env:
    def __init__(self, tmp_path, llm, **cfg):
        self.clock = Clock()
        self.store = ContinuityStore(tmp_path / "continuity.db", clock=self.clock)
        self.store.open()
        self.cfg = {"lifelike_session_extract_mode": "on", **cfg}
        self.llm = llm
        self.items = ItemExtractor(self.store, FakeLLM(), clock=self.clock)
        self.budget = CheapBudget(self.store, lambda k, d=None: self.cfg.get(k, d), self.clock)
        self.sx = SessionExtractor(
            self.store,
            self.items,
            llm,
            self.budget,
            mode_get=lambda: self.cfg.get("lifelike_session_extract_mode"),
            clock=self.clock,
            timeout_get=lambda: 10,
        )
        self.mid = 100

    def add(self, text, *, status="skipped", sender=OWNER, dt=30.0):
        self.mid += 1
        self.clock.t += dt
        self.store.record_inbound(SCOPE, self.mid, sender, text, self.clock.t)
        self.store.advance_watermark(SCOPE, self.mid, self.clock.t)
        if status != "pending":
            self.store.mark_inbound(SCOPE, [self.mid], status, "")
        return self.mid

    def bot(self, text, dt=5.0):
        self.clock.t += dt
        return self.store.add_outbound(SCOPE, "reply", text, m.OUT_SENT, "bot")

    def status(self, mid):
        return self.store.get_inbound(SCOPE, mid)["extract_status"]

    def state(self):
        raw = self.store.get_cursor(CUR)
        return None if raw is None else json.loads(raw)

    def active(self):
        return self.store.list_items(scope_key=SCOPE, statuses=(m.STATUS_ACTIVE,), limit=50)

    async def init(self):
        rep = await self.sx.run_scope(SCOPE, "init")
        assert rep.status == "empty"


@pytest.fixture
def make_env(tmp_path):
    envs = []

    def _make(*script, **cfg):
        e = Env(tmp_path / f"e{len(envs)}", FakeLLM(*script), **cfg)
        envs.append(e)
        return e

    yield _make
    for e in envs:
        e.store.close()


# ---------------------------------------------------------------- pure helpers


def test_normalize_mode():
    assert normalize_mode("ON") == "on" and normalize_mode(" shadow ") == "shadow"
    assert normalize_mode(None) == "off" and normalize_mode("true") == "off"


def test_parse_session_json_variants():
    assert parse_session_json('```json\n{"items":[{"op":"create"}]}\n```') == [{"op": "create"}]
    assert parse_session_json('前缀 {"ops": []} 后缀') == []
    assert parse_session_json('{"beliefs": []}') is None
    assert parse_session_json("not json") is None
    assert parse_session_json("[1]") is None
    assert parse_session_json(None) is None
    assert parse_session_json("{bad}") is None


def test_prompt_labels_speakers_and_done_rows():
    owner = [
        {"msg_id": 1, "msg_ts": BASE, "_text": "我在学吉他", "extract_status": "skipped"},
        {"msg_id": 2, "msg_ts": BASE + 60, "_text": "周五考试", "extract_status": "done", "_linked": ["x"]},
    ]
    bot = [{"text": "我也想学钢琴", "sent_at": BASE + 30}]
    p = build_session_prompt(owner, bot, {})
    lines = p.split("<dialogue>")[1].splitlines()
    assert "【主人】我在学吉他" in lines[1]
    assert "【角色】我也想学钢琴" in lines[2]
    assert "已记录" in lines[3] and "回复 角色" in lines[3]


# ---------------------------------------------------------------- off / init


@pytest.mark.asyncio
async def test_off_mode_touches_nothing(make_env):
    env = make_env(lifelike_session_extract_mode="off")
    env.add("最近在学吉他，手指好疼，想坚持下去")
    rep = await env.sx.run_scope(SCOPE, "manual")
    await env.sx.note_inbound(SCOPE)
    await env.sx.on_closure(SCOPE)
    await env.sx.start([SCOPE])
    assert rep.status == "off"
    assert env.llm.calls == 0 and env.state() is None
    assert not env.sx.running and env.sx.is_idle()
    assert env.store.get_counters() == {}


@pytest.mark.asyncio
async def test_first_enable_does_not_backfill_old_rows(make_env):
    env = make_env()
    old = env.add("以前的消息，很长很长很长的一段内容内容内容")
    rep = await env.sx.run_scope(SCOPE, "manual")
    assert rep.status == "empty" and env.llm.calls == 0
    assert env.state()["last_msg_id"] == old
    assert env.status(old) == "skipped"


# ---------------------------------------------------------------- on mode


@pytest.mark.asyncio
async def test_on_creates_item_marks_rows_and_no_duplicate_run(make_env):
    env = make_env(items(create("学吉他", "最近在学吉他")))
    await env.init()
    a = env.add("最近在学吉他，手指按弦好疼，想坚持下去")
    env.bot("加油，慢慢来")
    b = env.add("想坚持练到能弹一首完整的歌")
    rep = await env.sx.run_scope(SCOPE, "idle")
    assert rep.status == "done" and env.llm.calls == 1
    assert len(rep.created) == 1
    it = env.store.get_item(rep.created[0])
    assert it["subject_type"] == "user" and it["subject_id"] == OWNER
    assert it["source_refs"][0]["msg_id"] == a
    hist = env.store.item_history(it["item_id"])
    assert hist[0]["reason"] == "session_extract_create"
    assert env.status(a) == "done" and env.status(b) == "done"
    st = env.state()
    assert st["last_msg_id"] == b and st["attempts"] == 0 and st["session_version"] >= 2
    c = env.store.get_counters()
    assert c["lifelike.session_extract.created"] == 1 and c["lifelike.session_extract.llm"] == 1
    assert c["lifelike.budget.calls:lifelike_session_extract"] == 1
    # 只有主人与她已发送的回复进入 prompt
    assert "【角色】加油，慢慢来" in env.llm.prompts[0]
    # 再跑一次：没有新消息 → 不再调用
    rep2 = await env.sx.run_scope(SCOPE, "idle")
    assert rep2.status == "empty" and env.llm.calls == 1


@pytest.mark.asyncio
async def test_bot_words_never_become_owner_items(make_env):
    env = make_env(items(
        create("觉得雨声安心", "很安心"),  # 她自己的话：evidence 不在主人消息里
        create("喜欢雨声", "我最喜欢听雨声了"),  # 主人只是复述她的原话
    ))
    await env.init()
    env.add("今天下雨了，窗外一直在响")
    env.bot("我最喜欢听雨声了，很安心")
    env.add("我最喜欢听雨声了")  # 只是复述她的原话
    env.add("嗯嗯是的是的确实如此呢")
    rep = await env.sx.run_scope(SCOPE, "idle")
    assert rep.status == "done" and rep.created == [] and rep.invalid == 2
    assert env.active() == []


@pytest.mark.asyncio
async def test_non_owner_rows_filtered(make_env):
    env = make_env(items(create("外人的事", "别人说的一段话")))
    await env.init()
    env.add("别人说的一段话很长很长很长很长", sender="12345")
    env.add("我明天想去爬山，看看天气怎么样，希望别下雨")
    rep = await env.sx.run_scope(SCOPE, "idle")
    assert rep.owner_rows == 1 and rep.created == [] and rep.invalid == 1
    assert "别人说的" not in env.llm.prompts[0]


@pytest.mark.asyncio
async def test_fast_path_done_rows_not_recreated_and_dedupe(make_env):
    env = make_env(items(
        create("周五交论文", "周五要交论文", kind="plan"),
        create("学吉他", "在学吉他"),
        create("学吉他", "在学吉他"),
    ))
    await env.init()
    env.add("周五要交论文，好紧张", status="done")
    env.add("最近还在学吉他，进步好慢")
    rep = await env.sx.run_scope(SCOPE, "closure")
    assert len(rep.created) == 1 and rep.deduped == 1 and rep.invalid == 1
    assert [i["topic"] for i in env.active()] == ["学吉他"]


@pytest.mark.asyncio
async def test_update_and_complete_existing_item(make_env):
    env = make_env()
    await env.init()
    it = env.store.create_item(
        subject_id=OWNER, subject_type="user", scope_key=SCOPE, topic="面试结果", summary="在等面试结果",
        kind=m.KIND_PENDING_RESULT, source_kind=m.SOURCE_SHARED_DIALOGUE, source_refs=[],
        allowed_audiences=[SCOPE],
    )
    env.llm.script.append(items(
        {"op": "complete", "item_id": it["item_id"], "evidence": "面试结果出来了"},
    ))
    env.add("面试结果出来了，过了！开心得想转圈圈")
    rep = await env.sx.run_scope(SCOPE, "idle")
    assert rep.closed == [it["item_id"]]
    assert env.store.get_item(it["item_id"])["status"] == m.STATUS_DONE
    assert env.store.item_history(it["item_id"])[-1]["reason"] == "session_extract_complete"


# ---------------------------------------------------------------- shadow


@pytest.mark.asyncio
async def test_shadow_counts_only(make_env):
    env = make_env(items(create("学吉他", "最近在学吉他")), lifelike_session_extract_mode="shadow")
    await env.init()
    a = env.add("最近在学吉他，手指按弦好疼，想坚持下去")
    rep = await env.sx.run_scope(SCOPE, "idle")
    assert rep.status == "shadow" and rep.proposed == {"create": 1}
    assert env.active() == [] and env.status(a) == "skipped"
    assert env.state()["last_msg_id"] == a
    assert env.store.get_counters()["lifelike.session_extract.shadow_create"] == 1


# ---------------------------------------------------------------- gate / busy / more


@pytest.mark.asyncio
async def test_gatekeeper_skips_short_or_emoji(make_env):
    env = make_env()
    await env.init()
    env.add("哈哈哈")
    env.add("😂😂😂🤣🤣")
    last = env.add("好的呀")
    rep = await env.sx.run_scope(SCOPE, "closure")
    assert rep.status == "gated" and env.llm.calls == 0
    assert env.state()["last_msg_id"] == last
    assert env.store.get_counters()["lifelike.session_extract.gated"] == 1


@pytest.mark.asyncio
async def test_busy_while_fast_path_pending(make_env):
    env = make_env(items())
    await env.init()
    mid = env.add("下周三去医院复查，有点担心结果不太好", status="pending")
    rep = await env.sx.run_scope(SCOPE, "count")
    assert rep.status == "busy" and env.llm.calls == 0
    env.clock.t += se.BUSY_PENDING_MAX_AGE_S + 1  # 长期 pending 不再阻塞
    rep = await env.sx.run_scope(SCOPE, "count")
    assert rep.status == "done" and env.status(mid) == "pending"


@pytest.mark.asyncio
async def test_more_rows_than_cap(make_env, monkeypatch):
    monkeypatch.setattr(se, "MAX_ROWS", 3)
    env = make_env(items(), items())
    await env.init()
    ids = [env.add(f"第{i}条消息，内容足够长足够长足够长") for i in range(5)]
    rep = await env.sx.run_scope(SCOPE, "count")
    assert rep.more and rep.upto == ids[2]
    rep = await env.sx.run_scope(SCOPE, "more")
    assert not rep.more and rep.upto == ids[4]


# ---------------------------------------------------------------- budget


@pytest.mark.asyncio
async def test_budget_exhausted_falls_back(make_env):
    env = make_env(lifelike_budget_cheap_calls_daily=0)
    await env.init()
    a = env.add("最近在学吉他，手指按弦好疼，想坚持下去")
    rep = await env.sx.run_scope(SCOPE, "idle")
    assert rep.status == "budget" and rep.error == "calls" and env.llm.calls == 0
    assert env.status(a) == "skipped" and env.state()["last_msg_id"] < a
    assert env.store.get_counters()["lifelike.session_extract.budget"] == 1


@pytest.mark.asyncio
async def test_token_budget_and_module_cap(make_env, monkeypatch):
    env = make_env(lifelike_budget_cheap_tokens_daily=50)
    await env.init()
    env.add("最近在学吉他，手指按弦好疼，想坚持下去")
    rep = await env.sx.run_scope(SCOPE, "idle")
    assert rep.status == "budget" and rep.error == "tokens"

    monkeypatch.setattr(se, "MODULE_DAILY_CALLS", 1)
    env2 = make_env(items())
    await env2.init()
    env2.store.bump_counter(f"lifelike.budget.calls:{se.MODULE}", 1)
    env2.add("最近在学吉他，手指按弦好疼，想坚持下去")
    rep = await env2.sx.run_scope(SCOPE, "idle")
    assert rep.status == "budget" and rep.error == "module_daily" and env2.llm.calls == 0


# ---------------------------------------------------------------- failures


@pytest.mark.asyncio
async def test_transient_failure_retries_then_gives_up(make_env):
    env = make_env(asyncio.TimeoutError(), ConnectionError(), "garbage")
    await env.init()
    a = env.add("最近在学吉他，手指按弦好疼，想坚持下去")
    rep = await env.sx.run_scope(SCOPE, "idle")
    assert rep.status == "retry" and rep.error == "llm_timeout" and rep.retry_in == se.RETRY_BACKOFF_S
    assert env.state()["attempts"] == 1 and env.state()["last_msg_id"] < a
    rep = await env.sx.run_scope(SCOPE, "retry")
    assert rep.status == "retry" and rep.error == "llm_network" and rep.retry_in == 2 * se.RETRY_BACKOFF_S
    rep = await env.sx.run_scope(SCOPE, "retry")
    assert rep.status == "failed" and rep.error == "invalid_json"
    st = env.state()
    assert st["last_msg_id"] == a and st["attempts"] == 0 and st["last_error"] == "invalid_json"
    assert env.status(a) == "skipped" and env.active() == []
    c = env.store.get_counters()
    assert c["lifelike.session_extract.retry"] == 2 and c["lifelike.session_extract.failed"] == 1
    # 失败调用同样计入预算
    assert c["lifelike.budget.calls:lifelike_session_extract"] == 3


@pytest.mark.asyncio
async def test_unexpected_llm_error_and_internal_error(make_env, monkeypatch):
    env = make_env(RuntimeError("provider"))
    await env.init()
    env.add("最近在学吉他，手指按弦好疼，想坚持下去")
    rep = await env.sx.run_scope(SCOPE, "idle")
    assert rep.status == "retry" and rep.error == "llm_error"

    def boom(*a, **k):
        raise ValueError("x")

    monkeypatch.setattr(env.items, "_collect_candidates", boom)
    rep = await env.sx.run_scope(SCOPE, "retry")
    # 审查 L1：内部异常走 _fail 的退避计数（第 2 次尝试 → retry）
    assert rep.status == "retry" and rep.error.startswith("internal:")
    assert rep.retry_in == 2 * se.RETRY_BACKOFF_S and env.state()["attempts"] == 2


@pytest.mark.asyncio
async def test_internal_error_after_llm_with_more_does_not_requeue_fast(make_env, monkeypatch):
    """审查 L1：LLM 已调用 + more=True 后内部异常，不能每 15 秒重排同一段。"""
    monkeypatch.setattr(se, "MAX_ROWS", 1)
    env = make_env(items(), items(), items())
    await env.init()
    a = env.add("最近在学吉他，手指按弦好疼，想坚持下去")
    env.add("第二条消息，内容足够长足够长足够长")

    def boom(*a_, **k):
        raise ValueError("x")

    monkeypatch.setattr(env.sx, "_advance", boom)
    rep = await env.sx.run_scope(SCOPE, "idle")
    assert env.llm.calls == 1
    assert rep.status == "retry" and rep.more is False and rep.retry_in == se.RETRY_BACKOFF_S
    assert env.state()["last_msg_id"] < a
    # 第 3 次失败：跳过本段（游标前移），后续段可以继续排
    await env.sx.run_scope(SCOPE, "retry")
    rep = await env.sx.run_scope(SCOPE, "retry")
    assert rep.status == "failed" and rep.more is True
    assert env.state()["last_msg_id"] == a and env.llm.calls == 3


def test_module_daily_cap_matches_roadmap():
    assert se.MODULE_DAILY_CALLS == 12


@pytest.mark.asyncio
async def test_circuit_breaker_pauses(make_env):
    env = make_env(*[asyncio.TimeoutError() for _ in range(5)])
    await env.init()
    for i in range(5):
        env.add(f"第{i}条：最近在学吉他，手指按弦好疼，想坚持下去")
        await env.sx.run_scope(SCOPE, "retry")
    env.add("又一条：最近在学吉他，手指按弦好疼，想坚持下去")
    rep = await env.sx.run_scope(SCOPE, "retry")
    assert rep.status == "paused" and env.llm.calls == 5
    env.clock.t += se.BREAKER_PAUSE_S + 1
    env.llm.script.append(items())
    rep = await env.sx.run_scope(SCOPE, "retry")
    assert rep.status == "done"


@pytest.mark.asyncio
async def test_cursor_cas_discards_concurrent_result(make_env):
    env = make_env()
    await env.init()
    a = env.add("最近在学吉他，手指按弦好疼，想坚持下去")

    def race(prompt):
        env.sx._save_state(SCOPE, last_msg_id=a, session_version=9)  # 别处已推进游标
        return items(create("学吉他", "最近在学吉他"))

    env.llm.script.append(race)
    rep = await env.sx.run_scope(SCOPE, "idle")
    assert rep.status == "conflict" and env.active() == [] and env.status(a) == "skipped"


# ---------------------------------------------------------------- scheduling


@pytest.mark.asyncio
async def test_schedule_triggers(make_env, monkeypatch):
    monkeypatch.setattr(se, "TRIGGER_COUNT", 3)
    env = make_env()
    await env.init()
    env.add("第一条消息，内容内容内容内容内容")
    await env.sx.note_inbound(SCOPE)
    assert env.sx.running
    due1, trig = env.sx._due[SCOPE]
    assert trig == "idle"
    # 收尾提前；之后的空闲计时不会把它推迟
    await env.sx.on_closure(SCOPE)
    due2, trig = env.sx._due[SCOPE]
    assert trig == "closure" and due2 < due1
    await env.sx.note_inbound(SCOPE)
    assert env.sx._due[SCOPE] == (due2, "closure")
    env.sx._due.clear()
    env.add("第二条")
    env.add("第三条")
    await env.sx.note_inbound(SCOPE)
    assert env.sx._due[SCOPE][1] == "count"
    await env.sx.stop()
    assert not env.sx.running and env.sx.is_idle()


@pytest.mark.asyncio
async def test_worker_loop_runs_due_scope(make_env, monkeypatch):
    monkeypatch.setattr(se, "SETTLE_S", 0.0)
    env = make_env(items(create("学吉他", "最近在学吉他")))
    await env.init()
    env.add("最近在学吉他，手指按弦好疼，想坚持下去")
    await env.sx.on_closure(SCOPE)
    for _ in range(100):
        await asyncio.sleep(0.01)
        if env.llm.calls and env.sx.is_idle():
            break
    await env.sx.stop()
    assert len(env.active()) == 1


@pytest.mark.asyncio
async def test_start_recovers_pending_segments(make_env):
    env = make_env()
    await env.init()
    env.add("最近在学吉他，手指按弦好疼，想坚持下去")
    await env.sx.start([SCOPE, "private:nobody"])
    assert env.sx._due[SCOPE][1] == "recover"
    await env.sx.stop()
