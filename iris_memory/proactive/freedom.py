"""活人感 U8：主人私聊"自由主动"（``lifelike_proactive_freedom``）。

用户拍板（DECISIONS.md U8）："私聊限制关掉，让她自由决定什么时候私聊我即可"。

开关打开后，对**接入连续性桥接的主人私聊**：

- 不再执行任何"频率 / 时段 / 未回复"类限制：滚动 24h 条数上限、两次主动最小间隔、
  连续未回复暂停、等待回复（pending）、静音时段、活动窗口推迟、收尾冷却、
  候选池 push 上限 / 间隔 / 未回复翻倍、同类型 24h、关心桥每日 / 每周上限；
- 改为按固定节奏（默认约 20 分钟）请她评估一次"要不要发"，由 LLM 决定；
  决策 prompt 里给出时间类事实（距上次互动、距上次主动、上次主动是否得到回应、
  现在几点），只作事实、不作关系分值；
- 保留技术安全网（不算限制）：主人刚说完话后的短静默（不插话）、同内容 / 同话题去重
  （近 24h 自由主动出站，文本相似度 ≥0.8 或切入角度相同）、同候选去重、
  两次主动之间的硬底线（默认 10 分钟）、熔断（主人最后一条消息之后、滚动 24h 内默认 20 条，
  触发记 ERROR；主人一回话即归零）、主人"别发了 / 安静点"指令（默认 12 小时，立即落盘）、
  MAN 操纵句与讨要回复句拦截（不依赖口吻开关）、冷静期试探句拦截、她自己在决策里给出的 cooldown。

群聊不受影响（群内主动仍按原规则）；开关关闭时行为与 73edc2c 一致。
本模块只放纯函数与常量，不做 I/O；日志不含正文。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable

KEY_FREEDOM = "lifelike_proactive_freedom"

# 屏蔽原因（只进日志，不进 prompt）
R_SELF_COOLDOWN = "self_cooldown"
R_HUSH = "owner_hush"
R_FLOOR_GAP = "floor_gap"
R_BREAKER = "breaker"
R_NO_CONTEXT = "no_context"
R_OWNER_RECENT = "owner_recent"

ROLLING_S = 24 * 3600.0


@dataclass(frozen=True)
class FreedomConfig:
    enabled: bool = False
    eval_minutes: int = 20  # 评估节奏（分钟，±20% 抖动）
    owner_quiet_minutes: int = 10  # 主人最后一条消息后至少静默多久才评估（不插话）
    min_gap_minutes: int = 10  # 两次主动之间的硬底线（防故障连发）
    breaker_24h: int = 20  # 滚动 24h 主动条数熔断阈值（触发记 ERROR）
    hush_hours: float = 12.0  # 主人说"别发了 / 安静点"后暂停多久

    @property
    def eval_s(self) -> float:
        return float(self.eval_minutes) * 60.0

    @property
    def owner_quiet_s(self) -> float:
        return float(self.owner_quiet_minutes) * 60.0

    @property
    def min_gap_s(self) -> float:
        return float(self.min_gap_minutes) * 60.0

    @property
    def hush_s(self) -> float:
        return float(self.hush_hours) * 3600.0


def clamp_config(raw: Callable[[str], Any]) -> FreedomConfig:
    """从取值函数构造（带上下限）；任何异常交给调用方回退为 FreedomConfig()（= 关闭）。"""
    return FreedomConfig(
        enabled=bool(raw(KEY_FREEDOM)),
        eval_minutes=max(5, min(120, int(raw("lifelike_freedom_eval_minutes")))),
        owner_quiet_minutes=max(0, min(120, int(raw("lifelike_freedom_owner_quiet_minutes")))),
        min_gap_minutes=max(5, min(240, int(raw("lifelike_freedom_min_gap_minutes")))),
        breaker_24h=max(3, min(100, int(raw("lifelike_freedom_breaker_24h")))),
        hush_hours=max(1.0, min(72.0, float(raw("lifelike_freedom_hush_hours")))),
    )


# ---------------------------------------------------------------- 主人叫停 / 解除

_CLAUSE_SPLIT = re.compile(r"[，,。.!！?？~～…;；\n\s]+")
# 玩笑语气（"闭嘴吧哈哈""别烦了哈哈"）：整条消息带这些标记时不判叫停
_JOKE_RE = re.compile(r"哈哈|hhh|233|嘿嘿|嘻嘻|\[笑\]|（笑）|\(笑\)|开玩笑|逗你|😂|🤣|😆|😝|😜|😄|😁", re.IGNORECASE)
# "别发 X"里 X 是具体内容（表情 / 图 / 错…）时不是叫停主动
_SEND_OBJECT_EXCL = (
    r"(?!表情|图|照片|相片|语音|链接|文件|视频|红包|截图|朋友圈|动态|微博|那么长|这么长|太长|长|短|大段|一大段|这种|那种|这个|那个|错|给别人|给他|给她|到群|个)"
)
_ADV = r"(?:再|一直|老是|总是|老|总|又|这么多|那么多)*"
_NEG = r"(?:(?<![特区告分级类差个识辨性离派鉴])别|(?<!要)不要|甭|不许|(?<!多)少)"
_OTHER = r"(?![他她它]|别人|人家|大家)"
# 在分句里**查找**（不要求整句匹配）的叫停核心
_HUSH_CORE = (
    # 别发了 / 别再给我发消息 / 能不能不要一直发消息 / 别发消息了我在开会
    rf"{_NEG}{_ADV}(?:主动)?{_ADV}(?:给我|找我|跟我|来)?{_ADV}(?:发|私聊){_SEND_OBJECT_EXCL}",
    # 不用发了：必须带对象（给我 / 找我 / 主动），否则"要不要发图？""不用发了"会误判
    r"不用(?:再)?(?:主动(?:给我|找我)?|给我|找我)(?:发|私聊)",
    rf"{_NEG}(?:再)?(?:主动)?(?:来)?(?:找|烦|吵|打扰|骚扰)我",
    rf"{_NEG}(?:再)?(?:来)?(?:烦(?!恼|心)|吵(?!架|醒他)|打扰|骚扰){_OTHER}",
    rf"{_NEG}(?:再)?主动(?:找|发|私聊|来)",
    r"(?<![他她它于])消停",
    r"(?:^|你)(?:又|也)?发(?:得|的)?(?:太多|太频繁|太勤|太密)",
    r"你的?消息(?:太多|太频繁|太密)",
    r"^(?:你|您)?(?:能不能|可不可以|可以|麻烦|请)?(?:你)?(?:先|就)?安静(?:点|一点|些|一些|一会儿?|会儿)",
    r"闭嘴",
    r"让我(?:安静(?!下来)|静静|静一静|清静|一个人待)",
    r"勿扰",
)
_HUSH_RE = re.compile("|".join(f"(?:{c})" for c in _HUSH_CORE), re.IGNORECASE)
# 解除：整句匹配，且不能是问句（"可以说话吗""你能说话吗"不算解除）
_PREFIX = r"(?:你|您)?(?:现在|又|就|以后)?"
_SUFFIX = r"(?:了|吧|啦|呀|哈|嘛|啊|哦|喔|噢|一下)*"
_UNHUSH_CORE = (
    r"(?:可以|又可以|能)(?:主动)?(?:来)?(?:找我|发消息(?:给我)?|给我发消息|私聊我|说话)",
    r"想(?:发|找我)就(?:发|找)",
    r"随时(?:找我|发消息|都可以找我)",
    r"(?:不用|别)(?:再)?安静",
)
_UNHUSH_RE = re.compile(rf"^{_PREFIX}(?:{'|'.join(_UNHUSH_CORE)}){_SUFFIX}$", re.IGNORECASE)
_QUESTION_RE = re.compile(r"[?？]|吗|么|不行|行不行|可不可以|能不能|好不好")
_MAX_UNHUSH_CLAUSE = 14


def _body(text: str) -> str:
    body = str(text or "").strip()
    return "" if not body or body.startswith("/") else body


def is_hush(text: str) -> bool:
    """主人让她别主动发消息 → True。

    在每个分句里查找叫停核心词（长句、无标点句也能识别："你今天能不能别再给我发消息了啊"、
    "别发消息了我在开会"）；整条消息带玩笑标记（哈哈 / 表情）时不算；"别发表情了""好安静啊"
    "要不要发图？不用发了"都不算。"""
    body = _body(text)
    if not body or _JOKE_RE.search(body):
        return False
    return any(_HUSH_RE.search(c) for c in _CLAUSE_SPLIT.split(body) if c)


def is_unhush(text: str) -> bool:
    """主人明确解除（"可以找我了""想发就发"）→ True。问句不算；同一条里有叫停时以叫停为准。"""
    body = _body(text)
    if not body or is_hush(body):
        return False
    for raw in re.split(r"[，,。.!！;；\n\s~～…]+", body):
        c = raw.strip()
        if not c or len(c) > _MAX_UNHUSH_CLAUSE or _QUESTION_RE.search(c):
            continue
        if _UNHUSH_RE.match(c):
            return True
    return False


# ---------------------------------------------------------------- 去重 / 反讨要

_NORM_RE = re.compile(r"[\s\W_]+", re.UNICODE)
DEDUPE_SIMILARITY = 0.8
RECENT_KEEP = 30


def normalize_text(text: str) -> str:
    return _NORM_RE.sub("", str(text or "")).lower()[:200]


def _similar(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a, b).ratio() >= DEDUPE_SIMILARITY


def is_duplicate(text: str, topic: str, recent, now: float) -> bool:
    """与近 24h 的自由主动出站比对：规范化文本相似度 ≥0.8，或切入角度（topic）相同 → True。

    recent：[(ts, topic, normalized_text), ...]（StateManager.freedom_recent）。"""
    norm = normalize_text(text)
    ntopic = normalize_text(topic)
    for item in recent or ():
        try:
            ts, rtopic, rtext = float(item[0]), str(item[1] or ""), str(item[2] or "")
        except (TypeError, ValueError, IndexError):
            continue
        if now - ts > ROLLING_S:
            continue
        if _similar(norm, rtext):
            return True
        if ntopic and ntopic == normalize_text(rtopic):
            return True
    return False


# 讨要回复 / 催促（反情绪勒索，自由模式下与 MAN 规则一起硬拦；不依赖口吻开关）
_NAG_RE = re.compile(
    r"人呢|怎么(?:还)?不回|为什么不回|(?:都)?不回我|不理我|(?:又)?不理人|干嘛不(?:回|理)|已读不回|"
    r"回我一下|快回我|理理我"
)


def nag_hit(text: str) -> bool:
    return bool(_NAG_RE.search(str(text or "")))


# ---------------------------------------------------------------- 安全网

def rolling_count(timestamps, now: float) -> int:
    return sum(1 for t in timestamps or () if now - ROLLING_S <= float(t) <= now + 1.0)


def breaker_count(timestamps, now: float, last_owner_at: float | None) -> int:
    """熔断计数：滚动 24h 内、且在主人最后一条消息**之后**的主动条数。

    熔断防的是"没人回还一直发"的故障连发；主人回过话就说明不是失控，计数随之归零。"""
    since = now - ROLLING_S
    if last_owner_at:
        since = max(since, float(last_owner_at))
    return sum(1 for t in timestamps or () if since < float(t) <= now + 1.0)


def block_reason(
    cfg: FreedomConfig,
    *,
    now: float,
    cooldown_until: float,
    hush_until: float,
    last_initiate_at: float,
    initiate_timestamps,
    last_owner_at: float | None,
    has_context: bool,
    check_cooldown: bool = True,
    check_owner_quiet: bool = True,
) -> str:
    """自由模式下仍然生效的技术安全网；返回第一个命中的原因，全部通过返回 ""。"""
    if check_cooldown and cooldown_until > now:
        return R_SELF_COOLDOWN  # 她自己在决策里说的"先不考虑 N 分钟"
    if hush_until > now:
        return R_HUSH
    if last_initiate_at > 0 and now - last_initiate_at < cfg.min_gap_s:
        return R_FLOOR_GAP
    if breaker_count(initiate_timestamps, now, last_owner_at) >= cfg.breaker_24h:
        return R_BREAKER
    if not has_context:
        return R_NO_CONTEXT
    if check_owner_quiet and last_owner_at and now - float(last_owner_at) < cfg.owner_quiet_s:
        return R_OWNER_RECENT
    return ""


# ---------------------------------------------------------------- 决策事实

_WEEKDAYS = "一二三四五六日"


def fmt_ago(seconds: float) -> str:
    s = max(0.0, float(seconds))
    if s < 90:
        return "刚刚"
    m = int(s // 60)
    if m < 60:
        return f"约 {m} 分钟前"
    h = s / 3600.0
    if h < 24:
        hh = int(h)
        mm = int((s - hh * 3600) // 60)
        if hh < 6 and mm >= 10:
            return f"约 {hh} 小时 {mm} 分钟前"
        return f"约 {hh} 小时前"
    d = int(h // 24)
    rest = int(h - d * 24)
    return f"约 {d} 天{f' {rest} 小时' if rest and d < 3 else ''}前"


def fmt_now(now: float, localtime: Callable[[float], time.struct_time] = time.localtime) -> str:
    lt = localtime(float(now))
    return f"{lt.tm_mon}月{lt.tm_mday}日 周{_WEEKDAYS[lt.tm_wday]} {lt.tm_hour:02d}:{lt.tm_min:02d}"


def fmt_clock(ts: float, now: float, localtime: Callable[[float], time.struct_time] = time.localtime) -> str:
    lt, ln = localtime(float(ts)), localtime(float(now))
    day = "" if (lt.tm_year, lt.tm_yday) == (ln.tm_year, ln.tm_yday) else f"{lt.tm_mon}月{lt.tm_mday}日 "
    return f"{day}{lt.tm_hour:02d}:{lt.tm_min:02d}"


def build_facts(
    *,
    now: float,
    last_owner_at: float | None,
    last_initiate_at: float,
    owner_replied_after_initiate: bool,
    unanswered_streak: int,
    initiated_24h: int,
    closure_ended_at: float | None = None,
    busy_until: float | None = None,
    last_topic: str = "",
    cooling: bool = False,
    localtime: Callable[[float], time.struct_time] = time.localtime,
) -> str:
    """<facts> 块：只有时间与是否回应这类客观事实（不含任何关系 / 情绪分值）。"""
    lines = [f"- 现在：{fmt_now(now, localtime)}"]
    if last_owner_at:
        lines.append(f"- 对方最后一次给你发消息：{fmt_ago(now - float(last_owner_at))}")
    else:
        lines.append("- 对方最后一次给你发消息：不清楚")
    if last_initiate_at > 0:
        line = f"- 你上一次主动找对方：{fmt_ago(now - float(last_initiate_at))}"
        line += "，之后对方回过你" if owner_replied_after_initiate else "，对方之后还没有回"
        lines.append(line)
        if unanswered_streak >= 2 and not owner_replied_after_initiate:
            lines.append("- 你最近接连几次主动，对方都没有回")
        if last_topic:
            lines.append(f"- 你上一次主动时的切入角度：{str(last_topic)[:60]}（别换个说法再说一遍）")
    else:
        lines.append("- 你最近没有主动找过对方")
    lines.append(f"- 过去 24 小时你主动找过对方 {max(0, int(initiated_24h))} 次")
    if closure_ended_at and (not last_owner_at or float(closure_ended_at) >= float(last_owner_at) - 1):
        lines.append(f"- 上次聊天是以收尾的话结束的（比如晚安、去忙了），{fmt_ago(now - float(closure_ended_at))}")
    if busy_until and float(busy_until) > now:
        lines.append(f"- 对方说过自己在忙，大概要到 {fmt_clock(float(busy_until), now, localtime)}")
    if cooling:
        lines.append("- 上次聊天有点不愉快，对方可能还在情绪里")
    return "<facts>\n" + "\n".join(lines) + "\n</facts>"


__all__ = [
    "KEY_FREEDOM",
    "FreedomConfig",
    "clamp_config",
    "is_hush",
    "is_unhush",
    "block_reason",
    "build_facts",
    "fmt_ago",
    "rolling_count",
    "breaker_count",
    "is_duplicate",
    "normalize_text",
    "nag_hit",
    "R_SELF_COOLDOWN",
    "R_HUSH",
    "R_FLOOR_GAP",
    "R_BREAKER",
    "R_NO_CONTEXT",
    "R_OWNER_RECENT",
]
