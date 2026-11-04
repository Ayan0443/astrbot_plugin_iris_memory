"""
Iris Chat Memory - 主人限定的表情包库（stickers_library.db）

- 主人在私聊里回复一张 Telegram 贴纸并发 ``/sticker add <标签> [说明]``
  把它登记进库；bot 只能从库里挑表情包发送，不能自己找图。
- 库存的是 Telegram 文档引用（doc_id / access_hash / file_reference）与
  来源消息坐标；file_reference 过期时回源消息刷新后重试一次。
- 生成侧：``prompt_block`` 只给标签与文字说明（DESIGN §12：不出现任何
  数字/计数），模型可在回复最末尾附 ``[表情:标签]``；``parse_marker``
  剥离全部标记并返回第一个允许的标签。
- 限流：同一会话上一张 bot 表情包距今不足 COOLDOWN_SECONDS 则不发；
  除此之外不设任何频率规则（人格不应有固定的表情频率）。
- 隐私：scope='private' 只在主人私聊可用；scope='all' 在开启群聊表情时
  也可用于群；频道与静默会话永远不可用。

同步 sqlite3（check_same_thread=False + RLock），WAL、busy_timeout、
PRAGMA user_version 迁移。async ``send`` 内部的数据库写入走 to_thread。
日志只记录标签 / id，不记录消息正文。
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

try:  # 运行环境（AstrBot 容器）
    from astrbot.api import logger
except Exception:  # pragma: no cover - 独立导入时退化
    import logging

    logger = logging.getLogger("iris.stickers")

SCHEMA_VERSION = 3
DB_FILENAME = "stickers_library.db"

LABEL_MAX_LEN = 12
NOTE_MAX_LEN = 40
COOLDOWN_SECONDS = 120.0

SCOPE_PRIVATE = "private"
SCOPE_ALL = "all"
SCOPES = (SCOPE_PRIVATE, SCOPE_ALL)

SEND_SENT = "sent"
SEND_FAILED = "failed"

# 频道永不作为发送目标（DESIGN §2）；调用方可再追加 silent_sessions。
# 内置禁发列表为空：主人频道等请配置在 continuity.owner_channel_ids / proactive.silent_sessions
DEFAULT_BLOCKED_SCOPES: frozenset = frozenset()

_LABEL_RE = re.compile(r"^[0-9a-z㐀-䶿一-鿿]+$")

# 标记：[表情:x] / 【表情：x】 / ［表情：x］ / [表情 : x ] / [表情]（无标签）
_OPEN = r"[\[【［〔]"
_CLOSE = r"[\]】］〕]"
_MARKER_RE = re.compile(
    _OPEN + r"\s*表情(?:包)?\s*(?:[:：]\s*([^\]】］〕\[【［〔\n]{0,40}?))?\s*" + _CLOSE
)
# 回复末尾未闭合的残缺标记：…[表情:x  /  …【表情：x
_TRAILING_RE = re.compile(_OPEN + r"\s*表情(?:包)?\s*(?:[:：][^\]】］〕\n]{0,40})?\s*$")

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS library(
  label TEXT PRIMARY KEY,
  note TEXT NOT NULL DEFAULT '',
  doc_id INTEGER NOT NULL,
  access_hash INTEGER NOT NULL,
  file_reference BLOB NOT NULL DEFAULT x'',
  source_chat_id INTEGER,
  source_msg_id INTEGER,
  emoji TEXT NOT NULL DEFAULT '',
  scope TEXT NOT NULL DEFAULT 'private' CHECK(scope IN ('private','all')),
  enabled INTEGER NOT NULL DEFAULT 1,
  added_by TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  use_count INTEGER NOT NULL DEFAULT 0,
  last_used_at REAL
);
CREATE TABLE IF NOT EXISTS sends(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope_key TEXT NOT NULL,
  label TEXT NOT NULL,
  tg_msg_id INTEGER,
  status TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sends_scope_time ON sends(scope_key, status, created_at);
"""

# U19 自动学习：只新增表，不改 library / sends（回滚到旧代码时这些表被忽略，旧代码照常工作）。
# learned：从主人私聊自动学到的贴纸（与手动 library 分表，手动的永不自动淘汰）；
# learn_tombstones：主人说“别学这个”或删掉的自动贴纸，同 doc_id 不再学；
# learn_meta：主人 /sticker learn on|off 的暂停标记；
# owner_activity：主人私聊每天的消息条数与其中贴纸条数（只有计数，不存内容）。
_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS learned(
  label TEXT PRIMARY KEY,
  note TEXT NOT NULL DEFAULT '',
  occasion TEXT NOT NULL DEFAULT '',
  doc_id INTEGER NOT NULL UNIQUE,
  access_hash INTEGER NOT NULL,
  file_reference BLOB NOT NULL DEFAULT x'',
  source_chat_id INTEGER,
  source_msg_id INTEGER,
  emoji TEXT NOT NULL DEFAULT '',
  set_id INTEGER,
  group_ok INTEGER NOT NULL DEFAULT 1,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  owner_uses INTEGER NOT NULL DEFAULT 1,
  owner_last_used_at REAL NOT NULL,
  use_count INTEGER NOT NULL DEFAULT 0,
  last_used_at REAL
);
CREATE TABLE IF NOT EXISTS learn_tombstones(
  doc_id INTEGER PRIMARY KEY,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS learn_meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS owner_activity(
  day TEXT PRIMARY KEY,
  msgs INTEGER NOT NULL DEFAULT 0,
  stickers INTEGER NOT NULL DEFAULT 0
)
"""

# user_version=N 的库执行 _MIGRATIONS[N] 后升级到 N+1。只增不删。
# U20 主人在群里用贴纸的方式：learned 只新增列（旧代码 SELECT * / 列名 INSERT 均兼容），
# 按群的计数放新表 owner_activity_scene（scene='group:<群 id>'；私聊仍写 owner_activity，回滚后旧代码照常读）；
# learned_group_use 按群记“主人在哪个群用过哪张、几次、场合短语”——主人用过的证据只在那个群生效。
# learned.group_ok 仍是“无证据”判定（旧代码只看它，偏保守）；group_ok_owner = 有主人在群里用过的证据时的判定。
_SCHEMA_V3 = """
ALTER TABLE learned ADD COLUMN learned_in TEXT NOT NULL DEFAULT 'private';
ALTER TABLE learned ADD COLUMN group_occasion TEXT NOT NULL DEFAULT '';
ALTER TABLE learned ADD COLUMN group_uses INTEGER NOT NULL DEFAULT 0;
ALTER TABLE learned ADD COLUMN group_last_used_at REAL;
ALTER TABLE learned ADD COLUMN group_ok_owner INTEGER NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS learned_group_use(
  doc_id INTEGER NOT NULL,
  group_key TEXT NOT NULL,
  uses INTEGER NOT NULL DEFAULT 0,
  last_used_at REAL,
  occasion TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(doc_id, group_key)
);
CREATE TABLE IF NOT EXISTS owner_activity_scene(
  day TEXT NOT NULL,
  scene TEXT NOT NULL,
  msgs INTEGER NOT NULL DEFAULT 0,
  stickers INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(day, scene)
)
"""

_MIGRATIONS: Dict[int, str] = {0: _SCHEMA_V1, 1: _SCHEMA_V2, 2: _SCHEMA_V3}

SCENE_PRIVATE = "private"
SCENE_GROUP = "group"

SOURCE_MANUAL = "manual"
SOURCE_AUTO = "auto"
LEARN_CAP_DEFAULT = 60
LEARN_PROTECT_SECONDS = 7 * 86400.0


class StickerError(Exception):
    """表情包库的可预期错误（标签非法、不是贴纸、未找到等），消息可直接回给主人。"""


class StickerSendError(Exception):
    """发送失败（含 file_reference 刷新后仍失败）。"""


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def normalize_label(label: Any) -> str:
    """NFKC + 去空白 + ASCII 小写；不校验。"""
    s = unicodedata.normalize("NFKC", str(label or ""))
    s = re.sub(r"\s+", "", s).lower()
    return s


def validate_label(label: Any) -> str:
    """返回规范化标签；非法则抛 StickerError。"""
    s = normalize_label(label)
    if not s:
        raise StickerError("标签不能为空")
    if len(s) > LABEL_MAX_LEN:
        raise StickerError(f"标签太长了，最多 {LABEL_MAX_LEN} 个字")
    if not _LABEL_RE.match(s):
        raise StickerError("标签只能用中文、字母或数字")
    return s


def clean_note(note: Any) -> str:
    s = unicodedata.normalize("NFKC", str(note or ""))
    s = re.sub(r"[\[\]【】［］〔〕]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > NOTE_MAX_LEN:
        raise StickerError(f"说明太长了，最多 {NOTE_MAX_LEN} 个字")
    return s


def _sticker_doc(raw_message: Any) -> Any:
    """Telethon Message.sticker（Document 且带 DocumentAttributeSticker）或 None。"""
    if raw_message is None:
        return None
    try:
        doc = getattr(raw_message, "sticker", None)
    except Exception:
        doc = None
    if doc is None:
        return None
    if getattr(doc, "id", None) is None or getattr(doc, "access_hash", None) is None:
        return None
    return doc


def _sticker_emoji(doc: Any) -> str:
    for attr in getattr(doc, "attributes", None) or []:
        alt = getattr(attr, "alt", None)
        if type(attr).__name__ == "DocumentAttributeSticker" and alt:
            return str(alt)[:16]
    return ""


def _message_ids(result: Any) -> List[int]:
    items = result if isinstance(result, (list, tuple)) else [result]
    ids: List[int] = []
    for it in items:
        mid = getattr(it, "id", None)
        if isinstance(mid, int) and mid > 0:
            ids.append(mid)
    return ids


def _is_file_reference_error(exc: BaseException) -> bool:
    try:
        from telethon import errors as tg_errors

        ref_types = tuple(
            t
            for t in (
                getattr(tg_errors, "FileReferenceExpiredError", None),
                getattr(tg_errors, "FileReferenceInvalidError", None),
                getattr(tg_errors, "FileReferenceEmptyError", None),
            )
            if t is not None
        )
        if ref_types and isinstance(exc, ref_types):
            return True
    except Exception:  # pragma: no cover
        pass
    return "FILE_REFERENCE" in str(exc).upper()


def _opt_int(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


_DAY_TZ = timezone(timedelta(hours=8))


def _day_key(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), _DAY_TZ).strftime("%Y-%m-%d")


def _base_scope(scope_key: str) -> str:
    """去掉 forum 话题后缀 '#thread'。"""
    return str(scope_key or "").split("#", 1)[0]


def prompt_block(entries: Sequence[Dict[str, Any]]) -> str:
    """给生成 prompt 的中文说明。只含标签与文字说明，不含任何数字/计数。"""
    lines: List[str] = []
    seen = set()
    for e in entries or []:
        label = str(e.get("label") or "")
        if not label or label in seen:
            continue
        seen.add(label)
        note = str(e.get("note") or "").strip()
        lines.append(f"- {label}：{note}" if note else f"- {label}")
    if not lines:
        return ""
    return "\n".join(
        [
            "【可用表情包】",
            "主人给你准备了下面这些表情包（标签：什么时候适合用）：",
            *lines,
            "用法：如果某一句回复里正好自然地适合配一个表情包，可以在整条回复的最末尾"
            "附上一个标记 [表情:标签]，系统会把它换成对应的表情包发出去，标记本身不会被看到。",
            "注意：只能用上面列出的标签，不要自己编新标签；不必每次都用，大多数时候不用也很好；"
            "一条回复里只放一个，并且放在最后；表情包不能代替回答，该说的话照常说完。",
        ]
    )


_HWS = " \t\u3000"


def _marker_spans(text: str) -> List[Tuple[int, int, Optional[str]]]:
    """所有标记的 (start, end, raw_label)；末尾残缺标记只在最后一个完整标记之后找。"""
    spans: List[Tuple[int, int, Optional[str]]] = [
        (mt.start(), mt.end(), mt.group(1)) for mt in _MARKER_RE.finditer(text)
    ]
    offset = spans[-1][1] if spans else 0
    tail = _TRAILING_RE.search(text, offset)
    if tail:
        mt2 = re.search(r"[:：](.*)$", tail.group(0))
        spans.append((tail.start(), tail.end(), mt2.group(1) if mt2 else None))
    return spans


def has_marker(text: str) -> bool:
    """文本里是否真有 [表情:…] 标记（含残缺变体）；只出现“表情”二字不算。"""
    return bool(text) and "表情" in text and bool(_marker_spans(text))


def parse_marker(text: str, allowed_labels: Iterable[str]) -> Tuple[str, Optional[str]]:
    """剥离所有 [表情:…] 标记（含全角/残缺变体），返回 (clean_text, 第一个允许的标签|None)。

    没有任何标记时原样返回（逐字节相同）；有标记时只收拾被删片段紧邻的空白，
    其他位置的缩进、连续空格、空行一律不动（代码块等不受影响）。
    """
    if not text:
        return text or "", None
    spans = _marker_spans(text)
    if not spans:
        return text, None
    allowed = {normalize_label(x) for x in (allowed_labels or []) if x}
    found: Optional[str] = None
    for _, _, raw_label in spans:
        if found is None and raw_label:
            cand = normalize_label(raw_label)
            if cand and cand in allowed:
                found = cand

    # 相邻标记之间只有空白时合并为一个被删片段
    merged: List[List[int]] = []
    for s0, e0, _ in spans:
        if merged and not text[merged[-1][1]:s0].strip():
            merged[-1][1] = e0
        else:
            merged.append([s0, e0])

    chunks: List[str] = []
    prev = 0
    for s0, e0 in merged:
        chunks.append(text[prev:s0])
        prev = e0
    chunks.append(text[prev:])

    # 结尾的标记：去掉它前面紧邻的空白
    if not chunks[-1].strip():
        chunks[-1] = ""
        chunks[-2] = chunks[-2].rstrip()
    # 开头的标记：去掉它后面紧邻的空白（只到第一行的缩进为止，保留缩进）
    if not chunks[0].strip():
        chunks = chunks[1:]
        chunks[0] = re.sub(r"^[ \t\u3000]*\n*", "", chunks[0]) if chunks[0].strip() else ""

    out = chunks[0]
    for right in chunks[1:]:
        left = out.rstrip(_HWS)
        r = right.lstrip(_HWS)
        had_space = len(left) != len(out) or len(r) != len(right)
        if not left or not r:
            out = left + r
        elif left.endswith("\n") and r.startswith("\n"):
            out = left + r[1:]  # 标记独占一行：去掉多出的那一行
        elif left.endswith("\n") or r.startswith("\n"):
            out = left + r
        else:
            out = left + (" " if had_space else "") + r
    return out, found


def marker_raw_labels(text: str) -> List[str]:
    """所有 [表情:…] 标记里写的原始标签（按出现顺序；无标签的标记不计）。"""
    if not text or "表情" not in text:
        return []
    return [str(raw).strip() for _, _, raw in _marker_spans(text) if raw and str(raw).strip()]


# 模型模仿主人贴纸在上下文里的样子写成 “[贴纸：标签]”（U19）：只认她回复的最后一行**单独**是这个标记、
# 标签在库里、且前文不是在转述对方的贴纸（“你刚发的是 / 你发的”等）
_TIEZHI_LINE_RE = re.compile(
    r"^\s*" + _OPEN + r"\s*贴纸\s*[:：]\s*([^\]】］〕\[【［〔\n]{1,12}?)\s*" + _CLOSE + r"\s*$"
)
_RETELL_RE = re.compile(r"你(?:刚才?|刚刚)?(?:发|发了|发的|发来|给我发)|这张(?:贴纸|表情)?(?:是|叫)|贴纸(?:是|叫|上)|[：:]\s*$")


def take_tiezhi_marker(text: str, allowed_labels: Iterable[str]) -> Tuple[str, Optional[str]]:
    """最后一行单独的 [贴纸:标签]（标签必须在 allowed 里、前一行不是转述语境）→ (去掉该行后的文本, 标签)；否则原样 + None。"""
    if not text or "贴纸" not in text:
        return text or "", None
    body = text.rstrip()
    head, sep, last = body.rpartition("\n")
    if not sep or not head.strip():
        return text, None
    mt = _TIEZHI_LINE_RE.match(last)
    if not mt:
        return text, None
    allowed = {normalize_label(x) for x in (allowed_labels or []) if x}
    cand = normalize_label(mt.group(1))
    if cand not in allowed:
        return text, None
    prev = head.rstrip().rsplit("\n", 1)[-1]
    if _RETELL_RE.search(prev):
        return text, None
    return head.rstrip(), cand


# ---------------------------------------------------------------------------
# 库
# ---------------------------------------------------------------------------


class StickerLibrary:
    def __init__(
        self,
        db_path: "str | Path",
        clock: Optional[Callable[[], float]] = None,
        *,
        busy_timeout_ms: int = 5000,
        cooldown_seconds: float = COOLDOWN_SECONDS,
    ) -> None:
        self.db_path = Path(db_path)
        self._clock = clock or time.time
        self._busy_timeout_ms = int(busy_timeout_ms)
        self.cooldown_seconds = float(cooldown_seconds)
        self._lock = threading.RLock()
        self._db: Optional[sqlite3.Connection] = None
        self._migrate()

    # 纯函数也挂在实例上，方便调用方只持有 library 对象
    prompt_block = staticmethod(prompt_block)
    parse_marker = staticmethod(parse_marker)

    # -- 连接 / 迁移 ---------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        with self._lock:
            if self._db is None:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
                db = sqlite3.connect(
                    str(self.db_path), check_same_thread=False, isolation_level=None
                )
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA journal_mode=WAL")
                db.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
                self._db = db
            return self._db

    def schema_version(self) -> int:
        with self._lock:
            return int(self._conn().execute("PRAGMA user_version").fetchone()[0])

    def _migrate(self) -> None:
        with self._lock:
            db = self._conn()
            version = self.schema_version()
            while version < SCHEMA_VERSION:
                script = _MIGRATIONS[version]
                db.execute("BEGIN IMMEDIATE")
                try:
                    for stmt in [s.strip() for s in script.split(";") if s.strip()]:
                        db.execute(stmt)
                    db.execute(f"PRAGMA user_version={version + 1}")
                    db.execute("COMMIT")
                except Exception:
                    db.execute("ROLLBACK")
                    raise
                version += 1

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                try:
                    self._db.close()
                finally:
                    self._db = None

    @staticmethod
    def _row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        d = dict(row)
        fr = d.get("file_reference")
        d["file_reference"] = bytes(fr) if fr is not None else b""
        d["enabled"] = bool(d.get("enabled"))
        return d

    # -- 增删改查 ------------------------------------------------------------

    def get(self, label: str) -> Optional[Dict[str, Any]]:
        key = normalize_label(label)
        with self._lock:
            row = self._conn().execute("SELECT * FROM library WHERE label=?", (key,)).fetchone()
        return self._row(row)

    def add_from_message(
        self, label: str, note: str, raw_message: Any, added_by: Any, *, default_scope: str = "private"
    ) -> Dict[str, Any]:
        """登记 raw_message（Telethon Message）里的贴纸；同标签覆盖（保留 scope/计数）。

        default_scope：新条目的 scope（统一启用时为 'all'；已有条目覆盖时不改 scope）。"""
        key = validate_label(label)
        note_s = clean_note(note)
        doc = _sticker_doc(raw_message)
        if doc is None:
            raise StickerError("这条消息不是贴纸，请回复一张贴纸再发 /sticker add")
        fr = getattr(doc, "file_reference", None) or b""
        chat_id = getattr(raw_message, "chat_id", None)
        msg_id = getattr(raw_message, "id", None)
        new_scope = SCOPE_ALL if str(default_scope).strip().lower() == SCOPE_ALL else SCOPE_PRIVATE
        now = float(self._clock())
        with self._lock:
            db = self._conn()
            db.execute("BEGIN IMMEDIATE")
            try:
                self._yield_learned_locked(db, key, int(doc.id), now)
                db.execute(
                    """
                    INSERT INTO library(label, note, doc_id, access_hash, file_reference,
                        source_chat_id, source_msg_id, emoji, scope, enabled, added_by,
                        created_at, updated_at, use_count, last_used_at)
                    VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?,0,NULL)
                    ON CONFLICT(label) DO UPDATE SET
                        note=excluded.note, doc_id=excluded.doc_id,
                        access_hash=excluded.access_hash,
                        file_reference=excluded.file_reference,
                        source_chat_id=excluded.source_chat_id,
                        source_msg_id=excluded.source_msg_id,
                        emoji=excluded.emoji, enabled=1,
                        added_by=excluded.added_by, updated_at=excluded.updated_at
                    """,
                    (
                        key,
                        note_s,
                        int(doc.id),
                        int(doc.access_hash),
                        sqlite3.Binary(bytes(fr)),
                        int(chat_id) if chat_id is not None else None,
                        int(msg_id) if msg_id is not None else None,
                        _sticker_emoji(doc),
                        new_scope,
                        str(added_by or ""),
                        now,
                        now,
                    ),
                )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        logger.info(f"[stickers] 登记表情包 label={key} doc={int(doc.id)}")
        entry = self.get(key)
        assert entry is not None
        return entry

    def _yield_learned_locked(self, db: sqlite3.Connection, label: str, doc_id: int, now: float) -> None:
        """手动 /sticker add 优先（审查 2）：
        - 同一张贴纸（doc_id）已被自动学到 → 删掉那条自动条目（不记墓碑，主人只是改成手动管理）；
        - 有同名的自动条目（另一张贴纸）→ 自动条目改名为“标签二 / 标签三…”，其余内容不动。"""
        db.execute("DELETE FROM learned WHERE doc_id=?", (int(doc_id),))
        db.execute("DELETE FROM learned_group_use WHERE doc_id NOT IN (SELECT doc_id FROM learned)")
        row = db.execute("SELECT doc_id FROM learned WHERE label=?", (label,)).fetchone()
        if not row:
            return
        taken = {r[0] for r in db.execute("SELECT label FROM library").fetchall()}
        taken |= {r[0] for r in db.execute("SELECT label FROM learned").fetchall()}
        taken.add(label)
        new_label = next(
            (c for c in (f"{label}{x}" for x in "二三四五六七八九") if c not in taken and len(c) <= LABEL_MAX_LEN),
            None,
        )
        if new_label is None:
            db.execute("UPDATE learned SET enabled=0, updated_at=? WHERE label=?", (now, label))
            return
        db.execute("UPDATE learned SET label=?, updated_at=? WHERE label=?", (new_label, now, label))
        logger.info(f"[stickers.learn] 手动标签占用，自动表情包改名 {label} -> {new_label}")

    def remove(self, label: str) -> bool:
        key = normalize_label(label)
        with self._lock:
            cur = self._conn().execute("DELETE FROM library WHERE label=?", (key,))
        return cur.rowcount > 0

    def list(self, scope_key: Optional[str] = None) -> List[Dict[str, Any]]:
        """全部（含停用）条目；给了 scope_key 时只返回该作用域可见 scope 的启用条目
        （私聊 'private:*' → private+all；其他 → all）。权限判定请用 allowed_for。"""
        sql = "SELECT * FROM library"
        params: Tuple[Any, ...] = ()
        if scope_key is not None:
            if str(scope_key).startswith("private:"):
                sql += " WHERE enabled=1"
            else:
                sql += " WHERE enabled=1 AND scope='all'"
        sql += " ORDER BY created_at, label"
        with self._lock:
            rows = self._conn().execute(sql, params).fetchall()
        return [self._row(r) for r in rows]  # type: ignore[misc]

    def set_scope(self, label: str, scope: str) -> Dict[str, Any]:
        key = normalize_label(label)
        sc = str(scope or "").strip().lower()
        if sc not in SCOPES:
            raise StickerError("范围只能是 private 或 all")
        with self._lock:
            cur = self._conn().execute(
                "UPDATE library SET scope=?, updated_at=? WHERE label=?",
                (sc, float(self._clock()), key),
            )
        if cur.rowcount == 0:
            raise StickerError(f"没有叫「{key}」的表情包")
        entry = self.get(key)
        assert entry is not None
        return entry

    def set_enabled(self, label: str, enabled: bool) -> bool:
        key = normalize_label(label)
        with self._lock:
            cur = self._conn().execute(
                "UPDATE library SET enabled=?, updated_at=? WHERE label=?",
                (1 if enabled else 0, float(self._clock()), key),
            )
        return cur.rowcount > 0

    def allowed_for(
        self,
        scope_key: str,
        owner_ids: Iterable[Any],
        groups_enabled: bool,
        *,
        blocked_scopes: Iterable[Any] = (),
        unified: bool = False,
        include_learned: bool = False,
    ) -> List[Dict[str, Any]]:
        """该会话可用的表情包。

        unified=True（continuity.sticker_scope_unified）：忽略 private/all 区分——主人私聊与
        （groups_enabled 时的）群里都可用全部启用条目；频道 / blocked_scopes 仍永远为空。

        - 主人私聊 ``private:<owner>`` → scope private + all；
        - 其他私聊与群 → 仅 scope='all'，且仅当 groups_enabled；
        - 频道（DEFAULT_BLOCKED_SCOPES）与 blocked_scopes（silent_sessions 等）→ 永远为空。
        """
        sk = str(scope_key or "").strip()
        if not sk:
            return []
        blocked = {str(x) for x in DEFAULT_BLOCKED_SCOPES} | {str(x) for x in blocked_scopes or ()}
        if sk in blocked or _base_scope(sk) in blocked:
            return []
        owners = {str(x) for x in owner_ids or ()}
        if sk.startswith("private:"):
            uid = sk.split(":", 1)[1]
            if uid in owners:
                manual = self.list(scope_key=sk)
                if not include_learned:
                    return manual
                return self._with_learned(manual, private=True, scope_key=sk)
        if not groups_enabled:
            return []
        sql = (
            "SELECT * FROM library WHERE enabled=1 ORDER BY created_at, label"
            if unified
            else "SELECT * FROM library WHERE enabled=1 AND scope='all' ORDER BY created_at, label"
        )
        with self._lock:
            rows = self._conn().execute(sql).fetchall()
        manual = [self._row(r) for r in rows]  # type: ignore[misc]
        if not include_learned:
            return manual  # type: ignore[return-value]
        return self._with_learned(manual, private=False, scope_key=sk)  # type: ignore[arg-type]

    def _with_learned(
        self, manual: List[Dict[str, Any]], *, private: bool, scope_key: str = ""
    ) -> List[Dict[str, Any]]:
        """手动条目（source=manual）+ 自动学到的条目（source=auto）。
        群里：group_ok（无证据也可群用）的，加上 group_ok_owner 且主人在**这个群**用过的（U20 按群证据）；
        群里条目的 group_uses / group_occasion 换成这个群自己的统计（没有则为 0 / 空），不引用别的群。
        标签与手动条目重名、或 doc_id 已在手动库里的自动条目不返回（手动优先）。"""
        out = [dict(e, source=SOURCE_MANUAL) for e in manual]
        taken = {e["label"] for e in manual}
        docs = {int(e["doc_id"]) for e in manual}
        gkey = _base_scope(scope_key)
        with self._lock:
            db = self._conn()
            if private:
                rows = db.execute(
                    "SELECT * FROM learned WHERE enabled=1 ORDER BY owner_last_used_at DESC, label"
                ).fetchall()
                here: Dict[int, Tuple[int, str]] = {}
            else:
                rows = db.execute(
                    "SELECT * FROM learned WHERE enabled=1 AND (group_ok=1 OR (group_ok_owner=1 AND doc_id IN"
                    " (SELECT doc_id FROM learned_group_use WHERE group_key=? AND uses>0)))"
                    " ORDER BY owner_last_used_at DESC, label",
                    (gkey,),
                ).fetchall()
                here = {
                    int(r[0]): (int(r[1]), str(r[2] or ""))
                    for r in db.execute(
                        "SELECT doc_id, uses, occasion FROM learned_group_use WHERE group_key=?", (gkey,)
                    ).fetchall()
                }
        for r in rows:
            e = self._row(r)
            assert e is not None
            if e["label"] in taken or int(e["doc_id"]) in docs:
                continue
            e["source"] = SOURCE_AUTO
            e["group_ok"] = bool(e.get("group_ok"))
            if not private:
                uses, occ = here.get(int(e["doc_id"]), (0, ""))
                e["group_uses"], e["group_occasion"] = uses, occ
            out.append(e)
        return out

    def group_use_get(self, doc_id: int, group_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn().execute(
                "SELECT uses, last_used_at, occasion FROM learned_group_use WHERE doc_id=? AND group_key=?",
                (int(doc_id), _base_scope(group_key)),
            ).fetchone()
        return {"uses": int(row[0]), "last_used_at": row[1], "occasion": row[2]} if row else None

    @staticmethod
    def _note_group_use_locked(db: sqlite3.Connection, doc_id: int, group_key: str, occasion: str, t: float) -> None:
        gk = _base_scope(group_key)
        if not gk:
            return
        db.execute(
            "INSERT INTO learned_group_use(doc_id, group_key, uses, last_used_at, occasion) VALUES(?,?,1,?,?)"
            " ON CONFLICT(doc_id, group_key) DO UPDATE SET uses=uses+1, last_used_at=excluded.last_used_at,"
            " occasion=CASE WHEN excluded.occasion != '' THEN excluded.occasion ELSE occasion END",
            (int(doc_id), gk, t, str(occasion or "")[:20]),
        )

    # -- U19 自动学习 ----------------------------------------------------------

    def learned_get(self, label: str) -> Optional[Dict[str, Any]]:
        key = normalize_label(label)
        with self._lock:
            row = self._conn().execute("SELECT * FROM learned WHERE label=?", (key,)).fetchone()
        e = self._row(row)
        if e is not None:
            e["source"] = SOURCE_AUTO
            e["group_ok"] = bool(e.get("group_ok"))
        return e

    def learned_by_doc(self, doc_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn().execute("SELECT label FROM learned WHERE doc_id=?", (int(doc_id),)).fetchone()
        return self.learned_get(row[0]) if row else None

    def learned_list(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn().execute(
                "SELECT label FROM learned ORDER BY owner_last_used_at DESC, label"
            ).fetchall()
        return [e for e in (self.learned_get(r[0]) for r in rows) if e is not None]

    def is_tombstoned(self, doc_id: int) -> bool:
        with self._lock:
            row = self._conn().execute(
                "SELECT 1 FROM learn_tombstones WHERE doc_id=?", (int(doc_id),)
            ).fetchone()
        return row is not None

    def manual_has_doc(self, doc_id: int) -> bool:
        with self._lock:
            row = self._conn().execute("SELECT 1 FROM library WHERE doc_id=?", (int(doc_id),)).fetchone()
        return row is not None

    def taken_labels(self) -> set:
        with self._lock:
            db = self._conn()
            a = {r[0] for r in db.execute("SELECT label FROM library").fetchall()}
            b = {r[0] for r in db.execute("SELECT label FROM learned").fetchall()}
        return a | b

    def learn_touch(
        self,
        doc_id: int,
        raw_message: Any = None,
        occasion: str = "",
        *,
        now: Optional[float] = None,
        scene: str = SCENE_PRIVATE,
        group_ok: Optional[bool] = None,
        group_key: str = "",
        group_ok_owner: Optional[bool] = None,
    ) -> Optional[Dict[str, Any]]:
        """主人又用了一次已学过的贴纸：次数 +1、最近使用时间更新、刷新 file_reference / 来源消息；
        给了场合则更新场合。没有这条返回 None。

        scene=group（U20）：另记群里次数 / 最近时间，场合写进 group_occasion（私聊场合不动），
        并按 group_key 记“主人在这个群用过”（证据只在该群生效）；给了 group_ok_owner 则更新有证据时的判定。
        group_ok（无证据判定）只在显式给出时更新。"""
        t = float(self._clock() if now is None else now)
        doc = _sticker_doc(raw_message) if raw_message is not None else None
        with self._lock:
            db = self._conn()
            cur = db.execute(
                "UPDATE learned SET owner_uses=owner_uses+1, owner_last_used_at=?, updated_at=? WHERE doc_id=?",
                (t, t, int(doc_id)),
            )
            if cur.rowcount == 0:
                return None
            if doc is not None and int(getattr(doc, "id", 0) or 0) == int(doc_id):
                fr = getattr(doc, "file_reference", None) or b""
                db.execute(
                    "UPDATE learned SET access_hash=?, file_reference=?, source_chat_id=?, source_msg_id=?"
                    " WHERE doc_id=?",
                    (
                        int(doc.access_hash),
                        sqlite3.Binary(bytes(fr)),
                        _opt_int(getattr(raw_message, "chat_id", None)),
                        _opt_int(getattr(raw_message, "id", None)),
                        int(doc_id),
                    ),
                )
            if scene == SCENE_GROUP:
                db.execute(
                    "UPDATE learned SET group_uses=group_uses+1, group_last_used_at=? WHERE doc_id=?",
                    (t, int(doc_id)),
                )
                self._note_group_use_locked(db, int(doc_id), group_key, occasion, t)
                if occasion:
                    db.execute(
                        "UPDATE learned SET group_occasion=? WHERE doc_id=?", (str(occasion)[:20], int(doc_id))
                    )
            elif occasion:
                db.execute("UPDATE learned SET occasion=? WHERE doc_id=?", (str(occasion)[:20], int(doc_id)))
            if group_ok is not None:
                db.execute("UPDATE learned SET group_ok=? WHERE doc_id=?", (1 if group_ok else 0, int(doc_id)))
            if group_ok_owner is not None:
                db.execute(
                    "UPDATE learned SET group_ok_owner=? WHERE doc_id=?", (1 if group_ok_owner else 0, int(doc_id))
                )
        return self.learned_by_doc(doc_id)

    def learn_set_occasion(self, doc_id: int, occasion: str) -> bool:
        if not occasion:
            return False
        with self._lock:
            cur = self._conn().execute(
                "UPDATE learned SET occasion=? WHERE doc_id=?", (str(occasion)[:20], int(doc_id))
            )
        return cur.rowcount > 0

    def learn_add(
        self,
        raw_message: Any,
        labels: Sequence[str],
        note: str,
        *,
        occasion: str = "",
        group_ok: bool = True,
        set_id: Optional[int] = None,
        cap: int = LEARN_CAP_DEFAULT,
        now: Optional[float] = None,
        scene: str = SCENE_PRIVATE,
        group_key: str = "",
        group_ok_owner: Optional[bool] = None,
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """把主人私聊里的一张贴纸学进库。返回 (状态, 条目)：

        - ``added``：新加（labels 里第一个没被占用的标签）；超出 cap 时淘汰主人最久没用、用得最少的自动条目；
        - ``touched``：同 doc_id 已学过 → 次数 +1；
        - ``manual`` / ``tombstoned`` / ``no_label`` / ``not_sticker``：不学。"""
        doc = _sticker_doc(raw_message)
        if doc is None:
            return "not_sticker", None
        doc_id = int(doc.id)
        t = float(self._clock() if now is None else now)
        if self.is_tombstoned(doc_id):
            return "tombstoned", None
        if self.manual_has_doc(doc_id):
            return "manual", None
        touched = self.learn_touch(
            doc_id, raw_message, occasion, now=t, scene=scene,
            group_key=group_key, group_ok_owner=group_ok_owner,
        )
        if touched is not None:
            return "touched", touched
        note_s = re.sub(r"[\[\]【】［］〔〕]", "", unicodedata.normalize("NFKC", str(note or ""))).strip()[:NOTE_MAX_LEN]
        fr = getattr(doc, "file_reference", None) or b""
        with self._lock:
            db = self._conn()
            taken = self.taken_labels()
            label = next(
                (
                    k
                    for k in (normalize_label(x) for x in labels or ())
                    if k and k not in taken and len(k) <= LABEL_MAX_LEN and _LABEL_RE.match(k)
                ),
                None,
            )
            if label is None:
                return "no_label", None
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    """
                    INSERT INTO learned(label, note, occasion, doc_id, access_hash, file_reference,
                        source_chat_id, source_msg_id, emoji, set_id, group_ok, enabled,
                        created_at, updated_at, owner_uses, owner_last_used_at, use_count, last_used_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?,1,?,0,NULL)
                    """,
                    (
                        label,
                        note_s,
                        "" if scene == SCENE_GROUP else str(occasion or "")[:20],
                        doc_id,
                        int(doc.access_hash),
                        sqlite3.Binary(bytes(fr)),
                        _opt_int(getattr(raw_message, "chat_id", None)),
                        _opt_int(getattr(raw_message, "id", None)),
                        _sticker_emoji(doc),
                        _opt_int(set_id),
                        1 if group_ok else 0,
                        t,
                        t,
                        t,
                    ),
                )
                if group_ok_owner is not None:
                    db.execute(
                        "UPDATE learned SET group_ok_owner=? WHERE label=?", (1 if group_ok_owner else 0, label)
                    )
                if scene == SCENE_GROUP:
                    db.execute(
                        "UPDATE learned SET learned_in='group', group_uses=1, group_last_used_at=?,"
                        " group_occasion=? WHERE label=?",
                        (t, str(occasion or "")[:20], label),
                    )
                    self._note_group_use_locked(db, doc_id, group_key, occasion, t)
                evicted, kept = self._evict_locked(db, max(1, int(cap)), t, new_label=label)
                if not kept:
                    # 其余自动条目都还在保护期内：这张新的不收（不超出容量），如实返回 evicted
                    db.execute("DELETE FROM learned WHERE label=?", (label,))
                    db.execute("DELETE FROM learned_group_use WHERE doc_id NOT IN (SELECT doc_id FROM learned)")
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        if not kept:
            logger.info(f"[stickers.learn] 容量已满且其余都在保护期，新表情包未收 doc={doc_id}")
            return "evicted", None
        logger.info(
            f"[stickers.learn] 学到表情包 label={label} doc={doc_id} group_ok={int(bool(group_ok))} evicted={evicted}"
        )
        return "added", self.learned_get(label)

    @staticmethod
    def _evict_locked(
        db: sqlite3.Connection, cap: int, now: float, *, new_label: Optional[str] = None
    ) -> Tuple[int, bool]:
        """自动条目超过 cap：在“保护期外”的旧条目里，按“主人最近使用时间 + 使用次数加成”最旧的先淘汰。

        新学条目 LEARN_PROTECT_SECONDS（7 天）内不淘汰，刚插入的那条也不参与；手动条目不在此表，永不淘汰。
        返回 (淘汰条数, 新条目是否保留)；保护期外没有可淘汰的 → 新条目不保留（调用方删掉它）。"""
        n = int(db.execute("SELECT COUNT(*) FROM learned").fetchone()[0])
        over = n - cap
        if over <= 0:
            return 0, True
        rows = db.execute(
            "SELECT label FROM learned WHERE created_at <= ? AND label != ?"
            " ORDER BY owner_last_used_at + MIN(owner_uses, 10) * 172800.0 ASC,"
            " owner_uses ASC, created_at ASC LIMIT ?",
            (now - LEARN_PROTECT_SECONDS, str(new_label or ""), over),
        ).fetchall()
        for (label,) in rows:
            db.execute("DELETE FROM learned WHERE label=?", (label,))
            db.execute("DELETE FROM learned_group_use WHERE doc_id NOT IN (SELECT doc_id FROM learned)")
        if rows:
            logger.info(f"[stickers.learn] 容量已满，淘汰自动表情包 count={len(rows)}")
        return len(rows), len(rows) >= over

    def tombstone_doc(self, doc_id: int) -> Optional[str]:
        """主人说“别学这个 / 这个别用”：记墓碑并删掉同 doc_id 的自动条目；返回被删的标签（没有则 None）。"""
        t = float(self._clock())
        with self._lock:
            db = self._conn()
            row = db.execute("SELECT label FROM learned WHERE doc_id=?", (int(doc_id),)).fetchone()
            db.execute(
                "INSERT OR IGNORE INTO learn_tombstones(doc_id, created_at) VALUES(?,?)", (int(doc_id), t)
            )
            if row:
                db.execute("DELETE FROM learned WHERE doc_id=?", (int(doc_id),))
                db.execute("DELETE FROM learned_group_use WHERE doc_id NOT IN (SELECT doc_id FROM learned)")
            db.execute("DELETE FROM learned_group_use WHERE doc_id=?", (int(doc_id),))
        return row[0] if row else None

    def disable_manual_doc(self, doc_id: int) -> int:
        """主人回复手动加的贴纸说“这个别用”：停用（不删；重新 /sticker add 即恢复）。返回停用条数。"""
        with self._lock:
            cur = self._conn().execute(
                "UPDATE library SET enabled=0, updated_at=? WHERE doc_id=? AND enabled=1",
                (float(self._clock()), int(doc_id)),
            )
        return cur.rowcount

    def remove_learned(self, label: str) -> bool:
        """删除自动条目并记墓碑（以后不会再学回来）。"""
        e = self.learned_get(label)
        if e is None:
            return False
        self.tombstone_doc(int(e["doc_id"]))
        return True

    def learn_paused(self) -> bool:
        with self._lock:
            row = self._conn().execute("SELECT value FROM learn_meta WHERE key='paused'").fetchone()
        return bool(row and row[0] == "1")

    def set_learn_paused(self, paused: bool) -> None:
        with self._lock:
            self._conn().execute(
                "INSERT INTO learn_meta(key, value) VALUES('paused', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("1" if paused else "0",),
            )

    def note_owner_message(self, is_sticker: bool, now: Optional[float] = None) -> None:
        """主人私聊一条消息（只计数，不存内容）。"""
        day = _day_key(float(self._clock() if now is None else now))
        with self._lock:
            self._conn().execute(
                "INSERT INTO owner_activity(day, msgs, stickers) VALUES(?, 1, ?)"
                " ON CONFLICT(day) DO UPDATE SET msgs=msgs+1, stickers=stickers+excluded.stickers",
                (day, 1 if is_sticker else 0),
            )

    def note_owner_scene(self, scene: str, is_sticker: bool, now: Optional[float] = None) -> None:
        """U20：主人在某场景的一条消息（scene='group:<群 id>'，按群分开；只计数，不存内容）。"""
        day = _day_key(float(self._clock() if now is None else now))
        with self._lock:
            self._conn().execute(
                "INSERT INTO owner_activity_scene(day, scene, msgs, stickers) VALUES(?, ?, 1, ?)"
                " ON CONFLICT(day, scene) DO UPDATE SET msgs=msgs+1, stickers=stickers+excluded.stickers",
                (day, str(scene), 1 if is_sticker else 0),
            )

    def owner_activity_scene(
        self, scene: str, days: int = 7, now: Optional[float] = None
    ) -> Tuple[int, int]:
        """近 days 天主人在某场景的 (消息条数, 贴纸条数)。private 读 owner_activity（与 U19 相同来源）。"""
        if scene == SCENE_PRIVATE:
            return self.owner_activity(days, now)
        t = float(self._clock() if now is None else now)
        keys = [_day_key(t - 86400.0 * i) for i in range(max(1, int(days)))]
        q = ",".join("?" for _ in keys)
        with self._lock:
            row = self._conn().execute(
                f"SELECT COALESCE(SUM(msgs),0), COALESCE(SUM(stickers),0) FROM owner_activity_scene"
                f" WHERE scene=? AND day IN ({q})",
                [str(scene), *keys],
            ).fetchone()
        return int(row[0]), int(row[1])

    def owner_activity(self, days: int = 7, now: Optional[float] = None) -> Tuple[int, int]:
        """近 days 天（含今天，UTC+8 自然日）主人私聊 (消息条数, 贴纸条数)。"""
        t = float(self._clock() if now is None else now)
        keys = [_day_key(t - 86400.0 * i) for i in range(max(1, int(days)))]
        q = ",".join("?" for _ in keys)
        with self._lock:
            row = self._conn().execute(
                f"SELECT COALESCE(SUM(msgs),0), COALESCE(SUM(stickers),0) FROM owner_activity WHERE day IN ({q})",
                keys,
            ).fetchone()
        return int(row[0]), int(row[1])

    # -- 发送记账 / 限流 ------------------------------------------------------

    def should_send(
        self, scope_key: str, now: Optional[float] = None, cooldown: Optional[float] = None
    ) -> bool:
        """上一张 bot 表情包（status=sent）在此会话距今 < cooldown 则 False。无其他频率规则。

        cooldown 为 None 时用构造时的 cooldown_seconds（默认 120 秒；U19 使用意愿打开时调用方传 60）。"""
        t = float(self._clock() if now is None else now)
        limit = self.cooldown_seconds if cooldown is None else float(cooldown)
        with self._lock:
            row = self._conn().execute(
                "SELECT MAX(created_at) FROM sends WHERE scope_key=? AND status=?",
                (str(scope_key), SEND_SENT),
            ).fetchone()
        last = row[0] if row else None
        return last is None or (t - float(last)) >= limit

    def record_send(
        self,
        scope_key: str,
        label: str,
        tg_msg_ids: Sequence[int],
        status: str = SEND_SENT,
        now: Optional[float] = None,
        *,
        source: Optional[str] = None,
        doc_id: Optional[int] = None,
    ) -> None:
        """记一次发送。计数只更新发出的那一条：source=auto → learned（按 doc_id）；否则 → library（按标签）。"""
        t = float(self._clock() if now is None else now)
        key = normalize_label(label)
        ids: List[Optional[int]] = [int(x) for x in tg_msg_ids] or [None]
        with self._lock:
            db = self._conn()
            db.execute("BEGIN IMMEDIATE")
            try:
                for mid in ids:
                    db.execute(
                        "INSERT INTO sends(scope_key, label, tg_msg_id, status, created_at)"
                        " VALUES(?,?,?,?,?)",
                        (str(scope_key), key, mid, status, t),
                    )
                if status == SEND_SENT:
                    if source == SOURCE_AUTO and doc_id is not None:
                        db.execute(
                            "UPDATE learned SET use_count=use_count+1, last_used_at=? WHERE doc_id=?",
                            (t, int(doc_id)),
                        )
                    else:
                        db.execute(
                            "UPDATE library SET use_count=use_count+1, last_used_at=? WHERE label=?",
                            (t, key),
                        )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    def refresh_document(
        self, label: str, doc: Any, *, source: Optional[str] = None, old_doc_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """file_reference 过期后回源刷新。只改发送的那一条：

        - 手动（默认）：library 按标签更新（library 的 doc_id 不唯一，不会冲突）；
        - 自动（source=auto）：learned 按原 doc_id 更新；新 doc_id 已被另一条自动条目占用时，
          删掉那条重复的再更新；新 doc_id 已在手动库里 → 自动条目让位删除，返回手动条目。"""
        key = normalize_label(label)
        fr = getattr(doc, "file_reference", None) or b""
        now = float(self._clock())
        new_id = int(doc.id)
        with self._lock:
            db = self._conn()
            if source != SOURCE_AUTO:
                db.execute(
                    "UPDATE library SET doc_id=?, access_hash=?, file_reference=?, updated_at=? WHERE label=?",
                    (new_id, int(doc.access_hash), sqlite3.Binary(bytes(fr)), now, key),
                )
                entry = self.get(key)
            else:
                old = int(old_doc_id) if old_doc_id is not None else None
                if old is None:
                    row = db.execute("SELECT doc_id FROM learned WHERE label=?", (key,)).fetchone()
                    old = int(row[0]) if row else None
                if old is None:
                    raise StickerError(f"没有叫「{key}」的表情包")
                db.execute("BEGIN IMMEDIATE")
                try:
                    if new_id != old:
                        if db.execute("SELECT 1 FROM library WHERE doc_id=?", (new_id,)).fetchone():
                            db.execute("DELETE FROM learned WHERE doc_id=?", (old,))
                            db.execute("DELETE FROM learned_group_use WHERE doc_id NOT IN (SELECT doc_id FROM learned)")
                            db.execute("COMMIT")
                            raise StickerError("这张已在手动表情库里")
                        db.execute("DELETE FROM learned WHERE doc_id=?", (new_id,))
                        db.execute("DELETE FROM learned_group_use WHERE doc_id NOT IN (SELECT doc_id FROM learned)")
                    db.execute(
                        "UPDATE learned SET doc_id=?, access_hash=?, file_reference=?, updated_at=? WHERE doc_id=?",
                        (new_id, int(doc.access_hash), sqlite3.Binary(bytes(fr)), now, old),
                    )
                    if new_id != old:
                        # 按群的使用证据跟着新 doc_id 走
                        db.execute("DELETE FROM learned_group_use WHERE doc_id=?", (new_id,))
                        db.execute("UPDATE learned_group_use SET doc_id=? WHERE doc_id=?", (new_id, old))
                    db.execute("COMMIT")
                except StickerError:
                    raise
                except Exception:
                    db.execute("ROLLBACK")
                    raise
                entry = self.learned_by_doc(new_id)
        if entry is None:
            raise StickerError(f"没有叫「{key}」的表情包")
        return entry

    # -- 发送 ----------------------------------------------------------------

    async def send(
        self,
        client: Any,
        peer: Any,
        entry: Dict[str, Any],
        *,
        scope_key: Optional[str] = None,
        reply_to: Optional[int] = None,
    ) -> List[int]:
        """经 Telethon client 发送 entry 对应的贴纸，返回 Telegram message id 列表（发送证据）。

        file_reference 过期/无效 → 回源消息刷新库内引用后重试一次。
        失败抛 StickerSendError。给了 scope_key 时记账（sent / failed）。
        """
        from telethon.tl.types import InputDocument

        label = str(entry.get("label") or "")
        if client is None:
            raise StickerSendError("telethon client 不可用")

        def _input(e: Dict[str, Any]) -> Any:
            return InputDocument(
                id=int(e["doc_id"]),
                access_hash=int(e["access_hash"]),
                file_reference=bytes(e.get("file_reference") or b""),
            )

        async def _once(e: Dict[str, Any]) -> List[int]:
            kwargs: Dict[str, Any] = {"file": _input(e)}
            if reply_to:
                kwargs["reply_to"] = reply_to
            result = await client.send_file(peer, **kwargs)
            ids = _message_ids(result)
            if not ids:
                raise StickerSendError("Telegram 没有返回消息，无法确认已发送")
            return ids

        try:
            try:
                ids = await _once(entry)
            except Exception as exc:
                if not _is_file_reference_error(exc):
                    raise
                logger.info(f"[stickers] file_reference 失效，回源刷新 label={label}")
                fresh = await self._refetch(client, entry)
                entry = {**fresh, "source": entry.get("source") or fresh.get("source")}
                ids = await _once(entry)
        except Exception as exc:
            if scope_key is not None:
                try:
                    await asyncio.to_thread(
                        lambda: self.record_send(
                            scope_key, label, [], SEND_FAILED,
                            source=entry.get("source"), doc_id=entry.get("doc_id"),
                        )
                    )
                except Exception:  # pragma: no cover
                    pass
            if isinstance(exc, StickerSendError):
                raise
            raise StickerSendError(f"表情包发送失败: {type(exc).__name__}") from exc

        if scope_key is not None:
            try:
                await asyncio.to_thread(
                    lambda: self.record_send(
                        scope_key, label, ids, SEND_SENT,
                        source=entry.get("source"), doc_id=entry.get("doc_id"),
                    )
                )
            except Exception as exc:  # 记账失败不影响已发送事实
                logger.warning(f"[stickers] 记账失败 label={label}: {type(exc).__name__}")
        return ids

    async def _refetch(self, client: Any, entry: Dict[str, Any]) -> Dict[str, Any]:
        chat_id = entry.get("source_chat_id")
        msg_id = entry.get("source_msg_id")
        if chat_id is None or msg_id is None:
            raise StickerSendError("没有来源消息，无法刷新 file_reference")
        msg = await client.get_messages(int(chat_id), ids=int(msg_id))
        if isinstance(msg, (list, tuple)):
            msg = msg[0] if msg else None
        doc = _sticker_doc(msg)
        if doc is None:
            raise StickerSendError("来源消息已不存在或不再是贴纸，请重新登记")
        return await asyncio.to_thread(
            lambda: self.refresh_document(
                str(entry["label"]), doc, source=entry.get("source"), old_doc_id=entry.get("doc_id")
            )
        )

    # -- 主人命令 ------------------------------------------------------------

    def handle_command(
        self,
        args: Sequence[str],
        replied_raw_message: Any,
        sender_id: Any,
        owner_ids: Iterable[Any],
        *,
        is_group: bool = False,
        unified: bool = False,
        learning: bool = False,
    ) -> str:
        """处理 ``/sticker <子命令>``（args 不含 'sticker' 本身），返回回复文本。

        unified=True：新加的默认 all；list / add 不再显示范围；scope 仍可改但提示已统一启用。
        learning=True（U19 自动学习开关打开）：list 显示来源（手动 / 自动）与适用场合；
        del 也能删自动条目（并记墓碑不再学）；新增 learn on|off。"""
        owners = {str(x) for x in owner_ids or ()}
        if is_group:
            return "表情包只能在和主人的私聊里管理。"
        if str(sender_id) not in owners:
            return "只有主人可以管理表情包。"
        argv = [str(a) for a in (args or []) if str(a).strip()]
        sub = argv[0].lower() if argv else "help"
        try:
            if sub in ("add", "添加"):
                if len(argv) < 2:
                    return "用法：回复一张贴纸，发 /sticker add <标签> [说明]"
                if replied_raw_message is None:
                    return "请先回复一张贴纸，再发 /sticker add <标签> [说明]"
                label = argv[1]
                note = " ".join(argv[2:])
                existed = self.get(normalize_label(label)) is not None
                e = self.add_from_message(
                    label, note, replied_raw_message, sender_id,
                    default_scope=SCOPE_ALL if unified else SCOPE_PRIVATE,
                )
                verb = "已更新" if existed else "已添加"
                scope_txt = "私聊和群" if unified or e["scope"] != SCOPE_PRIVATE else "仅私聊"
                return f"{verb}表情包「{e['label']}」（{scope_txt}）" + (
                    f"：{e['note']}" if e["note"] else ""
                )
            if learning and sub in ("list", "ls", "列表"):
                return self._list_with_learned(unified)
            if learning and sub in ("learn", "学习"):
                return self._learn_command(argv[1:])
            if learning and sub in ("del", "rm", "remove", "删除") and len(argv) >= 2:
                key = normalize_label(argv[1])
                if self.remove(key):
                    return f"已删除「{key}」"
                if self.remove_learned(key):
                    return f"已删除自动学到的「{key}」，以后也不会再学这张"
                return f"没有叫「{key}」的表情包"
            if sub in ("list", "ls", "列表"):
                rows = self.list()
                if not rows:
                    return "表情包库还是空的。回复一张贴纸发 /sticker add <标签> [说明] 来添加。"
                lines = ["表情包库："]
                for e in rows:
                    flag = "私聊和群" if unified or e["scope"] != SCOPE_PRIVATE else "仅私聊"
                    if not e["enabled"]:
                        flag += "，已停用"
                    note = f" — {e['note']}" if e["note"] else ""
                    lines.append(f"· {e['label']}{e['emoji'] and ' ' + e['emoji']}（{flag}）{note}")
                return "\n".join(lines)
            if sub in ("del", "rm", "remove", "删除"):
                if len(argv) < 2:
                    return "用法：/sticker del <标签>"
                key = normalize_label(argv[1])
                return f"已删除「{key}」" if self.remove(key) else f"没有叫「{key}」的表情包"
            if sub in ("scope", "范围"):
                if len(argv) < 3:
                    return "用法：/sticker scope <标签> private|all"
                e = self.set_scope(argv[1], argv[2])
                if unified:
                    return f"表情包已统一启用（私聊和群都能用），无需区分；「{e['label']}」的范围记录已改为 {e['scope']}。"
                scope_txt = "仅私聊" if e["scope"] == SCOPE_PRIVATE else "私聊和群"
                return f"「{e['label']}」现在可用于：{scope_txt}"
            return _HELP + (_HELP_LEARN if learning else "")
        except StickerError as exc:
            return str(exc)

    def _list_with_learned(self, unified: bool) -> str:
        manual = self.list()
        learned = self.learned_list()
        if not manual and not learned:
            return "表情包库还是空的。回复一张贴纸发 /sticker add <标签> [说明] 来添加，或者直接在私聊里发贴纸让她学。"
        lines = ["表情包库："]
        for e in manual:
            flag = "手动，" + ("私聊和群" if unified or e["scope"] != SCOPE_PRIVATE else "仅私聊")
            if not e["enabled"]:
                flag += "，已停用"
            note = f" — {e['note']}" if e["note"] else ""
            lines.append(f"· {e['label']}{e['emoji'] and ' ' + e['emoji']}（{flag}）{note}")
        for e in learned:
            flag = "自动，" + ("私聊和群" if e.get("group_ok") else "仅私聊")
            if not e["enabled"]:
                flag += "，已停用"
            note = f" — {e['note']}" if e["note"] else ""
            occ = f"；你常在{e['occasion']}用" if e.get("occasion") else ""
            if e.get("group_occasion"):
                occ += f"；你在群里常在{e['group_occasion']}用"
            elif int(e.get("group_uses") or 0) > 0:
                occ += "；你在群里用过"
            lines.append(f"· {e['label']}{e['emoji'] and ' ' + e['emoji']}（{flag}）{note}{occ}")
        lines.append("自动学习：" + ("已暂停" if self.learn_paused() else "开启中"))
        return "\n".join(lines)

    def _learn_command(self, argv: Sequence[str]) -> str:
        arg = str(argv[0]).lower() if argv else ""
        if arg in ("on", "开", "开启", "1"):
            self.set_learn_paused(False)
            return "好，我会继续从你私聊发的贴纸里学。"
        if arg in ("off", "关", "关闭", "0"):
            self.set_learn_paused(True)
            return "好，先不学新的贴纸了（已经学到的还会用，/sticker learn on 恢复）。"
        state = "已暂停" if self.learn_paused() else "开启中"
        return f"自动学习{state}。用法：/sticker learn on|off"


_HELP = (
    "表情包管理（仅主人私聊）：\n"
    "/sticker add <标签> [说明] — 回复一张贴纸来添加（同名覆盖）\n"
    "/sticker list — 查看全部\n"
    "/sticker del <标签> — 删除\n"
    "/sticker scope <标签> private|all — private 仅私聊，all 也可用于群\n"
    "标签：中文、字母或数字，最多 12 个字；说明最多 40 个字。"
)
_HELP_LEARN = (
    "\n/sticker learn on|off — 开 / 停自动学习你私聊发的贴纸\n"
    "回复一张贴纸说“别学这个”或“这个别用”，以后就不学 / 不用它。"
)
