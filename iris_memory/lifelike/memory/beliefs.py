"""user_beliefs：可推翻的用户认知（ROADMAP P1-7，设计 §6.2、§6.5）。

- 事实 / 偏好 / 习惯 / 边界 / 推测分开存；推测（inference）永远带"没确认过"。
- 置信度只用文字档 ``试探 / 较确定 / 确定``，来源决定上限（``band_ceiling``）：
  stated（主人亲口说）→ 确定；observed（多次行为观察）→ 较确定；inferred（模型推断）→ 试探，
  且必须 needs_confirmation、必须有过期时间；owner_confirmed → 等同 stated。
- 同一条消息作为证据只计一次（fresh 语义）。
- 反例 → ``counter_evidence`` 追加、降一档；降到"试探"以下 → ``disputed``，不注入。
- 新的 stated 与旧条目同话题键但说法不同 → 旧条 ``superseded``，新条 ``supersedes`` 链接。
- 过期（inference 14 天、habit 90 天、preference 180 天、fact/boundary 无）→ ``expired``，不注入。
- 每次变更写 ``belief_history``；history 快照**不含正文**（只有 kind/band/status/话题哈希），
  所以 forget 只需覆写 ``user_beliefs`` 一处。

模式（HiddenConfig ``lifelike_memory_mode``）：
- ``off``：本模块不读不写，会话抽取 prompt 与改造前逐字一致；
- ``shadow``（默认）：会话抽取顺带输出 beliefs 并入库，不注入 prompt；
- ``on``：主人私聊回复时注入 ``<你记得的她>``（经 NumericLeakGuard，不含任何数值）。

``lifelike_memory_inference_inject``：``off`` 推测一律不注入；``hedged``（默认）只注入
"较确定"及以上的推测并带"没确认过"措辞（DECISIONS D8；inferred 来源上限是"试探"，
因此只有 observed 的推测可能进入 prompt）。

日志前缀 ``[lifelike.beliefs]``，只记 id / 计数 / 状态，不记正文。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from astrbot.api import logger

from ...continuity import models as m
from ..social import privacy
from . import tombstones as tb

__all__ = [
    "BeliefStore",
    "BeliefOp",
    "ensure_beliefs_schema",
    "band_ceiling",
    "band_up",
    "band_down",
    "normalize_memory_mode",
    "validate_belief_ops",
    "render_beliefs_block",
    "BELIEF_PROMPT_RULES",
    "BELIEF_PROMPT_SCHEMA",
    "KEY_MODE",
    "KEY_INFER",
]

KEY_MODE = "lifelike_memory_mode"
KEY_INFER = "lifelike_memory_inference_inject"
MODES = ("off", "shadow", "on")

KINDS = ("fact", "preference", "inference", "habit", "stance", "boundary")
EXTRACT_KINDS = ("fact", "preference", "inference", "habit", "boundary")
SOURCES = ("stated", "observed", "inferred", "owner_confirmed", "imported")
BANDS = ("试探", "较确定", "确定")
STATUS_ACTIVE = "active"
STATUS_DISPUTED = "disputed"
STATUS_SUPERSEDED = "superseded"
STATUS_RETRACTED = "retracted"
STATUS_EXPIRED = "expired"
STATUS_DELETED = "deleted"

STATEMENT_MAX = 40
TOPIC_MAX = 20
MAX_BELIEF_OPS = 6
CANDIDATE_LIMIT = 12
INJECT_MAX_ITEMS = 8
INJECT_MAX_CHARS = 320
DAY = 86400.0
EXPIRY_S = {
    "inference": 14 * DAY,
    "habit": 90 * DAY,
    "preference": 180 * DAY,
    "stance": 90 * DAY,
}

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS user_beliefs(
  belief_id TEXT PRIMARY KEY,
  scope_key TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL,
  kind TEXT NOT NULL,
  statement TEXT NOT NULL,
  topic_key TEXT NOT NULL,
  source TEXT NOT NULL,
  evidence TEXT NOT NULL DEFAULT '[]',
  counter_evidence TEXT NOT NULL DEFAULT '[]',
  band TEXT NOT NULL,
  status TEXT NOT NULL,
  needs_confirmation INTEGER NOT NULL DEFAULT 0,
  supersedes TEXT,
  first_seen REAL NOT NULL, last_confirmed REAL NOT NULL, expires_at REAL,
  revision INTEGER NOT NULL DEFAULT 1)""",
    "CREATE INDEX IF NOT EXISTS idx_beliefs_topic ON user_beliefs(topic_key, status)",
    "CREATE INDEX IF NOT EXISTS idx_beliefs_scope ON user_beliefs(scope_key, status)",
    """CREATE TABLE IF NOT EXISTS belief_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  belief_id TEXT NOT NULL, revision INTEGER NOT NULL,
  op TEXT NOT NULL,
  by_actor TEXT NOT NULL, reason TEXT NOT NULL,
  snapshot TEXT NOT NULL,
  at REAL NOT NULL)""",
)


def ensure_beliefs_schema(store: Any) -> None:
    """建 user_beliefs / belief_history / tombstones（幂等，不动 user_version）。"""
    with tb.write_tx(store) as db:
        for stmt in _SCHEMA:
            db.execute(stmt)
    tb.ensure_tombstones_schema(store)


def normalize_memory_mode(value: Any) -> str:
    v = str(value or "").strip().lower()
    return v if v in MODES else "off"


# ---------------------------------------------------------------- 纯函数：文字档


def band_ceiling(source: str) -> str:
    if source in ("stated", "owner_confirmed"):
        return "确定"
    if source in ("observed", "imported"):
        return "较确定"
    return "试探"


def initial_band(source: str) -> str:
    """首次记录：stated 先到"较确定"（再被印证一次才到"确定"），其余直接取上限。"""
    if source == "stated":
        return "较确定"
    return band_ceiling(source)


def band_up(band: str, ceiling: str) -> str:
    i = BANDS.index(band) if band in BANDS else 0
    j = BANDS.index(ceiling) if ceiling in BANDS else 0
    return BANDS[min(i + 1, j)] if i < j else BANDS[min(i, j)]


def band_down(band: str) -> Optional[str]:
    """降一档；已是最低档 → None（= disputed）。"""
    i = BANDS.index(band) if band in BANDS else 0
    return BANDS[i - 1] if i > 0 else None


def _expiry(kind: str, now: float) -> Optional[float]:
    span = EXPIRY_S.get(kind)
    return None if span is None else now + span


def _snapshot(row: Dict[str, Any]) -> str:
    """history 快照：不含正文"""
    return json.dumps(
        {
            "kind": row.get("kind"),
            "band": row.get("band"),
            "status": row.get("status"),
            "source": row.get("source"),
            "topic_hash": tb.topic_hash(str(row.get("topic_key") or ""))[:16] if row.get("topic_key") else "",
        },
        ensure_ascii=False,
        sort_keys=True,
    )


# ---------------------------------------------------------------- 抽取 prompt 片段（会话抽取复用同一次调用）

BELIEF_PROMPT_RULES = """另外，找出关于【主人本人】、之后值得记住的认知，写进 beliefs（没有就留空数组）：
- kind：fact 稳定事实 / preference 喜好与不喜欢 / habit 习惯 / boundary 她不喜欢被怎样对待或回应 / inference 你的推测。
- inference 必须是具体、可检验的行为推测（如"她约好的事一般不会临时取消"），不要写"她很重视承诺"这类宽泛性格判断。
- 只写主人自己的事；主人提到的其他人（朋友、家人、群友）的事一律不写。
- 健康与疾病、政治、宗教、性取向、住址与精确位置、证件与账号、收入财务、具体单位或学校名称、感情与家庭矛盾，不写。
- op：new 新认知；confirm 主人的话再次印证了下方已有认知；counter 主人的话与已有认知矛盾（反例）；retract 主人明确否认了已有认知。
- confirm/counter/retract 的 belief_id 只能取自下方 <beliefs> 列表；new 不带 belief_id。
- statement 不超过 40 字，用"她"指主人；topic 是 2～10 字的话题名；evidence 必须是【主人】某条消息原文中连续出现的片段（至少 2 个字）。
- 最多 6 条。"""

BELIEF_PROMPT_SCHEMA = (
    '"beliefs":[{"op":"new|confirm|counter|retract","belief_id":"","kind":"fact|preference|habit|boundary|inference",'
    '"statement":"","topic":"","evidence":""}]'
)


def candidates_block(rows: Sequence[Dict[str, Any]]) -> str:
    lines = ["<beliefs>"]
    if not rows:
        lines.append("（无）")
    for r in rows:
        lines.append(
            json.dumps(
                {"belief_id": r["belief_id"], "kind": r["kind"], "statement": r["statement"], "topic": r["topic_key"]},
                ensure_ascii=False,
            )
        )
    lines.append("</beliefs>")
    return "\n".join(lines)


# ---------------------------------------------------------------- 校验


@dataclass
class BeliefOp:
    op: str
    kind: str = ""
    statement: str = ""
    topic_key: str = ""
    source: str = "stated"
    belief_id: str = ""
    evidence_ref: str = ""  # "scope:msg_id"


@dataclass
class ValidateReport:
    ops: List[BeliefOp] = field(default_factory=list)
    invalid: int = 0
    sensitive: int = 0
    tombstoned: int = 0


def _find_msg(evidence: str, owner_rows: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    ev = str(evidence or "").strip()
    if len(ev) < 2:
        return None
    for r in owner_rows:
        if ev in str(r.get("_text") or r.get("text") or ""):
            return r
    return None


def validate_belief_ops(
    raw_ops: Any,
    owner_rows: Sequence[Dict[str, Any]],
    candidate_ids: Iterable[str],
    *,
    scope_key: str,
    tombstone_hit: Optional[Callable[..., bool]] = None,
) -> ValidateReport:
    """模型输出 → 合法 op。evidence 只能在主人消息里定位（她的话永远不是主人的认知）。"""
    rep = ValidateReport()
    if not isinstance(raw_ops, list):
        return rep
    cands = set(candidate_ids)
    rep.invalid += max(0, len(raw_ops) - MAX_BELIEF_OPS)
    for raw in raw_ops[:MAX_BELIEF_OPS]:
        if not isinstance(raw, dict):
            rep.invalid += 1
            continue
        op = str(raw.get("op") or "").strip().lower()
        msg = _find_msg(str(raw.get("evidence") or ""), owner_rows)
        if op not in ("new", "confirm", "counter", "retract") or msg is None:
            rep.invalid += 1
            continue
        ref = f"{scope_key}:{int(msg['msg_id'])}"
        if op != "new":
            bid = str(raw.get("belief_id") or "").strip()
            if bid not in cands:
                rep.invalid += 1
                continue
            rep.ops.append(BeliefOp(op=op, belief_id=bid, evidence_ref=ref))
            continue
        kind = str(raw.get("kind") or "").strip().lower()
        statement = str(raw.get("statement") or "").strip()
        topic_key = m.norm_topic(str(raw.get("topic") or ""))[:TOPIC_MAX]
        if kind not in EXTRACT_KINDS or not statement or len(statement) > STATEMENT_MAX or len(topic_key) < 2:
            rep.invalid += 1
            continue
        if privacy.is_sensitive(statement) or privacy.is_sensitive(str(raw.get("topic") or "")):
            rep.sensitive += 1
            continue
        try:
            from ..guards.numeric import scan as numeric_scan

            if numeric_scan(statement, strict=True):
                rep.invalid += 1
                continue
        except Exception:  # noqa: BLE001 - 守卫不可用时不阻断（注入时还会再过一次）
            pass
        if tombstone_hit is not None and tombstone_hit(statement, topic_key):
            rep.tombstoned += 1
            continue
        source = "inferred" if kind == "inference" else ("observed" if kind == "habit" else "stated")
        rep.ops.append(
            BeliefOp(op="new", kind=kind, statement=statement, topic_key=topic_key, source=source, evidence_ref=ref)
        )
    return rep


# ---------------------------------------------------------------- 存储


def _loads(raw: Any) -> List[str]:
    try:
        v = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(x) for x in v] if isinstance(v, list) else []


class BeliefStore:
    """user_beliefs 读写（同步，调用方放 to_thread）。"""

    def __init__(self, store: Any, clock: Callable[[], float] = time.time) -> None:
        self._store = store
        self._clock = clock
        self._ready = False

    def ensure(self) -> None:
        if not self._ready:
            ensure_beliefs_schema(self._store)
            self._ready = True

    # ------------------------------------------------------------ reads
    def get(self, belief_id: str) -> Optional[Dict[str, Any]]:
        self.ensure()
        rows = tb.read_rows(self._store, "SELECT * FROM user_beliefs WHERE belief_id=?", (belief_id,))
        return rows[0] if rows else None

    def list(
        self, scope_key: Optional[str] = None, statuses: Sequence[str] = (STATUS_ACTIVE,), limit: int = 50
    ) -> List[Dict[str, Any]]:
        self.ensure()
        sql = "SELECT * FROM user_beliefs WHERE 1=1"
        args: List[Any] = []
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args.extend(statuses)
        if scope_key is not None:
            sql += " AND scope_key=?"
            args.append(scope_key)
        sql += " ORDER BY last_confirmed DESC LIMIT ?"
        args.append(int(limit))
        return tb.read_rows(self._store, sql, args)

    def candidates(self, scope_key: str) -> List[Dict[str, Any]]:
        return self.list(scope_key, (STATUS_ACTIVE,), CANDIDATE_LIMIT)

    def history(self, belief_id: str) -> List[Dict[str, Any]]:
        self.ensure()
        return tb.read_rows(self._store, "SELECT * FROM belief_history WHERE belief_id=? ORDER BY id", (belief_id,))

    # ------------------------------------------------------------ writes
    def _hist(self, db: Any, row: Dict[str, Any], op: str, actor: str, reason: str, now: float) -> None:
        db.execute(
            "INSERT INTO belief_history(belief_id,revision,op,by_actor,reason,snapshot,at) VALUES (?,?,?,?,?,?,?)",
            (row["belief_id"], int(row.get("revision") or 1), op, actor, reason[:40], _snapshot(row), now),
        )

    def _update(self, db: Any, row: Dict[str, Any], changes: Dict[str, Any]) -> Dict[str, Any]:
        new = {**row, **changes, "revision": int(row.get("revision") or 1) + 1}
        cols = [*changes.keys(), "revision"]
        db.execute(
            f"UPDATE user_beliefs SET {', '.join(f'{c}=?' for c in cols)} WHERE belief_id=?",
            [new[c] for c in cols] + [row["belief_id"]],
        )
        return new

    def add(
        self,
        scope_key: str,
        kind: str,
        statement: str,
        topic_key: str,
        source: str,
        evidence_ref: str,
        *,
        actor: str = "session_extract",
        subject: str = "owner",
    ) -> Dict[str, Any]:
        """新认知；同话题键已有 active：说法相同 → confirm；stated 说法不同 → supersede；否则并存。"""
        self.ensure()
        now = float(self._clock())
        with tb.write_tx(self._store) as db:
            olds = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM user_beliefs WHERE scope_key=? AND topic_key=? AND status=? AND subject=?",
                    (scope_key, topic_key, STATUS_ACTIVE, subject),
                ).fetchall()
            ]
            for old in olds:
                if m.norm_topic(old["statement"]) == m.norm_topic(statement):
                    return self._confirm_row(db, old, evidence_ref, actor, now)
            supersedes = None
            if source in ("stated", "owner_confirmed"):
                for old in olds:
                    if old["kind"] in (kind, "inference") or kind in ("fact", "preference"):
                        new_old = self._update(db, old, {"status": STATUS_SUPERSEDED})
                        self._hist(db, new_old, "supersede", actor, "new_stated", now)
                        supersedes = old["belief_id"]
            row = {
                "belief_id": "b_" + uuid.uuid4().hex[:12],
                "scope_key": scope_key,
                "subject": subject,
                "kind": kind,
                "statement": statement[:STATEMENT_MAX],
                "topic_key": topic_key[:TOPIC_MAX],
                "source": source,
                "evidence": json.dumps([evidence_ref] if evidence_ref else []),
                "counter_evidence": "[]",
                "band": initial_band(source),
                "status": STATUS_ACTIVE,
                "needs_confirmation": 1 if source == "inferred" else 0,
                "supersedes": supersedes,
                "first_seen": now,
                "last_confirmed": now,
                "expires_at": _expiry(kind, now),
                "revision": 1,
            }
            cols = list(row.keys())
            db.execute(
                f"INSERT INTO user_beliefs({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [row[c] for c in cols],
            )
            self._hist(db, row, "add", actor, source, now)
            return row

    def _confirm_row(self, db: Any, row: Dict[str, Any], evidence_ref: str, actor: str, now: float) -> Dict[str, Any]:
        ev = _loads(row.get("evidence"))
        if evidence_ref and evidence_ref in ev:
            return row  # 同一条消息只计一次
        if evidence_ref:
            ev.append(evidence_ref)
        changes = {
            "evidence": json.dumps(ev[-10:]),
            "band": band_up(row["band"], band_ceiling(row["source"])),
            "last_confirmed": now,
            "expires_at": _expiry(row["kind"], now),
        }
        new = self._update(db, row, changes)
        self._hist(db, new, "confirm", actor, "evidence", now)
        return new

    def confirm(self, belief_id: str, evidence_ref: str, actor: str = "session_extract") -> Optional[Dict[str, Any]]:
        self.ensure()
        now = float(self._clock())
        with tb.write_tx(self._store) as db:
            row = db.execute("SELECT * FROM user_beliefs WHERE belief_id=?", (belief_id,)).fetchone()
            if row is None or row["status"] != STATUS_ACTIVE:
                return None
            return self._confirm_row(db, dict(row), evidence_ref, actor, now)

    def counter(self, belief_id: str, evidence_ref: str, actor: str = "session_extract") -> Optional[Dict[str, Any]]:
        """反例：降一档；已是"试探" → disputed（不注入）。"""
        self.ensure()
        now = float(self._clock())
        with tb.write_tx(self._store) as db:
            raw = db.execute("SELECT * FROM user_beliefs WHERE belief_id=?", (belief_id,)).fetchone()
            if raw is None or raw["status"] != STATUS_ACTIVE:
                return None
            row = dict(raw)
            ce = _loads(row.get("counter_evidence"))
            if evidence_ref and evidence_ref in ce:
                return row
            if evidence_ref:
                ce.append(evidence_ref)
            down = band_down(row["band"])
            changes: Dict[str, Any] = {"counter_evidence": json.dumps(ce[-10:])}
            if down is None:
                changes["status"] = STATUS_DISPUTED
            else:
                changes["band"] = down
            new = self._update(db, row, changes)
            self._hist(db, new, "counter", actor, "counter_evidence", now)
            return new

    def set_status(self, belief_id: str, status: str, op: str, actor: str, reason: str = "") -> Optional[Dict[str, Any]]:
        self.ensure()
        now = float(self._clock())
        with tb.write_tx(self._store) as db:
            raw = db.execute("SELECT * FROM user_beliefs WHERE belief_id=?", (belief_id,)).fetchone()
            if raw is None or raw["status"] == STATUS_DELETED:
                return None
            new = self._update(db, dict(raw), {"status": status})
            self._hist(db, new, op, actor, reason or op, now)
            return new

    def retract(self, belief_id: str, actor: str = "owner") -> Optional[Dict[str, Any]]:
        return self.set_status(belief_id, STATUS_RETRACTED, "retract", actor)

    def owner_confirm(self, belief_id: str) -> Optional[Dict[str, Any]]:
        """主人确认：来源升为 owner_confirmed，档位"确定"，不再需要确认；推测改记为事实。"""
        self.ensure()
        now = float(self._clock())
        with tb.write_tx(self._store) as db:
            raw = db.execute("SELECT * FROM user_beliefs WHERE belief_id=?", (belief_id,)).fetchone()
            if raw is None or raw["status"] not in (STATUS_ACTIVE, STATUS_DISPUTED):
                return None
            row = dict(raw)
            kind = "fact" if row["kind"] == "inference" else row["kind"]
            new = self._update(
                db,
                row,
                {
                    "source": "owner_confirmed",
                    "band": "确定",
                    "status": STATUS_ACTIVE,
                    "needs_confirmation": 0,
                    "kind": kind,
                    "last_confirmed": now,
                    "expires_at": _expiry(kind, now),
                },
            )
            self._hist(db, new, "confirm", "owner", "owner_confirmed", now)
            return new

    def expire_due(self) -> int:
        """维护：active 且过期 → expired"""
        self.ensure()
        now = float(self._clock())
        n = 0
        with tb.write_tx(self._store) as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM user_beliefs WHERE status=? AND expires_at IS NOT NULL AND expires_at<?",
                    (STATUS_ACTIVE, now),
                ).fetchall()
            ]
            for row in rows:
                new = self._update(db, row, {"status": STATUS_EXPIRED})
                self._hist(db, new, "expire", "maintenance", "ttl", now)
                n += 1
        return n

    def delete(self, belief_ids: Sequence[str], actor: str = "owner_forget") -> int:
        """forget：status=deleted，正文覆写为空，保留 id 与时间"""
        self.ensure()
        now = float(self._clock())
        n = 0
        with tb.write_tx(self._store) as db:
            for bid in belief_ids:
                raw = db.execute("SELECT * FROM user_beliefs WHERE belief_id=?", (bid,)).fetchone()
                if raw is None or raw["status"] == STATUS_DELETED:
                    continue
                new = self._update(
                    db,
                    dict(raw),
                    {"status": STATUS_DELETED, "statement": "", "topic_key": "", "evidence": "[]", "counter_evidence": "[]"},
                )
                db.execute(
                    "INSERT INTO belief_history(belief_id,revision,op,by_actor,reason,snapshot,at) VALUES (?,?,?,?,?,?,?)",
                    (bid, new["revision"], "delete", actor, "forget", json.dumps({"kind": new["kind"]}), now),
                )
                n += 1
        return n

    def search(self, keyword: str, scope_key: Optional[str] = None) -> List[Dict[str, Any]]:
        self.ensure()
        kw = str(keyword or "").strip()
        norm = m.norm_topic(kw)
        if len(norm) < 2:
            return []
        sql = "SELECT * FROM user_beliefs WHERE status!=? AND (statement LIKE ? OR topic_key LIKE ?)"
        args: List[Any] = [STATUS_DELETED, f"%{kw}%", f"%{norm}%"]
        if scope_key is not None:
            sql += " AND scope_key=?"
            args.append(scope_key)
        return tb.read_rows(self._store, sql, args)

    # ------------------------------------------------------------ apply extractor ops
    def apply_ops(self, scope_key: str, ops: Sequence[BeliefOp]) -> Dict[str, int]:
        out = {"added": 0, "confirmed": 0, "countered": 0, "retracted": 0, "skipped": 0}
        for op in ops:
            try:
                if op.op == "new":
                    self.add(scope_key, op.kind, op.statement, op.topic_key, op.source, op.evidence_ref)
                    out["added"] += 1
                elif op.op == "confirm":
                    out["confirmed" if self.confirm(op.belief_id, op.evidence_ref) else "skipped"] += 1
                elif op.op == "counter":
                    out["countered" if self.counter(op.belief_id, op.evidence_ref) else "skipped"] += 1
                elif op.op == "retract":
                    row = self.get(op.belief_id)
                    if row is not None and row["status"] == STATUS_ACTIVE:
                        self.set_status(op.belief_id, STATUS_RETRACTED, "retract", "owner_said")
                        out["retracted"] += 1
                    else:
                        out["skipped"] += 1
            except Exception as e:  # noqa: BLE001
                out["skipped"] += 1
                logger.warning(f"[lifelike.beliefs] apply op={op.op} failed: {type(e).__name__}")
        return out


# ---------------------------------------------------------------- 注入


_SOURCE_TAG = {"stated": "她自己说的", "owner_confirmed": "她确认过", "observed": "你观察到的", "imported": "以前记下的"}


def render_beliefs_block(rows: Sequence[Dict[str, Any]], *, inference_mode: str = "hedged", now: Optional[float] = None) -> str:
    """``<你记得的她>``：按 kind 分组；过期 / disputed / 非 active 不注入；推测只在 hedged 且"较确定"以上。

    不含任何数字分值；置信度以措辞体现。
    """
    now = float(now if now is not None else time.time())
    said: List[str] = []
    seen: List[str] = []
    bounds: List[str] = []
    guesses: List[str] = []
    for r in rows:
        if r.get("status") != STATUS_ACTIVE or r.get("subject") != "owner":
            continue
        if r.get("expires_at") is not None and float(r["expires_at"]) < now:
            continue
        stmt = str(r.get("statement") or "").strip()
        if not stmt or privacy.is_sensitive(stmt):
            continue
        kind = r.get("kind")
        band = r.get("band")
        if kind == "inference" or r.get("needs_confirmation"):
            if inference_mode != "hedged" or band not in ("较确定", "确定"):
                continue
            guesses.append(stmt)
        elif kind == "boundary":
            bounds.append(stmt)
        elif kind == "habit" or r.get("source") == "observed":
            seen.append(stmt + ("" if band == "确定" else "（大概）"))
        elif kind in ("fact", "preference"):
            tag = _SOURCE_TAG.get(str(r.get("source")), "")
            said.append(stmt + (f"（{tag}）" if tag else ""))
    lines: List[str] = []
    budget = INJECT_MAX_CHARS
    count = 0

    def take(items: List[str]) -> List[str]:
        nonlocal budget, count
        out: List[str] = []
        for it in items:
            if count >= INJECT_MAX_ITEMS or len(it) + 1 > budget:
                break
            out.append(it)
            budget -= len(it) + 1
            count += 1
        return out

    for label, items, tail in (
        ("她说过的：", take(said), ""),
        ("别这样对她：", take(bounds), ""),
        ("你观察到的：", take(seen), ""),
        ("你感觉（没确认过）：", take(guesses), "——别当成事实说出口，也别直接问她是不是这样。"),
    ):
        if items:
            lines.append(label + "；".join(items) + "。" + tail)
    if not lines:
        return ""
    return "\n".join(["<你记得的她>", *lines, "</你记得的她>"])
