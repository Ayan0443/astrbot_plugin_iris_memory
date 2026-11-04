"""活人感 U9：白名单群"自由搭话"（``lifelike_group_freedom``）。

用户拍板（DECISIONS.md U9）："让她自主决定跟白名单群里的群友搭话吧，不要加限制了"。

开关打开后，对 AstrBot 平台 ``id_whitelist`` 里的群（去掉私聊 id、主人频道、silent_sessions、
内置禁发频道）：

- 任何群友的消息都进入"要不要搭话"的决策（不只主人；不再只认 @ / 回复 / 叫名字）；
  一阵消息停下来后合并判断一次（去抖，默认 45 秒），由 LLM 决定接不接、接谁、说什么，可以不说；
- 群里安静一段时间后按节奏（默认安静 ≥45 分钟、约每 45 分钟一次）请她评估要不要自己开个话题；
- 取消：冷却作为闸门、采样概率、频率 / 条数上限、"只在被点名时回应"、跟进时长上限、质量分阈值、
  静音时段、``group_initiate_enabled``、``trigger_min_interval``；
- 保留技术安全网（不算限制）：同内容 / 同话题去重、同群主动搭话硬底线（默认 120 秒；被 @ / 回复 /
  叫名字时不受限）、连续无人说话熔断（默认她连续 2 条主动发言之间没有任何人说话 → 暂停主动直到有人说话）、
  群里有人叫她安静（主人默认 6 小时、群友默认 2 小时；主人说"可以说话了"解除）、她自己在决策里
  请求的短暂停（上限 5 分钟，只跳过群友闲聊的评估，点名不受影响）、MAN / 讨要回复拦截、
  PRIV-01/02 私聊内容不进群、频道永不发送。

本模块只放纯函数、常量与内存态追踪器，不做 I/O；日志不含正文。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from . import drive as dv
from . import freedom as fr

KEY_GROUP_FREEDOM = "lifelike_group_freedom"
KEY_CHAT_DRIVE = dv.KEY_CHAT_DRIVE

# 屏蔽原因（只进日志，不进 prompt）
R_HUSH = "group_hush"
R_FLOOR = "floor_gap"
R_BREAKER = "breaker"
R_NO_CONTEXT = "no_context"
R_GROUP_ACTIVE = "group_active"
R_SELF_PAUSE = "self_pause"


# 叫停只在"在对她说"时生效：被 @ / 回复 / 叫名字，或她最近 N 秒内在群里说过话
HUSH_RECENT_SELF_S = 600.0


@dataclass(frozen=True)
class GroupFreedomConfig:
    enabled: bool = False
    debounce_seconds: int = 45  # 一阵消息停下来后多久合并判断一次
    initiate_quiet_minutes: int = 45  # 群安静多久之后才评估要不要开话题
    initiate_eval_minutes: int = 45  # 开话题评估节奏（±20% 抖动）
    floor_seconds: int = 120  # 同群两次主动搭话之间的硬底线（防故障连发）
    breaker_consecutive: int = 2  # 她连续主动发言、中间无人说话达到此数 → 暂停主动直到有人说话
    hush_owner_hours: float = 6.0
    hush_member_hours: float = 2.0
    chat_drive: str = "off"  # 聊天欲望档位（措辞、开话题节奏、自我暂停上限）

    @property
    def floor_s(self) -> float:
        return float(self.floor_seconds)

    @property
    def initiate_quiet_s(self) -> float:
        return float(dv.group_quiet_minutes(self.chat_drive, self.initiate_quiet_minutes)) * 60.0

    @property
    def initiate_eval_s(self) -> float:
        return float(dv.group_eval_minutes(self.chat_drive, self.initiate_eval_minutes)) * 60.0

    @property
    def self_pause_cap(self) -> int:
        return dv.group_pause_cap(self.chat_drive)


def clamp_config(raw: Callable[[str], Any]) -> GroupFreedomConfig:
    """从取值函数构造（带上下限）；任何异常交给调用方回退为 GroupFreedomConfig()（= 关闭）。"""
    return GroupFreedomConfig(
        enabled=bool(raw(KEY_GROUP_FREEDOM)),
        debounce_seconds=max(10, min(300, int(raw("lifelike_gf_debounce_seconds")))),
        initiate_quiet_minutes=max(10, min(720, int(raw("lifelike_gf_initiate_quiet_minutes")))),
        initiate_eval_minutes=max(15, min(360, int(raw("lifelike_gf_initiate_eval_minutes")))),
        floor_seconds=max(60, min(1800, int(raw("lifelike_gf_floor_seconds")))),
        breaker_consecutive=max(2, min(10, int(raw("lifelike_gf_breaker_consecutive")))),
        hush_owner_hours=max(0.5, min(72.0, float(raw("lifelike_gf_hush_owner_hours")))),
        hush_member_hours=max(0.5, min(24.0, float(raw("lifelike_gf_hush_member_hours")))),
        chat_drive=dv.normalize(raw(KEY_CHAT_DRIVE)),
    )


# ---------------------------------------------------------------- 群里叫停

# 群聊里常见的"让她别说"说法（在 fr.is_hush 之外补充；fr 的玩笑豁免同样适用）
_GROUP_HUSH_RE = re.compile(
    r"(?:(?<![特区告分级类差个识辨性离派鉴])别|不要|甭|不许)(?:再)?(?:说话|说了|讲了|讲话|插嘴|插话|刷屏|接话|冒泡|出来|吵)"
    r"|少说(?:两句|几句|点)"
    r"|(?:^|你|您|先)安静(?:点|一点|些|一些|一会儿?|会儿)?$",
    re.IGNORECASE,
)


def is_group_hush(text: str) -> bool:
    """群里有人叫她安静 / 别说话 → True（调用方还要求"在对她说"）。"""
    body = fr._body(text)
    if not body or fr._JOKE_RE.search(body):
        return False
    if fr.is_hush(body):
        return True
    return any(_GROUP_HUSH_RE.search(c) for c in fr._CLAUSE_SPLIT.split(body) if c)


def is_group_unhush(text: str) -> bool:
    return fr.is_unhush(text)


# ---------------------------------------------------------------- 内存态追踪

class _GroupTrack:
    __slots__ = (
        "seq", "last_human_at", "last_self_at", "last_unsolicited_at", "consecutive",
        "humans_since_self", "addressed_since_self", "breaker_logged",
    )

    def __init__(self) -> None:
        self.seq = 0
        self.last_human_at = 0.0
        self.last_self_at = 0.0
        self.last_unsolicited_at = 0.0
        self.consecutive = 0
        self.humans_since_self = 0
        self.addressed_since_self = False
        self.breaker_logged = False


class GroupFreedomTracker:
    """每群的内存态（重启后保守地全部归零；叫停与去重记录在 StateManager 里持久化）。"""

    def __init__(self) -> None:
        self._groups: dict[str, _GroupTrack] = {}

    def _g(self, group_id: str) -> _GroupTrack:
        g = self._groups.get(group_id)
        if g is None:
            g = self._groups[group_id] = _GroupTrack()
        return g

    def known_groups(self) -> set[str]:
        return set(self._groups)

    # 去抖序号：每条进入去抖的消息领一个号，只有最后一个号在静默后做决策
    def next_seq(self, group_id: str) -> int:
        g = self._g(group_id)
        g.seq += 1
        return g.seq

    def current_seq(self, group_id: str) -> int:
        return self._g(group_id).seq

    def note_human(self, group_id: str, ts: float, *, addressed: bool = False) -> None:
        g = self._g(group_id)
        g.last_human_at = max(g.last_human_at, float(ts))
        g.consecutive = 0
        g.breaker_logged = False
        if g.last_self_at > 0:
            g.humans_since_self += 1
            if addressed:
                g.addressed_since_self = True

    def note_self(self, group_id: str, ts: float, *, unsolicited: bool) -> None:
        g = self._g(group_id)
        g.last_self_at = max(g.last_self_at, float(ts))
        g.humans_since_self = 0
        g.addressed_since_self = False
        if unsolicited:
            g.last_unsolicited_at = max(g.last_unsolicited_at, float(ts))
            g.consecutive += 1

    def last_self_at(self, group_id: str) -> float:
        return self._g(group_id).last_self_at

    def last_human_at(self, group_id: str) -> float:
        return self._g(group_id).last_human_at

    def floor_left(self, group_id: str, cfg: GroupFreedomConfig, now: float) -> float:
        last = self._g(group_id).last_unsolicited_at
        if last <= 0:
            return 0.0
        return max(0.0, cfg.floor_s - (now - last))

    def breaker_hit(self, group_id: str, cfg: GroupFreedomConfig) -> bool:
        return self._g(group_id).consecutive >= cfg.breaker_consecutive

    def breaker_first(self, group_id: str) -> bool:
        """熔断首次触发返回 True（只记一次 ERROR）。"""
        g = self._g(group_id)
        if g.breaker_logged:
            return False
        g.breaker_logged = True
        return True

    def response_state(self, group_id: str) -> tuple[int, bool]:
        g = self._g(group_id)
        return g.humans_since_self, g.addressed_since_self


def block_reason(
    cfg: GroupFreedomConfig,
    tracker: GroupFreedomTracker,
    group_id: str,
    *,
    now: float,
    hush_until: float,
    has_context: bool,
    last_human_at: float = 0.0,
    check_quiet: bool = False,
) -> str:
    """群聊自由模式下仍生效的技术安全网（主动搭话 / 开话题）；全部通过返回 ""。"""
    if hush_until > now:
        return R_HUSH
    if tracker.floor_left(group_id, cfg, now) > 0:
        return R_FLOOR
    if tracker.breaker_hit(group_id, cfg):
        return R_BREAKER
    if not has_context:
        return R_NO_CONTEXT
    if check_quiet and last_human_at > 0 and now - last_human_at < cfg.initiate_quiet_s:
        return R_GROUP_ACTIVE
    return ""


# ---------------------------------------------------------------- 决策事实

def build_facts(
    *,
    now: float,
    wake: str,
    messages: Iterable[Any],
    self_id: str,
    owner_ids: Iterable[str] = (),
    last_self_at: float = 0.0,
    humans_since_self: int = 0,
    addressed_since_self: bool = False,
    counter: Any = None,
    localtime: Callable[[float], time.struct_time] = time.localtime,
) -> str:
    """群聊 <facts> 块：时间、谁在说话、她上次说话距今多久、之后有没有人接——只有事实，不含任何分值。

    wake: "burst"（一阵消息停下来）| "name"（有人叫她名字）| "timer"（群里安静了，考虑开话题）"""
    msgs = list(messages or ())
    owners = {str(o) for o in owner_ids or ()}
    lines = [f"- 现在：{fr.fmt_now(now, localtime)}"]
    wake_lines = {
        "burst": "- 群里刚才的一阵消息停下来了",
        "name": "- 刚才有人在群里提到了你的名字",
        "hostile": "- 刚才有人的消息可能在冲你（或你很熟的人）说难听的话",
        "timer": "- 群里已经安静了一段时间",
    }
    if wake in wake_lines:
        lines.append(wake_lines[wake])
    if isinstance(counter, dict) and counter.get("level"):
        how = "回复了你很熟的人的消息" if counter.get("target") == "owner" else "@ / 回复 / 叫了你"
        strength = "明确的难听话" if counter.get("level") == "hostile" else "语气有点冲，也可能是玩笑"
        lines.append(f"- 规则初筛：这条消息{how}，内容是{strength}（只是关键词判断，可能不准）")
    speakers: list[str] = []
    owner_spoke = ""
    last_human_ts = 0.0
    for m in reversed(msgs[-12:]):
        sid = str(getattr(m, "sender_id", "") or "")
        name = str(getattr(m, "sender_name", "") or sid)
        if (self_id and sid == self_id) or name == "我":
            continue
        last_human_ts = max(last_human_ts, float(getattr(m, "timestamp", 0.0) or 0.0))
        label = f"{name[:20]}({sid})"
        if label not in speakers:
            speakers.append(label)
        if sid in owners and not owner_spoke:
            owner_spoke = sid
    if speakers:
        lines.append(f"- 最近在说话的人：{'、'.join(speakers[:6])}")
    if owner_spoke:
        lines.append(f"- 其中有你很熟的人（ID {owner_spoke}）")
    if last_human_ts > 0:
        lines.append(f"- 群里最后一条别人的消息：{fr.fmt_ago(now - last_human_ts)}")
    if last_self_at > 0:
        line = f"- 你上一次在这个群说话：{fr.fmt_ago(now - float(last_self_at))}"
        if addressed_since_self:
            line += "，之后有人回复或叫了你"
        elif humans_since_self > 0:
            line += f"，之后群里又有 {int(humans_since_self)} 条别人的消息"
        else:
            line += "，之后还没有人说话"
        lines.append(line)
    else:
        lines.append("- 你最近没有在这个群里说过话")
    return "<facts>\n" + "\n".join(lines) + "\n</facts>"


__all__ = [
    "KEY_GROUP_FREEDOM",
    "KEY_CHAT_DRIVE",
    "GroupFreedomConfig",
    "GroupFreedomTracker",
    "GroupFreedomHub",
    "clamp_config",
    "is_group_hush",
    "is_group_unhush",
    "block_reason",
    "build_facts",
    "R_HUSH",
    "R_FLOOR",
    "R_BREAKER",
    "R_NO_CONTEXT",
    "R_GROUP_ACTIVE",
    "R_SELF_PAUSE",
]


class GroupFreedomHub:
    """主插件与主动引擎共享的 U9 句柄：配置读取、群资格判定、内存态追踪。

    cfg_get / eligible 由主插件注入；任何异常 → 视为关闭（= 3c6e6e6 行为）。"""

    def __init__(
        self,
        cfg_get: Callable[[], GroupFreedomConfig],
        eligible: Callable[[str], bool],
        owner_ids_get: Callable[[], Iterable[str]] = lambda: (),
    ) -> None:
        self._cfg_get = cfg_get
        self._eligible = eligible
        self._owner_ids_get = owner_ids_get
        self.tracker = GroupFreedomTracker()

    def cfg(self) -> GroupFreedomConfig:
        try:
            cfg = self._cfg_get()
        except Exception:
            return GroupFreedomConfig()
        return cfg if isinstance(cfg, GroupFreedomConfig) else GroupFreedomConfig()

    def enabled(self) -> bool:
        return bool(self.cfg().enabled)

    def active(self, group_id: str) -> bool:
        gid = str(group_id or "")
        if not gid or gid.startswith("private:"):
            return False
        try:
            return self.enabled() and bool(self._eligible(gid))
        except Exception:
            return False

    def owner_ids(self) -> set[str]:
        try:
            return {str(x).strip() for x in (self._owner_ids_get() or ()) if str(x).strip()}
        except Exception:
            return set()
