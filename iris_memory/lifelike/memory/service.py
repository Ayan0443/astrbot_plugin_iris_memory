"""LifelikeMemory：P1-7 用户认知 / forget 与 P1-13 群友认知的装配门面（挂在 ContinuityService 上）。

- ``inject_blocks``：主人私聊被动回复时的注入段（``<你记得的她>``、``<你在群里对他的印象>``）。
  ``lifelike_memory_mode`` / ``lifelike_member_mode`` 不是 ``on`` 时返回空列表——不注入。
- ``maintenance``：认知过期扫描（memory_mode≠off）、群友印象增量（member_mode≠off，每小时最多一次）。
- 命令：``/iris_mem beliefs|forget``（经 commands.lifelike_handler）、``/iris_member``（main.py）。
  命令回执只返回给主人，不写 L1 / outbound / learning（本门面不触碰这些层）。

所有入口异常兜底：失败 → 不注入 / 不变更，聊天照常。日志不含正文。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Iterable, List, Optional, Sequence

from astrbot.api import logger

from ...continuity import models as m
from ..members.ledger import MemberImpressionLedger
from ..members.lens import MemberLens, valid_alias
from ..members.store import alias_hash
from . import tombstones as tb
from .beliefs import KEY_INFER, KEY_MODE, BeliefStore, normalize_memory_mode, render_beliefs_block
from .forget import ForgetService

__all__ = ["LifelikeMemory", "set_current", "get_current"]

_current: Optional["LifelikeMemory"] = None


def set_current(svc: Optional["LifelikeMemory"]) -> None:
    global _current
    _current = svc
    # 私聊 L2 收口的通配群（"*" / "*whitelist"）按群友认知同一套授权规则解析（U3）
    try:
        from ..social import l2_filter

        if svc is None:
            l2_filter.set_group_resolver(None)
        else:
            l2_filter.set_group_resolver(lambda value: [str(g) for g in svc.ledger.groups_for(value)])
    except Exception:  # noqa: BLE001
        pass


def get_current() -> Optional["LifelikeMemory"]:
    return _current


def _source_chats(profile: Any) -> List[str]:
    """member_profiles.chat_ids → 去重后的来源群（同一个群的不同写法算一个）"""
    import json

    from ..social.l2_filter import chat_variants

    try:
        raw = json.loads((profile or {}).get("chat_ids") or "[]")
    except (TypeError, ValueError, AttributeError):
        return []
    out: List[str] = []
    seen: set = set()
    for c in raw if isinstance(raw, list) else []:
        v = chat_variants(c)
        if not v or v & seen:
            continue
        seen |= v
        out.append(str(c))
    return out


def _day(ts: Any) -> str:
    try:
        return m.local_day(float(ts))
    except (TypeError, ValueError):
        return "-"


class LifelikeMemory:
    def __init__(
        self,
        store: Any,
        *,
        config_get: Callable[[str, Any], Any],
        owner_ids: Callable[[], Iterable[Any]],
        observe_chat_ids: Callable[[], Sequence[Any]],
        owner_channel_ids: Callable[[], Sequence[Any]] = lambda: (),
        profile_names: Any = None,
        l2_get: Optional[Callable[[], Any]] = None,
        learning_get: Optional[Callable[[], Any]] = None,
        clock: Callable[[], float] = time.time,
        id_whitelist: Callable[[], Optional[Sequence[Any]]] = lambda: None,
    ) -> None:
        self._store = store
        self._cfg = config_get
        self._owner_ids = owner_ids
        self._clock = clock
        self.beliefs = BeliefStore(store, clock)
        self.forget = ForgetService(store, self.beliefs, l2_get=l2_get, learning_get=learning_get, clock=clock)
        self.ledger = MemberImpressionLedger(
            store,
            config_get=config_get,
            observe_chat_ids=observe_chat_ids,
            owner_channel_ids=owner_channel_ids,
            profile_names=profile_names,
            clock=clock,
            id_whitelist=id_whitelist,
        )
        self.lens = MemberLens(
            self.ledger.members,
            mode_get=self.member_mode,
            authorized_groups=self.ledger.authorized_groups,
            bump=lambda name: store.bump_counter(name, 1),
        )
        self._belief_fails = 0
        self._belief_paused_until = 0.0
        self._pending_forget_all: dict = {}

    # ------------------------------------------------------------ modes
    def memory_mode(self) -> str:
        try:
            return normalize_memory_mode(self._cfg(KEY_MODE, "off"))
        except Exception:  # noqa: BLE001
            return "off"

    def inference_mode(self) -> str:
        try:
            v = str(self._cfg(KEY_INFER, "hedged") or "hedged").strip().lower()
        except Exception:  # noqa: BLE001
            return "off"
        return v if v in ("off", "hedged") else "off"

    def member_mode(self) -> str:
        return self.ledger.mode()

    def _owners(self) -> set:
        try:
            return {str(o).strip() for o in (self._owner_ids() or ()) if str(o).strip()}
        except Exception:  # noqa: BLE001
            return set()

    def is_owner_private(self, scope_key: Any, sender_id: Any) -> bool:
        return MemberLens.scope_allowed(scope_key, sender_id, self._owners(), "reply")

    # ------------------------------------------------------------ inject
    async def inject_blocks(self, scope_key: str, sender_id: Any, current_text: str) -> List[str]:
        mem_on = self.memory_mode() == "on"
        member_mode = self.member_mode()
        if not mem_on and member_mode == "off":
            return []
        if not self.is_owner_private(scope_key, sender_id):
            return []
        out: List[str] = []
        if mem_on:
            try:
                rows = await asyncio.to_thread(self.beliefs.list, scope_key)
                block = render_beliefs_block(rows, inference_mode=self.inference_mode(), now=float(self._clock()))
                if block:
                    from ..guards.numeric import guard_text

                    block = guard_text(block, source="beliefs", strict=True)
                if block and "<你记得的她>" in block and block.count("\n") >= 2:
                    out.append(block)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[lifelike.beliefs] inject failed: {type(e).__name__}")
        if member_mode != "off":
            try:
                texts = await asyncio.to_thread(self._pull_texts, scope_key, str(sender_id), current_text)
                block = await asyncio.to_thread(
                    self.lens.render, scope_key, sender_id, self._owners(), texts, purpose="reply"
                )
                if block:
                    out.append(block)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[lifelike.member] inject failed: {type(e).__name__}")
        return out

    def _pull_texts(self, scope_key: str, sender_id: str, current_text: str) -> List[str]:
        """MG-pull：本条 + 上一条主人私聊消息"""
        texts = [str(current_text or "")]
        try:
            rows = tb.read_rows(
                self._store,
                "SELECT text FROM inbound WHERE scope_key=? AND sender_id=? ORDER BY msg_ts DESC, msg_id DESC LIMIT 2",
                (scope_key, sender_id),
            )
        except Exception:  # noqa: BLE001
            rows = []
        prev = [str(r.get("text") or "") for r in rows]
        if prev and prev[0].strip() == texts[0].strip():
            prev = prev[1:]
        if prev:
            texts.append(prev[0])
        return texts

    # ------------------------------------------------------------ maintenance
    async def maintenance(self) -> None:
        if self.memory_mode() != "off" and self._clock() >= self._belief_paused_until:
            try:
                n = await asyncio.to_thread(self.beliefs.expire_due)
                self._belief_fails = 0
                if n:
                    logger.info(f"[lifelike.beliefs] expired={n}")
            except Exception as e:  # noqa: BLE001
                self._belief_fails += 1
                logger.warning(f"[lifelike.beliefs] expire failed: {type(e).__name__}")
                if self._belief_fails >= 5:
                    self._belief_fails = 0
                    self._belief_paused_until = float(self._clock()) + 3600.0
        try:
            await self.ledger.maybe_run()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[lifelike.member] maintenance failed: {type(e).__name__}")

    # ------------------------------------------------------------ commands: beliefs / forget
    _KIND_LABEL = {"fact": "事实", "preference": "偏好", "inference": "推测", "habit": "习惯", "boundary": "边界", "stance": "立场"}

    async def beliefs_command(self, owner_id: str, args: Sequence[str]) -> str:
        scope = f"private:{owner_id}"
        sub = (args[0].lower() if args else "list")
        if sub in ("list", "ls"):
            rows = await asyncio.to_thread(self.beliefs.list, scope, ("active", "disputed"), 30)
            if not rows:
                return f"（还没有记下的认知；当前模式 {self.memory_mode()}）"
            lines = [f"记下的认知（模式 {self.memory_mode()}；推测不会当事实说）："]
            for r in rows:
                exp = f" 到期{_day(r['expires_at'])}" if r.get("expires_at") else ""
                flag = " 待确认" if r.get("needs_confirmation") else ""
                lines.append(
                    f"- {r['belief_id']} [{self._KIND_LABEL.get(r['kind'], r['kind'])}·{r['band']}·{r['status']}{flag}]"
                    f" {r['statement']}{exp}"
                )
            lines.append("确认：/iris_mem beliefs confirm <id>；否认：/iris_mem beliefs retract <id>")
            return "\n".join(lines)
        if sub in ("confirm", "retract") and len(args) >= 2:
            bid = args[1].strip()
            row = await asyncio.to_thread(self.beliefs.get, bid)
            if row is None or row.get("scope_key") != scope:
                return "没找到这条。"
            if sub == "confirm":
                res = await asyncio.to_thread(self.beliefs.owner_confirm, bid)
                return "好，记成确定的了。" if res else "这条现在不能确认。"
            res = await asyncio.to_thread(self.beliefs.retract, bid)
            return "好，这条作废了，不会再用。" if res else "这条现在不能作废。"
        return "用法：/iris_mem beliefs [list] | confirm <id> | retract <id>"

    async def forget_command(self, owner_id: str, args: Sequence[str]) -> str:
        if not args:
            return "用法：/iris_mem forget <关键词>（先预览），再 /iris_mem forget confirm <确认码>"
        if args[0].lower() == "confirm":
            if len(args) < 2:
                return "缺确认码。"
            out = await self.forget.purge(args[1], owner_id)
            if out is None:
                return "确认码无效或已过期（10 分钟），请重新预览。"
            total = sum(out.values())
            return (
                f"好，不提了。已删除：认知 {out['beliefs']}、事项 {out['items']}、长期记忆 {out['l2']}、学习样本 {out['learning']}"
                f"（共 {total}）。以后不会再从对话里学回这个话题。"
                "\n说明：正文已覆写，无法恢复；原始聊天记录本身不动。"
            )
        keyword = " ".join(args).strip()
        pv = await self.forget.preview(owner_id, keyword)
        if pv is None:
            return f"关键词要 2～{tb.SUB_MAX} 个字。"
        c = pv.counts()
        return (
            f"预览（不显示内容）：认知 {c['beliefs']}、事项 {c['items']}、长期记忆 {c['l2']}、学习样本 {c['learning']}。\n"
            f"确认删除请在 10 分钟内发送：/iris_mem forget confirm {pv.token}\n"
            "删除后正文覆写、不可恢复，并会记住以后不再学这个话题。"
        )

    # ------------------------------------------------------------ commands: /iris_member
    async def member_command(self, owner_id: str, args: Sequence[str]) -> str:
        ms = self.ledger.members
        sub = (args[0].lower() if args else "list")
        rest = list(args[1:])
        if sub == "list":
            profiles = await asyncio.to_thread(ms.list_profiles, ("active", "muted"))
            if not profiles:
                return f"（还没有群友印象；模式 {self.member_mode()}）"
            lines = [f"群友印象（模式 {self.member_mode()}）："]
            for p in profiles[:40]:
                names = [a["alias"] for a in await asyncio.to_thread(ms.aliases, p["member_id"]) if not a.get("alias_hash")]
                notes = await asyncio.to_thread(ms.notes, p["member_id"])
                lines.append(
                    f"- {'/'.join(names[:3]) or '（无称呼）'} id={p['member_id']} 印象{len(notes)}条"
                    f" 来源群{len(_source_chats(p))}个 最近出现{_day(p['last_seen'])}"
                )
            return "\n".join(lines)
        if sub == "show" and rest:
            mid = await asyncio.to_thread(self._resolve_member, " ".join(rest))
            if mid is None:
                return "没找到，或者有重名（可以用 id）。"
            notes = await asyncio.to_thread(ms.notes, mid, ("active", "expired"))
            prof = await asyncio.to_thread(ms.get_profile, mid)
            chats = _source_chats(prof) if prof else []
            names = [a["alias"] for a in await asyncio.to_thread(ms.aliases, mid) if not a.get("alias_hash")]
            lines = [
                f"id={mid} 称呼 {'/'.join(names[:5]) or '-'}；来源群（{len(chats)} 个，跨群合并为一份）："
                f"{', '.join(chats) or '-'}："
            ]
            for n in notes:
                lines.append(
                    f"- {n['note_id']} {n['text']}（{n['confidence_band']}·{n['status']}·到期{_day(n['expires_at'])}）"
                )
            if len(lines) == 1:
                lines.append("（暂无印象条目）")
            return "\n".join(lines)
        if sub == "forget" and rest:
            mid = await asyncio.to_thread(self._resolve_member, " ".join(rest))
            if mid is None:
                return "没找到，或者有重名（可以用 id）。"
            n = await asyncio.to_thread(self.ledger.forget_member, mid)
            return f"好，关于这位群友的印象都删了（{n} 条），以后也不再记。"
        if sub == "forget-note" and rest:
            row = await asyncio.to_thread(ms.delete_note, rest[0])
            if row is None:
                return "没找到这条。"
            await asyncio.to_thread(
                tb.add_tombstone, self._store, tb.KIND_MEMBER_NOTE,
                tb.note_hash(row["member_id"], row.get("topic_key") or ""), float(self._clock()),
            )
            return "好，这条删了，不会再学回来。"
        if sub == "forget-all":
            now = float(self._clock())
            tok = self._pending_forget_all.get(owner_id)
            if rest and rest[0].lower() == "confirm" and tok and now - tok < 600:
                self._pending_forget_all.pop(owner_id, None)
                profiles = await asyncio.to_thread(ms.list_profiles, ("active", "muted"))
                for p in profiles:
                    await asyncio.to_thread(
                        tb.add_tombstone, self._store, tb.KIND_MEMBER, tb.member_hash(p["member_id"]), now
                    )
                n = await asyncio.to_thread(ms.delete_all)
                logger.info(f"[lifelike.member] op=forget_all members={n}")
                return f"好，全部群友印象都删了（{n} 位）。"
            self._pending_forget_all[owner_id] = now
            c = await asyncio.to_thread(ms.counts)
            return f"将删除 {c['members']} 位群友的全部印象并不再记录。确认请在 10 分钟内发送：/iris_member forget-all confirm"
        if sub == "alias" and len(rest) >= 2:
            mid = await asyncio.to_thread(self._resolve_member, rest[0])
            alias = " ".join(rest[1:]).strip()
            if mid is None:
                return "没找到这位群友，或者有重名（可以用 id）。"
            if alias.startswith("@"):
                await asyncio.to_thread(
                    ms.upsert_alias, mid, "#" + alias_hash(alias)[:8], "owner_told", private_only=1, ahash=alias_hash(alias)
                )
                return "好，记下了（用户名只存哈希）。"
            if not valid_alias(alias):
                return "这个叫法太短或太常见，不适合当称呼。"
            await asyncio.to_thread(ms.upsert_alias, mid, alias, "owner_told", private_only=1)
            return "好，记下了。"
        return (
            "用法：/iris_member list | show <称呼或id> | forget <称呼或id> | forget-note <条目id>"
            " | forget-all [confirm] | alias <称呼或id> <叫法>"
        )

    def _resolve_member(self, key: str) -> Optional[str]:
        from ..members.lens import MemberResolver

        ms = self.ledger.members
        key = str(key or "").strip()
        if not key:
            return None
        if ms.get_profile(key) is not None:
            return key
        aliases = ms.aliases()
        exact = {a["member_id"] for a in aliases if not a.get("alias_hash") and str(a["alias"]).lower() == key.lower()}
        if len(exact) == 1:
            return next(iter(exact))
        if key.startswith("@"):
            mid, reason = MemberResolver(aliases).match([key])
            return mid if reason == "hit" else None
        return None

