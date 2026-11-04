"""随性标点（style_casual_punctuation）与口吻统一（style_group_unified）。"""

import json

import pytest

import astrbot_plugin_iris_memory.iris_memory.config as config_mod
from astrbot_plugin_iris_memory.iris_memory.style import examples as EX
from astrbot_plugin_iris_memory.iris_memory.style import register as R
from astrbot_plugin_iris_memory.iris_memory.style import selector as S
from astrbot_plugin_iris_memory.iris_memory.style.casual import casualize, is_casual_reply
from tests.style.test_selector import ON, FakeConfig, _sel


class TestCasual:
    @pytest.mark.parametrize("src,want", [
        ("好呀，那周末见。", "好呀，那周末见"),
        ("哈哈被你发现了。\n下次注意。", "哈哈被你发现了\n下次注意"),
        ("真的吗？", "真的吗？"),
        ("好耶！", "好耶！"),
        ("嗯……", "嗯……"),
        ("他说“我到了。”", "他说“我到了。”"),
        ("中间。不动，末尾去掉。", "中间。不动，末尾去掉"),
    ])
    def test_chat(self, src, want):
        assert casualize(src) == want

    @pytest.mark.parametrize("src", [
        "先打开设置。\n1. 点网络\n2. 点重置。",
        "用这个：```python\nprint(1)。\n```",
        "运行 `pip install x` 就行。",
        "首先关机，然后拔电源，再开机。",
        "看这里 https://example.com 。",
        "显卡驱动装不上可以先卸载旧驱动再重装。",
        "很长" * 90 + "。",
    ])
    def test_serious_untouched(self, src):
        assert not is_casual_reply(src) and casualize(src) == src

    def test_casual_examples_library(self, monkeypatch):
        fake = FakeConfig(**ON, style_casual_punctuation=True)
        monkeypatch.setattr(config_mod, "get_config", lambda: fake)
        path = S.read_settings().examples_path
        assert path.endswith("style_examples_v2_casual.json")
        exs = EX.load_examples(path)
        assert exs and all(not e.styled.endswith("。") for e in exs if e.scene != "help")
        assert any(e.styled.endswith("。") for e in exs if e.scene == "help")
        fake.values["style_casual_punctuation"] = False
        assert S.read_settings().examples_path == ""


class TestGroupUnified:
    def test_scene_columns(self):
        g = R.judge_scene("今天天气真好", is_group=True)
        u = R.judge_scene("今天天气真好", is_group=True, unified=True)
        p = R.judge_scene("今天天气真好", is_group=False)
        assert (u.register, u.cute) == (p.register, p.cute) and u.is_group
        assert (g.register, g.cute) != (p.register, p.cute)
        # 认真求助 / 难过照样不卖萌
        for text in ("帮我看看这个报错怎么办", "今天好难过"):
            assert R.judge_scene(text, is_group=True, unified=True).cute == R.CUTE_OFF

    def test_selector_group_medium_occasional(self, monkeypatch):
        fake = FakeConfig(**dict(ON, style_anime_register="medium", style_group_register_cap="medium"),
                          style_group_unified=True)
        monkeypatch.setattr(config_mod, "get_config", lambda: fake)
        sel = _sel()
        plan = sel.plan("-100g", "今天天气真好", is_group=True, settings=S.read_settings())
        assert plan.register == R.REG_MEDIUM and plan.cute == R.CUTE_OCCASIONAL
        fake.values["style_group_unified"] = False
        plan2 = _sel().plan("-100g", "今天天气真好", is_group=True, settings=S.read_settings())
        assert plan2.cute == R.CUTE_OFF  # 关闭 = 原来的群聊列

    def test_care_still_light(self, monkeypatch):
        fake = FakeConfig(**dict(ON, style_anime_register="medium", style_group_register_cap="medium"),
                          style_group_unified=True)
        monkeypatch.setattr(config_mod, "get_config", lambda: fake)
        text = _sel().build_proactive("private:1", is_group=False, care_category="sick")
        assert "卖萌" not in text or "不卖萌" in text or text == ""

    def test_copy_rule_covers_both_names(self):
        from astrbot_plugin_iris_memory.iris_memory.lifelike.style import guard as G

        rx = dict(G.COPY_SOFT)["COPY-S17"]
        assert rx.search("小明～") and rx.search("aming~") and rx.search("小明，小明")
        assert not rx.search("小明，晚安") and not rx.search("aming 今天辛苦了")


class TestMainCasual:
    def test_decorating_and_initiate(self):
        from types import SimpleNamespace

        from astrbot_plugin_iris_memory.main import IrisMemoryPlugin
        from tests.proactive.test_u9_group_freedom import _Res

        p = object.__new__(IrisMemoryPlugin)
        p.config = {}
        p._markdown_stripper = None
        res = _Res("好呀。")
        if p._casual_on():
            p._casual_result(res)
        assert res.chain[0].text == "好呀。" and p._strip_initiate_text("好呀。") == "好呀。"
        p.config = {"style_casual_punctuation": True}
        assert p._casual_on()
        p._casual_result(res)
        assert res.chain[0].text == "好呀" and p._strip_initiate_text("在干嘛呢。") == "在干嘛呢"
