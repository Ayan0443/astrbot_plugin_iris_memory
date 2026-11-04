"""
Iris Chat Memory - 频道/群观察采集（DESIGN.md §9）

复用 Telethon 适配器已连接的 client（不另起 session、不发送任何消息）：

- supervisor 每 10s 现查 ``platform_getter()`` → ``inst.get_client()``；client
  身份变化时在旧 client 上 remove、新 client 上
  ``add_event_handler(cb, events.NewMessage(chats=chat_ids))``；stop() 时移除。
  绑定前清理 client 上其它 ChannelIngestor 实例（插件热重载残留）注册的回调，
  保证同一 client 上只有一个采集回调。
- 回调只做标准化 + ``store.add_observed_post``（asyncio.to_thread），不调 LLM、
  不下载媒体；任何异常都吞掉并记日志，绝不抛给 Telethon（不影响适配器自身 handler）。
- 绑定后补采：``iter_messages(chat, min_id=cursor, reverse=True, limit=200)``，
  每条成功保存（新建或重复）后才推进游标 ``backfill:<chat_id>``；实时事件不推进
  补采游标，因此实时先看到的新消息不会让游标越过未处理的历史区间。
  首次无游标 → 只取最近 50 条。
- FloodWait：等待时长 ≤ FLOOD_WAIT_MAX_S 时睡眠后重试（有上限），否则放弃本轮。
- 动态采集群（U3，只在传入 ``chat_ids_get`` 时启用；不传时行为与之前一致）：supervisor 每轮现查
  采集群集合，变化时在同一 client 上换绑回调，并对新加入的群执行一次常规补采。新群若为广播频道
  （且不在 ``history_chats_get`` 之外的显式列表）则跳过。
- 历史回补（只对 ``history_chats_get()`` 里的群，即只经白名单通配纳入的群）：每
  ``HISTORY_PERIOD_S`` 只取一页（≤200 条，新→旧），只保存近 ``HISTORY_DAYS`` 天的消息；
  碰到更早的消息或取完即标记完成（游标 ``backfill_hist:<chat_id>``）。1 vCPU 下分批进行。
- 实体解析失败（Could not find the input entity）→ 每次补采最多 get_dialogs 一次后重试。

日志只记录 chat_id / msg_id / 计数 / 异常类型，不记录正文。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import weakref
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timezone
from typing import Any

from astrbot.api import logger

from . import models as m

SUPERVISE_PERIOD_S = 10.0
BACKFILL_PAGE = 200
BACKFILL_MAX_PAGES = 5
FIRST_RUN_LIMIT = 50
BACKFILL_CHAT_TIMEOUT_S = 180.0
ENTITY_TIMEOUT_S = 15.0  # 白名单通配群实体核实（get_entity）超时
HISTORY_SKIP_AFTER = 3  # 历史回补同一条消息连续保存失败这么多次后跳过
FLOOD_WAIT_MAX_S = 60.0
FLOOD_RETRIES = 2
MAX_TEXT_CHARS = 4000
HISTORY_DAYS = 30
HISTORY_PERIOD_S = 120.0

VIA_REALTIME = "realtime"
VIA_BACKFILL = "backfill"

# 回调函数上的标记：用于识别（含旧模块代码的）本采集器注册的 handler
HANDLER_MARK = "_iris_continuity_channel_ingest"


def cursor_name(chat_id: int) -> str:
    return f"backfill:{int(chat_id)}"


def history_cursor_name(chat_id: int) -> str:
    return f"backfill_hist:{int(chat_id)}"


def _to_ts(date: Any, fallback: float) -> float:
    if isinstance(date, datetime):
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return date.timestamp()
    if isinstance(date, (int, float)) and date > 0:
        return float(date)
    return fallback


def _peer_label(peer: Any) -> str:
    if peer is None:
        return ""
    for attr, kind in (("user_id", "user"), ("channel_id", "channel"), ("chat_id", "chat")):
        val = getattr(peer, attr, None)
        if val is not None:
            return f"{kind}:{val}"
    return ""


def _fwd_label(fwd: Any) -> str:
    if fwd is None:
        return ""
    parts = [
        _peer_label(getattr(fwd, "from_id", None)),
        str(getattr(fwd, "from_name", None) or "").strip(),
    ]
    return " ".join(p for p in parts if p)[:200]


def _is_flood_wait(exc: BaseException) -> bool:
    try:
        from telethon.errors import FloodWaitError  # noqa: PLC0415

        if isinstance(exc, FloodWaitError):
            return True
    except Exception:  # telethon 不可用时按名称识别
        pass
    return type(exc).__name__ == "FloodWaitError" and hasattr(exc, "seconds")


def _is_entity_missing(exc: BaseException) -> bool:
    return isinstance(exc, ValueError) and "input entity" in str(exc).lower()


def _default_builder(chat_ids: list[int]) -> Any:
    from telethon import events  # noqa: PLC0415  延迟导入：测试/非 telethon 环境不依赖

    return events.NewMessage(chats=list(chat_ids))


class ChannelIngestor:
    """频道/群观察采集器；单实例绑定当前适配器 client"""

    def __init__(
        self,
        store: Any,
        platform_getter: Callable[[], Any],
        chat_ids: Iterable[int],
        owner_ids: Iterable[str],
        owner_channel_ids: Iterable[int] = (),
        clock: Callable[[], float] | None = None,
        *,
        period_s: float = SUPERVISE_PERIOD_S,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        builder_factory: Callable[[list[int]], Any] | None = None,
        chat_ids_get: Callable[[], Iterable[int]] | None = None,
        history_chats_get: Callable[[], Iterable[int]] | None = None,
        history_days: float = HISTORY_DAYS,
        history_period_s: float = HISTORY_PERIOD_S,
    ) -> None:
        self._store = store
        self._platform_getter = platform_getter
        self.chat_ids: list[int] = sorted({int(c) for c in chat_ids})
        self.owner_ids: set[str] = {str(o) for o in owner_ids if str(o)}
        self.owner_channel_ids: set[int] = {int(c) for c in owner_channel_ids}
        self._clock = clock or time.time
        self._period = max(0.01, float(period_s))
        self._sleep = sleep
        self._builder_factory = builder_factory or _default_builder

        self._bound_ref: Callable[[], Any] | None = None
        self._builder: Any = None
        self._self_id: str = ""
        self._task: asyncio.Task | None = None
        self._bind_lock = asyncio.Lock()
        self._backfill_lock = asyncio.Lock()
        self._stopped = False
        self._chat_ids_get = chat_ids_get
        self._history_get = history_chats_get
        self._history_s = max(0.0, float(history_days)) * 86400.0
        self._history_period = max(0.0, float(history_period_s))
        self._last_history = 0.0
        self._broadcast_skipped: set[int] = set()
        self._verified: set[int] = set()
        self._history_bad: dict[tuple[int, int], int] = {}
        self._inst: Any = None

    # ------------------------------------------------------------ lifecycle
    @property
    def bound_client(self) -> Any:
        return self._bound_ref() if self._bound_ref is not None else None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.chat_ids or self.running:
            return
        self._stopped = False
        self._task = asyncio.create_task(self._supervise(), name="iris-continuity-channel-ingest")

    async def stop(self) -> None:
        self._stopped = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        async with self._bind_lock:
            self._unbind()

    async def _supervise(self) -> None:
        while not self._stopped:
            try:
                await self.check_binding()
                if self._chat_ids_get is not None:
                    await self.refresh_chats()
                if self._history_get is not None:
                    await self.history_tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[continuity] channel supervise error: {type(e).__name__}")
            await self._sleep(self._period)

    # ------------------------------------------------------------ dynamic chats (U3)
    def _wanted_chats(self) -> list[int] | None:
        try:
            raw = self._chat_ids_get() if self._chat_ids_get is not None else None
            if raw is None:
                return None
            return sorted({int(c) for c in raw})
        except Exception as e:
            logger.debug(f"[continuity] channel chat list failed: {type(e).__name__}")
            return None

    def _history_chats(self) -> set[int]:
        if self._history_get is None:
            return set()
        try:
            return {int(c) for c in (self._history_get() or ())}
        except Exception:
            return set()

    async def _is_broadcast(self, client: Any, chat_id: int) -> bool | None:
        """True = 广播频道，False = 群；None = 这次没查成（不采、不缓存，下一轮再查）。"""
        get_entity = getattr(client, "get_entity", None)
        if not callable(get_entity):
            return False
        try:
            ent = await asyncio.wait_for(get_entity(chat_id), ENTITY_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.info(f"[continuity] channel ingest verify chat={chat_id} deferred: {type(e).__name__}")
            return None
        return getattr(ent, "broadcast", False) is True

    async def refresh_chats(self) -> list[int]:
        """采集群集合变化 → 换绑回调；新加入的群补采一次。返回新加入（且已纳入）的群。

        只经白名单通配进来的群（``history_chats_get``）在 client 可用时核实一次实体类型，
        广播频道不采（记在内存，进程内不再查）。"""
        wanted = self._wanted_chats()
        if wanted is None or self._stopped:
            return []
        dynamic_only = self._history_chats()
        added: list[int] = []
        # 实体核实是网络调用：放在 _bind_lock 外面做，不阻塞 supervisor
        unverified: set[int] = set()
        probe_client = self.bound_client
        for c in wanted:
            if c in dynamic_only and c not in self._verified and c not in self._broadcast_skipped:
                if probe_client is None:
                    unverified.add(c)
                    continue
                kind = await self._is_broadcast(probe_client, c)
                if kind is True:
                    self._broadcast_skipped.add(c)
                    logger.info(f"[continuity] channel ingest skip broadcast chat={c}")
                elif kind is False:
                    self._verified.add(c)
                else:
                    unverified.add(c)  # 没查成：本轮不采（宁可晚采，不误采广播频道）
        async with self._bind_lock:
            client = self.bound_client
            if client is None:
                unverified = set()  # 没有 client 时不绑定回调，只记录集合（与原行为一致）
            new = sorted(c for c in wanted if c not in self._broadcast_skipped and c not in unverified)
            if new == self.chat_ids:
                return []
            added = [c for c in new if c not in self.chat_ids]
            removed = [c for c in self.chat_ids if c not in new]
            self.chat_ids = new
            if client is not None:
                self._unbind()
                if new and not await self._bind(self._inst, client):
                    return []
            logger.info(f"[continuity] channel ingest chats changed added={len(added)} removed={len(removed)}")
        if client is None:
            return added
        for chat_id in added:
            if self._stopped:
                break
            try:
                await asyncio.wait_for(self.backfill(chat_id), BACKFILL_CHAT_TIMEOUT_S)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[continuity] channel backfill chat={chat_id} aborted: {type(e).__name__}")
        return added

    async def history_tick(self, force: bool = False) -> int:
        """历史回补一步：挑一个未完成的白名单通配群取一页（新→旧，只存近 N 天）。返回保存条数。"""
        client = self.bound_client
        if client is None or self._stopped or self._history_s <= 0:
            return 0
        now = float(self._clock())
        if not force and now - self._last_history < self._history_period:
            return 0
        self._last_history = now
        targets = sorted(c for c in self._history_chats() if c in self.chat_ids)
        for chat_id in targets:
            raw = await asyncio.to_thread(self._store.get_cursor, history_cursor_name(chat_id))
            if raw == "done":
                continue
            try:
                return await asyncio.wait_for(self._history_page(client, chat_id, raw, now), BACKFILL_CHAT_TIMEOUT_S)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if _is_flood_wait(e):
                    self._last_history = now + float(getattr(e, "seconds", 0) or 0)
                logger.warning(f"[continuity] channel history chat={chat_id} failed: {type(e).__name__}")
                return 0
        return 0

    async def _history_page(self, client: Any, chat_id: int, raw: Any, now: float) -> int:
        async with self._backfill_lock:
            if raw not in (None, "") and str(raw).isdigit():
                offset = int(raw)
            else:
                fwd = await asyncio.to_thread(self._store.get_cursor, cursor_name(chat_id))
                if fwd in (None, "") or not str(fwd).lstrip("-").isdigit():
                    return 0  # 常规补采还没跑过：等它先定下起点
                offset = int(fwd) + 1
            cutoff = now - self._history_s
            saved = 0
            seen = 0
            oldest = offset
            done = False
            async for msg in client.iter_messages(chat_id, offset_id=offset, limit=BACKFILL_PAGE):
                msg_id = getattr(msg, "id", None)
                if not isinstance(msg_id, int):
                    continue
                seen += 1
                oldest = min(oldest, msg_id)
                if _to_ts(getattr(msg, "date", None), now) < cutoff:
                    done = True
                    break
                try:
                    await self._save(msg, chat_id)
                except _SaveFailed:
                    key = (int(chat_id), int(msg_id))
                    self._history_bad[key] = self._history_bad.get(key, 0) + 1
                    if self._history_bad[key] < HISTORY_SKIP_AFTER:
                        raise
                    # 同一条连续失败多次：跳过它，别让一条坏消息卡住整个回补队列
                    logger.warning(f"[continuity] channel history chat={chat_id} skip bad msg={msg_id}")
                    self._history_bad.pop(key, None)
                    continue
                saved += 1
            if seen < BACKFILL_PAGE or oldest <= 1:
                done = True
            await asyncio.to_thread(
                self._store.set_cursor, history_cursor_name(chat_id), "done" if done else str(oldest)
            )
            if saved or done:
                logger.info(f"[continuity] channel history chat={chat_id} saved={saved} done={int(done)}")
            return saved

    # ------------------------------------------------------------ binding
    def _current_client(self) -> tuple[Any, Any]:
        try:
            inst = self._platform_getter()
        except Exception as e:
            logger.debug(f"[continuity] channel platform lookup failed: {type(e).__name__}")
            return None, None
        get_client = getattr(inst, "get_client", None) if inst is not None else None
        if not callable(get_client):
            return inst, None
        try:
            return inst, get_client()
        except Exception as e:
            logger.debug(f"[continuity] channel get_client failed: {type(e).__name__}")
            return inst, None

    async def check_binding(self) -> bool:
        """一次 supervisor 步骤；返回本次是否新绑定（并已执行补采）"""
        if self._stopped or not self.chat_ids:
            return False
        async with self._bind_lock:
            inst, client = self._current_client()
            bound = self.bound_client
            if client is None:
                if bound is not None:
                    self._unbind()
                return False
            if client is bound:
                return False
            is_connected = getattr(client, "is_connected", None)
            if callable(is_connected):
                try:
                    if not is_connected():
                        return False
                except Exception:
                    return False
            self._unbind()
            if not await self._bind(inst, client):
                return False
        await self.backfill_all()
        return True

    async def _bind(self, inst: Any, client: Any) -> bool:
        self._inst = inst
        self._purge_foreign_handlers(client)
        try:
            builder = self._builder_factory(self.chat_ids)
            client.add_event_handler(self._on_event, builder)
        except Exception as e:
            logger.warning(f"[continuity] channel bind failed: {type(e).__name__}")
            return False
        try:
            self._bound_ref = weakref.ref(client)
        except TypeError:
            self._bound_ref = lambda c=client: c
        self._builder = builder
        self._self_id = await self._resolve_self_id(inst, client)
        logger.info(f"[continuity] channel ingest bound chats={self.chat_ids}")
        return True

    def _unbind(self) -> None:
        client, builder = self.bound_client, self._builder
        self._bound_ref = None
        self._builder = None
        if client is None:
            return
        try:
            client.remove_event_handler(self._on_event, builder)
            logger.info("[continuity] channel ingest handler removed")
        except Exception as e:
            logger.debug(f"[continuity] channel unbind failed: {type(e).__name__}")

    def _purge_foreign_handlers(self, client: Any) -> None:
        """移除本采集器类（可能来自热重载前的旧模块）在 client 上遗留的回调"""
        lister = getattr(client, "list_event_handlers", None)
        if not callable(lister):
            return
        try:
            handlers = list(lister())
        except Exception:
            return
        for cb, ev in handlers:
            if not getattr(cb, HANDLER_MARK, False) or cb == self._on_event:
                continue
            try:
                client.remove_event_handler(cb, ev)
                logger.info("[continuity] channel removed stale ingest handler")
            except Exception as e:
                logger.debug(f"[continuity] channel stale handler removal failed: {type(e).__name__}")

    async def _resolve_self_id(self, inst: Any, client: Any) -> str:
        sid = str(getattr(inst, "self_id", "") or "").strip()
        if sid.isdigit():
            return sid
        try:
            me = await client.get_me()
            if getattr(me, "id", None) is not None:
                return str(me.id)
        except Exception as e:
            logger.debug(f"[continuity] channel get_me failed: {type(e).__name__}")
        return sid

    # ------------------------------------------------------------ normalize
    def _author_kind(self, chat_id: int, sender_id: str, msg: Any) -> str:
        if getattr(msg, "out", False) is True or (self._self_id and sender_id == self._self_id):
            return "self"
        if sender_id and sender_id in self.owner_ids:
            return "owner"
        if chat_id in self.owner_channel_ids:
            return "owner"
        if getattr(msg, "post", False) is True:
            return "channel"
        if not sender_id:
            return "unknown"
        return "other"

    def normalize(self, msg: Any, chat_id: int, via: str) -> dict[str, Any] | None:
        msg_id = getattr(msg, "id", None)
        if not isinstance(msg_id, int) or msg_id <= 0:
            return None
        if getattr(msg, "action", None) is not None:  # 服务消息（入群、置顶等）
            return None
        raw_sender = getattr(msg, "sender_id", None)
        sender_id = "" if raw_sender is None else str(raw_sender)
        text = getattr(msg, "raw_text", None)
        if text is None:
            text = getattr(msg, "message", None)
        fwd = getattr(msg, "fwd_from", None)
        return {
            "chat_id": int(chat_id),
            "msg_id": msg_id,
            "posted_at": _to_ts(getattr(msg, "date", None), self._clock()),
            "sender_id": sender_id,
            "author_kind": self._author_kind(int(chat_id), sender_id, msg),
            "is_forward": 1 if fwd is not None else 0,
            "fwd_from": _fwd_label(fwd),
            "text": m.clip(str(text or ""), MAX_TEXT_CHARS),
            "via": via,
        }

    # ------------------------------------------------------------ realtime
    async def _on_event(self, event: Any) -> None:
        try:
            msg = getattr(event, "message", None) or event
            chat_id = getattr(event, "chat_id", None)
            if chat_id is None:
                chat_id = getattr(msg, "chat_id", None)
            if chat_id is None or int(chat_id) not in self.chat_ids:
                return
            post = self.normalize(msg, int(chat_id), VIA_REALTIME)
            if post is None:
                return
            await asyncio.to_thread(self._store.add_observed_post, **post)
        except Exception as e:  # 绝不把异常抛给 Telethon
            logger.warning(f"[continuity] channel realtime save failed: {type(e).__name__}")

    # ------------------------------------------------------------ backfill
    async def backfill_all(self) -> dict[int, int]:
        out: dict[int, int] = {}
        for chat_id in self.chat_ids:
            if self._stopped:
                break
            try:
                out[chat_id] = await asyncio.wait_for(self.backfill(chat_id), BACKFILL_CHAT_TIMEOUT_S)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[continuity] channel backfill chat={chat_id} aborted: {type(e).__name__}")
                out[chat_id] = 0
        return out

    async def backfill(self, chat_id: int) -> int:
        """补采一个 chat；返回本轮处理（保存或确认重复）的消息数"""
        client = self.bound_client
        if client is None:
            return 0
        chat_id = int(chat_id)
        async with self._backfill_lock:
            raw = await asyncio.to_thread(self._store.get_cursor, cursor_name(chat_id))
            cursor = int(raw) if raw not in (None, "") and str(raw).lstrip("-").isdigit() else None
            processed = 0
            dialogs_loaded = False
            flood_retries = 0
            pages = 0
            while not self._stopped:
                try:
                    if cursor is None:
                        n, cursor = await self._first_run(client, chat_id)
                        processed += n
                        break
                    n, cursor = await self._page(client, chat_id, cursor)
                    processed += n
                    pages += 1
                    if n < BACKFILL_PAGE or pages >= BACKFILL_MAX_PAGES:
                        break
                except _SaveFailed:
                    break
                except Exception as e:
                    if _is_flood_wait(e):
                        seconds = float(getattr(e, "seconds", 0) or 0)
                        if seconds > FLOOD_WAIT_MAX_S or flood_retries >= FLOOD_RETRIES:
                            logger.warning(
                                f"[continuity] channel backfill chat={chat_id} flood wait {seconds:.0f}s, stop"
                            )
                            break
                        flood_retries += 1
                        logger.info(f"[continuity] channel backfill chat={chat_id} flood wait {seconds:.0f}s")
                        await self._sleep(seconds)
                        continue
                    if _is_entity_missing(e) and not dialogs_loaded:
                        dialogs_loaded = True
                        try:
                            await client.get_dialogs()
                        except Exception as de:
                            logger.warning(
                                f"[continuity] channel get_dialogs failed: {type(de).__name__}"
                            )
                            break
                        continue
                    logger.warning(f"[continuity] channel backfill chat={chat_id} failed: {type(e).__name__}")
                    break
            if processed:
                logger.info(f"[continuity] channel backfill chat={chat_id} processed={processed} cursor={cursor}")
            return processed

    async def _save(self, msg: Any, chat_id: int) -> None:
        post = self.normalize(msg, chat_id, VIA_BACKFILL)
        if post is None:
            return
        try:
            await asyncio.to_thread(self._store.add_observed_post, **post)
        except Exception as e:
            logger.warning(
                f"[continuity] channel backfill save failed chat={chat_id} msg={post['msg_id']}: {type(e).__name__}"
            )
            raise _SaveFailed from e

    async def _advance(self, chat_id: int, cursor: int | None, msg_id: int) -> int:
        new = msg_id if cursor is None else max(cursor, msg_id)
        if new != cursor:
            await asyncio.to_thread(self._store.set_cursor, cursor_name(chat_id), new)
        return new

    async def _first_run(self, client: Any, chat_id: int) -> tuple[int, int | None]:
        """无游标：取最近 FIRST_RUN_LIMIT 条，旧→新保存并逐条推进游标"""
        msgs = [msg async for msg in client.iter_messages(chat_id, limit=FIRST_RUN_LIMIT)]
        msgs = sorted((x for x in msgs if isinstance(getattr(x, "id", None), int)), key=lambda x: x.id)
        cursor: int | None = None
        n = 0
        for msg in msgs:
            await self._save(msg, chat_id)
            cursor = await self._advance(chat_id, cursor, msg.id)
            n += 1
        # 空结果：不写游标（下次仍按“最近 50 条”处理，避免 min_id=0 从最早历史爬起）
        return n, cursor

    async def _page(self, client: Any, chat_id: int, cursor: int) -> tuple[int, int]:
        n = 0
        async for msg in client.iter_messages(
            chat_id, min_id=cursor, reverse=True, limit=BACKFILL_PAGE
        ):
            msg_id = getattr(msg, "id", None)
            if not isinstance(msg_id, int) or msg_id <= cursor:
                continue
            await self._save(msg, chat_id)
            cursor = await self._advance(chat_id, cursor, msg_id)
            n += 1
        return n, cursor


setattr(ChannelIngestor._on_event, HANDLER_MARK, True)


class _SaveFailed(Exception):
    """补采保存失败：停止本 chat 补采，游标停在最后一条成功保存处"""


# ---------------------------------------------------------------------------
# 主人私聊离线补采（只读）
# ---------------------------------------------------------------------------

OWNER_CATCHUP_LIMIT = 50

OwnerFeed = Callable[[str, list[dict[str, Any]]], Awaitable[Any]]


def _reply_to_id(msg: Any) -> int | None:
    rid = getattr(msg, "reply_to_msg_id", None)
    if rid is None:
        rid = getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None)
    return rid if isinstance(rid, int) and rid > 0 else None


class OwnerCatchUp:
    """适配器 client（重新）绑定时，补采主人私聊里离线期间发来的消息。

    Telethon 适配器用 StringSession 且不 catch_up，离线期间的私聊不会以事件送达。
    每次 client 身份变化后：对每个主人 ``iter_messages(owner_peer,
    min_id=水位, reverse=True, limit=50)``（只读，不注册 handler、不发送），
    取主人本人发来（非 out、sender=主人、非服务消息）且比水位新的消息，
    交给 ``feed(scope_key, [msg_dict...])``（由服务推进水位/恢复/记入收件箱，
    不生成回复）。没有水位（首次运行）→ 跳过，不补采任何历史。
    """

    def __init__(
        self,
        platform_getter: Callable[[], Any],
        owner_ids_get: Callable[[], Iterable[Any]],
        get_watermark: Callable[[str], Any],
        feed: OwnerFeed,
        clock: Callable[[], float] | None = None,
        *,
        period_s: float = SUPERVISE_PERIOD_S,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        limit: int = OWNER_CATCHUP_LIMIT,
    ) -> None:
        self._platform_getter = platform_getter
        self._owner_ids_get = owner_ids_get
        self._get_watermark = get_watermark
        self._feed = feed
        self._clock = clock or time.time
        self._period = max(0.01, float(period_s))
        self._sleep = sleep
        self._limit = max(1, int(limit))
        self._bound_ref: Callable[[], Any] | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._stopped = False

    @property
    def bound_client(self) -> Any:
        return self._bound_ref() if self._bound_ref is not None else None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._stopped = False
        self._task = asyncio.create_task(self._supervise(), name="iris-continuity-owner-catchup")

    async def stop(self) -> None:
        self._stopped = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._bound_ref = None

    async def _supervise(self) -> None:
        while not self._stopped:
            try:
                await self.check_binding()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[continuity] owner catch-up supervise error: {type(e).__name__}")
            await self._sleep(self._period)

    def _owner_ids(self) -> list[str]:
        try:
            return sorted({str(o).strip() for o in (self._owner_ids_get() or ()) if str(o).strip()})
        except Exception:
            return []

    def _current_client(self) -> Any:
        try:
            inst = self._platform_getter()
            get_client = getattr(inst, "get_client", None) if inst is not None else None
            return get_client() if callable(get_client) else None
        except Exception as e:
            logger.debug(f"[continuity] owner catch-up get_client failed: {type(e).__name__}")
            return None

    async def check_binding(self) -> bool:
        """一次 supervisor 步骤：client 新绑定时补采；返回本次是否执行了补采"""
        if self._stopped:
            return False
        async with self._lock:
            client = self._current_client()
            if client is None:
                self._bound_ref = None
                return False
            if client is self.bound_client:
                return False
            is_connected = getattr(client, "is_connected", None)
            if callable(is_connected):
                try:
                    if not is_connected():
                        return False
                except Exception:
                    return False
            try:
                self._bound_ref = weakref.ref(client)
            except TypeError:
                self._bound_ref = lambda c=client: c
            await self.catch_up(client)
        return True

    async def catch_up(self, client: Any) -> dict[str, int]:
        out: dict[str, int] = {}
        for owner in self._owner_ids():
            if self._stopped:
                break
            try:
                out[owner] = await asyncio.wait_for(
                    self._catch_up_owner(client, owner), BACKFILL_CHAT_TIMEOUT_S
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[continuity] owner catch-up failed: {type(e).__name__}")
                out[owner] = 0
        return out

    async def _fetch(self, client: Any, peer: int, min_id: int) -> list[Any]:
        dialogs_loaded = False
        while True:
            try:
                return [
                    x
                    async for x in client.iter_messages(
                        peer, min_id=min_id, reverse=True, limit=self._limit
                    )
                ]
            except Exception as e:
                if _is_entity_missing(e) and not dialogs_loaded:
                    dialogs_loaded = True
                    await client.get_dialogs()
                    continue
                raise

    async def _catch_up_owner(self, client: Any, owner: str) -> int:
        if not owner.isdigit():
            return 0
        scope = f"private:{owner}"
        wm = await asyncio.to_thread(self._get_watermark, scope)
        if not wm:
            return 0  # 首次运行：没有水位，不补采历史
        try:
            last = int(wm.get("last_msg_id") or 0)
        except (TypeError, ValueError, AttributeError):
            return 0
        if last <= 0:
            return 0
        msgs = await self._fetch(client, int(owner), last)
        fresh: list[dict[str, Any]] = []
        for x in msgs:
            mid = getattr(x, "id", None)
            if not isinstance(mid, int) or mid <= last:
                continue
            if getattr(x, "out", False) is True or getattr(x, "action", None) is not None:
                continue
            sender = getattr(x, "sender_id", None)
            if sender is None or str(sender) != owner:
                continue
            text = getattr(x, "raw_text", None)
            if text is None:
                text = getattr(x, "message", None)
            text = str(text or "")
            has_media = getattr(x, "media", None) is not None
            fresh.append(
                {
                    "msg_id": mid,
                    "msg_ts": _to_ts(getattr(x, "date", None), self._clock()),
                    "text": text,
                    "sender_id": owner,
                    "reply_to_msg_id": _reply_to_id(x),
                    "has_media": has_media,
                    "media_only": has_media and not text.strip(),
                }
            )
        fresh.sort(key=lambda d: d["msg_id"])
        if fresh:
            await self._feed(scope, fresh)
            logger.info(f"[continuity] owner catch-up scope={scope} fetched={len(fresh)}")
        return len(fresh)
