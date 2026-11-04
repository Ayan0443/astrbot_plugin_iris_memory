"""MemberResolver + MemberLens：主人私聊里提到群友时注入轻量印象（设计 §6.8.6～6.8.7）。

代码断言（单元测试覆盖）：

- **MG-scope**：只有 ``purpose == "reply"`` 且 ``scope_key == private:<sender>`` 且 sender 是主人时才可能非空；
  群聊、其他私聊、主动消息生成路径一律返回 ''。
- **MG-pull**：主人**本条或上一条**私聊消息精确包含某群友的称呼（或 ``@用户名`` 哈希命中）才注入；
  同时命中多个群友（重名/歧义）→ 不注入。
- **MG-alias**：称呼 ≥2 字，且不是常用词（"猫""老师""哥"之类不做称呼匹配）。
- **MG-sens**：注入前再过一次 ``PrivacyScrubber``。
- **MG-push**：本模块不提供主动候选接口；``render(purpose="proactive")`` 恒为 ''。
- **R11**：来源群已不再授权或主人已离群（60 天无发言）→ 该群来的印象不注入。
- 只在 ``lifelike_member_mode == on`` 时注入；``shadow`` 只计数 ``lifelike.member.would_inject``。

注入预算 120 字，最多 1 个群友、3 条印象；措辞固定带来源与"不一定准"，经 NumericLeakGuard。
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from astrbot.api import logger

from ..social import privacy
from ..social.l2_filter import chat_variants

__all__ = ["valid_alias", "COMMON_WORDS", "MemberResolver", "MemberLens", "BLOCK_OPEN", "BLOCK_CLOSE"]

BLOCK_OPEN = "<你在群里对他的印象>"
BLOCK_CLOSE = "</你在群里对他的印象>"
INJECT_MAX_NOTES = 3
INJECT_BUDGET = 120

COMMON_WORDS = frozenset(
    "猫 狗 哥 姐 弟 妹 老师 同学 大佬 老板 群主 管理 管理员 朋友 大家 宝宝 宝贝 兄弟 姐妹 哥哥 姐姐 弟弟 妹妹 "
    "爸爸 妈妈 老公 老婆 先生 小姐 师傅 前辈 萌新 新人 路人 机器人 bot 你好 早安 晚安 哈哈 嗯嗯 好的 谢谢 "
    "今天 明天 昨天 时间 东西 什么 怎么 这个 那个 我们 你们 他们 她们 自己 群友 网友 小猫 小狗 猫猫 狗狗 "
    "user admin test 用户 匿名 未知用户 deleted account".split()
)

_AT_RX = re.compile(r"@([A-Za-z][A-Za-z0-9_]{3,31})")


def valid_alias(alias: str) -> bool:
    a = str(alias or "").strip()
    if len(a) < 2 or len(a) > 16:
        return False
    if a.lower() in COMMON_WORDS or a.isdigit():
        return False
    return True


class MemberResolver:
    """称呼 → member_id（MG-pull / MG-alias）"""

    def __init__(self, aliases: Sequence[Dict[str, Any]]) -> None:
        self._by_alias: Dict[str, Set[str]] = {}
        self._by_hash: Dict[str, Set[str]] = {}
        for a in aliases:
            mid = str(a.get("member_id") or "")
            if not mid:
                continue
            h = str(a.get("alias_hash") or "")
            if h:
                self._by_hash.setdefault(h, set()).add(mid)
                continue
            alias = str(a.get("alias") or "").strip()
            if valid_alias(alias):
                self._by_alias.setdefault(alias.lower(), set()).add(mid)

    def match(self, texts: Iterable[str]) -> Tuple[Optional[str], str]:
        """返回 (member_id, reason)；reason ∈ hit / none / ambiguous"""
        from .store import alias_hash

        hits: Set[str] = set()
        for text in texts:
            t = privacy.normalize(text or "")
            if not t:
                continue
            for alias, mids in self._by_alias.items():
                if alias in t:
                    hits |= mids
            for mt in _AT_RX.finditer(text or ""):
                hits |= self._by_hash.get(alias_hash(mt.group(1)), set())
        if not hits:
            return None, "none"
        if len(hits) > 1:
            return None, "ambiguous"
        return next(iter(hits)), "hit"


def _note_chats(note: Dict[str, Any]) -> Set[str]:
    import json

    try:
        refs = json.loads(note.get("evidence_refs") or "[]")
    except (TypeError, ValueError):
        return set()
    return {str(r).split(":", 1)[0] for r in refs if ":" in str(r)}


class MemberLens:
    def __init__(
        self,
        members: Any,
        *,
        mode_get: Callable[[], str],
        authorized_groups: Callable[[], Sequence[Any]],
        bump: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._members = members
        self._mode_get = mode_get
        self._groups = authorized_groups
        self._bump = bump

    @staticmethod
    def scope_allowed(scope_key: Any, sender_id: Any, owner_ids: Iterable[Any], purpose: str) -> bool:
        """MG-scope：仅主人私聊的被动回复"""
        if purpose != "reply":
            return False
        sid = str(sender_id or "").strip()
        owners = {str(o).strip() for o in owner_ids or () if str(o).strip()}
        return bool(sid) and sid in owners and str(scope_key or "") == f"private:{sid}"

    def render(
        self,
        scope_key: Any,
        sender_id: Any,
        owner_ids: Iterable[Any],
        texts: Sequence[str],
        *,
        purpose: str = "reply",
    ) -> str:
        """同步（调用方放 to_thread）。任何异常 → ''（不注入，聊天照常）。"""
        try:
            if not self.scope_allowed(scope_key, sender_id, owner_ids, purpose):
                return ""
            mode = self._mode_get()
            if mode not in ("shadow", "on"):
                return ""
            resolver = MemberResolver(self._members.aliases())
            mid, reason = resolver.match(texts)
            if mid is None:
                if reason == "ambiguous":
                    self._count("ambiguous")
                return ""
            block = self._block(mid)
            if not block:
                return ""
            if mode != "on":
                self._count("would_inject")
                return ""
            self._count("injected")
            return block
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[lifelike.member] render failed: {type(e).__name__}")
            return ""

    def _count(self, name: str) -> None:
        if self._bump is not None:
            try:
                self._bump(f"lifelike.member.{name}")
            except Exception:  # noqa: BLE001
                pass

    def _block(self, member_id: str) -> str:
        allowed: Set[str] = set()
        for g in self._groups() or ():
            allowed |= chat_variants(g)
        if not allowed:
            return ""
        aliases = [
            a for a in self._members.aliases(member_id)
            if not a.get("alias_hash") and valid_alias(str(a.get("alias") or ""))
        ]
        if not aliases:
            return ""
        aliases.sort(key=lambda a: (0 if a.get("source") == "owner_told" else 1, -float(a.get("last_seen") or 0)))
        name = str(aliases[0]["alias"])
        picked: List[str] = []
        budget = INJECT_BUDGET
        for n in self._members.notes(member_id):
            chats = _note_chats(n)
            if not chats or not (chats & allowed):
                continue  # R11：来源群已不授权 / 主人已离群
            text = str(n.get("text") or "").strip()
            if not text or privacy.is_sensitive(text):
                continue
            band = str(n.get("confidence_band") or "试探")
            if band not in ("试探", "较确定"):
                band = "试探"
            item = f"{text}（{band}）"
            if len(item) + 1 > budget:
                break
            picked.append(item)
            budget -= len(item) + 1
            if len(picked) >= INJECT_MAX_NOTES:
                break
        if not picked:
            return ""
        lines = [
            BLOCK_OPEN,
            f"{name}（你在群里见过的群友；下面只是你看群聊得来的印象，不一定准）：",
            "；".join(picked) + "。",
            "聊到他时可以自然接话，说清是在群里看到的；别当成事实下结论，也别打听他的私事。",
            BLOCK_CLOSE,
        ]
        text = "\n".join(lines)
        try:
            from ..guards.numeric import guard_text

            text = guard_text(text, source="member_lens", strict=True)
        except Exception:  # noqa: BLE001
            pass
        return text if BLOCK_OPEN in text and "（" in text else ""
