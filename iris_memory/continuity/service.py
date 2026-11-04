"""
Iris Chat Memory - 连续性服务门面（DESIGN.md §1/§6/§7/§8/§10）

``ContinuityService`` 组装 store / 抽取 worker / 收尾 / 合批 / 上下文 / 主动桥接 /
频道采集 / 摘要，供 main.py 调用。约定：

- 所有公共方法内部 try/except：任何故障只记日志（异常类型，不含正文）并返回
  安全默认值，原有私聊流程不受影响。
- 总开关 ``continuity.enable=false`` = 软回滚：停止新增选题、事项注入、收尾、
  合批、抽取、频道采集与摘要；**保留**发送目标硬拦截、出站记账、额度与暂停
  恢复（水位推进）、私聊专属人格层注入（它是人格的一部分，不是事项）。
- store 调用一律 ``asyncio.to_thread``。
"""

from __future__ import annotations

import asyncio
import functools
import json
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .adapter_watchdog import HEALTH_DIRNAME, AdapterWatchdog
from ..lifelike.guards.numeric import guard_text as guard_numeric
from .batching import PASS, MessageBatcher
from .channel_ingest import ChannelIngestor, OwnerCatchUp
from .closure import STATE_ENDED, ClosureTracker
from .context import (
    CLOSE_TAG,
    OPEN_TAG,
    PURPOSE_PROACTIVE,
    PURPOSE_REPLY,
    ContextAssembler,
)
from .digest import DigestTask
from .extractor import ExtractionWorker, ItemExtractor, record_bot_promise
from .proactive_bridge import DEFAULT_FORBIDDEN_CHATS, ProactiveBridge, SendPolicy
from . import models as m
from .store import ContinuityStore

# 活人感 P0-4（与 lifelike.memory.session_extractor 的 KEY_MODE / MODULE 一致）
SESSION_EXTRACT_KEY = "lifelike_session_extract_mode"
SESSION_EXTRACT_MODULE = "lifelike_session_extract"
WORLD_MODULE = "lifelike_world"  # 活人感 P1-5（token_stats 模块名）

__all__ = [
    "ContinuityConfig",
    "ContinuityService",
    "chat_id_variants",
    "is_silent_or_forbidden",
]

DB_DIRNAME = "continuity"
DB_FILENAME = "continuity.db"
MAINTENANCE_INTERVAL_S = 3600.0
# 活人感 P0-3：第三方原文保留期，每日一次（随 _maintenance 触发）
RETENTION_INTERVAL_S = 86400.0
RETENTION_DAYS_KEY = "lifelike_retention_days_observed"
RETENTION_DRY_RUN_KEY = "lifelike_retention_dry_run"
RETENTION_DAYS_DEFAULT = 30
RETENTION_DAYS_MAX = 3650
DEFAULT_PLATFORM_ID = "userbot"
PROBE_FILENAME = "acceptance_probe.json"
PROBE_RESULT_FILENAME = "acceptance_probe.result.json"
PROBE_PREFIX = "【验收】"
PROBE_MAX_CHARS = 200
PROBE_DAILY_MAX = 3
PROBE_COUNTER = "acceptance_probe"

_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f", ""}

# (default, lo, hi)
_INT_KEYS: dict[str, tuple[int, int, int]] = {
    "merge_quiet_ms": (1200, 0, 10000),
    "merge_cap_ms": (2000, 0, 15000),
    "explicit_wait_timeout_s": (180, 10, 1800),
    "closure_cooldown_minutes": (120, 0, 1440),
    "inject_max_items": (3, 1, 10),
    "inject_max_chars": (600, 100, 4000),
    "extract_debounce_s": (6, 0, 120),
    "extract_timeout_s": (30, 5, 300),
    "extract_queue_max": (50, 1, 1000),
    "digest_interval_hours": (3, 1, 72),
    "sticker_describe_daily_cap": (100, 0, 1000),
}
_BOOL_KEYS: dict[str, bool] = {
    "enable": True,
    "items_enable": True,
    "proactive_topics_enable": True,
    "closure_enable": True,
    "batching_enable": True,
    "channel_ingest_enable": False,
    "digest_enable": False,
    "sticker_understanding_enable": True,
    "image_understanding_enable": True,
    "sticker_send_enable": True,
    "sticker_groups_enable": False,
    "sticker_scope_unified": False,
}
_LIST_KEYS = ("observe_chat_ids", "owner_channel_ids")
# observe_chat_ids 里的通配（U3）：并入平台 id_whitelist 中的全部群（动态；排除私聊 id 与主人频道）
OBSERVE_WHITELIST_TOKEN = "*whitelist"
_DERIVED_FIELDS = frozenset({"observe_whitelist", "observe_explicit_ids"})


def _has_token(value: Any, token: str) -> bool:
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, (str, int)):
        items = str(value).replace(",", " ").split()
    else:
        try:
            items = [str(v) for v in value]
        except TypeError:
            return False
    return any(str(v).strip().lower() == token for v in items)


def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _TRUE:
            return True
        if v in _FALSE:
            return False
    return default


def _to_int(value: Any, default: int, lo: int, hi: int) -> int:
    if isinstance(value, bool) or value is None:
        val = default
    else:
        try:
            val = int(float(value))
        except (TypeError, ValueError, OverflowError):
            val = default
    return min(hi, max(lo, val))


def _to_int_list(value: Any) -> tuple[int, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        value = str(value).replace(",", " ").split()
    out: list[int] = []
    try:
        seq = list(value)
    except TypeError:
        return ()
    for v in seq:
        if isinstance(v, bool):
            continue
        try:
            n = int(str(v).strip())
        except (TypeError, ValueError):
            continue
        if n not in out:
            out.append(n)
    return tuple(out)


@dataclass(frozen=True)
class ContinuityConfig:
    enable: bool = True
    items_enable: bool = True
    proactive_topics_enable: bool = True
    closure_enable: bool = True
    batching_enable: bool = True
    channel_ingest_enable: bool = False
    digest_enable: bool = False
    observe_chat_ids: tuple[int, ...] = ()
    owner_channel_ids: tuple[int, ...] = ()
    merge_quiet_ms: int = 1200
    merge_cap_ms: int = 2000
    explicit_wait_timeout_s: int = 180
    closure_cooldown_minutes: int = 120
    inject_max_items: int = 3
    inject_max_chars: int = 600
    extract_debounce_s: int = 6
    extract_timeout_s: int = 30
    extract_queue_max: int = 50
    digest_interval_hours: int = 3
    sticker_understanding_enable: bool = True
    image_understanding_enable: bool = True
    sticker_send_enable: bool = True
    sticker_groups_enable: bool = False
    sticker_scope_unified: bool = False
    sticker_describe_daily_cap: int = 100
    # observe_chat_ids 含 "*whitelist"：服务侧 cfg 会把白名单群并入 observe_chat_ids（from_config 本身不展开）
    observe_whitelist: bool = False
    # 展开前的显式 observe_chat_ids（未展开时与 observe_chat_ids 相同；digest 只用显式列表）
    observe_explicit_ids: tuple[int, ...] | None = None

    # ---------------------------------------------------------------- derived
    @property
    def items_active(self) -> bool:
        return self.enable and self.items_enable

    @property
    def topics_active(self) -> bool:
        return self.enable and self.items_enable and self.proactive_topics_enable

    @property
    def closure_active(self) -> bool:
        return self.enable and self.closure_enable

    @property
    def batching_active(self) -> bool:
        return self.enable and self.batching_enable

    @property
    def channel_active(self) -> bool:
        return self.enable and self.channel_ingest_enable and bool(self.observe_chat_ids)

    @property
    def digest_active(self) -> bool:
        return self.enable and self.digest_enable and bool(self.observe_chat_ids)

    # ---------------------------------------------------------------- parse
    @classmethod
    def from_config(cls, cfg: Any) -> ContinuityConfig:
        """从 Iris ``Config``（``get("continuity.<key>")``）、嵌套 dict
        (``{"continuity": {...}}``) 或扁平 dict 读取；类型收敛 + 范围钳制。
        读取失败的键回退默认值，绝不抛出。"""

        def raw(key: str) -> Any:
            if cfg is None:
                return None
            try:
                if isinstance(cfg, Mapping):
                    sec = cfg.get("continuity")
                    if isinstance(sec, Mapping):
                        return sec.get(key)
                    return cfg.get(key)
                getter = getattr(cfg, "get", None)
                if getter is None:
                    return getattr(cfg, key, None)
                return getter(f"continuity.{key}", None)
            except Exception:
                return None

        values: dict[str, Any] = {}
        for key, default in _BOOL_KEYS.items():
            values[key] = _to_bool(raw(key), default)
        for key, (default, lo, hi) in _INT_KEYS.items():
            values[key] = _to_int(raw(key), default, lo, hi)
        values["merge_cap_ms"] = max(values["merge_cap_ms"], values["merge_quiet_ms"])
        for key in _LIST_KEYS:
            values[key] = _to_int_list(raw(key))
        if _has_token(raw("observe_chat_ids"), OBSERVE_WHITELIST_TOKEN):
            values["observe_whitelist"] = True
        return cls(**values)

    @property
    def explicit_observe_ids(self) -> tuple[int, ...]:
        return self.observe_chat_ids if self.observe_explicit_ids is None else self.observe_explicit_ids

    def as_dict(self) -> dict[str, Any]:
        """用户可见键（与 _conf_schema.json 的 continuity 分组一致；派生字段不含）"""
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name not in _DERIVED_FIELDS}


# ---------------------------------------------------------------------------
# 发送目标判定（被动回复装饰阶段也用）
# ---------------------------------------------------------------------------


def base_chat_id(chat_id: Any) -> str:
    """去掉话题后缀：``-100xxx#thread`` → ``-100xxx``（话题属于同一个群/频道）。"""
    return str(chat_id or "").strip().split("#", 1)[0].strip()


def chat_id_variants(chat_id: Any) -> set[str]:
    """Telegram 频道/超级群 id 的等价写法：-100xxx / -xxx / xxx（忽略 #话题 后缀）"""
    s = base_chat_id(chat_id)
    if not s:
        return set()
    out = {s}
    if s.startswith("-100"):
        out.add(s[4:])
        out.add("-" + s[4:])
    elif s.startswith("-"):
        out.add(s[1:])
        out.add("-100" + s[1:])
    else:
        out.add("-100" + s)
        out.add("-" + s)
    return out


def is_silent_or_forbidden(chat_id: Any, silent_sessions: Iterable[Any] = ()) -> bool:
    """群/频道 id 是否属于 silent_sessions 或内置禁发频道（任何等价写法）"""
    ids = chat_id_variants(chat_id)
    if not ids:
        return False
    try:
        blocked = {base_chat_id(x) for x in (silent_sessions or ()) if base_chat_id(x)}
    except TypeError:
        blocked = set()
    if base_chat_id(chat_id) in blocked:
        return True
    for c in set(DEFAULT_FORBIDDEN_CHATS) | blocked:
        if ids & chat_id_variants(c):
            return True
    return False


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_TEXT_COMPONENTS = frozenset({"Plain", "At", "AtAll", "Reply", "Face"})


def event_has_media(event: Any) -> bool:
    try:
        chain = event.get_messages() or []
    except Exception:
        return False
    try:
        return any(type(c).__name__ not in _TEXT_COMPONENTS for c in chain)
    except TypeError:
        return False


class _EventView:
    """MessageBatcher 期望的事件接口：message_str / has_media / stop()"""

    def __init__(self, event: Any) -> None:
        self._event = event
        self.has_media = event_has_media(event)

    @property
    def message_str(self) -> str:
        return str(getattr(self._event, "message_str", "") or "")

    @message_str.setter
    def message_str(self, value: str) -> None:
        self._event.message_str = value
        mo = getattr(self._event, "message_obj", None)
        if mo is not None and hasattr(mo, "message_str"):
            try:
                mo.message_str = value
            except Exception:
                pass

    def stop(self) -> None:
        self._event.stop_event()


class _LiveConfig:
    """把服务当前配置以属性形式暴露给子模块（热更新即时生效）"""

    def __init__(self, svc: ContinuityService) -> None:
        self._svc = svc

    def __getattr__(self, name: str) -> Any:
        return getattr(self._svc.cfg, name)


def _guard(default: Any = None) -> Callable:
    """公共 async 方法边界：异常 → 日志 + 默认值"""

    def deco(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(fn)
        async def wrapper(self: ContinuityService, *args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(self, *args, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[continuity] service.{fn.__name__} failed: {type(e).__name__}")
                return default() if callable(default) else default

        return wrapper

    return deco


# ---------------------------------------------------------------------------
# lifelike memory helpers（组件管理器只读访问；失败返回 None / []）
# ---------------------------------------------------------------------------


def _component(name: str) -> Any:
    try:
        from ..core import get_component_manager

        return get_component_manager().get_available_component(name)
    except Exception:
        return None


def _learning_storage() -> Any:
    comp = _component("learning")
    return getattr(comp, "storage", None) if comp is not None else None


async def _profile_names(member_id: str) -> list[str]:
    """Iris 画像里的称呼（只读 user_name / historical_names；其余字段不读）"""
    comp = _component("profile")
    if comp is None:
        return []
    prof = await comp.get_user_profile(str(member_id), "default")
    if prof is None:
        return []
    names = [str(prof.user_name or "")] + [str(n) for n in (prof.historical_names or [])[-3:]]
    return [n for n in names if n.strip()]


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------


class ContinuityService:
    def __init__(
        self,
        *,
        context: Any,
        config_source: Any,
        state: Any,
        owner_ids_get: Callable[[], Iterable[Any]],
        silent_sessions_get: Callable[[], Iterable[Any]],
        data_dir: Path | str | None = None,
        llm_manager: Any = None,
        umo_get: Callable[[str], str | None] | None = None,
        profile_text: Callable[[str], Awaitable[str | None]] | None = None,
        l1_recent: Callable[[str, int], Any] | None = None,
        save_fn: Callable[[], Awaitable[None]] | None = None,
        platform_id_get: Callable[[], str | None] | None = None,
        resume_cb: Callable[[str], Any] | None = None,
        clock: Callable[[], float] = time.time,
        store: Any = None,
    ) -> None:
        self._context = context
        self._config_source = config_source
        self._owner_ids_get = owner_ids_get
        self._silent_get = silent_sessions_get
        self._llm_manager = llm_manager
        self._platform_id_get = platform_id_get
        self._clock = clock
        self._open = False
        self._last_maintenance = 0.0
        self._last_retention = 0.0
        self._ingestor: ChannelIngestor | None = None
        self._owner_catchup: OwnerCatchUp | None = None
        # 水位真实推进后恢复主动（state.resume_after_owner_message）；离线补采用
        self._resume_cb = resume_cb
        self._digest: DigestTask | None = None
        self._watchdog: AdapterWatchdog | None = None
        self._data_dir = Path(data_dir) if data_dir is not None else None

        if store is None:
            if data_dir is None:
                raise ValueError("data_dir or store is required")
            store = ContinuityStore(Path(data_dir) / DB_DIRNAME / DB_FILENAME, clock=clock)
        self.store = store
        cfg = self.cfg
        live = _LiveConfig(self)

        self.closure = ClosureTracker(store, clock, explicit_wait_timeout_s=cfg.explicit_wait_timeout_s)
        self.batcher = MessageBatcher(cfg.as_dict(), closure_getter=store.get_closure)
        self.assembler = ContextAssembler(
            store,
            self.owner_ids,
            config=live,
            clock=clock,
            profile_text=profile_text,
            l1_recent=l1_recent,
            relation_text=self._relation_section,
            world_section=self._world_render,
        )
        self.extractor = ItemExtractor(store, self._llm_extract, config=live, clock=clock)
        self.worker = ExtractionWorker(self.extractor, config=cfg.as_dict())
        # 活人感 P0-4：会话批量抽取 + 廉价模型硬预算（lifelike_session_extract_mode=off 时完全不介入）
        # 延迟导入：lifelike 依赖 continuity 子模块，避免包级循环导入
        from ..lifelike.budget import CheapBudget
        from ..lifelike.memory.session_extractor import SessionExtractor

        self.cheap_budget = CheapBudget(store, self._config_get, clock)
        # 活人感 P1-7 / P1-13：用户认知、forget、群友认知（各自开关；off 时不读写任何状态）
        self.lifelike_memory = self._build_lifelike_memory(store, clock)
        self.session_extractor = SessionExtractor(
            store,
            self.extractor,
            self._llm_session_extract,
            self.cheap_budget,
            mode_get=lambda: self._config_get(SESSION_EXTRACT_KEY, "off"),
            clock=clock,
            usage_probe=self._llm_usage_probe,
            timeout_get=lambda: float(self.cfg.extract_timeout_s) + 15.0,
            beliefs=self.lifelike_memory.beliefs if self.lifelike_memory is not None else None,
            memory_mode_get=lambda: self._config_get("lifelike_memory_mode", "off"),
            memory_shadow_extract_get=lambda: self._config_get("lifelike_memory_shadow_extract", False),
        )
        # 活人感 P1-6 / P1-11：关系事件溯源 + 关系渐进（lifelike_relation_mode=off 时不读写任何状态）
        from ..lifelike.relation.service import RelationService

        self.relation = RelationService(
            store,
            self._config_get,
            clock=clock,
            llm_generate=self._llm_relation,
            budget=self.cheap_budget,
            usage_probe=self._llm_usage_probe,
        )
        # 活人感 P1-5：生活系统（lifelike_enable + lifelike_world_mode；off 时不建表、不起任务）
        from ..lifelike.world.service import WorldService

        self.world = WorldService(
            store,
            self._config_get,
            llm=self._llm_world,
            budget=self.cheap_budget,
            usage_probe=self._llm_usage_probe,
            provider_name=self.background_provider_id,
            is_owner_scope=self.is_owner_scope,
            clock=clock,
        )
        self.bridge = ProactiveBridge(
            store,
            state,
            policy_get=self.policy,
            context=context,
            umo_get=umo_get,
            speech_context_fn=self._speech_context,
            clock=clock,
            closure_cooldown_minutes=lambda: self.cfg.closure_cooldown_minutes
            if self.cfg.closure_active
            else 0,
            topics_enabled=lambda: self.cfg.topics_active,
            save_fn=save_fn,
            after_sent=self._after_proactive_sent,
        )
        # 活人感 P1-8 主动候选池 / P1-12 群→私聊关心桥（各自开关 off 时不读写新表，行为不变）
        self.initiative: Any = None
        self.care: Any = None
        self._init_lifelike_initiative(store, clock)

    # ------------------------------------------------------------ lifelike memory (P1-7 / P1-13)

    def _build_lifelike_memory(self, store: Any, clock: Callable[[], float]) -> Any:
        try:
            from ..lifelike.memory.service import LifelikeMemory, set_current

            lm = LifelikeMemory(
                store,
                config_get=self._config_get,
                owner_ids=self.owner_ids,
                observe_chat_ids=lambda: self.cfg.observe_chat_ids,
                owner_channel_ids=lambda: self.cfg.owner_channel_ids,
                id_whitelist=self._id_whitelist,
                profile_names=_profile_names,
                l2_get=lambda: _component("l2_memory"),
                learning_get=_learning_storage,
                clock=clock,
            )
            set_current(lm)
            return lm
        except Exception as e:
            logger.warning(f"[lifelike.memory] init failed: {type(e).__name__}")
            return None

    async def lifelike_blocks(self, scope_key: str, sender_id: Any, text: str) -> list[str]:
        """主人私聊被动回复的注入段（认知 / 群友印象）；模式非 on 或异常 → []"""
        lm = self.lifelike_memory
        if lm is None or not self._open:
            return []
        try:
            return await lm.inject_blocks(scope_key, sender_id, text)
        except Exception as e:
            logger.warning(f"[lifelike.memory] blocks failed: {type(e).__name__}")
            return []

    # ------------------------------------------------------------ config / identity

    @property
    def cfg(self) -> ContinuityConfig:
        cfg = ContinuityConfig.from_config(self._config_source)
        if cfg.observe_whitelist:
            cfg = self._expand_observe(cfg)
        return cfg

    def _expand_observe(self, cfg: ContinuityConfig) -> ContinuityConfig:
        """observe_chat_ids = 显式 id ∪ 平台 id_whitelist 里的群（排除私聊 id、主人 id 与主人频道）。
        白名单未启用 / 读取失败 → 只用显式 id。"""
        try:
            from ..lifelike.social.l2_filter import whitelist_group_ids

            extra = whitelist_group_ids(
                self._id_whitelist(), exclude=[*self.owner_ids(), *cfg.owner_channel_ids]
            )
        except Exception as e:
            logger.debug(f"[continuity] observe whitelist expand failed: {type(e).__name__}")
            extra = []
        merged = list(cfg.observe_chat_ids)
        for gid in extra:
            if gid not in merged:
                merged.append(gid)
        return replace(cfg, observe_chat_ids=tuple(merged), observe_explicit_ids=cfg.observe_chat_ids)

    @property
    def is_open(self) -> bool:
        return self._open

    def owner_ids(self) -> set[str]:
        try:
            return {str(o).strip() for o in (self._owner_ids_get() or ()) if str(o).strip()}
        except Exception as e:
            logger.warning(f"[continuity] owner_ids failed: {type(e).__name__}")
            return set()

    def silent_sessions(self) -> set[str]:
        try:
            return {str(s).strip() for s in (self._silent_get() or ()) if str(s).strip()}
        except Exception as e:
            logger.warning(f"[continuity] silent_sessions failed: {type(e).__name__}")
            return set()

    def policy(self) -> SendPolicy:
        """发送目标策略；软回滚时同样生效（硬拦截不随总开关关闭）"""
        silent = frozenset(self.silent_sessions())
        return SendPolicy(
            owner_ids=frozenset(self.owner_ids()),
            silent_sessions=silent,
            forbidden_chats=frozenset(DEFAULT_FORBIDDEN_CHATS) | silent,
        )

    def owner_scope(self, sender_id: Any) -> str | None:
        """主人私聊 scope_key；非主人返回 None"""
        sid = str(sender_id or "").strip()
        if sid and sid in self.owner_ids():
            return f"private:{sid}"
        return None

    def is_owner_scope(self, scope_key: Any) -> bool:
        s = str(scope_key or "")
        return s.startswith("private:") and s.split(":", 1)[1] in self.owner_ids()

    def is_blocked_target(self, chat_id: Any) -> bool:
        return is_silent_or_forbidden(chat_id, self.silent_sessions())

    # ------------------------------------------------------------ lifecycle

    @_guard(None)
    async def start(self) -> None:
        self._start_watchdog()  # 不依赖账本是否打开
        try:
            await asyncio.to_thread(self.store.open)
            self._open = True
        except Exception as e:
            # 失败关闭：bridge 拿不到账本时主动发起会以 bridge_error 跳过，不会绕过账本直发
            logger.error(f"[continuity] store open failed: {type(e).__name__}; continuity disabled")
            return
        cfg = self.cfg
        if cfg.items_active:
            await self.worker.start()
            try:
                await self._start_session_extract()
            except Exception as e:
                logger.warning(f"[continuity] session extract start failed: {type(e).__name__}")
        self._start_background(cfg)
        await self._start_world()
        self._register_cute_relax()
        logger.info(
            "[continuity] service started "
            f"enable={int(cfg.enable)} items={int(cfg.items_active)} topics={int(cfg.topics_active)} "
            f"closure={int(cfg.closure_active)} batching={int(cfg.batching_active)} "
            f"channel={int(cfg.channel_active)} digest={int(cfg.digest_active)}"
        )

    def _init_lifelike_initiative(self, store: Any, clock: Callable[[], float]) -> None:
        try:
            from ..lifelike.care.bridge import CareBridge
            from ..lifelike.initiative.coordinator import InitiativeCoordinator

            coord = InitiativeCoordinator(
                store,
                self._config_get,
                owner_private=self.is_owner_scope,
                budget=self.cheap_budget,
                clock=clock,
            )
            care = CareBridge(
                store,
                self._config_get,
                pool=coord.pool,
                owner_ids=self.owner_ids,
                observe_chat_ids=lambda: self.cfg.observe_chat_ids,
                owner_channel_ids=lambda: self.cfg.owner_channel_ids,
                id_whitelist=self._id_whitelist,
                initiative_mode=coord.mode,
                llm_generate=self._llm_care,
                budget=self.cheap_budget,
                usage_probe=self._llm_usage_probe,
                clock=clock,
            )
            coord.care = care
            coord.relation = getattr(self, "relation", None)  # U2：关系模式接入主动闸门/联动点
        except Exception as e:
            logger.warning(f"[lifelike.initiative] init failed, falling back to items only: {type(e).__name__}")
            return
        self.initiative = coord
        self.care = care
        self.bridge.initiative = coord
        self.assembler.lifelike_sources.append(("initiative_focus", self._lifelike_focus))
        self.assembler.lifelike_sources.append(("initiative_passive", self._lifelike_passive))

    async def _lifelike_focus(
        self, scope_key: str, purpose: str, owner_private: bool, focus_item_id: str | None
    ) -> str | None:
        if self.initiative is None or purpose != PURPOSE_PROACTIVE:
            return None
        return await self.initiative.focus_section(scope_key, focus_item_id, owner_private)

    async def _lifelike_passive(
        self, scope_key: str, purpose: str, owner_private: bool, focus_item_id: str | None
    ) -> str | None:
        if self.initiative is None or purpose == PURPOSE_PROACTIVE:
            return None
        return await self.initiative.passive_section(scope_key, owner_private)

    def _id_whitelist(self) -> list[str] | None:
        """AstrBot 平台 id_whitelist；未启用或为空（= 全部允许）→ None"""
        try:
            conf = self._context.get_config()
            ps = conf.get("platform_settings", {}) or {}
            if not ps.get("enable_id_white_list", True):
                return None
            wl = ps.get("id_whitelist") or []
            return [str(x) for x in wl] if wl else None
        except Exception:
            return None

    async def _llm_care(self, prompt: str, timeout: float) -> str:
        if self._llm_manager is None:
            raise RuntimeError("llm_manager unavailable")
        return await self._llm_manager.generate(
            prompt, module="lifelike_care", provider_id=self.background_provider_id(), timeout=timeout
        )

    def _start_care(self, cfg: ContinuityConfig) -> None:
        care = self.care
        if care is None or care.running or not cfg.channel_active:
            return
        try:
            if care.mode() == "off":
                return
            care.start()
        except Exception as e:
            logger.warning(f"[lifelike.care] start failed: {type(e).__name__}")

    def _start_background(self, cfg: ContinuityConfig) -> None:
        # lifelike P0-2：适配器失联心跳告警，独立于 continuity.enable / lifelike_enable
        self._start_watchdog()
        # 活人感 P1-12：关心桥每 10 分钟一轮（依赖频道/群采集；off 时不启动）
        self._start_care(cfg)
        # 主人私聊离线补采属于连续性本身，不依赖 channel_ingest_enable
        if cfg.enable and self._owner_catchup is None:
            try:
                self._owner_catchup = OwnerCatchUp(
                    self._platform_inst,
                    self.owner_ids,
                    self.store.get_watermark,
                    self.catch_up_owner_messages,
                    self._clock,
                )
                self._owner_catchup.start()
            except Exception as e:
                self._owner_catchup = None
                logger.warning(f"[continuity] owner catch-up start failed: {type(e).__name__}")
        if cfg.channel_active and self._ingestor is None:
            try:
                dynamic: dict[str, Any] = {}
                if cfg.observe_whitelist:
                    # U3：白名单群动态纳入；只经白名单进来的群做一次 ≤30 天的分批历史回补
                    dynamic = {
                        "chat_ids_get": lambda: self.cfg.observe_chat_ids,
                        "history_chats_get": lambda: set(self.cfg.observe_chat_ids)
                        - set(self.cfg.explicit_observe_ids),
                    }
                self._ingestor = ChannelIngestor(
                    self.store,
                    self._platform_inst,
                    cfg.observe_chat_ids,
                    self.owner_ids(),
                    cfg.owner_channel_ids,
                    self._clock,
                    **dynamic,
                )
                self._ingestor.start()
            except Exception as e:
                self._ingestor = None
                logger.warning(f"[continuity] channel ingest start failed: {type(e).__name__}")
        if cfg.digest_active and self._digest is None and cfg.explicit_observe_ids:
            owners = sorted(self.owner_ids())
            if not owners:
                logger.info("[continuity] digest not started: no owner configured")
                return
            try:
                self._digest = DigestTask(
                    self.store,
                    self._llm_digest,
                    f"private:{owners[0]}",
                    cfg.explicit_observe_ids,  # 近期动态只用显式采集群（白名单通配群不进 digest）
                    cfg.digest_interval_hours,
                    self._clock,
                    owner_channel_ids=cfg.owner_channel_ids,
                )
                self._digest.start()
            except Exception as e:
                self._digest = None
                logger.warning(f"[continuity] digest start failed: {type(e).__name__}")

    def _start_watchdog(self) -> None:
        if self._watchdog is not None:
            return
        try:
            wd = AdapterWatchdog(
                self._platform_inst,
                self._hidden_get,
                (self._data_dir / HEALTH_DIRNAME) if self._data_dir is not None else None,
                self._bump_counter,
                self._clock,
            )
            if not wd.enabled():
                return
            wd.start()
            self._watchdog = wd
        except Exception as e:
            self._watchdog = None
            logger.warning(f"[lifelike.watchdog] start failed: {type(e).__name__}")

    def _hidden_get(self, key: str, default: Any) -> Any:
        getter = getattr(self._config_source, "get", None)
        if getter is None:
            return default
        value = getter(key, default)
        return default if value is None else value

    async def _bump_counter(self, name: str) -> None:
        if not self._open:
            return
        await asyncio.to_thread(self.store.bump_counter, name)

    async def stop(self) -> None:
        self._unregister_cute_relax()
        for name, comp in (
            ("worker", self.worker),
            ("watchdog", self._watchdog),
            ("session_extractor", self.session_extractor),
            ("relation", getattr(self, "relation", None)),
            ("owner_catchup", self._owner_catchup),
            ("ingestor", self._ingestor),
            ("digest", self._digest),
            ("care", self.care),
            ("world", getattr(self, "world", None)),
        ):
            if comp is None:
                continue
            try:
                await comp.stop()
            except Exception as e:
                logger.warning(f"[continuity] stop {name} failed: {type(e).__name__}")
        self._ingestor = None
        self._owner_catchup = None
        self._digest = None
        self._watchdog = None
        if self._open:
            self._open = False
            try:
                await asyncio.to_thread(self.store.close)
            except Exception as e:
                logger.warning(f"[continuity] store close failed: {type(e).__name__}")

    def _platform_inst(self) -> Any:
        pid = DEFAULT_PLATFORM_ID
        if self._platform_id_get is not None:
            try:
                pid = self._platform_id_get() or DEFAULT_PLATFORM_ID
            except Exception:
                pid = DEFAULT_PLATFORM_ID
        return self._context.get_platform_inst(pid)

    # ------------------------------------------------------------ LLM adapters

    def background_provider_id(self) -> str | None:
        """后台任务（抽取/摘要/识别）没有消息事件可推导 provider：沿用现网 L1 总结已在用的
        provider（``l1_buffer.summary_provider``），不切换、不新增模型。"""
        try:
            getter = getattr(self._config_source, "get", None)
            pid = getter("l1_buffer.summary_provider", None) if getter is not None else None
        except Exception:
            pid = None
        pid = str(pid or "").strip()
        return pid or None

    async def _llm_extract(self, prompt: str, timeout: float) -> str:
        if self._llm_manager is None:
            raise RuntimeError("llm_manager unavailable")
        return await self._llm_manager.generate(
            prompt, module="continuity_extract", provider_id=self.background_provider_id(), timeout=timeout
        )

    def _config_get(self, key: str, default: Any = None) -> Any:
        """读 Iris 配置平铺键（HiddenConfig 单段名）；失败返回 default"""
        try:
            getter = getattr(self._config_source, "get", None)
            val = getter(key, default) if getter is not None else default
        except Exception:
            val = default
        return default if val is None else val

    async def _llm_session_extract(self, prompt: str, timeout: float) -> str:
        if self._llm_manager is None:
            raise RuntimeError("llm_manager unavailable")
        return await self._llm_manager.generate(
            prompt, module=SESSION_EXTRACT_MODULE, provider_id=self.background_provider_id(), timeout=timeout
        )

    async def _llm_usage_probe(self, module: str) -> int | None:
        """Iris token_stats 中该模块累计 token（输入+输出）；拿不到返回 None"""
        getter = getattr(self._llm_manager, "get_token_stats", None)
        if getter is None:
            return None
        stats = await getter(module)
        if not isinstance(stats, Mapping):
            return None
        return int(stats.get("total_input_tokens") or 0) + int(stats.get("total_output_tokens") or 0)

    async def _llm_relation(self, prompt: str, timeout: float) -> str:
        if self._llm_manager is None:
            raise RuntimeError("llm_manager unavailable")
        from ..lifelike.relation.models import MODULE as RELATION_MODULE

        return await self._llm_manager.generate(
            prompt, module=RELATION_MODULE, provider_id=self.background_provider_id(), timeout=timeout
        )

    async def _relation_section(self, scope_key: str, purpose: str = PURPOSE_REPLY) -> str | None:
        """<你们之间>：只有 relation_mode=on 时返回文字；off/shadow/异常返回 None（调用方已限定主人私聊）"""
        relation = getattr(self, "relation", None)
        if relation is None:
            return None
        return await relation.render(scope_key, purpose)

    def _cute_relax(self, scope: str) -> bool:
        """P1-11 卖萌两格放宽回调（同步、只读进程内缓存）：只认主人私聊；relation/bond 非 on → False。"""
        relation = getattr(self, "relation", None)
        if relation is None or not self.is_owner_scope(scope):
            return False
        return relation.cute_relaxed(str(scope))

    def _register_cute_relax(self) -> None:
        try:
            from ..style import selector as style_selector

            style_selector.set_cute_relax_provider(self._cute_relax)
        except Exception as e:
            logger.warning(f"[lifelike.relation] cute link register failed: {type(e).__name__}")

    def _unregister_cute_relax(self) -> None:
        try:
            from ..style import selector as style_selector

            if style_selector.get_cute_relax_provider() == self._cute_relax:
                style_selector.set_cute_relax_provider(None)
        except Exception:
            pass

    def relation_active(self) -> bool:
        relation = getattr(self, "relation", None)
        try:
            return relation is not None and relation.mode() != "off"
        except Exception:
            return False

    @_guard(None)
    async def note_owner_group_message(self, sender_id: Any, chat_id: Any, msg_id: Any, text: str) -> None:
        """主人本人在群里的发言：只用于关系模块"生病/情绪差/危机 → 立即解除冷静期"，不存原文。"""
        if not self._open or not self.relation_active():
            return
        scope = self.owner_scope(sender_id)
        if not scope or chat_id in (None, "") or msg_id is None:
            return
        if not self._owner_group_allowed(chat_id):
            return  # 只看已授权采集的群（审查 P6）：未授权群的发言不进关系库
        await self.relation.on_owner_group_message(scope, chat_id, msg_id, str(text or ""))

    def _owner_group_allowed(self, chat_id: Any) -> bool:
        """关心桥授权群（observe_chat_ids ∩ id_whitelist，去掉主人频道）；关心桥不可用时退回 observe_chat_ids。
        异常 → False（不记录）。"""
        try:
            from ..lifelike.care.rules import chat_variants

            variants = chat_variants(chat_id)
            care = getattr(self, "care", None)
            if care is not None:
                allowed = care.authorized_groups()
            else:
                allowed = set(self.cfg.observe_chat_ids or ())
            return any(str(g) in variants for g in allowed)
        except Exception:
            return False

    @_guard("")
    async def relation_command(self, scope_key: str, raw: str, msg_id: Any) -> str:
        if not self._open:
            return "连续性账本没有打开。"
        if not self.is_owner_scope(scope_key):
            return "这个命令只在主人私聊里用。"
        from ..lifelike.relation.commands import handle_rel_command

        return await handle_rel_command(self.relation, scope_key, raw, msg_id)

    async def _start_session_extract(self) -> None:
        if self.session_extractor.mode() == "off" or self.session_extractor.running:
            return
        scopes = [f"private:{o}" for o in sorted(self.owner_ids())]
        await self.session_extractor.start(scopes)

    async def _session_closure(self, scope_key: str, state: Any) -> None:
        if state == STATE_ENDED and self.cfg.items_active:
            await self.session_extractor.on_closure(scope_key)

    async def _llm_world(self, prompt: str, timeout: float) -> str:
        """生活系统廉价调用（DayPlanner / ClaimAuditor）；模块名由 CheapBudget 计数区分，
        token_stats 统一记在 lifelike_world 下。"""
        if self._llm_manager is None:
            raise RuntimeError("llm_manager unavailable")
        return await self._llm_manager.generate(
            prompt, module=WORLD_MODULE, provider_id=self.background_provider_id(), timeout=timeout
        )

    async def _start_world(self) -> None:
        try:
            await self.world.ensure_started()
        except Exception as e:
            logger.warning(f"[lifelike.world] start failed: {type(e).__name__}")

    async def _world_render(self, scope_key: str, *, owner_private: bool, now: float | None = None) -> str:
        return await self.world.render(scope_key, owner_private=owner_private, now=now)

    async def _after_proactive_sent(self, scope_key: str, outbound_id: Any, text: str) -> None:
        """主动消息拿到回执后的接入点（设计 §9.10 修订：主动不经 on_message_sent）"""
        self.world.schedule_audit(scope_key, outbound_id, text, "proactive")

    async def _llm_digest(self, prompt: str, timeout: float) -> str:
        if self._llm_manager is None:
            raise RuntimeError("llm_manager unavailable")
        return await self._llm_manager.generate(
            prompt, module="continuity_digest", provider_id=self.background_provider_id(), timeout=timeout
        )

    # ------------------------------------------------------------ inbound (owner private)

    @_guard(False)
    async def note_owner_message(
        self,
        scope_key: str,
        msg_id: Any,
        msg_ts: Any,
        *,
        text: str = "",
        sender_id: Any = None,
        reply_to_msg_id: Any = None,
        has_media: bool = False,
        media_only: bool = False,
    ) -> bool:
        """主人私聊新消息：推进水位（总开关关闭也推进，暂停恢复依赖它）；
        事项收件箱与收尾只在对应开关打开时处理。返回水位是否真实推进（重放 → False）。
        调用方在返回 True 时负责 ``state.resume_after_owner_message``。"""
        if not self._open or not self.is_owner_scope(scope_key):
            return False
        mid = int(msg_id)
        ts = float(msg_ts)
        relation_on = self.relation_active()
        prev_ts = None
        if relation_on:
            try:
                wm = await asyncio.to_thread(self.store.get_watermark, scope_key)
                prev_ts = float(wm["last_msg_ts"]) if wm else None
            except Exception:
                prev_ts = None
        advanced, _ = await asyncio.to_thread(self.store.advance_watermark, scope_key, mid, ts)
        cfg = self.cfg
        body = str(text or "").strip()
        is_command = body.startswith("/")
        if relation_on and advanced and body and not is_command and not media_only:
            await self.relation.on_owner_message(scope_key, mid, ts, body, prev_ts)
        if advanced and cfg.closure_active and not is_command:
            state = await asyncio.to_thread(
                self.closure.on_owner_message, scope_key, body, has_media=has_media
            )
            await self._session_closure(scope_key, state)
        if cfg.items_active and body and not is_command and not media_only:
            await self._record_inbound(scope_key, mid, sender_id, body, ts, reply_to_msg_id)
        if advanced and self.initiative is not None and not is_command:
            # 活人感 P1-8：归因结算 / G-busy；P1-12：一句话关闭、误判收场（开关 off 时不做任何事）
            try:
                await self.initiative.on_owner_message(scope_key, ts, body)
            except Exception as e:
                logger.warning(f"[lifelike.initiative] owner message hook failed: {type(e).__name__}")
        return bool(advanced)

    @_guard(False)
    async def catch_up_owner_messages(self, scope_key: str, messages: list[dict[str, Any]]) -> bool:
        """离线补采到的主人私聊消息：逐条走 note_owner_message（推进水位、记入收件箱、
        收尾），不生成任何回复；有任意一条真实推进水位时调用一次 resume_cb。"""
        if not self._open or not self.is_owner_scope(scope_key):
            return False
        advanced_any = False
        for msg in sorted(messages or (), key=lambda d: int(d.get("msg_id") or 0)):
            advanced = await self.note_owner_message(
                scope_key,
                msg["msg_id"],
                msg["msg_ts"],
                text=msg.get("text") or "",
                sender_id=msg.get("sender_id"),
                reply_to_msg_id=msg.get("reply_to_msg_id"),
                has_media=bool(msg.get("has_media")),
                media_only=bool(msg.get("media_only")),
            )
            advanced_any = advanced_any or bool(advanced)
        if advanced_any and self._resume_cb is not None:
            try:
                res = self._resume_cb(scope_key)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                logger.warning(f"[continuity] resume after catch-up failed: {type(e).__name__}")
        if advanced_any:
            logger.info(f"[continuity] owner catch-up scope={scope_key} advanced watermark")
        return advanced_any

    async def _record_inbound(
        self, scope_key: str, mid: int, sender_id: Any, body: str, ts: float, reply_to: Any
    ) -> None:
        try:
            reply = int(reply_to) if reply_to not in (None, "", 0) else None
        except (TypeError, ValueError):
            reply = None
        sender = str(sender_id or scope_key.split(":", 1)[1])
        try:
            fresh = await asyncio.to_thread(
                self.store.record_inbound, scope_key, mid, sender, body, ts, reply
            )
        except Exception as e:
            logger.warning(f"[continuity] record_inbound scope={scope_key} failed: {type(e).__name__}")
            return
        if not fresh:
            return
        if not self.worker.running:
            await self.worker.start()
        self.worker.notify(scope_key)
        await self.session_extractor.note_inbound(scope_key)

    # ------------------------------------------------------------ batching / closure

    async def gate_batch(self, scope_key: str, event: Any) -> str:
        """合批门：返回 'pass' / 'stop'（stop 时已 stop_event）。关闭或故障 → pass"""
        try:
            if not self.cfg.batching_active or not self.is_owner_scope(scope_key):
                return PASS
            return await self.batcher.gate(scope_key, _EventView(event))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[continuity] gate_batch scope={scope_key} failed: {type(e).__name__}")
            return PASS

    @_guard(None)
    async def closure_hint(self, scope_key: str) -> str | None:
        if not self._open or not self.cfg.closure_active or not self.is_owner_scope(scope_key):
            return None
        return await asyncio.to_thread(self.closure.hint_for_reply, scope_key)

    # ------------------------------------------------------------ context

    @_guard("")
    async def assemble(
        self,
        scope_key: str,
        *,
        purpose: str = PURPOSE_REPLY,
        sender_id: Any = None,
        focus_item_id: str | None = None,
    ) -> str:
        if not self._open or not scope_key:
            return ""
        sid = None if sender_id in (None, "") else str(sender_id)
        if not self.cfg.items_active:
            return await self._private_only(scope_key, purpose, sid)
        return await self.assembler.assemble(
            scope_key, purpose=purpose, sender_id=sid, focus_item_id=focus_item_id
        )

    async def _private_only(self, scope_key: str, purpose: str, sender_id: str | None) -> str:
        """软回滚：只保留私聊专属人格层（主动时另附画像），不注入事项/出站/收尾"""
        proactive = purpose == PURPOSE_PROACTIVE
        if not self.assembler.is_owner_private(scope_key, None if proactive else sender_id):
            return ""
        sections: list[str] = []
        sources = [("private_context", self.assembler._private_section(scope_key))]
        if self.relation_active():
            sources.append(("relation", self._relation_section(scope_key, purpose)))
        if proactive:
            sources.append(("profile", self.assembler._profile_section(scope_key)))
        for name, src in sources:
            try:
                text = await src
            except Exception as e:
                logger.warning(f"[continuity] private-only context failed: {type(e).__name__}")
                continue
            if text:
                text = guard_numeric(text, source=f"continuity.{name}", strict=name == "profile")
            if text:
                sections.append(text)
        return "\n".join([OPEN_TAG, *sections, CLOSE_TAG]) if sections else ""

    async def _speech_context(self, scope_key: str, focus_item_id: str | None) -> str:
        return await self.assemble(scope_key, purpose=PURPOSE_PROACTIVE, focus_item_id=focus_item_id)

    # ------------------------------------------------------------ outbound bookkeeping

    @_guard(None)
    async def record_reply_sent(
        self,
        scope_key: str,
        text: str,
        tg_msg_ids: Iterable[Any],
        *,
        receipt_supported: bool = True,
        reply_to_msg_id: Any = None,
    ) -> dict | None:
        """被动回复记账（总开关关闭也记）；有回执且为主人私聊时再做承诺检测与收尾"""
        if not self._open or not scope_key:
            return None
        ids: list[int] = []
        for v in tg_msg_ids or ():
            try:
                ids.append(int(v))
            except (TypeError, ValueError):
                continue
        try:
            reply_to = int(reply_to_msg_id) if reply_to_msg_id not in (None, "") else None
        except (TypeError, ValueError):
            reply_to = None
        turn_id = f"reply:{reply_to}" if reply_to is not None else None
        text = str(text or "")

        def _record() -> tuple[dict | None, bool]:
            replay = any(self.store.outbound_by_tg_id(scope_key, i) is not None for i in ids)
            row = self.bridge.ledger.record_reply(
                scope_key,
                text,
                ids,
                turn_id=turn_id,
                reply_to_msg_id=reply_to,
                receipt_supported=receipt_supported,
            )
            return row, replay

        row, replay = await asyncio.to_thread(_record)
        if not ids or replay or not self.is_owner_scope(scope_key):
            return row
        cfg = self.cfg
        if cfg.items_active:
            owner = scope_key.split(":", 1)[1]
            await asyncio.to_thread(
                record_bot_promise, self.store, scope_key, owner, text, ids, float(self._clock())
            )
        if cfg.closure_active:
            state = await asyncio.to_thread(self.closure.on_bot_sent, scope_key, text)
            await self._session_closure(scope_key, state)
        if row is not None and row.get("status") == m.OUT_SENT:
            # 活人感 P1-5：回复发出后核对自述（后台；mode=off 时直接返回）
            self.world.schedule_audit(scope_key, row.get("id"), text, "reply")
        return row

    @_guard(None)
    async def record_manual(self, scope_key: str, tg_msg_id: Any, text: str) -> dict | None:
        if not self._open or not scope_key:
            return None
        return await asyncio.to_thread(self.bridge.ledger.record_manual, scope_key, tg_msg_id, text or "")

    # ------------------------------------------------------------ windows / maintenance

    @_guard(0)
    async def save_windows(self, sliding_window: Any, whitelist: Iterable[str]) -> int:
        if not self._open:
            return 0
        saved = await self.bridge.save_windows(sliding_window, whitelist)
        await self._maintenance()
        return saved

    @_guard(None)
    async def run_acceptance_probe(self) -> dict | None:
        """运维验收探针：宿主机 root 写入 ``continuity/acceptance_probe.json``
        （``{"text": "【验收】..."}``）后，在下一次周期保存时向**主人私聊**发送一条可识别消息。

        - 目标固定为第一个主人私聊（不可指定），并经 bridge.check_target 硬拦截；
        - 正文必须以【验收】开头且 ≤200 字；每天最多 3 条；文件读到即删除（一次性）；
        - 走带回执的发送路径并记出站账本（origin=probe），不计主动额度、不影响暂停。
        结果写入 ``continuity/acceptance_probe.result.json`` 供核对。
        """
        if not self._open:
            return None
        base = self.store.db_path.parent
        path = base / PROBE_FILENAME
        if not path.exists():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
        finally:
            try:
                path.unlink()
            except OSError:
                pass
        result: dict[str, Any] = {"at": self._clock(), "status": "refused", "reason": ""}
        try:
            text = str((json.loads(raw) or {}).get("text", "")).strip()
        except Exception:
            text = ""
        owners = sorted(self.owner_ids())
        counters = await asyncio.to_thread(self.store.get_counters)
        if not text.startswith(PROBE_PREFIX) or len(text) > PROBE_MAX_CHARS:
            result["reason"] = "bad_text"
        elif not owners:
            result["reason"] = "no_owner"
        elif int(counters.get(PROBE_COUNTER, 0)) >= PROBE_DAILY_MAX:
            result["reason"] = "daily_limit"
        else:
            platform = self._platform_inst()
            pid = getattr(getattr(platform, "meta", lambda: None)(), "id", None) or DEFAULT_PLATFORM_ID
            scope = f"private:{owners[0]}"
            umo = f"{pid}:FriendMessage:{owners[0]}"
            reason = self.bridge.check_target(scope, umo)
            if reason:
                result["reason"] = reason
            else:
                await asyncio.to_thread(self.store.bump_counter, PROBE_COUNTER)
                ledger = self.bridge.ledger
                oid = await ledger.create_draft(scope, text, turn_id=None, part_index=0, origin="probe")
                outcome = await ledger.deliver(oid, scope, platform, umo, text)
                result.update(
                    status=outcome.status,
                    reason=getattr(outcome, "error", "") or "",
                    outbound_id=oid,
                    tg_msg_ids=list(getattr(outcome, "tg_msg_ids", []) or []),
                )
        logger.info(
            f"[continuity] acceptance probe status={result['status']} reason={result['reason'] or '-'} "
            f"ids={result.get('tg_msg_ids') or '-'}"
        )
        try:
            (base / PROBE_RESULT_FILENAME).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return result

    @_guard(0)
    async def restore_windows(self, sliding_window: Any) -> int:
        if not self._open:
            return 0
        return await self.bridge.restore_windows(sliding_window)

    async def _maintenance(self) -> None:
        now = float(self._clock())
        if now - self._last_maintenance < MAINTENANCE_INTERVAL_S:
            return
        self._last_maintenance = now
        try:
            n = await asyncio.to_thread(self.store.expire_items, now)
            if n:
                logger.info(f"[continuity] expired items={n}")
        except Exception as e:
            logger.warning(f"[continuity] expire_items failed: {type(e).__name__}")
        await self._retention_observed(now)
        cfg = self.cfg
        if cfg.items_active and not self.worker.running:
            await self.worker.start()
        if cfg.items_active:
            try:
                await self._start_session_extract()  # 运行时切到 shadow/on 后在维护周期启动
            except Exception as e:
                logger.warning(f"[continuity] session extract start failed: {type(e).__name__}")
        if self.relation_active():
            await self.relation.maintenance([f"private:{o}" for o in sorted(self.owner_ids())], now)
        self._start_background(cfg)
        if self.lifelike_memory is not None:
            try:
                await self.lifelike_memory.maintenance()
            except Exception as e:
                logger.warning(f"[lifelike.memory] maintenance failed: {type(e).__name__}")
        await self._start_world()  # 运行时切到 shadow/on 后在维护周期启动

    # ------------------------------------------------------------ retention (lifelike P0-3)

    def _hidden(self, key: str, default: Any) -> Any:
        src = self._config_source
        if src is None:
            return default
        try:
            if isinstance(src, Mapping):
                return src.get(key, default)
            getter = getattr(src, "get", None)
            if getter is None:
                return getattr(src, key, default)
            return getter(key, default)
        except Exception:
            return default

    async def _retention_observed(self, now: float, force: bool = False) -> dict | None:
        """群友原文（observed_posts.author_kind=other）超期清理；dry-run 只计数。

        日志只记行数与天数，不记正文。返回 ``{"days","dry_run","count"}``；跳过/失败返回 None。
        """
        if not force and now - self._last_retention < RETENTION_INTERVAL_S:
            return None
        self._last_retention = now
        days = _to_int(self._hidden(RETENTION_DAYS_KEY, RETENTION_DAYS_DEFAULT), RETENTION_DAYS_DEFAULT, 0, RETENTION_DAYS_MAX)
        if days <= 0:
            return None
        dry_run = _to_bool(self._hidden(RETENTION_DRY_RUN_KEY, True), True)
        cutoff = now - days * 86400.0
        try:
            if dry_run:
                n = await asyncio.to_thread(self.store.count_observed_expired, cutoff)
                logger.info(f"[lifelike.retention] observed dry_run=1 days={days} would_delete={n}")
                counter = "lifelike_retention_would_delete"
            else:
                n = await asyncio.to_thread(self.store.purge_observed, cutoff)
                logger.info(f"[lifelike.retention] observed dry_run=0 days={days} deleted={n}")
                counter = "lifelike_retention_deleted"
            try:
                await asyncio.to_thread(self.store.bump_counter, counter, int(n))
            except Exception as e:
                logger.debug(f"[lifelike.retention] counter failed: {type(e).__name__}")
            return {"days": days, "dry_run": dry_run, "count": int(n)}
        except Exception as e:
            logger.warning(f"[lifelike.retention] observed purge failed: {type(e).__name__}")
            return None
