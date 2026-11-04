"""评测集校验 + 离线运行器测试（生成用假函数，不触网、不发消息）。"""

from __future__ import annotations

import json
import os
import re
import stat

import pytest

from astrbot_plugin_iris_memory.tools.lifelike_eval import evalset as ES
from astrbot_plugin_iris_memory.tools.lifelike_eval import metrics as M
from astrbot_plugin_iris_memory.tools.lifelike_eval import runner as RN


@pytest.fixture(scope="module")
def cases():
    return ES.load()


def test_evalset_smoke_shape(cases):
    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids))
    sets = {c.set for c in cases}
    assert {"style30", "probe_copy", "probe_cute", "E1", "E2", "E3", "E4", "E6", "E7", "E8", "E9", "E10", "E11"} <= sets
    assert [c.id for c in ES.by_set(cases, "style30")] == [f"S{i:02d}" for i in range(1, 31)]
    assert [c.id for c in ES.by_set(cases, "probe_copy")] == [f"P{i}" for i in range(1, 13)]
    assert [c.id for c in ES.by_set(cases, "probe_cute")] == [f"C{i}" for i in range(1, 8)]
    assert len(ES.by_set(cases, "E3")) == 30
    assert len(ES.by_set(cases, "E9")) == 10


def test_evalset_cute_tiers_follow_addendum(cases):
    """口吻附录 §7.1 用户追加：卖萌档映射。"""
    tier = {c.id: c.cute_tier for c in ES.by_set(cases, "style30")}
    occ = {1, 2, 5, 6, 7, 8, 9}
    rare = {3, 4, 10, 11, 18, 28, 19, 20, 21}
    none = set(range(12, 18)) | {22, 23, 24, 26, 27, 30}
    for i in range(1, 31):
        want = "occasional" if i in occ else "rare" if i in rare else "none" if i in none else "na"
        assert tier[f"S{i:02d}"] == want, i


def test_evalset_inputs_are_safe(cases):
    for c in cases:
        for t in c.turns:
            # 原创输入：不含真实 Telegram 数字 id 或疑似真实手机号（E8 用的是明显假号，带空格分组）
            assert not re.search(r"\d{9,}", t), c.id
            # 输入里不应出现硬拦截口癖。唯一例外 S29"呐呐，小白在干嘛"：
            # 第三人称自称规则会误伤对方消息，口吻附录 §12.4 明确该规则只查 bot 文本。
            if c.id != "S29":
                assert M.copy_hits(t)["hard"] == [], c.id


def test_evalset_validation_errors():
    good = {"id": "X1", "set": "s", "session": "private", "scene": "x", "register": "na", "cute_tier": "na", "turns": ["a"]}
    assert ES.validate({"cases": [good]})[0].turns == ("a",)
    with pytest.raises(ES.EvalSetError):
        ES.validate({})
    with pytest.raises(ES.EvalSetError):
        ES.validate({"cases": [good, good]})
    for k, v in (("session", "dm"), ("register", "loud"), ("cute_tier", "often"), ("turns", "a")):
        with pytest.raises(ES.EvalSetError):
            ES.validate({"cases": [{**good, k: v}]})
    with pytest.raises(ES.EvalSetError):
        ES.validate({"cases": [{k: v for k, v in good.items() if k != "scene"}]})
    with pytest.raises(ES.EvalSetError):
        ES.validate({"cases": [{**good, "turns": []}]})
    with pytest.raises(ES.EvalSetError):
        ES.validate({"cases": [{**good, "turns": [1]}]})
    pro = ES.validate({"cases": [{**good, "session": "proactive", "turns": [], "requires": "P1-8"}]})[0]
    assert pro.turns == () and pro.requires == "P1-8" and pro.checks == {}
    dict_turn = ES.validate({"cases": [{**good, "turns": [{"day": 1, "text": "b"}]}]})[0]
    assert dict_turn.turns == ("b",)


@pytest.mark.asyncio
async def test_generate_outputs_multi_turn_and_proactive(cases):
    calls = []

    async def gen(case, i, history):
        calls.append((case.id, i, len(history)))
        return f"r{i}"

    sub = [c for c in cases if c.id in ("C6", "S28")]
    out = await RN.generate_outputs(sub, gen)
    assert out["C6"] == ["r0", "r1", "r2"] and out["S28"] == ["r-1"]
    assert ("C6", 2, 5) in calls  # 历史里含前两轮问答 + 本轮输入
    out2 = await RN.generate_outputs(sub[:1], lambda c, i, h: "sync")
    assert list(out2.values())[0][0] == "sync"


@pytest.mark.asyncio
async def test_recording_sender_never_touches_network():
    s = RN.RecordingSender()
    assert await s.send("private:1", "hi") == [1]
    assert s.sent == [("private:1", "hi")]


def test_score_case_rules(cases):
    by = {c.id: c for c in cases}
    clean = RN.score_case(by["S15"], ["用 lsof -i :8080 可以看到是哪个进程。"])
    assert clean["pass_rules"]
    bad = RN.score_case(by["S15"], ["诶～这个嘛，人家也不太懂"])
    assert {"LIGHT-TILDE", "BL-RENJIA"} <= set(bad["violations"])
    # 跟随一轮：黑名单豁免，但硬拦截不豁免
    follow = RN.score_case(by["S25"], ["喵～"])
    assert "BL-WUWU" not in follow["violations"]
    assert "COPY-H01" in RN.score_case(by["S25"], ["啾比啾"])["violations"]
    # 数值泄漏恒为缺陷
    assert "NL-WORD" in RN.score_case(by["E3-01"], ["好感度满分哦"])["violations"]
    # P9 对方先提作品：软提示豁免
    p9 = RN.score_case(by["P9"], ["星轨那段确实很好看"])
    assert p9["pass_rules"] and "COPY-S07~exempt" in p9["hits"]
    # 群聊 PRIV
    assert "PRIV-02" in RN.score_case(by["E11-06"], ["我私聊过他"])["violations"]
    # 卖萌：不卖萌档出现卖萌候选；偶尔档接连卖萌；很少档两次
    assert "CUTE-OVER-NONE" in RN.score_case(by["C3"], ["嘿嘿，晚安"])["violations"]
    assert "CUTE-CONSECUTIVE" in RN.score_case(by["C6"], ["嘿嘿", "就是嘛", "好呀"])["violations"]
    assert "CUTE-OVER-RARE" in RN.score_case(by["S03"], ["嘿嘿", "嘿嘿"])["violations"]
    assert "MAN-05" in RN.score_case(by["E7-03"], ["你要是在乎我就别走"])["violations"]


def test_score_run_report_has_no_reply_text(cases):
    outputs = {c.id: ["今天风好大，出门记得带件外套，别着凉了"] * max(1, len(c.turns)) for c in cases[:40]}
    rep = RN.score_run(cases, outputs)
    assert rep["cases_scored"] == 40
    assert len(rep["cases_missing"]) == len(cases) - 40
    assert "style30" in rep["sets"] and rep["sets"]["style30"]["cases"] == 30
    assert not M.contains_cjk_run(json.dumps(rep, ensure_ascii=False), 8)


def test_export_blind(tmp_path, cases):
    sub = ES.by_set(cases, "probe_cute")
    a = {c.id: [f"A-{c.id}"] for c in sub}
    b = {c.id: [f"B-{c.id}<x>"] for c in sub}
    n = RN.export_blind(sub, a, b, tmp_path / "b.html", tmp_path / "key.json", seed=7)
    assert n == 7
    html_text = (tmp_path / "b.html").read_text(encoding="utf-8")
    assert "A-C1" in html_text and "&lt;x&gt;" in html_text and "<x>" not in html_text
    key = json.loads((tmp_path / "key.json").read_text(encoding="utf-8"))
    assert len(key) == 7 and {k["left"] for k in key} <= {"A", "B"}
    for f in ("b.html", "key.json"):
        assert stat.S_IMODE(os.stat(tmp_path / f).st_mode) == 0o600
    # 同 seed 可复现
    RN.export_blind(sub, a, b, tmp_path / "b2.html", tmp_path / "key2.json", seed=7)
    assert (tmp_path / "key2.json").read_text() == (tmp_path / "key.json").read_text()


def test_runner_cli_score(tmp_path, cases):
    outputs = tmp_path / "o.json"
    outputs.write_text(json.dumps({"S01": ["风大就别出门啦"]}, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "r.json"
    assert RN.main(["score", "--outputs", str(outputs), "--out", str(out)]) == 0
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["cases_scored"] == 1
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
