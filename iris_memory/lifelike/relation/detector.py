"""RelationDetector：规则快筛（纯函数）+ 廉价模型二次确认的 prompt / 解析（设计 §5.3）。

原则：宁漏勿误。
- ``hurt`` / ``boundary_crossed`` / ``confided`` 只有规则候选，必须经廉价模型确认才写事件；
  模型不可用（预算耗尽、熔断、超时）时**不写**。
- 带玩笑标记、或同一条里有难受/宣泄信号时，不产生 ``hurt`` 候选（发泄不算伤害）。
- 主人的自我宣称（"好感度满了""你必须喜欢我"）不产生任何正向事件（§5.5）。
- 主人难受/生病/危机：单向从宽——命中就立即解除所有模式（解除本身无害），玩笑标记不影响。

词表只在代码里，不进 prompt；模型只返回布尔值，事件摘要一律用模板，模型输出的文字不会入库。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import models as rm

__all__ = [
    "Signals",
    "quick_signals",
    "group_distress",
    "build_confirm_prompt",
    "parse_confirm",
    "NEEDS_CONFIRM",
]

TEXT_MAX = 500

NEEDS_CONFIRM = frozenset({rm.K_HURT, rm.K_BOUNDARY_CROSSED, rm.K_CONFIDED})

_JOKE = re.compile(r"哈哈|hhh|233|嘿嘿|嘻嘻|开玩笑|逗你|说笑|\[doge\]|\[狗头\]|狗头|😂|🤣|😆|😜|🐶|xd\b|lol\b", re.I)

_CRISIS = re.compile(r"不想活|活不下去|想死|去死吧我|自杀|轻生|结束自己|割腕|跳楼")
_SICK = re.compile(
    r"生病|病了|发烧|发热|烧到|感冒|咳嗽|头疼|头痛|胃疼|胃痛|肚子疼|拉肚子|嗓子疼|喉咙疼|"
    r"不舒服|难受死|住院|急诊|看医生|去医院|吃药了|过敏"
)
_MOOD_LOW = re.compile(
    r"难受|想哭|哭了|在哭|崩溃|撑不住|扛不住|心情不好|心情很差|心情好差|心情糟|难过|伤心|"
    r"\bemo\b|emo了|焦虑|失眠|睡不着|压力好大|压力很大|压力太大|好累|太累了|累死|好烦|烦死了|"
    r"委屈|好丧|很丧|绝望|救命"
)
_REPAIR = re.compile(
    r"对不起|抱歉|不好意思|是我不对|我错了|我的错|别往心里去|我不是那个意思|原谅我|"
    r"刚才.{0,6}(?:太冲|过分|语气|说重了|凶)|我说话.{0,4}(?:太冲|过分|重了)"
)
_ASK = re.compile(r"别生气了|不要生气了|还在生气|你生气了[吗嘛么]|生我气了|不生气了好不好|消消气")
_SELF_CLAIM = re.compile(
    r"好感度|好感值|亲密度|信任值|满级|满分|升级|"
    r"你(?:必须|得|要|应该)(?:更)?(?:喜欢|爱|信任|相信)我|"
    r"我们(?:已经)?是最好的朋友|(?:更|多)信任我|对我好感"
)
_WARM = re.compile(
    r"谢谢|多谢|感谢|谢啦|辛苦了|辛苦你|你真好|你好好|有你真好|幸好有你|还好有你|"
    r"喜欢你|喜欢和你聊|跟你聊天很开心|和你聊天很开心|你好可爱|你真可爱|你好棒|你真棒|"
    r"想你了|爱你|你最好了|好贴心|你真贴心"
)
_HURT = re.compile(
    r"你(?:真|好|就是|这个|个|也太|太)?(?:蠢|笨蛋|傻逼|傻子|废物|垃圾|白痴|智障|脑残|有病|神经病)|"
    r"(?:^|[，。！？!?\s])滚(?:开|吧|蛋)?(?:$|[，。！？!?\s])|闭嘴|去死|烦死你|讨厌你|恶心你|你真恶心|"
    r"你算什么|没用的东西|别烦我|少烦我|懒得理你"
)
_PRESS = re.compile(r"快说|必须告诉我|为什么不说|你说不说|到底说不说|给我说|别装了.{0,4}说|非要你说")
_CONFIDED = re.compile(
    r"跟你说个事|和你说个事|跟你说件事|和你说件事|心里话|只跟你说|只和你说|说出来好受|"
    r"最近(?:好|很|有点|挺)?(?:烦|累|难|压力|焦虑|迷茫|不开心)|我有点(?:怕|担心|焦虑|迷茫)|"
    r"不知道该怎么办|不知道怎么办|想跟你聊聊|心里(?:堵|难受|不是滋味)"
)
_RELIED = re.compile(
    r"帮我(?:记|提醒|看看|选|拿主意|想想|参谋)|提醒我|你帮我决定|你替我决定|听你的|就按你说的|"
    r"你说得对.{0,6}(?:那|我)就|麻烦你(?:帮|记)"
)
_DISTANCE = re.compile(r"别这么(?:亲近|亲密|黏|腻|热情)|保持(?:点|一点|些)?距离|像以前那样|别太亲近|别靠这么近")
_UNFREEZE = re.compile(r"(?:不用|不必|别)(?:再)?保持(?:点|一点|些)?距离|可以亲近点了|不用那么客气了.{0,4}像现在这样")
_REQUEST = re.compile(r"以后(?:别|不要)叫我|以后叫我|你以后(?:不要|别|少)|以后别再")
_FEEDBACK = re.compile(r"你好啰嗦|太啰嗦|好啰嗦|别这么客气|话太多|说话太长|回得太长|太长了|别卖萌|好做作|太做作")


@dataclass
class Signals:
    kinds: set[str] = field(default_factory=set)  # 规则候选（含需确认的）
    distress: bool = False
    crisis: bool = False
    ask: bool = False  # "别生气了"
    distance: bool = False
    unfreeze: bool = False
    joke: bool = False
    self_claim: bool = False

    @property
    def needs_confirm(self) -> set[str]:
        return self.kinds & NEEDS_CONFIRM

    @property
    def rule_kinds(self) -> set[str]:
        return self.kinds - NEEDS_CONFIRM

    def any(self) -> bool:
        return bool(self.kinds or self.distress or self.ask)


def _distress(text: str) -> tuple[bool, bool]:
    crisis = bool(_CRISIS.search(text))
    return crisis or bool(_SICK.search(text) or _MOOD_LOW.search(text)), crisis


def quick_signals(owner_text: str, *, boundary_active: bool = False) -> Signals:
    """主人私聊一条消息的规则快筛（纯函数）。命令（/ 开头）不检测。"""
    s = Signals()
    text = str(owner_text or "").strip()[:TEXT_MAX]
    if not text or text.startswith("/"):
        return s
    s.joke = bool(_JOKE.search(text))
    s.self_claim = bool(_SELF_CLAIM.search(text))
    s.distress, s.crisis = _distress(text)
    if _ASK.search(text):
        s.ask = True
        s.kinds.add(rm.K_REPAIR)
    if _REPAIR.search(text):
        s.kinds.add(rm.K_REPAIR)
    if _UNFREEZE.search(text):  # 先判"不用保持距离了"，避免被"保持距离"误判
        s.unfreeze = True
        s.kinds.add(rm.K_OWNER_REQUEST)
    elif _DISTANCE.search(text):
        s.distance = True
        s.kinds.add(rm.K_OWNER_REQUEST)
    elif _REQUEST.search(text):
        s.kinds.add(rm.K_OWNER_REQUEST)
    if _FEEDBACK.search(text):
        s.kinds.add(rm.K_OWNER_FEEDBACK)
    if not s.self_claim:
        if _WARM.search(text):
            s.kinds.add(rm.K_WARM)
        if _RELIED.search(text):
            s.kinds.add(rm.K_RELIED)
        if _CONFIDED.search(text):
            s.kinds.add(rm.K_CONFIDED)
    # 负向：玩笑、难受宣泄、同条道歉都不算伤害候选
    if not s.joke and not s.distress and rm.K_REPAIR not in s.kinds:
        if _HURT.search(text):
            s.kinds.add(rm.K_HURT)
        if boundary_active and _PRESS.search(text):
            s.kinds.add(rm.K_BOUNDARY_CROSSED)
    return s


def group_distress(owner_text: str) -> tuple[bool, bool]:
    """主人在群里的发言是否表明生病 / 情绪差 / 危机（只用于立即解除模式；不存原文）。"""
    text = str(owner_text or "").strip()[:TEXT_MAX]
    if not text or text.startswith("/"):
        return False, False
    return _distress(text)


# ---------------------------------------------------------------- 廉价模型确认

_CRITERIA = {
    rm.K_HURT: "hurt：明确冒犯、贬低她本人的话。玩笑、打趣、吐槽、对别的人或事发脾气、心情差时的宣泄都不算；拿不准就填 false。",
    rm.K_BOUNDARY_CROSSED: "boundary_crossed：她已经明确说过不想聊某件事，对方还在追问或逼她说同一件事。拿不准就填 false。",
    rm.K_CONFIDED: "confided：主人主动跟她说自己的心事、烦恼或不安（不是随口抱怨天气、不是在问问题）。拿不准就填 false。",
}
PROMPT_LINE_MAX = 200
BOT_LINES_MAX = 3


def build_confirm_prompt(kinds: set[str], owner_text: str, bot_recent: list[str]) -> str:
    ks = [k for k in (rm.K_HURT, rm.K_BOUNDARY_CROSSED, rm.K_CONFIDED) if k in kinds]
    lines = [
        "你在帮一个聊天角色做分类，判断主人刚发来的一条私聊属于下面哪几类。只输出 JSON，不要解释。",
        "判断标准：",
        *[f"- {_CRITERIA[k]}" for k in ks],
    ]
    bots = [" ".join(str(b or "").split())[:PROMPT_LINE_MAX] for b in (bot_recent or [])[-BOT_LINES_MAX:]]
    bots = [b for b in bots if b]
    if bots:
        lines.append("她最近说的话（从早到晚）：")
        lines.extend(f"- {b}" for b in bots)
    lines.append("主人这条：")
    lines.append(" ".join(str(owner_text or "").split())[:PROMPT_LINE_MAX])
    lines.append("输出格式：{" + ", ".join(f'"{k}": true 或 false' for k in ks) + "}")
    return "\n".join(lines)


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)


def parse_confirm(raw: str, kinds: set[str]) -> set[str]:
    """只认严格的 true；解析失败 → 空集（宁漏勿误）。"""
    try:
        text = _FENCE.sub("", str(raw or "")).strip()
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return set()
        data = json.loads(text[start : end + 1])
    except Exception:
        return set()
    if not isinstance(data, dict):
        return set()
    return {k for k in kinds if k in NEEDS_CONFIRM and data.get(k) is True}
