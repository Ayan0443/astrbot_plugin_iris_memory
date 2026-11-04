"""P0 全部开关关闭时与现网提交 44c2f26 行为一致的回归快照（集成验收 ④）。

两部分：
1. prompt 组装：复用 E9 的真实 ``on_llm_request`` 夹具，但把 **所有** P0 新开关（含 E9 没列出的
   error_friendly_agent / emotion_hint / activity_gate / empty_window / budget 等）设为关闭，
   断言与 E9 基准（44c2f26 生成）逐字节一致，且关闭时不会向事件写入任何新 extra。
2. 主动决策关键路径：``ProactiveEngine`` 的调度扫描（``_scan_due`` / ``evaluate_timer``）、
   群聊与私聊（无桥接）发起、主人私聊（桥接）发起与空窗场景。固定时钟（凌晨 03:00，
   正好落在活动窗口先验时段内）与固定随机数，记录全部 LLM 调用参数、发送调用、桥接调用序列
   与状态字段，与 ``snapshots/p0_off_proactive_snapshot.json`` 逐字节比对。

基准生成方式：在 44c2f26 的临时 worktree 中放入本文件，``LIFELIKE_P0_OFF_UPDATE=1`` 运行一次，
把生成的 JSON 复制回来（44c2f26 没有任何 lifelike 键，等价于"开关全部不存在"）。
本文件不得 import 任何 lifelike 模块，保证在 44c2f26 上也能运行。
"""

from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import astrbot_plugin_iris_memory.iris_memory.config as config_mod
from astrbot_plugin_iris_memory.iris_memory.proactive.config import ConfigManager
from astrbot_plugin_iris_memory.iris_memory.proactive.decision import DecisionCore
from astrbot_plugin_iris_memory.iris_memory.proactive.perception import (
    ContextPackager,
    SlidingWindow,
    WindowMessage,
)
from astrbot_plugin_iris_memory.iris_memory.proactive import proactive as proactive_mod
from astrbot_plugin_iris_memory.iris_memory.proactive.proactive import ProactiveEngine
from astrbot_plugin_iris_memory.iris_memory.proactive.signals import SignalGate
from astrbot_plugin_iris_memory.iris_memory.proactive.state import StateManager

SNAPSHOT = Path(__file__).resolve().parent / "snapshots" / "p0_off_proactive_snapshot.json"
E9_SNAPSHOT = Path(__file__).resolve().parent / "snapshots" / "e9_prompt_snapshot.json"

# ROADMAP §2.1 中 P0 引入的全部开关：关闭/off/只记录的取值
ALL_P0_OFF = {
    "lifelike_enable": False,
    # P0-1 / P0-8
    "lifelike_strip_bypass_tools": False,
    "lifelike_proactive_quiet_hours": "",
    "lifelike_proactive_activity_gate": False,
    "lifelike_proactive_empty_window": False,
    # P0-2
    "lifelike_error_friendly_agent": False,
    "lifelike_adapter_watchdog": False,
    # P0-3
    "lifelike_log_redact": False,
    "lifelike_retention_days_observed": 0,
    "lifelike_retention_dry_run": True,
    # P0-4 / §2.3a
    "lifelike_session_extract_mode": "off",
    "lifelike_budget_cheap_calls_daily": 0,
    "lifelike_budget_cheap_tokens_daily": 0,
    # P0-5
    "lifelike_numeric_guard": False,
    "lifelike_emotion_hint": "raw",
    # P0-6
    "lifelike_style_guard_mode": "off",
    "learning_cutesy_gate": False,
    "learning_persona_review_anime": False,
    # P1-2 / P1-3 / P1-4（口吻风格层与 learning 反自我强化；关闭取值 = 改造前行为）
    "style_anime_register": "off",
    "style_inject_proactive": False,
    "learning_pattern_rotation": False,
    "learning_few_shot_scene_filter": False,
    "learning_expression_tail_particle_rule": True,
    "learning_inject_paused": False,
    # P1-8 / P1-12（关闭时与 3ad413e 一致）
    "lifelike_initiative_mode": "off",
    "lifelike_care_bridge_mode": "off",
    # P1-5 / P1-6 / P1-11 / P1-7 / P1-13 / R1 / R2（P1 集成；关闭时与 3ad413e 一致）
    "lifelike_world_mode": "off",
    "lifelike_relation_mode": "off",
    "lifelike_bond_mode": "off",
    "lifelike_memory_mode": "off",
    "lifelike_member_mode": "off",
    "lifelike_private_l2_filter": False,
    "lifelike_group_global_memory_guard": False,
    "lifelike_dream_contradiction_archive": False,
}

OWNER = "10001"
PRIVATE = f"private:{OWNER}"
GROUP = "-1009990001"
SELF_ID = "30003"
FIXED_NOW = 1793646000.0  # 2026-11-03 03:00 Asia/Shanghai（活动窗口先验时段内）

NEVER_MUTE = {"start_hour": 0, "start_minute": 0, "end_hour": 0, "end_minute": 0}

DECISION_JSON = (
    '{"action":"speak","topic":"接着问上次说的好感度 90 分那个游戏",'
    '"why_now":"空档自然","topic_source":"recent_context","obs":"在聊游戏",'
    '"watch":[],"watch_keywords":[],"why":"回访","drifted":false,"cooldown":0}'
)
SPEECH = "那个游戏后来通关了吗"
PROFILE_TEXT = "称呼: 小雨\n好感度: 80\n兴趣: 摄影"


# --------------------------------------------------------------------------- 1. prompt


@pytest.mark.asyncio
async def test_prompt_assembly_all_p0_off_matches_44c2f26(tmp_path, monkeypatch):
    from . import test_e9_prompt_snapshot as e9

    monkeypatch.setattr(e9, "P0_BASELINE_SWITCHES", dict(ALL_P0_OFF))
    extras_seen: list[set] = []
    orig_init = e9.FakeEvent.__init__

    def _init(self, *a, **k):
        orig_init(self, *a, **k)
        extras_seen.append(self.extras)

    monkeypatch.setattr(e9.FakeEvent, "__init__", _init)
    got = await e9._render_all(tmp_path, monkeypatch)
    want = json.loads(E9_SNAPSHOT.read_text(encoding="utf-8"))
    assert sorted(got) == sorted(want)
    for cid in sorted(want):
        assert json.dumps(got[cid], ensure_ascii=False, sort_keys=True) == json.dumps(
            want[cid], ensure_ascii=False, sort_keys=True
        ), f"prompt drift with all P0 off: {cid}"
    # 关闭 P0-2 时不写友好报错 extra（与 44c2f26 一致：core 读到的仍是人格自己的设置）
    for extras in extras_seen:
        assert "persona_custom_error_message" not in extras


# --------------------------------------------------------------------------- 2. proactive


class _Rec:
    """记录器：所有外部调用按顺序记成 (name, args) 便于快照。"""

    def __init__(self) -> None:
        self.calls: list = []

    def add(self, name: str, **kw) -> None:
        self.calls.append([name, {k: _plain(v) for k, v in sorted(kw.items())}])


def _plain(v):
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in sorted(v.items())}
    return type(v).__name__


class _Bridge:
    """主人私聊桥接桩：只实现 44c2f26 就存在的接口；新接口一旦被调用就记下来（快照会漂移）。"""

    def __init__(self, rec: _Rec) -> None:
        self.rec = rec

    def __getattr__(self, name):
        # 44c2f26 没有的接口（is_owner_private_scope / activity_snapshot ...）：记录后表现为"不存在"
        if name.startswith("_"):
            raise AttributeError(name)
        self.rec.add(f"bridge.UNEXPECTED.{name}")
        raise AttributeError(name)

    async def begin_turn(self, group_id, force):
        self.rec.add("bridge.begin_turn", group_id=group_id, force=force)
        return SimpleNamespace(turn={"turn_id": "t1"}, reason="", candidates=None)

    async def avoid_topics(self, group_id):
        self.rec.add("bridge.avoid_topics", group_id=group_id)
        return ["前任"]

    async def speech_context(self, group_id, item_id):
        self.rec.add("bridge.speech_context", group_id=group_id, item_id=item_id)
        return "<iris:continuity>\n她记得：周五交论文\n好感 90分\n</iris:continuity>"

    async def mark_turn(self, turn_id, state, **kw):
        self.rec.add("bridge.mark_turn", turn_id=turn_id, state=state, **kw)

    async def close_turn(self, turn_id, state, reason=""):
        self.rec.add("bridge.close_turn", turn_id=turn_id, state=state, reason=reason)

    async def presend_check(self, turn_id, umo, paused=False):
        self.rec.add("bridge.presend_check", turn_id=turn_id, umo=umo, paused=paused)
        return SimpleNamespace(ok=True, reason="")

    async def send_part(self, turn_id, umo, part, idx):
        self.rec.add("bridge.send_part", turn_id=turn_id, umo=umo, part=part, idx=idx)
        return SimpleNamespace(sent=True, status="sent", error="")

    async def finish_turn(self, turn_id, stop_reason=""):
        self.rec.add("bridge.finish_turn", turn_id=turn_id, stop_reason=stop_reason)
        return SimpleNamespace(
            parts_sent=1, sent_texts=[SPEECH], state="sent", reason="", turn_id=turn_id,
        )

    async def owner_replied_since(self, turn_id):
        self.rec.add("bridge.owner_replied_since", turn_id=turn_id)
        return False

    async def reconcile(self, force_query=False):
        self.rec.add("bridge.reconcile", force_query=force_query)

    async def restore_windows(self, window):
        self.rec.add("bridge.restore_windows")


def _cfg() -> ConfigManager:
    cfg = {
        "proactive": {
            "enabled": True,
            "proactive_enabled": True,
            "provider_id": "provider",
            "private_enabled": True,
            "private_whitelist": [OWNER],
        },
        "mute_period": dict(NEVER_MUTE),
        "group_initiate_enabled": True,
    }
    hidden = dict(ALL_P0_OFF)
    return ConfigManager(cfg, hidden_get=hidden.get)


def _engine(rec: _Rec, *, bridge: bool):
    config = _cfg()
    state = StateManager(config)
    window = SlidingWindow(config)
    packager = ContextPackager(config)
    core = DecisionCore(config, state, window, packager)

    async def generate_direct(**kw):
        rec.add("llm.generate_direct", **kw)
        n = sum(1 for c in rec.calls if c[0] == "llm.generate_direct")
        return DECISION_JSON if n % 2 == 1 else SPEECH

    async def send_message(umo, chain):
        rec.add("context.send_message", umo=umo, text=str(getattr(chain, "chain", chain)))
        return True

    async def profile_get(group_id, umo):
        rec.add("profile_get", group_id=group_id, umo=umo)
        return PROFILE_TEXT

    async def on_sent(group_id, text):
        rec.add("on_initiate_sent", group_id=group_id, text=text)

    context = SimpleNamespace(
        persona_manager=SimpleNamespace(
            get_default_persona_v3=AsyncMock(return_value={"prompt": "【人设占位】"})
        ),
        send_message=send_message,
        get_config=lambda _umo=None: {},
    )
    engine = ProactiveEngine(
        context,
        config,
        state,
        window,
        SignalGate(config, state),
        core,
        MagicMock(),  # stats：只吞调用
        llm_manager=SimpleNamespace(generate_direct=generate_direct),
        packager=packager,
        umo_get=lambda gid: f"umo:{gid}",
        is_busy=lambda _gid: False,
        self_id_get=lambda: SELF_ID,
        save_fn=AsyncMock(),
        on_initiate_sent=on_sent,
        profile_get=profile_get,
        bridge=_Bridge(rec) if bridge else None,
    )
    return engine, state, window


def _state_fields(state: StateManager, gid: str) -> dict:
    d = state.get_state(gid)
    keys = (
        "state", "last_human_message_at", "last_initiate_time", "initiate_pending_since",
        "initiate_next_check_at",
    )
    out = {}
    for k in keys:
        v = getattr(d, k, None)
        out[k] = _plain(v.value if hasattr(v, "value") else v)
    return out


@pytest.fixture
def frozen(monkeypatch):
    clock = {"now": FIXED_NOW}
    monkeypatch.setattr(time, "time", lambda: clock["now"])
    # 低随机数：概率门控一旦允许就点火，便于覆盖"点火 → 发起"路径
    monkeypatch.setattr(random, "random", lambda: 0.001)
    monkeypatch.setattr(random, "uniform", lambda a, b: a + (b - a) * 0.5)
    monkeypatch.setattr(random, "shuffle", lambda seq: None)
    fake = SimpleNamespace(get=lambda key, default=None: ALL_P0_OFF.get(key, default))
    monkeypatch.setattr(config_mod, "get_config", lambda: fake)
    # 时间提示走 datetime.now()，固定为常量文本
    monkeypatch.setattr(
        proactive_mod, "resolve_datetime_reminder", lambda _ctx, _umo: "当前时间：2026-11-03 03:00"
    )
    return clock


async def _scenario_scan(clock) -> dict:
    """调度扫描：群（有窗口）、主人私聊（桥接、窗口为空）。"""
    rec = _Rec()
    engine, state, window = _engine(rec, bridge=True)
    old = clock["now"] - 6 * 3600
    for gid in (GROUP, PRIVATE):
        state.add_to_whitelist(gid)
    state.record_human_message(GROUP, old)
    window.append(GROUP, WindowMessage("u1", "阿青", "周末去爬山吗", old))
    # 私聊：账本里有过互动，但窗口为空（重启场景）
    state.record_human_message(PRIVATE, 0.0)
    fired: list = []
    orig = engine._signals.evaluate_timer

    def _eval(gid, msgs, **kw):
        r = orig(gid, msgs, **kw)
        fired.append([gid, len(msgs or []), sorted(kw), r])
        return r

    engine._signals.evaluate_timer = _eval
    engine.attempt_initiate = AsyncMock(return_value="stub")
    await engine._ensure_schedules()
    snap1 = {gid: _state_fields(state, gid) for gid in (GROUP, PRIVATE)}
    deadline1 = engine._next_deadline()
    # 推进到群的候选时间之后，连扫三轮（每轮之后重新补齐预约）
    for _ in range(3):
        nxt = state.get_next_initiate_check(GROUP)
        clock["now"] = max(clock["now"], nxt) + 1
        await engine._scan_due()
        await engine._ensure_schedules()
    return {
        "calls": rec.calls,
        "evaluate_timer": fired,
        "attempt_initiate": [list(c.args) for c in engine.attempt_initiate.call_args_list],
        "state_after_first": snap1,
        "state_after_second": {gid: _state_fields(state, gid) for gid in (GROUP, PRIVATE)},
        "deadline_after_first": deadline1,
        "next_deadline": engine._next_deadline(),
    }


async def _scenario_initiate(clock, gid: str, *, bridge: bool) -> dict:
    rec = _Rec()
    engine, state, window = _engine(rec, bridge=bridge)
    state.add_to_whitelist(gid)
    old = clock["now"] - 5 * 3600
    sender = OWNER if gid.startswith("private:") else "u1"
    window.append(gid, WindowMessage(sender, "小雨", "那个游戏好感度刷到 90 分了", old))
    window.append(gid, WindowMessage(SELF_ID, "我", "这么快", old + 30))
    state.record_human_message(gid, old)
    result = await engine.attempt_initiate(gid)
    return {
        "result": result,
        "calls": rec.calls,
        "state": _state_fields(state, gid),
        "window": [[m.sender_name, m.content] for m in window.get_messages(gid)],
    }


async def _render(clock) -> dict:
    out = {"scan": await _scenario_scan(clock)}
    clock["now"] = FIXED_NOW
    out["group_legacy"] = await _scenario_initiate(clock, GROUP, bridge=False)
    clock["now"] = FIXED_NOW
    out["private_legacy"] = await _scenario_initiate(clock, PRIVATE, bridge=False)
    clock["now"] = FIXED_NOW
    out["private_bridge"] = await _scenario_initiate(clock, PRIVATE, bridge=True)
    clock["now"] = FIXED_NOW
    return out


@pytest.mark.asyncio
async def test_proactive_all_p0_off_matches_44c2f26(frozen):
    got = json.loads(json.dumps(await _render(frozen), ensure_ascii=False, sort_keys=True))
    if os.environ.get("LIFELIKE_P0_OFF_UPDATE") == "1":
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(
            json.dumps(got, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    want = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert sorted(got) == sorted(want)
    for key in sorted(want):
        assert json.dumps(got[key], ensure_ascii=False, sort_keys=True) == json.dumps(
            want[key], ensure_ascii=False, sort_keys=True
        ), f"proactive drift with all P0 off: {key}"


@pytest.mark.asyncio
async def test_proactive_snapshot_is_meaningful(frozen):
    got = await _render(frozen)
    # 三个发起场景都真正走到了决策 + 发言两次 LLM 调用，且数值行原样保留（守卫关闭）
    for key in ("group_legacy", "private_legacy", "private_bridge"):
        llm = [c for c in got[key]["calls"] if c[0] == "llm.generate_direct"]
        assert len(llm) == 2, key
        assert "90" in llm[1][1]["prompt"], key
    bridge_names = [c[0] for c in got["private_bridge"]["calls"]]
    assert "bridge.send_part" in bridge_names
    assert not any(n.startswith("bridge.UNEXPECTED") for n in bridge_names)
    assert not any(c[0].startswith("bridge.UNEXPECTED") for c in got["scan"]["calls"])
    # 群聊确实走到了概率门控；空窗私聊在开关关闭时不调度（= 现网）
    assert any(e[0] == GROUP and e[3] == "initiate" for e in got["scan"]["evaluate_timer"])
    assert got["scan"]["attempt_initiate"]
    assert all(e[0] != PRIVATE for e in got["scan"]["evaluate_timer"])
    assert got["scan"]["state_after_first"][PRIVATE]["initiate_next_check_at"] == 0.0
