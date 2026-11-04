"""群友认知存储（设计 §6.8.4，附录 A）。

只新增表（CREATE TABLE IF NOT EXISTS，不动 continuity.db 的 user_version；集成阶段合并进 v3）。

- ``member_profiles``：每个群友一行（member_id = observed_posts.sender_id）；
- ``member_aliases``：称呼索引；``@用户名`` 只存小写哈希（``alias_hash``，alias 列存占位 ``#<hash8>``）；
  ``owner_told``（主人告诉她的叫法）``private_only=1``；
- ``member_notes``：印象条目，≤30 字转述；状态只有 active/expired，删除 = 物理删除 + 墓碑。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..memory import tombstones as tb

__all__ = ["ensure_members_schema", "MemberStore", "alias_hash", "member_ref"]

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS member_profiles(
  member_id TEXT PRIMARY KEY,
  chat_ids TEXT NOT NULL,
  first_seen REAL NOT NULL, last_seen REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  updated_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS member_aliases(
  member_id TEXT NOT NULL REFERENCES member_profiles(member_id),
  alias TEXT NOT NULL,
  source TEXT NOT NULL,
  private_only INTEGER NOT NULL DEFAULT 0,
  alias_hash TEXT NOT NULL DEFAULT '',
  last_seen REAL NOT NULL,
  PRIMARY KEY(member_id, alias))""",
    """CREATE TABLE IF NOT EXISTS member_notes(
  note_id TEXT PRIMARY KEY,
  member_id TEXT NOT NULL REFERENCES member_profiles(member_id),
  kind TEXT NOT NULL,
  topic_key TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL,
  evidence_refs TEXT NOT NULL,
  evidence_days INTEGER NOT NULL DEFAULT 1,
  confidence_band TEXT NOT NULL,
  detector TEXT NOT NULL,
  first_seen REAL NOT NULL, last_seen REAL NOT NULL, expires_at REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'active')""",
    "CREATE INDEX IF NOT EXISTS ix_mnote_member ON member_notes(member_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_malias_alias ON member_aliases(alias)",
    # 群友常用称呼（lifelike_member_nickname）：别人在群里用某个叫法的次数（只存称呼本身，不存原文）
    """CREATE TABLE IF NOT EXISTS member_nick_usage(
  member_id TEXT NOT NULL,
  nick TEXT NOT NULL,
  uses INTEGER NOT NULL,
  speakers INTEGER NOT NULL DEFAULT 0,
  last_used REAL NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY(member_id, nick))""",
)

ALIAS_MAX = 16


def ensure_members_schema(store: Any) -> None:
    with tb.write_tx(store) as db:
        for stmt in _SCHEMA:
            db.execute(stmt)
        # 复核 N5：早期开发库里的 member_nick_usage 可能没有 speakers 列 → 补列（只新增，不改数据）
        cols = {r[1] for r in db.execute("PRAGMA table_info(member_nick_usage)").fetchall()}
        if cols and "speakers" not in cols:
            db.execute("ALTER TABLE member_nick_usage ADD COLUMN speakers INTEGER NOT NULL DEFAULT 0")
    tb.ensure_tombstones_schema(store)


def alias_hash(username: str) -> str:
    return hashlib.sha256(("alias:" + str(username or "").strip().lstrip("@").lower()).encode("utf-8")).hexdigest()


def member_ref(member_id: Any) -> str:
    """日志里的群友标识：哈希前 8 位（不记 id、称呼与正文）"""
    return hashlib.sha256(str(member_id).encode("utf-8")).hexdigest()[:8]


class MemberStore:
    def __init__(self, store: Any, clock: Callable[[], float] = time.time) -> None:
        self._store = store
        self._clock = clock
        self._ready = False

    def ensure(self) -> None:
        if not self._ready:
            ensure_members_schema(self._store)
            self._ready = True

    def _read(self, sql: str, args: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        self.ensure()
        return tb.read_rows(self._store, sql, args)

    # ------------------------------------------------------------ profiles
    def get_profile(self, member_id: str) -> Optional[Dict[str, Any]]:
        rows = self._read("SELECT * FROM member_profiles WHERE member_id=?", (str(member_id),))
        return rows[0] if rows else None

    def list_profiles(self, statuses: Sequence[str] = ("active",)) -> List[Dict[str, Any]]:
        return self._read(
            f"SELECT * FROM member_profiles WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY last_seen DESC",
            list(statuses),
        )

    def touch_profile(self, member_id: str, chat_id: Any, seen_at: float) -> None:
        self.ensure()
        now = float(self._clock())
        with tb.write_tx(self._store) as db:
            row = db.execute("SELECT * FROM member_profiles WHERE member_id=?", (str(member_id),)).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO member_profiles(member_id,chat_ids,first_seen,last_seen,status,updated_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (str(member_id), json.dumps([str(chat_id)]), seen_at, seen_at, "active", now),
                )
                return
            chats = _json_list(row["chat_ids"])
            if str(chat_id) not in chats:
                chats.append(str(chat_id))
            db.execute(
                "UPDATE member_profiles SET chat_ids=?, last_seen=MAX(last_seen,?), updated_at=? WHERE member_id=?",
                (json.dumps(chats), seen_at, now, str(member_id)),
            )

    def delete_member(self, member_id: str) -> int:
        """物理删除该群友全部数据（profile/aliases/notes），返回删除的印象条数"""
        self.ensure()
        mid = str(member_id)
        with tb.write_tx(self._store) as db:
            n = db.execute("DELETE FROM member_notes WHERE member_id=?", (mid,)).rowcount
            db.execute("DELETE FROM member_aliases WHERE member_id=?", (mid,))
            db.execute("DELETE FROM member_nick_usage WHERE member_id=?", (mid,))
            db.execute("DELETE FROM member_profiles WHERE member_id=?", (mid,))
        return max(0, int(n))

    def delete_all(self) -> int:
        self.ensure()
        with tb.write_tx(self._store) as db:
            n = db.execute("SELECT COUNT(*) FROM member_profiles").fetchone()[0]
            db.execute("DELETE FROM member_notes")
            db.execute("DELETE FROM member_aliases")
            db.execute("DELETE FROM member_nick_usage")
            db.execute("DELETE FROM member_profiles")
        return int(n)

    # ------------------------------------------------------------ aliases
    def upsert_alias(
        self, member_id: str, alias: str, source: str, *, private_only: int = 0, ahash: str = ""
    ) -> None:
        self.ensure()
        now = float(self._clock())
        with tb.write_tx(self._store) as db:
            db.execute(
                "INSERT INTO member_aliases(member_id,alias,source,private_only,alias_hash,last_seen)"
                " VALUES (?,?,?,?,?,?) ON CONFLICT(member_id,alias) DO UPDATE SET last_seen=excluded.last_seen,"
                " private_only=MAX(private_only, excluded.private_only)",
                (str(member_id), alias[:ALIAS_MAX], source, int(private_only), ahash, now),
            )

    def aliases(self, member_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if member_id is None:
            return self._read(
                "SELECT a.* FROM member_aliases a JOIN member_profiles p ON p.member_id=a.member_id"
                " WHERE p.status='active'"
            )
        return self._read("SELECT * FROM member_aliases WHERE member_id=?", (str(member_id),))

    # ------------------------------------------------------------ nicknames
    def set_nick_usage(self, member_id: str, usage: Dict[str, Any]) -> None:
        """整体替换该群友的称呼用量（usage: nick → (uses, last_used)）。"""
        self.ensure()
        now = float(self._clock())
        mid = str(member_id)
        with tb.write_tx(self._store) as db:
            db.execute("DELETE FROM member_nick_usage WHERE member_id=?", (mid,))
            for nick, val in (usage or {}).items():
                uses, last = val[0], val[1]
                speakers = int(val[2]) if len(val) > 2 else 0
                db.execute(
                    "INSERT INTO member_nick_usage(member_id,nick,uses,speakers,last_used,updated_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (mid, str(nick)[:ALIAS_MAX], int(uses), speakers, float(last or 0.0), now),
                )

    def nickname(self, member_id: str, min_uses: int = 2, min_speakers: int = 2) -> Optional[str]:
        """学到的常用称呼（≥2 个不同的人用过；次数最多、最近用过）；没有 / 档案非 active → None。"""
        rows = self._read(
            "SELECT u.nick FROM member_nick_usage u JOIN member_profiles p ON p.member_id=u.member_id"
            " WHERE u.member_id=? AND p.status='active' AND u.uses>=? AND u.speakers>=?"
            " ORDER BY u.uses DESC, u.last_used DESC LIMIT 1",
            (str(member_id), int(min_uses), int(min_speakers)),
        )
        return str(rows[0]["nick"]) if rows else None

    # ------------------------------------------------------------ notes
    def notes(self, member_id: str, statuses: Sequence[str] = ("active",)) -> List[Dict[str, Any]]:
        return self._read(
            f"SELECT * FROM member_notes WHERE member_id=? AND status IN ({','.join('?' * len(statuses))})"
            " ORDER BY last_seen DESC",
            [str(member_id), *statuses],
        )

    def get_note(self, note_id: str) -> Optional[Dict[str, Any]]:
        rows = self._read("SELECT * FROM member_notes WHERE note_id=?", (str(note_id),))
        return rows[0] if rows else None

    def upsert_note(self, row: Dict[str, Any]) -> str:
        """新建或刷新（同 note_id）；返回 'new' / 'refresh'"""
        self.ensure()
        with tb.write_tx(self._store) as db:
            cur = db.execute("SELECT note_id FROM member_notes WHERE note_id=?", (row["note_id"],)).fetchone()
            if cur is None:
                cols = list(row.keys())
                db.execute(
                    f"INSERT INTO member_notes({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    [row[c] for c in cols],
                )
                return "new"
            db.execute(
                "UPDATE member_notes SET text=?, evidence_refs=?, evidence_days=?, confidence_band=?, last_seen=?,"
                " expires_at=?, status='active' WHERE note_id=?",
                (
                    row["text"], row["evidence_refs"], row["evidence_days"], row["confidence_band"],
                    row["last_seen"], row["expires_at"], row["note_id"],
                ),
            )
            return "refresh"

    def delete_note(self, note_id: str) -> Optional[Dict[str, Any]]:
        self.ensure()
        with tb.write_tx(self._store) as db:
            row = db.execute("SELECT * FROM member_notes WHERE note_id=?", (str(note_id),)).fetchone()
            if row is None:
                return None
            db.execute("DELETE FROM member_notes WHERE note_id=?", (str(note_id),))
            return dict(row)

    def expire_notes(self, note_ids: Sequence[str]) -> int:
        if not note_ids:
            return 0
        self.ensure()
        with tb.write_tx(self._store) as db:
            return int(
                db.execute(
                    f"UPDATE member_notes SET status='expired' WHERE status='active' AND note_id IN"
                    f" ({','.join('?' * len(note_ids))})",
                    list(note_ids),
                ).rowcount
            )

    def sweep(self, now: float, *, member_expire_s: float, member_purge_s: float) -> Dict[str, int]:
        """过期：条目到期 → expired；群友久未发言 → 全部 expired；更久 → 物理删除"""
        self.ensure()
        out = {"expired": 0, "member_expired": 0, "purged": 0}
        with tb.write_tx(self._store) as db:
            out["expired"] = int(
                db.execute(
                    "UPDATE member_notes SET status='expired' WHERE status='active' AND expires_at<?", (now,)
                ).rowcount
            )
            old = [
                r[0]
                for r in db.execute(
                    "SELECT member_id FROM member_profiles WHERE last_seen<?", (now - member_expire_s,)
                ).fetchall()
            ]
            for mid in old:
                out["member_expired"] += int(
                    db.execute(
                        "UPDATE member_notes SET status='expired' WHERE status='active' AND member_id=?", (mid,)
                    ).rowcount
                )
            gone = [
                r[0]
                for r in db.execute(
                    "SELECT member_id FROM member_profiles WHERE last_seen<?", (now - member_purge_s,)
                ).fetchall()
            ]
            for mid in gone:
                db.execute("DELETE FROM member_notes WHERE member_id=?", (mid,))
                db.execute("DELETE FROM member_aliases WHERE member_id=?", (mid,))
                db.execute("DELETE FROM member_profiles WHERE member_id=?", (mid,))
                out["purged"] += 1
        return out

    def counts(self) -> Dict[str, int]:
        rows = self._read(
            "SELECT (SELECT COUNT(*) FROM member_profiles) AS members,"
            " (SELECT COUNT(*) FROM member_notes WHERE status='active') AS notes"
        )
        return {k: int(v) for k, v in rows[0].items()} if rows else {"members": 0, "notes": 0}


def _json_list(raw: Any) -> List[str]:
    try:
        v = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(x) for x in v] if isinstance(v, list) else []
