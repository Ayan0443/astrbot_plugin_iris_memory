"""iris_memory.identity：名字 / 主人称呼 / 参照角色文件全部来自配置。"""
from __future__ import annotations

import json
import re

from astrbot_plugin_iris_memory.iris_memory import identity as I


def _fresh():
    I.reset()


def test_empty_identity_placeholders_never_match_but_other_branches_do():
    _fresh()
    rx = I.lazy_re(r"你|{BOT}", re.IGNORECASE)
    assert rx.search("你好")
    assert not rx.search("小白在吗")
    assert I.owner_display() == "主人"
    assert I.owner_names_phrase() == ""
    assert I.reference() == {}
    assert I.work_pattern() == ""
    assert not I.lazy_re("{WORK}").search("任何作品")


def test_names_from_config_getter_and_hot_change():
    _fresh()
    cfg = {"identity_bot_names": "小白, xiaobai，小白", "identity_owner_names": "阿明,小明"}
    I.set_source(lambda k, d="": cfg.get(k, d))
    assert I.bot_names() == ("小白", "xiaobai")  # 去空白、全角逗号、去重
    assert I.owner_display() == "阿明"
    assert I.owner_names_phrase() == "阿明 或 小明"
    rx = I.lazy_re(r"^{BOT}觉得", re.IGNORECASE)
    assert rx.search("XiaoBai觉得行")
    cfg["identity_bot_names"] = "阿白"
    assert rx.search("阿白觉得行") and not rx.search("小白觉得行")
    _fresh()


def test_names_are_regex_escaped():
    _fresh()
    I.configure(bot_names="a.b,c+d")
    rx = I.lazy_re(r"^{BOT}$")
    assert rx.search("a.b") and rx.search("c+d")
    assert not rx.search("axb") and not rx.search("ccd")
    _fresh()


def test_configure_overrides_config():
    _fresh()
    I.set_source(lambda k, d="": {"identity_owner_names": "配置里的"}.get(k, d))
    I.configure(owner_names="覆盖的")
    assert I.owner_display() == "覆盖的"
    I.configure(owner_names=None)
    assert I.owner_display() == "配置里的"
    _fresh()


def test_reference_file_relative_to_data_dir_and_bad_rows_skipped(tmp_path):
    _fresh()
    ref = {
        "work_mention": "星海物语|star\\s*sea",
        "copy_hard": [["R-H1", "啾比啾", False], ["R-BAD", "(unclosed", False], ["R-H2", "Hoshi", True], "not-a-row"],
        "copy_soft": [["R-S1", "星轨"]],
        "reviewer_note": "尤其是星海物语的口癖。",
    }
    (tmp_path / "ref.json").write_text(json.dumps(ref, ensure_ascii=False), encoding="utf-8")
    I.set_source(lambda k, d="": {"persona_reference_file": "ref.json"}.get(k, d), tmp_path)
    assert [r[0] for r in I.reference_rows("copy_hard")] == ["R-H1", "R-H2"]
    assert I.reference_rows("copy_hard")[1][2] == re.IGNORECASE
    assert I.reference_rows("copy_soft") == (("R-S1", "星轨", 0),)
    assert I.reference_text("reviewer_note") == "尤其是星海物语的口癖。"
    assert I.lazy_re("{WORK}", re.IGNORECASE).search("在补 Star Sea")

    rules = I.LazyRules((("BASE", "人家", 0),), pattern_index=1, flags_index=2, drop_flags=True, reference_kind="copy_hard")
    ids = [rid for rid, rx in rules if rx.search("人家啾比啾 hoshi")]
    assert ids == ["BASE", "R-H1", "R-H2"]
    assert len(rules) == 3
    assert [r[0] for r in rules + (("X", None),)] == ["BASE", "R-H1", "R-H2", "X"]
    _fresh()


def test_reference_file_missing_or_broken_falls_back_to_empty(tmp_path):
    _fresh()
    I.set_source(lambda k, d="": {"persona_reference_file": "nope.json"}.get(k, d), tmp_path)
    assert I.reference() == {}
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    I.set_source(lambda k, d="": {"persona_reference_file": "bad.json"}.get(k, d), tmp_path)
    assert I.reference() == {}
    assert I.reference_rows("copy_hard") == ()
    _fresh()


def test_reference_reloads_when_file_changes(tmp_path):
    import os
    import time

    _fresh()
    p = tmp_path / "ref.json"
    p.write_text(json.dumps({"work_mention": "甲"}), encoding="utf-8")
    I.set_source(lambda k, d="": {"persona_reference_file": str(p)}.get(k, d))
    assert I.work_pattern() == "甲"
    p.write_text(json.dumps({"work_mention": "乙"}), encoding="utf-8")
    later = time.time() + 5
    os.utime(p, (later, later))
    assert I.work_pattern() == "乙"
    _fresh()


def test_style_guard_uses_identity_names():
    from astrbot_plugin_iris_memory.iris_memory.lifelike.style.guard import rule_tags, scan

    _fresh()
    I.configure(bot_names="阿白", owner_names="阿明")
    assert "COPY-H04" in rule_tags(scan("阿白觉得可以"))
    assert "COPY-S17" in rule_tags(scan("阿明～"))
    I.configure(bot_names="", owner_names="")
    assert "COPY-H04" not in rule_tags(scan("阿白觉得可以"))
    _fresh()


def test_reviewer_appends_reference_note():
    from astrbot_plugin_iris_memory.iris_memory.learning import reviewer

    src = open(reviewer.__file__, encoding="utf-8").read()
    assert "《" not in src  # 审查标准里不写死任何作品名；作品相关说明来自 reviewer_note
