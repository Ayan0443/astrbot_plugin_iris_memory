"""生活系统存储（设计 §4.2～4.4、附录 A）。

复用 ``ContinuityStore`` 的连接与 RLock（同一个 continuity.db），只新增表：
``life_days``、``life_events``、``life_event_log``（append-only，触发器禁止 UPDATE/DELETE）、
``life_state``、``life_claims``。

**不改 user_version**：建表由 ``ensure_world_schema`` 以 CREATE ... IF NOT EXISTS 执行
（只在生活系统开启时调用一次）；集成阶段统一并入 v3 迁移。

日志与表都不存主人原文：事件只存她自己的短语，``life_claims.span`` 是她自己说出口的
片段（≤60 字），``payload`` 只放 id / 规则 id / 计数。
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any, Iterable, Optional, Sequence

from . import models as wm
from . import rules

__all__ = ["WorldStore", "ensure_world_schema", "WORLD_SCHEMA"]

WORLD_SCHEMA: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS life_days(
  day TEXT PRIMARY KEY,
  plan_status TEXT NOT NULL,
  outline TEXT NOT NULL DEFAULT '',
  generator TEXT NOT NULL DEFAULT '',
  inputs_digest TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL, updated_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS life_events(
  event_id TEXT PRIMARY KEY,
  day TEXT NOT NULL,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  slot_start REAL, slot_end REAL, due_at REAL,
  status TEXT NOT NULL,
  outcome TEXT NOT NULL DEFAULT '',
  mood_note TEXT NOT NULL DEFAULT '',
  energy_cost TEXT NOT NULL DEFAULT '',
  scene TEXT NOT NULL DEFAULT '在家',
  source TEXT NOT NULL,
  source_ref TEXT NOT NULL DEFAULT '[]',
  parent_id TEXT,
  shareable TEXT NOT NULL DEFAULT 'owner',
  retracted INTEGER NOT NULL DEFAULT 0,
  mention_count INTEGER NOT NULL DEFAULT 0,
  last_mentioned_at REAL,
  revision INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, settled_at REAL)""",
    "CREATE INDEX IF NOT EXISTS idx_life_events_day ON life_events(day,status)",
    "CREATE INDEX IF NOT EXISTS idx_life_events_due ON life_events(status,due_at)",
    """CREATE TABLE IF NOT EXISTS life_event_log(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL,
  op TEXT NOT NULL,
  from_status TEXT, to_status TEXT,
  actor TEXT NOT NULL,
  reason TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  at REAL NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_life_log_event ON life_event_log(event_id,seq)",
    """CREATE TRIGGER IF NOT EXISTS trg_life_log_no_update BEFORE UPDATE ON life_event_log
  BEGIN SELECT RAISE(ABORT,'life_event_log is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_life_log_no_delete BEFORE DELETE ON life_event_log
  BEGIN SELECT RAISE(ABORT,'life_event_log is append-only'); END""",
    """CREATE TABLE IF NOT EXISTS life_state(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL, source_event TEXT, updated_at REAL NOT NULL)""",
    # applied：该结论是否已落实到事实表（shadow 只记录判定，不补登事件）；
    # rendered_at：conflict 提示已注入过一次（on 模式）
    """CREATE TABLE IF NOT EXISTS life_claims(
  claim_id TEXT PRIMARY KEY,
  outbound_id INTEGER NOT NULL,
  scope_key TEXT NOT NULL,
  span TEXT NOT NULL,
  claim_type TEXT NOT NULL,
  resolution TEXT NOT NULL,
  event_id TEXT,
  applied INTEGER NOT NULL DEFAULT 0,
  reason TEXT NOT NULL DEFAULT '',
  corrected_at REAL,
  created_at REAL NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_life_claims_created ON life_claims(created_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_life_claims_ob_span ON life_claims(outbound_id,span)",
)


def ensure_world_schema(store: Any) -> None:
    """在 continuity.db 上幂等建生活系统表（单事务；不改 user_version）。"""
    with store._tx() as db:  # noqa: SLF001 - 复用 ContinuityStore 的连接与锁
        for stmt in WORLD_SCHEMA:
            db.execute(stmt)


def _new_id(prefix: str = "le") -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


class WorldStore:
    """生活系统表的唯一读写入口（同步；线程安全，调用方用 to_thread）。"""

    def __init__(self, base: Any, clock=time.time) -> None:
        self._base = base
        self._clock = clock
        self._ready = False

    def _now(self) -> float:
        return float(self._clock())

    def ensure_schema(self) -> None:
        if not self._ready:
            ensure_world_schema(self._base)
            self._ready = True

    def _tx(self):
        return self._base._tx()  # noqa: SLF001

    def _read(self, sql: str, args: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._base._lock:  # noqa: SLF001
            rows = self._base._conn().execute(sql, tuple(args)).fetchall()  # noqa: SLF001
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ log
    @staticmethod
    def _log(db: Any, event_id: str, op: str, frm: Optional[str], to: Optional[str], actor: str,
             reason: str, payload: Optional[dict], at: float) -> int:
        cur = db.execute(
            "INSERT INTO life_event_log(event_id,op,from_status,to_status,actor,reason,payload,at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (event_id, op, frm, to, actor, str(reason)[:120],
             json.dumps(payload or {}, ensure_ascii=False), at),
        )
        return int(cur.lastrowid)

    def append_log(self, event_id: str, op: str, actor: str, reason: str, payload: Optional[dict] = None) -> int:
        with self._tx() as db:
            return self._log(db, event_id, op, None, None, actor, reason, payload, self._now())

    def log_for(self, event_id: str) -> list[dict[str, Any]]:
        return self._read("SELECT * FROM life_event_log WHERE event_id=? ORDER BY seq", (event_id,))

    def all_log(self, limit: int = 1000) -> list[dict[str, Any]]:
        return self._read("SELECT * FROM life_event_log ORDER BY seq LIMIT ?", (int(limit),))

    # ------------------------------------------------------------ days
    def get_day(self, day: str) -> Optional[dict[str, Any]]:
        rows = self._read("SELECT * FROM life_days WHERE day=?", (day,))
        return rows[0] if rows else None

    def last_planned_day(self, before: str) -> Optional[str]:
        rows = self._read("SELECT day FROM life_days WHERE day<? ORDER BY day DESC LIMIT 1", (before,))
        return str(rows[0]["day"]) if rows else None

    def write_plan(self, plan: wm.DayPlan, plan_status: str, *, actor: str, inputs_digest: str = "",
                   rejected: Iterable[tuple[str, str]] = ()) -> list[str]:
        """一次事务：写 life_days（已存在则不覆盖，幂等）+ 事件（propose/accept|adjust 日志）。

        返回新写入的 event_id；当天已有计划返回 []。
        """
        now = self._now()
        ids: list[str] = []
        with self._tx() as db:
            if db.execute("SELECT 1 FROM life_days WHERE day=?", (plan.day,)).fetchone():
                return []
            db.execute(
                "INSERT INTO life_days(day,plan_status,outline,generator,inputs_digest,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (plan.day, plan_status, plan.outline, plan.generator, inputs_digest, now, now),
            )
            for reason in rejected:
                self._log(db, f"day:{plan.day}", "reject", None, None, actor, reason[1], {"kind": reason[0]}, now)
            for ev in plan.events:
                self._insert(db, ev, actor=actor, reason="dayplan", now=now)
                ids.append(ev.event_id)
        return ids

    # ------------------------------------------------------------ events
    def _insert(self, db: Any, ev: wm.LifeEvent, *, actor: str, reason: str, now: float,
                op: str = "accept") -> None:
        if not rules.can_transition(None, ev.status, actor):
            raise ValueError(f"illegal create status={ev.status} actor={actor}")
        db.execute(
            "INSERT INTO life_events(event_id,day,kind,title,detail,slot_start,slot_end,due_at,status,outcome,"
            "mood_note,energy_cost,scene,source,source_ref,parent_id,shareable,retracted,mention_count,"
            "last_mentioned_at,revision,created_at,updated_at,settled_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,NULL,1,?,?,?)",
            (ev.event_id, ev.day, ev.kind, ev.title, ev.detail, ev.slot_start, ev.slot_end, ev.due_at,
             ev.status, ev.outcome, ev.mood_note, ev.energy_cost, ev.scene, ev.source,
             json.dumps(ev.source_ref, ensure_ascii=False), ev.parent_id, ev.shareable, now, now,
             ev.settled_at),
        )
        self._log(db, ev.event_id, "propose", None, None, actor, reason, {"source": ev.source}, now)
        self._log(db, ev.event_id, op, None, ev.status, actor, reason, {"source": ev.source}, now)

    def insert_event(self, ev: wm.LifeEvent, *, actor: str, reason: str, op: str = "accept") -> None:
        with self._tx() as db:
            self._insert(db, ev, actor=actor, reason=reason, now=self._now(), op=op)

    def get_event(self, event_id: str) -> Optional[wm.LifeEvent]:
        rows = self._read("SELECT * FROM life_events WHERE event_id=?", (event_id,))
        return wm.LifeEvent.from_row(rows[0]) if rows else None

    def list_events(self, days: Sequence[str] = (), statuses: Sequence[str] = (),
                    include_retracted: bool = False, limit: int = 100) -> list[wm.LifeEvent]:
        sql = "SELECT * FROM life_events WHERE 1=1"
        args: list[Any] = []
        if days:
            sql += f" AND day IN ({','.join('?' * len(days))})"
            args += list(days)
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        if not include_retracted:
            sql += " AND retracted=0"
        sql += " ORDER BY COALESCE(slot_start, due_at, created_at), created_at LIMIT ?"
        args.append(int(limit))
        return [wm.LifeEvent.from_row(r) for r in self._read(sql, args)]

    def pending_events(self, now: float) -> list[wm.LifeEvent]:
        """需要推进的事件：planned 到点 / in_progress 到期（未结算）"""
        rows = self._read(
            "SELECT * FROM life_events WHERE settled_at IS NULL AND ("
            " (status='planned' AND COALESCE(slot_start,due_at)<=?) OR"
            " (status='in_progress' AND due_at<=?)) ORDER BY COALESCE(slot_start,due_at), due_at",
            (float(now), float(now)),
        )
        return [wm.LifeEvent.from_row(r) for r in rows]

    def transition(self, event_id: str, to_status: str, *, actor: str, reason: str,
                   expected_from: Sequence[str], outcome: str = "", mood_note: str = "",
                   settle: bool = False, payload: Optional[dict] = None) -> bool:
        """CAS 转移：只有当前状态 ∈ expected_from 且（结算时）settled_at IS NULL 才生效。

        每个事件只结算一次（重启补结算幂等，验收 ③）。
        """
        now = self._now()
        with self._tx() as db:
            row = db.execute(
                "SELECT status, settled_at FROM life_events WHERE event_id=?", (event_id,)
            ).fetchone()
            if row is None:
                return False
            frm = str(row["status"])
            if frm not in expected_from or not rules.can_transition(frm, to_status, actor):
                return False
            if settle and row["settled_at"] is not None:
                return False
            sets = ["status=?", "updated_at=?", "revision=revision+1"]
            args: list[Any] = [to_status, now]
            if outcome:
                sets.append("outcome=?")
                args.append(outcome[: wm.OUTCOME_MAX])
            if mood_note:
                sets.append("mood_note=?")
                args.append(mood_note[: wm.MOOD_MAX])
            if settle or to_status in wm.TERMINAL:
                sets.append("settled_at=?")
                args.append(now)
            cur = db.execute(
                f"UPDATE life_events SET {', '.join(sets)} WHERE event_id=? AND status=? AND settled_at IS NULL",
                (*args, event_id, frm),
            )
            if cur.rowcount != 1:
                return False
            op = {wm.ST_IN_PROGRESS: "start", wm.ST_DONE: "settle", wm.ST_CANCELLED: "cancel",
                  wm.ST_CHANGED: "change"}.get(to_status, "adjust")
            self._log(db, event_id, op, frm, to_status, actor, reason, payload, now)
        return True

    def note_mention(self, event_id: str, actor: str = "claim_auditor") -> None:
        now = self._now()
        with self._tx() as db:
            db.execute(
                "UPDATE life_events SET mention_count=mention_count+1, last_mentioned_at=? WHERE event_id=?",
                (now, event_id),
            )
            self._log(db, event_id, "mention", None, None, actor, "claim_matched", None, now)

    def retract(self, event_id: str, actor: str, reason: str) -> bool:
        """撤回：不改 status、不删行；投影忽略（设计 §4.3）"""
        now = self._now()
        with self._tx() as db:
            cur = db.execute(
                "UPDATE life_events SET retracted=1, shareable='none', updated_at=? WHERE event_id=? AND retracted=0",
                (now, event_id),
            )
            if cur.rowcount != 1:
                return False
            self._log(db, event_id, "retract", None, None, actor, reason, None, now)
        return True

    # ------------------------------------------------------------ state
    def put_state(self, state: wm.LifeState) -> None:
        now = self._now()
        values = {
            "scene": (state.scene, state.current_event_id),
            "energy": (state.energy, None),
            "mood": (state.mood, state.mood_cause),
            "mood_cause": (state.mood_cause or "", state.mood_cause),
            "current_event": (state.current_event_id or "", state.current_event_id),
            "awake": ("1" if state.awake else "0", None),
        }
        with self._tx() as db:
            for k, (v, src) in values.items():
                db.execute(
                    "INSERT INTO life_state(key,value,source_event,updated_at) VALUES (?,?,?,?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value, source_event=excluded.source_event,"
                    " updated_at=excluded.updated_at",
                    (k, str(v), src, now),
                )

    def get_state(self) -> dict[str, str]:
        return {r["key"]: str(r["value"]) for r in self._read("SELECT key,value FROM life_state")}

    # ------------------------------------------------------------ claims
    def add_claim(self, outbound_id: int, scope_key: str, span: str, claim_type: str, resolution: str,
                  event_id: Optional[str], applied: bool, reason: str = "") -> Optional[str]:
        cid = _new_id("lc")
        with self._tx() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO life_claims(claim_id,outbound_id,scope_key,span,claim_type,resolution,"
                "event_id,applied,reason,corrected_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,NULL,?)",
                (cid, int(outbound_id), scope_key, span[: wm.SPAN_MAX], claim_type, resolution, event_id,
                 1 if applied else 0, reason[:60], self._now()),
            )
            return cid if cur.rowcount == 1 else None

    def claims_since(self, since: float, resolution: Optional[str] = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM life_claims WHERE created_at>=?"
        args: list[Any] = [float(since)]
        if resolution:
            sql += " AND resolution=?"
            args.append(resolution)
        return self._read(sql + " ORDER BY created_at", args)

    def audited(self, outbound_id: int) -> bool:
        return bool(self._read("SELECT 1 FROM life_claims WHERE outbound_id=? LIMIT 1", (int(outbound_id),)))

    def adopted_count(self, day: str) -> int:
        rows = self._read(
            "SELECT COUNT(*) AS n FROM life_events WHERE day=? AND source='chat_claim'", (day,)
        )
        return int(rows[0]["n"]) if rows else 0

    def mark_claim_rendered(self, claim_ids: Sequence[str]) -> None:
        if not claim_ids:
            return
        now = self._now()
        with self._tx() as db:
            db.executemany(
                "UPDATE life_claims SET corrected_at=? WHERE claim_id=? AND corrected_at IS NULL",
                [(now, c) for c in claim_ids],
            )


def new_event_id() -> str:
    return _new_id("le")
