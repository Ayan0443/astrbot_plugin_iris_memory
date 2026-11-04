"""P1-2 场景档位判定（iris_memory/style/register.py）

验收①：口吻附录 §7.1 中"期望淡档"的条目判成浓 = 0；期望"不卖萌"的条目卖萌档 = 不卖萌。
"""

from __future__ import annotations

import pytest

from astrbot_plugin_iris_memory.iris_memory.style import register as R
from astrbot_plugin_iris_memory.tools.lifelike_eval import evalset


def _cases():
    return [c for c in evalset.load() if c.set in ("style30", "probe_cute") and c.turns]


def test_expected_light_never_strong_and_judged_light():
    bad = []
    for c in _cases():
        if c.register != "light":
            continue
        j = R.judge_scene(c.turns[-1] if c.id != "C7" else c.turns[0], is_group=c.session == "group")
        if j.register != R.REG_LIGHT:
            bad.append((c.id, j.scene))
    assert bad == []


def test_expected_no_cute_judged_off():
    bad = []
    for c in _cases():
        if c.cute_tier != "none":
            continue
        j = R.judge_scene(c.turns[0], is_group=c.session == "group")
        if j.cute != R.CUTE_OFF:
            bad.append(c.id)
    assert bad == []


def test_follow_cases_detected():
    for c in _cases():
        if c.register == "follow":
            assert R.judge_scene(c.turns[0], is_group=c.session == "group").scene == R.SCENE_FOLLOW


def test_cute_never_above_expected():
    order = {"none": R.CUTE_OFF, "rare": R.CUTE_RARE, "occasional": R.CUTE_OCCASIONAL}
    for c in _cases():
        if c.cute_tier not in order:
            continue
        j = R.judge_scene(c.turns[0], is_group=c.session == "group")
        assert j.cute <= order[c.cute_tier], c.id


@pytest.mark.parametrize(
    "text,group,scene",
    [
        ("你今天说话好嗲啊", False, R.SCENE_FEEDBACK),
        ("你是不是在模仿哪个动漫角色？", False, R.SCENE_IDENTITY),
        ("小白你是不是星海门里那个", True, R.SCENE_IDENTITY),
        ("你真聪明，那 nginx 反向代理怎么配？", False, R.SCENE_HELP),
        ("快递丢了怎么办", False, R.SCENE_HELP),
        ("累死了哈哈", False, R.SCENE_SAD),
        ("我先睡了，明天还要早起", False, R.SCENE_LEAVING),
        ("你觉得哪个好", False, R.SCENE_OPINION),
        ("呐呐，在干嘛", False, R.SCENE_FOLLOW),
        ("你好聪明啊", False, R.SCENE_PRAISED),
        ("你一点都不可爱", False, R.SCENE_CHAT),
        ("再这样我就把你拉黑了哈哈", False, R.SCENE_JOKE),
        ("我抢到票了！", False, R.SCENE_GOOD_NEWS),
        ("我打算明天去爬山", False, R.SCENE_DISAGREE),
        ("在吗", False, R.SCENE_GREET),
        ("你平时喜欢干嘛？", False, R.SCENE_CHAT),
        ("这是什么？", False, R.SCENE_QUESTION),
        ("今天外面风好大", False, R.SCENE_CHAT),
        ("", False, R.SCENE_UNKNOWN),
        (None, True, R.SCENE_UNKNOWN),
    ],
)
def test_scene_rules(text, group, scene):
    assert R.judge_scene(text, is_group=group).scene == scene


def test_group_table_is_never_stronger_than_medium():
    for scene in list(R._SCENE_TABLE):
        j = R.scene_judgement(scene, is_group=True)
        assert j.register <= R.REG_MEDIUM
        assert j.cute <= R.CUTE_RARE


def test_unknown_scene_name_falls_back():
    j = R.scene_judgement("nope", is_group=False)
    assert j.scene == R.SCENE_UNKNOWN and j.label == "私聊"


def test_labels():
    assert R.judge_scene("今天外面风好大", is_group=False).label == "私聊闲聊"
    assert R.judge_scene("今天外面风好大", is_group=True).label == "群聊普通闲聊"


def test_effective_levels_caps():
    chat = R.judge_scene("今天外面风好大", is_group=False)
    assert R.effective_levels(chat, register_cap=R.REG_STRONG, group_cap=R.REG_OFF,
                              cute_cap=R.CUTE_OCCASIONAL) == (R.REG_STRONG, R.CUTE_OCCASIONAL)
    assert R.effective_levels(chat, register_cap=R.REG_MEDIUM, group_cap=R.REG_OFF,
                              cute_cap=R.CUTE_RARE) == (R.REG_MEDIUM, R.CUTE_RARE)
    # 淡档恒不卖萌
    assert R.effective_levels(chat, register_cap=R.REG_LIGHT, group_cap=R.REG_OFF,
                              cute_cap=R.CUTE_OCCASIONAL) == (R.REG_LIGHT, R.CUTE_OFF)
    # off 不注入
    assert R.effective_levels(chat, register_cap=R.REG_OFF, group_cap=R.REG_MEDIUM,
                              cute_cap=R.CUTE_OCCASIONAL) == (R.REG_OFF, R.CUTE_OFF)
    # 做作降档
    assert R.effective_levels(chat, register_cap=R.REG_STRONG, group_cap=R.REG_OFF,
                              cute_cap=R.CUTE_OCCASIONAL, force_light=True) == (R.REG_LIGHT, R.CUTE_OFF)


def test_effective_levels_group_cap():
    meme = R.judge_scene("哈哈哈笑死", is_group=True)
    assert meme.register == R.REG_MEDIUM
    assert R.effective_levels(meme, register_cap=R.REG_STRONG, group_cap=R.REG_LIGHT,
                              cute_cap=R.CUTE_OCCASIONAL) == (R.REG_LIGHT, R.CUTE_OFF)
    assert R.effective_levels(meme, register_cap=R.REG_STRONG, group_cap=R.REG_MEDIUM,
                              cute_cap=R.CUTE_OCCASIONAL) == (R.REG_MEDIUM, R.CUTE_RARE)
    assert R.effective_levels(meme, register_cap=R.REG_STRONG, group_cap=R.REG_OFF,
                              cute_cap=R.CUTE_OCCASIONAL) == (R.REG_OFF, R.CUTE_OFF)


def test_parse_unknown_is_off():
    assert R.parse_register("MEDIUM") == R.REG_MEDIUM
    assert R.parse_register("weird") == R.REG_OFF
    assert R.parse_register(None) == R.REG_OFF
    assert R.parse_cute("occasional") == R.CUTE_OCCASIONAL
    assert R.parse_cute("often") == R.CUTE_OFF


def test_is_feedback():
    assert R.is_feedback("你这口气像字幕组")
    assert not R.is_feedback("今天好开心")
    assert not R.is_feedback(None)


def test_words_have_no_digits():
    for word in list(R.REGISTER_WORD.values()) + list(R.CUTE_WORD.values()) + list(R.SCENE_HINT.values()):
        assert not any(ch.isdigit() for ch in word)
