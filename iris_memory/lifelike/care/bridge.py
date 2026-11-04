"""群→私聊关心桥（设计 §7.9；ROADMAP P1-12）

``CareBridge`` = ``CareSignalScanner`` + ``CareCandidateBuilder`` + 每 10 分钟一轮的低频任务：

1. 增量读 ``observed_posts``（``store.posts_since`` + 游标 ``care_bridge``），只留主人本人在
   授权群里的发言（``author_kind='owner'`` 且 ``sender_id ∈ owner_ids()``、非转发、非命令）；
   群友发言一律不读、不送模型；
2. 规则初筛 → 模糊项批量送廉价模型（``l1_buffer.summary_provider``，计入 P0 预算，模块日上限）；
3. 合并 episode → 抑制检查（私聊已说过 / 冷却 / 关闭）→ 决定 push 还是顺口提 →
   入 P1-8 候选池（``ctype='group_care'``，只能是主人私聊）。

开关 ``lifelike_care_bridge_mode``：off 不读不写；shadow 只检测、落 ``care_signals``、写日志，不入队；
on 入队。push 还需要 ``lifelike_initiative_mode=on`` 与 ``lifelike_care_bridge_push=true``，否则只顺口提。
日志只记 id / 类别 / 档位 / 原因，不记正文、不记 gist。任何异常：本轮跳过、游标不前进，不影响聊天。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from astrbot.api import logger

from ...continuity import models as m
from ..budget import CheapBudgetExceeded
from ..initiative.gate import CTYPE_GROUP_CARE, DELIVERY_PASSIVE, DELIVERY_PUSH, PRIVACY_OWNER_ONLY
from ..initiative.pool import CandidatePool, InitiativeCandidate
from ..social import privacy
from . import rules as r
from . import store as cs

KEY_MODE = "lifelike_care_bridge_mode"
KEY_GROUPS = "lifelike_care_bridge_groups"
KEY_CATS = "lifelike_care_bridge_categories"
KEY_PUSH = "lifelike_care_bridge_push"
KEY_CHANNEL = "lifelike_care_include_owner_channel"
KEY_LLM = "lifelike_care_llm"
KEY_LLM_CAP = "lifelike_care_llm_daily_cap"

DEFAULT_MODE = "on"
DEFAULT_GROUPS = "*"
DEFAULT_CATS = ",".join(r.CATEGORIES)
DEFAULT_LLM_CAP = 8

MODULE = "lifelike_care"
_LOG = "[lifelike.care]"

PERIOD_S = 600.0
FIRST_DELAY_S = 60.0
FIRST_RUN_LOOKBACK_S = 3600.0
LATE_LOOKBACK_S = 48 * 3600.0  # rowid 游标下迟到帖子的最长回看（超过最长有效期的信号没有意义）
POST_FETCH_LIMIT = 500
NEIGHBOR_S = 600.0
LLM_BATCH = 10
LLM_TIMEOUT_S = 45.0
FAIL_LIMIT = 5
PAUSE_S = 3600.0
RETENTION_INTERVAL_S = 86400.0
GIST_CLEAR_S = 30 * 86400.0
DELETE_S = 90 * 86400.0
PRIVATE_KNOWN_LOOKBACK_S = 6 * 3600.0
REPLIED_IN_GROUP_S = 600.0
OWNER_ACTIVE_S = 30 * 60.0
CATEGORY_COOLDOWN_S = 24 * 3600.0

EPISODE_S = {"sick": 72 * 3600.0}
EPISODE_DEFAULT_S = 12 * 3600.0
EXPIRY_S = {
    ("sick", r.BAND_STRONG): 48 * 3600.0,
    ("sick", r.BAND_LIGHT): 48 * 3600.0,
    ("mood_low", r.BAND_STRONG): 24 * 3600.0,
    ("good_news", r.BAND_STRONG): 36 * 3600.0,
    ("good_news", r.BAND_LIGHT): 36 * 3600.0,
}
EXPIRY_DEFAULT_S = 12 * 3600.0

MODE_OFF, MODE_SHADOW, MODE_ON = "off", "shadow", "on"

_LLM_PROMPT = (
    "下面是同一个人在群聊里自己说的几段话（只有她本人的话，没有别人的消息）。"
    "逐段判断她是不是在说**她自己**此刻的状态，以及属于哪一类：\n"
    "tired=累/困/熬夜，sick=身体不舒服，overtime=加班/赶工，mood_low=情绪差，good_news=有好事，none=都不是。\n"
    "玩笑、夸张、接梗、引用别人的话、说别人的事、问别人的问题，都算 about_self=false 或 is_joke=true。\n"
    "crisis 只在明确表达伤害自己的想法时为 true。\n"
    "只输出 JSON 数组，每段一个对象："
    '[{"i":序号,"about_self":true/false,"category":"tired|sick|overtime|mood_low|good_news|none",'
    '"intensity":"轻|明显","is_joke":true/false,"crisis":true/false,"gist":"以“说”开头、不超过24字的概括（例如“说加班到很晚”），不照抄原话、不出现任何人名"}]\n\n'
)


def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(float(ts), m.TZ_SHANGHAI)


def next_range_start(ts: float, rng: Optional[Tuple[int, int]]) -> Optional[float]:
    """ts 之后（含当前已处在区间内 → 返回 ts）下一次进入 [start,end) 分钟区间的时间戳"""
    if rng is None:
        return None
    start, end = rng
    t = _local(ts)
    minute = t.hour * 60 + t.minute
    inside = (start <= minute < end) if start < end else (minute >= start or minute < end)
    if inside:
        return float(ts)
    day0 = t.replace(hour=0, minute=0, second=0, microsecond=0)
    cand = day0 + timedelta(minutes=start)
    if cand.timestamp() <= ts:
        cand += timedelta(days=1)
    return cand.timestamp()


def parse_groups(raw: Any) -> Tuple[bool, set]:
    """'*' → (True, ∅)：全部已采集群；否则 (False, {chat_id})"""
    if isinstance(raw, (list, tuple, set)):
        items = [str(x) for x in raw]
    else:
        items = str(raw if raw is not None else "").replace("，", ",").replace(" ", ",").split(",")
    out = set()
    wildcard = False
    for it in items:
        s = it.strip()
        if not s:
            continue
        if s == "*":
            wildcard = True
            continue
        try:
            out.add(int(s))
        except ValueError:
            continue
    return wildcard, out


def parse_categories(raw: Any) -> set:
    if isinstance(raw, (list, tuple, set)):
        items = [str(x) for x in raw]
    else:
        items = str(raw if raw is not None else "").replace("，", ",").split(",")
    return {s.strip() for s in items if s.strip() in r.CATEGORIES}


def _whitelist_allows(chat_id: int, whitelist: Optional[Sequence[str]]) -> bool:
    if whitelist is None:
        return True
    variants = r.chat_variants(chat_id)
    for entry in whitelist:
        s = str(entry or "").strip()
        if not s:
            continue
        tail = s.rsplit(":", 1)[-1].partition("#")[0].strip()
        if r.chat_variants(tail) & variants:
            return True
    return False


class CareBridge:
    def __init__(
        self,
        store: Any,
        config_get: Callable[[str, Any], Any],
        *,
        pool: CandidatePool,
        owner_ids: Callable[[], Iterable[str]],
        observe_chat_ids: Callable[[], Iterable[int]],
        owner_channel_ids: Callable[[], Iterable[int]] = lambda: (),
        id_whitelist: Callable[[], Optional[Sequence[str]]] = lambda: None,
        initiative_mode: Callable[[], str] = lambda: MODE_OFF,
        llm_generate: Optional[Callable[[str, float], Awaitable[str]]] = None,
        budget: Any = None,
        usage_probe: Optional[Callable[[str], Awaitable[Optional[int]]]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.base = store
        self.cs = cs.CareStore(store, clock)
        self._get = config_get
        self.pool = pool
        self._owner_ids = owner_ids
        self._observe = observe_chat_ids
        self._channels = owner_channel_ids
        self._whitelist = id_whitelist
        self._initiative_mode = initiative_mode
        self._llm = llm_generate
        self._budget = budget
        self._usage_probe = usage_probe
        self._clock = clock
        self._task: Optional[asyncio.Task] = None
        self._fails = 0
        self._paused_until = 0.0
        self._last_retention = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ config

    def _cfg(self, key: str, default: Any) -> Any:
        try:
            val = self._get(key, default)
        except Exception:
            return default
        return default if val is None else val

    def _bool(self, key: str, default: bool) -> bool:
        v = self._cfg(key, default)
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    def mode(self) -> str:
        s = str(self._cfg(KEY_MODE, DEFAULT_MODE) or "").strip().lower()
        return s if s in (MODE_OFF, MODE_SHADOW, MODE_ON) else MODE_OFF

    def categories(self) -> set:
        return parse_categories(self._cfg(KEY_CATS, DEFAULT_CATS))

    def owners(self) -> set:
        try:
            return {str(o).strip() for o in (self._owner_ids() or ()) if str(o).strip()}
        except Exception:
            return set()

    def channels(self) -> set:
        try:
            return {int(c) for c in (self._channels() or ())}
        except Exception:
            return set()

    def authorized_groups(self) -> set:
        """lifelike_care_bridge_groups ∩ observe_chat_ids ∩ id_whitelist，去掉主人频道"""
        try:
            observe = {int(c) for c in (self._observe() or ())}
        except Exception:
            observe = set()
        wildcard, explicit = parse_groups(self._cfg(KEY_GROUPS, DEFAULT_GROUPS))
        groups = set(observe) if wildcard else (explicit & observe)
        if not self._bool(KEY_CHANNEL, False):
            groups -= self.channels()
        try:
            wl = self._whitelist()
        except Exception:
            wl = None
        return {gid for gid in groups if _whitelist_allows(gid, wl)}

    def is_off(self) -> bool:
        try:
            return bool(self.cs.pref().get("off"))
        except Exception:
            return False

    @property
    def paused(self) -> bool:
        return float(self._clock()) < self._paused_until

    def _failed(self, where: str, exc: BaseException) -> None:
        self._fails += 1
        logger.warning(f"{_LOG} {where} failed: {type(exc).__name__}")
        if self._fails >= FAIL_LIMIT:
            self._fails = 0
            self._paused_until = float(self._clock()) + PAUSE_S
            logger.warning(f"{_LOG} paused_error: {FAIL_LIMIT} consecutive failures, pause 1h")

    def _bump(self, name: str, n: int = 1) -> None:
        try:
            self.base.bump_counter(f"lifelike.care.{name}", n)
        except Exception:
            pass

    # ------------------------------------------------------------ task

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.get_running_loop().create_task(self._loop())
        logger.info(f"{_LOG} task started period={int(PERIOD_S)}s")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _loop(self) -> None:
        await asyncio.sleep(FIRST_DELAY_S)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self._failed("tick", e)
            await asyncio.sleep(PERIOD_S)

    async def tick(self) -> int:
        """一轮：增量扫描 + 过期 + 每日保留期。返回处理的帖子数。"""
        if self.mode() == MODE_OFF or self.paused:
            return 0
        async with self._lock:
            now = float(self._clock())
            n = await self.run_incremental(now)
            try:
                await asyncio.to_thread(self.cs.expire, now)
                if now - self._last_retention >= RETENTION_INTERVAL_S:
                    self._last_retention = now
                    cleared, deleted = await asyncio.to_thread(self.cs.retention, now, GIST_CLEAR_S, DELETE_S)
                    if cleared or deleted:
                        logger.info(f"{_LOG} retention cleared={cleared} deleted={deleted}")
            except Exception as e:
                self._failed("maintenance", e)
            return n

    # ------------------------------------------------------------ scanner

    def accept(self, post: Dict[str, Any], owner_ids: set, groups: set) -> bool:
        return r.accept_post(
            post, owner_ids, groups, self.channels(), include_owner_channel=self._bool(KEY_CHANNEL, False)
        )

    async def run_incremental(self, now: float) -> int:
        """增量处理；失败 → 游标不前进（下轮重试）。返回读到的帖子数。"""
        if self.mode() == MODE_OFF:
            return 0
        groups = self.authorized_groups()
        owners = self.owners()
        if not groups or not owners:
            return 0
        try:
            cursor = await asyncio.to_thread(self.cs.scan_cursor)
            if cursor is None:
                # 首次启用不回补更早的历史
                after, min_ts = 0, now - FIRST_RUN_LOOKBACK_S
            elif "rowid" in cursor:
                # rowid 游标：迟到的离线补采帖子也能扫到（只看近 LATE_LOOKBACK_S 内发的）
                after, min_ts = int(cursor["rowid"]), now - LATE_LOOKBACK_S
            else:
                after, min_ts = 0, float(cursor["ts"])  # 旧格式时间戳游标：迁移到 rowid
            posts = await asyncio.to_thread(self.cs.posts_after, sorted(groups), after, min_ts, POST_FETCH_LIMIT)
        except Exception as e:
            self._failed("read", e)
            return 0
        if not posts:
            if cursor is None or "rowid" not in cursor:
                try:
                    top = await asyncio.to_thread(self.cs.max_post_rowid)
                    await asyncio.to_thread(self.cs.set_scan_cursor, max(after, top))
                except Exception as e:
                    self._failed("cursor", e)
            return 0
        try:
            accepted = [p for p in posts if self.accept(p, owners, groups)]
            if accepted:
                await self._process_posts(accepted, now)
            newest = max(int(p.get("_rowid") or 0) for p in posts)
            await asyncio.to_thread(self.cs.set_scan_cursor, max(newest, after))
            self._fails = 0
        except Exception as e:
            self._failed("process", e)
            return 0
        logger.info(f"{_LOG} scan posts={len(posts)} owner_posts={len(accepted)}")
        return len(posts)

    async def _process_posts(self, posts: List[Dict[str, Any]], now: float) -> None:
        hyperbole = await asyncio.to_thread(self.cs.hyperbole, now)
        enabled = self.categories()
        signals: List[Dict[str, Any]] = []
        fuzzy: List[Tuple[Dict[str, Any], r.RuleResult, List[str]]] = []
        for p in posts:
            neighbors = [
                str(q.get("text") or "")
                for q in posts
                if q is not p
                and int(q["chat_id"]) == int(p["chat_id"])
                and abs(float(q["posted_at"]) - float(p["posted_at"])) <= NEIGHBOR_S
            ]
            res = r.classify_text(str(p.get("text") or ""), neighbors, hyperbole)
            if res.level == r.LEVEL_NONE or res.category not in enabled:
                continue
            if res.level == r.LEVEL_HIGH:
                signals.append(self._signal(p, res, r.CONF_SURE, "rule", r.rule_gist(res.category, res.keyword)))
            else:
                fuzzy.append((p, res, neighbors))
        if fuzzy:
            signals += await self._resolve_fuzzy(fuzzy, enabled, now)
        self._bump("detected", len(signals))
        for sig in signals:
            await asyncio.to_thread(self.process_signal, sig, now)

    @staticmethod
    def _signal(post: Dict[str, Any], res: r.RuleResult, conf: str, detector: str, gist: str,
                *, category: str = "", intensity: str = "", crisis: Optional[bool] = None) -> Dict[str, Any]:
        return {
            "chat_id": int(post["chat_id"]),
            "msg_ids": [int(post["msg_id"])],
            "first_at": float(post["posted_at"]),
            "last_at": float(post["posted_at"]),
            "sender_id": str(post.get("sender_id") or ""),
            "category": category or res.category,
            "intensity_band": intensity or res.intensity,
            "confidence_band": conf,
            "keyword": res.keyword,
            "gist": gist,
            "detector": detector,
            "crisis": res.crisis if crisis is None else bool(crisis),
            "reason": res.reason,
        }

    def _llm_allowed(self) -> bool:
        if self._llm is None or self._budget is None or not self._bool(KEY_LLM, True):
            return False
        try:
            cap = max(0, int(self._cfg(KEY_LLM_CAP, DEFAULT_LLM_CAP)))
        except (TypeError, ValueError):
            cap = DEFAULT_LLM_CAP
        try:
            used = int(self.base.get_counters().get(f"lifelike.budget.calls:{MODULE}", 0))
        except Exception:
            return False
        return used < cap

    async def _resolve_fuzzy(self, fuzzy, enabled: set, now: float) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        verdicts: Dict[int, Optional[Dict[str, Any]]] = {}
        for start in range(0, len(fuzzy), LLM_BATCH):
            batch = fuzzy[start:start + LLM_BATCH]
            if not await asyncio.to_thread(self._llm_allowed):
                break
            try:
                parsed = await self.classify_llm([[str(p.get("text") or "")] + nb for p, _res, nb in batch])
            except CheapBudgetExceeded:
                self._bump("llm_budget")
                break
            except Exception as e:  # noqa: BLE001 - 模型失败：只用高置信规则
                self._bump("llm_failed")
                logger.warning(f"{_LOG} llm failed: {type(e).__name__}")
                break
            self._bump("llm_calls")
            for i, v in enumerate(parsed):
                verdicts[start + i] = v
        aliases = await asyncio.to_thread(self._known_aliases) if verdicts else []
        for idx, (p, res, nb) in enumerate(fuzzy):
            if idx not in verdicts:
                # 模型不可用：含糊的危机句按 mood_low(明显) 正常关心（不用危机模板、不豁免上限）；其余丢弃
                if res.crisis_candidate:
                    out.append(self._signal(p, res, r.CONF_SURE, "rule", r.rule_gist("mood_low"),
                                            category="mood_low", intensity=r.BAND_STRONG, crisis=False))
                continue
            v = verdicts[idx]
            if not v or not v.get("about_self") or v.get("is_joke"):
                continue
            cat = str(v.get("category") or "")
            if cat not in r.CATEGORIES or cat not in enabled:
                continue
            crisis = bool(v.get("crisis")) and res.crisis_candidate
            sources = [str(p.get("text") or "")] + list(nb)
            gist = _normalize_gist(r.scrub_gist(str(v.get("gist") or ""), sources))
            if gist and (privacy.is_sensitive(gist) or r.gist_mentions_others(gist, aliases)):
                # 模型没守住"不出现人名/不写私事"：退回规则模板（审查 P8）
                self._bump("gist_rejected")
                gist = ""
            gist = gist or r.rule_gist(cat, res.keyword if cat == res.category else "")
            conf = r.CONF_TENTATIVE if res.reason == "hyperbole" else r.CONF_SURE
            band = v.get("intensity") if v.get("intensity") in (r.BAND_LIGHT, r.BAND_STRONG) else res.intensity
            if crisis:
                band = r.BAND_STRONG
            out.append(self._signal(p, res, conf, "llm", gist, category=cat, intensity=band, crisis=crisis))
        return out

    def _known_aliases(self) -> List[str]:
        """群友称呼表（P1-13 member_aliases，只用于概括校验，不外传）；不可用 → []"""
        try:
            with self.base._lock:
                rows = self.base._conn().execute(
                    "SELECT DISTINCT alias FROM member_aliases WHERE length(alias)>=2 LIMIT 2000"
                ).fetchall()
            return [str(r[0]) for r in rows if r and r[0]]
        except Exception:
            return []

    async def classify_llm(self, batch: List[List[str]]) -> List[Optional[Dict[str, Any]]]:
        """批量送廉价模型；输入只有主人自己的话（每段 = 目标消息 + 她紧邻的几句）"""
        lines = []
        for i, texts in enumerate(batch):
            main = " ".join(str(texts[0] or "").split())[:200]
            ctx = " / ".join(" ".join(str(t or "").split())[:80] for t in texts[1:4])
            lines.append(f"{i}. {main}" + (f"（她前后还说：{ctx}）" if ctx else ""))
        prompt = _LLM_PROMPT + "\n".join(lines)
        raw = await self._budget.guarded_generate(
            MODULE, prompt, self._llm, LLM_TIMEOUT_S, usage_probe=self._usage_probe, output_reserve=60 * len(batch)
        )
        return parse_llm_output(raw, len(batch))

    # ------------------------------------------------------------ builder

    def process_signal(self, sig: Dict[str, Any], now: float) -> Optional[str]:
        """合并 episode → 抑制 → delivery → 入队；返回 signal_id（未落库返回 None）"""
        cat = sig["category"]
        pref = self.cs.pref()
        if self.mode() == MODE_OFF:
            return None
        if pref["off"] or cat in pref["muted"] or cat not in self.categories():
            logger.info(f"{_LOG} chat={sig['chat_id']} cat={cat} op=suppress reason=off")
            self._bump("suppressed")
            return None
        merged = self.merge_episode(sig, now)
        if merged is not None:
            return merged
        crisis = bool(sig.get("crisis"))
        day = m.local_day(sig["first_at"])
        dedupe = f"care:{'crisis' if crisis else cat}:{day}"
        expires = self.expiry(sig)
        base = dict(
            chat_id=sig["chat_id"], msg_ids=sig["msg_ids"], first_at=sig["first_at"], last_at=sig["last_at"],
            category=cat, intensity_band=sig["intensity_band"], confidence_band=sig["confidence_band"],
            gist=sig["gist"], keyword=sig.get("keyword") or "", detector=sig["detector"], crisis=crisis,
            dedupe_key=dedupe, expires_at=expires,
        )
        tag = (
            f"chat={sig['chat_id']} cat={cat} det={sig['detector']} band={sig['confidence_band']}"
            f" crisis={int(crisis)}"
        )
        if crisis:
            logger.warning(f"{_LOG} crisis signal detected {tag} (owner should be informed; no text logged)")
        if self.mode() == MODE_SHADOW:
            sid = self.cs.insert(status=cs.S_DETECTED, **base)
            logger.info(f"{_LOG} sig={sid} {tag} op=detect mode=shadow")
            return sid
        if now >= expires:
            sid = self.cs.insert(status=cs.S_EXPIRED, **base)
            logger.info(f"{_LOG} sig={sid} {tag} op=suppress reason=expired")
            return sid
        owner = sig.get("sender_id") or ""
        scope = f"private:{owner}"
        reason = self.suppress_reason(sig, scope, now)
        if reason:
            sid = self.cs.insert(status=cs.S_SUPPRESSED, suppress_reason=reason, **base)
            self._bump("suppressed")
            logger.info(f"{_LOG} sig={sid} {tag} op=suppress reason={reason}")
            return sid
        delivery, why = self.delivery(sig, scope, now)
        sid = self.cs.insert(status=cs.S_DETECTED, delivery=delivery, **base)
        cand_id = self.enqueue(sid, sig, scope, delivery, expires)
        if cand_id is None:
            self.cs.update(sid, status=cs.S_SUPPRESSED, suppress_reason="dup")
            logger.info(f"{_LOG} sig={sid} {tag} op=suppress reason=dup")
            return sid
        self.cs.update(sid, status=cs.S_QUEUED, cand_id=cand_id)
        self._bump("enqueued_push" if delivery == DELIVERY_PUSH else "enqueued_passive")
        logger.info(f"{_LOG} sig={sid} {tag} op=enqueue delivery={delivery} why={why or '-'} cand={cand_id}")
        return sid

    def merge_episode(self, sig: Dict[str, Any], now: float) -> Optional[str]:
        """同类别 12h（sick 72h）内的多条合并为一个信号；返回被合并到的 signal_id"""
        cat = sig["category"]
        window = EPISODE_S.get(cat, EPISODE_DEFAULT_S)
        rows = self.cs.recent_by_category(cat, float(sig["first_at"]) - window)
        target = None
        for row in rows:
            if bool(row.get("crisis")) != bool(sig.get("crisis")) and sig.get("crisis"):
                continue  # 危机单列：不并入普通信号
            target = row
            break
        if target is None:
            day_key = f"care:{'crisis' if sig.get('crisis') else cat}:{m.local_day(sig['first_at'])}"
            target = self.cs.by_dedupe(day_key)
        if target is None:
            return None
        ids = sorted(set(target.get("msg_ids") or []) | set(sig["msg_ids"]))
        changes: Dict[str, Any] = {"msg_ids": ids, "last_at": max(float(target["last_at"]), float(sig["last_at"]))}
        if sig["intensity_band"] == r.BAND_STRONG and target.get("intensity_band") != r.BAND_STRONG:
            changes["intensity_band"] = r.BAND_STRONG
        if sig["confidence_band"] == r.CONF_SURE and target.get("confidence_band") != r.CONF_SURE:
            changes["confidence_band"] = r.CONF_SURE
        self.cs.update(target["signal_id"], **changes)
        logger.info(f"{_LOG} sig={target['signal_id']} cat={cat} op=merge msgs={len(ids)}")
        upgraded = "intensity_band" in changes or "confidence_band" in changes
        if (
            upgraded
            and target.get("status") == cs.S_QUEUED
            and target.get("delivery") == DELIVERY_PASSIVE
            and target.get("cand_id")
            and self.mode() == MODE_ON
        ):
            # 同一件事变得更明显（例如情绪从"轻"到"明显"）：顺口提升级为推送（仍受全部闸门约束）
            merged = dict(target, **changes)
            merged["msg_ids"] = ids
            merged["sender_id"] = sig.get("sender_id")
            delivery, _why = self.delivery(merged, f"private:{sig.get('sender_id') or ''}", now)
            if delivery == DELIVERY_PUSH and self.pool.set_delivery(str(target["cand_id"]), DELIVERY_PUSH):
                self.cs.update(target["signal_id"], delivery=DELIVERY_PUSH)
                logger.info(f"{_LOG} sig={target['signal_id']} cat={cat} op=upgrade delivery=push")
        return str(target["signal_id"])

    def expiry(self, sig: Dict[str, Any]) -> float:
        cat, band = sig["category"], sig["intensity_band"]
        if sig.get("confidence_band") != r.CONF_SURE:
            ttl = EXPIRY_DEFAULT_S
        else:
            ttl = EXPIRY_S.get((cat, band), EXPIRY_DEFAULT_S)
        exp = float(sig["last_at"]) + ttl
        if cat in ("tired", "overtime") and not sig.get("crisis"):
            # 静音时段前不推就直接过期：第二天早上再问"昨晚加班到几点"像在盯着她
            from ...proactive.activity_gate import parse_hhmm_range

            rng = parse_hhmm_range(str(self._cfg("lifelike_activity_prior_inactive", "01:00-08:00") or ""))
            nxt = next_range_start(float(sig["last_at"]), rng)
            if nxt is not None and nxt > float(sig["last_at"]):
                exp = min(exp, nxt)
        return exp

    def suppress_reason(self, sig: Dict[str, Any], scope: str, now: float) -> str:
        """private_known | replied_in_group | cooldown | ''（off 已在入口处理）"""
        cat = sig["category"]
        if not sig.get("crisis") and self._private_known(scope, cat, float(sig["first_at"])):
            return "private_known"
        if not sig.get("crisis"):
            try:
                if self._replied_in_group(sig["chat_id"], sig["msg_ids"]):
                    return "replied_in_group"  # 她已经在群里当场回应过：私聊不再重复关心
            except Exception:
                return "check_failed"
        if not sig.get("crisis"):
            for row in self.cs.recent_by_category(cat, now - CATEGORY_COOLDOWN_S):
                if row.get("status") in (cs.S_USED, cs.S_OFFERED) and float(row["updated_at"]) >= now - CATEGORY_COOLDOWN_S:
                    return "cooldown"
        return ""

    def _private_known(self, scope: str, cat: str, first_at: float) -> bool:
        try:
            rows = self.base.inbound_after(scope, 0, first_at - PRIVATE_KNOWN_LOOKBACK_S, 200)
        except Exception:
            return False
        for row in rows:
            res = r.classify_text(str(row.get("text") or ""))
            if res.level != r.LEVEL_NONE and res.category == cat:
                return True
        return False

    def _replied_in_group(self, chat_id: int, msg_ids: Sequence[int]) -> bool:
        ids = [int(x) for x in msg_ids]
        if not ids:
            return False
        with self.base._lock:
            conn = self.base._conn()
            marks = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT posted_at FROM observed_posts WHERE chat_id=? AND msg_id IN ({marks})", [int(chat_id), *ids]
            ).fetchall()
            for row in rows:
                t = float(row[0])
                hit = conn.execute(
                    "SELECT 1 FROM observed_posts WHERE chat_id=? AND author_kind='self' AND posted_at>? AND posted_at<=?"
                    " LIMIT 1",
                    (int(chat_id), t, t + REPLIED_IN_GROUP_S),
                ).fetchone()
                if hit:
                    return True
        return False

    def delivery(self, sig: Dict[str, Any], scope: str, now: float) -> Tuple[str, str]:
        """推送还是顺口提（设计 §7.9.4 表，关系阶段按"聊得来的网友"一列）；返回 (delivery, 原因)"""
        cat, band, conf = sig["category"], sig["intensity_band"], sig["confidence_band"]
        push_ok = self._bool(KEY_PUSH, True) and self._initiative_mode() == MODE_ON
        if not push_ok:
            return DELIVERY_PASSIVE, "push_disabled"
        if sig.get("crisis"):
            return DELIVERY_PUSH, "crisis"
        if conf != r.CONF_SURE:
            return DELIVERY_PASSIVE, "tentative"
        pushable = cat in ("sick", "good_news") or (cat == "mood_low" and band == r.BAND_STRONG)
        if not pushable:
            return DELIVERY_PASSIVE, "low_intensity"
        try:
            last = self.base.last_interaction_ts(scope)
        except Exception:
            last = None
        if last is not None and now - float(last) < OWNER_ACTIVE_S:
            return DELIVERY_PASSIVE, "owner_active"
        return DELIVERY_PUSH, ""

    def enqueue(self, sid: str, sig: Dict[str, Any], scope: str, delivery: str, expires: float) -> Optional[str]:
        refs = [f"post:{sig['chat_id']}:{mid}" for mid in sig["msg_ids"]]
        refs += [f"signal:{sid}", f"cat:{sig['category']}", f"band:{sig['intensity_band']}"]
        if sig.get("crisis"):
            refs.append("flag:crisis")
        gist = str(sig.get("gist") or "")
        cand = InitiativeCandidate(
            scope_key=scope,
            ctype=CTYPE_GROUP_CARE,
            dedupe_key=f"care:{sid}",
            why_now=f"她在群里{gist}（群里她自己说的）",
            material=gist,
            expires_at=expires,
            source_ref=refs,
            privacy=PRIVACY_OWNER_ONLY,
            delivery=delivery,
        )
        return self.pool.enqueue(cand)

    # ------------------------------------------------------------ callbacks from the coordinator

    def _signal_of(self, cand_id: str) -> Optional[Dict[str, Any]]:
        cid = str(cand_id or "")
        if cid.startswith("cand:"):
            cid = cid[5:]
        return self.cs.by_cand(cid) if cid else None

    def on_candidate_used(self, cand_id: str, ts: float, passive: bool = False) -> None:
        sig = self._signal_of(cand_id)
        if sig is not None and sig["status"] in cs.OPEN:
            self.cs.update(sig["signal_id"], status=cs.S_USED)
            self._bump("passive_used" if passive else "pushed")
            logger.info(f"{_LOG} sig={sig['signal_id']} cat={sig['category']} op={'passive' if passive else 'push'}")

    def on_candidate_blocked(self, cand_id: str) -> None:
        sig = self._signal_of(cand_id)
        if sig is not None and sig["status"] in cs.OPEN:
            self.cs.update(sig["signal_id"], status=cs.S_SUPPRESSED, suppress_reason="blocked_style")
            self._bump("blocked_style")
            logger.info(f"{_LOG} sig={sig['signal_id']} cat={sig['category']} op=suppress reason=blocked_style")

    def on_candidates_expired(self, cand_ids: Sequence[str]) -> None:
        for cid in cand_ids:
            sig = self._signal_of(cid)
            if sig is not None and sig["status"] in cs.OPEN:
                self.cs.update(sig["signal_id"], status=cs.S_EXPIRED)

    def on_misread(self, cand_id: str) -> None:
        """主人回"开玩笑的/没事" → declined(misread)；同一说法误判 ≥2 次进口头夸张表。不产生任何关系事件。"""
        sig = self._signal_of(cand_id)
        if sig is None:
            return
        self.cs.update(sig["signal_id"], status=cs.S_DECLINED, suppress_reason="misread")
        n = self.cs.note_misread(sig["category"], sig.get("keyword") or "")
        self._bump("misread")
        logger.info(f"{_LOG} sig={sig['signal_id']} cat={sig['category']} op=declined reason=misread n={n}")

    def set_off(self, scope: str, off: bool, why: str = "cmd") -> int:
        self.cs.set_pref(off=off)
        n = 0
        if off:
            ids = self.pool.suppress_open(scope, CTYPE_GROUP_CARE)
            for cid in ids:
                sig = self.cs.by_cand(cid)
                if sig is not None and sig["status"] in cs.OPEN:
                    self.cs.update(sig["signal_id"], status=cs.S_SUPPRESSED, suppress_reason="off")
            for sig in self.cs.open_signals():
                self.cs.update(sig["signal_id"], status=cs.S_SUPPRESSED, suppress_reason="off")
            n = len(ids)
        logger.info(f"{_LOG} op={'off' if off else 'on'} by={why} suppressed={n}")
        return n

    def mute(self, scope: str, category: str, muted: bool) -> None:
        if not muted:
            self.cs.set_pref(unmute=[category])
            return
        self.cs.set_pref(mute=[category])
        now = float(self._clock())
        for c in self.pool.open(scope, now, ctype=CTYPE_GROUP_CARE):
            sig = self.cs.by_cand(c["cand_id"])
            if sig is None or sig["category"] != category:
                continue
            self.pool.set_status(c["cand_id"], "suppressed", only_from=("queued", "offered"))
            if sig["status"] in cs.OPEN:
                self.cs.update(sig["signal_id"], status=cs.S_SUPPRESSED, suppress_reason="off")

    def source_texts(self, cand: Dict[str, Any]) -> List[str]:
        """候选来源帖（主人自己的话）原文，只用于发送前的"不复述原句"检查，不进 prompt"""
        out: List[str] = []
        for ref in cand.get("source_ref") or []:
            s = str(ref)
            if not s.startswith("post:"):
                continue
            try:
                _, chat, mid = s.split(":", 2)
                with self.base._lock:
                    row = self.base._conn().execute(
                        "SELECT text FROM observed_posts WHERE chat_id=? AND msg_id=? AND author_kind='owner'",
                        (int(chat), int(mid)),
                    ).fetchone()
            except Exception:
                continue
            if row is not None:
                out.append(str(row[0] or ""))
        return out

    # ------------------------------------------------------------ text for the prompt (no numbers)

    @staticmethod
    def _cat_of(cand: Dict[str, Any]) -> Tuple[str, str, bool]:
        cat = band = ""
        crisis = False
        for ref in cand.get("source_ref") or []:
            s = str(ref)
            if s.startswith("cat:"):
                cat = s[4:]
            elif s.startswith("band:"):
                band = s[5:]
            elif s == "flag:crisis":
                crisis = True
        return cat, band, crisis

    def focus_text(self, cand: Dict[str, Any], now: Optional[float] = None) -> str:
        cat, band, crisis = self._cat_of(cand)
        gist = str(cand.get("material") or "").strip() or "提到了自己的近况"
        now = float(self._clock()) if now is None else float(now)
        when = when_phrase(float(cand.get("created_at") or now), now)
        example = "刚在群里看到你说……" if when == "刚才" else f"{when}在群里看到你说……"
        if crisis:
            return (
                f"【这次想说的事】你{when}在群里看到她说的话让你有点担心（{gist}）。私下认真、温和地问她还好吗，"
                "告诉她你在；必要时建议她联系身边信任的人或专业帮助。要说明是在群里看到的；"
                "不说教、不追问细节、不提群里其他人。"
            )
        if cat in ("sick", "mood_low"):
            tone = "语气平淡温和，不卖萌。"
        elif cat == "good_news":
            tone = "可以真心替她高兴，卖萌很少。"
        else:
            tone = "卖萌很少。"
        return (
            f"【这次想说的事】你{when}在群里看到她{gist}。私下关心一句，自然地说明是{when}在群里看到的"
            f"（比如“{example}”），时间别说错；一两句就好，不追问细节，不说教；她不想聊就算了。"
            "不要照抄她的原话，不提群里其他人的反应，不要说你一直在看群消息。" + tone
        )

    def passive_line(self, cand: Dict[str, Any], now: float) -> str:
        gist = str(cand.get("material") or "").strip() or "提到了自己的近况"
        when = when_phrase(float(cand.get("created_at") or now), now)
        return f"（可选）她{when}在群里{gist}。合适就顺口关心一句，说清是在群里看到的；不合适就不提。"

    # ------------------------------------------------------------ owner-facing status (command only)

    def status_text(self, scope: str, now: float) -> str:
        pref = self.cs.pref()
        counts = self.cs.counts_by_status()
        sent = self.pool.sent_since(scope, now - 7 * 86400.0)
        care = [d for d in sent if d.get("chosen_ctype") == CTYPE_GROUP_CARE]
        today = sum(1 for d in care if m.local_day(float(d["sent_at"])) == m.local_day(now))
        hyper = self.cs.hyperbole(now)
        lines = [
            f"关心桥：模式 {self.mode()}，{'已关闭（主人设置）' if pref['off'] else '开启'}",
            f"推送：{'开' if self._bool(KEY_PUSH, True) else '关（只顺口提）'}；主动候选池：{self._initiative_mode()}",
            f"授权群：{len(self.authorized_groups())} 个；已静音类别：{','.join(pref['muted']) or '无'}",
            f"今日关心 {today} 次，近 7 天 {len(care)} 次",
            "信号状态：" + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "无"),
        ]
        if hyper:
            lines.append("口头夸张表：" + "，".join(f"{c}/{k}" for c, k in hyper))
        return "\n".join(lines)

    def why_text(self, n: int = 5) -> str:
        rows = self.cs.recent(n)
        if not rows:
            return "最近没有关心桥信号。"
        out = []
        for row in rows:
            when = _local(float(row["last_at"])).strftime("%m-%d %H:%M")
            reason = f"({row['suppress_reason']})" if row.get("suppress_reason") else ""
            out.append(
                f"- {when} 群{row['chat_id']} 消息{','.join(str(x) for x in row['msg_ids'][:5])}"
                f" 类别={row['category']} 强度={row['intensity_band']} 置信={row['confidence_band']}"
                f" 检测={row['detector']} 状态={row['status']}{reason}"
                f" 送达={row.get('delivery') or '-'}"
            )
        return "\n".join(out)


def when_phrase(created: float, now: float) -> str:
    """群里那句话离现在多久 → 文字时间（刚才 / 今天早些时候 / 昨晚 / 昨天 / 前两天）"""
    if now - created < 3 * 3600:
        return "刚才"
    try:
        days = (_local(now).date() - _local(created).date()).days
    except Exception:
        return "前两天"
    if days <= 0:
        return "今天早些时候"
    if days == 1:
        return "昨晚" if _local(created).hour >= 18 else "昨天"
    return "前两天"


def _normalize_gist(gist: str) -> str:
    g = str(gist or "").strip().rstrip("。.!！")
    if g.startswith("她"):
        g = g[1:]
    if not g:
        return ""
    if not g.startswith(("说", "好像", "提到")):
        g = "说" + g
    return g if len(g) <= r.GIST_MAX else ""


def parse_llm_output(raw: Any, n: int) -> List[Optional[Dict[str, Any]]]:
    """严格解析模型 JSON 数组；无法解析的项为 None（宁漏勿误）"""
    out: List[Optional[Dict[str, Any]]] = [None] * n
    text = str(raw or "").strip()
    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        return out
    try:
        data = json.loads(match.group(0))
    except (TypeError, ValueError):
        return out
    if not isinstance(data, list):
        return out
    for pos, obj in enumerate(data):
        if not isinstance(obj, dict):
            continue
        try:
            i = int(obj.get("i", pos))
        except (TypeError, ValueError):
            continue
        if 0 <= i < n:
            out[i] = {
                "about_self": obj.get("about_self") is True,
                "category": str(obj.get("category") or "none"),
                "intensity": str(obj.get("intensity") or ""),
                "is_joke": obj.get("is_joke") is True,
                "crisis": obj.get("crisis") is True,
                "gist": str(obj.get("gist") or ""),
            }
    return out
