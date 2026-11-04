"""只读快照导出测试：全部在 tmp_path 里造库，绝不连真实数据目录。"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from astrbot_plugin_iris_memory.tools.lifelike_eval import metrics as M
from astrbot_plugin_iris_memory.tools.lifelike_eval import snapshot as S

T0 = 1793600000.0
LONG_TEXT = "这是一段故意写得很长的原创回复正文用来检查不会被导出"


def _mk(path: Path, ddl: list[str], rows: list[tuple[str, tuple]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    for d in ddl:
        c.execute(d)
    for sql, args in rows:
        c.execute(sql, args)
    c.commit()
    c.close()


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    cdb = root / S.DB_PATHS["continuity"]
    out_rows = []
    texts = [
        "你今天吃饭了吗？",
        "好吧，那就早点睡吧。",
        LONG_TEXT,
        "诶～真的吗？",
        "人家觉得星轨不错",
    ]
    for i, t in enumerate(texts):
        out_rows.append(
            (
                "INSERT INTO outbound(scope_key,turn_id,origin,part_index,text,status,author,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                ("private:123456789", f"t{i}", "reply", 0, t, "sent", "bot", T0 + i * 60, T0 + i * 60),
            )
        )
    out_rows.append(
        (
            "INSERT INTO outbound(scope_key,turn_id,origin,part_index,text,status,author,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            ("private:123456789", "t1", "reply", 1, "第二段", "sent", "bot", T0 + 61, T0 + 61),
        )
    )
    for i in range(3):
        out_rows.append(
            (
                "INSERT INTO outbound(scope_key,turn_id,origin,part_index,text,status,author,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                ("-1001234567890", f"g{i}", "reply", 0, f"群里回复第{i}条，大家好", "sent", "bot", T0 + i, T0 + i),
            )
        )
    out_rows.append(
        (
            "INSERT INTO outbound(scope_key,turn_id,origin,part_index,text,status,author,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            ("private:123456789", "f1", "reply", 0, "失败的", "failed", "bot", T0, T0),
        )
    )
    _mk(
        cdb,
        [
            "CREATE TABLE outbound(id INTEGER PRIMARY KEY AUTOINCREMENT, scope_key TEXT NOT NULL, turn_id TEXT,"
            " origin TEXT NOT NULL, part_index INTEGER NOT NULL DEFAULT 0, text TEXT NOT NULL, status TEXT NOT NULL,"
            " author TEXT NOT NULL, tg_msg_ids TEXT NOT NULL DEFAULT '[]', reply_to_msg_id INTEGER,"
            " item_ids TEXT NOT NULL DEFAULT '[]', error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,"
            " updated_at REAL NOT NULL, sent_at REAL)",
            "CREATE TABLE inbound(scope_key TEXT NOT NULL, msg_id INTEGER NOT NULL, sender_id TEXT NOT NULL,"
            " text TEXT NOT NULL, msg_ts REAL NOT NULL, reply_to_msg_id INTEGER, received_at REAL NOT NULL,"
            " extract_status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,"
            " last_error TEXT NOT NULL DEFAULT '', PRIMARY KEY(scope_key,msg_id))",
            "CREATE TABLE items(item_id TEXT PRIMARY KEY, status TEXT, kind TEXT)",
            "CREATE TABLE proactive_turns(turn_id TEXT PRIMARY KEY, scope_key TEXT, state TEXT, reason TEXT,"
            " parts_sent INTEGER NOT NULL DEFAULT 0)",
            "CREATE TABLE closures(scope_key TEXT PRIMARY KEY, state TEXT)",
            "CREATE TABLE private_context(section TEXT PRIMARY KEY, content TEXT, source TEXT, updated_at REAL,"
            " audience TEXT)",
            "CREATE TABLE observed_posts(chat_id INTEGER, msg_id INTEGER, posted_at REAL, sender_id TEXT,"
            " author_kind TEXT, text TEXT, PRIMARY KEY(chat_id,msg_id))",
            "CREATE TABLE counters(day TEXT, name TEXT, value INTEGER, PRIMARY KEY(day,name))",
        ],
        out_rows
        + [
            (
                "INSERT INTO inbound VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("private:123456789", 1, "123456789", "我最近在补星海物语这部动画片", T0 + 200, None, T0, "skipped", 0, ""),
            ),
            ("INSERT INTO items VALUES(?,?,?)", ("i1", "active", "plan")),
            ("INSERT INTO proactive_turns VALUES(?,?,?,?,?)", ("p1", "private:123456789", "skipped", "llm_declined", 0)),
            ("INSERT INTO closures VALUES(?,?)", ("private:123456789", "ended")),
            ("INSERT INTO private_context VALUES(?,?,?,?,?)", ("identity", LONG_TEXT, "x", T0, "private:1")),
            ("INSERT INTO observed_posts VALUES(?,?,?,?,?,?)", (-1009876543210, 1, T0, "1", "owner", LONG_TEXT)),
            ("INSERT INTO observed_posts VALUES(?,?,?,?,?,?)", (-1009876543210, 2, T0, "2", "other", LONG_TEXT)),
            ("INSERT INTO counters VALUES(?,?,?)", ("2026-11-02", "acceptance_probe", 1)),
            ("INSERT INTO counters VALUES(?,?,?)", ("2026-11-02", "中文计数名不应输出的那种长名字啊", 1)),
        ],
    )
    _mk(
        root / S.DB_PATHS["learning"],
        [
            "CREATE TABLE few_shot(id INTEGER PRIMARY KEY, user_text TEXT, bot_text TEXT, status TEXT)",
            "CREATE TABLE expression_pattern(id INTEGER PRIMARY KEY, expression TEXT, hit_count INTEGER, status TEXT)",
        ],
        [
            ("INSERT INTO few_shot(user_text,bot_text,status) VALUES(?,?,?)", ("在吗", "人家在呢", "approved")),
            ("INSERT INTO few_shot(user_text,bot_text,status) VALUES(?,?,?)", ("在吗", LONG_TEXT, "pending_review")),
            ("INSERT INTO expression_pattern(expression,hit_count,status) VALUES(?,?,?)", ("好呀", 33, "approved")),
            ("INSERT INTO expression_pattern(expression,hit_count,status) VALUES(?,?,?)", ("哈哈哈，", 0, "approved")),
        ],
    )
    _mk(
        root / S.DB_PATHS["main"],
        [
            "CREATE TABLE conversations(user_id TEXT, content JSON)",
            "CREATE TABLE provider_stats(created_at DATETIME, token_input_other INTEGER, token_input_cached INTEGER,"
            " token_output INTEGER)",
            "CREATE TABLE personas(id INTEGER PRIMARY KEY, system_prompt TEXT)",
        ],
        [
            (
                "INSERT INTO conversations VALUES(?,?)",
                (
                    "userbot:FriendMessage:123456789",
                    json.dumps([{"role": "user", "content": LONG_TEXT}, {"role": "assistant", "content": "x"}]),
                ),
            ),
            ("INSERT INTO conversations VALUES(?,?)", ("userbot:GroupMessage:-100", "not json")),
            ("INSERT INTO provider_stats VALUES(?,?,?,?)", ("2026-11-02 10:00:00", 1000, 200, 50)),
            ("INSERT INTO personas(system_prompt) VALUES(?)", (LONG_TEXT + "好感度",)),
        ],
    )
    return root


def test_snapshot_smoke_aggregates_without_text(data_root):
    snap = S.take_snapshot(data_root, include_rows=True)
    dumped = json.dumps(snap, ensure_ascii=False)
    # 验收②：输出中扫描不到连续 ≥8 汉字的原文片段；也没有长 id
    assert not M.contains_cjk_run(dumped, 8)
    assert "123456789" not in dumped and "9876543210" not in dumped
    assert snap["sources"] == {"continuity": "ok", "learning": "ok", "persona_evolution": "missing", "main": "ok"}

    ob = snap["continuity"]["outbound"]
    priv = ob["private"]
    assert priv["n"] == 5  # 多段按 turn 合并，failed 不计
    assert priv["question_end_rate"] == 0.4
    assert priv["copy_hard_hit_rate"] == 0.2
    # 私聊回复配上了对方前文：作品提及让软提示被豁免
    assert priv["copy_soft_hit_rate"] == 0.0 and priv["copy_soft_exempt_count"] == 1
    assert ob["group"]["n"] == 3 and ob["group"]["days_covered"] == 1
    assert priv["sessions"] == {"n": 1, "insufficient": 1, "alerts": {}}

    assert snap["continuity"]["inbound"]["private"]["extract_status"] == {"skipped": 1}
    assert snap["continuity"]["proactive_turns"]["state_reason"] == {"skipped|llm_declined": 1}
    assert snap["continuity"]["private_context_len"] == {"identity": len(LONG_TEXT)}
    posts = snap["continuity"]["observed_posts"]
    assert posts["per_chat"] == [{"chat": "chat_1", "other": 1, "owner": 1, "owner_days": 1}]
    assert snap["continuity"]["counters"] == {"acceptance_probe": 1}

    learn = snap["learning"]
    assert learn["few_shot_approved"]["blacklist_hit_count"] == 1
    assert learn["expression_approved"]["hit_count_top5"] == [33, 0]
    assert learn["expression_approved"]["reaction_opening"] == 1

    main = snap["main"]
    assert main["conversations"]["private"]["user_turns"] == 1
    assert main["conversations"]["group"]["user_turns"] == 0
    assert main["provider_stats_daily"][0]["input_other"] == 1000
    assert main["personas"][0]["numeric_leak"] == ["NL-WORD"]

    rows = snap["rows"]
    assert {r["kind"] for r in rows} == {"in", "out"}
    assert all(set(r) <= {"kind", "ch", "origin", "day", "len", "rules"} for r in rows)


def test_open_ro_rejects_writes_and_outside_paths(data_root, tmp_path):
    db = data_root / S.DB_PATHS["learning"]
    conn = S.open_ro(db, [data_root])
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO few_shot(user_text,bot_text,status) VALUES('a','b','c')")
    conn.close()
    with pytest.raises(S.SnapshotSafetyError):
        S.open_ro(db, [tmp_path / "elsewhere"])
    with pytest.raises(S.SnapshotSafetyError):
        S.open_ro(data_root / "nope.db", [data_root])
    assert not (data_root / "nope.db").exists()  # 不会意外新建库
    assert S.ro_uri(Path("/a b/c.db")).endswith("?mode=ro")


def test_assert_no_text_catches_leaks():
    with pytest.raises(S.SnapshotSafetyError):
        S.assert_no_text({"x": [LONG_TEXT]})
    with pytest.raises(S.SnapshotSafetyError):
        S.assert_no_text({LONG_TEXT: 1})
    with pytest.raises(S.SnapshotSafetyError):
        S.assert_no_text({"scope": "private:1234567"})
    S.assert_no_text({"tail:吧": 0.5, "n": 12345678, "day": "2026-11-02"})


def test_write_private_json_perms_and_guard(tmp_path, data_root):
    out = S.write_private_json(tmp_path / "eval" / "b.json", {"n": 1}, data_root=data_root)
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    assert json.loads(out.read_text()) == {"n": 1}
    with pytest.raises(S.SnapshotSafetyError):
        S.write_private_json(data_root / "plugin_data" / "x.json", {"n": 1}, data_root=data_root)
    ok = S.write_private_json(data_root / "dev" / "x.json", {"n": 1}, data_root=data_root)
    assert ok.exists()
    with pytest.raises(S.SnapshotSafetyError):
        S.write_private_json(tmp_path / "leak.json", {"t": LONG_TEXT})
    assert not (tmp_path / "leak.json").exists()


def test_cli_stdout_and_out(data_root, tmp_path, capsys):
    out = tmp_path / "eval" / "base.json"
    assert S.main(["--data-root", str(data_root), "--out", str(out), "--label", "baseline-x/../中文"]) == 0
    snap = json.loads(out.read_text())
    assert snap["label"] == "baseline-x.." and snap["tool"] == S.TOOL_VERSION
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    assert S.main(["--data-root", str(data_root), "--stdout"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert "rows" not in printed
    with pytest.raises(SystemExit):
        S.main(["--data-root", str(data_root)])


def test_missing_and_broken_sources(tmp_path):
    root = tmp_path / "data"
    _mk(root / S.DB_PATHS["learning"], ["CREATE TABLE unrelated(x)"], [])
    snap = S.take_snapshot(root)
    assert snap["sources"]["continuity"] == "missing"
    assert snap["learning"] == {"tables": {"unrelated": 0}}
