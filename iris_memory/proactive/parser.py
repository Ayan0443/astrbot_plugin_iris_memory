from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger
from ..utils.log_redact import text_for_log

_INITIATE_TOPIC_SOURCES = {"recent_context", "time_context", "shared_interest"}


@dataclass
class Decision:
    """LLM 统一决策的结构化结果。"""

    action: str = "none"  # "speak" | "none"
    mode: str = ""  # 确认后的发言模式（跟随请求动机）
    message: str = ""  # 指令要求直出时的发言内容
    topic: str = ""  # initiate 模式下 LLM 自选的切入角度
    why_now: str = ""  # 为什么此刻说，比话题本身更重要
    topic_source: str = ""  # recent_context / time_context / shared_interest
    observation: str = ""
    watch: list[str] = field(default_factory=list)
    watch_keywords: list[str] = field(default_factory=list)
    why: str = ""
    drifted: bool = False
    cooldown_minutes: int = 0
    parse_failed: bool = False
    item_id: str | None = None  # 主动发起围绕的连续性候选事项（由调用方按候选集校验）

    @property
    def should_speak(self) -> bool:
        return self.action == "speak"


def extract_json(text: str) -> dict | None:
    """从 LLM 输出中提取第一个合法 JSON 对象。

    依次尝试：markdown 代码块剥离 -> True/False 修正 -> 整体解析 ->
    括号配平扫描（跳过字符串内部的花括号）。
    """
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()

    text = re.sub(r'"\s*:\s*True', '": true', text)
    text = re.sub(r'"\s*:\s*False', '": false', text)

    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    in_string = False
    escape = False
    brace_count = 0
    start = -1
    for i, c in enumerate(text):
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            if brace_count == 0:
                start = i
            brace_count += 1
        elif c == "}":
            brace_count -= 1
            if brace_count == 0 and start >= 0:
                try:
                    obj = json.loads(text[start : i + 1])
                    if isinstance(obj, dict):
                        return obj
                except json.JSONDecodeError:
                    start = -1

    return None


def parse_bool(val: Any) -> bool:
    if isinstance(val, str):
        return val.lower() in ("true", "yes", "1")
    if isinstance(val, (int, float)):
        return bool(val)
    return bool(val)


def parse_string_list(raw: Any, max_len: int = 10) -> list[str]:
    if not isinstance(raw, list):
        return []
    result = [str(u).strip() for u in raw if str(u).strip()]
    return result[:max_len]


def _parse_action(raw: Any, fallback_reply: Any) -> str:
    """解析 action 字段；缺失时兼容旧的 reply 布尔字段。"""
    if raw is not None:
        text = str(raw).strip().lower()
        if text in ("speak", "reply", "yes", "true"):
            return "speak"
        if text in ("none", "skip", "no", "false", ""):
            return "none"
    return "speak" if parse_bool(fallback_reply) else "none"


def _parse_cooldown(raw: Any) -> int:
    try:
        minutes = int(float(raw))
    except (TypeError, ValueError):
        return 0
    if minutes <= 0:
        return 0
    return min(minutes, 120)


def parse_decision(text: str, mode: str = "", *, speak_wins: bool = False) -> Decision:
    """解析 LLM 统一决策输出，兼容多种字段命名。

    speak_wins=True（U8 / U9 打开且聊天欲望调整打开时）：她明确决定 speak 时，
    同时给出的 drifted（旧话题结束）/ cooldown 不再把这次开口改成 none——drifted 仍原样返回
    （调用方据此关闭旧锚点），cooldown 归零；主动开题 topic 为空时用 obs 补、仍为空则不说，缺 why_now / topic_source 时补默认依据。
    默认 False = 原有解析。"""
    obj = extract_json(text)
    if not obj:
        logger.warning("Iris Reply: decision JSON parse failed, raw text: %s", text_for_log(text, 300))
        return Decision(mode=mode, parse_failed=True)

    action = _parse_action(obj.get("action"), obj.get("reply"))
    message = str(obj.get("message", "") or "").strip()
    topic = str(obj.get("topic", "") or "").strip()
    why_now = str(obj.get("why_now", "") or "").strip()
    topic_source = str(obj.get("topic_source", "") or "").strip()
    observation = str(obj.get("obs", obj.get("observation", "")))
    watch = parse_string_list(obj.get("watch", obj.get("follow_up_users", [])))
    watch_keywords = parse_string_list(
        obj.get("watch_keywords", obj.get("follow_up_keywords", [])),
        max_len=10,
    )
    why = str(obj.get("why", obj.get("interest_reason", "")))
    drifted = parse_bool(obj.get("drifted", obj.get("topic_drifted", False)))
    cooldown = _parse_cooldown(obj.get("cooldown", 0))
    raw_item_id = obj.get("item_id")
    if raw_item_id in (None, "") and isinstance(obj.get("candidate_id"), str):
        # 活人感 P1-8：决策 JSON 的 candidate_id 与 item_id 等价（仍由调用方按候选集校验）
        raw_item_id = obj.get("candidate_id")
    item_id = (
        str(raw_item_id).strip()
        if isinstance(raw_item_id, (str, int)) and not isinstance(raw_item_id, bool)
        else ""
    )

    # 防御：不要求发言时忽略 message/topic；drifted 时不应发言
    if action != "speak":
        message = ""
        topic = ""
        why_now = ""
        topic_source = ""
    if speak_wins and action == "speak":
        cooldown = 0
    if drifted and action == "speak" and not speak_wins:
        action = "none"
        message = ""
        topic = ""
        why_now = ""
        topic_source = ""
    # 主动开题必须说明“为什么是现在”以及依据来源；缺少现场理由时宁可不说。
    if speak_wins and mode == "initiate" and action == "speak":
        # 审查 M8：仍要有切入角度——topic 为空用 obs 补，仍为空就不说；why_now / topic_source 缺了补默认依据
        if not topic:
            topic = observation.strip()[:60]
        if not topic:
            action = "none"
            message = why_now = topic_source = ""
        elif topic_source not in _INITIATE_TOPIC_SOURCES:
            topic_source = "recent_context"
    if not speak_wins and mode == "initiate" and action == "speak" and not (
        topic and why_now and topic_source in _INITIATE_TOPIC_SOURCES
    ):
        action = "none"
        message = ""
        topic = ""
        why_now = ""
        topic_source = ""

    return Decision(
        action=action,
        mode=mode,
        message=message,
        topic=topic,
        why_now=why_now,
        topic_source=topic_source,
        observation=observation,
        watch=watch,
        watch_keywords=watch_keywords,
        why=why,
        drifted=drifted,
        cooldown_minutes=cooldown,
        item_id=(item_id or None) if action == "speak" else None,
    )
