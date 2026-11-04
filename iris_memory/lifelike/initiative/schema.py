"""主动候选池的表（设计附录 A；ROADMAP P1-8）。

按约定**不改** continuity.db 的 ``user_version``：本模块用 ``CREATE TABLE IF NOT EXISTS``
独立建表（``ensure_initiative_schema``），集成阶段统一并入 v3 迁移。只新增表，不动旧表。
旧代码不会读这些表，回退代码不需要回退库。
"""

from __future__ import annotations

from typing import Any

INITIATIVE_DDL = """
CREATE TABLE IF NOT EXISTS initiative_candidates(
  cand_id TEXT PRIMARY KEY,
  scope_key TEXT NOT NULL,
  ctype TEXT NOT NULL,
  dedupe_key TEXT NOT NULL UNIQUE,
  why_now TEXT NOT NULL,
  material TEXT NOT NULL,
  source_ref TEXT NOT NULL DEFAULT '[]',
  privacy TEXT NOT NULL,
  delivery TEXT NOT NULL,
  not_before REAL NOT NULL, expires_at REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  offered_at REAL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cand_open ON initiative_candidates(scope_key, status, expires_at);

CREATE TABLE IF NOT EXISTS initiative_decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, scope_key TEXT NOT NULL, turn_id TEXT,
  mode TEXT NOT NULL DEFAULT 'on',
  gate_result TEXT NOT NULL,
  ranked TEXT NOT NULL DEFAULT '[]',
  decision TEXT NOT NULL DEFAULT '',
  outcome TEXT NOT NULL DEFAULT '',
  chosen_cand TEXT, chosen_ctype TEXT NOT NULL DEFAULT '',
  why_now TEXT NOT NULL DEFAULT '',
  style_hits TEXT NOT NULL DEFAULT '[]',
  sent_at REAL,
  replied INTEGER, reply_latency_band TEXT, reply_tone TEXT,
  settled_at REAL);
CREATE INDEX IF NOT EXISTS idx_idec_scope ON initiative_decisions(scope_key, ts);
CREATE INDEX IF NOT EXISTS idx_idec_turn ON initiative_decisions(turn_id);
"""

_FLAG = "_lifelike_initiative_schema_ok"


def run_ddl(store: Any, ddl: str, flag: str) -> None:
    """在 ContinuityStore 的连接上执行只含 CREATE ... IF NOT EXISTS 的脚本（幂等，每个 store 一次）"""
    if getattr(store, flag, False):
        return
    lock = getattr(store, "_lock")
    with lock:
        conn = store._conn()
        conn.executescript(ddl)
    try:
        setattr(store, flag, True)
    except Exception:  # noqa: BLE001 - 只是缓存标记
        pass


def ensure_initiative_schema(store: Any) -> None:
    run_ddl(store, INITIATIVE_DDL, _FLAG)
