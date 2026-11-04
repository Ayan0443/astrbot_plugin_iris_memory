from __future__ import annotations

from collections.abc import Callable
from typing import Any

from astrbot.api import AstrBotConfig


_DEFAULTS = {
    "enabled": True,
    "stats_enabled": False,
    "mute_period": {
        "start_hour": 1,
        "start_minute": 0,
        "end_hour": 7,
        "end_minute": 0,
    },
    "window_size": 15,
    "default_n": 15,
    "default_t": 30,
    "max_token": 3000,
    "follow_up_ttl": 10,
    "follow_up_aggregate_window": 6,
    "quality_threshold": 0.2,
    "provider_id": "",
    "trigger_min_interval": 30,
    "passive_watch_min_interval": 600,
    "passive_watch_enabled": False,
    "boost_factor": 0.6,
    "boost_duration": 15,
    "max_boosted_replies": 5,
    "proactive_enabled": False,
    "proactive_check_interval": 5,
    "proactive_quiet_minutes": 120,
    "proactive_max_per_day": 2,
    "proactive_min_interval": 360,
    "proactive_drift_delay": 15,
    "proactive_pending_timeout": 30,
    "proactive_max_streak": 2,
    "proactive_instruction": "",
    "proactive_max_message_len": 300,
    "private_enabled": False,
    "private_whitelist": [],
    # 群聊门控：主人发言交给决策层判断是否接话；其他人仅在明确
    # @/回复/叫名字后的一段时间内可被跟进。
    "owner_chime_debounce": 60,
    "engaged_window": 60,
    # 她被叫的名字；留空 = 用 identity_bot_names
    "bot_name_keywords": [],
    # 群里冷场时由她不定时开话题；默认关闭，只保留私聊定时发起。
    "group_initiate_enabled": False,
}

# _conf_schema.json 中 proactive 分组内的面板键（嵌套存储，面板管理）
_SCHEMA_GROUP = "proactive"
_SCHEMA_KEYS = {
    "enabled",
    "proactive_enabled",
    "stats_enabled",
    "provider_id",
    "private_enabled",
    "private_whitelist",
}

# 页面管理键 → 隐藏配置键（HiddenConfig 字段名）。
# 主动发起类键名与隐藏配置字段同名，基本参数类加 reply_ 前缀，
# mute_period 拆为四个独立整数键。
_HIDDEN_KEY_MAP = {
    "window_size": "reply_window_size",
    "default_n": "reply_default_n",
    "default_t": "reply_default_t",
    "max_token": "reply_max_token",
    "follow_up_ttl": "reply_follow_up_ttl",
    "follow_up_aggregate_window": "reply_follow_up_aggregate_window",
    "quality_threshold": "reply_quality_threshold",
    "trigger_min_interval": "reply_trigger_min_interval",
    "passive_watch_min_interval": "reply_passive_watch_min_interval",
    "passive_watch_enabled": "reply_passive_watch_enabled",
    "boost_factor": "reply_boost_factor",
    "boost_duration": "reply_boost_duration",
    "max_boosted_replies": "reply_max_boosted_replies",
    "proactive_check_interval": "proactive_check_interval",
    "proactive_quiet_minutes": "proactive_quiet_minutes",
    "proactive_max_per_day": "proactive_max_per_day",
    "proactive_min_interval": "proactive_min_interval",
    "proactive_drift_delay": "proactive_drift_delay",
    "proactive_pending_timeout": "proactive_pending_timeout",
    "proactive_max_streak": "proactive_max_streak",
    "proactive_instruction": "proactive_instruction",
    "proactive_max_message_len": "proactive_max_message_len",
}

_MUTE_HIDDEN_KEYS = {
    "start_hour": "reply_mute_start_hour",
    "start_minute": "reply_mute_start_minute",
    "end_hour": "reply_mute_end_hour",
    "end_minute": "reply_mute_end_minute",
}

HiddenGetFn = Callable[[str], Any]

# 活人感 P0-8 主动安全底线（HiddenConfig 平铺键；hidden_get 缺失时取这里的默认值）
_LIFELIKE_FLOOR_DEFAULTS: dict[str, Any] = {
    "lifelike_proactive_quiet_hours": "",
    "lifelike_proactive_activity_gate": True,
    "lifelike_activity_recent_hours": 6.0,
    "lifelike_activity_lookback_days": 28,
    "lifelike_activity_min_days": 7,
    "lifelike_activity_inactive_ratio": 0.1,
    "lifelike_activity_prior_inactive": "01:00-08:00",
    "lifelike_proactive_empty_window": False,  # 审查 M5：首发默认关
    # 活人感 P1-8：主动候选池（off/shadow/on）；只用于"空窗时有可推送候选也可调度"的判断
    "lifelike_initiative_mode": "on",
    # 候选池有通过闸门的可推送候选（如群→私聊关心）时，空窗也允许调度（D14；false = 空窗一律不调度）
    "lifelike_pool_empty_window": True,
    # 活人感 U8：主人私聊自由主动（false = 73edc2c 行为；见 proactive/freedom.py）
    "lifelike_proactive_freedom": False,
    "lifelike_freedom_eval_minutes": 20,
    "lifelike_freedom_owner_quiet_minutes": 10,
    "lifelike_freedom_min_gap_minutes": 10,
    "lifelike_freedom_breaker_24h": 20,
    "lifelike_freedom_hush_hours": 12.0,
    # 活人感 U9：白名单群自由搭话（false = 3c6e6e6 行为；见 proactive/group_freedom.py）
    "lifelike_group_freedom": False,
    "lifelike_gf_debounce_seconds": 45,
    "lifelike_gf_initiate_quiet_minutes": 45,
    "lifelike_gf_initiate_eval_minutes": 45,
    "lifelike_gf_floor_seconds": 120,
    "lifelike_gf_breaker_consecutive": 2,
    "lifelike_gf_hush_owner_hours": 6.0,
    "lifelike_gf_hush_member_hours": 2.0,
    # 聊天欲望档位 off/low/medium/high（只在 U8 / U9 打开时生效；off = 3c6e6e6 的措辞与节奏）
    "lifelike_chat_drive": "off",
    # 被恶意对待时反击（off = 3c6e6e6 行为）
    "lifelike_counter_mode": "off",
    "lifelike_counter_owner_names": "",
    "lifelike_counter_max_rounds": 3,
}


def _coerce_like(default: Any, value: Any) -> Any:
    """按默认值类型强制转换迁移值，失败返回 None（调用方跳过）。"""
    try:
        if isinstance(default, bool):
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("true", "1", "yes", "on")
        if isinstance(default, int):
            return int(value)
        if isinstance(default, float):
            return float(value)
        if isinstance(default, str):
            return str(value).strip()
    except (TypeError, ValueError):
        return None
    return value


class ConfigManager:
    """主动回复配置管理

    取值优先级：隐藏配置（hidden_get）> AstrBotConfig 平铺值（遗留兼容）> 内置默认值。
    enabled / proactive_enabled / stats_enabled / provider_id 四个面板键仍读
    _conf_schema.json 的 proactive 嵌套组，不经过隐藏配置。
    """

    def __init__(
        self,
        config: AstrBotConfig,
        hidden_get: HiddenGetFn | None = None,
    ) -> None:
        self._cfg = config
        self._hidden_get = hidden_get

    @staticmethod
    def legacy_overrides_to_hidden(data: dict | None) -> dict[str, Any]:
        """把旧版 KV 页面覆写（iris_reply:config_overrides）翻译为隐藏配置键值。

        返回 {隐藏配置键: 值}，非法/未知键被跳过；空输入返回空 dict。
        """
        if not isinstance(data, dict) or not data:
            return {}
        result: dict[str, Any] = {}
        for key, value in data.items():
            if key == "mute_period":
                if not isinstance(value, dict):
                    continue
                dmp = _DEFAULTS["mute_period"]
                for sub_key, hidden_key in _MUTE_HIDDEN_KEYS.items():
                    coerced = _coerce_like(0, value.get(sub_key, dmp[sub_key]))
                    if coerced is not None:
                        result[hidden_key] = coerced
                continue
            hidden_key = _HIDDEN_KEY_MAP.get(key)
            if hidden_key is None or key not in _DEFAULTS:
                continue
            coerced = _coerce_like(_DEFAULTS[key], value)
            if coerced is not None:
                result[hidden_key] = coerced
        return result

    def _get(self, key: str, default=None):
        if key in _SCHEMA_KEYS:
            # 面板配置项存于 _conf_schema.json 的 proactive 分组（嵌套 dict）
            group = self._cfg.get(_SCHEMA_GROUP)
            if isinstance(group, dict):
                val = group.get(key)
                if val is not None:
                    return val
            return _DEFAULTS.get(key, default)

        hidden_key = _HIDDEN_KEY_MAP.get(key)
        if hidden_key is not None and self._hidden_get is not None:
            val = self._hidden_get(hidden_key)
            if val is not None:
                return val

        val = self._cfg.get(key)
        if val is not None:
            return val
        return _DEFAULTS.get(key, default)

    def _get_mute_part(self, part: str) -> int:
        dmp = _DEFAULTS["mute_period"]
        hidden_key = _MUTE_HIDDEN_KEYS[part]
        if self._hidden_get is not None:
            val = self._hidden_get(hidden_key)
            if val is not None:
                try:
                    return int(val)
                except (TypeError, ValueError):
                    pass
        mp = self._cfg.get("mute_period")
        if isinstance(mp, dict):
            try:
                return int(mp.get(part, dmp[part]))
            except (TypeError, ValueError):
                pass
        return dmp[part]

    @property
    def enabled(self) -> bool:
        return bool(self._get("enabled"))

    @property
    def stats_enabled(self) -> bool:
        return bool(self._get("stats_enabled"))

    @property
    def mute_period(self) -> tuple[int, int, int, int]:
        return (
            self._get_mute_part("start_hour"),
            self._get_mute_part("start_minute"),
            self._get_mute_part("end_hour"),
            self._get_mute_part("end_minute"),
        )

    @property
    def mute_start_hour(self) -> int:
        return self.mute_period[0]

    @property
    def mute_start_minute(self) -> int:
        return self.mute_period[1]

    @property
    def mute_end_hour(self) -> int:
        return self.mute_period[2]

    @property
    def mute_end_minute(self) -> int:
        return self.mute_period[3]

    @property
    def window_size(self) -> int:
        return max(5, min(30, int(self._get("window_size"))))

    @property
    def default_n(self) -> int:
        return max(5, min(120, int(self._get("default_n"))))

    @property
    def default_t(self) -> int:
        return max(5, min(180, int(self._get("default_t"))))

    @property
    def max_token(self) -> int:
        return max(1000, min(8000, int(self._get("max_token"))))

    @property
    def follow_up_ttl(self) -> int:
        return max(5, min(120, int(self._get("follow_up_ttl"))))

    @property
    def follow_up_aggregate_window(self) -> int:
        return max(3, min(30, int(self._get("follow_up_aggregate_window"))))

    @property
    def quality_threshold(self) -> float:
        return max(0.0, min(1.0, float(self._get("quality_threshold"))))

    @property
    def provider_id(self) -> str:
        return str(self._get("provider_id", ""))

    @property
    def trigger_min_interval(self) -> int:
        return max(10, min(120, int(self._get("trigger_min_interval"))))

    @property
    def passive_watch_min_interval(self) -> int:
        return max(60, min(3600, int(self._get("passive_watch_min_interval"))))

    @property
    def passive_watch_enabled(self) -> bool:
        return bool(self._get("passive_watch_enabled"))

    @property
    def boost_factor(self) -> float:
        return max(0.3, min(0.95, float(self._get("boost_factor"))))

    @property
    def boost_duration(self) -> int:
        return max(1, min(60, int(self._get("boost_duration"))))

    @property
    def max_boosted_replies(self) -> int:
        return max(2, min(10, int(self._get("max_boosted_replies"))))

    @property
    def proactive_enabled(self) -> bool:
        v = self._get("proactive_enabled")
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("true", "1", "yes", "on")

    @property
    def proactive_check_interval(self) -> int:
        return max(1, min(30, int(self._get("proactive_check_interval"))))

    @property
    def proactive_quiet_minutes(self) -> int:
        return max(30, min(720, int(self._get("proactive_quiet_minutes"))))

    @property
    def proactive_max_per_day(self) -> int:
        return max(1, min(10, int(self._get("proactive_max_per_day"))))

    @property
    def proactive_min_interval(self) -> int:
        return max(60, min(1440, int(self._get("proactive_min_interval"))))

    @property
    def proactive_drift_delay(self) -> int:
        return max(5, min(120, int(self._get("proactive_drift_delay"))))

    @property
    def proactive_pending_timeout(self) -> int:
        return max(5, min(120, int(self._get("proactive_pending_timeout"))))

    @property
    def proactive_max_streak(self) -> int:
        return max(1, min(5, int(self._get("proactive_max_streak"))))

    @property
    def proactive_instruction(self) -> str:
        return str(self._get("proactive_instruction", ""))

    @property
    def proactive_max_message_len(self) -> int:
        return max(50, min(1000, int(self._get("proactive_max_message_len"))))

    @property
    def owner_chime_debounce(self) -> int:
        """主人群发言触发接话决策的最小间隔（秒）。"""
        return max(0, min(3600, int(self._get("owner_chime_debounce"))))

    @property
    def engaged_window(self) -> int:
        """非主人群友明确找过她之后，可被跟进的最长时长（分钟）。"""
        return max(1, min(240, int(self._get("engaged_window"))))

    @property
    def group_initiate_enabled(self) -> bool:
        return bool(self._get("group_initiate_enabled"))

    @property
    def bot_name_keywords(self) -> list[str]:
        raw = self._get("bot_name_keywords", [])
        if isinstance(raw, str):
            raw = [x for x in raw.replace("，", ",").split(",")]
        names = [str(x).strip().lower() for x in (raw or []) if str(x).strip()]
        if names:
            return names
        from ..identity import bot_names

        return [n.lower() for n in bot_names()]

    @property
    def private_enabled(self) -> bool:
        v = self._get("private_enabled")
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("true", "1", "yes", "on")

    @property
    def silent_sessions(self) -> list[str]:
        """只记录不发言的会话 ID（面板 proactive.silent_sessions，直接读取，不进默认表）。"""
        try:
            group = self._cfg.get(_SCHEMA_GROUP)
        except Exception:
            group = None
        raw = group.get("silent_sessions", []) if isinstance(group, dict) else []
        if isinstance(raw, str):
            raw = [part for part in raw.replace("，", ",").split(",")]
        if not isinstance(raw, (list, tuple, set)):
            return []
        return [str(item).strip() for item in raw if str(item).strip()]

    @property
    def private_whitelist(self) -> list[str]:
        """允许私聊主动消息的用户 ID 列表（字符串形式）。"""
        raw = self._get("private_whitelist", [])
        if isinstance(raw, str):
            raw = [part for part in raw.replace("，", ",").split(",")]
        if not isinstance(raw, (list, tuple, set)):
            return []
        return [str(item).strip() for item in raw if str(item).strip()]

    # ---- 活人感 P0-8：主动安全底线 ----

    def _lifelike(self, key: str) -> Any:
        default = _LIFELIKE_FLOOR_DEFAULTS[key]
        if self._hidden_get is None:
            return default
        try:
            val = self._hidden_get(key)
        except Exception:
            return default
        if val is None:
            return default
        coerced = _coerce_like(default, val)
        return default if coerced is None else coerced

    @property
    def initiative_on(self) -> bool:
        """活人感 P1-8：lifelike_initiative_mode == on（读取失败 → False，= 旧行为）"""
        try:
            return str(self._lifelike("lifelike_initiative_mode") or "").strip().lower() == "on"
        except Exception:
            return False

    @property
    def pool_empty_window(self) -> bool:
        """活人感 P1-8：候选池可推送时空窗也调度（读取失败 → False，= 旧行为）"""
        try:
            return bool(self._lifelike("lifelike_pool_empty_window"))
        except Exception:
            return False

    @property
    def freedom_config(self):
        """活人感 U8：自由主动配置；读取 / 解析失败 → 关闭（= 73edc2c 行为）。"""
        from .freedom import FreedomConfig, clamp_config

        try:
            return clamp_config(self._lifelike)
        except Exception:
            return FreedomConfig()

    @property
    def group_freedom_config(self):
        """活人感 U9：群聊自由搭话配置；读取 / 解析失败 → 关闭（= 3c6e6e6 行为）。"""
        from .group_freedom import GroupFreedomConfig, clamp_config

        try:
            return clamp_config(self._lifelike)
        except Exception:
            return GroupFreedomConfig()

    @property
    def chat_drive(self) -> str:
        """聊天欲望档位（U8 / U9 打开时才有作用）；读取失败 → off（= 3c6e6e6 措辞）。"""
        from .drive import normalize

        try:
            return normalize(self._lifelike("lifelike_chat_drive"))
        except Exception:
            return "off"

    @property
    def counter_mode(self) -> bool:
        """被恶意对待时反击（on / off）；读取失败 → False。"""
        try:
            return str(self._lifelike("lifelike_counter_mode") or "").strip().lower() in ("on", "true", "1")
        except Exception:
            return False

    @property
    def counter_owner_names(self) -> list[str]:
        try:
            raw = str(self._lifelike("lifelike_counter_owner_names") or "")
        except Exception:
            raw = ""
        names = [x.strip().lower() for x in raw.replace("，", ",").split(",") if x.strip()]
        if names:
            return names
        from ..identity import owner_names

        return [n.lower() for n in owner_names()]

    @property
    def counter_max_rounds(self) -> int:
        try:
            return max(2, min(6, int(self._lifelike("lifelike_counter_max_rounds"))))
        except Exception:
            return 3

    @property
    def activity_gate_config(self):
        """读取 P0-8 开关组；任何异常返回"全部关闭"（= 现网行为）。"""
        from .activity_gate import ActivityGateConfig, parse_hhmm_range

        try:
            return ActivityGateConfig(
                quiet_hours=parse_hhmm_range(self._lifelike("lifelike_proactive_quiet_hours")),
                activity_gate=bool(self._lifelike("lifelike_proactive_activity_gate")),
                recent_hours=max(0.0, min(72.0, float(self._lifelike("lifelike_activity_recent_hours")))),
                lookback_days=max(1, min(120, int(self._lifelike("lifelike_activity_lookback_days")))),
                min_days=max(1, min(120, int(self._lifelike("lifelike_activity_min_days")))),
                inactive_ratio=max(0.0, min(1.0, float(self._lifelike("lifelike_activity_inactive_ratio")))),
                prior_inactive=parse_hhmm_range(self._lifelike("lifelike_activity_prior_inactive")),
                empty_window=bool(self._lifelike("lifelike_proactive_empty_window")),
            )
        except Exception:
            return ActivityGateConfig()
