"""MemberImpressionLedger：从授权群公开发言形成群友的轻量印象（ROADMAP P1-13 基础版，设计 §6.8.2～6.8.5）。

规则优先、零 LLM（``lifelike_member_llm`` 改写未实现，本模块不发起任何模型调用）。

**授权**（§6.8.2，U3）：``lifelike_member_groups`` ∩ ``continuity.observe_chat_ids``（不含
``owner_channel_ids`` 与私聊 id）。``lifelike_member_groups`` 支持通配：``*`` = 全部采集群；
``*whitelist`` = 平台 ``platform_settings.id_whitelist`` 里的全部群（动态读取，新加白名单的群自动纳入）；
可与显式 id 混写。"主人 60 天内在群里发过言"（``author_kind='owner'``）由
``lifelike_member_require_owner_present`` 控制：``auto``（默认）= 只对显式群列表要求（与 73edc2c 一致），
通配时不要求；``true`` / ``false`` 强制。不满足的群不产生新印象，来自该群的已有印象也不注入（lens 侧再判一次）。

**跨群合并**（U3）：档案按 Telegram 用户 id（``observed_posts.sender_id``）一人一份——来源群记为集合
（``member_profiles.chat_ids``），话题用该人在所有授权群的公开发言合并重算（同一话题一条，
天数跨群累计），称呼来自 Iris 画像（与群无关）；墓碑 / 未成年 / "别记我" 按人生效，跨群。

**新纳入群回补**：游标里记录已处理过的授权群集合；新出现的授权群从头（rowid≤游标、近 30 天）分批回补，
每轮最多 ``BACKFILL_BATCH`` 条，补完并入已处理集合。旧格式游标（无群集合）视为全部授权群都需回补（幂等）。

**群友** = 授权群里 ``author_kind='other'`` 且 ``sender_id`` 非空的发言者。

**每次运行**（随 continuity 维护周期，至少间隔 ``RUN_INTERVAL_S``）：
1. 按游标 ``member_impr``（cursors 表，observed_posts 的 rowid）增量读新帖；游标在写库成功后才推进。
2. 逐帖：已有墓碑（member / member_minor）→ 跳过；可识别为未成年的自述 → 整人删除 + ``member_minor``
   墓碑（§6.8.1）；"别记我"→ 整人删除 + ``member`` 墓碑（§6.8.5 第 3 条）；转发/命令/纯链接/代码块
   不计；敏感类别 → 不计（计数 ``sensitive_dropped``）。其余更新 ``member_profiles``。
3. 对本轮有新发言的群友，用其近 30 天授权群公开发言（同样过闸门）重算话题：
   2～6 字的词在 ≥2 个自然日出现、不是群里人人都在说的泛词、不命中地点/机构/医疗等话题词典 →
   ``topic`` 印象（模板转述"最近在群里聊过「…」"，≤30 字，与原帖连续重合 ≥8 字拒绝）。
   跨 ≥3 个自然日为"较确定"，否则"试探"；永远不是"确定"。每人生效 topic ≤6 条。
4. 称呼：Iris 画像 ``user_name`` / ``historical_names``（只读）→ ``member_aliases(source=profile)``。
5. 过期：topic 30 天无新证据 → expired；群友 60 天无发言 → 全部 expired；90 天 → 物理删除。

不记活动时间（``last_seen`` 只用于过期与主人命令里的"最近出现日期"）；不写人物小传。
日志 ``[lifelike.member]`` 只记哈希前 8 位与计数。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from astrbot.api import logger

from ...continuity import models as m
from ..memory import tombstones as tb
from ..social import privacy
from ..social.l2_filter import chat_variants, split_group_tokens
from . import nickname as nk
from .lens import valid_alias
from .store import MemberStore, member_ref

__all__ = ["MemberImpressionLedger", "LedgerReport", "extract_terms", "KEY_MODE", "KEY_GROUPS"]

KEY_MODE = "lifelike_member_mode"
KEY_GROUPS = "lifelike_member_groups"
KEY_REQUIRE_OWNER = "lifelike_member_require_owner_present"
CURSOR = "member_impr"
DAY = 86400.0
OWNER_PRESENT_S = 60 * DAY
WINDOW_S = 30 * DAY
NICK_RECHECK_S = DAY  # 群友称呼每人每天最多重算一次
TOPIC_TTL_S = 30 * DAY
MEMBER_EXPIRE_S = 60 * DAY
MEMBER_PURGE_S = 90 * DAY
RUN_INTERVAL_S = 3600.0
BATCH = 2000
BACKFILL_BATCH = 1000
MAX_TOPICS = 6
MIN_DAYS = 2
SURE_DAYS = 3
NOTE_MAX = 30
QUOTE_OVERLAP = 8
TERM_MIN, TERM_MAX = 2, 6
GENERIC_MEMBER_RATIO = 0.5  # 窗口内超过一半活跃群友都说过的词视为泛词
EVIDENCE_REFS = 5
GENERIC_SCAN_MAX = 5000

_URL_ONLY = re.compile(r"^\s*(?:https?://\S+\s*)+$", re.I)
_CODE = re.compile(r"```|^\s{4,}\S", re.M)
_COMMAND = re.compile(r"^\s*[/!！#＃]\S*")
_TOKEN = re.compile(r"[一-鿿]+|[A-Za-z][A-Za-z0-9+._-]{1,15}|\d{3,5}[A-Za-z]{0,3}")
_STOP = frozenset(
    "我们 你们 他们 她们 自己 这个 那个 这些 那些 就是 不是 没有 什么 怎么 这样 那样 可以 因为 所以 但是 而且 "
    "还是 或者 如果 已经 正在 知道 觉得 感觉 应该 可能 现在 今天 明天 昨天 时候 地方 东西 事情 问题 一下 一点 "
    "一些 一样 一直 一个 每个 哪个 这么 那么 真的 确实 好的 好吧 是的 对呀 不要 不用 不能 不会 哈哈 嘿嘿 嘻嘻 "
    "啊啊 嗯嗯 哦哦 大家 有人 怎么样 为什么 然后 还有 其实 感觉 看看 一起 时间 晚上 早上 中午 下午 上午 刚才 "
    "最近 以前 之前 之后 今年 明年 去年 周末 http https www com the and you for this that with".split()
)
_NOISE = frozenset("的吗呢吧啊呀嘛哦哈啦了着过和跟与或又在就都也很还把被让向从往对于是我你他她它这那有个不没")


def _valid_term(term: str) -> bool:
    if not term or term in _STOP or term.isdigit():
        return False
    if len(set(term)) == 1:
        return False
    if term[0] in _NOISE or term[-1] in _NOISE:
        return False
    return not privacy.blocked_topic(term)


def extract_term_contexts(text: str) -> Dict[str, Set[Tuple[str, str]]]:
    """候选话题词 → 出现时的 (左邻字, 右邻字) 集合。中文取 2～6 字 n-gram，英文/型号词整词。

    没有分词器：真正的词在不同句子里左右邻字会变化，固定句子里截出来的窗口邻字不变——
    用"左右邻都至少出现两种"筛掉窗口碎片（与 jargon 候选的左右邻思路一致）。纯函数。
    """
    norm = privacy.normalize(text)
    out: Dict[str, Set[Tuple[str, str]]] = defaultdict(set)
    for mt in _TOKEN.finditer(norm):
        tok = mt.group(0)
        start, end = mt.start(), mt.end()
        if not _is_cjk(tok[0]):
            if TERM_MIN <= len(tok) <= 16 and _valid_term(tok):
                left = norm[start - 1] if start > 0 else "^"
                right = norm[end] if end < len(norm) else "$"
                out[tok].add((left, right))
            continue
        n = len(tok)
        for size in range(TERM_MIN, min(TERM_MAX, n) + 1):
            for i in range(n - size + 1):
                t = tok[i : i + size]
                if _valid_term(t):
                    left = tok[i - 1] if i > 0 else "^"
                    right = tok[i + size] if i + size < n else "$"
                    out[t].add((left, right))
    return out


def _is_cjk(ch: str) -> bool:
    return 0x4E00 <= ord(ch) <= 0x9FFF


def extract_terms(text: str) -> Set[str]:
    return set(extract_term_contexts(text))


def _countable(post: Dict[str, Any]) -> bool:
    text = str(post.get("text") or "")
    if not text.strip() or int(post.get("is_forward") or 0):
        return False
    if _COMMAND.match(text) or _URL_ONLY.match(text) or _CODE.search(text):
        return False
    return True


def _longest_overlap(a: str, b: str) -> int:
    if not a or not b:
        return 0
    best = 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def quotes_post(text: str, posts: Iterable[str]) -> bool:
    """与任一原帖连续重合 ≥8 字（MG-quote）"""
    return any(_longest_overlap(text, p) >= QUOTE_OVERLAP for p in posts)


@dataclass
class LedgerReport:
    status: str = "off"
    posts: int = 0
    members: int = 0
    notes_new: int = 0
    notes_refresh: int = 0
    notes_expired: int = 0
    sensitive_dropped: int = 0
    minors: int = 0
    forget_requests: int = 0
    aliases: int = 0
    sweep: Dict[str, int] = field(default_factory=dict)


ProfileNames = Callable[[str], Awaitable[List[str]]]


class MemberImpressionLedger:
    def __init__(
        self,
        store: Any,
        *,
        config_get: Callable[[str, Any], Any],
        observe_chat_ids: Callable[[], Sequence[Any]],
        owner_channel_ids: Callable[[], Sequence[Any]] = lambda: (),
        profile_names: Optional[ProfileNames] = None,
        clock: Callable[[], float] = time.time,
        id_whitelist: Callable[[], Optional[Sequence[Any]]] = lambda: None,
    ) -> None:
        self._store = store
        self._cfg = config_get
        self._observe = observe_chat_ids
        self._channels = owner_channel_ids
        self._whitelist = id_whitelist
        self._profile_names = profile_names
        self._clock = clock
        self.members = MemberStore(store, clock)
        self._last_run = 0.0
        self._generic_cache: Tuple[Any, Set[str]] = (None, set())
        self._fails = 0
        self._paused_until = 0.0

    # ------------------------------------------------------------ config
    def mode(self) -> str:
        try:
            v = str(self._cfg(KEY_MODE, "off") or "off").strip().lower()
        except Exception:  # noqa: BLE001
            return "off"
        return v if v in ("off", "shadow", "on") else "off"

    def _owner_present(self, chat_id: int, now: float) -> bool:
        rows = tb.read_rows(
            self._store,
            "SELECT 1 AS x FROM observed_posts WHERE chat_id=? AND author_kind='owner' AND posted_at>=? LIMIT 1",
            (int(chat_id), now - OWNER_PRESENT_S),
        )
        return bool(rows)

    def _require_owner(self, wildcard: bool) -> bool:
        try:
            v = self._cfg(KEY_REQUIRE_OWNER, "auto")
        except Exception:  # noqa: BLE001
            v = "auto"
        if isinstance(v, bool):
            return v
        s = str(v if v is not None else "auto").strip().lower()
        if s in ("1", "true", "on", "yes"):
            return True
        if s in ("0", "false", "off", "no"):
            return False
        return not wildcard  # auto：显式群列表要求主人在群（73edc2c 行为），通配不要求

    def groups_for(self, value: Any, now: Optional[float] = None) -> List[int]:
        """按群配置值（显式 id / ``*`` / ``*whitelist``）选出授权群：∩ 采集群 − 主人频道 − 私聊 id；
        需要时再要求主人 60 天内在群发过言。异常 → []。"""
        now = float(self._clock() if now is None else now)
        try:
            all_flag, wl_flag, explicit = split_group_tokens(value)
            wanted: Set[str] = set()
            for g in explicit:
                wanted |= chat_variants(g)
            wl: Set[str] = set()
            if wl_flag:
                try:
                    for g in self._whitelist() or ():
                        wl |= chat_variants(str(g).split("#", 1)[0])
                except Exception:  # noqa: BLE001 - 读不到白名单 → 通配白名单不扩大范围
                    wl = set()
            channels: Set[str] = set()
            for c in self._channels() or ():
                channels |= chat_variants(c)
            need_owner = self._require_owner(all_flag or wl_flag)
            out: List[int] = []
            for cid in self._observe() or ():
                variants = chat_variants(cid)
                if variants & channels:
                    continue
                try:
                    icid = int(cid)
                except (TypeError, ValueError):
                    continue
                if variants & wanted:
                    pass
                elif icid < 0 and (all_flag or (variants & wl)):
                    pass
                else:
                    continue
                if icid in out:
                    continue
                if need_owner and not self._owner_present(icid, now):
                    continue
                out.append(icid)
            return out
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[lifelike.member] authorized_groups failed: {type(e).__name__}")
            return []

    def authorized_groups(self, now: Optional[float] = None) -> List[int]:
        """授权群（int chat_id，observed_posts 的写法）：见模块说明"""
        try:
            value = self._cfg(KEY_GROUPS, "")
        except Exception:  # noqa: BLE001
            return []
        return self.groups_for(value, now)

    # ------------------------------------------------------------ run
    async def maybe_run(self, force: bool = False) -> Optional[LedgerReport]:
        if self.mode() == "off":
            return None
        now = float(self._clock())
        if not force and (now - self._last_run < RUN_INTERVAL_S or now < self._paused_until):
            return None
        self._last_run = now
        try:
            rep = await self.run_once(now)
            self._fails = 0
            return rep
        except Exception as e:  # noqa: BLE001 - 失败：游标不动，下次再来；连续 5 次熔断 1 小时
            self._fails += 1
            logger.warning(f"[lifelike.member] run failed: {type(e).__name__}")
            if self._fails >= 5:
                self._fails = 0
                self._paused_until = now + 3600.0
                logger.warning("[lifelike.member] paused_error: 5 consecutive failures, pause 3600s")
            return None

    def _cursor_state(self) -> Dict[str, Any]:
        """游标 = {"rowid": 已处理的 observed_posts 最大 rowid（迟到的离线补采帖子也不会漏）,
        "groups": 已处理过的授权群, "bf": 新纳入群回补进度 {"groups","upto","after"}}；
        旧格式只有 rowid（groups=None：视为全部授权群都需回补）"""
        raw = self._store.get_cursor(CURSOR)
        try:
            data = json.loads(raw) if raw else {}
            rowid = int(data.get("rowid", 0)) if isinstance(data, dict) else 0
        except (ValueError, TypeError, AttributeError):
            return {"rowid": 0, "groups": [], "bf": None}
        if not isinstance(data, dict) or not raw:
            return {"rowid": 0, "groups": [], "bf": None}
        groups = data.get("groups")
        if isinstance(groups, list):
            try:
                groups = sorted({int(g) for g in groups})
            except (TypeError, ValueError):
                groups = None
        else:
            groups = None
        bf = data.get("bf") if isinstance(data.get("bf"), dict) else None
        return {"rowid": rowid, "groups": groups, "bf": bf}

    def _cursor(self) -> int:
        return int(self._cursor_state()["rowid"])

    def _save_cursor(self, state: Dict[str, Any]) -> None:
        out: Dict[str, Any] = {"rowid": int(state["rowid"])}
        if state.get("groups") is not None:
            out["groups"] = sorted({int(g) for g in state["groups"]})
        if state.get("bf"):
            out["bf"] = state["bf"]
        self._store.set_cursor(CURSOR, json.dumps(out, sort_keys=True))

    def _backfill_posts(self, groups: Sequence[int], after_rowid: int, upto_rowid: int, now: float) -> List[Dict[str, Any]]:
        return tb.read_rows(
            self._store,
            f"SELECT rowid AS _rowid, * FROM observed_posts WHERE rowid>? AND rowid<=?"
            f" AND chat_id IN ({','.join('?' * len(groups))}) AND posted_at>=? ORDER BY rowid LIMIT ?",
            [int(after_rowid), int(upto_rowid), *[int(g) for g in groups], now - WINDOW_S, BACKFILL_BATCH],
        )

    def _posts_after(self, groups: Sequence[int], after_rowid: int, now: float) -> List[Dict[str, Any]]:
        return tb.read_rows(
            self._store,
            f"SELECT rowid AS _rowid, * FROM observed_posts WHERE rowid>? AND chat_id IN ({','.join('?' * len(groups))})"
            " AND posted_at>=? ORDER BY rowid LIMIT ?",
            [int(after_rowid), *[int(g) for g in groups], now - WINDOW_S, BATCH],
        )

    async def run_once(self, now: float) -> LedgerReport:
        rep = LedgerReport(status=self.mode())
        await asyncio.to_thread(self.members.ensure)
        groups = await asyncio.to_thread(self.authorized_groups, now)
        touched: Set[str] = set()
        backfilled = 0
        if groups:
            state = await asyncio.to_thread(self._cursor_state)
            after = int(state["rowid"])
            posts = await asyncio.to_thread(self._posts_after, groups, after, now)
            # 新纳入的授权群：游标之前的近 30 天帖子分批回补（每轮 ≤BACKFILL_BATCH 条）
            # known 不与本轮授权群取交集：白名单暂时读不到（*whitelist 展开为空）时，
            # 已处理过的群不会掉出 known，恢复后也不会被当成新群整段重扫
            known = set(state["groups"]) if state["groups"] is not None else set()
            bf = state.get("bf")
            if bf:
                try:
                    bf_groups = [int(g) for g in bf.get("groups") or []]
                    bf_upto, bf_after = int(bf.get("upto", 0)), int(bf.get("after", 0))
                except (TypeError, ValueError):
                    bf_groups, bf_upto, bf_after = [], 0, 0
            else:
                bf_groups, bf_upto, bf_after = [], 0, 0
            pending = sorted(set(groups) - known - set(bf_groups))
            if pending:
                # 有新群：并入回补并从头补到当前游标（进行中的回补群重扫一遍，写入幂等）
                bf_groups = sorted(set(bf_groups) | set(pending))
                bf_upto, bf_after = max(bf_upto, after), 0
            bf_posts: List[Dict[str, Any]] = []
            bf_live = [g for g in bf_groups if g in groups]  # 只读当前仍授权的群
            if bf_live and bf_upto > bf_after:
                bf_posts = await asyncio.to_thread(self._backfill_posts, bf_live, bf_after, bf_upto, now)
            all_posts = list(bf_posts) + list(posts)
            rep.posts = len(all_posts)
            backfilled = len(bf_posts)
            touched = await asyncio.to_thread(self._ingest, all_posts, rep)
            for mid in sorted(touched):
                await asyncio.to_thread(self._recompute_topics, mid, groups, now, rep)
                if await asyncio.to_thread(self._blocked_member, mid):
                    continue  # 重算时发现未成年 / 别记我 → 已墓碑，不再写别名
                await self._refresh_aliases(mid, rep)
                await self._learn_nickname(mid, groups, now)
            new_state: Dict[str, Any] = {"rowid": after, "groups": sorted(known), "bf": None}
            if posts:
                new_state["rowid"] = max(int(p["_rowid"]) for p in posts)
            if bf_groups:
                if len(bf_posts) >= BACKFILL_BATCH:
                    last = max(int(p["_rowid"]) for p in bf_posts)
                    new_state["bf"] = {"groups": bf_groups, "upto": bf_upto, "after": last}
                elif len(bf_live) < len(bf_groups):
                    # 部分回补群本轮暂时不在授权里：保留进度，等授权恢复再补完（不提前标记为已处理）
                    new_state["bf"] = {"groups": bf_groups, "upto": bf_upto, "after": bf_after}
                else:
                    new_state["groups"] = sorted(known | set(bf_groups))
            if new_state != state:
                await asyncio.to_thread(self._save_cursor, new_state)
        rep.members = len(touched)
        rep.sweep = await asyncio.to_thread(
            self.members.sweep, now, member_expire_s=MEMBER_EXPIRE_S, member_purge_s=MEMBER_PURGE_S
        )
        await asyncio.to_thread(self._bump, rep)
        if rep.posts or any(rep.sweep.values()):
            logger.info(
                f"[lifelike.member] run mode={rep.status} groups={len(groups)} posts={rep.posts}"
                f" backfill={backfilled}"
                f" members={rep.members} note_new={rep.notes_new} note_refresh={rep.notes_refresh}"
                f" note_expired={rep.notes_expired} sensitive_dropped={rep.sensitive_dropped}"
                f" minors={rep.minors} forget_req={rep.forget_requests} aliases={rep.aliases}"
                f" sweep={json.dumps(rep.sweep, sort_keys=True)}"
            )
        return rep

    def _bump(self, rep: LedgerReport) -> None:
        for name, v in (
            ("note_new", rep.notes_new),
            ("note_expired", rep.notes_expired + rep.sweep.get("expired", 0) + rep.sweep.get("member_expired", 0)),
            ("sensitive_dropped", rep.sensitive_dropped),
            ("minor", rep.minors),
            ("forget_request", rep.forget_requests),
            ("purged", rep.sweep.get("purged", 0)),
        ):
            if v:
                try:
                    self._store.bump_counter(f"lifelike.member.{name}", int(v))
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------ ingest（worker thread）
    def _blocked_member(self, member_id: str) -> bool:
        return tb.has_tombstone(self._store, tb.member_hash(member_id))

    def forget_member(self, member_id: str, kind: str = tb.KIND_MEMBER) -> int:
        n = self.members.delete_member(member_id)
        tb.add_tombstone(self._store, kind, tb.member_hash(member_id), float(self._clock()))
        logger.info(f"[lifelike.member] op=forget member={member_ref(member_id)} kind={kind} notes={n}")
        return n

    def _ingest(self, posts: Sequence[Dict[str, Any]], rep: LedgerReport) -> Set[str]:
        touched: Set[str] = set()
        blocked: Dict[str, bool] = {}
        for p in posts:
            if p.get("author_kind") != "other":
                continue
            mid = str(p.get("sender_id") or "").strip()
            if not mid:
                continue
            if mid not in blocked:
                blocked[mid] = self._blocked_member(mid)
            if blocked[mid]:
                continue
            text = str(p.get("text") or "")
            if not int(p.get("is_forward") or 0) and privacy.minor_signal(text):
                self.forget_member(mid, tb.KIND_MEMBER_MINOR)
                blocked[mid] = True
                touched.discard(mid)
                rep.minors += 1
                continue
            if not int(p.get("is_forward") or 0) and privacy.forget_me_request(text):
                self.forget_member(mid, tb.KIND_MEMBER)
                blocked[mid] = True
                touched.discard(mid)
                rep.forget_requests += 1
                continue
            if not _countable(p):
                continue
            if privacy.is_sensitive(text):
                rep.sensitive_dropped += 1
                continue
            self.members.touch_profile(mid, p["chat_id"], float(p["posted_at"]))
            touched.add(mid)
        return touched

    def _member_posts(self, member_id: str, groups: Sequence[int], now: float) -> List[Dict[str, Any]]:
        if not groups:
            return []
        return tb.read_rows(
            self._store,
            f"SELECT chat_id,msg_id,posted_at,text,is_forward FROM observed_posts WHERE sender_id=?"
            f" AND author_kind='other' AND chat_id IN ({','.join('?' * len(groups))}) AND posted_at>=?"
            " ORDER BY posted_at",
            [member_id, *[int(g) for g in groups], now - WINDOW_S],
        )

    def _generic_terms(self, groups: Sequence[int], now: float) -> Tuple[Set[str], int]:
        """泛词 = 任一授权群里被该群超过一半活跃群友用过的词（按群分别算再取并集：
        某个群人人都在说的群主题词，不当成跨群合并后某个人的个人话题）"""
        out: Set[str] = set()
        total = 0
        for g in groups:
            terms, n = self._generic_terms_one(int(g), now)
            out |= terms
            total += n
        return out, total

    def _generic_terms_one(self, group: int, now: float) -> Tuple[Set[str], int]:
        rows = tb.read_rows(
            self._store,
            "SELECT sender_id,text,is_forward FROM observed_posts WHERE author_kind='other'"
            " AND chat_id=? AND posted_at>=? AND sender_id!=''"
            " ORDER BY posted_at DESC LIMIT ?",
            [int(group), now - WINDOW_S, GENERIC_SCAN_MAX],
        )
        by_member: Dict[str, Set[str]] = defaultdict(set)
        for r in rows:
            if _countable(r) and not privacy.is_sensitive(str(r.get("text") or "")):
                by_member[str(r["sender_id"])] |= extract_terms(str(r["text"]))
        n = len(by_member)
        if n < 4:
            return set(), n
        freq: Dict[str, int] = defaultdict(int)
        for terms in by_member.values():
            for t in terms:
                freq[t] += 1
        return {t for t, c in freq.items() if c / n > GENERIC_MEMBER_RATIO}, n

    def _recompute_topics(self, member_id: str, groups: Sequence[int], now: float, rep: LedgerReport) -> None:
        if self._blocked_member(member_id):
            return
        raw_posts = self._member_posts(member_id, groups, now)
        # 跨群读出的帖子里可能有还没轮到回补的"我才初二""别记我"：先扫一遍，命中直接墓碑
        for p in raw_posts:
            if int(p.get("is_forward") or 0):
                continue
            text = str(p.get("text") or "")
            if privacy.minor_signal(text):
                self.forget_member(member_id, tb.KIND_MEMBER_MINOR)
                rep.minors += 1
                return
            if privacy.forget_me_request(text):
                self.forget_member(member_id, tb.KIND_MEMBER)
                rep.forget_requests += 1
                return
        posts = [
            p for p in raw_posts
            if _countable(p) and not privacy.is_sensitive(str(p.get("text") or ""))
        ]
        if not posts:
            return
        if self._generic_cache[0] != (tuple(groups), int(now // 3600)):
            self._generic_cache = ((tuple(groups), int(now // 3600)), self._generic_terms(groups, now)[0])
        generic = self._generic_cache[1]
        days: Dict[str, Set[str]] = defaultdict(set)
        refs: Dict[str, List[Tuple[float, str]]] = defaultdict(list)
        last_ts: Dict[str, float] = {}
        texts: Dict[str, List[str]] = defaultdict(list)
        lefts: Dict[str, Set[str]] = defaultdict(set)
        rights: Dict[str, Set[str]] = defaultdict(set)
        for p in posts:
            day = m.local_day(float(p["posted_at"]))
            for t, ctx in extract_term_contexts(str(p["text"])).items():
                if t in generic:
                    continue
                for left, right in ctx:
                    lefts[t].add(left)
                    rights[t].add(right)
                days[t].add(day)
                refs[t].append((float(p["posted_at"]), f"{p['chat_id']}:{p['msg_id']}"))
                last_ts[t] = max(last_ts.get(t, 0.0), float(p["posted_at"]))
                texts[t].append(str(p["text"]))
        cands = [
            t for t, d in days.items()
            if len(d) >= MIN_DAYS and len(lefts[t]) >= 2 and len(rights[t]) >= 2
        ]
        # 天数多的优先，同天数长词优先；与已选词互相包含的去掉
        cands.sort(key=lambda t: (-len(days[t]), -len(t), -last_ts[t]))
        chosen: List[str] = []
        for t in cands:
            if any(t in c or c in t for c in chosen):
                continue
            chosen.append(t)
            if len(chosen) >= MAX_TOPICS:
                break
        keep_ids: Set[str] = set()
        for t in chosen:
            if tb.has_tombstone(self._store, tb.note_hash(member_id, t)):
                continue
            if tb.text_hits_topic_tombstone(self._store, t):
                continue
            text = f"最近在群里聊过「{t}」"[:NOTE_MAX]
            if privacy.is_sensitive(text) or quotes_post(text, texts[t]):
                rep.sensitive_dropped += 1
                continue
            nid = hashlib.sha256(f"{member_id}|topic|{t}".encode("utf-8")).hexdigest()[:16]
            ev = [r for _, r in sorted(refs[t])[-EVIDENCE_REFS:]]
            row = {
                "note_id": nid,
                "member_id": member_id,
                "kind": "topic",
                "topic_key": t,
                "text": text,
                "evidence_refs": json.dumps(ev),
                "evidence_days": len(days[t]),
                "confidence_band": "较确定" if len(days[t]) >= SURE_DAYS else "试探",
                "detector": "rule",
                "first_seen": float(self._clock()),
                "last_seen": last_ts[t],
                "expires_at": last_ts[t] + TOPIC_TTL_S,
                "status": "active",
            }
            res = self.members.upsert_note(row)
            keep_ids.add(nid)
            if res == "new":
                rep.notes_new += 1
                logger.info(f"[lifelike.member] op=note_add member={member_ref(member_id)} kind=topic det=rule")
            else:
                rep.notes_refresh += 1
        # 每人生效 topic ≤6：不在本轮选中的旧 topic 过期
        stale = [n["note_id"] for n in self.members.notes(member_id) if n["kind"] == "topic" and n["note_id"] not in keep_ids]
        rep.notes_expired += self.members.expire_notes(stale)

    def nickname_on(self) -> bool:
        try:
            v = self._cfg("lifelike_member_nickname", "off")
        except Exception:  # noqa: BLE001
            return False
        return str(v if v is not None else "off").strip().lower() in ("on", "true", "1")

    async def _learn_nickname(self, member_id: str, groups: Sequence[int], now: float) -> None:
        """群友常用称呼：数授权群近 30 天其他人发言里各候选叫法出现的次数（lifelike_member_nickname=on）。"""
        if not self.nickname_on() or self._profile_names is None or not groups:
            return
        checked = getattr(self, "_nick_checked", None)
        if checked is None:
            checked = self._nick_checked = {}
        if now - checked.get(member_id, 0.0) < NICK_RECHECK_S:
            return  # 审查 L7：每人每天最多重算一次（LIKE 扫描有成本）
        checked[member_id] = now
        if len(checked) > 5000:
            checked.clear()
        try:
            names = [n for n in (await self._profile_names(member_id) or []) if not str(n).startswith("@")]
            cands = [c for c in nk.candidates(names) if not privacy.is_sensitive(c)]
            if not cands:
                return
            usage = await asyncio.to_thread(self._nick_usage, member_id, list(groups), cands, now)
            await asyncio.to_thread(self.members.set_nick_usage, member_id, usage)
            best = nk.choose(usage)
            if best:
                await asyncio.to_thread(self.members.upsert_alias, member_id, best, nk.SOURCE_NICK)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[lifelike.member] nickname learn failed: {type(e).__name__}")

    def _nick_usage(self, member_id: str, groups: Sequence[int], cands: Sequence[str], now: float) -> Dict[str, Any]:
        likes = " OR ".join("text LIKE ?" for _ in cands)
        rows = tb.read_rows(
            self._store,
            f"SELECT text, posted_at, sender_id FROM observed_posts WHERE chat_id IN ({','.join('?' * len(groups))})"
            f" AND sender_id<>? AND author_kind IN ('other','owner') AND is_forward=0 AND posted_at>=?"
            f" AND ({likes}) ORDER BY posted_at DESC LIMIT 800",
            [*[int(g) for g in groups], str(member_id), now - WINDOW_S, *[f"%{c}%" for c in cands]],
        )
        return nk.count_usage(((r["text"], r["posted_at"], r["sender_id"]) for r in rows), cands)

    async def _refresh_aliases(self, member_id: str, rep: LedgerReport) -> None:
        if self._profile_names is None:
            return
        try:
            names = await self._profile_names(member_id)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[lifelike.member] profile names failed: {type(e).__name__}")
            return
        for name in names or []:
            name = str(name or "").strip()
            if name.startswith("@"):
                continue  # 账号标识不落库
            if not valid_alias(name) or privacy.is_sensitive(name):
                continue
            await asyncio.to_thread(self.members.upsert_alias, member_id, name, "profile")
            rep.aliases += 1
