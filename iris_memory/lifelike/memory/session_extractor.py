"""会话批量抽取 SessionExtractor（ROADMAP P0-4，设计 §6.3 的 items 部分）。

现网逐条抽取（``continuity/extractor.py``）的预筛只放行带时间/计划词的消息，
其余全部 ``skipped``，事项几乎进不来。本模块在逐条快路径之外，按“会话段”对主人
私聊做一次廉价模型抽取：

1. **触发**（三者先到）：收尾成立（closure → ended）；该段未抽取消息 ≥12 条；
   最后一条消息后 30 分钟无新消息且有未抽取消息。
2. **守门**（规则）：本段主人消息去掉应答/表情后总字数 <15 → 跳过，不调模型。
3. **一次廉价模型调用**（``background_provider_id()``，module=``lifelike_session_extract``，
   计入 ``lifelike_budget_cheap_*`` 硬预算）；输入只含主人（inbound 按 sender 硬过滤）
   与 角色 已发送的回复（outbound author=bot status=sent）；P0 只输出 ``items``，
   ``beliefs/moments/rel_signals`` 字段预留（P1-7/P2-3 启用）。
4. **来源过滤**：op 的 evidence 只能在主人消息里定位（复用 ``validate_op``，候选行只给
   主人消息），角色 自己的话永远不会成为主人的事项；仅复述 角色 原话的短消息不新建。
5. 逐条抽取原样保留为“带明确时间词”的快路径；本段中快路径已处理过（done）的消息
   不能再被 create（防重复），可以 update/complete。

状态与防重复：每个 scope 一条游标（``cursors`` 表，键 ``lifelike.session_extract:<scope>``，
JSON：last_msg_id / session_version / attempts / last_error）。只处理 msg_id >
last_msg_id 的入站行；提交时在同一事务里比较游标（CAS），游标已被推进则丢弃本次结果；
单 worker 串行。会话版本取 ``watermarks.session_version`` 记入游标与日志。
首次启用时游标初始化为窗口内已处理过的最大 msg_id —— 不回补启用前的旧消息
（ROADMAP ⑥ 的回补需单独批准，未实现）。

模式（HiddenConfig ``lifelike_session_extract_mode``）：
- ``off``：不读写任何状态、不调模型（与改造前一致）；
- ``shadow``：调模型、校验，只写 ``counters`` 与日志，不写 items、不改入站状态；
- ``on``：写 items / item_history，并把本段被覆盖的 ``skipped`` 主人消息标为 ``done``。

失败：超时/网络/坏 JSON → 游标不动，退避重试（最多 3 次），仍失败则跳过本段（行保持
旧状态 = 旧行为）；连续 5 次失败熔断 1 小时。预算耗尽 → 不调模型、游标不动（当天
预算恢复前维持旧行为）。任何异常都不影响聊天与逐条快路径。

日志前缀 ``[lifelike.session_extract]``，只记 id / 计数 / 状态，不记正文。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

from ...continuity import models as m
from ...continuity import rules
from ...continuity.extractor import (
    ExtractResult,
    ItemExtractor,
    _Candidate,
    _InvalidOp,
    _Op,
    _owner_from_scope,
    _fmt_ts,
    validate_op,
)
from ...continuity.store import ItemNotFound, RevisionConflict
from ..budget import CheapBudget, CheapBudgetExceeded

__all__ = [
    "SessionExtractor",
    "SessionReport",
    "MODE_OFF",
    "MODE_SHADOW",
    "MODE_ON",
    "MODULE",
    "KEY_MODE",
    "build_session_prompt",
    "parse_session_json",
    "parse_session_beliefs",
    "normalize_mode",
]

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ON = "on"
MODES = (MODE_OFF, MODE_SHADOW, MODE_ON)
KEY_MODE = "lifelike_session_extract_mode"
MODULE = "lifelike_session_extract"
CURSOR_PREFIX = "lifelike.session_extract:"
COUNTER_PREFIX = "lifelike.session_extract."

TRIGGER_COUNT = 12  # 未抽取主人消息 ≥12 条
IDLE_S = 30 * 60.0  # 30 分钟无新消息
SETTLE_S = 15.0  # 收尾/条数触发后稍等，让合批与逐条快路径先落定
BUSY_RECHECK_S = 20.0  # 段内仍有快路径 pending 行时的复查间隔
BUSY_PENDING_MAX_AGE_S = 600.0  # 超过此时长仍 pending 的行不再阻塞会话抽取
WINDOW_S = 86400.0  # 只看 24 小时内的入站（与快路径 stale 阈值一致）
MAX_ROWS = 40  # 单次调用最多覆盖的入站行
MIN_OWNER_CHARS = 15
OWNER_LINE_MAX = 300
BOT_LINE_MAX = 160
BOT_CONTEXT_MAX = 20
BOT_MARGIN_S = 600.0
MAX_ITEM_OPS = 6
MAX_ATTEMPTS = 3
RETRY_BACKOFF_S = 120.0  # ×2^(attempts-1)
BREAKER_FAILS = 5
BREAKER_PAUSE_S = 3600.0
MODULE_DAILY_CALLS = 12  # 本模块每日调用内部上限（全局硬预算之外再兜一层；= ROADMAP P0-4 验收口径，审查 L2）
DEFAULT_TIMEOUT_S = 45.0
OUTPUT_RESERVE = 600

_IDLE_TRIGGERS = frozenset({"idle", "recover"})
_RETRYABLE = (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)

LLMGenerate = Callable[[str, float], Awaitable[str]]


def normalize_mode(value: Any) -> str:
    v = str(value or "").strip().lower()
    return v if v in MODES else MODE_OFF


def _content_chars(text: str) -> int:
    """去掉表情/标点/空白后的字数（中文、字母、数字）"""
    return sum(1 for ch in text if ch.isalnum())


# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------


@dataclass
class SessionReport:
    scope_key: str
    trigger: str = ""
    mode: str = MODE_OFF
    # off | paused | no_owner | empty | busy | gated | budget | retry | failed | conflict | shadow | done
    status: str = "empty"
    rows: int = 0
    owner_rows: int = 0
    upto: int = 0
    session_version: int = 0
    more: bool = False  # 本段超过 MAX_ROWS，剩余行待下一次
    llm_called: bool = False
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    deduped: int = 0
    invalid: int = 0
    proposed: dict[str, int] = field(default_factory=dict)  # 合法 op 分布（shadow 用）
    marked_done: int = 0
    error: str = ""
    retry_in: float = 0.0


# ---------------------------------------------------------------------------
# Prompt / 解析
# ---------------------------------------------------------------------------

_PROMPT_HEAD = """你是对话事项记录器。下面是主人与 角色 最近的一段私聊（按时间排序）。
只根据【主人】说的话，找出之后值得 角色 记住、到时候接着关心的“待续事项”，新建或更新。
事项种类：plan（主人打算做/要去做的事）、pending_result（主人在等结果的事）、
unfinished_topic（没聊完、之后值得接着聊的话题）、note（主人要求记下的信息）。
操作：create 新建；update 修改已有事项的话题/摘要/种类/提醒；reschedule 改时间；
complete 已完成；cancel 取消/不做了；decline 主人明确不想再聊该事项。
规则：
- 【角色】的话只用来理解上下文。角色 自己的经历、说法、建议、承诺都不是主人的事项，evidence 不能取自 角色 的话。
- 闲聊、情绪表达、纯应答、已经说完且没有后续的话题不建事项；没有就返回 {"items":[]}。
- 同一件事只建一条；候选里已有的事项用 update/complete 等操作，不要重复 create。
  标“已记录”的主人消息已经处理过，不要据此重复新建。
- item_id 只能取自下方候选列表；create 不带 item_id。
- evidence 必须是【主人】某条消息原文中连续出现的片段（至少 2 个字），不要改写。
- when_text 必须是原文中的时间表达原样片段，没有就留空字符串；不要自己换算日期。
- remind_requested 只有主人明确要求提醒时才为 true。
- topic 不超过 40 字，summary 不超过 160 字，用第三人称“她”指主人。最多 6 条。
- beliefs、moments、rel_signals 保持空数组。
只输出 JSON，不要任何解释：
{"items":[{"op":"create|update|complete|cancel|decline|reschedule","item_id":"","kind":"plan|pending_result|unfinished_topic|note","topic":"","summary":"","when_text":"","remind_requested":false,"evidence":""}],"beliefs":[],"moments":[],"rel_signals":[]}
"""


_BELIEFS_EMPTY_LINE = "- beliefs、moments、rel_signals 保持空数组。\n"
_BELIEFS_JSON_EMPTY = '"beliefs":[]'


def _prompt_head(with_beliefs: bool) -> str:
    """lifelike_memory_mode=off 时与 P0-4 原 prompt 逐字一致；否则同一次调用顺带输出 beliefs（P1-7）。"""
    if not with_beliefs:
        return _PROMPT_HEAD
    from .beliefs import BELIEF_PROMPT_RULES, BELIEF_PROMPT_SCHEMA

    head = _PROMPT_HEAD.replace(
        _BELIEFS_EMPTY_LINE, "- moments、rel_signals 保持空数组。\n" + BELIEF_PROMPT_RULES + "\n"
    )
    return head.replace(_BELIEFS_JSON_EMPTY, BELIEF_PROMPT_SCHEMA)


def build_session_prompt(
    owner_rows: Sequence[dict[str, Any]],
    bot_rows: Sequence[dict[str, Any]],
    candidates: dict[str, _Candidate],
    belief_candidates: Sequence[dict[str, Any]] | None = None,
) -> str:
    lines = [_prompt_head(belief_candidates is not None)]
    if belief_candidates is not None:
        from .beliefs import candidates_block

        lines.append(candidates_block(belief_candidates))
    lines.append("<candidates>")
    if not candidates:
        lines.append("（无）")
    for cid, c in candidates.items():
        it = c.item
        when = it.get("time_basis") or ""
        if it.get("event_at_start") is not None:
            when = f"{when}（{_fmt_ts(it['event_at_start'])} 起）"
        lines.append(
            json.dumps(
                {"item_id": cid, "kind": it["kind"], "topic": it["topic"], "summary": it["summary"],
                 "when": when, "tags": c.tags},
                ensure_ascii=False,
            )
        )
    lines.append("</candidates>")
    merged: list[tuple[float, int, str]] = []
    for r in owner_rows:
        label = "主人"
        if r.get("extract_status") == m.INBOUND_DONE:
            label += "（已记录）"
        if r.get("_linked"):
            label += "（回复 角色 关于候选事项的消息）"
        merged.append((float(r["msg_ts"]), 1, f"[{_fmt_ts(r['msg_ts'])}] 【{label}】{r['_text']}"))
    for b in bot_rows:
        ts = float(b.get("sent_at") or b.get("created_at") or 0)
        text = m.clip(str(b.get("text") or ""), BOT_LINE_MAX)
        merged.append((ts, 0, f"[{_fmt_ts(ts)}] 【角色】{text}"))
    merged.sort(key=lambda t: (t[0], t[1]))
    lines.append("<dialogue>")
    lines.extend(t[2] for t in merged)
    lines.append("</dialogue>")
    return "\n".join(lines)


def parse_session_json(raw: Any) -> list[Any] | None:
    """{"items":[...]}（兼容 {"ops":[...]}）→ op 列表；格式不对 → None"""
    if not isinstance(raw, str):
        return None
    text = _FENCE_RE.sub("", raw.strip())
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("items", "ops"):
        if isinstance(data.get(key), list):
            return data[key]
    return None


def parse_session_beliefs(raw: Any) -> list[Any] | None:
    """同一份输出里的 beliefs 数组；没有或格式不对 → None"""
    if not isinstance(raw, str):
        return None
    text = _FENCE_RE.sub("", raw.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("beliefs"), list):
        return None
    return data["beliefs"]


# ---------------------------------------------------------------------------
# 抽取器 + 调度
# ---------------------------------------------------------------------------


class SessionExtractor:
    """单 worker：按 scope 调度会话段抽取。store 调用经 asyncio.to_thread。"""

    def __init__(
        self,
        store: Any,
        items: ItemExtractor,
        llm_generate: LLMGenerate,
        budget: CheapBudget,
        *,
        mode_get: Callable[[], Any],
        clock: Callable[[], float] = time.time,
        owner_resolver: Callable[[str], str | None] | None = None,
        usage_probe: Callable[[str], Awaitable[int | None]] | None = None,
        timeout_get: Callable[[], float] | None = None,
        beliefs: Any = None,
        memory_mode_get: Callable[[], Any] | None = None,
        memory_shadow_extract_get: Callable[[], Any] | None = None,
    ) -> None:
        self._store = store
        # P1-7：用户认知（lifelike_memory_mode）。None / off → 与 P0-4 完全一致
        self._beliefs = beliefs
        self._memory_mode_get = memory_mode_get
        # shadow 默认不改现网 P0-4 抽取 prompt；lifelike_memory_shadow_extract=true 才在 shadow 下并入 beliefs
        self._memory_shadow_extract_get = memory_shadow_extract_get
        self._items = items
        self._llm = llm_generate
        self._budget = budget
        self._mode_get = mode_get
        self._clock = clock
        self._owner_resolver = owner_resolver or _owner_from_scope
        self._usage_probe = usage_probe
        self._timeout_get = timeout_get
        self._due: dict[str, tuple[float, str]] = {}  # scope → (loop 到期时间, trigger)
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._busy = False
        self._fails = 0
        self._paused_until = 0.0

    # ------------------------------------------------------------ mode / state
    def mode(self) -> str:
        try:
            return normalize_mode(self._mode_get())
        except Exception:  # noqa: BLE001 - 读配置失败 → off（旧行为）
            return MODE_OFF

    def memory_mode(self) -> str:
        if self._beliefs is None or self._memory_mode_get is None:
            return MODE_OFF
        try:
            return normalize_mode(self._memory_mode_get())
        except Exception:  # noqa: BLE001
            return MODE_OFF

    def beliefs_in_prompt(self) -> bool:
        """本轮抽取是否并入 beliefs：on 并入；shadow 只在显式打开 shadow_extract 时并入（审查 #3）"""
        mode = self.memory_mode()
        if mode == MODE_ON:
            return True
        if mode != MODE_SHADOW or self._memory_shadow_extract_get is None:
            return False
        try:
            raw = self._memory_shadow_extract_get()
        except Exception:  # noqa: BLE001
            return False
        if isinstance(raw, str):
            return raw.strip().lower() in ("1", "true", "yes", "on")
        return bool(raw)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def is_idle(self) -> bool:
        return not self._due and not self._busy

    @staticmethod
    def _cursor_name(scope_key: str) -> str:
        return CURSOR_PREFIX + scope_key

    def _load_state(self, scope_key: str) -> dict[str, Any] | None:
        raw = self._store.get_cursor(self._cursor_name(scope_key))
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            data = None
        if not isinstance(data, dict):
            return None
        return {
            "last_msg_id": int(data.get("last_msg_id") or 0),
            "session_version": int(data.get("session_version") or 0),
            "attempts": int(data.get("attempts") or 0),
            "last_error": str(data.get("last_error") or ""),
        }

    def _save_state(self, scope_key: str, **state: Any) -> None:
        payload = {
            "last_msg_id": int(state.get("last_msg_id") or 0),
            "session_version": int(state.get("session_version") or 0),
            "attempts": int(state.get("attempts") or 0),
            "last_error": str(state.get("last_error") or "")[:80],
            "updated_at": float(self._clock()),
        }
        self._store.set_cursor(self._cursor_name(scope_key), json.dumps(payload, sort_keys=True))

    def _ensure_state(self, scope_key: str) -> dict[str, Any]:
        """首次启用：游标 = 窗口内已处理（非 pending）的最大 msg_id，不回补启用前的消息"""
        state = self._load_state(scope_key)
        if state is not None:
            return state
        now = float(self._clock())
        rows = self._store.inbound_after(scope_key, 0, now - WINDOW_S, 10000)
        done_ids = [int(r["msg_id"]) for r in rows if r.get("extract_status") != m.INBOUND_PENDING]
        wm = self._store.get_watermark(scope_key) or {}
        state = {
            "last_msg_id": max(done_ids) if done_ids else 0,
            "session_version": int(wm.get("session_version") or 0),
            "attempts": 0,
            "last_error": "",
        }
        self._save_state(scope_key, **state)
        logger.info(
            f"[lifelike.session_extract] init scope={scope_key} cursor={state['last_msg_id']}"
            f" v={state['session_version']}"
        )
        return state

    def _bump(self, name: str, n: int = 1) -> None:
        if n:
            try:
                self._store.bump_counter(COUNTER_PREFIX + name, int(n))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[lifelike.session_extract] counter failed: {type(e).__name__}")

    def _unextracted(self, scope_key: str) -> list[dict[str, Any]]:
        state = self._ensure_state(scope_key)
        now = float(self._clock())
        return self._store.inbound_after(scope_key, state["last_msg_id"], now - WINDOW_S, 10000)

    # ------------------------------------------------------------ triggers (event loop)
    async def note_inbound(self, scope_key: str) -> None:
        """主人私聊新入站行已记录：≥12 条未抽取 → 尽快；否则 30 分钟空闲后。"""
        try:
            if self.mode() == MODE_OFF:
                return
            rows = await asyncio.to_thread(self._unextracted, scope_key)
            await self._ensure_running()
            owner = self._owner_resolver(scope_key)
            n = sum(1 for r in rows if str(r.get("sender_id")) == str(owner))
            if n >= TRIGGER_COUNT:
                self.schedule(scope_key, SETTLE_S, "count")
            else:
                self.schedule(scope_key, IDLE_S, "idle", reset=True)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[lifelike.session_extract] note_inbound failed scope={scope_key}: {type(e).__name__}")

    async def on_closure(self, scope_key: str) -> None:
        """收尾成立（closures.state → ended）"""
        try:
            if self.mode() == MODE_OFF:
                return
            await self._ensure_running()
            self.schedule(scope_key, SETTLE_S, "closure")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[lifelike.session_extract] on_closure failed scope={scope_key}: {type(e).__name__}")

    def schedule(self, scope_key: str, delay: float, trigger: str, *, reset: bool = False) -> None:
        """登记到期。reset=True（空闲计时）：推迟到 now+delay，除非已有更早的非空闲触发。"""
        try:
            loop_now = asyncio.get_running_loop().time()
        except RuntimeError:
            return
        due = loop_now + max(0.0, float(delay))
        cur = self._due.get(scope_key)
        if cur is None or cur[1] in _IDLE_TRIGGERS:
            # 空闲计时可被任何新信号替换（空闲计时本身随新消息顺延）
            self._due[scope_key] = (due, trigger)
        elif not reset and due < cur[0]:
            # 已有明确触发（收尾/条数/重试）：只会提前，不会被推迟
            self._due[scope_key] = (due, trigger)
        if self._wake is not None:
            self._wake.set()

    # ------------------------------------------------------------ lifecycle
    async def _ensure_running(self) -> None:
        if not self.running:
            self._wake = asyncio.Event()
            self._task = asyncio.create_task(self._run_loop(), name="iris-lifelike-session-extract")

    async def start(self, scopes: Sequence[str] = ()) -> None:
        """启动 worker 并恢复：有未抽取消息的 scope 按“最后消息 + 30 分钟”重新计时"""
        if self.mode() == MODE_OFF:
            return
        await self._ensure_running()
        now = float(self._clock())
        recovered = 0
        for sk in scopes:
            try:
                rows = await asyncio.to_thread(self._unextracted, sk)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[lifelike.session_extract] recovery failed scope={sk}: {type(e).__name__}")
                continue
            if not rows:
                continue
            last_ts = max(float(r["msg_ts"]) for r in rows)
            self.schedule(sk, max(SETTLE_S, last_ts + IDLE_S - now), "recover")
            recovered += 1
        if recovered:
            logger.info(f"[lifelike.session_extract] recovery scopes={recovered}")

    async def stop(self) -> None:
        task, self._task = self._task, None
        self._due.clear()
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _run_loop(self) -> None:
        assert self._wake is not None
        loop = asyncio.get_running_loop()
        while True:
            if not self._due:
                self._wake.clear()
                await self._wake.wait()
                continue
            scope, (due, trigger) = min(self._due.items(), key=lambda kv: kv[1][0])
            wait = due - loop.time()
            if wait > 0:
                self._wake.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), wait)
                continue
            self._due.pop(scope, None)
            self._busy = True
            try:
                rep = await self.run_scope(scope, trigger)
            except Exception as e:  # noqa: BLE001 - run_scope 已兜底，这里防御
                logger.warning(f"[lifelike.session_extract] worker error scope={scope}: {type(e).__name__}")
                continue
            finally:
                self._busy = False
            if rep.status == "busy":
                self.schedule(scope, BUSY_RECHECK_S, trigger)
            elif rep.status == "retry":
                self.schedule(scope, rep.retry_in, "retry")
            elif rep.more and rep.status in ("done", "shadow", "gated", "failed"):
                self.schedule(scope, SETTLE_S, "more")

    # ------------------------------------------------------------ one run
    async def run_scope(self, scope_key: str, trigger: str = "manual") -> SessionReport:
        rep = SessionReport(scope_key=scope_key, trigger=trigger, mode=self.mode())
        if rep.mode == MODE_OFF:
            rep.status = MODE_OFF
            return rep
        now = float(self._clock())
        if now < self._paused_until:
            rep.status = "paused"
            return rep
        owner = self._owner_resolver(scope_key)
        if not owner:
            rep.status = "no_owner"
            return rep
        try:
            await self._run(scope_key, str(owner), now, rep)
        except Exception as e:  # noqa: BLE001 - 边界兜底：游标不动，行保持旧状态
            err = f"internal:{type(e).__name__}"
            rep.status, rep.error = "failed", err
            # 审查 L1：内部异常（可能已经调过 LLM）不能按 more 每 15 秒重排，
            # 改走 _fail 的退避 / 尝试计数；_fail 自身失败时也不重排。
            more, rep.more = rep.more, False
            try:
                state = await asyncio.to_thread(self._load_state_tx, scope_key)
                if state is not None:
                    await asyncio.to_thread(self._fail, scope_key, state, rep, err)
                    if rep.status == "failed":
                        rep.more = more  # 已跳过本段（游标前移），后续段可以接着排
            except Exception as e2:  # noqa: BLE001
                rep.status, rep.error = "failed", err
                logger.warning(
                    f"[lifelike.session_extract] fail bookkeeping error scope={scope_key}: {type(e2).__name__}"
                )
            self._note_failure(now)
        if rep.status not in ("empty", "busy", MODE_OFF):
            logger.info(
                f"[lifelike.session_extract] scope={scope_key} trigger={trigger} mode={rep.mode}"
                f" status={rep.status} rows={rep.rows} owner_rows={rep.owner_rows} upto={rep.upto}"
                f" v={rep.session_version} llm={int(rep.llm_called)} created={len(rep.created)}"
                f" updated={len(rep.updated)} closed={len(rep.closed)} dedup={rep.deduped}"
                f" invalid={rep.invalid} done_rows={rep.marked_done}"
                + (f" proposed={json.dumps(rep.proposed, sort_keys=True)}" if rep.proposed else "")
                + (f" error={rep.error}" if rep.error else "")
            )
        return rep

    def _note_failure(self, now: float) -> None:
        self._fails += 1
        if self._fails >= BREAKER_FAILS:
            self._fails = 0
            self._paused_until = now + BREAKER_PAUSE_S
            logger.warning(
                f"[lifelike.session_extract] paused_error: {BREAKER_FAILS} consecutive failures,"
                f" pause {int(BREAKER_PAUSE_S)}s"
            )

    async def _run(self, scope_key: str, owner: str, now: float, rep: SessionReport) -> None:
        state = await asyncio.to_thread(self._ensure_state, scope_key)
        rows = await asyncio.to_thread(
            self._store.inbound_after, scope_key, state["last_msg_id"], now - WINDOW_S, MAX_ROWS + 1
        )
        if not rows:
            rep.status = "empty"
            return
        # 快路径还在处理的新行 → 稍后再来（太久的 pending 不再阻塞）
        if any(
            r.get("extract_status") == m.INBOUND_PENDING
            and now - float(r.get("received_at") or 0) < BUSY_PENDING_MAX_AGE_S
            for r in rows
        ):
            rep.status = "busy"
            return
        if len(rows) > MAX_ROWS:
            rows, rep.more = rows[:MAX_ROWS], True
        rep.rows = len(rows)
        rep.upto = max(int(r["msg_id"]) for r in rows)
        wm = await asyncio.to_thread(self._store.get_watermark, scope_key)
        rep.session_version = int((wm or {}).get("session_version") or 0)

        # 按 sender 硬过滤：只要主人本人、非命令、非空
        work: list[dict[str, Any]] = []
        for r in rows:
            text = str(r.get("text") or "")
            if str(r.get("sender_id")) != owner or not text.strip() or text.strip().startswith("/"):
                continue
            w = dict(r)
            w["_text"] = text[:OWNER_LINE_MAX]
            work.append(w)
        rep.owner_rows = len(work)

        content = [w for w in work if not rules.is_ack_only(w["_text"])]
        if sum(_content_chars(w["_text"]) for w in content) < MIN_OWNER_CHARS:
            rep.status = "gated"
            ok = await asyncio.to_thread(self._advance, scope_key, state, rep, [], {}, work, False)
            if ok:
                await asyncio.to_thread(self._bump, "gated")
            return

        candidates = await asyncio.to_thread(self._items._collect_candidates, scope_key, work, now)
        bot_rows = await asyncio.to_thread(self._bot_context, scope_key, work, now)
        belief_cands = None
        if self.beliefs_in_prompt():
            try:
                belief_cands = await asyncio.to_thread(self._beliefs.candidates, scope_key)
            except Exception as e:  # noqa: BLE001 - 认知读失败 → 本轮按 P0-4 原样抽取
                logger.warning(f"[lifelike.beliefs] candidates failed scope={scope_key}: {type(e).__name__}")
                belief_cands = None
        prompt = build_session_prompt(work, bot_rows, candidates, belief_cands)

        # 模块内部日上限（全局预算之外再兜一层）
        counters = await asyncio.to_thread(self._store.get_counters)
        if int(counters.get(f"lifelike.budget.calls:{MODULE}", 0)) >= MODULE_DAILY_CALLS:
            rep.status, rep.error = "budget", "module_daily"
            await asyncio.to_thread(self._bump, "budget")
            return

        rep.llm_called = True
        timeout = self._timeout()
        try:
            raw = await self._budget.guarded_generate(
                MODULE,
                prompt,
                lambda p, t: asyncio.wait_for(self._llm(p, t), t + 5.0),
                timeout,
                usage_probe=self._usage_probe,
                output_reserve=OUTPUT_RESERVE,
            )
        except CheapBudgetExceeded as e:
            rep.llm_called = False
            rep.status, rep.error = "budget", e.reason
            await asyncio.to_thread(self._bump, "budget")
            return
        except _RETRYABLE as e:
            err = "llm_timeout" if isinstance(e, (asyncio.TimeoutError, TimeoutError)) else "llm_network"
            await asyncio.to_thread(self._fail, scope_key, state, rep, err)
            self._note_failure(now)
            return
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[lifelike.session_extract] llm error scope={scope_key}: {type(e).__name__}")
            await asyncio.to_thread(self._fail, scope_key, state, rep, "llm_error")
            self._note_failure(now)
            return
        await asyncio.to_thread(self._bump, "llm")

        raw_ops = parse_session_json(raw)
        if raw_ops is None:
            await asyncio.to_thread(self._fail, scope_key, state, rep, "invalid_json")
            self._note_failure(now)
            return
        self._fails = 0

        ops = self._validate(raw_ops, work, bot_rows, candidates, rep)
        for op in ops:
            rep.proposed[op.op] = rep.proposed.get(op.op, 0) + 1
        apply = rep.mode == MODE_ON
        ok = await asyncio.to_thread(
            self._advance, scope_key, state, rep, ops if apply else [], candidates, work, apply
        )
        if not ok:
            return
        if belief_cands is not None:
            await self._apply_beliefs(scope_key, raw, work, belief_cands)
        if apply:
            rep.status = "done"
            await asyncio.to_thread(self._bump_many, {
                "created": len(rep.created), "updated": len(rep.updated), "closed": len(rep.closed),
                "deduped": rep.deduped, "invalid": rep.invalid, "rows_done": rep.marked_done,
            })
        else:
            rep.status = "shadow"
            await asyncio.to_thread(
                self._bump_many,
                {**{f"shadow_{k}": v for k, v in rep.proposed.items()}, "invalid": rep.invalid},
            )

    async def _apply_beliefs(
        self, scope_key: str, raw: Any, work: list[dict[str, Any]], belief_cands: list[dict[str, Any]]
    ) -> None:
        """P1-7：游标已推进后写入认知（shadow/on 都写；注入只在 on）。失败只记日志。"""
        try:
            from .beliefs import validate_belief_ops
            from .tombstones import text_hits_topic_tombstone

            raw_beliefs = parse_session_beliefs(raw)
            if not raw_beliefs:
                return
            vr = validate_belief_ops(
                raw_beliefs,
                work,
                [c["belief_id"] for c in belief_cands],
                scope_key=scope_key,
                tombstone_hit=lambda *t: text_hits_topic_tombstone(self._store, *t),
            )
            out = await asyncio.to_thread(self._beliefs.apply_ops, scope_key, vr.ops) if vr.ops else {}
            counts = {f"beliefs_{k}": v for k, v in out.items()}
            counts.update(
                {"beliefs_invalid": vr.invalid, "beliefs_sensitive": vr.sensitive, "beliefs_tombstoned": vr.tombstoned}
            )
            await asyncio.to_thread(self._bump_many, counts)
            logger.info(
                f"[lifelike.beliefs] scope={scope_key} mode={self.memory_mode()} ops={len(vr.ops)}"
                f" invalid={vr.invalid} sensitive={vr.sensitive} tombstoned={vr.tombstoned}"
                + (f" result={json.dumps(out, sort_keys=True)}" if out else "")
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[lifelike.beliefs] apply failed scope={scope_key}: {type(e).__name__}")

    def _bump_many(self, counts: dict[str, int]) -> None:
        for k, v in counts.items():
            self._bump(k, v)

    def _timeout(self) -> float:
        try:
            t = float(self._timeout_get()) if self._timeout_get is not None else DEFAULT_TIMEOUT_S
        except Exception:  # noqa: BLE001
            t = DEFAULT_TIMEOUT_S
        return max(5.0, t)

    def _bot_context(self, scope_key: str, work: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
        """角色 已发送的回复（只作上下文）：本段时间范围 ± 10 分钟内，最多 20 条"""
        if not work:
            return []
        lo = min(float(w["msg_ts"]) for w in work) - BOT_MARGIN_S
        hi = max(float(w["msg_ts"]) for w in work) + BOT_MARGIN_S
        try:
            out = self._store.recent_outbound(scope_key, (m.OUT_SENT,), ("bot",), BOT_CONTEXT_MAX * 3)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[lifelike.session_extract] outbound read failed: {type(e).__name__}")
            return []
        picked = [
            o for o in out
            if lo <= float(o.get("sent_at") or o.get("created_at") or 0) <= min(hi, now)
            and str(o.get("text") or "").strip()
        ]
        picked.sort(key=lambda o: float(o.get("sent_at") or o.get("created_at") or 0))
        return picked[-BOT_CONTEXT_MAX:]

    def _validate(
        self,
        raw_ops: list[Any],
        work: list[dict[str, Any]],
        bot_rows: list[dict[str, Any]],
        candidates: dict[str, _Candidate],
        rep: SessionReport,
    ) -> list[_Op]:
        bot_texts = [str(b.get("text") or "") for b in bot_rows]
        ops: list[_Op] = []
        rep.invalid += max(0, len(raw_ops) - MAX_ITEM_OPS)
        for raw_op in raw_ops[:MAX_ITEM_OPS]:
            try:
                # 候选行只给主人消息：evidence 必须在主人原文里（她的话不能成为主人的事项）
                op = validate_op(raw_op, work, candidates)
            except _InvalidOp:
                rep.invalid += 1
                continue
            if op.op == "create":
                if op.msg.get("extract_status") == m.INBOUND_DONE:
                    rep.invalid += 1  # 快路径已处理过的消息不再新建（防重复）
                    continue
                owner_text = op.msg["_text"].strip()
                if any(op.evidence in bt for bt in bot_texts) and len(owner_text) <= len(op.evidence) + 4:
                    rep.invalid += 1  # 只是在复述 角色 的原话
                    continue
            ops.append(op)
        res = ExtractResult(scope_key=rep.scope_key)
        ops = ItemExtractor._guard_closing(ops, candidates, work, res)
        rep.invalid += res.invalid_ops
        return ops

    # ------------------------------------------------------------ commit (worker thread)
    def _tx(self):
        tx = getattr(self._store, "_tx", None)
        return tx() if callable(tx) else contextlib.nullcontext()

    def _advance(
        self,
        scope_key: str,
        state: dict[str, Any],
        rep: SessionReport,
        ops: list[_Op],
        candidates: dict[str, _Candidate],
        work: list[dict[str, Any]],
        apply: bool,
    ) -> bool:
        """单事务：CAS 游标 → （on）写事项、覆盖行 skipped→done → 推进游标。False=冲突丢弃"""
        with self._tx():
            cur = self._load_state(scope_key)
            if cur is None or cur["last_msg_id"] != state["last_msg_id"]:
                rep.status = "conflict"
                return False
            if apply:
                for op in ops:
                    self._apply_op(scope_key, rep, op, candidates)
                skipped = [int(w["msg_id"]) for w in work if w.get("extract_status") == m.INBOUND_SKIPPED]
                if skipped:
                    rep.marked_done = self._store.mark_inbound(scope_key, skipped, m.INBOUND_DONE, "")
            self._save_state(
                scope_key,
                last_msg_id=max(rep.upto, cur["last_msg_id"]),
                session_version=rep.session_version,
                attempts=0,
                last_error="",
            )
        return True

    def _apply_op(
        self, scope_key: str, rep: SessionReport, op: _Op, candidates: dict[str, _Candidate]
    ) -> None:
        store = self._store
        owner = str(op.msg["sender_id"])
        base_ts = float((op.when_msg or op.msg)["msg_ts"])
        remind_text = op.msg["_text"]
        try:
            if op.op == "create":
                assert op.kind is not None
                if store.find_active_by_dedupe(m.make_dedupe_key(scope_key, op.kind, op.topic)):
                    rep.deduped += 1
                    return
                item = self._items._create(
                    scope_key, owner, op, remind_text, base_ts, reason="session_extract_create"
                )
                if item is not None:
                    rep.created.append(item["item_id"])
                    self._items._adopt_promises(scope_key, item, float(op.msg["msg_ts"]))
                return
            assert op.item_id is not None
            cur = store.get_item(op.item_id)
            if cur is None or cur["status"] != m.STATUS_ACTIVE:
                rep.invalid += 1
                return
            changes = ItemExtractor._item_changes(op, cur, remind_text, base_ts)
            if not changes:
                return
            new = store.update_item(
                op.item_id,
                expected_revision=int(cur["revision"]),
                reason=f"session_extract_{op.op}",
                source_msg_id=str(op.msg["msg_id"]),
                **changes,
            )
            if new["status"] != m.STATUS_ACTIVE:
                rep.closed.append(op.item_id)
            else:
                rep.updated.append(op.item_id)
        except (RevisionConflict, ItemNotFound) as e:
            rep.invalid += 1
            logger.info(
                f"[lifelike.session_extract] op={op.op} item={op.item_id} skipped: {type(e).__name__}"
            )

    def _load_state_tx(self, scope_key: str) -> dict[str, Any] | None:
        with self._tx():
            return self._load_state(scope_key)

    def _fail(self, scope_key: str, state: dict[str, Any], rep: SessionReport, err: str) -> None:
        """暂时性失败：attempts<3 → 游标不动、退避重试；否则跳过本段（行保持旧状态）"""
        rep.error = err
        attempts = int(state.get("attempts") or 0) + 1
        with self._tx():
            cur = self._load_state(scope_key)
            if cur is None or cur["last_msg_id"] != state["last_msg_id"]:
                rep.status = "conflict"
                return
            if attempts < MAX_ATTEMPTS:
                self._save_state(
                    scope_key,
                    last_msg_id=cur["last_msg_id"],
                    session_version=cur["session_version"],
                    attempts=attempts,
                    last_error=err,
                )
                rep.status = "retry"
                rep.retry_in = RETRY_BACKOFF_S * (2 ** (attempts - 1))
            else:
                self._save_state(
                    scope_key,
                    last_msg_id=max(rep.upto, cur["last_msg_id"]),
                    session_version=rep.session_version,
                    attempts=0,
                    last_error=err,
                )
                rep.status = "failed"
        self._bump("retry" if rep.status == "retry" else "failed")
