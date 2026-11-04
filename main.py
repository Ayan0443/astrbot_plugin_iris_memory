"""
Iris Memory - AstrBot 轻量化三合一插件

v3.0 架构：
- 记忆侧（源自 Iris Chat Memory 轻量方案）：
  L1 消息上下文缓冲 / L2 记忆库（FAISS + SQLite）/ L3 知识图谱（SQLite）
  + 用户/群聊画像 + 梦境离线加工 + 图片解析
- 主动回复侧（源自 Iris Reply 统一决策模型）：
  chime_in 跟话 / follow_up 跟进 / initiate 发起 / watch 被动评估，
  SignalGate 本地零成本门控 + 单次 LLM 统一决策 + ThreadAnchor 记账
- 人格自学习迭代：
  脱敏采样 + 风格分析 + 候选生成 + 独立审查 + Revision 审批/回滚

钩子编排（等价于原两插件并存时的兼容性契约）：
  群消息 → on_message（主动回复门控，设 iris_mode extra）
        → on_all_message（入 L1、图片入队）
  门控命中 → 统一决策（llm_generate 直调，不触发钩子）
        → 决策发言：劫持主管线，注入 SPEAK_HINTS（mark_as_temp）
  on_llm_request → 记忆侧：清空 contexts，注入 L1/L2/L3/画像（mark_as_temp）
  on_llm_response → 记忆侧：bot 回复入 L1 → 主动回复侧：按 iris_mode 记账
  after_message_sent → 主动回复侧：入滑动窗口 + 写 ThreadAnchor
  initiate 直发（context.send_message）→ 手动记账 + 回填 L1
"""

import asyncio
import hashlib
import re
import time
from datetime import datetime
from typing import Any, Optional

from .iris_memory.config import init_config, Config

from astrbot.api import AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from pathlib import Path

from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart
from astrbot.core.provider.entities import LLMResponse, ProviderRequest

from .iris_memory.core import (
    ComponentManager,
    get_logger,
    create_components,
    initialize_components,
    shutdown_components,
    handle_user_message,
    preprocess_llm_request,
    handle_llm_response,
    handle_agent_done,
    handle_pre_request_cleanup,
    handle_initiate_backfill,
    set_component_manager,
    get_run_log_manager,
)
from .iris_memory.tools import register_llm_tools
from .iris_memory.stickers.library import (
    DEFAULT_BLOCKED_SCOPES,
    StickerLibrary,
    StickerSendError,
    has_marker,
    marker_raw_labels,
    parse_marker,
    prompt_block,
    take_tiezhi_marker,
)
from .iris_memory.stickers import learn as _stl
from .iris_memory.stickers.store import StickerStore
from .iris_memory.stickers.understanding import (
    StickerUnderstanding,
    extract_photo_meta,
    extract_sticker_meta,
    frame_getter_from_event,
    make_describe_image,
    rewrite_event,
)
from .iris_memory.utils.token_counter import warm_up_encoders_async
from .iris_memory.web import register_all_routes
from .iris_memory.llm import LLMManager
from .iris_memory.llm_modules import FRAMEWORK_REPLY, proactive_reply_module
from .iris_memory.commands import (
    get_registry,
    execute_command,
    L1CommandHandler,
    L2CommandHandler,
    L3CommandHandler,
    ProfileCommandHandler,
    AllCommandHandler,
    LearningCommandHandler,
    EvolutionCommandHandler,
    BeliefsCommandHandler,
    ForgetCommandHandler,
)
from .iris_memory.proactive.admin import AdminCommands
from .iris_memory.proactive.api import (
    register_web_apis as register_reply_web_apis,
    sync_stats_group_state,
)
from .iris_memory.proactive.config import ConfigManager as ReplyConfigManager
from .iris_memory.proactive.decision import (
    INPUT_SAFETY_COOLDOWN_MINUTES,
    DecisionCore,
    DecisionRequest,
    SafetyCleanupResult,
)
from .iris_memory.proactive.perception import (
    ContextPackager,
    Gatekeeper,
    SlidingWindow,
    WindowMessage,
)
from .iris_memory.proactive.prompts import (
    GROUP_CONTEXT_NOTE,
    GROUP_SPEAK_HINT,
    SPEAK_HINTS,
)
from .iris_memory.proactive.proactive import ProactiveEngine
from .iris_memory.proactive import freedom as _fr
from .iris_memory.proactive import group_freedom as _gfm
from .iris_memory.proactive import hostility as _hs
from .iris_memory.proactive.prompts import (
    COUNTER_DEFEND_LINE,
    COUNTER_LAST_ROUND_LINE,
    COUNTER_NEUTRAL_HINT,
    COUNTER_SPEAK_HINT,
    GF_NEUTRAL_PROMPT,
    OWNER_HARSH_HINT,
)
from .iris_memory.lifelike.members import nickname as _nk
from .iris_memory.style.casual import casualize as _casualize
from .iris_memory.proactive.signals import SignalGate
from .iris_memory.proactive.state import GroupState, StateManager
from .iris_memory.proactive.stats import StatsCollector
from .iris_memory.proactive.tickets import DecisionTicketRegistry
from .iris_memory.proactive.time_hint import resolve_datetime_reminder
from .iris_memory.proactive.tools import ToolContext
from .iris_memory.extras import ErrorFriendlyProcessor, MarkdownStripper
from .iris_memory.lifelike.care import commands as care_commands
from .iris_memory.lifelike.care.rules import strip_private_mentions
from .iris_memory.lifelike.style.audit import (
    CHANNEL_PASSIVE as _STYLE_PASSIVE,
    CHANNEL_PROACTIVE as _STYLE_PROACTIVE,
    StyleAuditor,
)
from .iris_memory.continuity.service import (
    ContinuityConfig,
    ContinuityService,
    base_chat_id,
    event_has_media,
    is_silent_or_forbidden,
)
from .iris_memory.core.bypass_tools import BypassToolStripper
from .iris_memory.core.llm_response_hook import (
    PENDING_ASSISTANT_EXTRA,
    commit_pending_assistant,
    commit_pending_learning,
)

logger = get_logger("main")

PLUGIN_NAME = "astrbot_plugin_iris_memory"

# 旧版（v2.x）数据自动迁移开关；v4 删除本常量与 iris_memory/legacy_migration/ 即彻底移除
LEGACY_MIGRATION_ENABLED = True

_IRIS_ACTIVE_TIMEOUT = 120
# 已认领但迟迟没进入决策的群（事件在 on_llm_request 之前被别处 stop）：
# 下一条消息到来时就地释放，而不是干等周期清理。
_TRIGGER_UNDECIDED_TIMEOUT = 45
_GROUP_HINT_WINDOW = 12
# 活人感 U9：去抖等待中的事件标记（group_id, seq, sender_id）与资格缓存秒数
_GF_WAIT_EXTRA = "iris_gf_wait"
_GF_ELIGIBLE_TTL = 60.0
# 决定开口后多久还没有 LLM 响应就释放"回复进行中"（秒；主模型带工具时可能较慢，取 60）
_GF_REPLY_TIMEOUT = 60.0
_UMO_KV_KEY = "iris_reply:group_umo"

# 连续性（DESIGN §6）：Telethon 补丁在真实发出后写入的回执 extra
_RECEIPT_PLATFORM = "telethon_userbot"
_SENT_IDS_EXTRA = "telethon_sent_message_ids"
# U19：同一会话两张贴纸的最小间隔（只作防故障底线；use 开关 off 时仍是库默认的 120 秒）
_STICKER_FLOOR_SECONDS = 60.0
# U19 审查 4：私聊里她连续两条回复都带了贴纸，下一条不发（防故障频率底线，不是使用规则）
_STICKER_STREAK_MAX = 2
_REPLY_TEXT_EXTRA = "iris_continuity_reply"

_PASSIVE_WATCH_SIGNAL = re.compile(
    r"[?？]|"
    r"我(?:会|将|之后|稍后|晚点|等会|回头)|"
    r"(?:下次|之后|稍后|晚点|有结果|有消息)(?:再|会)?(?:告诉|提醒|联系|确认|回复)|"
    r"(?:等|等待).{0,16}(?:回复|消息|结果)|"
    r"\b(?:i(?:'ll| will)|follow up|let you know|remind|check back|waiting for)\b",
    re.IGNORECASE,
)


def _detect_passive_trigger(event: AstrMessageEvent, req, context: Context) -> None:
    """检测 LLM 请求是否为被动触发（sampling/主动回复）

    当用户消息不以唤醒前缀开头且未 @机器人 时，LLM 请求可能是由 AstrBot 的
    active_reply/sampling 机制触发的。此时标记事件，供后续钩子
    判断是否跳过图片解析等高 token 消耗操作。

    注：本插件主动回复触发的请求会将 is_at_or_wake_command 置 True，
    天然不会被误判为被动触发，无需额外处理 iris_mode。
    """
    try:
        is_at_or_wake = getattr(event, "is_at_or_wake_command", False)
        if not is_at_or_wake:
            event.set_extra("iris_passive_trigger", True)
            logger.debug(
                "检测到被动触发（sampling/主动回复），is_at_or_wake_command 为 False"
            )
    except Exception as e:
        logger.debug(f"被动触发检测异常（不影响正常流程）：{e}")


def _is_pure_at_self(event: AstrMessageEvent) -> bool:
    """Return whether the message consists only of an @ mention to the bot."""
    try:
        messages = event.get_messages()
        return (
            len(messages) == 1
            and isinstance(messages[0], At)
            and str(messages[0].qq) == str(event.get_self_id())
        )
    except Exception:
        return False


def _message_meta(event) -> tuple[Optional[int], float, Optional[int]]:
    """平台消息 id / 原始发送时间 / 被回复消息 id（取不到时 id 为 None、时间为当前）。"""
    msg_id: Optional[int] = None
    msg_ts: Optional[float] = None
    reply_to: Optional[int] = None
    try:
        message_obj = getattr(event, "message_obj", None)
        raw = getattr(message_obj, "raw_message", None) if message_obj is not None else None
        for cand in (
            getattr(message_obj, "message_id", None) if message_obj is not None else None,
            getattr(raw, "id", None) if raw is not None else None,
        ):
            if isinstance(cand, bool) or cand in (None, ""):
                continue
            try:
                msg_id = int(cand)
                break
            except (TypeError, ValueError):
                continue
        date = getattr(raw, "date", None) if raw is not None else None
        if isinstance(date, datetime):
            msg_ts = float(date.timestamp())
        if msg_ts is None and message_obj is not None:
            stamp = getattr(message_obj, "timestamp", None)
            if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and stamp > 0:
                msg_ts = float(stamp)
        header = getattr(raw, "reply_to", None) if raw is not None else None
        rid = getattr(header, "reply_to_msg_id", None) if header is not None else None
        if isinstance(rid, int) and not isinstance(rid, bool) and rid > 0:
            reply_to = rid
    except Exception:
        pass
    return msg_id, (msg_ts if msg_ts is not None else time.time()), reply_to


def _int_ids(values) -> list[int]:
    out: list[int] = []
    if not isinstance(values, (list, tuple)):
        return out
    for v in values:
        if isinstance(v, bool):
            continue
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


class IrisMemoryPlugin(Star):
    """AstrBot 轻量化三合一插件主类。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.context: Context = context

        try:
            # ── 记忆侧初始化 ──
            data_dir = StarTools.get_data_dir()
            self.config: Config = init_config(config, data_dir)
            logger.info(f"插件数据目录：{data_dir}")
            self._install_identity(data_dir)
            self._install_log_redact()

            components = create_components(context, self)
            self.component_manager: Optional[ComponentManager] = ComponentManager(
                components
            )
            self._llm_manager = self.component_manager.get_component(
                "llm_manager", LLMManager
            )

            set_component_manager(self.component_manager)

            from .iris_memory.image.recorder_bridge import init_recorder_bridge

            init_recorder_bridge(context)

            # ── extras（自 v2 保留的低成本功能） ──
            self._error_processor = ErrorFriendlyProcessor(self.config)
            # 活人感 P0-1：剥离绕过 Iris 账本的内置出站工具
            self._bypass_stripper = BypassToolStripper(
                lambda: self.config.get("lifelike_strip_bypass_tools", True)
            )
            self._markdown_stripper = MarkdownStripper(
                context=self.context,
                config=self.config,
            )

            # ── 主动回复侧初始化 ──
            self._reply_config = ReplyConfigManager(
                config if config else context.get_config(),
                hidden_get=self.config.get,
            )
            self._state = StateManager(self._reply_config)
            self._tool_ctx = ToolContext()
            self._gatekeeper = Gatekeeper(self._reply_config, self._state)
            self._sliding_window = SlidingWindow(self._reply_config)
            self._context_packager = ContextPackager(
                self._reply_config, self_id_get=lambda: self._self_id
            )
            self._signals = SignalGate(self._reply_config, self._state)
            self._decision_core = DecisionCore(
                self._reply_config, self._state, self._sliding_window, self._context_packager,
                time_hint_get=lambda gid: resolve_datetime_reminder(
                    self.context, self._group_umo.get(gid),
                ),
            )
            self._admin = AdminCommands(self._state)
            self._stats = StatsCollector()
            self._reply_in_progress: dict[str, float] = {}
            self._passive_active: dict[str, float] = {}
            self._triggering: dict[str, float] = {}
            self._decision_tickets = DecisionTicketRegistry(_IRIS_ACTIVE_TIMEOUT)
            self._passive_watch_last: dict[str, float] = {}
            self._passive_watch_hash: dict[str, str] = {}
            self._follow_pending: set[str] = set()
            self._group_umo: dict[str, str] = {}
            self._umo_dirty: bool = False
            self._self_id: str = ""
            self._save_task: asyncio.Task | None = None
            self._save_interval = 30
            # 连续性服务（DESIGN §1）：构造失败时为 None，全部回到原有行为
            self._continuity: Optional[ContinuityService] = self._build_continuity()
            # StyleGuard log 模式（lifelike P0-6）：只记规则 id，不改写回复；构造失败即关闭
            self._style_auditor: Optional[StyleAuditor] = self._build_style_auditor()
            # 贴纸理解 + 主人限定表情包（DESIGN §13）：构造失败只影响贴纸功能
            self._init_stickers(data_dir)
            # 活人感 U9：白名单群自由搭话（开关关闭时所有入口都退回原有行为）
            self._gf_cache: dict[str, tuple[float, bool]] = {}
            # 被恶意对待时反击：同一人纠缠轮数（内存态）；群友称呼缓存
            self._counter_tracker = _hs.CounterTracker()
            self._gf_watchdogs: set = set()
            self._nick_cache: dict[str, tuple[float, Optional[str]]] = {}
            self._nick_seen: dict[str, str] = {}
            self._gf = _gfm.GroupFreedomHub(
                cfg_get=lambda: self._reply_config.group_freedom_config,
                eligible=self._gf_eligible,
                owner_ids_get=self._owner_id_list,
            )
            if self._continuity is not None and getattr(self._continuity, "bridge", None) is not None:
                try:
                    self._continuity.bridge.group_allow = self._gf.active
                except Exception:
                    pass
            self._proactive = ProactiveEngine(
                self.context,
                self._reply_config,
                self._state,
                self._sliding_window,
                self._signals,
                self._decision_core,
                self._stats,
                llm_manager=self._llm_manager,
                packager=self._context_packager,
                umo_get=lambda gid: self._group_umo.get(gid),
                is_busy=self._is_busy,
                self_id_get=lambda: self._self_id,
                save_fn=lambda: self._state.save_dirty(self._kv_save),
                on_initiate_sent=self._on_initiate_sent,
                text_transform=self._strip_initiate_text,
                profile_get=self._profile_text_for,
                bridge=self._continuity.bridge if self._continuity else None,
                group_freedom=self._gf,
                sticker_block=self._proactive_sticker_block,
                sticker_take=self._proactive_sticker_take,
            )

            # ── 外部注册动作（依赖就绪后在构造函数末尾统一执行） ──
            self._register_llm_tools()
            self._register_command_handlers()
            self._register_web_api()

            self._warn_empty_mention_conflict()

            logger.info("Iris Memory 整合插件已加载（等待异步初始化）")
        except Exception:
            logger.error(
                "Iris Memory 插件初始化失败，真实错误如下（框架可能将其掩盖为 "
                "'missing 1 required positional argument: config'，请以下方堆栈为准）：",
                exc_info=True,
            )
            raise

    def _warn_empty_mention_conflict(self) -> None:
        """Guide users to disable AstrBot's built-in pure-mention waiter."""
        try:
            if not self.config.get("extras.pure_at_reply.enable", False):
                return
            platform_settings = self.context.get_config().get("platform_settings", {})
            if platform_settings.get("empty_mention_waiting", True):
                logger.warning(
                    "检测到 AstrBot「只 @ 机器人触发等待」已开启：纯 @ 会先被 "
                    "AstrBot 内置处理器拦截，Iris 无法接管。若要让纯 @ 直接使用 "
                    "L1 上下文，请关闭 platform_settings.empty_mention_waiting。"
                )
        except Exception as exc:
            logger.debug(f"检查 AstrBot 纯 @ 配置失败（已忽略）：{exc}")

    async def _prepare_pure_at_request(self, event: AstrMessageEvent) -> bool:
        """Route a pure @ through the standard agent pipeline using existing L1.

        AstrBot rejects a truly empty ProviderRequest before on_llm_request hooks
        run. A whitespace-only transport prompt keeps the request alive through
        that guard; ProviderRequest.assemble_context() drops it before provider
        dispatch, so the model receives no synthetic user content and relies on
        the normal Iris L1 injection.
        """
        if not _is_pure_at_self(event) or not self.component_manager:
            return False

        try:
            if not self.config.get("extras.pure_at_reply.enable", False):
                return False
            cfg = self.context.get_config(umo=event.unified_msg_origin)
            if cfg.get("platform_settings", {}).get("empty_mention_waiting", True):
                return False

            from .iris_memory.platform import get_adapter

            adapter = get_adapter(event)
            buffer = self.component_manager.get_available_component("l1_buffer")
            if not buffer or not buffer.get_context(adapter.get_session_id(event), 1):
                logger.debug("纯 @ 前没有可注入的 L1 上下文，保持 AstrBot 默认空请求行为")
                return False

            conv_mgr = getattr(self.context, "conversation_manager", None)
            if conv_mgr is None:
                logger.warning("纯 @ 接管失败：AstrBot conversation_manager 不可用")
                return False

            umo = event.unified_msg_origin
            conversation_id = await conv_mgr.get_curr_conversation_id(umo)
            if not conversation_id:
                conversation_id = await conv_mgr.new_conversation(
                    umo, platform_id=event.get_platform_id()
                )
            conversation = await conv_mgr.get_conversation(umo, conversation_id)
            if conversation is None:
                logger.warning("纯 @ 接管失败：无法取得标准会话")
                return False

            request = event.request_llm(
                prompt=" ",
                session_id=conversation_id,
                contexts=[],
                system_prompt="",
                conversation=conversation,
            )
            event.set_extra("provider_request", request)
            event.set_extra("iris_pure_at", True)
            logger.info("已接管纯 @ 消息，将通过标准管道使用现有 L1 上下文")
            return True
        except Exception as exc:
            logger.error(f"纯 @ 消息接管失败，已回退 AstrBot 默认流程：{exc}", exc_info=True)
            return False

    # ========================================================================
    # 连续性服务装配
    # ========================================================================

    def _build_continuity(self) -> Optional[ContinuityService]:
        try:
            return ContinuityService(
                context=self.context,
                config_source=self.config,
                state=self._state,
                owner_ids_get=lambda: self._reply_config.private_whitelist,
                silent_sessions_get=lambda: self.config.get("proactive.silent_sessions", []) or [],
                data_dir=self.config.data_dir,
                llm_manager=self._llm_manager,
                umo_get=lambda gid: self._group_umo.get(gid),
                profile_text=lambda sk: self._profile_text_for(sk, self._group_umo.get(sk, "")),
                l1_recent=self._l1_recent_for,
                save_fn=lambda: self._state.save_dirty(self._kv_save),
                platform_id_get=self._continuity_platform_id,
                resume_cb=self._continuity_resume,
            )
        except Exception:
            logger.error("连续性服务初始化失败，已降级为原有行为", exc_info=True)
            return None

    def _continuity_resume(self, scope: str) -> bool:
        """离线补采推进了主人水位：与实时消息同一条规则恢复主动（只恢复一次）。"""
        try:
            if self._state.resume_after_owner_message(scope):
                logger.info(f"[continuity] scope={scope} proactive resumed by offline owner message")
                return True
        except Exception as e:
            logger.warning(f"[continuity] resume failed: {type(e).__name__}")
        return False

    def _continuity_platform_id(self) -> str:
        """主人私聊 UMO 的平台 ID（频道采集复用该适配器的 client）。"""
        try:
            for owner in self._reply_config.private_whitelist or []:
                umo = self._group_umo.get(f"private:{owner}")
                if umo and ":" in umo:
                    return umo.split(":", 1)[0]
        except Exception:
            pass
        return "userbot"

    def _l1_recent_for(self, session_key: str, limit: int) -> list[tuple[str, str]]:
        if not self.component_manager:
            return []
        buffer = self.component_manager.get_available_component("l1_buffer")
        if buffer is None:
            return []
        messages = buffer.get_context(session_key, limit) or []
        return [
            (str(getattr(m, "role", "")), str(getattr(m, "content", "")))
            for m in messages
        ]

    @staticmethod
    def _session_key_quiet(event) -> str | None:
        """同 _get_session_key，但取不到时不写 event result（无副作用）。"""
        try:
            group_id = event.get_group_id()
            if group_id:
                return str(group_id)
            if event.is_private_chat():
                sender_id = event.get_sender_id()
                if sender_id:
                    return f"private:{sender_id}"
        except Exception:
            pass
        return None

    @staticmethod
    def _is_self_message(event) -> bool:
        """本账号发出的消息（其他设备手动发言以 outgoing 事件进入）。"""
        try:
            message_obj = getattr(event, "message_obj", None)
            raw = getattr(message_obj, "raw_message", None) if message_obj is not None else None
            if raw is not None and getattr(raw, "out", None) is True:
                return True
            sender_id = event.get_sender_id()
            self_id = event.get_self_id()
            if not isinstance(sender_id, (str, int)) or not isinstance(self_id, (str, int)):
                return False
            return bool(str(sender_id)) and str(sender_id) == str(self_id)
        except Exception:
            return False

    @staticmethod
    def _outgoing_scope(event) -> str | None:
        """outgoing 事件的会话键：群为群 ID，私聊为对端 ``private:<peer>``。"""
        try:
            group_id = event.get_group_id()
            if group_id:
                return str(group_id)
            message_obj = getattr(event, "message_obj", None)
            peer = getattr(message_obj, "session_id", None) if message_obj is not None else None
            if not peer:
                peer = getattr(event, "session_id", None)
            if isinstance(peer, (str, int)) and str(peer):
                return f"private:{peer}"
        except Exception:
            pass
        return None

    def _is_receipt_platform(self, event) -> bool:
        """平台能给出真实发送回执（打过回执补丁的 Telethon 适配器）。"""
        try:
            if event.get_platform_name() != _RECEIPT_PLATFORM:
                return False
            inst = self.context.get_platform_inst(event.get_platform_id())
            return callable(getattr(inst, "send_with_receipt", None))
        except Exception:
            return False

    def _blocked_send_target(self, event) -> bool:
        """只读会话 / 内置禁发频道：任何被动回复都不得发出。"""
        try:
            group_id = event.get_group_id()
        except Exception:
            return False
        if not group_id or not isinstance(group_id, (str, int)):
            return False
        try:
            config = getattr(self, "config", None)
            silent = config.get("proactive.silent_sessions", []) if config is not None else []
        except Exception:
            silent = []  # 配置读取失败：内置禁发频道仍然拦截
        return is_silent_or_forbidden(group_id, silent or [])

    async def _continuity_on_owner_message(self, event) -> None:
        """主人私聊新消息：推进水位（重放不推进）→ 推进时恢复主动；收件箱/收尾由服务按开关处理。"""
        svc = getattr(self, "_continuity", None)
        if svc is None:
            return
        try:
            if not event.is_private_chat():
                if not self._is_self_message(event):
                    await self._continuity_on_owner_group_message(svc, event)
                return
            if self._is_self_message(event):
                return
            sender_id = str(event.get_sender_id() or "")
            scope = svc.owner_scope(sender_id)
            if not scope:
                return
            msg_id, msg_ts, reply_to = _message_meta(event)
            if msg_id is None:
                return
            advanced = await svc.note_owner_message(
                scope,
                msg_id,
                msg_ts,
                text=event.message_str or "",
                sender_id=sender_id,
                reply_to_msg_id=reply_to,
                has_media=event_has_media(event),
                media_only=self._is_media_only_message(event),
            )
            if advanced and self._state.resume_after_owner_message(scope):
                logger.info(f"[continuity] scope={scope} proactive resumed by owner message")
            if advanced:
                # 活人感 U8：自由主动开关打开时识别"别发了 / 安静点"与解除（只做规则判断，不存正文）
                engine = getattr(self, "_proactive", None)
                note = getattr(engine, "note_owner_text", None) if engine is not None else None
                if note is not None and note(scope, event.message_str or "", msg_ts) in ("hush", "unhush"):
                    # 叫停 / 解除立即落盘（不等周期保存，重启也不丢）
                    await self._state.save_dirty(self._kv_save)
        except Exception as e:
            logger.warning(f"[continuity] owner message bookkeeping failed: {type(e).__name__}")

    async def _continuity_on_owner_group_message(self, svc, event) -> None:
        """活人感 P1-6：主人本人在群里说生病/情绪差/危机 → 关系冷静期立即解除（只做规则判断，不存原文）。"""
        try:
            active = getattr(svc, "relation_active", None)
            if active is None or not active():
                return
            sender_id = str(event.get_sender_id() or "")
            if not sender_id or not svc.owner_scope(sender_id):
                return
            group_id = event.get_group_id()
            msg_id, _msg_ts, _reply_to = _message_meta(event)
            if not group_id or msg_id is None:
                return
            await svc.note_owner_group_message(sender_id, group_id, msg_id, event.message_str or "")
        except Exception as e:
            logger.warning(f"[lifelike.relation] owner group hook failed: {type(e).__name__}")

    async def _inject_continuity(self, event, req) -> None:
        """普通回复：追加连续性上下文与收尾提示（临时内容，不进对话历史）。"""
        svc = getattr(self, "_continuity", None)
        if svc is None:
            return
        try:
            scope = self._session_key_quiet(event)
            if not scope:
                return
            sender_id = event.get_sender_id()
            text = await svc.assemble(scope, purpose="reply", sender_id=sender_id)
            if text:
                req.extra_user_content_parts.append(TextPart(text=text).mark_as_temp())
            # 活人感 P1-7 / P1-13：认知与群友印象（仅主人私聊、对应模式为 on 时非空）
            blocks_fn = getattr(svc, "lifelike_blocks", None)
            if blocks_fn is not None:
                for block in await blocks_fn(scope, sender_id, event.message_str or ""):
                    req.extra_user_content_parts.append(TextPart(text=block).mark_as_temp())
            hint = await svc.closure_hint(scope)
            if hint and svc.owner_scope(sender_id) == scope:
                req.extra_user_content_parts.append(TextPart(text=hint).mark_as_temp())
        except Exception as e:
            logger.warning(f"[continuity] context injection failed: {type(e).__name__}")

    async def _continuity_after_sent(self, event) -> None:
        """发送后：只有拿到 Telegram 回执才把助手回复写入 L1，并做出站记账。

        回执平台上没拿到回执（发送失败）：stop_event，让 AstrBot 跳过
        ``_save_to_history``（它在 Respond/after_message_sent 之后、且仅在事件未停止时
        才保存对话历史），没发出去的草稿不会成为“已经说过的话”；学习采集同样丢弃。
        """
        try:
            pending = event.get_extra(PENDING_ASSISTANT_EXTRA)
            reply_text = event.get_extra(_REPLY_TEXT_EXTRA)
        except Exception:
            return
        if not pending and not reply_text:
            return
        try:
            ids = _int_ids(event.get_extra(_SENT_IDS_EXTRA))
            inbound_id, _, _ = _message_meta(event)
            turn = f"reply:{inbound_id}" if inbound_id is not None else ""
            if not ids and self._is_receipt_platform(event):
                event.stop_event()
                logger.info("[continuity] reply has no send receipt; not saved to conversation history")
            if pending:
                if ids and self.component_manager:
                    await commit_pending_assistant(event, self.component_manager, ids, turn)
                else:
                    event.set_extra(PENDING_ASSISTANT_EXTRA, None)
                    logger.info("[continuity] reply has no send receipt; assistant turn not written to L1")
            try:
                await commit_pending_learning(event, self.component_manager, bool(ids))
            except Exception as e:
                logger.warning(f"[continuity] deferred learning failed: {type(e).__name__}")
            event.set_extra(_REPLY_TEXT_EXTRA, None)
            svc = getattr(self, "_continuity", None)
            scope = self._session_key_quiet(event)
            if svc is None or not reply_text or not scope:
                return
            result = event.get_result()
            sent_text = ""
            if result is not None:
                try:
                    parts = [
                        str(getattr(c, "text", "") or "").strip()
                        for c in (getattr(result, "chain", None) or [])
                        if type(c).__name__ == "Plain"
                    ]
                    sent_text = "\n".join(p for p in parts if p) or (
                        result.get_plain_text() or ""
                    ).strip()
                except Exception:
                    sent_text = ""
            await svc.record_reply_sent(
                scope,
                sent_text or str(reply_text),
                ids,
                receipt_supported=self._is_receipt_platform(event),
                reply_to_msg_id=inbound_id,
            )
        except Exception as e:
            logger.warning(f"[continuity] after-sent bookkeeping failed: {type(e).__name__}")

    # ========================================================================
    # 贴纸：理解（入站）与主人限定表情包（出站）
    # ========================================================================

    def _init_stickers(self, data_dir) -> None:
        self._sticker_store = None
        self._sticker_understanding = None
        self._sticker_library = None
        try:
            base = Path(str(data_dir)) / "stickers"
            base.mkdir(parents=True, exist_ok=True)
            cfg = ContinuityConfig.from_config(self.config)
            self._sticker_store = StickerStore.for_data_dir(data_dir)
            self._sticker_understanding = StickerUnderstanding(
                self._sticker_store,
                make_describe_image(
                    self._llm_manager,
                    lambda: (str(self.config.get("l1_buffer.summary_provider", "") or "").strip() or None),
                ),
                timeout=15.0,
                daily_cap=cfg.sticker_describe_daily_cap,
            )
            self._sticker_library = StickerLibrary(base / "stickers_library.db")
        except Exception as e:
            logger.warning(f"[stickers] init failed, sticker features off: {type(e).__name__}: {e}")

    def _sticker_cfg(self) -> ContinuityConfig:
        return ContinuityConfig.from_config(self.config)

    def _owner_id_list(self) -> list[str]:
        try:
            return [str(x) for x in (self._reply_config.private_whitelist or [])]
        except Exception:
            return []

    def _sticker_inbound_allowed(self, event) -> bool:
        """只理解主人私聊与已启用群里的贴纸；频道 / silent 会话不处理。"""
        try:
            if event.is_private_chat():
                return str(event.get_sender_id()) in set(self._owner_id_list())
            group_id = event.get_group_id()
            if not group_id or self._blocked_send_target(event):
                return False
            return self._state.is_whitelisted(str(group_id))
        except Exception:
            return False

    # ---- U19 开关（HiddenConfig，受 lifelike_enable 总开关控制；off = 676cd8a 行为） ----

    def _flag_on(self, key: str) -> bool:
        try:
            v = self.config.get(key, "off")
        except Exception:
            return False
        if v is True:
            return True
        return str(v if v is not None else "off").strip().lower() in ("on", "true", "1")

    def _sticker_use_on(self) -> bool:
        return self._flag_on("lifelike_sticker_use")

    def _sticker_learn_on(self) -> bool:
        return self._flag_on("lifelike_sticker_auto_learn")

    def _sticker_learn_groups_on(self) -> bool:
        """U20：也学主人在白名单群里的贴纸用法（需同时打开自动学习）。"""
        return self._sticker_learn_on() and self._flag_on("lifelike_sticker_learn_groups")

    async def _sticker_entries(self, event) -> tuple[Optional[str], list[dict]]:
        lib = getattr(self, "_sticker_library", None)
        if lib is None:
            return None, []
        try:
            cfg = self._sticker_cfg()
            if not cfg.sticker_send_enable:
                return None, []
            scope = self._session_key_quiet(event)
            # 带 #thread 的会话：U19 使用意愿打开、且确认是论坛话题（forum topic）时才发（发到同一话题）；
            # 普通群里的回复串 / 频道评论串等其他线程一律不发（审查 3）
            if not scope or ("#" in scope and not (self._sticker_use_on() and self._is_forum_topic_event(event))):
                return None, []
            if self._blocked_send_target(event) or not self._is_receipt_platform(event):
                return None, []
            return scope, await self._sticker_entries_for(scope, cfg)
        except Exception as e:
            logger.warning(f"[stickers] entries failed: {type(e).__name__}")
            return None, []

    async def _sticker_entries_for(self, scope: str, cfg=None) -> list[dict]:
        """某会话可用的表情包（不含平台 / 只读会话判断，调用方负责）。"""
        lib = getattr(self, "_sticker_library", None)
        if lib is None or not scope:
            return []
        cfg = cfg or self._sticker_cfg()
        if not cfg.sticker_send_enable:
            return []
        silent = [str(x) for x in (self.config.get("proactive.silent_sessions", []) or [])]
        extra: dict = {"unified": True} if getattr(cfg, "sticker_scope_unified", False) else {}
        if self._sticker_learn_on():
            extra["include_learned"] = True
        entries = await asyncio.to_thread(
            lib.allowed_for,
            scope,
            self._owner_id_list(),
            cfg.sticker_groups_enable,
            blocked_scopes=silent,
            **extra,
        )
        return list(entries or [])

    @staticmethod
    def _is_forum_topic_event(event) -> bool:
        """适配器原始消息能确认是论坛话题（forum topic）里的消息。拿不到标识 → False。"""
        try:
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            if raw is None:
                return False
            if getattr(raw, "forum_topic_id", None):
                return True
            reply_to = getattr(raw, "reply_to", None)
            return bool(reply_to is not None and getattr(reply_to, "forum_topic", False) is True)
        except Exception:
            return False

    async def _sticker_prompt_for(self, entries: list[dict], scope: str = "") -> str:
        if not entries:
            return ""
        if not self._sticker_use_on():
            return prompt_block(entries)
        if scope and not str(scope).startswith("private:") and self._sticker_learn_groups_on():
            tier, owner_data = await self._sticker_group_tier(str(scope))
            return _stl.prompt_block_v2(
                entries, tier, private=False, owner_data=owner_data, group_habit=True
            )
        tier, owner_data = await self._sticker_tier()
        return _stl.prompt_block_v2(
            entries, tier, private=str(scope).startswith("private:"), owner_data=owner_data
        )

    @staticmethod
    def _group_scene_key(scope: str) -> str:
        return "group:" + str(scope or "").split("#", 1)[0]

    async def _sticker_group_tier(self, scope: str) -> tuple:
        """U20 群聊档位：主人在**当前群**近 7 天发贴纸的占比为准（owner_activity_scene，按群分开）；
        当前群数据不足回落到私聊 / drive 起点，此时措辞中性（owner_data=False），不引用别的群或私聊画像。"""
        lib = getattr(self, "_sticker_library", None)
        if lib is not None:
            try:
                gm, gs = await asyncio.to_thread(lib.owner_activity_scene, self._group_scene_key(scope), 7)
                own = _stl.owner_tier(gm, gs)
                if own is not None:
                    return own, True
            except Exception:
                pass
        tier, _ = await self._sticker_tier()
        return tier, False

    async def _sticker_tier(self) -> tuple:
        """(档位, 是否来自主人数据)：主人近 7 天私聊贴纸占比（只读计数）为准；数据不足时由聊天欲望档位给起点。"""
        lib = getattr(self, "_sticker_library", None)
        msgs = stickers = 0
        if lib is not None:
            try:
                msgs, stickers = await asyncio.to_thread(lib.owner_activity, 7)
            except Exception:
                msgs = stickers = 0
        try:
            drive = str(self.config.get("lifelike_chat_drive", "off") or "off")
        except Exception:
            drive = "off"
        return _stl.sticker_tier(msgs, stickers, drive)

    async def _inject_sticker_prompt(self, event, req) -> None:
        if self._sticker_use_on() and event.get_extra("iris_counter"):
            return  # 被冒犯、正在回击：不给表情说明（审查 4）
        scope, entries = await self._sticker_entries(event)
        if not entries:
            return
        block = await self._sticker_prompt_for(entries, scope or "")
        if block:
            req.system_prompt = f"{req.system_prompt or ''}\n\n{block}".strip()

    def _sticker_pick_label(self, text: str, entries: list[dict]) -> tuple[str, Optional[str]]:
        """剥离标记并挑出标签。use 开关打开时额外接受末尾 [贴纸:标签] 与唯一近似标签。"""
        labels = [e["label"] for e in entries]
        if not has_marker(text):
            if self._sticker_use_on():
                return take_tiezhi_marker(text, labels)
            return text, None
        clean, label = parse_marker(text, labels)
        if label is None and self._sticker_use_on():
            for raw in marker_raw_labels(text):
                label = _stl.fuzzy_label(raw, labels)
                if label:
                    break
            if label is None:
                # 只记有标记但标签不在库里这件事，不记标签与正文
                logger.info(f"[stickers] marker label not in library; dropped (entries={len(labels)})")
        return clean, label

    async def _take_sticker_marker(self, event, resp) -> None:
        """回复文本里的 [表情:标签] 标记：剥离；合法标签暂存，待文字确认发出后再发。"""
        text = getattr(resp, "completion_text", "") or ""
        use_on = self._sticker_use_on()
        # 只出现“表情”二字（如代码、讨论）而没有标记：原样不动，不论开关
        if ("表情" not in text or not has_marker(text)) and not (use_on and "贴纸" in text):
            return
        try:
            scope, entries = await self._sticker_entries(event)
            clean, label = self._sticker_pick_label(text, entries)
            resp.completion_text = clean
            if label and use_on and event.get_extra("iris_counter"):
                logger.info("[stickers] sticker skipped: counter reply")
                label = None
            if not label:
                return
            if clean.strip():
                event.set_extra("iris_sticker_pending", label)
            else:
                # 只有表情、没有文字：没有文字回执可等，直接按同样的边界发送
                await self._send_sticker(event, label)
        except Exception as e:
            logger.warning(f"[stickers] marker handling failed: {type(e).__name__}")

    async def _send_pending_sticker(self, event) -> bool:
        try:
            label = event.get_extra("iris_sticker_pending")
        except Exception:
            return False
        if not label:
            return False
        event.set_extra("iris_sticker_pending", None)
        if not _int_ids(event.get_extra(_SENT_IDS_EXTRA)):
            logger.info("[stickers] text reply not confirmed sent; sticker skipped")
            return False
        return bool(await self._send_sticker(event, label))

    def _sticker_streak_reset(self, event) -> None:
        """私聊里一条确认发出、但没带贴纸的回复：连续带贴纸计数清零（use 开关 off 时不做）。"""
        try:
            if not self._sticker_use_on() or not _int_ids(event.get_extra(_SENT_IDS_EXTRA)):
                return
            scope = self._session_key_quiet(event)
            if scope and scope.startswith("private:"):
                streak = getattr(self, "_sticker_streak", None)
                if streak is not None:
                    streak.pop(scope, None)
        except Exception:
            pass

    @staticmethod
    def _sticker_peer(scope: str) -> tuple[int, Optional[int]]:
        """会话 → (Telegram peer, 论坛话题 id)。private:<uid> / -100… / -100…#thread。"""
        sk = str(scope)
        if sk.startswith("private:"):
            return int(sk.split(":", 1)[1]), None
        base, _, thread = sk.partition("#")
        return int(base), (int(thread) if thread.strip().lstrip("-").isdigit() else None)

    async def _send_sticker(self, event, label: str):
        lib = getattr(self, "_sticker_library", None)
        if lib is None:
            return
        scope, entries = await self._sticker_entries(event)  # 发送时重新校验权限
        entry = next((e for e in entries if e.get("label") == label), None)
        if not scope or entry is None:
            if self._sticker_use_on():
                logger.info("[stickers] sticker skipped: label no longer allowed here")
            return
        if self._sticker_use_on():
            try:
                inst = self.context.get_platform_inst(event.get_platform_id())
            except Exception:
                inst = None
            return await self._send_sticker_entry(inst, scope, entry, label)
        try:
            if not await asyncio.to_thread(lib.should_send, scope):
                return
            inst = self.context.get_platform_inst(event.get_platform_id())
            client = inst.get_client() if inst is not None else None
            if client is None:
                return
            peer = int(str(event.get_group_id() or event.get_sender_id()))
            ids = await lib.send(client, peer, entry, scope_key=scope)
        except StickerSendError as e:
            logger.warning(f"[stickers] send failed: {e}")
            return
        except Exception as e:
            logger.warning(f"[stickers] send failed: {type(e).__name__}")
            return
        await self._sticker_bookkeep(scope, label, ids)

    async def _send_sticker_entry(self, inst, scope: str, entry: dict, label: str) -> bool:
        """U19 发送路径：60 秒防故障底线（按群计，去掉 #话题后缀）、私聊连续两条带贴纸后下一条不发、
        论坛话题发到同一话题、每个静默分支都留计数日志（不含正文）。"""
        lib = getattr(self, "_sticker_library", None)
        if lib is None:
            return False
        streak = getattr(self, "_sticker_streak", None)
        if streak is None:
            streak = self._sticker_streak = {}
        private = str(scope).startswith("private:")
        if private and streak.get(scope, 0) >= _STICKER_STREAK_MAX:
            streak.pop(scope, None)
            logger.info("[stickers] sticker skipped: streak floor")
            return False
        floor_scope = str(scope).split("#", 1)[0]
        try:
            if not await asyncio.to_thread(lib.should_send, floor_scope, None, _STICKER_FLOOR_SECONDS):
                logger.info("[stickers] sticker skipped: floor interval")
                return False
            client = inst.get_client() if inst is not None and hasattr(inst, "get_client") else None
            if client is None:
                logger.info("[stickers] sticker skipped: no telethon client")
                return False
            peer, thread = self._sticker_peer(scope)
            ids = await lib.send(client, peer, entry, scope_key=floor_scope, reply_to=thread)
        except StickerSendError as e:
            logger.warning(f"[stickers] send failed: {e}")
            return False
        except Exception as e:
            logger.warning(f"[stickers] send failed: {type(e).__name__}")
            return False
        logger.info(f"[stickers] sticker sent label={label} source={entry.get('source') or 'manual'}")
        if private:
            streak[scope] = streak.get(scope, 0) + 1
        await self._sticker_bookkeep(scope, label, ids)
        return True

    async def _sticker_bookkeep(self, scope: str, label: str, ids) -> None:
        svc = getattr(self, "_continuity", None)
        store = getattr(svc, "store", None) if svc is not None else None
        if store is None or not ids:
            return
        try:
            oid = await asyncio.to_thread(
                store.add_outbound, scope, "reply", f"[贴纸：{label}]", "sending", "bot"
            )
            await asyncio.to_thread(store.mark_outbound, oid, "sent", ids)
        except Exception as e:
            logger.warning(f"[stickers] outbound bookkeeping failed: {type(e).__name__}")

    # ---- U19 主动消息配贴纸（ProactiveEngine 回调；use 开关 off 时全部空操作） ----

    def _receipt_inst_for_umo(self, umo: str):
        try:
            pid = str(umo or "").split(":", 1)[0]
            inst = self.context.get_platform_inst(pid) if pid else None
            if inst is None or not callable(getattr(inst, "send_with_receipt", None)):
                return None
            meta = getattr(inst, "meta", None)
            name = getattr(meta() if callable(meta) else meta, "name", _RECEIPT_PLATFORM)
            return inst if name == _RECEIPT_PLATFORM else None
        except Exception:
            return None

    async def _proactive_sticker_block(self, group_id: str, umo: str) -> str:
        if not self._sticker_use_on() or "#" in str(group_id):
            return ""  # 主动消息拿不到论坛话题标识：带 #线程 的会话不配贴纸
        try:
            if self._receipt_inst_for_umo(umo) is None:
                return ""
            entries = await self._sticker_entries_for(str(group_id))
            return await self._sticker_prompt_for(entries, str(group_id))
        except Exception as e:
            logger.warning(f"[stickers] proactive block failed: {type(e).__name__}")
            return ""

    async def _proactive_sticker_take(self, group_id: str, umo: str, text: str) -> str:
        """主动消息生成后：剥离标记，合法标签暂存到该会话，等文字确认发出（_on_initiate_sent）再补发。"""
        pend = getattr(self, "_proactive_sticker_pending", None)
        if pend is None:
            pend = self._proactive_sticker_pending = {}
        pend.pop(str(group_id), None)
        if not text or not self._sticker_use_on():
            return text
        if not has_marker(text) and "贴纸" not in text:
            return text
        try:
            entries = [] if "#" in str(group_id) else await self._sticker_entries_for(str(group_id))
            clean, label = self._sticker_pick_label(text, entries)
            if label and clean.strip():
                pend[str(group_id)] = (time.time(), label, str(umo or ""))
            return clean
        except Exception as e:
            logger.warning(f"[stickers] proactive marker failed: {type(e).__name__}")
            text, _ = parse_marker(text, [])
            return text

    async def _proactive_sticker_after_sent(self, group_id: str) -> None:
        pend = getattr(self, "_proactive_sticker_pending", None) or {}
        item = pend.pop(str(group_id), None)
        if not item or not self._sticker_use_on():
            return
        ts, label, umo = item
        if time.time() - ts > 600:
            return
        inst = self._receipt_inst_for_umo(umo)
        if inst is None:
            return
        entries = await self._sticker_entries_for(str(group_id))  # 发送时重新校验权限
        entry = next((e for e in entries if e.get("label") == label), None)
        if entry is None:
            return
        await self._send_sticker_entry(inst, str(group_id), entry, label)

    async def _run_l2_probe(self) -> None:
        """运维验收（T16）：宿主机 root 写入 ``continuity/l2_probe.json`` 触发一次 L2 操作。

        - ``{"op":"write","text":"【验收测试记忆】..."}``：写入一条只属于主人私聊（group_id=""）的测试记忆；
        - ``{"op":"search","query":"..."}``：私聊口径检索 + 群口径检索（验证群里召回不到）；
        - ``{"op":"delete","memory_id":"...","query":"定位用检索词"}``：只允许删除正文带【验收测试记忆】标记的条目。
        文件读到即删除；结果写入 ``l2_probe.result.json``（只含 id、命中与否、分数，不含其它记忆正文）。
        """
        cm = getattr(self, "component_manager", None)
        svc = getattr(self, "_continuity", None)
        store = getattr(svc, "store", None) if svc is not None else None
        if cm is None or store is None:
            return
        base = store.db_path.parent
        path = base / "l2_probe.json"
        if not path.exists():
            return
        import json as _json

        try:
            raw = path.read_text(encoding="utf-8")
        finally:
            try:
                path.unlink()
            except OSError:
                pass
        marker = "【验收测试记忆】"
        result: dict = {"at": time.time(), "ok": False}
        try:
            req = _json.loads(raw) or {}
            op = str(req.get("op", ""))
            result["op"] = op
            adapter = cm.get_available_component("l2_memory")
            if adapter is None:
                result["error"] = "l2_unavailable"
            elif op == "write":
                text = str(req.get("text", "")).strip()
                owners = self._owner_id_list()
                if not text.startswith(marker) or len(text) > 200 or not owners:
                    result["error"] = "bad_text"
                else:
                    mid, created = await adapter.add_memory_with_result(
                        text,
                        metadata={"group_id": "", "user_id": owners[0], "source": "acceptance_probe"},
                        skip_dedup=True,
                    )
                    result.update(ok=bool(mid), memory_id=mid, created=created)
            elif op == "search":
                query = str(req.get("query", "")).strip()[:100]
                private_hits = await adapter.retrieve(query, group_id=None, top_k=5)
                probe_gid = str(req.get("group_id", "") or "").strip()
                group_hits = await adapter.retrieve(query, group_id=probe_gid, top_k=5) if probe_gid else []

                def _brief(hits):
                    out = []
                    for h in hits or []:
                        content = str(getattr(h.entry, "content", "") or "")
                        out.append({
                            "memory_id": getattr(h.entry, "id", ""),
                            "score": round(float(getattr(h, "score", 0.0) or 0.0), 3),
                            "is_probe": content.startswith(marker),
                        })
                    return out

                result.update(ok=True, private=_brief(private_hits), group=_brief(group_hits))
            elif op == "delete":
                mid = str(req.get("memory_id", ""))
                locate = str(req.get("query", "") or marker)[:100]
                hits = await adapter.retrieve(locate, group_id=None, top_k=20)
                allowed = {
                    getattr(h.entry, "id", "")
                    for h in hits or []
                    if str(getattr(h.entry, "content", "") or "").startswith(marker)
                }
                if mid not in allowed:
                    result["error"] = "not_a_probe_memory"
                else:
                    result.update(ok=bool(await adapter.delete_entries([mid])), memory_id=mid)
            else:
                result["error"] = "bad_op"
        except Exception as e:
            result["error"] = type(e).__name__
        logger.info(f"[continuity] l2 probe op={result.get('op')} ok={result.get('ok')} err={result.get('error', '-')}")
        try:
            (base / "l2_probe.result.json").write_text(_json.dumps(result, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    async def _close_stickers(self) -> None:
        for task in list(getattr(self, "_sticker_learn_tasks", None) or ()):
            task.cancel()
        su = getattr(self, "_sticker_understanding", None)
        if su is not None:
            try:
                await su.aclose()
            except Exception:
                pass
        for obj in (getattr(self, "_sticker_store", None), getattr(self, "_sticker_library", None)):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass

    # ========================================================================
    # 记忆侧注册
    # ========================================================================

    def _register_llm_tools(self) -> None:
        """统一注册全部 LLM Tool"""
        try:
            self._registered_llm_tools = register_llm_tools(
                context=self.context,
                owner_module=self.__class__.__module__,
                state=self._state,
                tool_context=self._tool_ctx,
            )
            logger.info(f"已注册 {len(self._registered_llm_tools)} 个 Iris LLM Tool")
        except Exception as e:
            logger.error(f"注册 Iris LLM Tool 失败：{e}", exc_info=True)

    def _register_command_handlers(self) -> None:
        """注册记忆侧指令处理器"""
        try:
            registry = get_registry()
            handlers = [
                L1CommandHandler(),
                L2CommandHandler(),
                L3CommandHandler(),
                ProfileCommandHandler(),
                AllCommandHandler(),
                LearningCommandHandler(),
                EvolutionCommandHandler(),
                BeliefsCommandHandler(),
                ForgetCommandHandler(),
            ]
            for handler in handlers:
                registry.register(handler)
            logger.info(f"已注册 {len(handlers)} 个记忆指令处理器")
        except Exception as e:
            logger.error(f"注册记忆指令处理器失败：{e}", exc_info=True)

    def _register_web_api(self) -> None:
        try:
            register_all_routes(self.context)
        except Exception as e:
            logger.error(f"注册记忆 Web API 失败：{e}", exc_info=True)

    # ========================================================================
    # 生命周期
    # ========================================================================

    async def initialize(self) -> None:
        # 0. 后台预热 tiktoken 编码器：首次运行需联网下载 BPE 文件，
        #    放线程池执行，预热期间 token 计数临时走字符估算
        self._encoder_warmup_task = asyncio.create_task(warm_up_encoders_async())
        # 让预热协程先设置 pending 标志，再进入其余组件初始化；否则本轮
        # 事件循环中更早发生的 token 计数仍可能同步触发首次下载。
        await asyncio.sleep(0)

        # 1. 记忆组件初始化
        try:
            await initialize_components(self.component_manager)
        except Exception as e:
            logger.error(f"记忆组件初始化失败：{e}", exc_info=True)

        # 2. 旧版（v2.x）数据自动迁移（独立模块，失败不阻断启动）
        if LEGACY_MIGRATION_ENABLED:
            try:
                from .iris_memory.legacy_migration import migrate_if_needed

                await migrate_if_needed(
                    self.context, self, StarTools.get_data_dir(), self.component_manager
                )
            except Exception:
                logger.error("旧数据迁移失败（不影响插件启动）", exc_info=True)

        # 3. 主动回复侧初始化
        await self._state.load_all(self._kv_load)
        umo_data = await self._kv_load(_UMO_KV_KEY)
        if isinstance(umo_data, dict):
            self._group_umo = {str(k): str(v) for k, v in umo_data.items()}
        # 旧版页面配置（KV overrides）一次性迁移到隐藏参数，迁移后清空避免重复覆盖
        config_overrides = await self._kv_load("iris_reply:config_overrides")
        migrated = ReplyConfigManager.legacy_overrides_to_hidden(config_overrides)
        if migrated:
            self.config.update_hidden(migrated)
            await self._kv_save("iris_reply:config_overrides", {})
            logger.info(f"已将 {len(migrated)} 项旧版主动回复页面配置迁移至隐藏参数")
        self._save_task = asyncio.create_task(self._periodic_save())
        self._stats.enabled = self._reply_config.stats_enabled
        register_reply_web_apis(
            context=self.context,
            plugin_name=PLUGIN_NAME,
            state=self._state,
            stats=self._stats,
            window=self._sliding_window,
            kv_save=self._kv_save,
        )
        # 连续性服务须在引擎启动前打开账本：引擎启动时会对账并恢复滑动窗口
        # （restore_windows 发生在 load_all 之后）
        if getattr(self, "_continuity", None) is not None:
            await self._continuity.start()
        await self._proactive.start()

        # 4. 功能重叠插件检测（重复注入/门控警告）
        for other in ("astrbot_plugin_iris_chat_memory", "astrbot_plugin_iris_reply"):
            try:
                if self.context.get_registered_star(other):
                    logger.warning(
                        f"检测到插件 {other} 已安装，与本插件功能重叠，"
                        "建议停用其一，避免记忆重复注入与主动回复重复门控"
                    )
            except Exception:
                pass
        # 活人感 P0-1：第二套主动插件仍启用时提示（只读检测，不修改它）
        try:
            stripper = getattr(self, "_bypass_stripper", None)
            if stripper is not None:
                stripper.check_proactive_chat(self.context)
                # 审查 M1：已排好的 cron 任务到点不经过 on_llm_request，只能提示
                await stripper.check_cron_jobs(self.context)
            else:
                logger.warning(
                    "[lifelike.outbound] bypass stripper not initialised; "
                    "send_message_to_user/future_task are NOT stripped"
                )
        except Exception:
            pass

        logger.info("Iris Memory 整合插件异步初始化完成")

    async def terminate(self):
        """插件卸载清理"""
        try:
            await self._terminate_inner()
        finally:
            # 审查 L5：中途抛异常也要卸掉日志过滤器（停用而非重载时不残留）
            self._uninstall_log_redact()

    async def _terminate_inner(self):
        logger.info("开始关闭插件组件...")
        warmup = getattr(self, "_encoder_warmup_task", None)
        if warmup and not warmup.done():
            warmup.cancel()
            try:
                await warmup
            except asyncio.CancelledError:
                pass
        # 主动回复侧
        await self._proactive.stop()
        if self._save_task and not self._save_task.done():
            self._save_task.cancel()
            try:
                await self._save_task
            except asyncio.CancelledError:
                pass
        await self._state.save_all(self._kv_save)
        await self._kv_save(_UMO_KV_KEY, dict(self._group_umo))
        continuity = getattr(self, "_continuity", None)
        if continuity is not None:
            await continuity.save_windows(self._sliding_window, self._state.get_whitelist())
            await continuity.stop()
        await self._close_stickers()
        self._follow_pending.clear()
        self._reply_in_progress.clear()
        self._passive_active.clear()
        self._triggering.clear()
        self._decision_tickets.clear()
        self._passive_watch_last.clear()
        self._passive_watch_hash.clear()
        # 记忆侧
        await shutdown_components(self.component_manager)
        logger.info("Iris Memory 整合插件已卸载")

    # ========================================================================
    # 活人感 P0-3：日志去正文（lifelike_log_redact）
    # ========================================================================

    def _install_identity(self, data_dir) -> None:
        """角色名 / 主人称呼 / 参照角色文件都从配置读取（iris_memory.identity）；失败不影响启动。"""
        try:
            from .iris_memory import identity

            cfg = self.config
            identity.set_source(lambda key, default="": cfg.get(key, default), data_dir)
            logger.info(
                f"[identity] bot_names={len(identity.bot_names())} owner_names={len(identity.owner_names())} "
                f"reference={'on' if identity.reference() else 'off'}"
            )
        except Exception as e:
            logger.warning(f"[identity] install failed: {type(e).__name__}")

    def _install_log_redact(self) -> None:
        """在 core / 适配器 / 本插件 logger 上挂正文过滤器；开关每条日志实时读取（热修改生效）。
        失败只记日志，不影响插件启动（等同旧行为）。"""
        try:
            from .iris_memory.utils import log_redact

            cfg = self.config

            def _enabled() -> object:
                return cfg.get(log_redact.FLAG_KEY, True)

            log_redact.set_flag_getter(_enabled)
            self._log_redact_filter = log_redact.install(_enabled)
            logger.info(f"[lifelike.log_redact] installed loggers={len(self._log_redact_filter.loggers)}")
        except Exception as e:
            self._log_redact_filter = None
            logger.warning(f"[lifelike.log_redact] install failed: {type(e).__name__}")

    def _uninstall_log_redact(self) -> None:
        try:
            from .iris_memory.utils import log_redact

            log_redact.uninstall(getattr(self, "_log_redact_filter", None))
            self._log_redact_filter = None
        except Exception as e:
            logger.warning(f"[lifelike.log_redact] uninstall failed: {type(e).__name__}")

    # ========================================================================
    # 主动回复侧：状态保存与互斥
    # ========================================================================

    async def _periodic_save(self) -> None:
        while True:
            await asyncio.sleep(self._save_interval)
            try:
                await self._state.save_dirty(self._kv_save)
                if self._umo_dirty:
                    self._umo_dirty = False
                    await self._kv_save(_UMO_KV_KEY, dict(self._group_umo))
                self._sliding_window.cleanup(self._state.get_whitelist())
                self._cleanup_stale_active()
                sync_stats_group_state(self._state, self._stats)
                continuity = getattr(self, "_continuity", None)
                if continuity is not None:
                    await continuity.save_windows(
                        self._sliding_window, self._state.get_whitelist()
                    )
                    await continuity.run_acceptance_probe()
                await self._run_l2_probe()
            except Exception as e:
                logger.warning(f"Iris Reply: periodic save error: {e}")

    def _cleanup_stale_active(self) -> None:
        now = time.time()
        stale_rip = [gid for gid, ts in self._reply_in_progress.items() if now - ts > _IRIS_ACTIVE_TIMEOUT]
        for gid in stale_rip:
            logger.info(f"Iris Reply: cleaning up stale reply_in_progress for group {gid} (timeout)")
            self._reply_in_progress.pop(gid, None)
        stale_passive = [gid for gid, ts in self._passive_active.items() if now - ts > _IRIS_ACTIVE_TIMEOUT]
        for gid in stale_passive:
            logger.info(f"Iris Reply: cleaning up stale passive for group {gid} (timeout)")
            self._passive_active.pop(gid, None)
        stale_triggering = [gid for gid, ts in self._triggering.items()
                            if gid not in self._reply_in_progress and now - ts > _IRIS_ACTIVE_TIMEOUT]
        for gid in stale_triggering:
            logger.info(f"Iris Reply: cleaning up stale triggering for group {gid}")
            self._triggering.pop(gid, None)
        registry = getattr(self, "_decision_tickets", None)
        if registry is not None:
            for ticket in registry.cleanup(now):
                logger.info(
                    "Iris Reply: cleaning up stale decision ticket "
                    f"for group {ticket.group_id}, event={ticket.event_id}"
                )
                if ticket.group_id not in self._reply_in_progress:
                    self._triggering.pop(ticket.group_id, None)

    @staticmethod
    def _decision_event_id(event: AstrMessageEvent) -> str:
        """取得平台消息 ID；缺失时退化为当前事件对象 ID。"""
        message_obj = getattr(event, "message_obj", None)
        raw_id = getattr(message_obj, "message_id", None) if message_obj else None
        if raw_id not in (None, ""):
            return f"message:{raw_id}"
        return f"event:{id(event)}"

    def _release_decision_ticket(self, group_id: str, ticket_id: str = "") -> None:
        """仅由 ticket 所有者释放，避免迟到事件清掉新事件的决策状态。"""
        registry = getattr(self, "_decision_tickets", None)
        if registry is None:
            self._triggering.pop(group_id, None)
            return
        active = registry.get(group_id)
        if active is not None and ticket_id and active.ticket_id != ticket_id:
            return
        registry.release(group_id, ticket_id)
        self._triggering.pop(group_id, None)

    def _release_claim(self, event) -> None:
        """事件在到达 on_llm_request 前被 Iris 自己 stop 时，释放 on_message 的认领。"""
        info = event.get_extra("iris_decision") or {}
        key = str(info.get("session_key") or "") if isinstance(info, dict) else ""
        key = key or self._session_key_quiet(event) or ""
        if not key:
            return
        if info:
            self._release_decision_ticket(key, str(info.get("ticket_id") or ""))
            try:
                self._tool_ctx.clear_context()
            except Exception:
                pass
            event.set_extra("iris_decision", None)
        if event.get_extra("iris_mode") == "passive":
            self._passive_active.pop(key, None)

    def _release_undecided_claim(self, group_id: str, now: float) -> None:
        ts = self._triggering.get(group_id)
        if (
            ts is not None
            and group_id not in self._reply_in_progress
            and now - ts > _TRIGGER_UNDECIDED_TIMEOUT
        ):
            logger.info(
                f"Iris Reply: releasing undecided claim for group {group_id} "
                f"(held {now - ts:.0f}s without reaching a decision)"
            )
            self._release_decision_ticket(group_id)

    def _is_busy(self, group_id: str) -> bool:
        return (
            group_id in self._reply_in_progress
            or group_id in self._triggering
            or group_id in self._passive_active
        )

    async def _kv_save(self, key: str, value: Any) -> None:
        await self.put_kv_data(key, value)

    async def _kv_load(self, key: str) -> Any:
        return await self.get_kv_data(key, None)

    async def _profile_text_for(self, session_key: str, umo: str) -> str:
        """为主动消息生成画像摘要；仅私聊有对应用户，失败返回空串。"""
        if not self.component_manager or not str(session_key).startswith("private:"):
            return ""
        user_id = str(session_key).split(":", 1)[1]
        try:
            storage = self.component_manager.get_available_component("profile")
            if storage is None:
                return ""
            profile = await storage.get_user_profile(user_id, "default", "default")
        except Exception as exc:
            logger.warning(f"[iris] 主动消息画像读取失败：{exc}")
            return ""
        if profile is None:
            return ""
        parts = []
        if getattr(profile, "interests", None):
            parts.append("兴趣：" + "、".join(profile.interests[:8]))
        if getattr(profile, "occupation", None):
            parts.append("身份：" + profile.occupation)
        if getattr(profile, "communication_style", None):
            parts.append("沟通偏好：" + profile.communication_style)
        if getattr(profile, "important_events", None):
            parts.append("经历过：" + "；".join(profile.important_events[-3:]))
        if getattr(profile, "taboo_topics", None):
            parts.append("避开的话题：" + "、".join(profile.taboo_topics[:4]))
        return "\n".join(parts)

    def _get_session_key(self, event) -> str | None:
        """返回会话键：群聊为群 ID，私聊为 ``private:<uid>``。

        主动回复引擎内部只把该键当作不透明字符串使用，真正发送时再通过
        ``_group_umo`` 映射到 unified_msg_origin，因此私聊无需改动引擎。
        """
        group_id = event.get_group_id()
        if group_id:
            return group_id
        if event.is_private_chat():
            sender_id = event.get_sender_id()
            if sender_id:
                return f"private:{sender_id}"
        event.set_result("无法获取会话标识")
        return None

    async def _get_provider_id(self, event, preferred: str = "") -> str | None:
        if preferred:
            return preferred
        try:
            return await self.context.get_current_chat_provider_id(
                event.unified_msg_origin
            )
        except Exception:
            logger.error("Iris Reply: failed to get provider ID")
            return None

    def _strip_initiate_text(self, text: str) -> str:
        """initiate 直发消息的 Markdown 去除

        直发通路（context.send_message）不触发 on_decorating_result 钩子，
        消息始终以纯文本发送到平台，此处补齐与管线消息一致的 Markdown 去除，
        避免同群内跟话消息与主动发起消息格式处理不一致。
        """
        text = self._casual_text(text)
        stripper = self._markdown_stripper
        if not stripper or not text:
            return text
        try:
            if not stripper.should_strip(text, use_t2i=False):
                return text
            return stripper.strip(text)
        except Exception as e:
            logger.warning(f"initiate 消息 Markdown 去除失败：{e}")
            return text

    def _casual_on(self) -> bool:
        try:
            v = self.config.get("style_casual_punctuation", False)
        except Exception:
            return False
        return v is True or str(v).strip().lower() in ("true", "1", "on")

    def _casual_text(self, text: str) -> str:
        """随性标点（style_casual_punctuation）：闲聊类文本去掉段末句号；关闭 / 异常 → 原文。"""
        if not text or not self._casual_on():
            return text
        try:
            return _casualize(text)
        except Exception:
            return text

    async def _on_initiate_sent(self, group_id: str, text: str) -> None:
        """initiate 直发成功后，把 bot 发言回填进 L1 缓冲

        直发通路（context.send_message）不触发任何事件钩子，
        若不回填，L1 上下文中将看不到这类发起消息。
        """
        auditor = getattr(self, "_style_auditor", None)
        if auditor is not None and text:
            auditor.schedule(str(group_id), _STYLE_PROACTIVE, text)
        try:
            await self._proactive_sticker_after_sent(group_id)
        except Exception as e:
            logger.warning(f"[stickers] proactive sticker failed: {type(e).__name__}")
        if not self.component_manager or not text:
            return
        try:
            await handle_initiate_backfill(group_id, text, self.component_manager)
        except Exception as e:
            logger.warning(f"initiate 消息回填 L1 失败：{e}")

    # ========================================================================
    # 主动回复侧：LLM 工具（已迁移至 iris_memory/tools/proactive.py，
    # 由 _register_llm_tools() 统一注册）
    # ========================================================================

    # ========================================================================
    # 主动回复侧：管理指令
    # ========================================================================

    @filter.command_group("iris_reply")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.ALL)
    def iris_reply_group(self):
        pass

    @iris_reply_group.command("enable")
    async def cmd_enable(self, event) -> None:
        group_id = self._get_session_key(event)
        if not group_id:
            return
        self._state.add_to_whitelist(group_id)
        await self._state.save_dirty(self._kv_save)
        event.set_result(f"群 {group_id} 已启用主动回复")

    @iris_reply_group.command("disable")
    async def cmd_disable(self, event) -> None:
        group_id = self._get_session_key(event)
        if not group_id:
            return
        self._state.remove_from_whitelist(group_id)
        self._sliding_window.remove_group(group_id)
        self._state.remove_group_lock(group_id)
        await self._state.save_dirty(self._kv_save)
        event.set_result(f"群 {group_id} 已禁用主动回复")

    @iris_reply_group.command("status")
    async def cmd_status(self, event) -> None:
        group_id = self._get_session_key(event)
        if not group_id:
            return
        text = self._admin.get_status(group_id)
        engine = getattr(self, "_proactive", None)
        extra = getattr(engine, "freedom_status_lines", None) if engine is not None else None
        if extra is not None:
            try:
                lines = extra(group_id)
            except Exception:
                lines = []
            if lines:
                text = text + "\n" + "\n".join(lines)
        event.set_result(text)

    @iris_reply_group.command("reset")
    async def cmd_reset(self, event) -> None:
        group_id = self._get_session_key(event)
        if not group_id:
            return
        msg = self._admin.reset_group(group_id)
        self._sliding_window.remove_group(group_id)
        await self._state.save_dirty(self._kv_save)
        event.set_result(msg)

    @iris_reply_group.command("cooldown")
    async def cmd_cooldown(self, event, minutes: int = 5) -> None:
        group_id = self._get_session_key(event)
        if not group_id:
            return
        msg = self._admin.set_cooldown(group_id, minutes)
        await self._state.save_dirty(self._kv_save)
        event.set_result(msg)

    @iris_reply_group.command("willingness")
    async def cmd_willingness(self, event, level: str = "") -> None:
        group_id = self._get_session_key(event)
        if not group_id:
            return
        if not level.strip():
            current = self._admin.get_willingness(group_id)
            event.set_result(f"群 {group_id} 当前回复意愿: {current}\n可选: 低/中/高 (low/medium/high)")
            return
        msg = self._admin.set_willingness(group_id, level.strip())
        await self._state.save_dirty(self._kv_save)
        event.set_result(msg)

    @iris_reply_group.command("initiate")
    async def cmd_initiate(self, event) -> None:
        group_id = self._get_session_key(event)
        if not group_id:
            return
        result = await self._proactive.attempt_initiate(group_id, force=True)
        event.set_result(f"主动发起: {result}")

    # ========================================================================
    # AstrBot 钩子
    # ========================================================================

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def on_self_outgoing(self, event: AstrMessageEvent) -> None:
        """本账号在其他设备手动发出的消息：记为 manual 出站并终止事件。

        优先级高于所有入站处理器（含其他插件），不当作用户输入写入 L1、
        不触发 LLM，也不算 bot 生成。
        """
        # lifelike P0-2：agent 异常直发路径兜底（早于 core 构建 agent，覆盖构建期异常）
        self._install_error_guard(event)
        if not self._is_self_message(event):
            return
        try:
            continuity = getattr(self, "_continuity", None)
            if continuity is not None:
                scope = self._outgoing_scope(event)
                msg_id, _, _ = _message_meta(event)
                if scope and msg_id is not None:
                    await continuity.record_manual(scope, msg_id, event.message_str or "")
        except Exception as e:
            logger.warning(f"[continuity] manual outgoing record failed: {type(e).__name__}")
        finally:
            event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL, priority=900)
    async def on_sticker_understanding(self, event: AstrMessageEvent) -> None:
        """贴纸/照片先变成文字描述（在媒体门控与 L1 记录之前），门控与记忆都能“看到”它。"""
        su = getattr(self, "_sticker_understanding", None)
        if su is None or self._is_self_message(event):
            return
        try:
            if not self._sticker_inbound_allowed(event):
                if self._sticker_learn_groups_on() and not event.is_private_chat():
                    await self._sticker_group_observe_cached(event)
                return
            cfg = self._sticker_cfg()
            raw = getattr(event.message_obj, "raw_message", None)
            meta = extract_sticker_meta(raw) if cfg.sticker_understanding_enable else None
            if meta is None and cfg.image_understanding_enable:
                meta = extract_photo_meta(raw)
            if meta is None:
                await self._sticker_owner_observe(event, raw, None, None)
                return
            desc = await su.describe(meta, frame_getter_from_event(event))
            rewrite_event(event, desc, meta)
            await self._sticker_owner_observe(event, raw, meta, desc)
        except Exception as e:
            logger.warning(f"[stickers] understanding failed: {type(e).__name__}")

    # ---- U19 自动学习主人私聊里的贴纸 ----

    _STK_CTX_WINDOW = 60.0  # 发贴纸前多久内的对话算“当时的场合”（审查 8：缩短到 60 秒）
    _STK_AFTER_WINDOW = 120.0  # 发贴纸后多久内主人的下一句可补判场合

    def _is_owner_private(self, event) -> bool:
        try:
            return bool(event.is_private_chat()) and not event.get_group_id() and (
                str(event.get_sender_id()) in set(self._owner_id_list())
            )
        except Exception:
            return False

    def _stk_ctx(self) -> dict:
        ctx = getattr(self, "_sticker_learn_ctx", None)
        if ctx is None:
            ctx = self._sticker_learn_ctx = {}
        return ctx

    async def _sticker_owner_observe(self, event, raw, meta, desc) -> None:
        """主人私聊里的一条消息（贴纸或文字）：计数、记场合（只存固定短语）、学贴纸、“别学这个”。

        两个 U19 开关都 off 时什么都不做。只处理主人与她的私聊；群、频道、群友一律不学。"""
        learn_on = self._sticker_learn_on()
        if not (learn_on or self._sticker_use_on()):
            return
        lib = getattr(self, "_sticker_library", None)
        if lib is None or self._is_self_message(event):
            return
        if not self._is_owner_private(event):
            await self._sticker_owner_group_observe(event, raw, meta, desc)
            return
        try:
            if meta is None:
                is_sticker = extract_sticker_meta(raw) is not None
            else:
                is_sticker = getattr(meta, "kind", "") in ("static", "tgs", "video")
            text = "" if is_sticker else str(getattr(event, "message_str", "") or "")
            if text.strip().startswith("/"):
                return
            await asyncio.to_thread(lib.note_owner_message, is_sticker)
            if not learn_on:
                return
            scope = f"private:{event.get_sender_id()}"
            ctx = self._stk_ctx().setdefault(scope, {})
            now = time.time()
            if not is_sticker:
                if text and _stl.is_dont_learn(text) and await self._sticker_dont_learn(event, raw):
                    return
                occ = _stl.classify_owner_text(text)
                pend = ctx.pop("after", None)
                if occ and pend and now - pend[0] <= self._STK_AFTER_WINDOW:
                    await asyncio.to_thread(lib.learn_set_occasion, pend[1], occ)
                if occ:
                    ctx["last"] = (now, occ)
                else:
                    ctx.pop("last", None)  # 判不出就清空，不让更早的场合串到后面的贴纸上
                return
            if desc is None or meta is None or getattr(meta, "kind", "") not in ("static", "tgs", "video"):
                return  # 没有描述（失败 / 额度用完）就不学；下次主人再发时缓存命中再学
            if raw is not None and getattr(raw, "fwd_from", None):
                logger.info("[stickers.learn] forwarded sticker; not learned")
                return  # 转发来的贴纸不学（审查 7）
            last = ctx.pop("last", None)
            occ = last[1] if last and now - last[0] <= self._STK_CTX_WINDOW else ""
            if not occ:
                ctx["after"] = (now, int(meta.doc_id))
            self._sticker_spawn(self._sticker_learn_one(raw, meta, str(desc), occ))
        except Exception as e:
            logger.warning(f"[stickers.learn] observe failed: {type(e).__name__}")

    # ---- U20 主人在白名单群里的贴纸用法 ----

    def _owner_group_learnable(self, event) -> bool:
        """主人本人在 U9 资格群（id_whitelist 里的群，排除频道 / silent / 主人频道）里发的消息。"""
        try:
            if event.is_private_chat():
                return False
            gid = str(event.get_group_id() or "")
            if not gid or str(event.get_sender_id()) not in set(self._owner_id_list()):
                return False
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            if raw is not None and getattr(raw, "post", None) is True:
                return False  # 广播频道帖子
            base = gid.split("#", 1)[0]
            if base in {str(x) for x in DEFAULT_BLOCKED_SCOPES} or self._blocked_send_target(event):
                return False
            return bool(self._gf_eligible(gid))
        except Exception:
            return False

    async def _sticker_group_observe_cached(self, event) -> None:
        """贴纸理解没覆盖到的群（不在主动回复白名单）：只用描述缓存，不新增视觉调用。"""
        if self._is_self_message(event) or not self._owner_group_learnable(event):
            return
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        meta = extract_sticker_meta(raw)
        desc = None
        store = getattr(self, "_sticker_store", None)
        if meta is not None and store is not None:
            try:
                cached = await asyncio.to_thread(store.get, int(meta.doc_id))
                desc = cached.description if cached is not None else None
            except Exception:
                desc = None
        await self._sticker_owner_group_observe(event, raw, meta, desc)

    async def _sticker_owner_group_observe(self, event, raw, meta, desc) -> None:
        """主人在白名单群里的一条消息：按场景计数、记群里场合（只存短语）、学贴纸。开关 off → 什么都不做。"""
        if not self._sticker_learn_groups_on():
            return
        lib = getattr(self, "_sticker_library", None)
        if lib is None or self._is_self_message(event) or not self._owner_group_learnable(event):
            return
        try:
            if meta is None:
                is_sticker = extract_sticker_meta(raw) is not None
            else:
                is_sticker = getattr(meta, "kind", "") in ("static", "tgs", "video")
            text = "" if is_sticker else str(getattr(event, "message_str", "") or "")
            if text.strip().startswith("/"):
                return
            base = str(event.get_group_id() or "").split("#", 1)[0]
            await asyncio.to_thread(lib.note_owner_scene, self._group_scene_key(base), is_sticker)
            ctx = self._stk_ctx().setdefault(f"group:{base}:{event.get_sender_id()}", {})
            now = time.time()
            if not is_sticker:
                occ = _stl.classify_group_text(text)
                if occ:
                    ctx["last"] = (now, occ)
                else:
                    ctx.pop("last", None)
                return
            if desc is None or meta is None or getattr(meta, "kind", "") not in ("static", "tgs", "video"):
                return
            if raw is not None and getattr(raw, "fwd_from", None):
                logger.info("[stickers.learn] forwarded group sticker; not learned")
                return
            last = ctx.pop("last", None)
            text_occ = last[1] if last and now - last[0] <= self._STK_CTX_WINDOW else None
            self._sticker_spawn(self._sticker_learn_group_one(raw, meta, str(desc), text_occ, base))
        except Exception as e:
            logger.warning(f"[stickers.learn] group observe failed: {type(e).__name__}")

    async def _sticker_group_reply_kind(self, raw) -> tuple:
        """主人这张贴纸直接回复的是谁：("bot" | "member" | None, 被回复的那条是否 @ 了主人)。不保存任何群友信息。"""
        rt = getattr(raw, "reply_to", None) if raw is not None else None
        if rt is None:
            return None, False
        if getattr(rt, "forum_topic", False) is True and not getattr(rt, "reply_to_top_id", None):
            return None, False  # 只是论坛话题根，不是回复某条消息
        try:
            replied = await raw.get_reply_message()
        except Exception:
            return None, False
        if replied is None:
            return None, False
        owners = set(self._owner_id_list())
        sid = str(getattr(replied, "sender_id", "") or "")
        mentioned = False
        for ent in getattr(replied, "entities", None) or []:
            if type(ent).__name__ in ("MessageEntityMentionName", "InputMessageEntityMentionName"):
                uid = getattr(ent, "user_id", None)
                uid = getattr(uid, "user_id", uid)
                if str(uid) in owners:
                    mentioned = True
        self_id = str(getattr(self, "_self_id", "") or "")
        if not self_id:
            return None, False  # 不知道她自己的 id：无法区分回她还是回别人，不判定（审查 U20-6）
        if sid == self_id:
            return "bot", mentioned
        if sid in owners or not sid:
            return None, False
        return "member", mentioned

    async def _sticker_learn_group_one(self, raw, meta, desc: str, text_occ, group_key: str = "") -> None:
        try:
            kind, mentioned = await self._sticker_group_reply_kind(raw)
        except Exception:
            kind, mentioned = None, False
        occ = _stl.group_occasion(text_occ, kind, mentioned)
        await self._sticker_learn_one(raw, meta, desc, occ, scene="group", group_key=group_key)

    def _sticker_spawn(self, coro) -> None:
        tasks = getattr(self, "_sticker_learn_tasks", None)
        if tasks is None:
            tasks = self._sticker_learn_tasks = set()
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def _sticker_learn_one(
        self, raw, meta, desc: str, occasion: str, scene: str = "private", group_key: str = ""
    ) -> None:
        lib = getattr(self, "_sticker_library", None)
        if lib is None:
            return
        lock = getattr(self, "_sticker_learn_lock", None)
        if lock is None:
            lock = self._sticker_learn_lock = asyncio.Lock()
        async with lock:
            try:
                doc_id = int(meta.doc_id)
                if await asyncio.to_thread(lib.is_tombstoned, doc_id):
                    return
                in_group = scene == "group"
                existing = await asyncio.to_thread(lib.learned_by_doc, doc_id)
                if existing is not None:
                    if in_group:
                        # 主人在这个群亲自用过：记按群的证据；有证据时的判定 group_ok_owner 只在该群生效
                        ok_owner = _stl.group_verdict(desc, True)
                        await asyncio.to_thread(
                            lambda: lib.learn_touch(
                                doc_id, raw, occasion, scene="group",
                                group_key=group_key, group_ok_owner=ok_owner,
                            )
                        )
                    else:
                        await asyncio.to_thread(lib.learn_touch, doc_id, raw, occasion)
                    return
                if await asyncio.to_thread(lib.learn_paused):
                    return
                if await asyncio.to_thread(lib.manual_has_doc, doc_id):
                    return
                emoji = str(getattr(meta, "emoji", "") or "")
                tag = _stl.derive_tag(emoji, desc)
                note = _stl.default_note(tag) if tag else ""
                if tag is None:
                    got = await self._sticker_label_llm(emoji, desc)
                    if got is None:
                        logger.info("[stickers.learn] no label derived; skipped")
                        return
                    tag, note = got
                cap = self.config.get("lifelike_sticker_learn_max", 60)
                try:
                    cap = max(10, min(200, int(cap)))
                except (TypeError, ValueError):
                    cap = 60
                status, _ = await asyncio.to_thread(
                    lambda: lib.learn_add(
                        raw,
                        _stl.label_candidates(tag, desc),
                        note,
                        occasion=occasion,
                        group_ok=_stl.group_verdict(desc, False),
                        group_ok_owner=_stl.group_verdict(desc, True),
                        set_id=getattr(meta, "set_id", None),
                        cap=cap,
                        scene="group" if in_group else "private",
                        group_key=group_key,
                    )
                )
                if status not in ("added", "touched"):
                    logger.info(f"[stickers.learn] not learned status={status}")
            except Exception as e:
                logger.warning(f"[stickers.learn] learn failed: {type(e).__name__}")

    def _sticker_note_bot_reply(self, event) -> None:
        """她在主人私聊里刚发出的回复在夸主人 → 记“被夸时”（只存短语，不存原话）。"""
        try:
            if not self._sticker_learn_on() or not self._is_owner_private(event):
                return
            if not _int_ids(event.get_extra(_SENT_IDS_EXTRA)):
                return
            result = event.get_result()
            text = result.get_plain_text() if result is not None else ""
            occ = _stl.classify_bot_text(text or "")
            if occ:
                self._stk_ctx().setdefault(f"private:{event.get_sender_id()}", {})["last"] = (time.time(), occ)
        except Exception:
            pass

    _STICKER_LABEL_MODULE = "lifelike_sticker_learn"
    _STICKER_LABEL_DAILY = 10

    async def _sticker_label_llm(self, emoji: str, desc: str):
        """规则推不出标签：一次廉价文本模型调用（输入只有画面描述与 emoji），走活人感预算 + 模块日上限。"""
        svc = getattr(self, "_continuity", None)
        budget = getattr(svc, "cheap_budget", None) if svc is not None else None
        llm = getattr(self, "_llm_manager", None)
        if budget is None or llm is None:
            return None
        from .iris_memory.lifelike.budget import CheapBudgetExceeded

        try:
            counters = await asyncio.to_thread(svc.store.get_counters)
            if int(counters.get(f"lifelike.budget.calls:{self._STICKER_LABEL_MODULE}", 0)) >= self._STICKER_LABEL_DAILY:
                logger.info("[stickers.learn] label llm skipped: module daily cap")
                return None
            provider = None
            try:
                provider = svc.background_provider_id()
            except Exception:
                provider = None

            async def _gen(prompt: str, timeout: float) -> str:
                return await asyncio.wait_for(
                    llm.generate(prompt, module=self._STICKER_LABEL_MODULE, provider_id=provider, timeout=timeout),
                    timeout + 5.0,
                )

            raw = await budget.guarded_generate(
                self._STICKER_LABEL_MODULE,
                _stl.label_prompt(emoji, desc),
                _gen,
                20.0,
                output_reserve=80,
            )
        except CheapBudgetExceeded as e:
            logger.info(f"[stickers.learn] label llm skipped: budget {e.reason}")
            return None
        except Exception as e:
            logger.info(f"[stickers.learn] label llm failed: {type(e).__name__}")
            return None
        return _stl.parse_label_json(raw)

    async def _sticker_dont_learn(self, event, raw) -> bool:
        """主人回复一张贴纸说“别学这个 / 这个别用”：墓碑 + 删掉自动条目。返回是否处理了。"""
        lib = getattr(self, "_sticker_library", None)
        if lib is None or raw is None or getattr(raw, "reply_to", None) is None:
            return False
        try:
            replied = await raw.get_reply_message()
        except Exception:
            return False
        doc = getattr(replied, "sticker", None) if replied is not None else None
        doc_id = getattr(doc, "id", None)
        if doc_id is None:
            return False
        removed = await asyncio.to_thread(lib.tombstone_doc, int(doc_id))
        manual = await asyncio.to_thread(lib.disable_manual_doc, int(doc_id))
        event.set_extra("iris_sticker_dont_learn", "manual" if manual else ("done" if removed else "never"))
        logger.info(f"[stickers.learn] owner said don't learn; removed={int(bool(removed))} manual={int(manual)}")
        return True

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("sticker")
    async def sticker_cmd(self, event: AstrMessageEvent):
        """主人在私聊管理表情包库：/sticker add <标签> [说明]（回复一张贴纸）| list | del | scope | learn on|off | help"""
        lib = getattr(self, "_sticker_library", None)
        if lib is None:
            yield event.plain_result("表情包功能未启用。")
            return
        raw = getattr(event.message_obj, "raw_message", None)
        replied = None
        try:
            if raw is not None and getattr(raw, "reply_to", None) is not None:
                replied = await raw.get_reply_message()
        except Exception:
            replied = None
        parts = (event.message_str or "").strip().lstrip("/").split()
        args = parts[1:] if parts and parts[0].lower() == "sticker" else parts
        try:
            try:
                extra = {"unified": True} if self._sticker_cfg().sticker_scope_unified else {}
            except Exception:
                extra = {}
            if self._sticker_learn_on():
                extra["learning"] = True
            text = await asyncio.to_thread(
                lib.handle_command,
                args,
                replied,
                str(event.get_sender_id()),
                self._owner_id_list(),
                is_group=bool(event.get_group_id()),
                **extra,
            )
        except Exception as e:
            logger.warning(f"[stickers] command failed: {type(e).__name__}")
            text = "操作失败了，稍后再试。"
        yield event.plain_result(text)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event) -> None:
        """主动回复消息唤醒：门控 → 标记 → 交由 on_llm_request 决策"""
        # 连续性：主人私聊水位 / 暂停恢复 / 事项收件箱（与主动回复开关无关）
        await self._continuity_on_owner_message(event)

        if await self._prepare_pure_at_request(event):
            return

        if not self._reply_config.enabled:
            return

        gf_group = self._gf_event_group(event)
        if gf_group:
            # 活人感 U9：白名单群自由搭话（静音 / 质量分 / 采样 / 仅点名等门控不再适用）
            await self._gf_on_message(event, gf_group)
            return

        if not self._gatekeeper.should_process(event):
            return

        group_id = self._get_session_key(event)
        if not group_id:
            return

        # 私聊首次通过门控后登记到白名单，调度循环才能遍历到该会话
        if event.is_private_chat() and not self._state.is_whitelisted(group_id):
            self._state.add_to_whitelist(group_id)
            await self._state.save_dirty(self._kv_save)

        # 缓存会话标识与自身 ID，供主动发起通路使用
        umo = getattr(event, "unified_msg_origin", "")
        if umo and self._group_umo.get(group_id) != umo:
            self._group_umo[group_id] = umo
            self._umo_dirty = True
        if not self._self_id:
            self._self_id = event.get_self_id() or ""

        message_str = event.message_str or ""
        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name() or sender_id

        # 发起后的首次接话：清除 pending，该消息直接获得一次跟进评估资格
        pending_reply = self._state.consume_initiate_pending(group_id)
        is_followed = bool(sender_id and self._state.match_anchor_user(group_id, sender_id))

        score = self._gatekeeper.quality_score(message_str)
        if score < self._reply_config.quality_threshold and not is_followed and not pending_reply:
            return

        message_timestamp = time.time()
        self._sliding_window.append(
            group_id,
            WindowMessage(
                sender_id=sender_id,
                sender_name=sender_name,
                content=message_str,
                timestamp=message_timestamp,
            ),
        )
        # 每条真实群消息都会使旧主动候选失效，并为该群独立重新预约。
        self._proactive.notify_human_message(group_id, message_timestamp)

        if event.is_at_or_wake_command:
            if not event.is_private_chat() and str(sender_id) not in set(self._owner_id_list()):
                # 群友明确找她：开放有限时长的跟进窗口
                self._signals.mark_engaged(group_id, sender_id, message_timestamp)
            self._release_decision_ticket(group_id)
            self._state.increment_msg_count(group_id)
            self._passive_active[group_id] = time.time()
            event.set_extra("iris_mode", "passive")
            return

        self._release_undecided_claim(group_id, message_timestamp)
        if self._is_busy(group_id) or self._proactive.is_initiating(group_id):
            logger.info(
                f"Iris Reply: group {group_id} busy, message not evaluated "
                f"(reply_in_progress={group_id in self._reply_in_progress}, "
                f"triggering={group_id in self._triggering}, "
                f"passive={group_id in self._passive_active})"
            )
            return

        trigger = ""
        async with self._state.get_lock(group_id):
            if event.is_private_chat():
                motive = self._signals.evaluate_message(group_id, sender_id, message_str)
            else:
                motive, trigger = self._signals.evaluate_group_message(
                    group_id,
                    sender_id,
                    message_str,
                    set(self._owner_id_list()),
                    message_timestamp,
                )

        if not motive and pending_reply:
            motive = "follow_up"
        if not motive:
            return

        is_follow_up = motive == "follow_up"

        if is_follow_up:
            if group_id in self._follow_pending:
                logger.debug(f"Iris Reply: follow-up aggregation pending for group {group_id}")
                return
            self._follow_pending.add(group_id)
            try:
                await asyncio.sleep(self._reply_config.follow_up_aggregate_window)
            finally:
                self._follow_pending.discard(group_id)

            if self._is_busy(group_id):
                return
            if not pending_reply and not self._state.get_anchor(group_id).active:
                return

        if not self._state.can_detect(group_id, follow_up=is_follow_up):
            logger.debug(f"Iris Reply: trigger rate-limited for group {group_id}")
            return

        provider_id = self._reply_config.provider_id
        if not provider_id:
            provider_id = await self._get_provider_id(event)
            if not provider_id:
                logger.error(f"Iris Reply: failed to get provider ID for group {group_id}")
                return

        async with self._state.get_lock(group_id):
            if group_id in self._triggering:
                logger.debug(f"Iris Reply: trigger already in progress for group {group_id}")
                return
            event_id = self._decision_event_id(event)
            registry = getattr(self, "_decision_tickets", None)
            ticket = registry.claim(group_id, event_id) if registry else None
            if registry is not None and ticket is None:
                logger.debug(
                    f"Iris Reply: duplicate decision event rejected for group {group_id}, "
                    f"event={event_id}"
                )
                return
            self._state.record_detect_time(group_id)
            self._triggering[group_id] = time.time()

        event.set_extra("iris_decision", {
            "motive": motive,
            "session_key": group_id,
            "trigger": trigger,
            "trigger_user": str(sender_id or ""),
            "provider_id": provider_id,
            "event_id": event_id,
            "ticket_id": ticket.ticket_id if ticket else "",
        })

        event.is_at_or_wake_command = True
        event.is_wake = True
        if provider_id:
            event.set_extra("selected_provider", provider_id)
        self._tool_ctx.set_context(group_id)
        logger.info(
            f"Iris Reply: {motive} candidate activated for group {group_id}"
            f"{f' (trigger={trigger})' if trigger else ''}, deferred to on_llm_request"
        )

    # ---- 活人感 U9：白名单群自由搭话 ----

    def _gf_eligible(self, group_id: str) -> bool:
        """平台 id_whitelist 里的群（去掉私聊 id、主人、主人频道、silent_sessions、内置禁发频道）。

        白名单未启用 / 为空（= 全部允许）时不据此扩大范围 → False。结果缓存 60 秒。"""
        base = str(group_id or "").split("#", 1)[0].strip()
        if not base or base.startswith("private:"):
            return False
        cache = getattr(self, "_gf_cache", None)
        if cache is None:
            cache = self._gf_cache = {}
        now = time.time()
        hit = cache.get(base)
        if hit is not None and now - hit[0] < _GF_ELIGIBLE_TTL:
            return hit[1]
        ok = False
        try:
            from .iris_memory.lifelike.social.l2_filter import chat_variants, whitelist_group_ids

            conf = self.context.get_config()
            ps = conf.get("platform_settings", {}) or {}
            wl = (ps.get("id_whitelist") or []) if ps.get("enable_id_white_list", True) else []
            try:
                channels = list(ContinuityConfig.from_config(self.config).owner_channel_ids or ())
            except Exception:
                channels = []
            ids = whitelist_group_ids([str(x) for x in wl], exclude=[*self._owner_id_list(), *channels])
            variants = chat_variants(base)
            if any(variants & chat_variants(str(i)) for i in ids):
                silent = self.config.get("proactive.silent_sessions", []) or []
                ok = not is_silent_or_forbidden(base, silent)
        except Exception as e:
            logger.warning(f"[lifelike.group_freedom] eligibility check failed: {type(e).__name__}")
            ok = False
        cache[base] = (now, ok)
        return ok

    def _gf_event_group(self, event) -> str:
        """该事件属于开启了自由搭话的白名单群 → 返回会话键；否则 ""（= 原有门控）。"""
        gf = getattr(self, "_gf", None)
        if gf is None:
            return ""
        try:
            if event.is_private_chat() or not gf.enabled():
                return ""
            group_id = event.get_group_id()
            if not group_id:
                return ""
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            if raw is not None and getattr(raw, "post", None) is True:
                return ""  # 审查 L4：广播频道帖子（成员不能发言）不算
            return str(group_id) if gf.active(str(group_id)) else ""
        except Exception:
            return ""

    def _gf_last_self(self, group_id: str) -> float:
        last = 0.0
        try:
            last = self._gf.tracker.last_self_at(group_id)
            self_id = str(self._self_id or "")
            for msg in reversed(self._sliding_window.get_messages(group_id)):
                if msg.sender_name == "我" or (self_id and str(msg.sender_id) == self_id):
                    last = max(last, float(msg.timestamp))
                    break
        except Exception:
            pass
        return last

    async def _gf_check_hush(
        self, group_id: str, text: str, *, is_owner: bool, addressed: bool, now: float
    ) -> None:
        """群里叫她安静 / 别说话 → 暂停主动搭话（审查 L3：群友只认 @ / 回复她 / 叫名字；主人说的都算）。

        主人默认 6 小时、群友默认 2 小时，立即落盘；主人说"可以说话了"解除。只判规则，不存正文。"""
        try:
            cfg = self._gf.cfg()
            current = self._state.get_proactive_hush(group_id)
            changed = False
            if (addressed or is_owner) and _gfm.is_group_hush(text):
                hours = cfg.hush_owner_hours if is_owner else cfg.hush_member_hours
                until = now + hours * 3600.0
                if until > current:
                    self._state.set_proactive_hush(group_id, until)
                    changed = True
                logger.info(
                    f"[lifelike.group_freedom] scope={group_id} op=hush who={'owner' if is_owner else 'member'} "
                    f"hours={hours:.1f}"
                )
            elif is_owner and current > now and _gfm.is_group_unhush(text):
                self._state.set_proactive_hush(group_id, 0.0)
                changed = True
                logger.info(f"[lifelike.group_freedom] scope={group_id} op=unhush")
            if changed:
                await self._state.save_dirty(self._kv_save)
        except Exception as e:
            logger.warning(f"[lifelike.group_freedom] hush check failed: {type(e).__name__}")

    async def _gf_on_message(self, event, group_id: str) -> None:
        """U9 群消息入口：入窗 → 叫停 / 恶意初筛 → @/回复走被动；叫名字、恶意立即决策；其余进入去抖。

        每条消息都推进去抖序号（审查 M2）：任何新消息都会让更早的等待让位，同一段对话只决策一次。"""
        message_str = event.message_str or ""
        if not message_str or message_str.startswith("/"):
            return
        if self._is_self_message(event):
            return
        umo = getattr(event, "unified_msg_origin", "")
        if umo and self._group_umo.get(group_id) != umo:
            self._group_umo[group_id] = umo
            self._umo_dirty = True
        if not self._self_id:
            self._self_id = event.get_self_id() or ""

        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name() or sender_id
        now = time.time()
        is_owner = bool(sender_id) and str(sender_id) in set(self._owner_id_list())
        name_hit = self._signals.mentions_bot_name(message_str)
        addressed = bool(event.is_at_or_wake_command) or name_hit
        counter = self._counter_check(
            event, group_id, message_str, str(sender_id or ""), is_owner=is_owner, addressed=addressed, now=now
        )
        if not (counter is not None and counter.get("level") == _hs.LEVEL_HOSTILE and not is_owner):
            # 带辱骂的"闭嘴"不当作礼貌的叫停（否则恶意的人一句话就能让她闭麦）；主人说的照样算
            await self._gf_check_hush(group_id, message_str, is_owner=is_owner, addressed=addressed, now=now)

        self._state.consume_initiate_pending(group_id)
        self._sliding_window.append(
            group_id,
            WindowMessage(sender_id=sender_id, sender_name=sender_name, content=message_str, timestamp=now),
        )
        self._gf.tracker.note_human(group_id, now, addressed=addressed)
        self._proactive.notify_human_message(group_id, now)
        seq = self._gf.tracker.next_seq(group_id)

        if counter is not None and counter.get("plan") == "ignore":
            return  # 纠缠超过轮数 / 本群反击次数到上限：不再理会（@ 的事件在 on_llm_request 里停掉）
        if event.is_at_or_wake_command:
            self._release_decision_ticket(group_id)
            self._state.increment_msg_count(group_id)
            self._passive_active[group_id] = time.time()
            event.set_extra("iris_mode", "passive")
            return

        self._release_undecided_claim(group_id, now)
        if counter is not None and counter.get("level") == _hs.LEVEL_HOSTILE:
            # 明确冲她（叫名字）/ 回复主人并冲主人的恶意：不等去抖，视同被点名
            event.set_extra(_GF_WAIT_EXTRA, (group_id, seq, str(sender_id or ""), "hostile"))
            return
        if name_hit:
            # 叫她名字：不去抖、不受硬底线 / 叫停 / 她自己的暂停影响
            event.set_extra(_GF_WAIT_EXTRA, (group_id, seq, str(sender_id or ""), "name"))
            return
        if self._state.get_proactive_hush(group_id) > now:
            logger.debug(f"[lifelike.group_freedom] scope={group_id} hushed, burst not evaluated")
            return
        event.set_extra(_GF_WAIT_EXTRA, (group_id, seq, str(sender_id or ""), "free"))

    # ---- 被恶意对待时反击 ----

    def _counter_on(self) -> bool:
        try:
            return bool(self._reply_config.counter_mode)
        except Exception:
            return False

    @staticmethod
    def _reply_sender(event) -> str:
        try:
            for comp in event.get_messages() or []:
                if type(comp).__name__ == "Reply":
                    return str(getattr(comp, "sender_id", "") or "")
        except Exception:
            pass
        return ""

    def _counter_check(
        self, event, group_id: str, text: str, sender_id: str, *, is_owner: bool, addressed: bool, now: float
    ) -> Optional[dict]:
        """规则初筛 + 对象判定（审查 H1）；命中写入事件 extra（iris_counter / iris_counter_ignore）。日志不含正文。

        - 冲她：只认 @ / 回复她 / 叫名字（不再用"她刚说过话"推断）；
        - 冲主人：回复主人的消息，且是带第二人称的明确恶意（hostile）；
        - maybe：只作为中性事实（"语气有点冲，玩笑就接梗"），不改变节奏、不计轮数；
        - hostile：同一人 30 分钟内最多 N 轮、每群 10 分钟内最多 GROUP_CAP 次，超过即不再理会（主人除外）。"""
        if not self._counter_on():
            return None
        try:
            level = _hs.classify(text)
            if not level:
                return None
            owners = set(self._owner_id_list())
            reply_to = self._reply_sender(event)
            toward_her = bool(addressed)
            toward_owner = (
                (not is_owner)
                and level == _hs.LEVEL_HOSTILE
                and bool(reply_to)
                and reply_to in owners
                and _hs.second_person(text)
            )
            if not (toward_her or toward_owner):
                return None
            if level == _hs.LEVEL_MAYBE:
                plan = "neutral"
            elif is_owner:
                plan = "owner"
            else:
                done = self._counter_tracker.rounds(group_id, sender_id, now)
                plan = _hs.round_plan(done, self._reply_config.counter_max_rounds)
                if plan != "ignore" and self._counter_tracker.group_count(group_id, now) >= _hs.GROUP_CAP:
                    plan = "ignore"
            info = {
                "target": "owner" if (toward_owner and not toward_her) else "her",
                "plan": plan,
                "level": level,
                "sender": sender_id,
                "owner_sender": bool(is_owner),
            }
            if plan == "ignore":
                event.set_extra("iris_counter_ignore", True)
            else:
                event.set_extra("iris_counter", info)
            logger.info(
                f"[lifelike.counter] scope={group_id} level={level} target={info['target']} plan={plan}"
            )
            return info
        except Exception as e:
            logger.warning(f"[lifelike.counter] check failed: {type(e).__name__}")
            return None

    def _counter_hint(self, event) -> str:
        """回复提示：明确恶意 → 反击提示；语气冲但可能是玩笑 → 中性提示；主人说重话 → 不记仇提示；其余 ""。"""
        if not self._counter_on():
            return ""
        try:
            info = event.get_extra("iris_counter")
            if info:
                if info.get("plan") == "neutral":
                    return COUNTER_NEUTRAL_HINT
                if info.get("owner_sender"):
                    return OWNER_HARSH_HINT
                from .iris_memory.identity import owner_display as _owner_display

                owner_name = _owner_display()
                target = owner_name if info.get("target") == "owner" else "你"
                return COUNTER_SPEAK_HINT.format(
                    target=target,
                    defend_line=COUNTER_DEFEND_LINE.format(owner=owner_name) if info.get("target") == "owner" else "",
                    round_line=COUNTER_LAST_ROUND_LINE if info.get("plan") == "last" else "",
                )
            if event.is_private_chat() and str(event.get_sender_id()) in set(self._owner_id_list()):
                # 审查 M4：只有带第二人称、冲她的明确恶意才算"对她说重话"；骂第三方不算
                text = event.message_str or ""
                if _hs.classify(text) == _hs.LEVEL_HOSTILE and _hs.second_person(text):
                    return OWNER_HARSH_HINT
        except Exception as e:
            logger.warning(f"[lifelike.counter] hint failed: {type(e).__name__}")
        return ""

    @staticmethod
    def _counter_full_guard(event) -> bool:
        """完整底线的适用范围（最终复核）：只有当前这条消息本身被判为明确恶意、且对象是她或主人时。
        回击过的人之后的普通消息走全局兜底（删空退回原文），不会换成冷硬的收尾句。"""
        info = event.get_extra("iris_counter") or {}
        return bool(info) and info.get("level") == _hs.LEVEL_HOSTILE and not info.get("owner_sender") \
            and info.get("target") in ("her", "owner")

    def _counter_guard(self, event, result) -> bool:
        """发送前底线（审查 H2 / 复核 N1），反击开关打开时：

        - 反击回复：完整底线（侮辱 / 脏话 / 诅咒、身份攻击、威胁、人肉 / 隐私、性内容、煽动围攻），
          命中的句子删掉；删空换一句不越线的收尾；
        - 其他所有回复：只查"第二人称直接辱骂 / 脏话"这一窄类；只删命中的那句，删空就退回原文并记日志，
          绝不静默不发。

        返回 True 表示整条回复已被拦下（目前不会发生，保留给调用方的统一接口）。日志只记规则名。"""
        if not self._counter_on():
            return False
        try:
            is_llm = getattr(result, "is_llm_result", None)
            if callable(is_llm) and not is_llm():
                return False
            full = self._counter_full_guard(event)
            hits: list[str] = []
            restored = False
            for comp in list(getattr(result, "chain", None) or []):
                if isinstance(comp, Plain) and comp.text:
                    new, ids = _hs.guard_reply(comp.text, fallback=full, narrow=not full)
                    if ids:
                        if not full and new == comp.text:
                            restored = True
                        comp.text = new
                        hits.extend(i for i in ids if i not in hits)
            if hits:
                logger.info(
                    f"[lifelike.counter] floor guard mode={'full' if full else 'narrow'} rules={','.join(hits)}"
                    f"{' kept_original=1' if restored else ''}"
                )
            return False
        except Exception as e:
            logger.warning(f"[lifelike.counter] guard skipped: {type(e).__name__}")
            return False

    # ---- 群友常用称呼 ----

    def _nick_on(self) -> bool:
        try:
            v = self.config.get("lifelike_member_nickname", "off")
        except Exception:
            return False
        return str(v if v is not None else "off").strip().lower() in ("on", "true", "1")

    async def _nick_learned(self, member_id: str) -> Optional[str]:
        cache = getattr(self, "_nick_cache", None)
        if cache is None:
            cache = self._nick_cache = {}
        now = time.time()
        hit = cache.get(member_id)
        if hit is not None and now - hit[0] < 600:
            return hit[1]
        val: Optional[str] = None
        try:
            lm = getattr(getattr(self, "_continuity", None), "lifelike_memory", None)
            members = lm.ledger.members if lm is not None else None
            if members is not None:
                val = await asyncio.to_thread(members.nickname, member_id)
        except Exception:
            val = None
        if len(cache) > 500:
            cache.clear()  # 审查 L5：简单的上限清理
        cache[member_id] = (now, val)
        return val

    async def _nickname_note(self, event) -> str:
        """称呼表：群聊 = 最近发言的群友；主人私聊 = 消息里提到的群友（显示名）。关闭 → ""。"""
        if not self._nick_on():
            return ""
        try:
            owners = set(self._owner_id_list())
            self_id = str(self._self_id or "")
            seen = getattr(self, "_nick_seen", None)
            if seen is None:
                seen = self._nick_seen = {}
            people: list[tuple[str, str]] = []
            if event.is_private_chat():
                if str(event.get_sender_id()) not in owners:
                    return ""
                text = event.message_str or ""
                people = [(d, m) for d, m in list(seen.items()) if d and d in text][:8]
                note_tpl = _nk.NICKNAME_NOTE_PRIVATE
            else:
                group_id = str(event.get_group_id() or "")
                msgs = list(self._sliding_window.get_messages(group_id))[-15:]
                cur = (str(event.get_sender_name() or ""), str(event.get_sender_id() or ""))
                for msg in [*msgs, WindowMessage(cur[1], cur[0], "", 0.0)]:
                    sid = str(msg.sender_id or "")
                    name = str(msg.sender_name or "")
                    if not sid or not name or sid in owners or sid == self_id or name == "我":
                        continue
                    seen[name] = sid
                    if (name, sid) not in people:
                        people.append((name, sid))
                note_tpl = _nk.NICKNAME_NOTE_GROUP
            if not people:
                return ""
            pairs = []
            for display, mid in people[-12:]:
                pairs.append((display, _nk.address_for(display, await self._nick_learned(mid))))
            table = _nk.render_table(pairs)
            return note_tpl.format(table=table, owner_line=_nk.owner_address_line()) if table else ""
        except Exception as e:
            logger.warning(f"[lifelike.member] nickname note skipped: {type(e).__name__}")
            return ""

    def _gf_message_block(self, group_id: str, now: float) -> str:
        data = self._state.get_state(group_id)
        if float(data.proactive_hush_until or 0.0) > now:
            return _gfm.R_HUSH
        if data.state == GroupState.COOLDOWN and data.cooldown_until > now:
            return _gfm.R_SELF_PAUSE
        cfg = self._gf.cfg()
        if self._gf.tracker.breaker_hit(group_id, cfg):
            return _gfm.R_BREAKER
        if not self._sliding_window.get_messages(group_id):
            return _gfm.R_NO_CONTEXT
        return ""

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-5)
    async def on_group_freedom_wait(self, event: AstrMessageEvent) -> None:
        """U9：在记忆侧记录（on_all_message）之后执行。

        - free（群友闲聊）：去抖——等待期间群里又有新消息 → 本事件让位；硬底线未到 → 等到底线结束再判断；
        - name / hostile：立即判断；hostile 遇到正在回复 → 排队等当前回复结束（最多 90 秒）。
        决策在这里直接调用（审查 M3），只有决定开口才唤醒主管线，不会"正在输入"却不说话。"""
        info = event.get_extra(_GF_WAIT_EXTRA)
        if not info:
            return
        event.set_extra(_GF_WAIT_EXTRA, None)
        gf = getattr(self, "_gf", None)
        if gf is None:
            return
        try:
            group_id, seq, sender_id = info[0], info[1], info[2]
            mode = info[3] if len(info) > 3 else "free"
            if mode == "free":
                await asyncio.sleep(gf.cfg().debounce_seconds)
                for _ in range(32):
                    if gf.tracker.current_seq(group_id) != seq:
                        return  # 被更新的消息接替
                    left = gf.tracker.floor_left(group_id, gf.cfg(), time.time())
                    if left <= 0:
                        break
                    await asyncio.sleep(left + 1.0)
                else:
                    return
            elif mode == "hostile":
                for _ in range(45):
                    if not (self._is_busy(group_id) or self._proactive.is_initiating(group_id)):
                        break
                    await asyncio.sleep(2.0)
            if not gf.active(group_id):
                return
            now = time.time()
            if mode == "free":
                reason = self._gf_message_block(group_id, now)
                if reason:
                    logger.info(f"[lifelike.group_freedom] scope={group_id} op=hold reason={reason}")
                    return
            elif mode == "hostile":
                if self._state.get_proactive_hush(group_id) > now:
                    # 审查 M5：叫停期间只回应 @ / 回复她的恶意（那些走被动路径，不会到这里）
                    logger.info(f"[lifelike.counter] scope={group_id} hushed, counter skipped")
                    return
            await self._gf_activate(
                event, group_id, trigger=mode, sender_id=sender_id, unsolicited=(mode == "free")
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[lifelike.group_freedom] wait failed: {type(e).__name__}")

    async def _gf_activate(
        self, event, group_id: str, *, trigger: str, sender_id: str, unsolicited: bool
    ) -> None:
        """认领决策 → 直接决策（与原有 chime_in 同一套认领 / 票据 / 记账逻辑）→ 决定开口才唤醒主管线。"""
        if self._is_busy(group_id) or self._proactive.is_initiating(group_id):
            logger.info(f"[lifelike.group_freedom] scope={group_id} busy, burst not evaluated")
            return
        provider_id = self._reply_config.provider_id
        if not provider_id:
            provider_id = await self._get_provider_id(event)
            if not provider_id:
                logger.error(f"Iris Reply: failed to get provider ID for group {group_id}")
                return
        async with self._state.get_lock(group_id):
            if group_id in self._triggering:
                return
            event_id = self._decision_event_id(event)
            registry = getattr(self, "_decision_tickets", None)
            ticket = registry.claim(group_id, event_id) if registry else None
            if registry is not None and ticket is None:
                return
            self._state.record_detect_time(group_id)
            self._triggering[group_id] = time.time()
        event.set_extra("iris_decision", {
            "motive": "chime_in",
            "session_key": group_id,
            "trigger": trigger,
            "trigger_user": str(sender_id or ""),
            "provider_id": provider_id,
            "event_id": event_id,
            "ticket_id": ticket.ticket_id if ticket else "",
            "gf": True,
            "gf_unsolicited": bool(unsolicited),
        })
        self._tool_ctx.set_context(group_id)
        try:
            if await self._handle_reply_decision(event):
                return  # 不开口 / 失败：记账与释放已在决策里完成，事件已 stop
        except Exception as e:
            # 复核 N4：认领后异常 → 立即释放，不等 120 秒兜底
            self._release_decision_ticket(group_id, ticket.ticket_id if ticket else "")
            self._reply_in_progress.pop(group_id, None)
            event.stop_event()
            logger.warning(f"[lifelike.group_freedom] scope={group_id} decision failed: {type(e).__name__}")
            return
        started = self._reply_in_progress.get(group_id)
        if started is not None:
            try:
                tasks = getattr(self, "_gf_watchdogs", None)
                if tasks is None:
                    tasks = self._gf_watchdogs = set()
                task = asyncio.get_running_loop().create_task(self._gf_reply_watchdog(group_id, started))
                tasks.add(task)  # 保存引用，避免任务被回收；完成时移除
                task.add_done_callback(tasks.discard)
            except Exception:
                pass
        event.set_extra("iris_gf_predecided", True)
        event.is_at_or_wake_command = True
        event.is_wake = True
        event.set_extra("selected_provider", provider_id)
        logger.info(
            f"Iris Reply: chime_in decided to speak for group {group_id} "
            f"(group_freedom, trigger={trigger}), waking main pipeline"
        )

    async def _gf_reply_watchdog(self, group_id: str, started: float) -> None:
        """复核 N4：决定开口后 _GF_REPLY_TIMEOUT 秒内没等到 on_llm_response（主管线被其他插件拦下、出错等）
        就释放"回复进行中"，不让这个群挂到 120 秒的周期清理。"""
        try:
            await asyncio.sleep(_GF_REPLY_TIMEOUT)
        except asyncio.CancelledError:
            return
        if self._reply_in_progress.get(group_id) == started:
            self._reply_in_progress.pop(group_id, None)
            logger.info(f"[lifelike.group_freedom] scope={group_id} reply watchdog released (no LLM response)")

    def _gf_prepare_request(self, req, group_id: str, event=None) -> None:
        """U9 决策请求：群聊自由版 prompt + <facts>（只有事实）+ 聊天欲望调整。"""
        req.group_free = True
        try:
            cfg = self._gf.cfg()
            req.self_pause_cap = cfg.self_pause_cap
            if cfg.chat_drive != "off":
                req.chat_drive = cfg.chat_drive
                req.speak_wins = True
            if req.trigger == "hostile":
                req.speak_wins = True
            humans, addressed = self._gf.tracker.response_state(group_id)
            req.freedom_facts = _gfm.build_facts(
                now=time.time(),
                wake=req.trigger if req.trigger in ("name", "hostile") else "burst",
                messages=self._sliding_window.get_messages(group_id),
                self_id=str(self._self_id or ""),
                owner_ids=self._owner_id_list(),
                last_self_at=self._gf_last_self(group_id),
                humans_since_self=humans,
                addressed_since_self=addressed,
                counter=(event.get_extra("iris_counter") if event is not None else None),
            )
        except Exception as e:
            logger.warning(f"[lifelike.group_freedom] facts failed: {type(e).__name__}")
            req.freedom_facts = f"<facts>\n- 现在：{_fr.fmt_now(time.time())}\n</facts>"

    def _gf_reply_blocked(self, event, result) -> bool:
        """U9 主动搭话的发送前检查：MAN / 讨要回复（不随口吻开关）、同内容去重；命中 → 不发送。"""
        if not event.get_extra("iris_gf_unsolicited"):
            return False
        try:
            text = "\n".join(
                str(getattr(c, "text", "") or "").strip()
                for c in (getattr(result, "chain", None) or [])
                if isinstance(c, Plain)
            ).strip()
            if not text:
                return False
            group_id = str(event.get_group_id() or "")
            from .iris_memory.proactive.proactive import _proactive_man_hits

            reason = ""
            hits = _proactive_man_hits(text, always=True)
            if hits:
                reason = "style_man:" + ",".join(hits)
            elif _fr.nag_hit(text):
                reason = "nag"
            elif _fr.is_duplicate(text, "", self._state.get_freedom_recent(group_id), time.time()):
                reason = "dedupe"
            if not reason:
                return False
            result.chain = []
            event.stop_event()
            logger.info(f"[lifelike.group_freedom] scope={group_id} op=reply_blocked reason={reason}")
            return True
        except Exception as e:
            logger.warning(f"[lifelike.group_freedom] reply check skipped: {type(e).__name__}")
            return False

    def _casual_result(self, result) -> None:
        try:
            is_llm = getattr(result, "is_llm_result", None)
            if callable(is_llm) and not is_llm():
                return
            for comp in getattr(result, "chain", None) or []:
                if isinstance(comp, Plain) and comp.text:
                    comp.text = _casualize(comp.text)
        except Exception as e:
            logger.warning(f"[lifelike.style] casual punctuation skipped: {type(e).__name__}")

    def _gf_note_reply_sent(self, event, group_id: str, bot_text: str) -> None:
        gf = getattr(self, "_gf", None)
        if gf is None or not bot_text:
            return
        try:
            if not gf.active(group_id):
                return
            unsolicited = bool(event.get_extra("iris_gf_unsolicited"))
            info = event.get_extra("iris_counter") or {}
            countered = info.get("level") == _hs.LEVEL_HOSTILE and not info.get("owner_sender")
            # 审查 M5：反击回复同样计入硬底线 / 熔断统计（不计入去重）
            gf.tracker.note_self(group_id, time.time(), unsolicited=unsolicited or countered)
            if unsolicited:
                self._state.note_freedom_sent(
                    group_id, ts=time.time(), topic="", norm_text=_fr.normalize_text(bot_text)
                )
        except Exception as e:
            logger.warning(f"[lifelike.group_freedom] note sent failed: {type(e).__name__}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent) -> None:
        """记忆侧：全类型消息入 L1 缓冲、图片入队"""
        if self.component_manager:
            await handle_user_message(event, self.component_manager)

        # 只记录、不发言的会话（例如 Telegram 广播频道：成员无法发言）
        # （话题群的会话 id 带 #thread 后缀：按所属群比较）
        silent = self.config.get("proactive.silent_sessions", []) or []
        if silent:
            gid = base_chat_id(event.get_group_id() or "")
            if gid and gid in {base_chat_id(x) for x in silent}:
                self._release_claim(event)
                event.stop_event()
                return

        # 纯媒体消息（贴纸/图片/语音，没有实际文字）：先记录，再交给模型判断
        # 要不要回应——贴纸往往只是情绪表达，逐条回应会显得聒噪。
        # 已带群聊决策认领的事件交给决策层（speak/none）判断，不再额外过媒体门控：
        # 否则门控 stop 后认领不会释放，整个群会被锁住。
        if (
            self._is_media_only_message(event)
            and not event.get_extra("iris_decision")
            and not event.get_extra(_GF_WAIT_EXTRA)
            and not await self._should_reply_to_media(event)
        ):
            self._release_claim(event)
            event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-10)
    async def on_continuity_batch(self, event: AstrMessageEvent) -> None:
        """主人私聊连续短消息合批 / “等我说完”（在 Iris 记录 L1 之后执行）。

        被合并的早先事件 stop_event；最后一个事件的 message_str 改为合并文本。
        """
        continuity = getattr(self, "_continuity", None)
        if continuity is None:
            return
        try:
            if not event.is_private_chat() or self._is_self_message(event):
                return
            scope = continuity.owner_scope(event.get_sender_id())
            text = (event.message_str or "").strip()
            if not scope or not text or text.startswith("/"):
                return
        except Exception:
            return
        await continuity.gate_batch(scope, event)

    async def _should_reply_to_media(self, event: AstrMessageEvent) -> bool:
        """让模型判断一条纯媒体消息是否值得回应；判断失败时默认不回应。"""
        manager = None
        if self.component_manager:
            manager = self.component_manager.get_available_component("llm_manager")
        if manager is None:
            return False

        recent = ""
        try:
            buffer = self.component_manager.get_available_component("l1_buffer")
            if buffer is not None:
                from .iris_memory.platform import get_adapter

                adapter = get_adapter(event)
                session_id = adapter.get_session_id(event)
                messages = buffer.get_context(session_id, 6) or []
                recent = "\n".join(
                    f"{getattr(m, 'sender_name', '')}: {getattr(m, 'content', '')}"
                    for m in messages[-6:]
                )
        except Exception:
            recent = ""

        prompt = (
            "你收到一条只含贴纸/图片、没有文字的消息。请判断此刻要不要回一句。\n"
            "原则：贴纸大多只是情绪表达，默认不回；只有当它明显在等你接话、"
            "或与刚聊的内容强相关、或对方连续发来像在求关注时，才需要回应。\n"
            "只输出 JSON，不要解释：{\"reply\": true} 或 {\"reply\": false}\n\n"
            f"最近对话：\n{recent or '（无）'}\n\n"
            f"本条消息：{event.message_str}"
        )
        try:
            provider_id = await self._get_provider_id(event)
            out = await manager.generate(
                prompt,
                module="media_reply_gate",
                provider_id=provider_id or None,
                timeout=30,
            )
        except Exception as exc:
            logger.warning(f"[iris] 媒体回应判断失败，默认不回复：{exc}")
            return False
        return '"reply":true' in out.replace(" ", "").lower()

    @staticmethod
    def _is_media_only_message(event: AstrMessageEvent) -> bool:
        """判断消息是否只有媒体占位符、没有真实文字。"""
        text = (event.message_str or "").strip()
        if not text:
            return True
        try:
            if event.get_extra("iris_sticker") or event.get_extra("iris_photo"):
                text = re.sub(r"\[(?:贴纸|图片)[^\]]*\]", " ", text)
        except Exception:
            pass
        # 去掉 [图片]/[贴纸]/[视频]/[语音]/[文件] 一类占位符
        rest = re.sub(r"\[[^\]]{1,8}\]", " ", text)
        # 去掉 emoji 与标点等非文字符号；中文/字母/数字会保留
        rest = re.sub(r"[\W_]+", "", rest, flags=re.UNICODE)
        return not rest.strip()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("iris_mem")
    async def iris_mem(self, event: AstrMessageEvent) -> None:
        if self.component_manager:
            result = await execute_command(event)
            if result:
                yield event.plain_result(result)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("iris_care")
    async def iris_care_cmd(self, event: AstrMessageEvent):
        """群→私聊关心桥（只在主人私聊响应）：/iris_care status | why [n] | on | off [purge] | mute <类别> | unmute <类别>"""
        svc = getattr(self, "_continuity", None)
        try:
            if svc is None or not event.is_private_chat():
                event.stop_event()  # 群里不响应、不展示，也不交给 LLM
                return
            scope = svc.owner_scope(str(event.get_sender_id() or ""))
            if not scope or not svc.is_open:
                event.stop_event()
                return
            text = await care_commands.handle(
                getattr(svc, "care", None), getattr(svc, "initiative", None), scope, event.message_str or ""
            )
        except Exception as e:
            logger.warning(f"[lifelike.care] command failed: {type(e).__name__}")
            text = "关心桥命令执行失败。"
        if text:
            yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("iris_rel")
    async def iris_rel(self, event: AstrMessageEvent) -> None:
        """活人感 P1-6/P1-11：关系原因 / 撤回 / 解除 / 关系渐进（仅主人私聊；不经 LLM）"""
        svc = getattr(self, "_continuity", None)
        if svc is None:
            yield event.plain_result("连续性服务没有启动。")
            return
        try:
            if not event.is_private_chat():
                yield event.plain_result("这个命令只在私聊里用。")
                return
            scope = svc.owner_scope(str(event.get_sender_id() or ""))
            if not scope:
                yield event.plain_result("这个命令只在主人私聊里用。")
                return
            msg_id, _msg_ts, _reply_to = _message_meta(event)
            text = await svc.relation_command(scope, event.message_str or "", msg_id)
        except Exception as e:
            logger.warning(f"[lifelike.relation] command failed: {type(e).__name__}")
            text = ""
        yield event.plain_result(text or "没有执行成功。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("iris_member")
    async def iris_member(self, event: AstrMessageEvent) -> None:
        """活人感 P1-13：群友印象查看/删除（仅主人私聊；群里不响应、不展示任何条目）。

        回执只回给主人，不写 L1 / outbound / learning。
        """
        from .iris_memory.lifelike.memory.service import get_current

        try:
            if event.get_group_id() or not event.is_private_chat():
                event.stop_event()  # 群里不响应、不展示，也不交给 LLM
                return
            sender = str(event.get_sender_id() or "")
            svc = getattr(self, "_continuity", None)
            if svc is None or sender not in svc.owner_ids():
                event.stop_event()
                return
            lm = get_current()
            if lm is None:
                yield event.plain_result("群友认知模块不可用。")
                return
            text = (event.message_str or "").strip()
            parts = text.split()
            if parts and parts[0].lstrip("/").lower() == "iris_member":
                parts = parts[1:]
            yield event.plain_result(await lm.member_command(sender, parts))
        except Exception as e:
            logger.warning(f"[lifelike.member] command failed: {type(e).__name__}")
            yield event.plain_result("执行失败，详见服务日志。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("memory")
    async def memory_legacy_alias(self, event: AstrMessageEvent) -> None:
        """旧版 /memory 指令指引别名

        v2 插件的 /memory 指令在本插件中已更名为 /iris_mem。旧指令输入
        此前不会被任何处理器响应（无反馈、无删除），迁移用户容易误以为
        清除已生效。此处仅返回迁移指引，不执行任何操作。
        """
        yield event.plain_result(
            "ℹ️ /memory 指令已迁移到 /iris_mem\n"
            "按层清除：/iris_mem l1|l2|l3 clear [--group|--all|@用户]\n"
            "全部清除：/iris_mem all clear [--group|--all]\n"
            "完整用法：/iris_mem help"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("iris_life")
    async def iris_life(self, event: AstrMessageEvent) -> None:
        """活人感 P1-5：生活系统查看 / 撤回 / 手动生成（仅主人私聊响应，群里不展示）"""
        svc = getattr(self, "_continuity", None)
        world = getattr(svc, "world", None) if svc is not None else None
        try:
            if world is None or not svc.is_open:
                return
            if svc.owner_scope(event.get_sender_id()) != self._session_key_quiet(event):
                event.stop_event()  # 群里 / 非主人会话不响应，也不交给 LLM
                return
            args = (event.message_str or "").split()
            if args and args[0].lstrip("/").startswith("iris_life"):
                args = args[1:]
            sub = args[0].lower() if args else "today"
            if sub == "undo" and len(args) >= 2:
                text = await world.undo(args[1])
            elif sub in ("plan-now", "plan_now"):
                text = await world.plan_now()
            elif sub == "today":
                text = await world.today_text()
            else:
                text = "用法：/iris_life today | undo <事件id> | plan-now"
        except Exception as e:
            logger.warning(f"[lifelike.world] command failed: {type(e).__name__}")
            text = "生活系统命令执行失败。"
        yield event.plain_result(text)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        # 0. 活人感 P0-1：先收口旁路出站工具（与后续步骤是否 stop/抛错无关）
        stripper = getattr(self, "_bypass_stripper", None)
        if stripper is not None:
            stripper.apply(req)
        elif not getattr(self, "_bypass_missing_warned", False):
            self._bypass_missing_warned = True
            logger.warning("[lifelike.outbound] bypass stripper missing; tools not stripped")
        # 0. lifelike P0-2：core 已解析人格，此时写入友好报错 extra 才不会被覆盖
        self._apply_error_friendly_extra(event)

        # 1. 主动回复统一决策（仅处理 _triggering 中的群；可能 stop_event 终止请求）
        #    U9 已在 on_group_freedom_wait 里决策过的事件不再决策一次
        if not event.get_extra("iris_gf_predecided") and await self._handle_reply_decision(event):
            return
        if event.get_extra("iris_counter_ignore"):
            # 被同一个人纠缠超过轮数：不再理会（防无限对骂的刷屏安全网）
            event.stop_event()
            logger.info("[lifelike.counter] reply skipped: round limit reached")
            return

        # 2. 记忆侧：被动触发检测 + 上下文接管 + L1/L2/L3/画像注入
        if self.component_manager:
            _detect_passive_trigger(event, req, self.context)
            await handle_pre_request_cleanup(
                event, req, self.context, self.component_manager
            )
            await preprocess_llm_request(event, req, self.component_manager)

        # 2.5 连续性上下文 + 收尾提示（在记忆注入之后、发言提示之前）
        await self._inject_continuity(event, req)
        await self._inject_sticker_prompt(event, req)

        # 3. 主动回复发言提示：决策通过后由 _handle_reply_decision 暂存，
        # 在记忆注入之后追加，保证 LLM 先看到上下文、再看到发言指令
        if event.get_group_id() and not event.is_private_chat():
            note = GROUP_CONTEXT_NOTE.format(
                sender_name=event.get_sender_name() or event.get_sender_id(),
                sender_id=event.get_sender_id(),
            )
            req.extra_user_content_parts.append(TextPart(text=note).mark_as_temp())
        hint = event.get_extra("iris_speak_hint")
        if hint:
            req.extra_user_content_parts.append(TextPart(text=hint).mark_as_temp())
        nick_note = await self._nickname_note(event)
        if nick_note:
            req.extra_user_content_parts.append(TextPart(text=nick_note).mark_as_temp())
        dont = event.get_extra("iris_sticker_dont_learn")
        if dont:
            note = (
                "（系统提示：主人刚回复一张贴纸说别学 / 别用它。这张是主人手动加进表情库的，"
                "已经停用、以后不会再发；想恢复可以重新 /sticker add。简单回应一句就好。）"
                if dont == "manual"
                else (
                    "（系统提示：主人刚回复一张贴纸说别学 / 别用它，已经从你的表情包里去掉、以后也不会再学。简单回应一句就好。）"
                    if dont == "done"
                    else "（系统提示：主人刚回复一张贴纸说别学它。这张你还没学过，已经记下以后不学；简单回应一句，比如“好，以后不学它”。）"
                )
            )
            req.extra_user_content_parts.append(TextPart(text=note).mark_as_temp())
        counter_hint = self._counter_hint(event)
        if counter_hint:
            req.extra_user_content_parts.append(TextPart(text=counter_hint).mark_as_temp())
        if event.get_extra("iris_gf_unsolicited"):
            # 审查 L1：一阵消息里不只最后一个人在说话，正文换成中性说明（仍带上最后一条，避免丢内容）
            try:
                req.prompt = GF_NEUTRAL_PROMPT.format(
                    name=event.get_sender_name() or event.get_sender_id(),
                    text=(event.message_str or "")[:200],
                )
            except Exception:
                pass

        # 所有 AstrBot 主管线请求也必须取得 Governor lease。此处刻意位于
        # 查询改写/图片关联解析等预处理之后，避免单并发配置下嵌套申请死锁。
        mode = event.get_extra("iris_mode")
        if self._llm_manager:
            provider_id = event.get_extra("iris_llm_provider_id") or ""
            if not provider_id:
                provider_id = await self._get_provider_id(event) or ""
            module = (
                proactive_reply_module(mode)
                if mode in ("chime_in", "follow_up", "passive")
                else FRAMEWORK_REPLY
            )
            lease = await self._llm_manager.acquire_framework_lease(
                module=module,
                provider_id=provider_id,
            )
            try:
                # 先把 lease ID 写入事件，确保后续任一步骤抛错时都能在
                # 当前栈内释放，而不必等待 watchdog 回收。
                event.set_extra("iris_llm_lease_id", lease.lease_id)
                await self._llm_manager.record_framework_attempt(module)
                event.set_extra(
                    "iris_llm_tracking",
                    {
                        "module": module,
                        "provider_id": provider_id,
                        "started_at": time.time(),
                        "prompt": getattr(req, "prompt", "") or "",
                        "priority": lease.priority.name,
                        "queue_wait_ms": lease.queue_wait_ms,
                        "in_flight_at_start": lease.in_flight_at_start,
                        "queue_depth_at_start": lease.queue_depth_at_start,
                    },
                )
            except Exception:
                event.set_extra("iris_llm_lease_id", None)
                await self._llm_manager.release_framework_lease(
                    lease.lease_id,
                    provider_id=provider_id,
                    success=False,
                )
                raise

    async def _handle_reply_decision(self, event: AstrMessageEvent) -> bool:
        """主动回复统一决策执行点。

        Returns:
            True 表示已调用 event.stop_event()，调用方应立即返回，
            不再进行记忆注入。
        """
        group_id = event.get_group_id()
        if not group_id or group_id not in self._triggering:
            return False

        info = event.get_extra("iris_decision")
        if not info:
            self._release_decision_ticket(group_id)
            return False

        motive = info.get("motive", "")
        provider_id = info.get("provider_id", "")
        ticket_id = str(info.get("ticket_id") or "")
        event_id = str(info.get("event_id") or "")
        registry = getattr(self, "_decision_tickets", None)
        if registry is not None:
            active = registry.get(group_id)
            # 旧测试/升级中的遗留事件没有 ticket_id，继续走兼容路径；生产事件
            # 一旦携带 ticket，就必须同时匹配 ticket_id + event_id。
            if ticket_id and not registry.owns(group_id, ticket_id, event_id):
                logger.warning(
                    f"Iris Reply: stale decision ticket rejected for group {group_id}, "
                    f"event={event_id}"
                )
                event.stop_event()
                return True
            if active is not None and not ticket_id:
                logger.warning(
                    f"Iris Reply: decision event without owner ticket rejected for group {group_id}"
                )
                event.stop_event()
                return True

        req = DecisionRequest(
            group_id=group_id,
            wake="message",
            motive=motive,
            trigger=str(info.get("trigger") or ""),
            trigger_user=str(info.get("trigger_user") or ""),
        )
        gf = bool(info.get("gf")) and getattr(self, "_gf", None) is not None
        if gf:
            self._gf_prepare_request(req, group_id, event)
        outcome = await self._decision_core.decide(req, self._llm_manager, provider_id)

        if outcome.error or outcome.decision is None:
            if outcome.error_kind == "input_content_safety_1026":
                cleanup, cooldown = await self._clear_rejected_reply_context(
                    group_id,
                    outcome.dynamic_context_sources,
                    record_skip=True,
                )
                logger.error(
                    "Iris Reply: decision input rejected by provider safety filter "
                    f"for group {group_id} (1026, retryable=false, "
                    f"dynamic_sources={cleanup.dynamic_source_count}, "
                    f"window_removed={cleanup.window_removed}, "
                    f"observation_cleared={cleanup.observation_cleared}, "
                    f"anchor_cleared={cleanup.anchor_cleared}, "
                    f"cooldown={cooldown}min): "
                    f"{outcome.error}"
                )
            else:
                logger.error(f"Iris Reply: decision LLM call failed for group {group_id}: {outcome.error}")
                async with self._state.get_lock(group_id):
                    self._state.record_skip_reply(group_id)
                await self._state.save_dirty(self._kv_save)
            self._stats.record_decision_error(group_id, motive)
            self._release_decision_ticket(group_id, ticket_id)
            event.stop_event()
            return True

        decision = outcome.decision
        logger.info(
            f"Iris Reply: decision raw for group {group_id} (motive={motive}, "
            f"len={len(outcome.raw_text)}): {outcome.raw_text:.500s}"
        )
        self._stats.record_decision(
            group_id, motive,
            system_prompt=outcome.system_prompt,
            user_prompt=outcome.user_prompt,
            response_text=outcome.raw_text,
            decision=decision,
            duration_ms=outcome.duration_ms,
        )
        logger.info(
            f"Iris Reply: decision parsed for group {group_id}: speak={decision.should_speak}, "
            f"drifted={decision.drifted}, watch={decision.watch}, "
            f"watch_keywords={decision.watch_keywords}, cooldown={decision.cooldown_minutes}"
        )

        async with self._state.get_lock(group_id):
            if decision.observation:
                self._state.set_observation(group_id, decision.observation)

        if decision.parse_failed:
            logger.warning(f"Iris Reply: decision parse failed for group {group_id}")
            async with self._state.get_lock(group_id):
                self._state.record_skip_reply(group_id)
            await self._state.save_dirty(self._kv_save)
            self._release_decision_ticket(group_id, ticket_id)
            event.stop_event()
            return True

        if group_id in self._passive_active:
            logger.info(f"Iris Reply: aborting {motive} for group {group_id}, passive reply in progress")
            async with self._state.get_lock(group_id):
                self._state.record_skip_reply(group_id)
            await self._state.save_dirty(self._kv_save)
            self._release_decision_ticket(group_id, ticket_id)
            event.stop_event()
            return True

        if decision.cooldown_minutes:
            minutes = decision.cooldown_minutes
            if gf:
                # U9：她自己请求的暂停只跳过群友闲聊的评估，且不超过上限
                minutes = min(minutes, req.self_pause_cap)
            async with self._state.get_lock(group_id):
                actual = self._state.set_cooldown(group_id, minutes)
                self._state.record_skip_reply(group_id)
            await self._state.save_dirty(self._kv_save)
            logger.info(f"Iris Reply: decision requested cooldown {actual} min for group {group_id}")
            self._release_decision_ticket(group_id, ticket_id)
            event.stop_event()
            return True

        if decision.drifted and gf and decision.should_speak:
            # U9 + 聊天欲望：旧话题结束但她决定说新的 → 关锚点，照常开口
            async with self._state.get_lock(group_id):
                self._state.close_anchor(group_id)
                self._state.record_drift(group_id)
            await self._state.save_dirty(self._kv_save)
        elif decision.drifted:
            async with self._state.get_lock(group_id):
                self._state.close_anchor(group_id)
                self._state.record_drift(group_id)
            await self._state.save_dirty(self._kv_save)
            logger.info(f"Iris Reply: topic drifted for group {group_id}, anchor closed")
            self._release_decision_ticket(group_id, ticket_id)
            event.stop_event()
            return True

        if decision.watch or decision.watch_keywords:
            if decision.should_speak:
                event.set_extra("iris_pending_watch", (
                    decision.watch, decision.watch_keywords, decision.why,
                ))
            else:
                async with self._state.get_lock(group_id):
                    self._state.add_anchor_watch(
                        group_id,
                        users=decision.watch or None,
                        keywords=decision.watch_keywords or None,
                        reason=decision.why,
                    )
            logger.info(
                f"Iris Reply: decision watch for group {group_id}, users={decision.watch}, "
                f"keywords={decision.watch_keywords}, reason={decision.why} (speak={decision.should_speak})"
            )

        if not decision.should_speak:
            async with self._state.get_lock(group_id):
                self._state.record_skip_reply(group_id)
            await self._state.save_dirty(self._kv_save)
            logger.debug(f"Iris Reply: decision skip for group {group_id}")
            self._release_decision_ticket(group_id, ticket_id)
            event.stop_event()
            return True

        self._reply_in_progress[group_id] = time.time()
        self._release_decision_ticket(group_id, ticket_id)

        event.set_extra("iris_mode", motive)
        event.set_extra("iris_llm_provider_id", provider_id)
        event.set_extra("iris_decision_obs", decision.observation)
        if gf and info.get("gf_unsolicited"):
            event.set_extra("iris_gf_unsolicited", True)

        if motive in ("chime_in", "follow_up"):
            hint = self._build_group_speak_hint(group_id, motive, decision)
        else:
            hint = SPEAK_HINTS.get(motive, SPEAK_HINTS["chime_in"])
        # 发言提示延迟到记忆注入之后追加（见 on_llm_request），
        # 保持「上下文在前、指令在后」的提示顺序
        event.set_extra("iris_speak_hint", hint)
        logger.info(f"Iris Reply: decision speak ({motive}) for group {group_id}")
        return False

    def _build_group_speak_hint(self, group_id: str, motive: str, decision) -> str:
        """群聊接话/跟进：把决策看到的同群窗口与决定交给回复模型（只含本群内容）。"""
        self_id = str(self._self_id or "")
        lines = []
        try:
            messages = list(self._sliding_window.get_messages(group_id))[-_GROUP_HINT_WINDOW:]
        except Exception:
            messages = []
        for msg in messages:
            is_self = (self_id and str(msg.sender_id) == self_id) or msg.sender_name == "我"
            name = "我" if is_self else (msg.sender_name or msg.sender_id)
            content = (msg.content or "").strip().replace("\n", " ")
            if len(content) > 200:
                content = content[:200] + "…"
            lines.append(f"[{name}({msg.sender_id})] {content}")
        window_block = (
            "群里最近的消息（较早在前，[我] 是你自己说的）：\n" + "\n".join(lines) + "\n"
            if lines else ""
        )
        why = (decision.why or decision.observation or "").strip()
        why_block = f"你决定开口的原因：{why}\n" if why else ""
        draft = (decision.message or "").strip()
        draft_block = f"可参考的思路（可以改写，不必照抄）：{draft}\n" if draft else ""
        return GROUP_SPEAK_HINT.format(
            mode_label="接话" if motive == "chime_in" else "跟进",
            window_block=window_block,
            why_block=why_block,
            draft_block=draft_block,
        )

    async def _clear_rejected_reply_context(
        self,
        group_id: str,
        dynamic_context_sources: list[dict[str, Any]] | None,
        *,
        record_skip: bool,
    ) -> tuple[SafetyCleanupResult, int]:
        """隔离一次被 Provider 安全过滤拒绝的主动回复动态上下文。"""
        async with self._state.get_lock(group_id):
            cleanup = self._decision_core.clear_rejected_dynamic_context(
                group_id,
                dynamic_context_sources,
            )
            if record_skip:
                self._state.record_skip_reply(group_id)
            cooldown = self._state.set_cooldown(
                group_id,
                INPUT_SAFETY_COOLDOWN_MINUTES,
            )
        await self._state.save_dirty(self._kv_save)
        return cleanup, cooldown

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse) -> None:
        tracking = event.get_extra("iris_llm_tracking")
        lease_id = event.get_extra("iris_llm_lease_id")
        if lease_id and self._llm_manager:
            try:
                await self._llm_manager.release_framework_lease(
                    lease_id,
                    provider_id=(tracking or {}).get("provider_id", ""),
                    success=True,
                )
            except Exception as e:
                # 响应已经产生，租约统计故障不应中断消息回填；Governor
                # watchdog 仍会兜底回收尚未释放的 framework lease。
                logger.warning(f"Iris Reply: 主管线 lease 释放失败：{e}")
            finally:
                event.set_extra("iris_llm_lease_id", None)

        if tracking and self._llm_manager:
            try:
                await self._llm_manager.record_framework_response(
                    module=tracking["module"],
                    provider_id=tracking.get("provider_id", ""),
                    response=resp,
                    started_at=tracking.get("started_at"),
                    prompt=tracking.get("prompt", ""),
                    priority=tracking.get("priority", "INTERACTIVE"),
                    queue_wait_ms=tracking.get("queue_wait_ms", 0),
                    in_flight_at_start=tracking.get("in_flight_at_start", 0),
                    queue_depth_at_start=tracking.get("queue_depth_at_start", 0),
                )
                event.set_extra("iris_llm_tracking", None)
            except Exception as e:
                logger.warning(f"Iris Reply: 主管线 LLM 统计失败：{e}")

        # 0. 表情包标记：先剥离，L1 与出站记录都不保存标记本身
        await self._take_sticker_marker(event, resp)
        # 0.5 lifelike P0-2（审查 M4）：agent 报错 / 友好报错文案不进 L1、不进学习采集，
        #     避免被学成回复样例；出站账本照常记（消息确实发出去了）。开关关闭 = 旧行为。
        skip_memory = False
        try:
            proc = getattr(self, "_error_processor", None)
            skip_memory = bool(proc is not None and proc.is_error_response(resp))
        except Exception:
            skip_memory = False
        if skip_memory:
            logger.info("[lifelike.error_friendly] error response kept out of L1/learning")
        # 1. 记忆侧：bot 回复入 L1。能给出发送回执的平台延迟到 after_message_sent
        #    确认真实发出后再写（DESIGN §6）；其他平台保持立即写入。
        if self.component_manager and not skip_memory:
            await handle_llm_response(
                event,
                resp,
                self.component_manager,
                defer_assistant=self._is_receipt_platform(event),
            )
        if getattr(self, "_continuity", None) is not None:
            completion = resp.completion_text or ""
            if completion.strip():
                event.set_extra(_REPLY_TEXT_EXTRA, completion)
        # 2. 主动回复侧：按 iris_mode 记账
        await self._reply_on_llm_response(event, resp)

    async def _reply_on_llm_response(self, event: AstrMessageEvent, response: LLMResponse) -> None:
        group_id = event.get_group_id()
        if not group_id:
            # 私聊：on_message 以 private:<uid> 登记了 passive 标记，这里必须释放，
            # 否则 120s 内 _is_busy 为真，会挡住该会话的主动发起
            session_key = self._session_key_quiet(event)
            if session_key:
                self._passive_active.pop(session_key, None)
                self._reply_in_progress.pop(session_key, None)
            return

        event.set_extra("iris_llm_replied", True)
        mode = event.get_extra("iris_mode")

        if mode in ("chime_in", "follow_up"):
            self._reply_in_progress.pop(group_id, None)
            self._passive_active.pop(group_id, None)

            async with self._state.get_lock(group_id):
                self._state.record_actual_reply(group_id)

            self._tool_ctx.clear_context()
            await self._state.save_dirty(self._kv_save)
            try:
                get_run_log_manager().record(
                    "proactive",
                    f"{'插话' if mode == 'chime_in' else '跟进'}回复已发送",
                    success=True,
                    group_id=group_id,
                    wake="message",
                    motive=mode,
                    stage="reply",
                    message=(response.completion_text or "").strip(),
                )
            except Exception:
                pass
            logger.info(f"Iris Reply: {mode} reply sent for group {group_id}")
        elif mode == "passive":
            self._passive_active.pop(group_id, None)

            async with self._state.get_lock(group_id):
                self._state.record_actual_reply(group_id, count_consecutive=False)

            self._stats.record_passive_reply(group_id)
            await self._state.save_dirty(self._kv_save)
            logger.info(f"Iris Reply: passive reply boost applied for group {group_id}")
        else:
            if not self._state.is_whitelisted(group_id):
                return
            async with self._state.get_lock(group_id):
                self._state.record_actual_reply(group_id, count_consecutive=False)
            await self._state.save_dirty(self._kv_save)
            logger.info(f"Iris Reply: normal LLM reply for group {group_id}, boost applied")

    @filter.after_message_sent()
    async def on_message_sent(self, event) -> None:
        """主动回复侧：bot 消息入滑动窗口 + 写 ThreadAnchor"""
        # 连续性：凭发送回执写 L1 助手回复 + 出站记账（先于原有逻辑）
        await self._continuity_after_sent(event)
        if not await self._send_pending_sticker(event):
            self._sticker_streak_reset(event)
        self._sticker_note_bot_reply(event)

        group_id = event.get_group_id()
        if not group_id:
            return
        gf_group = self._gf_event_group(event)
        if not self._state.is_whitelisted(group_id) and not gf_group:
            return
        sender_id = event.get_sender_id()
        if not sender_id:
            return
        result = event.get_result()
        bot_text = result.get_plain_text().strip() if result else ""
        if gf_group:
            self._gf_note_reply_sent(event, gf_group, bot_text)
            info = event.get_extra("iris_counter")
            if info and bot_text and not info.get("owner_sender") and info.get("level") == _hs.LEVEL_HOSTILE:
                try:
                    self._counter_tracker.note(gf_group, str(info.get("sender") or ""))
                except Exception:
                    pass
        if bot_text:
            self._sliding_window.append(group_id, WindowMessage(
                sender_id=event.get_self_id() or "iris",
                sender_name="我",
                content=bot_text,
                timestamp=time.time(),
            ))
        mode = event.get_extra("iris_mode")
        if mode in ("chime_in", "follow_up"):
            pending = event.get_extra("iris_pending_watch")
            users = list(pending[0]) if pending else []
            if sender_id not in users:
                users.append(sender_id)
            keywords = list(pending[1]) if pending else []
            reason = pending[2] if pending else ""
            topic = event.get_extra("iris_decision_obs", "")
            async with self._state.get_lock(group_id):
                self._state.write_anchor(
                    group_id,
                    kind=mode,
                    topic=topic,
                    bot_message=bot_text,
                    users=users,
                    keywords=keywords or None,
                    reason=reason,
                )
            await self._state.save_dirty(self._kv_save)
            logger.debug(f"Iris Reply: anchor written ({mode}) for group {group_id}")
        elif mode == "passive":
            provider_id = self._reply_config.provider_id or await self._get_provider_id(event)
            if provider_id and getattr(self._reply_config, "passive_watch_enabled", True):
                await self._passive_watch_eval(group_id, provider_id, sender_id, bot_text)
            else:
                async with self._state.get_lock(group_id):
                    self._state.write_anchor(
                        group_id, kind="passive", bot_message=bot_text, users=[sender_id],
                    )
                await self._state.save_dirty(self._kv_save)
        elif event.get_extra("iris_llm_replied"):
            async with self._state.get_lock(group_id):
                self._state.write_anchor(
                    group_id, kind="reply", bot_message=bot_text, users=[sender_id],
                )
            await self._state.save_dirty(self._kv_save)
            logger.debug(f"Iris Reply: anchor written (reply) for group {group_id}")

    async def _passive_watch_eval(
        self, group_id: str, provider_id: str, fallback_sender: str, bot_text: str,
    ) -> None:
        """被动回复后的跟进评估（motive=watch）：只决定是否建立关注锚点。"""
        messages = self._sliding_window.get_messages(group_id)
        if not messages:
            return
        reply_config = getattr(self, "_reply_config", None)
        if reply_config is not None and not getattr(
            reply_config, "passive_watch_enabled", True
        ):
            return

        # object.__new__ 构造的兼容测试实例没有这些映射；正常插件实例始终
        # 存在。门控只影响生产路径，不改变旧对象的错误清理语义。
        watch_last = getattr(self, "_passive_watch_last", None)
        watch_hashes = getattr(self, "_passive_watch_hash", None)
        if isinstance(watch_last, dict) and isinstance(watch_hashes, dict):
            if not bot_text or not _PASSIVE_WATCH_SIGNAL.search(bot_text):
                logger.debug(
                    f"Iris Reply: passive watch local gate skipped group {group_id}"
                )
                return

            now = time.monotonic()
            min_interval = float(self._reply_config.passive_watch_min_interval)
            if now - watch_last.get(group_id, 0.0) < min_interval:
                return

            digest_source = "\n".join(
                [
                    *(str(getattr(item, "content", "")) for item in messages[-10:]),
                    bot_text,
                ]
            )
            digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
            if watch_hashes.get(group_id) == digest:
                return

            if self._llm_manager:
                metrics = self._llm_manager.get_governor_metrics()
                queue_limit = max(1, int(metrics.get("queue_limit", 500)))
                if int(metrics.get("queue_depth", 0)) >= int(queue_limit * 0.7):
                    logger.info(
                        f"Iris Reply: passive watch skipped by backpressure for group {group_id}"
                    )
                    return

            # 在调用前记账：失败也遵守冷却，避免后续每条回复立即重新采样。
            watch_last[group_id] = now
            watch_hashes[group_id] = digest

        req = DecisionRequest(group_id=group_id, wake="message", motive="watch")
        outcome = await self._decision_core.decide(req, self._llm_manager, provider_id)

        if outcome.error or outcome.decision is None:
            if outcome.error_kind == "input_content_safety_1026":
                cleanup, cooldown = await self._clear_rejected_reply_context(
                    group_id,
                    outcome.dynamic_context_sources,
                    record_skip=False,
                )
                logger.warning(
                    "Iris Reply: passive watch input rejected by provider safety filter "
                    f"for group {group_id} (1026, retryable=false, "
                    f"dynamic_sources={cleanup.dynamic_source_count}, "
                    f"window_removed={cleanup.window_removed}, "
                    f"observation_cleared={cleanup.observation_cleared}, "
                    f"anchor_cleared={cleanup.anchor_cleared}, "
                    f"cooldown={cooldown}min): "
                    f"{outcome.error}"
                )
                self._stats.record_decision_error(group_id, "watch")
                return
            else:
                logger.warning(f"Iris Reply: passive watch eval failed for group {group_id}: {outcome.error}")
            self._stats.record_decision_error(group_id, "watch")
            async with self._state.get_lock(group_id):
                self._state.write_anchor(
                    group_id, kind="passive", bot_message=bot_text, users=[fallback_sender],
                )
            await self._state.save_dirty(self._kv_save)
            return

        decision = outcome.decision
        self._stats.record_decision(
            group_id, "watch",
            system_prompt=outcome.system_prompt,
            user_prompt=outcome.user_prompt,
            response_text=outcome.raw_text,
            decision=decision,
            duration_ms=outcome.duration_ms,
        )
        logger.info(
            f"Iris Reply: passive watch eval for group {group_id}: watch={decision.watch}, "
            f"keywords={decision.watch_keywords}, drifted={decision.drifted}"
        )

        async with self._state.get_lock(group_id):
            if decision.observation:
                self._state.set_observation(group_id, decision.observation)
            if decision.parse_failed:
                self._state.write_anchor(
                    group_id, kind="passive", bot_message=bot_text, users=[fallback_sender],
                )
            elif decision.drifted:
                self._state.close_anchor(group_id)
                self._state.record_drift(group_id)
                logger.info(f"Iris Reply: topic drifted (passive) for group {group_id}, anchor closed")
            elif decision.watch or decision.watch_keywords:
                self._state.write_anchor(
                    group_id,
                    kind="passive",
                    bot_message=bot_text,
                    users=decision.watch or None,
                    keywords=decision.watch_keywords or None,
                    reason=decision.why,
                )
            else:
                self._state.write_anchor(
                    group_id, kind="passive", bot_message=bot_text, users=[fallback_sender],
                )
        await self._state.save_dirty(self._kv_save)

    # AstrBot >= 4.23 才将 on_agent_done 暴露为插件钩子（旧版对话清理路径，默认不走）；
    # 低版本 AstrBot 下不注册该钩子，保证插件可正常加载
    if hasattr(filter, "on_agent_done"):
        @filter.on_agent_done()
        async def on_agent_done(self, event: AstrMessageEvent, run_context, resp) -> None:
            if self.component_manager:
                await handle_agent_done(event, resp, self.context, self.component_manager)

    def _install_error_guard(self, event: AstrMessageEvent) -> None:
        processor = getattr(self, "_error_processor", None)
        if processor is None:
            return
        try:
            processor.install_send_guard(event)
        except Exception as e:
            logger.warning(f"[lifelike.error_friendly] guard hook failed: {type(e).__name__}")

    def _apply_error_friendly_extra(self, event: AstrMessageEvent) -> None:
        processor = getattr(self, "_error_processor", None)
        if processor is None:
            return
        try:
            processor.apply_agent_error_extra(event)
            processor.install_send_guard(event)
        except Exception as e:
            logger.warning(f"[lifelike.error_friendly] extra hook failed: {type(e).__name__}")

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        """消息发送前处理：只读会话拦截 + 错误友好化 + Markdown 去除"""
        # 发送边界硬拦截：只读频道 / silent_sessions 永不作为回复目标
        if self._blocked_send_target(event):
            blocked = event.get_result()
            if blocked is not None:
                try:
                    blocked.chain = []
                except Exception:
                    pass
            event.stop_event()
            logger.info("[continuity] reply to read-only session blocked")
            return

        result = event.get_result()
        if not result:
            return

        # 兜底：表情包标记永远不以文字形式发出
        try:
            for comp in result.chain or []:
                if isinstance(comp, Plain) and has_marker(comp.text or ""):
                    comp.text, _ = parse_marker(comp.text, [])
        except Exception:
            pass

        if self._error_processor and self._error_processor.should_process(event):
            self._error_processor.process_result(result)

        if self._markdown_stripper and self._markdown_stripper.should_process(event):
            self._markdown_stripper.process_result(result)

        if self._care_priv02(event, result):
            return

        if self._counter_guard(event, result):
            return

        if self._gf_reply_blocked(event, result):
            return

        if self._casual_on():
            self._casual_result(result)

        self._style_audit_passive(event, result)
        self._style_register_observe(event, result)

    def _style_register_observe(self, event: AstrMessageEvent, result) -> None:
        """P1-2 卖萌上限：记下定稿回复是否卖萌（只记布尔，不存原文）；开关关闭时不做任何事"""
        try:
            is_llm = getattr(result, "is_llm_result", None)
            if callable(is_llm) and not is_llm():
                return
            text = "\n".join(
                str(getattr(c, "text", "") or "").strip()
                for c in (getattr(result, "chain", None) or [])
                if isinstance(c, Plain)
            ).strip()
            if not text:
                return
            from .iris_memory.style.selector import observe_event_reply

            observe_event_reply(event, text)
        except Exception as e:
            logger.warning(f"[lifelike.style_inject] observe skipped: {type(e).__name__}")

    def _care_priv02(self, event: AstrMessageEvent, result) -> bool:
        """活人感 P1-12 PRIV-02：群聊回复里删掉"私聊过/你私下跟我说过"类句子。

        关心桥开关 off 时不做任何事（= 旧行为）；shadow 只记会删几句、不改回复；on 才删。
        删完没有任何内容 → 不发送（返回 True）并记 WARN。日志只记句数。任何异常都不影响发送。"""
        try:
            if event.is_private_chat():
                return False
            mode = str(self.config.get("lifelike_care_bridge_mode", "on") or "").strip().lower()
            if mode not in ("shadow", "on"):
                return False
            is_llm = getattr(result, "is_llm_result", None)
            if callable(is_llm) and not is_llm():
                return False
            chain = list(getattr(result, "chain", None) or [])
            if mode == "shadow":
                would = sum(
                    strip_private_mentions(c.text)[1] for c in chain if isinstance(c, Plain) and c.text
                )
                if would:
                    logger.info(f"[lifelike.care] PRIV-02 shadow group reply would_remove={would}")
                return False
            removed = 0
            for comp in chain:
                if isinstance(comp, Plain) and comp.text:
                    new, n = strip_private_mentions(comp.text)
                    if n:
                        comp.text = new
                        removed += n
            if not removed:
                return False
            logger.info(f"[lifelike.care] PRIV-02 group reply sentences_removed={removed}")
            if all(isinstance(c, Plain) and not (c.text or "").strip() for c in chain):
                result.chain = []
                event.stop_event()
                logger.warning(
                    f"[lifelike.care] PRIV-02 whole group reply suppressed sentences_removed={removed}"
                )
                return True
            return False
        except Exception as e:
            logger.warning(f"[lifelike.care] PRIV-02 check skipped: {type(e).__name__}")
            return False

    def _build_style_auditor(self) -> Optional[StyleAuditor]:
        try:
            return StyleAuditor(
                mode_get=lambda: self.config.get("lifelike_style_guard_mode", "log"),
                store_get=self._style_audit_store,
            )
        except Exception as e:
            logger.warning(f"[lifelike.style] auditor init failed, style guard off: {type(e).__name__}")
            return None

    def _style_audit_store(self):
        svc = getattr(self, "_continuity", None)
        if svc is None or not svc.is_open:
            return None
        return svc.store

    def _style_audit_passive(self, event: AstrMessageEvent, result) -> None:
        """被动回复定稿后的风格审计（log 模式：只记录，不改写；任何异常都不影响发送）"""
        auditor = getattr(self, "_style_auditor", None)
        if auditor is None:
            return
        try:
            is_llm = getattr(result, "is_llm_result", None)
            if callable(is_llm) and not is_llm():
                return
            text = "\n".join(
                str(getattr(c, "text", "") or "").strip()
                for c in (getattr(result, "chain", None) or [])
                if isinstance(c, Plain)
            ).strip()
            scope = self._session_key_quiet(event)
            if not text or not scope:
                return
            auditor.schedule(
                scope, _STYLE_PASSIVE, text,
                user_text=str(getattr(event, "message_str", "") or ""),
            )
        except Exception as e:
            logger.warning(f"[lifelike.style] passive audit skipped: {type(e).__name__}")
