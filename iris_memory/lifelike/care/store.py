"""关心桥的表与读写（设计 §7.9.2、§7.9.4b、§7.9.6；附录 A ``care_signals``）

- ``care_signals``：只存 msg_id 与 ≤24 字概括，不存原文；
- ``care_hyperbole``：主人的"口头夸张"表，只存 (类别, 关键词) 与误判次数，不存原句；
- 主人偏好（``/iris_care off`` / ``mute``）存 ``cursors`` 表的 ``care_bridge:pref``，
  不改 Iris 配置文件。

按约定不改 ``user_version``：``ensure_care_schema`` 独立 CREATE TABLE IF NOT EXISTS，
集成阶段并入 v3 迁移。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ..initiative.schema import run_ddl

CARE_DDL = """
CREATE TABLE IF NOT EXISTS care_signals(
  signal_id TEXT PRIMARY KEY,
  chat_id INTEGER NOT NULL,
  msg_ids TEXT NOT NULL,
  first_at REAL NOT NULL, last_at REAL NOT NULL,
  category TEXT NOT NULL,
  intensity_band TEXT NOT NULL,
  confidence_band TEXT NOT NULL,
  gist TEXT NOT NULL DEFAULT '',
  keyword TEXT NOT NULL DEFAULT '',
  detector TEXT NOT NULL,
  crisis INTEGER NOT NULL DEFAULT 0,
  dedupe_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL,
  suppress_reason TEXT NOT NULL DEFAULT '',
  delivery TEXT NOT NULL DEFAULT '',
  cand_id TEXT,
  expires_at REAL NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_care_status ON care_signals(status, expires_at);
CREATE INDEX IF NOT EXISTS ix_care_cat ON care_signals(category, last_at);

CREATE TABLE IF NOT EXISTS care_hyperbole(
  category TEXT NOT NULL, keyword TEXT NOT NULL,
  misread_n INTEGER NOT NULL DEFAULT 0,
  last_at REAL NOT NULL,
  PRIMARY KEY(category, keyword));
"""
_FLAG = "_lifelike_care_schema_ok"

PREF_CURSOR = "care_bridge:pref"
SCAN_CURSOR = "care_bridge"

S_DETECTED = "detected"
S_QUEUED = "queued"
S_OFFERED = "offered"
S_USED = "used"
S_DECLINED = "declined"
S_EXPIRED = "expired"
S_SUPPRESSED = "suppressed"
OPEN = (S_DETECTED, S_QUEUED, S_OFFERED)
TERMINAL = (S_USED, S_DECLINED, S_EXPIRED, S_SUPPRESSED)

HYPERBOLE_MIN_MISREAD = 2
HYPERBOLE_TTL_S = 30 * 86400.0


def ensure_care_schema(store: Any) -> None:
    run_ddl(store, CARE_DDL, _FLAG)


def _row(row: Any) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    d = dict(row)
    try:
        d["msg_ids"] = [int(x) for x in json.loads(d.get("msg_ids") or "[]")]
    except (TypeError, ValueError):
        d["msg_ids"] = []
    return d


class CareStore:
    def __init__(self, store: Any, clock: Callable[[], float] = time.time) -> None:
        self._store = store
        self._clock = clock

    @property
    def base(self) -> Any:
        return self._store

    def _now(self) -> float:
        return float(self._clock())

    def _q(self, sql: str, args: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        ensure_care_schema(self._store)
        with self._store._lock:
            rows = self._store._conn().execute(sql, list(args)).fetchall()
        return [_row(r) for r in rows]  # type: ignore[misc]

    def _exec(self, sql: str, args: Sequence[Any] = ()) -> int:
        ensure_care_schema(self._store)
        with self._store._tx() as db:
            return db.execute(sql, list(args)).rowcount

    # ------------------------------------------------------------ signals

    def get(self, signal_id: str) -> Optional[Dict[str, Any]]:
        rows = self._q("SELECT * FROM care_signals WHERE signal_id=?", (signal_id,))
        return rows[0] if rows else None

    def by_dedupe(self, dedupe_key: str) -> Optional[Dict[str, Any]]:
        rows = self._q("SELECT * FROM care_signals WHERE dedupe_key=?", (dedupe_key,))
        return rows[0] if rows else None

    def by_cand(self, cand_id: str) -> Optional[Dict[str, Any]]:
        rows = self._q("SELECT * FROM care_signals WHERE cand_id=?", (cand_id,))
        return rows[0] if rows else None

    def recent_by_category(self, category: str, since: float) -> List[Dict[str, Any]]:
        return self._q(
            "SELECT * FROM care_signals WHERE category=? AND last_at>=? ORDER BY last_at DESC",
            (category, float(since)),
        )

    def insert(self, **f: Any) -> str:
        now = self._now()
        sid = str(f.get("signal_id") or uuid.uuid4().hex[:16])
        self._exec(
            "INSERT INTO care_signals(signal_id,chat_id,msg_ids,first_at,last_at,category,intensity_band,"
            "confidence_band,gist,keyword,detector,crisis,dedupe_key,status,suppress_reason,delivery,cand_id,"
            "expires_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sid,
                int(f["chat_id"]),
                json.dumps([int(x) for x in f.get("msg_ids") or []]),
                float(f["first_at"]),
                float(f["last_at"]),
                str(f["category"]),
                str(f.get("intensity_band") or "轻"),
                str(f.get("confidence_band") or "试探"),
                str(f.get("gist") or "")[:24],
                str(f.get("keyword") or "")[:8],
                str(f.get("detector") or "rule"),
                1 if f.get("crisis") else 0,
                str(f["dedupe_key"]),
                str(f.get("status") or S_DETECTED),
                str(f.get("suppress_reason") or ""),
                str(f.get("delivery") or ""),
                f.get("cand_id"),
                float(f["expires_at"]),
                now,
                now,
            ),
        )
        return sid

    _UPDATABLE = (
        "msg_ids", "last_at", "intensity_band", "confidence_band", "gist", "keyword", "detector", "crisis",
        "status", "suppress_reason", "delivery", "cand_id", "expires_at",
    )

    def update(self, signal_id: str, **changes: Any) -> None:
        bad = set(changes) - set(self._UPDATABLE)
        if bad:
            raise ValueError(f"care update unknown fields {sorted(bad)}")
        if "msg_ids" in changes:
            changes["msg_ids"] = json.dumps([int(x) for x in changes["msg_ids"]])
        if "crisis" in changes:
            changes["crisis"] = 1 if changes["crisis"] else 0
        cols = list(changes)
        if not cols:
            return
        self._exec(
            f"UPDATE care_signals SET {''.join(c + '=?,' for c in cols)}updated_at=? WHERE signal_id=?",
            [changes[c] for c in cols] + [self._now(), signal_id],
        )

    def counts_by_status(self) -> Dict[str, int]:
        rows = self._q("SELECT status, COUNT(*) AS n FROM care_signals GROUP BY status")
        return {str(r["status"]): int(r["n"]) for r in rows}

    def recent(self, limit: int = 5) -> List[Dict[str, Any]]:
        return self._q(
            "SELECT * FROM care_signals ORDER BY updated_at DESC LIMIT ?", (max(1, min(20, int(limit))),)
        )

    def open_signals(self) -> List[Dict[str, Any]]:
        return self._q(
            f"SELECT * FROM care_signals WHERE status IN ({','.join('?' * len(OPEN))})", list(OPEN)
        )

    def expire(self, now: float) -> int:
        return self._exec(
            f"UPDATE care_signals SET status=?, updated_at=? WHERE status IN ({','.join('?' * len(OPEN))})"
            " AND expires_at<=?",
            [S_EXPIRED, now, *OPEN, float(now)],
        )

    def retention(self, now: float, clear_after_s: float, delete_after_s: float) -> Tuple[int, int]:
        """终态 30 天清空 gist，90 天物理删除（设计 §7.9.6 第 6 条）"""
        marks = ",".join("?" * len(TERMINAL))
        cleared = self._exec(
            f"UPDATE care_signals SET gist='', keyword='', updated_at=updated_at WHERE status IN ({marks})"
            " AND updated_at<=? AND (gist<>'' OR keyword<>'')",
            [*TERMINAL, now - clear_after_s],
        )
        deleted = self._exec(
            f"DELETE FROM care_signals WHERE status IN ({marks}) AND updated_at<=?",
            [*TERMINAL, now - delete_after_s],
        )
        return cleared, deleted

    def purge_text(self) -> int:
        return self._exec("UPDATE care_signals SET gist='', keyword='' WHERE gist<>'' OR keyword<>''")

    # ------------------------------------------------------------ hyperbole

    def note_misread(self, category: str, keyword: str) -> int:
        kw = str(keyword or "").strip()[:8]
        if not kw:
            return 0
        now = self._now()
        self._exec(
            "INSERT INTO care_hyperbole(category,keyword,misread_n,last_at) VALUES (?,?,1,?)"
            " ON CONFLICT(category,keyword) DO UPDATE SET misread_n=misread_n+1, last_at=excluded.last_at",
            (category, kw, now),
        )
        rows = self._q("SELECT misread_n FROM care_hyperbole WHERE category=? AND keyword=?", (category, kw))
        return int(rows[0]["misread_n"]) if rows else 0

    def hyperbole(self, now: float) -> List[Tuple[str, str]]:
        rows = self._q(
            "SELECT category, keyword FROM care_hyperbole WHERE misread_n>=? AND last_at>=?",
            (HYPERBOLE_MIN_MISREAD, float(now) - HYPERBOLE_TTL_S),
        )
        return [(str(r["category"]), str(r["keyword"])) for r in rows]

    def clear_hyperbole(self) -> int:
        return self._exec("DELETE FROM care_hyperbole")

    # ------------------------------------------------------------ prefs / cursor

    def pref(self) -> Dict[str, Any]:
        ensure_care_schema(self._store)
        raw = self._store.get_cursor(PREF_CURSOR)
        try:
            data = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        return {"off": bool(data.get("off")), "muted": sorted({str(x) for x in data.get("muted") or []})}

    def set_pref(self, *, off: Optional[bool] = None, mute: Iterable[str] = (), unmute: Iterable[str] = ()) -> Dict[str, Any]:
        cur = self.pref()
        if off is not None:
            cur["off"] = bool(off)
        muted = set(cur["muted"]) | {str(x) for x in mute}
        muted -= {str(x) for x in unmute}
        cur["muted"] = sorted(muted)
        self._store.set_cursor(PREF_CURSOR, json.dumps(cur, sort_keys=True))
        return cur

    def scan_cursor(self) -> Optional[Dict[str, float]]:
        """扫描游标：``{"rowid": n}``（observed_posts 的 rowid，迟到的离线补采帖子不会漏）；
        旧格式（一个时间戳）→ ``{"ts": t}``；未设置 → None。"""
        raw = self._store.get_cursor(SCAN_CURSOR)
        if raw in (None, ""):
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            data = raw
        if isinstance(data, dict) and "rowid" in data:
            try:
                return {"rowid": int(data["rowid"])}
            except (TypeError, ValueError):
                return None
        try:
            return {"ts": float(data)}
        except (TypeError, ValueError):
            return None

    def set_scan_cursor(self, rowid: int) -> None:
        self._store.set_cursor(SCAN_CURSOR, json.dumps({"rowid": int(rowid)}))

    def max_post_rowid(self) -> int:
        with self._store._lock:
            row = self._store._conn().execute("SELECT MAX(rowid) FROM observed_posts").fetchone()
        return int(row[0] or 0) if row is not None else 0

    def posts_after(
        self, chat_ids: Iterable[int], after_rowid: int, min_posted_at: float, limit: int
    ) -> List[Dict[str, Any]]:
        """rowid > after_rowid 且 posted_at > min_posted_at 的帖子（按 rowid 旧→新），带 ``_rowid``"""
        ids = sorted({int(c) for c in chat_ids})
        if not ids:
            return []
        with self._store._lock:
            rows = self._store._conn().execute(
                f"SELECT rowid AS _rowid, * FROM observed_posts WHERE rowid>? AND chat_id IN ({','.join('?' * len(ids))})"
                " AND posted_at>? ORDER BY rowid LIMIT ?",
                [int(after_rowid), *ids, float(min_posted_at), max(0, int(limit))],
            ).fetchall()
        return [dict(r) for r in rows]
