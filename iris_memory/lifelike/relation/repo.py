"""关系模块的同步库访问（continuity.db，经 ContinuityStore 的锁与事务）。

- ``RelationLedger.append`` 是 relationship_events 的唯一写入口：``cause_ref`` 非空且每条引用
  都能在库里找到（或是格式合法的命令 / 群消息 id），否则拒绝 —— 没有原因事件就没有关系变化。
- bond_transitions 只 INSERT（append-only）；relationship_modes 只改 expires_at / released_*。
- 只存 id、枚举与模板摘要，不存聊天正文。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from astrbot.api import logger

from ..guards import numeric
from . import models as rm
from .schema import ensure_relation_schema

__all__ = ["RelationRepo", "valid_scope"]

_REF = re.compile(r"^(msg|out|item|rel|cmd|grp|bt):(.+)$")
# 主人群发言引用：旧格式 chat:msg，或新格式 h<12 位哈希>（不落群 id / 消息 id 原值）
_GRP = re.compile(r"^(?:-?\d+:\d+|h[0-9a-f]{12})$")


def valid_scope(scope_key: str) -> bool:
    return str(scope_key or "").startswith("private:") and len(str(scope_key)) > len("private:")


def _new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:10]}"


class RelationRepo:
    def __init__(self, store: Any) -> None:
        self._store = store
        self._ready = False

    # ------------------------------------------------------------ schema
    def ensure(self) -> None:
        if self._ready:
            return
        with self._store._tx() as db:  # noqa: SLF001 - 同库同锁，集成时并入 v3 迁移
            ensure_relation_schema(db)
        self._ready = True

    def _q(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        self.ensure()
        with self._store._lock:  # noqa: SLF001
            rows = self._store._conn().execute(sql, args).fetchall()  # noqa: SLF001
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ refs
    def _ref_exists(self, db: Any, scope_key: str, ref: str) -> bool:
        m = _REF.match(str(ref or ""))
        if not m:
            return False
        kind, val = m.group(1), m.group(2)
        if kind == "msg":
            if not val.isdigit():
                return False
            mid = int(val)
            if db.execute("SELECT 1 FROM inbound WHERE scope_key=? AND msg_id=?", (scope_key, mid)).fetchone():
                return True
            wm = db.execute("SELECT last_msg_id FROM watermarks WHERE scope_key=?", (scope_key,)).fetchone()
            return wm is not None and int(wm[0]) >= mid
        if kind == "out":
            return val.isdigit() and bool(
                db.execute("SELECT 1 FROM outbound WHERE id=? AND scope_key=?", (int(val), scope_key)).fetchone()
            )
        if kind == "item":
            return bool(db.execute("SELECT 1 FROM items WHERE item_id=?", (val,)).fetchone())
        if kind == "rel":
            return bool(
                db.execute(
                    "SELECT 1 FROM relationship_events WHERE rel_event_id=? AND scope_key=?", (val, scope_key)
                ).fetchone()
            )
        if kind == "bt":
            return bool(db.execute("SELECT 1 FROM bond_transitions WHERE transition_id=?", (val,)).fetchone())
        if kind == "cmd":  # 主人命令消息 id（命令不一定先于本处理器推进水位，只校验格式）
            return val.isdigit()
        if kind == "grp":  # 主人群发言引用（只允许 owner_distress 使用）
            return bool(_GRP.match(val))
        return False

    # ------------------------------------------------------------ events
    def append(self, ev: rm.RelationshipEvent) -> bool:
        """写一条关系事件；校验失败返回 False（不抛）。"""
        refs = [str(r) for r in (ev.cause_ref or []) if str(r or "").strip()]
        reason = ""
        if not valid_scope(ev.scope_key):
            reason = "scope"
        elif ev.kind not in rm.ALL_KINDS:
            reason = "kind"
        elif ev.detector not in rm.DETECTORS:
            reason = "detector"
        elif not refs:
            reason = "no_cause"
        elif any(r.startswith("grp:") for r in refs) and ev.kind != rm.K_DISTRESS:
            reason = "grp_ref"
        elif len(ev.summary or "") > rm.SUMMARY_MAX or numeric.scan(ev.summary or "", strict=True):
            reason = "summary"
        if reason:
            logger.warning(f"{rm.LOG} append rejected kind={ev.kind} reason={reason}")
            return False
        self.ensure()
        with self._store._tx() as db:  # noqa: SLF001
            missing = [r for r in refs if not self._ref_exists(db, ev.scope_key, r)]
            if missing:
                logger.warning(f"{rm.LOG} append rejected kind={ev.kind} reason=cause_missing n={len(missing)}")
                return False
            db.execute(
                "INSERT INTO relationship_events(rel_event_id,ts,scope_key,kind,cause_ref,summary,detector,"
                "decay_at,supersedes,active,undo_of,context) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ev.rel_event_id,
                    float(ev.ts),
                    ev.scope_key,
                    ev.kind,
                    json.dumps(refs, ensure_ascii=False),
                    ev.summary,
                    ev.detector,
                    ev.decay_at,
                    ev.supersedes,
                    1 if ev.active else 0,
                    ev.undo_of,
                    ev.context or "",
                ),
            )
        return True

    @staticmethod
    def make_event(
        scope_key: str,
        kind: str,
        refs: list[str],
        ts: float,
        detector: str,
        *,
        supersedes: str | None = None,
        context: str = "",
        undo_of: str | None = None,
        active: bool = True,
    ) -> rm.RelationshipEvent:
        decay = rm.DECAY_S.get(kind)
        return rm.RelationshipEvent(
            rel_event_id=_new_id("re_"),
            ts=float(ts),
            scope_key=scope_key,
            kind=kind,
            cause_ref=list(refs),
            summary=rm.SUMMARIES.get(kind, ""),
            detector=detector,
            decay_at=(float(ts) + decay) if decay else None,
            supersedes=supersedes,
            active=active,
            undo_of=undo_of,
            context=context,
        )

    def events(self, scope_key: str, since: float = 0.0, *, include_inactive: bool = True) -> list[dict[str, Any]]:
        rows = self._q(
            "SELECT * FROM relationship_events WHERE scope_key=? AND ts>=? ORDER BY ts, rowid",
            (scope_key, float(since)),
        )
        for r in rows:
            try:
                r["cause_ref"] = json.loads(r.get("cause_ref") or "[]")
            except Exception:
                r["cause_ref"] = []
        return rows if include_inactive else [r for r in rows if int(r.get("active") or 0)]

    def get_event(self, rel_event_id: str) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM relationship_events WHERE rel_event_id=?", (rel_event_id,))
        return rows[0] if rows else None

    def undo(self, scope_key: str, rel_event_id: str, ref: str, now: float) -> str | None:
        """active→0，并另写一条 undo 事件（原因 = 命令引用）；返回 undo 事件 id。"""
        row = self.get_event(rel_event_id)
        if row is None or row.get("scope_key") != scope_key or row.get("kind") == rm.K_UNDO:
            return None
        if not int(row.get("active") or 0):
            return None
        ev = self.make_event(
            scope_key, rm.K_UNDO, [ref, f"rel:{rel_event_id}"], now, rm.DETECTOR_CMD, undo_of=rel_event_id
        )
        if not self.append(ev):
            return None
        with self._store._tx() as db:  # noqa: SLF001
            db.execute("UPDATE relationship_events SET active=0 WHERE rel_event_id=?", (rel_event_id,))
        return ev.rel_event_id

    # ------------------------------------------------------------ modes
    def modes(self, scope_key: str, *, live_at: float | None = None) -> list[dict[str, Any]]:
        if live_at is None:
            return self._q(
                "SELECT * FROM relationship_modes WHERE scope_key=? ORDER BY started_at, rowid", (scope_key,)
            )
        return self._q(
            "SELECT * FROM relationship_modes WHERE scope_key=? AND released_at IS NULL AND expires_at>?"
            " ORDER BY started_at, rowid",
            (scope_key, float(live_at)),
        )

    def create_mode(self, scope_key: str, plan: dict[str, Any], cause_event_id: str) -> str:
        self.ensure()
        mode_id = _new_id("rm_")
        with self._store._tx() as db:  # noqa: SLF001
            db.execute(
                "INSERT INTO relationship_modes(mode_id,scope_key,kind,cause_event_id,topic,started_at,"
                "min_until,expires_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    mode_id,
                    scope_key,
                    plan["kind"],
                    cause_event_id,
                    "",
                    float(plan["started_at"]),
                    float(plan["min_until"]),
                    float(plan["expires_at"]),
                ),
            )
        return mode_id

    def extend_mode(self, mode_id: str, expires_at: float) -> None:
        self.ensure()
        with self._store._tx() as db:  # noqa: SLF001
            db.execute(
                "UPDATE relationship_modes SET expires_at=? WHERE mode_id=? AND released_at IS NULL",
                (float(expires_at), mode_id),
            )

    def release_modes(self, mode_ids: list[str], reason: str, ref: str, now: float) -> int:
        if not mode_ids:
            return 0
        self.ensure()
        n = 0
        with self._store._tx() as db:  # noqa: SLF001
            for mid in mode_ids:
                cur = db.execute(
                    "UPDATE relationship_modes SET released_at=?, release_reason=?, release_ref=?"
                    " WHERE mode_id=? AND released_at IS NULL",
                    (float(now), reason, ref, mid),
                )
                n += int(cur.rowcount or 0)
        return n

    def expire_modes(self, now: float) -> int:
        """到期自动解除（持久化，重启照常）：released_at=expires_at, reason=expired。"""
        self.ensure()
        with self._store._tx() as db:  # noqa: SLF001
            cur = db.execute(
                "UPDATE relationship_modes SET released_at=expires_at, release_reason=?, release_ref='expired'"
                " WHERE released_at IS NULL AND expires_at<=?",
                (rm.REL_EXPIRED, float(now)),
            )
            return int(cur.rowcount or 0)

    # ------------------------------------------------------------ bond
    def transitions(self, scope_key: str) -> list[dict[str, Any]]:
        rows = self._q(
            "SELECT * FROM bond_transitions WHERE scope_key=? ORDER BY created_at, rowid", (scope_key,)
        )
        for r in rows:
            try:
                r["cause_refs"] = json.loads(r.get("cause_refs") or "[]")
            except Exception:
                r["cause_refs"] = []
        return rows

    def add_transition(self, scope_key: str, t: rm.BondTransition, now: float) -> str | None:
        """append-only；cause_refs 非空且（自动转移时）都是已存在的关系事件，否则拒绝。"""
        refs = [str(r) for r in (t.cause_refs or []) if str(r or "").strip()]
        if not valid_scope(scope_key) or not refs:
            logger.warning(f"{rm.LOG_BOND} transition rejected kind={t.kind} reason=no_cause")
            return None
        if t.from_stage not in rm.STAGE_CODES or t.to_stage not in rm.STAGE_CODES:
            logger.warning(f"{rm.LOG_BOND} transition rejected kind={t.kind} reason=stage")
            return None
        self.ensure()
        tid = t.transition_id or _new_id("bt_")
        with self._store._tx() as db:  # noqa: SLF001
            for r in refs:
                if t.actor == "bond_folder":
                    ok = bool(
                        db.execute(
                            "SELECT 1 FROM relationship_events WHERE rel_event_id=? AND scope_key=?",
                            (r, scope_key),
                        ).fetchone()
                    )
                else:
                    ok = self._ref_exists(db, scope_key, r) or bool(
                        db.execute(
                            "SELECT 1 FROM relationship_events WHERE rel_event_id=? AND scope_key=?",
                            (r, scope_key),
                        ).fetchone()
                    )
                if not ok:
                    logger.warning(f"{rm.LOG_BOND} transition rejected kind={t.kind} reason=cause_missing")
                    return None
            db.execute(
                "INSERT INTO bond_transitions(transition_id,scope_key,from_stage,to_stage,kind,cause_refs,"
                "rule_id,status,actor,created_at,track,ref_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    tid,
                    scope_key,
                    t.from_stage,
                    t.to_stage,
                    t.kind,
                    json.dumps(refs, ensure_ascii=False),
                    t.rule_id or "",
                    t.status,
                    t.actor,
                    float(t.created_at or now),
                    t.track or rm.MODE_ON,
                    t.ref_id or "",
                ),
            )
        return tid

    # ------------------------------------------------------------ misc reads
    def bot_lines_recent(self, scope_key: str, limit: int = 3) -> list[str]:
        rows = self._q(
            "SELECT text FROM outbound WHERE scope_key=? AND status='sent' AND author='bot'"
            " ORDER BY COALESCE(sent_at, created_at) DESC LIMIT ?",
            (scope_key, int(limit)),
        )
        return [str(r.get("text") or "") for r in reversed(rows)]

    def bot_sent_since(self, scope_key: str, ts: float) -> bool:
        rows = self._q(
            "SELECT 1 FROM outbound WHERE scope_key=? AND status='sent' AND author='bot'"
            " AND COALESCE(sent_at, created_at)>? LIMIT 1",
            (scope_key, float(ts)),
        )
        return bool(rows)

    def promises_done_since(self, scope_key: str, since: float) -> list[dict[str, Any]]:
        return self._q(
            "SELECT item_id, updated_at FROM items WHERE scope_key=? AND kind='bot_promise' AND status='done'"
            " AND updated_at>? ORDER BY updated_at",
            (scope_key, float(since)),
        )

    def derived_exists(self, scope_key: str, ref: str) -> bool:
        rows = self._q(
            "SELECT 1 FROM relationship_events WHERE scope_key=? AND kind=? AND cause_ref=?",
            (scope_key, rm.K_PROMISE_KEPT, json.dumps([ref], ensure_ascii=False)),
        )
        return bool(rows)
