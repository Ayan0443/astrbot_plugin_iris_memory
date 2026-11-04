"""ForgetService："忘掉这件事"（ROADMAP P1-7，设计 §6.6）。

两步：``preview(关键词)`` 只给各层**数量**（不打印原文）并返回确认码；``purge(确认码)`` 才执行：

- user_beliefs → ``status=deleted``，正文覆写为空（BeliefStore.delete）；
- items → ``status=cancelled``、``closed_reason=forgotten``，topic/summary/source_refs 覆写，
  dedupe_key 改为不含话题的占位；item_history 中该事项的快照一并覆写为 ``{}``；
- L2 → 现有 ``delete_entries``（同步清 FAISS/FTS），归档表里的同类条目用 ``delete_archived_memory``；
- learning few-shot → ``delete_rows("few_shot", ids)``；
- 写 ``tombstones(topic)``：之后的抽取（逐条快路径、会话抽取、认知）遇到含该关键词的话题直接跳过。

原生历史与 ``inbound`` 原文不动（那是聊天记录本身；``--hard`` 未实现）。画像字段不在本服务范围。
只处理属于该主人的数据（scope / user_id 匹配）。日志只记 id 数量。
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from astrbot.api import logger

from ...continuity import models as m
from . import tombstones as tb
from .beliefs import BeliefStore

__all__ = ["ForgetService", "ForgetPreview", "KEYWORD_MAX"]

KEYWORD_MIN = 2
KEYWORD_MAX = tb.SUB_MAX
PREVIEW_TTL_S = 600.0
L2_SCAN_MAX = 5000


@dataclass
class ForgetPreview:
    token: str
    owner_id: str
    keyword: str
    created_at: float
    belief_ids: List[str] = field(default_factory=list)
    item_ids: List[str] = field(default_factory=list)
    l2_ids: List[str] = field(default_factory=list)
    l2_archive_ids: List[str] = field(default_factory=list)
    few_shot_ids: List[int] = field(default_factory=list)

    def counts(self) -> Dict[str, int]:
        return {
            "beliefs": len(self.belief_ids),
            "items": len(self.item_ids),
            "l2": len(self.l2_ids) + len(self.l2_archive_ids),
            "learning": len(self.few_shot_ids),
        }


def _owner_of(meta: Dict[str, Any], owner_id: str) -> bool:
    if str(meta.get("user_id") or "") == owner_id:
        return True
    active = meta.get("active_users")
    if isinstance(active, str):
        return owner_id in [a.strip() for a in active.split(",")]
    if isinstance(active, list):
        return owner_id in [str(a) for a in active]
    return False


class ForgetService:
    def __init__(
        self,
        store: Any,
        beliefs: BeliefStore,
        *,
        l2_get: Optional[Callable[[], Any]] = None,
        learning_get: Optional[Callable[[], Any]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._beliefs = beliefs
        self._l2_get = l2_get
        self._learning_get = learning_get
        self._clock = clock
        self._pending: Dict[str, ForgetPreview] = {}

    @staticmethod
    def valid_keyword(keyword: str) -> bool:
        n = len(m.norm_topic(keyword))
        return KEYWORD_MIN <= n <= KEYWORD_MAX

    # ------------------------------------------------------------ preview
    async def preview(self, owner_id: str, keyword: str) -> Optional[ForgetPreview]:
        keyword = str(keyword or "").strip()
        if not self.valid_keyword(keyword):
            return None
        owner_id = str(owner_id)
        scope = f"private:{owner_id}"
        now = float(self._clock())
        pv = ForgetPreview(token=secrets.token_hex(3), owner_id=owner_id, keyword=keyword, created_at=now)
        pv.belief_ids = [r["belief_id"] for r in await asyncio.to_thread(self._beliefs.search, keyword, scope)]
        pv.item_ids = await asyncio.to_thread(self._find_items, owner_id, scope, keyword)
        await self._find_l2(pv)
        await self._find_learning(pv)
        self._gc(now)
        self._pending[pv.token] = pv
        logger.info(f"[lifelike.forget] preview counts={json.dumps(pv.counts(), sort_keys=True)}")
        return pv

    def _gc(self, now: float) -> None:
        for tok in [t for t, p in self._pending.items() if now - p.created_at > PREVIEW_TTL_S]:
            self._pending.pop(tok, None)

    def _find_items(self, owner_id: str, scope: str, keyword: str) -> List[str]:
        like = f"%{keyword}%"
        rows = tb.read_rows(
            self._store,
            "SELECT item_id FROM items WHERE (scope_key=? OR subject_id=?) AND closed_reason!='forgotten'"
            " AND (topic LIKE ? OR summary LIKE ?)",
            (scope, owner_id, like, like),
        )
        return [r["item_id"] for r in rows]

    async def _find_l2(self, pv: ForgetPreview) -> None:
        l2 = self._l2_get() if self._l2_get else None
        if l2 is None or not getattr(l2, "is_available", False):
            return
        try:
            entries = await l2.get_all_entries()
            for e in entries[:L2_SCAN_MAX]:
                if pv.keyword in str(e.content or "") and _owner_of(e.metadata or {}, pv.owner_id):
                    pv.l2_ids.append(e.id)
        except Exception as ex:  # noqa: BLE001
            logger.warning(f"[lifelike.forget] l2 scan failed: {type(ex).__name__}")
        try:
            lister = getattr(l2, "list_archived_memories", None)
            if lister is not None:
                arch = await lister(limit=L2_SCAN_MAX)
                for a in arch or []:
                    meta = a.get("metadata") if isinstance(a, dict) else None
                    if isinstance(meta, str):
                        try:
                            meta = json.loads(meta)
                        except ValueError:
                            meta = {}
                    content = str((a or {}).get("content") or "")
                    if pv.keyword in content and (
                        str(a.get("user_id") or "") == pv.owner_id or _owner_of(meta or {}, pv.owner_id)
                    ):
                        pv.l2_archive_ids.append(str(a.get("id") or a.get("memory_id")))
        except Exception as ex:  # noqa: BLE001
            logger.warning(f"[lifelike.forget] l2 archive scan failed: {type(ex).__name__}")

    async def _find_learning(self, pv: ForgetPreview) -> None:
        storage = self._learning_get() if self._learning_get else None
        if storage is None:
            return
        try:
            finder = getattr(storage, "find_few_shot_ids", None)
            if finder is not None:
                pv.few_shot_ids = list(await asyncio.to_thread(finder, pv.keyword, pv.owner_id))
        except Exception as ex:  # noqa: BLE001
            logger.warning(f"[lifelike.forget] learning scan failed: {type(ex).__name__}")

    def pending(self, token: str) -> Optional[ForgetPreview]:
        self._gc(float(self._clock()))
        return self._pending.get(str(token or "").strip())

    # ------------------------------------------------------------ purge
    async def purge(self, token: str, owner_id: str) -> Optional[Dict[str, int]]:
        pv = self.pending(token)
        if pv is None or pv.owner_id != str(owner_id):
            return None
        self._pending.pop(pv.token, None)
        out = {"beliefs": 0, "items": 0, "l2": 0, "learning": 0}
        # 先写墓碑：即使后面某层失败，也不会再学回来
        await asyncio.to_thread(tb.add_tombstone, self._store, tb.KIND_TOPIC, tb.topic_hash(pv.keyword), self._clock())
        out["beliefs"] = await asyncio.to_thread(self._beliefs.delete, pv.belief_ids)
        out["items"] = await asyncio.to_thread(self._purge_items, pv.item_ids)
        l2 = self._l2_get() if self._l2_get else None
        if l2 is not None and (pv.l2_ids or pv.l2_archive_ids):
            try:
                if pv.l2_ids and await l2.delete_entries(pv.l2_ids):
                    out["l2"] += len(pv.l2_ids)
                for mid in pv.l2_archive_ids:
                    if await l2.delete_archived_memory(mid):
                        out["l2"] += 1
            except Exception as ex:  # noqa: BLE001
                logger.warning(f"[lifelike.forget] l2 purge failed: {type(ex).__name__}")
        storage = self._learning_get() if self._learning_get else None
        if storage is not None and pv.few_shot_ids:
            try:
                out["learning"] = int(await asyncio.to_thread(storage.delete_rows, "few_shot", pv.few_shot_ids))
            except Exception as ex:  # noqa: BLE001
                logger.warning(f"[lifelike.forget] learning purge failed: {type(ex).__name__}")
        try:
            await asyncio.to_thread(self._store.bump_counter, "lifelike.forget.purged", 1)
        except Exception:  # noqa: BLE001
            pass
        logger.info(f"[lifelike.forget] purge counts={json.dumps(out, sort_keys=True)}")
        return out

    def _purge_items(self, item_ids: List[str]) -> int:
        n = 0
        now = float(self._clock())
        with tb.write_tx(self._store) as db:
            for iid in item_ids:
                row = db.execute("SELECT * FROM items WHERE item_id=?", (iid,)).fetchone()
                if row is None:
                    continue
                db.execute(
                    "UPDATE items SET topic='', summary='', source_refs='[]', last_outbound_msg_ids='[]',"
                    " time_basis='', status=?, closed_reason='forgotten', proactive_allowed=0, remind_requested=0,"
                    " dedupe_key=?, updated_at=?, revision=revision+1 WHERE item_id=?",
                    (m.STATUS_CANCELLED, f"{row['scope_key']}|{row['kind']}|forgotten:{iid}", now, iid),
                )
                db.execute("UPDATE item_history SET snapshot='{}' WHERE item_id=?", (iid,))
                n += 1
        return n

