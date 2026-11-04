"""continuity.db v3 迁移（活人感 P1 批次）：统一建 P1 新表；备份 0600；旧代码读 v3 不报错；
各模块 ensure_*_schema 仍是幂等兜底，且与 v3 建出的结构完全一致。"""

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from astrbot_plugin_iris_memory.iris_memory.continuity import store as store_mod
from astrbot_plugin_iris_memory.iris_memory.continuity.store import ContinuityStore
from astrbot_plugin_iris_memory.iris_memory.lifelike import schema_v3
from astrbot_plugin_iris_memory.iris_memory.lifelike.care.store import ensure_care_schema
from astrbot_plugin_iris_memory.iris_memory.lifelike.initiative.schema import ensure_initiative_schema
from astrbot_plugin_iris_memory.iris_memory.lifelike.members.store import ensure_members_schema
from astrbot_plugin_iris_memory.iris_memory.lifelike.memory.beliefs import ensure_beliefs_schema
from astrbot_plugin_iris_memory.iris_memory.lifelike.memory.tombstones import ensure_tombstones_schema
from astrbot_plugin_iris_memory.iris_memory.lifelike.relation.schema import ensure_relation_schema
from astrbot_plugin_iris_memory.iris_memory.lifelike.world.store import ensure_world_schema


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "continuity.db"


def _schema(conn) -> dict:
    return {
        (r[0], r[1]): r[2]
        for r in conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        )
    }


def _v2_store(db_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(store_mod, "SCHEMA_VERSION", 2)
    s = ContinuityStore(db_path, clock=Clock())
    s.open()
    s.set_cursor("keep", "yes")
    s.record_style_audit("private:1", "passive", ["AIW-08"])
    s.close()


def _ensure_all(s: ContinuityStore) -> None:
    ensure_world_schema(s)
    with s._tx() as db:
        ensure_relation_schema(db)
    ensure_tombstones_schema(s)
    ensure_beliefs_schema(s)
    ensure_members_schema(s)
    ensure_initiative_schema(s)
    ensure_care_schema(s)


def test_v3_statements_are_additive_only():
    stmts = schema_v3.statements()
    assert stmts
    for stmt in stmts:
        head = stmt.upper()
        assert "DROP " not in head and "ALTER " not in head
        assert "IF NOT EXISTS" in head


def test_fresh_db_is_v3_with_all_tables(db_path):
    s = ContinuityStore(db_path, clock=Clock())
    s.open()
    try:
        assert s.get_schema_version() == store_mod.SCHEMA_VERSION == 3
        names = {r[0] for r in s._conn().execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert set(schema_v3.V3_TABLES) <= names
        assert "style_audit" in names
        # 新表全部为空
        for t in schema_v3.V3_TABLES:
            assert s._conn().execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0
    finally:
        s.close()
    assert not list(db_path.parent.glob("*.backup_v*.db"))  # 新库不需要备份


def test_v2_db_migrates_to_v3_with_0600_backup_and_keeps_data(db_path, monkeypatch):
    _v2_store(db_path, monkeypatch)
    monkeypatch.setattr(store_mod, "SCHEMA_VERSION", 3)
    s = ContinuityStore(db_path, clock=Clock())
    s.open()
    try:
        assert s.get_schema_version() == 3
        assert s.get_cursor("keep") == "yes"
        names = {r[0] for r in s._conn().execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert set(schema_v3.V3_TABLES) <= names
        assert s._conn().execute("SELECT COUNT(*) FROM style_audit").fetchone()[0] == 1
    finally:
        s.close()
    backup = db_path.with_name("continuity.backup_v2.db")
    assert backup.exists()
    assert stat.S_IMODE(os.stat(backup).st_mode) == 0o600
    con = sqlite3.connect(str(backup))
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 2
        assert con.execute("SELECT value FROM cursors WHERE name='keep'").fetchone()[0] == "yes"
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not (set(schema_v3.V3_TABLES) & names)
    finally:
        con.close()


def test_v2_db_with_tables_from_ensure_functions_migrates_cleanly(db_path, monkeypatch):
    """P1 分支单独运行过（ensure_* 已在 v2 库上建表）→ v3 迁移照常通过，数据保留。"""
    _v2_store(db_path, monkeypatch)
    s = ContinuityStore(db_path, clock=Clock())
    s.open()
    _ensure_all(s)
    with s._tx() as db:
        db.execute("INSERT INTO tombstones(key_hash, kind, created_at) VALUES('h', 'topic', 1.0)")
    s.close()
    monkeypatch.setattr(store_mod, "SCHEMA_VERSION", 3)
    s2 = ContinuityStore(db_path, clock=Clock())
    s2.open()
    try:
        assert s2.get_schema_version() == 3
        assert s2._conn().execute("SELECT COUNT(*) FROM tombstones").fetchone()[0] == 1
    finally:
        s2.close()


def test_old_code_opens_v3_db_without_error(db_path, monkeypatch):
    s = ContinuityStore(db_path, clock=Clock())
    s.open()
    s.close()
    monkeypatch.setattr(store_mod, "SCHEMA_VERSION", 2)  # 模拟回退到 3ad413e（v2）
    old = ContinuityStore(db_path, clock=Clock())
    old.open()
    try:
        assert old.get_schema_version() == 3  # 不降级、不报错
        old.set_cursor("c", "1")
        assert old.get_cursor("c") == "1"
        old.record_style_audit("private:1", "passive", ["AIW-08"])
    finally:
        old.close()
    assert not list(db_path.parent.glob("*.backup_v*.db"))


def test_ensure_functions_idempotent_and_match_v3(tmp_path, monkeypatch):
    """ensure_* 兜底在 v3 库上是 no-op；在 v2 库上建出的结构与 v3 迁移完全一致（单一来源）。"""
    v3 = ContinuityStore(tmp_path / "a.db", clock=Clock())
    v3.open()
    before = _schema(v3._conn())
    _ensure_all(v3)
    _ensure_all(v3)
    assert _schema(v3._conn()) == before
    monkeypatch.setattr(store_mod, "SCHEMA_VERSION", 2)
    v2 = ContinuityStore(tmp_path / "b.db", clock=Clock())
    v2.open()
    _ensure_all(v2)
    assert _schema(v2._conn()) == before
    v2.close()
    v3.close()


def test_failed_v3_migration_rolls_back_to_v2(db_path, monkeypatch):
    _v2_store(db_path, monkeypatch)
    monkeypatch.setattr(store_mod, "SCHEMA_VERSION", 3)
    monkeypatch.setitem(
        store_mod._MIGRATIONS, 2, lambda: ["CREATE TABLE IF NOT EXISTS t_ok(x)", "THIS IS NOT SQL"]
    )
    s = ContinuityStore(db_path, clock=Clock())
    s.open()  # v3 失败不抛：回滚并停在 v2 继续运行（审查 #10）
    try:
        assert s.get_schema_version() == 2
    finally:
        s.close()
    monkeypatch.setattr(store_mod, "SCHEMA_VERSION", 2)
    s2 = ContinuityStore(db_path, clock=Clock())
    s2.open()
    try:
        assert s2.get_schema_version() == 2
        names = {r[0] for r in s2._conn().execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "t_ok" not in names
    finally:
        s2.close()


def test_life_event_log_append_only_trigger_created_by_v3(db_path):
    s = ContinuityStore(db_path, clock=Clock())
    s.open()
    try:
        with s._tx() as db:
            db.execute(
                "INSERT INTO life_event_log(event_id, op, actor, reason, at) VALUES('e', 'create', 'x', 'r', 1.0)"
            )
        with pytest.raises(sqlite3.Error):
            with s._tx() as db:
                db.execute("DELETE FROM life_event_log")
    finally:
        s.close()


def test_v3_statement_import_failure_keeps_v2_usable(db_path, monkeypatch):
    """P1 模块导入失败（statements() 抛出）→ 停在 v2、连续性库照常可用"""
    _v2_store(db_path, monkeypatch)
    monkeypatch.setattr(store_mod, "SCHEMA_VERSION", 3)

    def boom():
        raise ImportError("broken p1 module")

    monkeypatch.setitem(store_mod._MIGRATIONS, 2, boom)
    s = ContinuityStore(db_path, clock=Clock())
    s.open()
    try:
        assert s.get_schema_version() == 2
        s.set_cursor("x", "1")
        assert s.get_cursor("x") == "1"
    finally:
        s.close()


def test_v1_failure_still_raises(tmp_path, monkeypatch):
    """v0→v1 / v1→v2 失败仍然抛出（核心表缺失不能静默继续）"""
    monkeypatch.setitem(store_mod._MIGRATIONS, 0, "THIS IS NOT SQL")
    s = ContinuityStore(tmp_path / "n.db", clock=Clock())
    with pytest.raises(sqlite3.Error):
        s.open()
