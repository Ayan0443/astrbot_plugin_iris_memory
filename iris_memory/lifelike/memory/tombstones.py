"""tombstones：删除后不再学回来（设计 §6.6、§6.8.4，P1-7 / P1-13 / P0-4 / learning 共用）。

表只存不可逆哈希，不存话题明文：

- ``topic``：``sha256("topic:" + norm_topic(关键词))``——``/iris_mem forget`` 写入；之后任何抽取
  的话题/摘要只要**包含**该关键词（规范化后子串），就跳过。检查方式是把候选文本的所有
  2～24 字子串分别哈希后查表，所以表里不需要明文。
- ``member`` / ``member_minor``：``sha256("member:" + member_id)``——群友被删除 / 识别为未成年。
- ``member_note``：``sha256("note:" + member_id + ":" + 话题键)``——单条印象被删除。

建表用 ``ensure_tombstones_schema``（CREATE TABLE IF NOT EXISTS，不动 user_version；集成阶段
合并进 continuity.db v3 迁移）。没有任何 forget 发生过时表不存在，所有检查直接返回 False，
与改造前行为一致。
"""

from __future__ import annotations

import contextlib
import hashlib
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

from ...continuity import models as m

__all__ = [
    "ensure_tombstones_schema",
    "topic_hash",
    "member_hash",
    "note_hash",
    "add_tombstone",
    "has_tombstone",
    "text_hits_topic_tombstone",
    "table_exists",
    "read_rows",
    "write_tx",
    "KIND_TOPIC",
    "KIND_MEMBER",
    "KIND_MEMBER_MINOR",
    "KIND_MEMBER_NOTE",
]

KIND_TOPIC = "topic"
KIND_MEMBER = "member"
KIND_MEMBER_MINOR = "member_minor"
KIND_MEMBER_NOTE = "member_note"

SUB_MIN = 2
SUB_MAX = 24
TEXT_MAX = 200
_CHUNK = 400

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS tombstones("
    "key_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, created_at REAL NOT NULL)",
)


# ---------------------------------------------------------------- 低层 DB 助手（复用 ContinuityStore 连接与锁）


@contextlib.contextmanager
def write_tx(store: Any) -> Iterator[Any]:
    """ContinuityStore 的写事务（BEGIN IMMEDIATE，可重入）。"""
    with store._tx() as db:  # noqa: SLF001 - 同包内复用连接与锁，不另开连接
        yield db


def read_rows(store: Any, sql: str, args: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    with store._lock:  # noqa: SLF001
        rows = store._conn().execute(sql, tuple(args)).fetchall()  # noqa: SLF001
    return [dict(r) for r in rows]


def table_exists(store: Any, name: str) -> bool:
    rows = read_rows(store, "SELECT 1 AS x FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return bool(rows)


def ensure_tombstones_schema(store: Any) -> None:
    with write_tx(store) as db:
        for stmt in _SCHEMA:
            db.execute(stmt)


# ---------------------------------------------------------------- 哈希


def _sha(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def topic_hash(keyword: str) -> str:
    return _sha("topic:" + m.norm_topic(keyword))


def member_hash(member_id: Any) -> str:
    return _sha("member:" + str(member_id))


def note_hash(member_id: Any, topic_key: str) -> str:
    return _sha("note:" + str(member_id) + ":" + m.norm_topic(topic_key))


# ---------------------------------------------------------------- 读写


def add_tombstone(store: Any, kind: str, key_hash: str, now: Optional[float] = None) -> None:
    ensure_tombstones_schema(store)
    with write_tx(store) as db:
        db.execute(
            "INSERT OR IGNORE INTO tombstones(key_hash,kind,created_at) VALUES (?,?,?)",
            (key_hash, kind, float(now if now is not None else time.time())),
        )


def _any_hash(store: Any, hashes: Iterable[str]) -> bool:
    hs = list(dict.fromkeys(hashes))
    for i in range(0, len(hs), _CHUNK):
        part = hs[i : i + _CHUNK]
        rows = read_rows(
            store,
            f"SELECT 1 AS x FROM tombstones WHERE key_hash IN ({','.join('?' * len(part))}) LIMIT 1",
            part,
        )
        if rows:
            return True
    return False


def has_tombstone(store: Any, key_hash: str) -> bool:
    if not table_exists(store, "tombstones"):
        return False
    return _any_hash(store, [key_hash])


def _substring_hashes(text: str) -> List[str]:
    norm = m.norm_topic(text)[:TEXT_MAX]
    out: List[str] = []
    n = len(norm)
    for size in range(SUB_MIN, min(SUB_MAX, n) + 1):
        for i in range(n - size + 1):
            out.append(_sha("topic:" + norm[i : i + size]))
    return out


def text_hits_topic_tombstone(store: Any, *texts: str) -> bool:
    """任一文本（规范化后）包含某个已删除话题关键词 → True。表不存在 → False。"""
    if not table_exists(store, "tombstones"):
        return False
    hashes: List[str] = []
    for t in texts:
        if t:
            hashes.extend(_substring_hashes(t))
    return bool(hashes) and _any_hash(store, hashes)
