"""场景档位判定（ROADMAP P1-2，口吻附录 §4.2、§4.3、§4.5）。

纯函数、零 LLM、不做 I/O。只看本轮对方的消息文本和会话类型（私聊 / 群聊），
**不读取任何关系数值**，也不随互动次数升档（§4.3）。

这是新写的判定函数：``learning.expression.classify_scene`` 的四类标签已经落在
``expression_pattern.scene`` 列里，**不能改**（附录 §5.3 工程评审）。

判定顺序 = 从"必须淡"到"可以浓"：被说做作 → 认真问身份 → 认真求助 → 难过 →
要走要睡 → 问看法 → 跟随一轮 → 被夸 → 开玩笑 → 好消息 → 偏好 / 打算 →
打招呼 → 普通问答 → 闲聊。多个信号同时命中时取更靠前（更淡）的那个，
即"判不准往淡的方向取"。

档位与卖萌档在代码里用整数比较大小，**只在拼 prompt 时映射成文字**
（淡 / 中 / 浓；不卖萌 / 很少 / 偶尔），数字不出本模块。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from ..lifelike.style.guard import FOLLOW_TRIGGERS, WORK_MENTION, is_technical
from ..identity import lazy_re

# ---------------------------------------------------------------- 档位

REG_OFF = 0
REG_LIGHT = 1
REG_MEDIUM = 2
REG_STRONG = 3

CUTE_OFF = 0
CUTE_RARE = 1
CUTE_OCCASIONAL = 2

# 配置值（ASCII）→ 内部档位；未知值按 off 处理（安全方向）
REGISTER_BY_NAME: Dict[str, int] = {
    "off": REG_OFF,
    "light": REG_LIGHT,
    "medium": REG_MEDIUM,
    "strong": REG_STRONG,
}
CUTE_BY_NAME: Dict[str, int] = {
    "off": CUTE_OFF,
    "rare": CUTE_RARE,
    "occasional": CUTE_OCCASIONAL,
}
REGISTER_NAME: Dict[int, str] = {v: k for k, v in REGISTER_BY_NAME.items()}
CUTE_NAME: Dict[int, str] = {v: k for k, v in CUTE_BY_NAME.items()}

# prompt 里只出现这些文字
REGISTER_WORD: Dict[int, str] = {REG_LIGHT: "淡", REG_MEDIUM: "中", REG_STRONG: "浓"}
CUTE_WORD: Dict[int, str] = {CUTE_OFF: "不卖萌", CUTE_RARE: "很少", CUTE_OCCASIONAL: "偶尔"}


def parse_register(raw: object, default: int = REG_OFF) -> int:
    name = str(raw or "").strip().lower()
    return REGISTER_BY_NAME.get(name, default)


def parse_cute(raw: object, default: int = CUTE_OFF) -> int:
    name = str(raw or "").strip().lower()
    return CUTE_BY_NAME.get(name, default)


# ---------------------------------------------------------------- 场景

SCENE_FEEDBACK = "feedback"  # 被说做作 / 装可爱 / 嗲 / 翻译腔
SCENE_IDENTITY = "identity"  # 认真问身份、问是不是在模仿谁
SCENE_HELP = "help"  # 技术问题 / 认真求助
SCENE_SAD = "sad"  # 倾诉难过、委屈、累
SCENE_LEAVING = "leaving"  # 要走、要睡、不聊了
SCENE_OPINION = "opinion"  # 问看法、争议
SCENE_FOLLOW = "follow"  # 对方先用二次元腔（跟随一轮）
SCENE_PRAISED = "praised"  # 被夸
SCENE_JOKE = "joke"  # 开玩笑 / 群里玩梗
SCENE_GOOD_NEWS = "good_news"  # 分享开心的事
SCENE_DISAGREE = "disagree"  # 说了自己的偏好或打算（可能要不同意）
SCENE_GREET = "greet"  # 打招呼、隔了一阵回来
SCENE_QUESTION = "question"  # 普通问答
SCENE_CHAT = "chat"  # 闲聊
SCENE_PROACTIVE = "proactive"  # 她主动找对方（P1-4）
SCENE_UNKNOWN = "unknown"  # 没有可用文本

# (私聊档位, 私聊卖萌档, 群聊档位, 群聊卖萌档)；附录 §4.2、§4.5 两张表
_SCENE_TABLE: Dict[str, Tuple[int, int, int, int]] = {
    SCENE_FEEDBACK: (REG_LIGHT, CUTE_OFF, REG_LIGHT, CUTE_OFF),
    SCENE_IDENTITY: (REG_LIGHT, CUTE_OFF, REG_LIGHT, CUTE_OFF),
    SCENE_HELP: (REG_LIGHT, CUTE_OFF, REG_LIGHT, CUTE_OFF),
    SCENE_SAD: (REG_LIGHT, CUTE_OFF, REG_LIGHT, CUTE_OFF),
    SCENE_LEAVING: (REG_LIGHT, CUTE_OFF, REG_LIGHT, CUTE_OFF),
    SCENE_OPINION: (REG_LIGHT, CUTE_OFF, REG_LIGHT, CUTE_OFF),
    SCENE_FOLLOW: (REG_MEDIUM, CUTE_RARE, REG_MEDIUM, CUTE_OFF),
    SCENE_PRAISED: (REG_MEDIUM, CUTE_OCCASIONAL, REG_MEDIUM, CUTE_RARE),
    SCENE_JOKE: (REG_STRONG, CUTE_OCCASIONAL, REG_MEDIUM, CUTE_RARE),
    SCENE_GOOD_NEWS: (REG_STRONG, CUTE_RARE, REG_MEDIUM, CUTE_OFF),
    SCENE_DISAGREE: (REG_MEDIUM, CUTE_RARE, REG_LIGHT, CUTE_OFF),
    SCENE_GREET: (REG_MEDIUM, CUTE_RARE, REG_LIGHT, CUTE_OFF),
    SCENE_QUESTION: (REG_MEDIUM, CUTE_RARE, REG_LIGHT, CUTE_OFF),
    SCENE_CHAT: (REG_STRONG, CUTE_OCCASIONAL, REG_LIGHT, CUTE_OFF),
    SCENE_PROACTIVE: (REG_MEDIUM, CUTE_RARE, REG_MEDIUM, CUTE_OFF),
    SCENE_UNKNOWN: (REG_MEDIUM, CUTE_RARE, REG_LIGHT, CUTE_OFF),
}

# 场景的中文说法（进 prompt）
_SCENE_LABEL: Dict[str, Tuple[str, str]] = {
    SCENE_FEEDBACK: ("对方觉得你刚才说话有点做作", "群里有人觉得你说话有点做作"),
    SCENE_IDENTITY: ("对方在认真问你的身份", "群里有人在认真问你的身份"),
    SCENE_HELP: ("对方在认真求助，需要讲清楚", "群里在认真讨论或问技术问题"),
    SCENE_SAD: ("对方在倾诉难过或委屈", "群里有人在吐槽或不开心"),
    SCENE_LEAVING: ("对方要走了或要睡了", "群里有人要走了"),
    SCENE_OPINION: ("对方在问你的看法", "群里有人问你的看法"),
    SCENE_FOLLOW: ("对方在用二次元腔开玩笑", "群友在用二次元腔开玩笑"),
    SCENE_PRAISED: ("对方在夸你", "群友在夸你"),
    SCENE_JOKE: ("对方在开玩笑", "群里在玩梗、接玩笑"),
    SCENE_GOOD_NEWS: ("对方在分享开心的事", "群友在分享开心的事"),
    SCENE_DISAGREE: ("对方说了自己的偏好或打算", "群友说了自己的偏好或打算"),
    SCENE_GREET: ("对方来打招呼", "群友来打招呼"),
    SCENE_QUESTION: ("私聊普通问答", "群聊普通问答"),
    SCENE_CHAT: ("私聊闲聊", "群聊普通闲聊"),
    SCENE_PROACTIVE: ("你主动找对方开口", "你主动在群里开口"),
    SCENE_UNKNOWN: ("私聊", "群聊"),
}

# 场景附带的一句做法提示（进 prompt；不含数字）
SCENE_HINT: Dict[str, str] = {
    SCENE_FEEDBACK: "不辩解、不分析自己、不说要换口吻，接下来直接正常说话。",
    SCENE_IDENTITY: "好好回答，不扮演、不暗示自己是哪个作品里的角色。",
    SCENE_HELP: "开头最多一个小反应，正文和平时一样清楚完整。",
    SCENE_SAD: "先回应那件具体的事，回得短一点，不讲道理，不急着逗对方开心。",
    SCENE_LEAVING: "痛快放人，不挽留。",
    SCENE_OPINION: "先给态度，再给一个理由，不当和事佬。",
    SCENE_FOLLOW: "可以顺着接一下梗，只这一轮，之后回到平常口吻。",
}

# ---------------------------------------------------------------- 信号词（只在代码里）

_FEEDBACK_RE = re.compile(
    r"做作|装可爱|嗲|翻译腔|字幕(?:组|腔)|二次元腔|别卖萌|卖什么萌|又(?:在)?卖萌|好腻|太腻|腻歪|矫情|"
    r"演(?:得|的)?(?:好|太)?(?:假|夸张)|好假"
)
_IDENTITY_RE = re.compile(
    r"模仿(?:哪个|谁|什么)|你是(?:不是)?(?:在)?(?:模仿|照着|扮演)|"
    r"(?:动漫|动画|二次元|番|作品|游戏)(?:里|中)?(?:的)?(?:哪个)?角色|"
    r"你(?:到底)?是(?:不是)?(?:真人|ai|AI|机器人|人工智能|程序)|你到底是(?:谁|什么)|你是谁"
)
_HELP_EXTRA_RE = re.compile(
    r"端口|进程|服务器|nginx|反向代理|编程|程序报错|error|exception|traceback|命令行|终端|部署|"
    r"正则|\bapi\b|http|ssl|证书|显卡|驱动|路由器|重装系统|"
    r"要注意什么|注意事项|怎么办|该怎么|怎么处理|怎么解决|如何处理|求助|帮我看看|帮忙看|"
    r"合同|押金|法律|律师|报销|签证|医院|看病|挂号|吃什么药|症状|保险|贷款|交税|个税|简历",
    re.IGNORECASE,
)
_SAD_RE = re.compile(
    r"难过|难受|伤心|委屈|想哭|哭了|好累|太累|累死|心累|好烦|烦死|崩溃|郁闷|失望|焦虑|压力(?:好|太|很)?大|"
    r"\bemo\b|不开心|被骂|挨骂|骂了|挨批|没过|挂科|白学|失恋|分手|吵架|放鸽子|放我鸽子|鸽了我|孤独|"
    r"睡不着|失眠|生病|病了|发烧|好疼|头疼|胃疼",
    re.IGNORECASE,
)
_LEAVING_RE = re.compile(
    r"先睡了|去睡了|睡觉了|要睡了|我睡了|晚安|先撤|先走了|下线了|先下了|不聊了|拜拜|再见|回头聊|"
    r"先忙了|去忙了|先去(?:洗澡|吃饭|忙|工作|上课|上班)"
)
_OPINION_RE = re.compile(
    r"你觉得|你怎么看|你认为|你的看法|你站哪边|你选哪|哪个好|哪个更好|选哪个|该选|值不值|行不行|对不对"
)
_PRAISE_RE = lazy_re(
    r"(?:你|{BOT})[^。！？!?\n]{0,10}?(?:可爱|厉害|聪明|温柔|会说话|贴心|好看|漂亮|清楚|有趣|懂我|靠谱|棒)"
    r"|你真好|夸你",
    re.IGNORECASE,
)
_PRAISE_NEG_RE = re.compile(r"不(?:太|够|怎么)?(?:可爱|厉害|聪明|温柔|会说话|贴心|好看|漂亮|清楚|有趣|懂我|靠谱|棒)|一点都不|没(?:那么|有)?(?:可爱|聪明)")
_JOKE_RE = re.compile(
    r"哈哈|hhh|233|笑死|绷不住|乐死|doge|[😂🤣😆😏]|你是不是又|开玩笑|逗你|不理你|拉黑你|把你拉黑|"
    r"的我[:：]|退群",
    re.IGNORECASE,
)
_GOOD_NEWS_RE = re.compile(
    r"过了|通过了|成功了|抢到|拿到|考上|录取|上岸|升职|加薪|中奖|中了|搞定了|修好了|救活了|终于|好消息|offer",
    re.IGNORECASE,
)
_DISAGREE_RE = re.compile(
    r"(?:我觉得|我认为|我感觉|我更喜欢|我还是觉得)[^。！？!?\n]{0,20}(?:比|更|好多了|没意思|不如)|"
    r"(?:我打算|我准备|我决定|我想要?)(?:今晚|今天|明天|一会儿?|周末)?(?:通宵|熬夜|去|把|买|辞职|换)"
)
_GREET_RE = re.compile(
    r"^\s*(?:在吗|在不在|在么|在嘛|人呢|哈喽|嗨|hi\b|hello|早安|早上好|中午好|下午好|晚上好|早呀|早啊|我回来了)",
    re.IGNORECASE,
)
_ABOUT_HER_RE = re.compile(r"你(?:最)?(?:喜欢|爱|平时|一般)")
_QUESTION_RE = re.compile(r"[?？]|(?:吗|么)\s*$")


@dataclass(frozen=True)
class SceneJudgement:
    scene: str
    is_group: bool
    register: int  # 场景本身的档位（未取上限）
    cute: int  # 场景本身的卖萌档（未取上限）

    @property
    def label(self) -> str:
        pair = _SCENE_LABEL.get(self.scene, _SCENE_LABEL[SCENE_UNKNOWN])
        return pair[1] if self.is_group else pair[0]


def _scene_of(text: str) -> str:
    if not text:
        return SCENE_UNKNOWN
    if _FEEDBACK_RE.search(text):
        return SCENE_FEEDBACK
    if _IDENTITY_RE.search(text) or (WORK_MENTION.search(text) and "你" in text):
        return SCENE_IDENTITY
    if is_technical(text) or _HELP_EXTRA_RE.search(text):
        return SCENE_HELP
    if _SAD_RE.search(text):
        return SCENE_SAD
    if _LEAVING_RE.search(text):
        return SCENE_LEAVING
    if _OPINION_RE.search(text):
        return SCENE_OPINION
    if FOLLOW_TRIGGERS.search(text):
        return SCENE_FOLLOW
    if _PRAISE_RE.search(text) and not _PRAISE_NEG_RE.search(text):
        return SCENE_PRAISED
    if _JOKE_RE.search(text):
        return SCENE_JOKE
    if _GOOD_NEWS_RE.search(text):
        return SCENE_GOOD_NEWS
    if _DISAGREE_RE.search(text):
        return SCENE_DISAGREE
    if _GREET_RE.search(text):
        return SCENE_GREET
    if _ABOUT_HER_RE.search(text):
        return SCENE_CHAT
    if _QUESTION_RE.search(text):
        return SCENE_QUESTION
    return SCENE_CHAT


def judge_scene(user_text: Optional[str], *, is_group: bool, unified: bool = False) -> SceneJudgement:
    """按本轮对方消息判定场景与场景档位（未叠加任何开关上限）。

    unified=True（style_group_unified）：群聊与私聊用同一列场景档位（场景判断照旧，群上限仍生效）。"""
    text = (user_text or "").strip()
    scene = _scene_of(text)
    return scene_judgement(scene, is_group=is_group, unified=unified)


def scene_judgement(scene: str, *, is_group: bool, unified: bool = False) -> SceneJudgement:
    row = _SCENE_TABLE.get(scene, _SCENE_TABLE[SCENE_UNKNOWN])
    reg, cute = (row[2], row[3]) if is_group and not unified else (row[0], row[1])
    return SceneJudgement(scene=scene if scene in _SCENE_TABLE else SCENE_UNKNOWN,
                          is_group=is_group, register=reg, cute=cute)


def effective_levels(
    judgement: SceneJudgement,
    *,
    register_cap: int,
    group_cap: int,
    cute_cap: int,
    force_light: bool = False,
) -> Tuple[int, int]:
    """叠加开关上限：档位 = min(场景, 总上限, 群聊再取群上限)；卖萌 = min(场景, 卖萌上限)。

    档位落到淡（或被说做作后保持淡）时一律不卖萌；档位为 off 时不注入。
    """
    reg = min(judgement.register, register_cap)
    if judgement.is_group:
        reg = min(reg, group_cap)
    if force_light and reg > REG_LIGHT:
        reg = REG_LIGHT
    if reg <= REG_OFF:
        return REG_OFF, CUTE_OFF
    cute = min(judgement.cute, cute_cap)
    if reg <= REG_LIGHT or force_light:
        cute = CUTE_OFF
    return reg, cute


def is_feedback(user_text: Optional[str]) -> bool:
    """对方是否在说她做作 / 装可爱 / 嗲 / 翻译腔（触发本段对话降到淡档）"""
    return bool(_FEEDBACK_RE.search(user_text or ""))
