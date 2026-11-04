"""主动候选池存储 + 决策记录 + 打分（设计 §7.2、§7.4、§7.5、§7.7）

全部是同步方法（在 ``asyncio.to_thread`` 里调用），直接用 ContinuityStore 的连接与锁。
只有后台服务能入队（LLM 不能入队）。日志不在这里写。
"""

from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .gate import CTYPE_FOLLOWUP, CTYPE_GROUP_CARE, DELIVERY_PASSIVE, DELIVERY_PUSH, PRIVACY_OWNER_ONLY
from .schema import ensure_initiative_schema

CAND_PREFIX = "cand:"
ITEM_PREFIX = "item:"

ST_QUEUED = "queued"
ST_OFFERED = "offered"
ST_USED = "used"
ST_DECLINED = "declined"
ST_EXPIRED = "expired"
ST_BLOCKED = "blocked"
ST_SUPPRESSED = "suppressed"
OPEN_STATUSES = (ST_QUEUED, ST_OFFERED)
TERMINAL_STATUSES = (ST_USED, ST_DECLINED, ST_EXPIRED, ST_BLOCKED, ST_SUPPRESSED)
CAND_STATUSES = frozenset(OPEN_STATUSES + TERMINAL_STATUSES)

CTYPES = frozenset(
    {
        "followup_item", "life_share", "moment_followup", "owner_post", "group_topic",
        "confirm_inference", "next_opening", "group_care",
    }
)

WHY_NOW_MAX = 40
MATERIAL_MAX = 120

# 打分权重（设计 §7.4；只排序、不进 prompt）
W_TYPE = {
    "followup_item": 6.0,
    "group_care": 5.0,
    "moment_followup": 4.0,
    "owner_post": 3.0,
    "life_share": 2.0,
    "group_topic": 1.5,
    "confirm_inference": 1.0,
    "next_opening": 1.0,
}
W_FRESH = 2.0
W_RESP = 2.0
W_FATIGUE = 1.0
FRESH_HALF_LIFE_S = 12 * 3600.0


def clip(text: Any, limit: int) -> str:
    s = " ".join(str(text or "").split())
    return s[:limit]


@dataclass
class InitiativeCandidate:
    scope_key: str
    ctype: str
    dedupe_key: str
    why_now: str
    material: str
    expires_at: float
    source_ref: List[str] = field(default_factory=list)
    privacy: str = PRIVACY_OWNER_ONLY
    delivery: str = DELIVERY_PUSH
    not_before: float = 0.0
    cand_id: str = ""


def _row(row: Any) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    d = dict(row)
    if "source_ref" in d:
        try:
            d["source_ref"] = json.loads(d.get("source_ref") or "[]")
        except (TypeError, ValueError):
            d["source_ref"] = []
    for k in ("ranked", "style_hits"):
        if k in d:
            try:
                d[k] = json.loads(d.get(k) or "[]")
            except (TypeError, ValueError):
                d[k] = []
    return d


class CandidatePool:
    """initiative_candidates / initiative_decisions 的读写（同步，线程安全由 store 锁保证）"""

    def __init__(self, store: Any, clock: Callable[[], float] = time.time) -> None:
        self._store = store
        self._clock = clock

    def _now(self) -> float:
        return float(self._clock())

    def _ensure(self) -> None:
        ensure_initiative_schema(self._store)

    def _q(self, sql: str, args: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        self._ensure()
        with self._store._lock:
            rows = self._store._conn().execute(sql, list(args)).fetchall()
        return [_row(r) for r in rows]  # type: ignore[misc]

    def _one(self, sql: str, args: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        rows = self._q(sql, args)
        return rows[0] if rows else None

    # ------------------------------------------------------------ candidates

    def enqueue(self, c: InitiativeCandidate) -> Optional[str]:
        """入队；同 dedupe_key 已存在（任意状态）→ None。scope 必须是私聊。"""
        if not str(c.scope_key).startswith("private:"):
            raise ValueError("initiative candidates are owner-private only")
        if c.ctype not in CTYPES:
            raise ValueError(f"unknown ctype {c.ctype}")
        if c.delivery not in (DELIVERY_PUSH, DELIVERY_PASSIVE):
            raise ValueError("bad delivery")
        if c.ctype == CTYPE_GROUP_CARE and c.privacy != PRIVACY_OWNER_ONLY:
            raise ValueError("group_care must be owner_only")
        self._ensure()
        now = self._now()
        cand_id = c.cand_id or uuid.uuid4().hex[:16]
        with self._store._tx() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO initiative_candidates(cand_id,scope_key,ctype,dedupe_key,why_now,"
                "material,source_ref,privacy,delivery,not_before,expires_at,status,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cand_id,
                    c.scope_key,
                    c.ctype,
                    c.dedupe_key,
                    clip(c.why_now, WHY_NOW_MAX),
                    clip(c.material, MATERIAL_MAX),
                    json.dumps(list(c.source_ref or []), ensure_ascii=False),
                    c.privacy,
                    c.delivery,
                    float(c.not_before or 0.0),
                    float(c.expires_at),
                    ST_QUEUED,
                    now,
                    now,
                ),
            )
            if cur.rowcount != 1:
                return None
        return cand_id

    def get(self, cand_id: str) -> Optional[Dict[str, Any]]:
        cid = str(cand_id or "")
        if cid.startswith(CAND_PREFIX):
            cid = cid[len(CAND_PREFIX):]
        return self._one("SELECT * FROM initiative_candidates WHERE cand_id=?", (cid,))

    def open(
        self, scope_key: str, now: float, delivery: Optional[str] = None, ctype: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        sql = (
            "SELECT * FROM initiative_candidates WHERE scope_key=? AND status IN (?,?)"
            " AND expires_at>? AND not_before<=?"
        )
        args: List[Any] = [scope_key, ST_QUEUED, ST_OFFERED, float(now), float(now)]
        if delivery:
            sql += " AND delivery=?"
            args.append(delivery)
        if ctype:
            sql += " AND ctype=?"
            args.append(ctype)
        return self._q(sql + " ORDER BY created_at, cand_id", args)

    def set_status(
        self, cand_id: str, status: str, *, only_from: Iterable[str] = (), offered_at: Optional[float] = None
    ) -> bool:
        if status not in CAND_STATUSES:
            raise ValueError(f"bad status {status}")
        cid = str(cand_id or "")
        if cid.startswith(CAND_PREFIX):
            cid = cid[len(CAND_PREFIX):]
        self._ensure()
        sql = "UPDATE initiative_candidates SET status=?, updated_at=?"
        args: List[Any] = [status, self._now()]
        if offered_at is not None:
            sql += ", offered_at=COALESCE(offered_at, ?)"
            args.append(float(offered_at))
        sql += " WHERE cand_id=?"
        args.append(cid)
        froms = list(only_from)
        if froms:
            sql += f" AND status IN ({','.join('?' * len(froms))})"
            args += froms
        with self._store._tx() as db:
            return db.execute(sql, args).rowcount == 1

    def set_delivery(self, cand_id: str, delivery: str) -> bool:
        if delivery not in (DELIVERY_PUSH, DELIVERY_PASSIVE):
            raise ValueError("bad delivery")
        self._ensure()
        with self._store._tx() as db:
            return db.execute(
                "UPDATE initiative_candidates SET delivery=?, updated_at=? WHERE cand_id=? AND status IN (?,?)",
                (delivery, self._now(), str(cand_id), ST_QUEUED, ST_OFFERED),
            ).rowcount == 1

    def suppress_open(self, scope_key: str, ctype: str, status: str = ST_SUPPRESSED) -> List[str]:
        """把某类型的全部未结候选置为终态（例如关心桥关闭）；返回 cand_id 列表"""
        rows = self._q(
            "SELECT cand_id FROM initiative_candidates WHERE scope_key=? AND ctype=? AND status IN (?,?)",
            (scope_key, ctype, ST_QUEUED, ST_OFFERED),
        )
        ids = [r["cand_id"] for r in rows]
        for cid in ids:
            self.set_status(cid, status, only_from=OPEN_STATUSES)
        return ids

    def expire(self, now: float) -> List[Dict[str, Any]]:
        rows = self._q(
            "SELECT cand_id, ctype, source_ref FROM initiative_candidates WHERE status IN (?,?) AND expires_at<=?",
            (ST_QUEUED, ST_OFFERED, float(now)),
        )
        for r in rows:
            self.set_status(r["cand_id"], ST_EXPIRED, only_from=OPEN_STATUSES)
        return rows

    def recent_terminal(self, scope_key: str, ctype: str, statuses: Sequence[str], since: float) -> List[Dict[str, Any]]:
        sts = list(statuses)
        return self._q(
            f"SELECT * FROM initiative_candidates WHERE scope_key=? AND ctype=? AND status IN ({','.join('?' * len(sts))})"
            " AND updated_at>=? ORDER BY updated_at DESC",
            [scope_key, ctype, *sts, float(since)],
        )

    # ------------------------------------------------------------ decisions

    def record_decision(self, **fields: Any) -> int:
        self._ensure()
        cols = {
            "ts": float(fields.get("ts") or self._now()),
            "scope_key": str(fields["scope_key"]),
            "turn_id": fields.get("turn_id"),
            "mode": str(fields.get("mode") or "on"),
            "gate_result": str(fields.get("gate_result") or ""),
            "ranked": json.dumps(fields.get("ranked") or [], ensure_ascii=False),
            "decision": str(fields.get("decision") or ""),
            "outcome": str(fields.get("outcome") or "")[:80],
            "chosen_cand": fields.get("chosen_cand"),
            "chosen_ctype": str(fields.get("chosen_ctype") or ""),
            "why_now": clip(fields.get("why_now"), WHY_NOW_MAX),
            "style_hits": json.dumps(fields.get("style_hits") or [], ensure_ascii=False),
            "sent_at": fields.get("sent_at"),
        }
        names = list(cols)
        with self._store._tx() as db:
            cur = db.execute(
                f"INSERT INTO initiative_decisions({','.join(names)}) VALUES ({','.join('?' * len(names))})",
                [cols[n] for n in names],
            )
            return int(cur.lastrowid)

    def settle_decision(self, dec_id: int, replied: bool, latency_band: str = "", tone: str = "") -> None:
        self._ensure()
        with self._store._tx() as db:
            db.execute(
                "UPDATE initiative_decisions SET replied=?, reply_latency_band=?, reply_tone=?, settled_at=?"
                " WHERE id=? AND settled_at IS NULL",
                (1 if replied else 0, latency_band or None, tone or None, self._now(), int(dec_id)),
            )

    def unsettled_sent(self, scope_key: str) -> List[Dict[str, Any]]:
        return self._q(
            "SELECT * FROM initiative_decisions WHERE scope_key=? AND sent_at IS NOT NULL AND settled_at IS NULL"
            " ORDER BY sent_at",
            (scope_key,),
        )

    def sent_since(self, scope_key: str, since: float) -> List[Dict[str, Any]]:
        """since 之后真正发出的主动（mode=on），新→旧"""
        return self._q(
            "SELECT * FROM initiative_decisions WHERE scope_key=? AND sent_at IS NOT NULL AND sent_at>=?"
            " AND mode='on' ORDER BY sent_at DESC",
            (scope_key, float(since)),
        )

    def last_sent(self, scope_key: str) -> Optional[Dict[str, Any]]:
        return self._one(
            "SELECT * FROM initiative_decisions WHERE scope_key=? AND sent_at IS NOT NULL AND mode='on'"
            " ORDER BY sent_at DESC LIMIT 1",
            (scope_key,),
        )

    def decision_for_turn(self, turn_id: str) -> Optional[Dict[str, Any]]:
        return self._one(
            "SELECT * FROM initiative_decisions WHERE turn_id=? ORDER BY id DESC LIMIT 1", (str(turn_id),)
        )

    def offered_push(self) -> List[Dict[str, Any]]:
        """停在 offered 的推送候选（崩溃遗留检查用）"""
        return self._q(
            "SELECT * FROM initiative_candidates WHERE status=? AND delivery=? ORDER BY created_at",
            (ST_OFFERED, DELIVERY_PUSH),
        )

    def latest_turn_for_item(self, item_id: str) -> Optional[Dict[str, Any]]:
        """proactive_turns 里选中该条目的最近一个回合（只读）"""
        return self._one(
            "SELECT turn_id, scope_key, item_id, state, reason, updated_at FROM proactive_turns"
            " WHERE item_id=? ORDER BY created_at DESC LIMIT 1",
            (str(item_id),),
        )

    def recent_decisions(self, scope_key: str, limit: int = 10) -> List[Dict[str, Any]]:
        return self._q(
            "SELECT * FROM initiative_decisions WHERE scope_key=? ORDER BY ts DESC, id DESC LIMIT ?",
            (scope_key, max(1, int(limit))),
        )

    def response_stats(self, scope_key: str, since: float) -> Dict[str, Tuple[int, int]]:
        """ctype → (已结算发送数, 其中被回复数)"""
        rows = self._q(
            "SELECT chosen_ctype, COUNT(*) AS n, SUM(CASE WHEN replied=1 THEN 1 ELSE 0 END) AS r"
            " FROM initiative_decisions WHERE scope_key=? AND settled_at IS NOT NULL AND sent_at>=?"
            " GROUP BY chosen_ctype",
            (scope_key, float(since)),
        )
        return {str(r["chosen_ctype"] or ""): (int(r["n"] or 0), int(r["r"] or 0)) for r in rows}


# ---------------------------------------------------------------- scorer


class CandidateScorer:
    """内部打分，只用于排序前 k 名；分数只写 initiative_decisions.ranked，不进 prompt"""

    @staticmethod
    def score(
        cand: Dict[str, Any],
        now: float,
        response: Dict[str, Tuple[int, int]],
        fatigue: Dict[str, int],
    ) -> float:
        ctype = str(cand.get("ctype") or "")
        base = W_TYPE.get(ctype, 0.5)
        age = max(0.0, now - float(cand.get("created_at") or now))
        fresh = math.exp(-math.log(2) * age / FRESH_HALF_LIFE_S)
        n, r = response.get(ctype, (0, 0))
        resp = (r + 1.0) / (n + 2.0)  # 平滑回复率
        return round(base + W_FRESH * fresh + W_RESP * resp - W_FATIGUE * fatigue.get(ctype, 0), 4)

    def rank(
        self,
        cands: Sequence[Dict[str, Any]],
        now: float,
        response: Optional[Dict[str, Tuple[int, int]]] = None,
        fatigue: Optional[Dict[str, int]] = None,
        k: int = 3,
    ) -> List[Tuple[Dict[str, Any], float]]:
        response = response or {}
        fatigue = fatigue or {}
        scored = [(c, self.score(c, now, response, fatigue)) for c in cands]
        scored.sort(key=lambda x: (-x[1], float(x[0].get("created_at") or 0.0), str(x[0].get("cand_id") or "")))
        return scored[: max(0, int(k))]


__all__ = [
    "CAND_PREFIX",
    "ITEM_PREFIX",
    "CTYPE_FOLLOWUP",
    "CandidatePool",
    "CandidateScorer",
    "InitiativeCandidate",
]
