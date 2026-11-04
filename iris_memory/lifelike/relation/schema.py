"""关系模块的 continuity.db 新表（只 CREATE，不改旧表、不动 user_version）。

集成阶段会把 ``RELATION_SCHEMA`` 并入统一的 v3 迁移；在那之前由
``ensure_relation_schema`` 在模块首次启用（mode≠off）时幂等建表。mode=off 时从不调用，
库与 3ad413e 完全一致。DDL 以设计附录 A 为准，另加三列（均为新表，不涉旧表）：

- ``relationship_events.context``：文字枚举（ask/distance/group/…），不含正文；
- ``relationship_modes.release_ref``：解除该模式的原因事件 id（每次变化都有原因）；
- ``bond_transitions.track`` / ``ref_id``：shadow 轨与正式轨分开；approve/reject/undo
  指向被处理的转移行（表 append-only，不 UPDATE）。
"""

from __future__ import annotations

import sqlite3

__all__ = ["RELATION_SCHEMA", "RELATION_TABLES", "ensure_relation_schema"]

RELATION_TABLES = ("relationship_events", "relationship_modes", "relationship_digest", "bond_transitions")

RELATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS relationship_events(
  rel_event_id TEXT PRIMARY KEY,
  ts REAL NOT NULL,
  scope_key TEXT NOT NULL,
  kind TEXT NOT NULL,
  cause_ref TEXT NOT NULL CHECK (cause_ref <> '[]' AND cause_ref <> ''),
  summary TEXT NOT NULL,
  detector TEXT NOT NULL,
  decay_at REAL,
  supersedes TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  undo_of TEXT,
  context TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_rel_scope ON relationship_events(scope_key, ts);
CREATE TABLE IF NOT EXISTS relationship_modes(
  mode_id TEXT PRIMARY KEY,
  scope_key TEXT NOT NULL,
  kind TEXT NOT NULL,
  cause_event_id TEXT NOT NULL REFERENCES relationship_events(rel_event_id),
  topic TEXT NOT NULL DEFAULT '',
  started_at REAL NOT NULL, min_until REAL NOT NULL, expires_at REAL NOT NULL,
  released_at REAL, release_reason TEXT NOT NULL DEFAULT '',
  release_ref TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_rel_modes_scope ON relationship_modes(scope_key, released_at);
CREATE TABLE IF NOT EXISTS relationship_digest(
  day TEXT PRIMARY KEY, text TEXT NOT NULL, source_events TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS bond_transitions(
  transition_id TEXT PRIMARY KEY,
  scope_key TEXT NOT NULL,
  from_stage TEXT NOT NULL, to_stage TEXT NOT NULL,
  kind TEXT NOT NULL,
  cause_refs TEXT NOT NULL CHECK (cause_refs <> '[]' AND cause_refs <> ''),
  rule_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  actor TEXT NOT NULL,
  created_at REAL NOT NULL,
  track TEXT NOT NULL DEFAULT 'on',
  ref_id TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_bond_scope ON bond_transitions(scope_key, created_at)
"""


def ensure_relation_schema(conn: sqlite3.Connection) -> None:
    """幂等建表；调用方负责事务（ContinuityStore._tx）。"""
    for stmt in RELATION_SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)
