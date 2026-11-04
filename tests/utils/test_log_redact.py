"""活人感 P0-3：日志去正文过滤器（lifelike_log_redact）。"""

from __future__ import annotations

import logging

import pytest
from astrbot_plugin_iris_memory.iris_memory.config.defaults import HiddenConfig
from astrbot_plugin_iris_memory.iris_memory.utils import log_redact as lr

SECRET = "今天加班到十点好累"


def _rec(msg, args=(), level=logging.INFO, **extra) -> logging.LogRecord:
    r = logging.LogRecord("astrbot", level, __file__, 1, msg, args, None)
    for k, v in extra.items():
        setattr(r, k, v)
    return r


def _telethon(text=SECRET, level=logging.INFO) -> logging.LogRecord:
    return _rec(
        "[Telethon] Committing AstrBot event: session_id=%s type=%s sender=%s text=%r",
        ("123456789", "FriendMessage", "123456789", text),
        level=level,
    )


def _apply(record, enabled=True) -> str:
    lr.ChatTextRedactFilter(lambda: enabled).filter(record)
    return record.getMessage()


def test_hidden_config_defaults():
    h = HiddenConfig()
    assert h.lifelike_log_redact is True
    assert h.lifelike_retention_days_observed == 30
    assert h.lifelike_retention_dry_run is True


def test_telethon_inbound_redacted():
    out = _apply(_telethon())
    assert SECRET not in out
    assert "text_len=9" in out and "session_id=123456789" in out and "type=FriendMessage" in out


def test_telethon_none_text():
    out = _apply(_telethon(text=None))
    assert "text_len=0" in out


def test_event_bus_inbound_with_name():
    r = _rec(f"[default] [userbot(telethon)] owner/123456789: {SECRET}", category="user_chat")
    out = _apply(r)
    assert SECRET not in out
    assert out == "[default] [userbot(telethon)] owner/123456789: len=9"


def test_event_bus_name_with_space_and_body_slash():
    r = _rec(f"[default] [userbot(telethon)] Aya No/123456789: {SECRET} a/b: c", category="user_chat")
    out = _apply(r)
    assert SECRET not in out and "a/b" not in out
    assert out.startswith("[default] [userbot(telethon)] Aya No/123456789: len=")


def test_event_bus_inbound_without_name():
    r = _rec(f"[default] [userbot(telethon)] 123456789: {SECRET} x/y: z", category="user_chat")
    out = _apply(r)
    assert out == f"[default] [userbot(telethon)] 123456789: len={len(SECRET + ' x/y: z')}"


def test_respond_stage_outbound():
    reply = "那就早点休息嘛"
    r = _rec(f"Prepare to send - owner/123456789: {reply}", category="user_chat")
    out = _apply(r)
    assert reply not in out
    assert out == f"Prepare to send - owner/123456789: len={len(reply)}"


def test_command_name_kept_args_dropped():
    r = _rec("[default] [userbot(telethon)] owner/123456789: /iris_care off 隐私参数", category="user_chat")
    out = _apply(r)
    assert "隐私参数" not in out
    assert "cmd=/iris_care" in out


def test_unknown_user_chat_shape_fails_closed():
    r = _rec(f"weird {SECRET}", category="user_chat")
    out = _apply(r)
    assert SECRET not in out and out.startswith("[user_chat redacted] len=")


def test_user_chat_with_percent_args():
    r = _rec("Prepare to send - %s/%s: %s", ("owner", "1", SECRET), category="user_chat")
    out = _apply(r)
    assert SECRET not in out and out.endswith("len=9")


def test_other_records_untouched():
    r = _rec("[continuity] expired items=%d", (3,))
    assert _apply(r) == "[continuity] expired items=3"
    r2 = _rec(f"system text {SECRET}", category="system")
    assert _apply(r2) == f"system text {SECRET}"
    r3 = _rec(12345)
    assert _apply(r3) == "12345"


def test_debug_level_not_redacted():
    out = _apply(_telethon(level=logging.DEBUG))
    assert SECRET in out


def test_switch_off_is_passthrough():
    out = _apply(_telethon(), enabled=False)
    assert repr(SECRET) in out
    r = _rec(f"Prepare to send - a/1: {SECRET}", category="user_chat")
    assert _apply(r, enabled="false") == f"Prepare to send - a/1: {SECRET}"


def test_enabled_getter_error_defaults_on():
    def boom():
        raise RuntimeError("x")

    r = _telethon()
    lr.ChatTextRedactFilter(boom).filter(r)
    assert SECRET not in r.getMessage()


def test_redact_exception_falls_back_to_passthrough(monkeypatch):
    monkeypatch.setattr(lr, "_warned", False)

    def boom(record):
        raise ValueError("bad")

    monkeypatch.setattr(lr, "redact_record", boom)
    r = _telethon()
    assert lr.ChatTextRedactFilter(lambda: True).filter(r) is True
    assert repr(SECRET) in r.getMessage()  # 旧行为
    assert lr._warned is True


def test_install_idempotent_and_uninstall_specific():
    name = "astrbot.plugin.test_lr_dummy"
    f1 = lr.install(lambda: True, logger_names=[name], include_existing_plugins=False)
    f2 = lr.install(lambda: True, logger_names=[name], include_existing_plugins=False)
    lg = logging.getLogger(name)
    flagged = [f for f in lg.filters if getattr(f, lr._FILTER_FLAG, False)]
    assert flagged == [f2]
    assert lr.uninstall(f1) == 0  # 旧实例卸载不误删新实例的过滤器
    assert f2 in lg.filters
    assert lr.uninstall(f2) == 1
    assert not [f for f in lg.filters if getattr(f, lr._FILTER_FLAG, False)]
    assert lr.uninstall(None) == 0


def test_install_picks_existing_plugin_loggers():
    logging.getLogger("astrbot.plugin.test_lr_existing")
    f = lr.install(lambda: True, logger_names=[])
    try:
        assert "astrbot.plugin.test_lr_existing" in [lg.name for lg in f.loggers]
    finally:
        lr.uninstall(f)


def test_end_to_end_through_logger(caplog):
    name = "astrbot.plugin.test_lr_e2e"
    lg = logging.getLogger(name)
    lg.setLevel(logging.DEBUG)
    f = lr.install(lambda: True, logger_names=[name], include_existing_plugins=False)
    try:
        with caplog.at_level(logging.DEBUG, logger=name):
            lg.info(
                "[Telethon] Committing AstrBot event: session_id=%s type=%s sender=%s text=%r",
                "1", "FriendMessage", "1", SECRET,
            )
            lg.info(f"Prepare to send - a/1: {SECRET}", extra={"category": "user_chat"})
        assert caplog.records
        assert all(SECRET not in rec.getMessage() for rec in caplog.records)
    finally:
        lr.uninstall(f)


@pytest.mark.parametrize("val,expected", [(True, True), ("off", False), ("1", True), (None, True), ("??", True), (0, False)])
def test_to_bool(val, expected):
    assert lr._to_bool(val, True) is expected


def test_text_for_log(monkeypatch):
    lr.set_flag_getter(lambda: True)
    try:
        assert lr.text_for_log(SECRET) == "len=9"
        lr.set_flag_getter(lambda: False)
        assert lr.text_for_log(SECRET * 10, 50) == (SECRET * 10)[:50]
        lr.set_flag_getter(lambda: (_ for _ in ()).throw(RuntimeError()))
        assert lr.text_for_log(SECRET) == "len=9"
    finally:
        lr.set_flag_getter(None)


# ---------------------------------------------------------------- 隐私审查 #2：Iris 自身 INFO/WARNING 不直插原文

_PLUGIN_ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]

# (文件, 必须出现的去正文写法, 不应再出现的原文写法)
_REDACTED_SITES = [
    ("iris_memory/proactive/parser.py", "text_for_log(text, 300)", "raw text: %.300s\", text)"),
    ("iris_memory/profile/analyzer.py", "text_for_log(response, 100)", "{response[:100]}"),
    ("iris_memory/profile/user_profile.py", "text_for_log(topic, len(str(topic)))", "-> {topic}\")"),
    ("iris_memory/profile/group_profile.py", "text_for_log(topic, len(str(topic)))", "-> {topic}\")"),
    ("iris_memory/web/routes/memory.py", "text_for_log(query, 20)", "{query[:20]}"),
    ("iris_memory/proactive/proactive.py", "text_for_log(decision.why_now, 100)", "decision.why_now, decision.topic,"),
]


@pytest.mark.parametrize("rel,must,forbidden", _REDACTED_SITES)
def test_known_content_log_sites_use_text_for_log(rel, must, forbidden):
    src = (_PLUGIN_ROOT / rel).read_text(encoding="utf-8")
    assert must in src
    assert forbidden not in src


def test_no_new_raw_content_in_info_logs():
    """禁止新增 logger.info/warning 直接插入常见正文变量（text/content/message/query/topic/response）。"""
    import re

    pat = re.compile(
        r"logger\.(?:info|warning)\(\s*f?[\"'][^\"'\n]*\{(?:text|content|message|query|topic|response|raw_text)"
        r"(?:\[[^\]]*\])?\}"
    )
    # 基线已存在、且内容不是聊天正文的行（系统状态文案）
    allow = {"iris_memory/persona_evolution/service.py"}
    offenders = []
    for path in (_PLUGIN_ROOT / "iris_memory").rglob("*.py"):
        if str(path.relative_to(_PLUGIN_ROOT)) in allow:
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pat.search(line):
                offenders.append(f"{path.relative_to(_PLUGIN_ROOT)}:{i}")
    assert offenders == []


@pytest.mark.asyncio
async def test_terminate_uninstalls_filter_even_on_error():
    """部署审查 L5：terminate 中途抛异常也卸载日志过滤器。"""
    from astrbot_plugin_iris_memory.main import IrisMemoryPlugin

    p = object.__new__(IrisMemoryPlugin)
    calls = []

    async def boom():
        raise RuntimeError("shutdown failed")

    p._terminate_inner = boom
    p._uninstall_log_redact = lambda: calls.append(1)
    with pytest.raises(RuntimeError):
        await p.terminate()
    assert calls == [1]


def test_stt_result_redacted():
    r = _rec(f"Speech-to-text result: {SECRET} a: b")
    assert lr.redact_record(r) is True
    out = r.getMessage()
    assert SECRET not in out and out.startswith("Speech-to-text result: len=")


def test_stt_referenced_result_redacted():
    r = _rec(f"Speech-to-text (referenced message) result: {SECRET}")
    assert lr.redact_record(r) is True
    out = r.getMessage()
    assert SECRET not in out and out == f"Speech-to-text (referenced message) result: len={len(SECRET)}"


def test_stt_failure_line_untouched():
    r = _rec("Speech-to-text failed: timeout")
    assert lr.redact_record(r) is False
    assert r.getMessage() == "Speech-to-text failed: timeout"


def test_stt_result_through_filter_on_core_logger():
    filt = lr.ChatTextRedactFilter(lambda: True)
    r = _rec(f"Speech-to-text result: {SECRET}")
    assert filt.filter(r) is True
    assert SECRET not in r.getMessage()
