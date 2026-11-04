"""P1-2 / P1-4 <iris:style> 段组装（iris_memory/style/selector.py）

冷却轮换、卖萌上限、群聊上限、被说做作后降档、主动消息固定中档、熔断、预算裁剪、
prompt 不含数字。
"""

from __future__ import annotations

import random
import re

import pytest

import astrbot_plugin_iris_memory.iris_memory.config as config_mod
from astrbot_plugin_iris_memory.iris_memory.lifelike.guards import numeric
from astrbot_plugin_iris_memory.iris_memory.style import examples as EX
from astrbot_plugin_iris_memory.iris_memory.style import register as R
from astrbot_plugin_iris_memory.iris_memory.style import selector as S


class FakeConfig:
    def __init__(self, **values):
        self.values = dict(values)

    def get(self, key, default=None):
        return self.values.get(key, default)


ON = dict(
    style_anime_register="strong",
    style_group_register_cap="light",
    style_cute_cap="occasional",
    style_examples_path="",
    style_downgrade_on_feedback=True,
    style_inject_proactive=True,
)


@pytest.fixture
def cfg(monkeypatch):
    fake = FakeConfig(**ON)
    monkeypatch.setattr(config_mod, "get_config", lambda: fake)
    return fake


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def _sel(clock=None, seed=7, tokens=len):
    return S.StyleSelector(clock=clock or Clock(), rng=random.Random(seed), token_counter=tokens)


def test_settings_default_off_without_config(monkeypatch):
    monkeypatch.setattr(config_mod, "get_config", lambda: FakeConfig())
    s = S.read_settings()
    assert not s.enabled and not s.inject_proactive
    assert _sel().build_passive("p1", "今天外面风好大", is_group=False) == ""
    assert _sel().build_proactive("private:1", is_group=False) == ""
    assert not S.style_enabled() and not S.proactive_style_enabled()


def test_settings_exception_is_off(monkeypatch):
    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(config_mod, "get_config", boom)
    assert not S.read_settings().enabled


def test_real_hidden_defaults(tmp_path):
    from astrbot_plugin_iris_memory.iris_memory.config import init_config
    from astrbot_plugin_iris_memory.iris_memory.config.config import reset_config

    init_config({}, tmp_path)
    try:
        s = S.read_settings()
        assert s.register_cap == R.REG_MEDIUM
        assert s.group_cap == R.REG_LIGHT
        assert s.cute_cap == R.CUTE_OCCASIONAL
        assert s.inject_proactive and s.downgrade_on_feedback and s.examples_path == ""
    finally:
        reset_config()


def test_private_chat_render(cfg):
    meta = {}
    text = _sel().build_passive("p1", "今天外面风好大", is_group=False, meta=meta)
    assert text.startswith("## 本轮口吻\n场景：私聊闲聊。口吻：浓。")
    assert "卖萌：偶尔" in text
    assert "参考（只学语气，不照抄内容和句式）：" in text
    assert not re.search(r"\d", text)
    assert meta["scene"] == "chat" and meta["register"] == "strong" and meta["cute"] == "occasional"
    assert 1 <= len(meta["examples"]) <= 2


def test_register_cap_medium(cfg):
    cfg.values["style_anime_register"] = "medium"
    meta = {}
    text = _sel().build_passive("p1", "今天外面风好大", is_group=False, meta=meta)
    assert "口吻：中。" in text and meta["register"] == "medium"
    lib = {e.id: e for e in EX.load_examples("")}
    assert all(lib[i].register <= R.REG_MEDIUM for i in meta["examples"])


def test_light_scene_one_example_no_cute(cfg):
    meta = {}
    text = _sel().build_passive("p1", "今天被领导当众骂了，好难受", is_group=False, meta=meta)
    assert "口吻：淡。" in text and "卖萌：这一轮不卖萌。" in text
    assert "先回应那件具体的事" in text
    assert len(meta["examples"]) <= 1


def test_group_capped_light_and_off(cfg):
    meta = {}
    text = _sel().build_passive("g1", "哈哈哈笑死", is_group=True, meta=meta)
    assert "口吻：淡。" in text and meta["cute"] == "off"
    lib = {e.id: e for e in EX.load_examples("")}
    assert all("group" in lib[i].channels and lib[i].register == R.REG_LIGHT for i in meta["examples"])
    cfg.values["style_group_register_cap"] = "medium"
    meta = {}
    _sel().build_passive("g1", "哈哈哈笑死", is_group=True, meta=meta)
    assert meta["register"] == "medium" and meta["cute"] == "rare"
    cfg.values["style_group_register_cap"] = "off"
    meta = {}
    assert _sel().build_passive("g1", "哈哈哈笑死", is_group=True, meta=meta) == ""
    assert meta["skipped"] == "register_off"
    # 私聊不受群上限影响
    assert "口吻：浓。" in _sel().build_passive("p1", "今天外面风好大", is_group=False)


def test_cooldown_no_repeat_across_consecutive_rounds(cfg):
    sel = _sel()
    seen = []
    for _ in range(3):
        meta = {}
        sel.build_passive("p1", "今天外面风好大", is_group=False, meta=meta)
        seen.append(set(meta["examples"]))
    assert not (seen[0] & seen[1]) and not (seen[1] & seen[2])
    # 冷却按会话分开
    meta = {}
    _sel(seed=7).build_passive("p2", "今天外面风好大", is_group=False, meta=meta)
    assert set(meta["examples"]) == seen[0]


def test_no_match_injects_description_only(cfg):
    meta = {}
    text = _sel().build_passive("p1", "你是不是在模仿哪个动漫角色？", is_group=False, meta=meta)
    assert meta["examples"] == [] and "参考" not in text
    assert "不扮演" in text


def test_cute_examples_only_when_occasional(cfg):
    lib = EX.load_examples("")
    cute_ids = {e.id for e in lib if e.cute}
    assert cute_ids
    cfg.values["style_cute_cap"] = "rare"
    for seed in range(10):
        meta = {}
        _sel(seed=seed).build_passive("p1", "你好聪明啊", is_group=False, meta=meta)
        assert meta["cute"] == "rare"
        assert not (set(meta["examples"]) & cute_ids)


def test_cute_cap_off(cfg):
    cfg.values["style_cute_cap"] = "off"
    text = _sel().build_passive("p1", "你好聪明啊", is_group=False)
    assert "卖萌：这一轮不卖萌。" in text


def test_occasional_not_twice_in_a_row(cfg):
    sel = _sel()
    meta = {}
    sel.build_passive("p1", "你好聪明啊", is_group=False, meta=meta)
    assert meta["cute"] == "occasional"
    sel.observe_reply("p1", "真的？那你再多夸两句，我还挺爱听的。")  # CUTE-PROUD
    meta = {}
    text = sel.build_passive("p1", "今天外面风好大", is_group=False, meta=meta)
    assert meta["cute"] == "off" and "卖萌：这一轮不卖萌。" in text
    sel.observe_reply("p1", "风大就别出门了，在家待着吧。")
    meta = {}
    sel.build_passive("p1", "今天外面风好大", is_group=False, meta=meta)
    assert meta["cute"] == "occasional"
    # 最近一段回复里卖萌占到一半 → 收一档
    sel.observe_reply("p1", "才没有啦，我可没偷懒。")  # CUTE-INSIST
    sel.observe_reply("p1", "嗯，窗户关好。")
    meta = {}
    sel.build_passive("p1", "今天外面风好大", is_group=False, meta=meta)
    assert meta["cute"] == "off"
    sel.observe_reply("p1", "那就窝着看会儿书。")
    sel.observe_reply("p1", "好。")
    meta = {}
    sel.build_passive("p1", "今天外面风好大", is_group=False, meta=meta)
    assert meta["cute"] == "occasional"


def test_rare_once_per_segment_and_segment_reset(cfg):
    clock = Clock()
    sel = _sel(clock=clock)
    meta = {}
    sel.build_passive("p1", "我抢到票了！", is_group=False, meta=meta)
    assert meta["cute"] == "rare"
    sel.observe_reply("p1", "被你发现了，我就是很开心。")
    sel.observe_reply("p1", "好啊。")
    clock.now += 60
    meta = {}
    sel.build_passive("p1", "我抢到票了！", is_group=False, meta=meta)
    assert meta["cute"] == "off"
    clock.now += 31 * 60  # 新的一段对话
    meta = {}
    sel.build_passive("p1", "我抢到票了！", is_group=False, meta=meta)
    assert meta["cute"] == "rare"


def test_feedback_sticky_until_segment_end(cfg):
    clock = Clock()
    sel = _sel(clock=clock)
    meta = {}
    text = sel.build_passive("p1", "你又在装可爱了", is_group=False, meta=meta)
    assert meta["scene"] == "feedback" and meta["register"] == "light"
    assert "不辩解" in text
    clock.now += 60
    meta = {}
    text = sel.build_passive("p1", "不过挺好玩的", is_group=False, meta=meta)
    assert meta["register"] == "light" and meta["cute"] == "off" and meta["sticky"] is True
    assert "保持正常说话" in text
    clock.now += 31 * 60
    meta = {}
    sel.build_passive("p1", "不过挺好玩的", is_group=False, meta=meta)
    assert meta["register"] == "strong" and meta["sticky"] is False


def test_feedback_downgrade_switch_off(cfg):
    cfg.values["style_downgrade_on_feedback"] = False
    sel = _sel()
    sel.build_passive("p1", "你又在装可爱了", is_group=False)
    meta = {}
    sel.build_passive("p1", "不过挺好玩的", is_group=False, meta=meta)
    assert meta["register"] == "strong" and meta["sticky"] is False


def test_proactive_fixed_medium(cfg):
    sel = _sel()
    text = sel.build_proactive("private:1", is_group=False)
    assert "场景：你主动找对方开口。口吻：中。" in text
    assert "卖萌：很少" in text
    assert "/ 回复：" in text
    assert not re.search(r"\d", text)
    # 群聊主动再取群上限
    assert "口吻：淡。" in sel.build_proactive("-100123", is_group=True)
    cfg.values["style_group_register_cap"] = "off"
    assert sel.build_proactive("-100123", is_group=True) == ""
    cfg.values["style_cute_cap"] = "off"
    assert "卖萌：这一轮不卖萌。" in sel.build_proactive("private:1", is_group=False)


def test_proactive_switches(cfg):
    cfg.values["style_inject_proactive"] = False
    assert _sel().build_proactive("private:1", is_group=False) == ""
    assert not S.proactive_style_enabled()
    cfg.values["style_inject_proactive"] = True
    cfg.values["style_anime_register"] = "off"
    assert _sel().build_proactive("private:1", is_group=False) == ""
    assert not S.proactive_style_enabled()


def test_budget_trim():
    sel = _sel(tokens=len)
    lib = EX.load_examples("")
    ex = [e for e in lib if e.scene == "chat" and e.register == R.REG_STRONG][:2]
    plan = S.StylePlan(scene="chat", label="私聊闲聊", register=R.REG_STRONG, cute=R.CUTE_OFF,
                       examples=tuple(ex), anti=ex[0], sticky=False)
    full = sel.render(plan)
    assert "别这样" in full
    S_MAX = S._STYLE_MAX_TOKENS
    try:
        S._STYLE_MAX_TOKENS = len(full) - 1
        trimmed = sel.render(plan)
        assert "别这样" not in trimmed and trimmed.count("/ 回复：") == 2
        S._STYLE_MAX_TOKENS = 10
        tiny = sel.render(plan)
        assert "参考" not in tiny and tiny.startswith("## 本轮口吻")
    finally:
        S._STYLE_MAX_TOKENS = S_MAX


def test_render_all_scenes_no_digits_and_numeric_guard_keeps(cfg, monkeypatch):
    monkeypatch.setattr(numeric, "guard_enabled", lambda: True)
    for scene in R._SCENE_TABLE:
        for group in (False, True):
            sel = _sel()
            plan = sel.plan("s", "", is_group=group, settings=S.read_settings(), scene=scene,
                            channel="group" if group else "private")
            if plan is None:
                continue
            text = sel.render(plan)
            assert not re.search(r"\d", text), scene
            assert numeric.guard_text(text, source="style") == text


def test_breaker_and_failure_isolation(cfg, monkeypatch):
    clock = Clock()
    sel = _sel(clock=clock)

    def boom(_path=""):
        raise RuntimeError("disk")

    monkeypatch.setattr(EX, "load_examples", boom)
    meta = {}
    assert sel.build_passive("p1", "今天外面风好大", is_group=False, meta=meta) == ""
    assert meta["error"] == "RuntimeError"
    for _ in range(4):
        sel.build_passive("p1", "今天外面风好大", is_group=False)
    meta = {}
    monkeypatch.undo()
    monkeypatch.setattr(config_mod, "get_config", lambda: cfg)
    assert sel.build_passive("p1", "今天外面风好大", is_group=False, meta=meta) == ""
    assert meta["skipped"] == "paused"
    assert sel.build_proactive("private:1", is_group=False) == ""
    clock.now += 3601
    assert sel.build_passive("p1", "今天外面风好大", is_group=False) != ""
    # 主动入口同样隔离
    monkeypatch.setattr(EX, "load_examples", boom)
    assert sel.build_proactive("private:1", is_group=False) == ""


def test_empty_scope_and_observe_noop(cfg):
    sel = _sel()
    assert sel.build_passive("", "hi", is_group=False) == ""
    sel.observe_reply("", "x")
    sel.observe_reply("p1", "")
    sel.reset()


def test_scope_lru_bounded(cfg):
    sel = _sel()
    for i in range(S._MAX_SCOPES + 10):
        sel.observe_reply(f"s{i}", "好。")
    assert len(sel._scopes) == S._MAX_SCOPES


def test_event_helpers(cfg, monkeypatch):
    from types import SimpleNamespace
    import astrbot_plugin_iris_memory.iris_memory.platform as platform_mod

    adapter = SimpleNamespace(get_group_id=lambda e: "", get_session_id=lambda e: "sess")
    monkeypatch.setattr(platform_mod, "get_adapter", lambda e: adapter)
    assert S.scope_key(object()) == "sess" and S.is_group_event(object()) is False
    S.get_selector().reset()
    S.observe_event_reply(object(), "被你发现了。")
    assert list(S.get_selector()._scopes["sess"].cute_flags) == [True]
    cfg.values["style_anime_register"] = "off"
    S.observe_event_reply(object(), "被你发现了。")
    assert list(S.get_selector()._scopes["sess"].cute_flags) == [True]

    def boom(e):
        raise RuntimeError

    monkeypatch.setattr(platform_mod, "get_adapter", boom)
    assert S.scope_key(object()) == "" and S.is_group_event(object()) is False
    S.get_selector().reset()
