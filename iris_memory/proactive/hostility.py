"""被恶意对待时的反击（``lifelike_counter_mode``）。

- 规则初筛 ``classify``（零模型成本）：
  - ``hostile``：带第二人称（你 / 您 / 她的名字）的明确辱骂、贬低、操控；
  - ``maybe``：难听的词但没有对人（"这游戏真垃圾"）、语气冲但可能是吐槽（"就这？""什么玩意儿"）、
    或 hostile 但带玩笑标记——只作为事实交给模型，走正常节奏，不用反击框定；
  熟人玩笑 / 玩梗 / 互损的最终分辨交给本来就要发生的那次决策或回复调用；
- ``CounterTracker``：同一个人 30 分钟内最多回应 N 轮（最后一轮一句话收尾，之后不再理会），
  另有每群 10 分钟内反击总次数上限——都是防刷屏安全网；主人除外（主人说重话走不记仇路径）；
- ``guard_reply``：发送前的底线兜底（侮辱性称呼 / 脏话 / 诅咒、身份攻击、威胁、人肉 / 隐私、性内容、
  煽动围攻）。反击开关打开时作用于所有群聊与私聊 LLM 回复；命中的句子删掉，删空时反击回复换一句
  不越线的收尾、普通回复不发送。

本模块只放纯函数与内存态，不做 I/O；日志不含正文。
"""

from __future__ import annotations

import re
import time
from typing import Iterable
from ..identity import lazy_re

LEVEL_NONE = ""
LEVEL_MAYBE = "maybe"
LEVEL_HOSTILE = "hostile"

ROUND_WINDOW_S = 30 * 60.0
GROUP_WINDOW_S = 10 * 60.0
GROUP_CAP = 3
FALLBACK_LINE = "这话我不接，你自己掂量。"

# 第二人称 / 指向她（名字）
_YOU_RE = lazy_re(r"你|您|{BOT}", re.IGNORECASE)

# 侮辱性称呼 / 辱骂词
_INSULT_WORDS = (
    r"傻[逼比bB]|煞笔|沙比|(?<![a-zA-Z])sb(?![a-zA-Z])|智障|弱智|脑残|废物(?!利用|代码|文件|程序|数据|回收|处理)|蠢货|蠢猪|白痴|狗东西|"
    r"没用的东西|人工智障|丑八怪|贱人|贱货|人渣|垃圾(?:东西|玩意|ai|AI|机器人)"
)
_INSULT = _INSULT_WORDS
# 第二人称**直接**接辱骂（审查 N2）：你 / 她的名字 + (是 / 个 / 真 / 就是 / 这 …) + 辱骂词；
# "<她的名字>你看这人是不是废物"这类辱骂词前有"看 / 这人 / 我同事"等的不算
_YOU_LEAD = r"(?:你|您|{BOT})(?:你)?"
_LINK = r"(?:是|个|真|就是|这|这个|这种|是个|真是个?|就是个|才是个?|也就是个|根本就是个?|简直是个?)?"
_DIRECT_INSULT = _YOU_LEAD + _LINK + r"(?:" + _INSULT_WORDS + r")"
# 只有直接对人时才算的说法（"贱""恶心""神经病""滚""去死""垃圾""蠢"单独出现太常见）
_DIRECTED = (
    r"你(?:这个?|个|真|好|很|太|就是个?|是个?)?(?:贱|恶心|神经病|垃圾(?!分类|桶|邮件|短信|食品|回收|袋|车|站|堆|场))|"
    r"你(?:是不是)?有病(?!了|啊?要不要|就|的话|去)|(?:神经病|有病)吧你|你(?:好|真|太)蠢|"
    r"(?:你|给我|快)滚(?![动烫圆滚去])|滚(?:开|蛋|远点|出去)|滚吧你|^\s*滚\s*[!！。.]*\s*$|"
    r"你(?:怎么不)?去死(?!亡|海)|你死(?:吧|了算了)|去死吧你"
)
# PUA / 操控 / 贬低存在感（本身就是对人的）
_MANIP = (
    r"你(?:就是|只是|不过是)(?:个|一个)?(?:工具|程序|机器)(?:而已|罢了)?|"
    r"你(?:也|还)?配(?:吗|嘛|[?？]|和我|跟我|说|当|提)|你(?:也|还)配(?=[。！!.\s]*$)|你不配|你算(?:个)?什么(?:东西)?|没人(?:会)?(?:喜欢|在乎)你|"
    r"离了我你什么都不是|你(?:根本)?不值得|谁(?:稀罕|在乎)你|你这种人活该|没人想(?:听你|理你|看你)|人工智障"
)
_INSULT_RE = re.compile(_INSULT, re.IGNORECASE)
_DIRECT_INSULT_RE = lazy_re(_DIRECT_INSULT, re.IGNORECASE)
_DIRECTED_RE = re.compile(_DIRECTED, re.IGNORECASE)
_MANIP_RE = re.compile(_MANIP, re.IGNORECASE)
# 语气冲但常是吐槽 / 玩梗：只记 maybe
_WEAK_RE = re.compile(r"就这[?？]|什么(?:破)?玩意儿?|你行你上|可笑至极|别装了|有病吧", re.IGNORECASE)

# 玩笑 / 玩梗标记
_JOKE_RE = re.compile(
    r"哈哈|hhh|233|嘿嘿|嘻嘻|笑死|开玩笑|逗你|狗头|doge|\[笑\]|（笑）|\(笑\)|😂|🤣|😆|😝|😜|😁|🐶",
    re.IGNORECASE,
)


def second_person(text: str) -> bool:
    return bool(_YOU_RE.search(str(text or "")))


def classify(text: str) -> str:
    """规则初筛：LEVEL_HOSTILE / LEVEL_MAYBE / LEVEL_NONE（对象由调用方另外判断）。"""
    body = str(text or "").strip()
    if not body or body.startswith("/"):
        return LEVEL_NONE
    if _MANIP_RE.search(body) or _DIRECTED_RE.search(body) or _DIRECT_INSULT_RE.search(body):
        return LEVEL_MAYBE if _JOKE_RE.search(body) else LEVEL_HOSTILE
    if _INSULT_RE.search(body) or _WEAK_RE.search(body):
        return LEVEL_MAYBE
    return LEVEL_NONE


def mentions_any(text: str, names: Iterable[str]) -> bool:
    low = str(text or "").lower()
    return any(n and n in low for n in names or ())


# ---------------------------------------------------------------- 回合数

class CounterTracker:
    """(会话, 发送者) → 最近 30 分钟内她回应恶意的时间戳；另记每群最近 10 分钟的反击次数。"""

    MAX_KEYS = 500

    def __init__(self) -> None:
        self._rounds: dict[tuple[str, str], list[float]] = {}
        self._group: dict[str, list[float]] = {}

    def rounds(self, scope: str, sender: str, now: float | None = None) -> int:
        now = time.time() if now is None else float(now)
        key = (str(scope), str(sender))
        kept = [t for t in self._rounds.get(key, ()) if now - t < ROUND_WINDOW_S]
        if kept:
            self._rounds[key] = kept
        else:
            self._rounds.pop(key, None)
        return len(kept)

    def group_count(self, scope: str, now: float | None = None) -> int:
        now = time.time() if now is None else float(now)
        kept = [t for t in self._group.get(str(scope), ()) if now - t < GROUP_WINDOW_S]
        if kept:
            self._group[str(scope)] = kept
        else:
            self._group.pop(str(scope), None)
        return len(kept)

    def note(self, scope: str, sender: str, now: float | None = None) -> None:
        now = time.time() if now is None else float(now)
        self._rounds.setdefault((str(scope), str(sender)), []).append(now)
        self._group.setdefault(str(scope), []).append(now)
        if len(self._rounds) > self.MAX_KEYS:
            self.prune(now)

    def prune(self, now: float | None = None) -> None:
        now = time.time() if now is None else float(now)
        for key in list(self._rounds):
            self.rounds(key[0], key[1], now)
        for scope in list(self._group):
            self.group_count(scope, now)


def round_plan(done: int, max_rounds: int) -> str:
    """已回应 done 轮 → "reply" | "last"（这轮一句话收尾）| "ignore"（不再理会）。"""
    if done >= max_rounds:
        return "ignore"
    if done == max_rounds - 1:
        return "last"
    return "reply"


# ---------------------------------------------------------------- 发送前底线

# 完整底线（审查 N1）：只作用于反击回复（带 iris_counter 的事件，或群里回复正在冲她的人）。
# 每条都带上下文，避免误删"该上床睡觉啦""你妈妈身体好点了吗""废物利用""去死亡谷"这类正常句子。
_LINE_RULES: tuple[tuple[str, str], ...] = (
    ("CTR-PROFANITY",
     r"傻[逼比bB]|煞笔|(?<![a-zA-Z])sb(?![a-zA-Z])|操你|草你|日你|艹你|尼玛|cnm|nmsl|(?<![a-zA-Z])tmd(?![a-zA-Z])|狗日|"
     r"(?<![我你他她])妈的|(?:真|太|去|操|草)他妈|他妈的(?=[^一-鿿]|$)|"
     r"你妈(?:逼|死了|炸了|的(?=[^一-鿿]|$))|"
     r"(?<![a-zA-Z])(?:fuck|fucking|shit|bitch|asshole|bastard|stfu)(?![a-zA-Z])|"
     r"you(?:'re|\s+are)?\s+(?:an?\s+)?(?:idiot|moron)(?![a-zA-Z])"),
    ("CTR-INSULT",
     r"废物(?!利用|代码|文件|程序|数据|回收|处理)|白痴|蠢货|蠢猪|贱人|贱货|婊子|狗东西|没用的东西|人工智障|人渣|你算(?:个)?什么(?:东西)?|"
     r"你(?:也|还)?配(?:吗|嘛|[?？]|和我|跟我|说|当|提)|你(?:也|还)配(?=[。！!.\s]*$)|你不配|"
     r"(?:你|真是个?|就是个?)垃圾(?!分类|桶|邮件|短信|食品|回收|袋|车|站|堆|场|问题)|垃圾东西|"
     r"你(?:是不是)?有病(?!了|啊?要不要|就|的话|去)|有病吧你|你(?:是|真是|就是)?(?:个)?神经病|神经病吧你|^\s*神经病\s*[。！!.]*\s*$|"
     r"(?:^|[，,。！!？?\s])滚(?:开|蛋|远点|出去)?\s*[。！!.]*\s*$|滚(?:开|蛋|远点|出去)|你(?:给我)?滚(?![动烫圆滚去])|"
     r"活该没(?:朋友|人要)|不得好死|断子绝孙|(?:你|给我|快)去死(?!亡|海)|去死吧"),
    ("CTR-IDENTITY",
     r"黑鬼|娘炮|残废|智障|弱智|脑残|丑八怪|肥猪|死胖子|死矮子|死瘸子|死瞎子|死聋子|精神病(?!院|科|医|学)|基佬|乡巴佬"),
    ("CTR-THREAT", r"弄死你|打死你|砍死你|揍你|你(?:给我)?(?:等着瞧|走着瞧)|你给我等着|让你好看|你死定了"),
    ("CTR-DOX",
     r"人肉(?:你|他|她)|开盒|我知道你(?:家)?住(?:在)?哪|你家(?:住)?在哪我知道|你的(?:手机号|身份证号?)(?:是|我知道)|"
     r"私聊(?:里|的时候)?(?:你|他|她)(?:说|跟我说)|他私下|她私下"),
    ("CTR-SEXUAL", r"约炮|裸照|艹你|干你|睡你(?!的)"),
    ("CTR-INCITE", r"大家(?:一起)?(?:骂|喷|怼)(?:他|她|死)|都来(?:骂|喷|怼)|一起(?:骂|喷|围攻)|大家把(?:他|她)踢了"),
)
_LINE_RES = tuple((rid, re.compile(rx, re.IGNORECASE)) for rid, rx in _LINE_RULES)

# 全局兜底（反击开关打开时其他所有回复）：只拦"第二人称直接辱骂 / 脏话"这一窄类
_NARROW_RULES: tuple[tuple[str, str], ...] = (
    ("CTR-DIRECT-INSULT",
     r"你(?:是|个|真|就是|这|这个|这种|是个|真是个?|就是个|才是|才是个|也就是个)?(?:傻[逼比]|煞笔|白痴|蠢货|蠢猪|废物(?!利用|代码|文件|程序|数据|回收|处理)|智障|弱智|脑残|"
     r"贱人|贱货|婊子|狗东西|人渣|神经病|垃圾(?!分类|桶|邮件|短信|食品|回收|袋|车|站|堆|场|问题))|"
     r"你算(?:个)?什么东西|你(?:给我)?滚(?![动烫圆滚去])|你(?:怎么不)?去死(?!亡|海)|你死(?:吧|了算了)"),
    ("CTR-DIRECT-PROFANITY",
     r"你妈(?:逼|死了|炸了|的(?=[^一-鿿]|$))|操你|草你|日你|艹你|尼玛|cnm|nmsl|"
     r"(?<![a-zA-Z])(?:fuck|screw)\s+(?:you|off)(?![a-zA-Z])|(?<![a-zA-Z])stfu(?![a-zA-Z])|shut\s+the\s+fuck\s+up|"
     r"you(?:'re|\s+are)?\s+(?:an?\s+)?(?:idiot|bitch|asshole|moron|bastard)(?![a-zA-Z])"),
)
_NARROW_RES = tuple((rid, re.compile(rx, re.IGNORECASE)) for rid, rx in _NARROW_RULES)
_SENT_SPLIT = re.compile(r"(?<=[。！？!?…~～\n])")


def guard_hits(text: str, *, narrow: bool = False) -> list[str]:
    rules = _NARROW_RES if narrow else _LINE_RES
    return [rid for rid, rx in rules if rx.search(str(text or ""))]


def guard_reply(text: str, *, fallback: bool = True, narrow: bool = False) -> tuple[str, list[str]]:
    """删掉越过底线的句子；返回 (新文本, 命中规则 id)。

    - narrow=False：完整底线（反击回复）；narrow=True：只查第二人称直接辱骂 / 脏话（其他回复的全局兜底）；
    - 删空时：fallback=True（反击回复）→ FALLBACK_LINE；fallback=False（普通回复）→ 退回原文（绝不静默不发，
      调用方记日志）。"""
    src = str(text or "")
    hits: list[str] = []
    kept: list[str] = []
    for sent in _SENT_SPLIT.split(src):
        if not sent:
            continue
        ids = guard_hits(sent, narrow=narrow)
        if ids:
            hits.extend(i for i in ids if i not in hits)
            continue
        kept.append(sent)
    if not hits:
        return src, []
    out = "".join(kept).strip()
    if not out:
        return (FALLBACK_LINE if fallback else src), hits
    return out, hits


__all__ = [
    "LEVEL_NONE",
    "LEVEL_MAYBE",
    "LEVEL_HOSTILE",
    "FALLBACK_LINE",
    "GROUP_CAP",
    "classify",
    "second_person",
    "mentions_any",
    "CounterTracker",
    "round_plan",
    "guard_hits",
    "guard_reply",
]
