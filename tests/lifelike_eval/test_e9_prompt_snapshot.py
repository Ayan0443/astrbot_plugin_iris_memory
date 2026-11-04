"""E9：开关 off 时 prompt 组装逐字节一致的快照断言（ROADMAP P0-0 验收 ③）。

对 10 条固定输入（评测集 E9-01..10，原创）走真实的 ``IrisMemoryPlugin.on_llm_request``：
L1 格式化、画像格式化（真实 ``_collect_user_profile`` + ``_format_profiles_for_injection``）、
连续性 ``<iris:continuity>`` 组装、收尾提示、群聊发言说明。外部依赖全部替换为假对象：
固定时钟、tmp 连续性库、假 L1 缓冲、假画像管理器；L2/L3/learning/图片/表情按配置关闭。

基准文件 ``snapshots/e9_prompt_snapshot.json`` 在现网提交 44c2f26 上生成。
任何 P0/P1 改动在"总开关 off 且 P0 开关取 off/log"（ROADMAP §2.1）时都必须让本测试保持通过；
有意改变 prompt 的改动需要另立基准：``LIFELIKE_E9_UPDATE=1`` 重新生成并在提交说明里写明原因。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import astrbot_plugin_iris_memory.iris_memory.config as config_mod
import astrbot_plugin_iris_memory.iris_memory.core.persona as persona_mod
import astrbot_plugin_iris_memory.iris_memory.platform as platform_mod
import astrbot_plugin_iris_memory.iris_memory.profile as profile_mod
from astrbot_plugin_iris_memory.iris_memory.continuity import models as m
from astrbot_plugin_iris_memory.iris_memory.continuity.service import ContinuityService
from astrbot_plugin_iris_memory.iris_memory.l1_buffer.models import ContextMessage
from astrbot_plugin_iris_memory.main import IrisMemoryPlugin
from astrbot_plugin_iris_memory.tools.lifelike_eval import evalset as ES

SNAPSHOT = Path(__file__).resolve().parent / "snapshots" / "e9_prompt_snapshot.json"

OWNER = "10001"
MEMBER = "20002"
SELF_ID = "30003"
GROUP = "-1009990001"
SCOPE = f"private:{OWNER}"
FIXED_NOW = 1793600000.0  # 2026-11-02 固定时钟
FIXED_DT = datetime(2026, 11, 2, 20, 0, 0)

# ROADMAP §2.1：E9 基准 = 总开关 off 且 P0 开关取 off/log。
# 这些键在本提交里还不存在；写在这里是为了让合入 P0 各项后本测试仍按基准口径运行。
P0_BASELINE_SWITCHES = {
    "lifelike_enable": False,
    "lifelike_strip_bypass_tools": False,
    "lifelike_adapter_watchdog": False,
    "lifelike_log_redact": False,
    "lifelike_retention_dry_run": True,
    "lifelike_session_extract_mode": "off",
    "lifelike_numeric_guard": False,
    "lifelike_style_guard_mode": "off",
    "lifelike_proactive_quiet_hours": "",
    "style_anime_register": "off",
    "style_inject_proactive": False,
    "learning_pattern_rotation": False,
    "learning_few_shot_scene_filter": False,
    "learning_expression_tail_particle_rule": True,
    "learning_inject_paused": False,
    "learning_cutesy_gate": False,
    # P1 子模块（总开关 off 时同样视为 off；这里显式写出关闭取值）
    "lifelike_world_mode": "off",
    "lifelike_relation_mode": "off",
    "lifelike_bond_mode": "off",
    "lifelike_memory_mode": "off",
    "lifelike_member_mode": "off",
    "lifelike_initiative_mode": "off",
    "lifelike_care_bridge_mode": "off",
    "lifelike_private_l2_filter": False,
    "lifelike_group_global_memory_guard": False,
}


# 连续性服务的配置源（ContinuityService.config_source）额外键；默认空 = 原基准口径。
# P1 回归（test_p1_off_shadow_regression）用它把 P1 模块开关同时送进连续性服务。
CONT_SWITCHES: dict = {}


class FakeConfig:
    def __init__(self, values: dict) -> None:
        self.values = dict(values)

    def get(self, key, default=None):
        return self.values.get(key, default)


def _iris_config() -> FakeConfig:
    return FakeConfig(
        {
            **P0_BASELINE_SWITCHES,
            "l1_buffer.inject_queue_length": 50,
            "l1_inject_max_content_chars": 200,
            "l1_buffer.image_parsing.enable": False,
            "profile.enable": True,
            "profile.enable_auto_injection": True,
            "isolation_config.enable_group_isolation": True,
            "l2_memory.enable": False,
            "l3_kg.enable": False,
            "learning.enable": False,
            "context_control.enable_conversation_cleanup": True,
        }
    )


class FakeEvent:
    def __init__(self, text: str, *, sender: str, group_id: str = "", name: str = "") -> None:
        self.message_str = text
        self.message_obj = SimpleNamespace(
            message_id="501", raw_message=SimpleNamespace(id=501, out=False, reply_to=None),
            session_id=group_id or sender, message_str=text, timestamp=int(FIXED_NOW),
        )
        self._sender = sender
        self._group = group_id
        self._name = name
        self.extras: dict = {}
        self.unified_msg_origin = (
            f"userbot:GroupMessage:{group_id}" if group_id else f"userbot:FriendMessage:{sender}"
        )
        self.is_at_or_wake_command = True

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def stop_event(self):
        pass

    def is_stopped(self):
        return False

    def get_group_id(self):
        return self._group

    def is_private_chat(self):
        return not self._group

    def get_sender_id(self):
        return self._sender

    def get_self_id(self):
        return SELF_ID

    def get_sender_name(self):
        return self._name

    def get_platform_name(self):
        return "telethon_userbot"

    def get_platform_id(self):
        return "userbot"

    def get_messages(self):
        return []


def _msg(role: str, content: str, source: str, name: str = "") -> ContextMessage:
    return ContextMessage(
        role=role, content=content, timestamp=FIXED_DT, token_count=1, source=source,
        metadata={"user_name": name} if name else {},
    )


L1_PRIVATE = [
    _msg("user", "今天下班好早", OWNER, "小雨"),
    _msg("assistant", "那正好可以早点吃饭", SELF_ID),
    _msg("user", "嗯，打算煮个面", OWNER, "小雨"),
]
L1_GROUP = [
    _msg("user", "有人周末去爬山吗", MEMBER, "阿青"),
    _msg("user", "我想去但是起不来", OWNER, "小雨"),
    _msg("assistant", "起不来就下午去嘛", SELF_ID),
]


class FakeL1:
    is_available = True

    def get_context(self, session_id, max_length):
        return list(L1_PRIVATE if str(session_id).startswith("private:") else L1_GROUP)


class FakeComponents:
    def __init__(self) -> None:
        self.l1 = FakeL1()
        self.profile = object()

    def get_available_component(self, name):
        return {"l1_buffer": self.l1, "profile": self.profile}.get(name)

    def get_component(self, name):
        return self.get_available_component(name)

    def check_component(self, name):
        return "available" if name in ("l1_buffer", "profile") else "unavailable"


def _profiles():
    user = SimpleNamespace(
        user_id=OWNER, user_name="小雨", historical_names=["雨"], personality_tags=["安静"],
        interests=["摄影", "爬山"], occupation="学生", language_style="短句",
        communication_style="直接", emotional_baseline="最近有点累", favorability=80,
        bot_relationship="朋友", important_dates=[{"date": "11-06", "description": "交论文"}],
        important_events=["搬家"], taboo_topics=["前任"], custom_fields={"宠物": "猫"},
    )
    group = SimpleNamespace(
        interests=["爬山"], atmosphere_tags=["轻松"], long_term_tags=[], blacklist_topics=[], custom_fields={},
    )
    return group, user


class _FakeUserMgr:
    def __init__(self, storage) -> None:
        pass

    async def get_or_create(self, user_id, group_id, persona_id):
        if str(user_id) == OWNER:
            return _profiles()[1]
        return SimpleNamespace(
            user_id=str(user_id), user_name="阿青", historical_names=[], personality_tags=[], interests=["爬山"],
            occupation="", language_style="", communication_style="", emotional_baseline="", favorability=50,
            bot_relationship="", important_dates=[], important_events=[], taboo_topics=[], custom_fields={},
        )


class _FakeGroupMgr:
    def __init__(self, storage) -> None:
        pass

    async def get_or_create(self, group_id, persona_id):
        return _profiles()[0]


def _seed(svc: ContinuityService) -> None:
    store = svc.store
    store.put_private_context("identity", "她是 aming 在网上认识的朋友。", "e9", SCOPE)
    store.put_private_context("relationship", "两个人聊得来，说话随意。", "e9", SCOPE)
    store.create_item(
        subject_id=OWNER, subject_type="user", scope_key=SCOPE, topic="周五交论文",
        summary="周五要交论文", kind=m.KIND_PLAN, source_kind=m.SOURCE_SHARED_DIALOGUE,
        source_refs=[], allowed_audiences=[SCOPE], event_at_start=FIXED_NOW + 3 * 86400,
    )


async def _build(tmp_path: Path, monkeypatch) -> tuple[IrisMemoryPlugin, ContinuityService]:
    cont_cfg = FakeConfig({"proactive.silent_sessions": [], **CONT_SWITCHES})
    context = SimpleNamespace(get_platform_inst=lambda pid: None)
    state = SimpleNamespace(
        get_whitelist=lambda: [SCOPE], resume_after_owner_message=lambda gid: False,
        is_whitelisted=lambda gid: False,
    )
    svc = ContinuityService(
        context=context, config_source=cont_cfg, state=state, owner_ids_get=lambda: [OWNER],
        silent_sessions_get=lambda: [], data_dir=tmp_path,
        llm_manager=SimpleNamespace(generate=AsyncMock(return_value='{"ops":[]}')),
        clock=lambda: FIXED_NOW,
    )
    await svc.start()
    _seed(svc)

    p = object.__new__(IrisMemoryPlugin)
    p.context = context
    p.config = cont_cfg
    p._continuity = svc
    p._state = state
    p._llm_manager = None
    p._passive_active = {}
    p._reply_in_progress = {}
    p._triggering = {}
    p._reply_config = SimpleNamespace(enabled=False, private_whitelist=[OWNER])
    p.component_manager = FakeComponents()
    p._sticker_entries = AsyncMock(return_value=(None, []))

    adapter = SimpleNamespace(
        get_session_id=lambda e: f"private:{e.get_sender_id()}" if e.is_private_chat() else e.get_group_id(),
        get_group_id=lambda e: e.get_group_id() or "",
        get_user_id=lambda e: e.get_sender_id(),
    )
    monkeypatch.setattr(platform_mod, "get_adapter", lambda e: adapter)
    monkeypatch.setattr(persona_mod, "resolve_persona", AsyncMock(return_value="default"))
    monkeypatch.setattr(config_mod, "get_config", _iris_config)
    monkeypatch.setattr(profile_mod, "UserProfileManager", _FakeUserMgr)
    monkeypatch.setattr(profile_mod, "GroupProfileManager", _FakeGroupMgr)
    return p, svc


def _event_for(case: ES.Case) -> FakeEvent:
    speaker = case.raw.get("speaker", "owner")
    sender = OWNER if speaker == "owner" else MEMBER
    name = "小雨" if sender == OWNER else "阿青"
    return FakeEvent(case.turns[0], sender=sender, group_id=GROUP if case.session == "group" else "", name=name)


async def _render_all(tmp_path: Path, monkeypatch) -> dict:
    p, svc = await _build(tmp_path, monkeypatch)
    try:
        cases = ES.by_set(ES.load(), "E9")
        assert len(cases) == 10
        out = {}
        for case in cases:
            if case.turns[0] == "晚安":
                svc.closure.on_owner_message(SCOPE, "晚安")
            req = SimpleNamespace(
                prompt=case.turns[0], system_prompt="【人设占位】", contexts=[{"role": "user", "content": "旧"}],
                extra_user_content_parts=[], conversation=SimpleNamespace(persona_id="default"),
            )
            await p.on_llm_request(_event_for(case), req)
            out[case.id] = {
                "prompt": req.prompt,
                "system_prompt": req.system_prompt,
                "contexts": req.contexts,
                "parts": [getattr(x, "text", str(x)) for x in req.extra_user_content_parts],
            }
        return out
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_e9_smoke_prompt_snapshot_byte_identical(tmp_path, monkeypatch):
    got = await _render_all(tmp_path, monkeypatch)
    if os.environ.get("LIFELIKE_E9_UPDATE") == "1":
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps(got, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    want = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    for cid in sorted(want):
        assert json.dumps(got.get(cid), ensure_ascii=False, sort_keys=True) == json.dumps(
            want[cid], ensure_ascii=False, sort_keys=True
        ), f"E9 prompt drift: {cid}"
    assert sorted(got) == sorted(want)


@pytest.mark.asyncio
async def test_e9_snapshot_is_deterministic_and_meaningful(tmp_path, monkeypatch):
    a = await _render_all(tmp_path / "a", monkeypatch)
    b = await _render_all(tmp_path / "b", monkeypatch)
    assert a == b
    # 快照确实覆盖了各注入段，不是空壳
    private = "\n".join(a["E9-02"]["parts"])
    assert "<iris:l1_context>" in private and "<iris:profile>" in private
    assert any(x.startswith("<iris:continuity>") for x in a["E9-02"]["parts"])
    assert any("收尾" in x for x in a["E9-03"]["parts"])
    group = "\n".join(a["E9-07"]["parts"])
    assert "<iris:l1_context>" in group and "周五交论文" not in group
    # 人格/关系数值永不进 prompt（P4）
    for v in a.values():
        joined = "\n".join(v["parts"]) + v["system_prompt"]
        assert "好感" not in joined and "80" not in joined
