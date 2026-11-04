"""RelationNarrator：把近期基调与关系渐进阶段写成 ``<你们之间>`` 文字段（纯函数，附录 B）。

- 只有文字，不含任何数字；每行都过 NumericLeakGuard（strict）与禁用词表
  （等级/Lv/进度/还差/%/分/好感/升级……），命中整行丢弃并 WARN（不记正文）。
- 晋升证据种类、门槛、"倾诉会拉近关系"之类因果从不出现（§5.7.1 第 5 条）。
- 冷静期措辞按设计修订版：照常认真回应，不刻意缩短、不冷淡。
- U8（11-03 用户拍板）：关系状态不通过"少找他"表达情绪；冷静期只影响语气与内容，
  主动消息（purpose=proactive）另有一句"不试探、不提别扭、不讨要关注"。
"""

from __future__ import annotations

import re

from astrbot.api import logger

from ..guards import numeric
from . import models as rm

__all__ = ["render", "line_ok", "OPEN", "CLOSE"]

OPEN = "<你们之间>"
CLOSE = "</你们之间>"

_FORBIDDEN = re.compile(
    r"\d|等级|lv|进度|还差|%|％|分数|积分|好感|亲密度|信任度|升级|晋升|降级|升阶|降阶|门槛|解锁", re.I
)

LINE_REUNION = "有阵子没聊了：先自然寒暄，别抱怨，也别提他没来。"
LINE_BOUNDARY = "有件事你说过不太想聊：他要是又问起，平静地说一次就够，不反复提，也不因为这个冷淡；别的事照常认真回应。"
_TAIL_AWKWARD = "说话会收着一点，少开玩笑，不主动分享生活；他的话照常认真回应，该说多少说多少，不刻意缩短，也不冷淡。"
LINE_MADE_UP_FIRST = "刚和好：他刚才解释了。如果自然的话，可以说一句刚才为什么别扭，说一次就够，别翻旧账。"
LINE_MADE_UP_AFTER = "刚和好：已经没事了，照常聊，别翻旧账。"
LINE_PAUSE = "冷静一下：说话收着一点，不提刚才的事；他来找你时照常认真回应，不冷淡。"
_TAIL_AWKWARD_PROACTIVE = (
    "这次是你主动找他：语气平常、收着一点，只说想说的这件事；不试探（不问在不在、是不是生气了），"
    "不提刚才的别扭，不讨要关注。"
)
LINE_BOUNDARY_PROACTIVE = "有件事你说过不太想聊：这次主动找他时不提那件事，也不因为这个冷淡。"


def line_ok(line: str) -> bool:
    return not _FORBIDDEN.search(line) and not numeric.scan(line, strict=True)


def _tone_line(view: rm.RelationView, explained: bool, proactive: bool = False) -> str:
    reason = view.reasons[0] if view.reasons else ""
    if view.stage == rm.TONE_AWKWARD:
        tail = _TAIL_AWKWARD_PROACTIVE if proactive else _TAIL_AWKWARD
        return f"有点别扭：{reason}。{tail}" if reason else f"有点别扭：{tail}"
    if view.stage == rm.TONE_MADE_UP:
        return LINE_MADE_UP_AFTER if explained else LINE_MADE_UP_FIRST
    if view.stage == rm.TONE_CLOSE:
        tail = "可以多分享一点自己的事，玩笑也可以多一点。"
        return f"最近挺亲近：{reason}。{tail}" if reason else f"最近挺亲近：{tail}"
    if view.stage == rm.TONE_PAUSE:
        return f"{LINE_PAUSE}{_TAIL_AWKWARD_PROACTIVE}" if proactive else LINE_PAUSE
    return ""


def render(
    view: rm.RelationView | None,
    bond: rm.BondView | None,
    *,
    explained: bool = False,
    reunion: bool = False,
    purpose: str = "reply",
) -> str:
    """返回 ``<你们之间>…</你们之间>``；没有可说的行返回 ''。"""
    lines: list[str] = []
    if bond is not None:
        lines.append(f"{rm.STAGE_NAMES.get(bond.stage, rm.STAGE_NAMES['s1'])}：{rm.STAGE_DESC.get(bond.stage, rm.STAGE_DESC['s1'])}")
    if reunion:
        lines.append(LINE_REUNION)
    proactive = purpose == "proactive"
    if view is not None:
        tl = _tone_line(view, explained, proactive)
        if tl:
            lines.append(tl)
        if view.boundary_active:
            lines.append(LINE_BOUNDARY_PROACTIVE if proactive else LINE_BOUNDARY)
    kept = []
    for ln in lines:
        if line_ok(ln):
            kept.append(ln)
        else:
            logger.warning(f"{rm.LOG} narrator dropped line reason=forbidden")
    return "\n".join([OPEN, *kept, CLOSE]) if kept else ""
