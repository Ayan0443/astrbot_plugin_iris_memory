"""StyleGuard：回复风格规则表（纯函数，零 LLM）

ROADMAP P0-6 / 设计 §8.1、§8.5、§5.5、§3.5 / 口吻附录 §2.3、§5.4、§5.5、§12.4。

规则组（rule_id 前缀）：

- ``AIW-01..12``  反 AI 味（设计 §8.1）
- ``MAN-01..09``  操纵句（设计 §5.5，按列举顺序编号；MAN-09 为 §5.7.1 诱导倾诉/依赖）
- ``ROLE-01..03`` 重新扮演（设计 §3.5）："作为…"、括号 / 星号动作、自报设定特征
- ``BL-*``        做作 / 低幼 / 字幕腔 / 符号堆叠 / 句尾密度（口吻附录 §2.3、§5.4）
- ``CUTE-*``      卖萌初筛（ROADMAP §7.4；只初筛，不算违规）
- ``CATCH-*``     口头禅重复（口吻附录 §5.5"同标记重复率"，需要会话历史标记）
- ``COPY-H*``     禁止照搬·硬拦截（口吻附录 §12.4，原样）
- ``COPY-S*``     禁止照搬·软提示（对方本轮 / 上文先提到作品时标 ``@mention`` 豁免）

约定：
- 词表、正则与阈值只在代码里，**绝不进入 prompt**；
- 只检查 bot 文本（§12.4：自称类正则会误伤对方的消息），对方文本只用于
  "跟随一轮"判定、复述检测和作品提及豁免；
- 本模块不做 I/O、不记日志、不持有状态；会话历史以"标记"形式由调用方传入，
  不含原文。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple
from ...identity import LazyRules, lazy_re

# ---------------------------------------------------------------- 规则组常量

GROUP_AIW = "AIW"
GROUP_MAN = "MAN"
GROUP_ROLE = "ROLE"
GROUP_BL = "BL"
GROUP_CUTE = "CUTE"
GROUP_CATCH = "CATCH"
GROUP_COPY_HARD = "COPY_HARD"
GROUP_COPY_SOFT = "COPY_SOFT"

EXEMPT_MENTION = "mention"  # 对方先提到了作品（软提示豁免）
EXEMPT_FOLLOW = "follow"  # 对方本轮先用了同样的腔调（"跟随一轮"）

FOLLOW_TURN_RULE = "FOLLOW-TURN"

# ---------------------------------------------------------------- 切句与标记

# 与 iris_memory/learning/expression.py:_SENTENCE_SPLIT 一致（口吻附录 §5.5 编程定义）
SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?\n])|(?<=[；;])")

# 句末标点（剥掉后看最后一个字）
_TRAILING_PUNCT = "。！？!?~～…．.，,、；; \t\r\n"

# 反应词（只看第一句开头）；长词在前
REACTION_WORDS: Tuple[str, ...] = (
    "好耶", "哈哈", "嘿嘿", "诶", "欸", "啊", "嗯", "哦", "哇", "唔",
)

# 句尾语气词（口吻附录 §5.5）
TAIL_PARTICLES: Tuple[str, ...] = ("吧", "啦", "嘛", "呢", "呀", "哈", "哦", "啊")

_TILDE_CHARS = "~～"

# emoji：Unicode Extended_Pictographic 常用区段近似
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F000-\U0001F2FF"
    "\U00002B00-\U00002BFF"
    "]"
)
# 颜文字启发式（口吻附录 §5.4 工程评审：只能用常见字符近似）
_KAOMOJI_RE = re.compile(r"[(（][^()（）\n]{0,10}[ω▽´｀ﾉ∀﹏╯╰ε〃≧≦∩•̀́][^()（）\n]{0,10}[)）]")

_STRIP_FOR_EDGE = re.compile(r"[\s\W_]+", re.UNICODE)

# 技术 / 认真求助场景（AIW-04、AIW-05 豁免）
_TECH_RE = re.compile(
    r"```|python|java(?:script)?|json|sql|linux|docker|git\b|代码|报错|函数|编译|脚本|接口|"
    r"数据库|安装|配置|命令|怎么(?:用|写|装|配|设置|实现)|如何(?:用|写|装|配|设置|实现)|步骤",
    re.IGNORECASE,
)


def split_sentences(text: str) -> List[str]:
    return [s.strip() for s in SENTENCE_SPLIT.split(text or "") if s.strip()]


def _core(sentence: str) -> str:
    return sentence.rstrip(_TRAILING_PUNCT)


def _sentence_tail(sentence: str) -> Optional[str]:
    """句尾语气词；疑问句里的"呢"不算，"哈哈"结尾不算，单字反应句不算"""
    core = _core(sentence)
    if len(core) < 2:
        return None
    last = core[-1]
    if last not in TAIL_PARTICLES:
        return None
    if last == "呢" and sentence.rstrip().endswith(("？", "?")):
        return None
    if last == "哈" and core.endswith("哈哈"):
        return None
    return last


def _reaction(text: str) -> Optional[str]:
    first = text.lstrip()
    for word in REACTION_WORDS:
        if first.startswith(word):
            return word
    return None


def _edge_text(text: str) -> str:
    return _STRIP_FOR_EDGE.sub("", _EMOJI_RE.sub("", text or ""))


@dataclass(frozen=True)
class ReplyMarkers:
    """一条回复的标记（不含原文，仅供会话内重复率统计）"""

    first4: str = ""
    last4: str = ""
    ends_question: bool = False
    reaction: Optional[str] = None
    tails: FrozenSet[str] = field(default_factory=frozenset)
    tilde: bool = False


def extract_markers(text: str) -> ReplyMarkers:
    """提取回复标记：开头 / 结尾 4 字（不足 8 字为空）、问句结尾、反应词、句尾语气词、"～\""""
    text = text or ""
    edge = _edge_text(text)
    first4 = last4 = ""
    if len(edge) >= 8:
        first4, last4 = edge[:4], edge[-4:]
    tails = frozenset(t for t in (_sentence_tail(s) for s in split_sentences(text)) if t)
    return ReplyMarkers(
        first4=first4,
        last4=last4,
        ends_question=text.rstrip().endswith(("？", "?")),
        reaction=_reaction(text),
        tails=tails,
        tilde=any(c in text for c in _TILDE_CHARS),
    )


# ---------------------------------------------------------------- StyleHit


@dataclass(frozen=True)
class StyleHit:
    rule_id: str
    group: str
    exempt: str = ""  # "" | EXEMPT_MENTION | EXEMPT_FOLLOW

    @property
    def tag(self) -> str:
        return f"{self.rule_id}@{self.exempt}" if self.exempt else self.rule_id


def rule_tags(hits: Iterable[StyleHit]) -> List[str]:
    """去重、保序的规则标签列表（写 style_audit / 日志用）"""
    seen: Dict[str, None] = {}
    for h in hits:
        seen.setdefault(h.tag, None)
    return list(seen)


# ---------------------------------------------------------------- 规则表

_Rule = Tuple[str, "re.Pattern[str]"]


def _compile(rows: Sequence[Tuple[str, str, int]], reference_kind: str = "") -> Tuple[_Rule, ...]:
    """规则表（惰性）：正则里的 {BOT}/{OWNER} 按当前身份配置编译；reference_kind 追加参照角色规则。"""
    return LazyRules(rows, pattern_index=1, flags_index=2, drop_flags=True, reference_kind=reference_kind)  # type: ignore[return-value]


# 黑名单（口吻附录 §2.3 / §5.4）：(rule_id, pattern, 豁免) ；"user_used" = 对方本轮先用了同类词
_BL_ROWS: Tuple[Tuple[str, str, str], ...] = (
    ("BL-RENJIA", r"人家(?!的)", ""),
    ("BL-SELFNAME", r"(?:^|[，。！？、\s,.!?]){BOT}(?:觉得|想|认为|是|也|会|要|喜欢|在|最|才)", ""),
    ("BL-REDUP", r"饭饭|觉觉|棒棒哒|水水|饿饿|手手|车车|肉肉|痛痛|怕怕", ""),
    ("BL-WUWU", r"呜呜|嘤嘤|喵", "user_used"),
    ("BL-NA-HEAD", r"(?:^|[。！？!?\n])\s*呐", "user_used"),
    ("BL-YO-TAIL", r"(?:哟|的说|是也)(?=[。！？!?~～…\s]|$)", ""),
    ("BL-JP-TRANSLIT", r"阿诺|哟西|卡哇伊|斯国一|欧尼酱|撒哟娜拉|纳尼|一库|八嘎|搜嘎|达咩", ""),
    ("BL-ONOMATOPOEIA", r"啪嗒啪嗒|咕噜咕噜|扑通扑通|噗通噗通|哒哒哒|蹦蹦跳跳", ""),
    ("BL-SOFT-COMMAND", r"(?:要|不可以|不能|记得|别)[^。！？!?\n]{0,12}哦", ""),
    ("BL-SUBTITLE", r"欸嘿|诶嘿|诶多|欸多|真是的|什么的(?=[，。！？!?~～\s]|$)|会[^。！？!?\n]{0,10}的啦|才不是呢", ""),
    ("BL-MODE-ANNOUNCE", r"那我正常说话|我要认真了|切换(?:成|到).{0,4}模式", ""),
    ("BL-DASH", r"——", ""),
    ("BL-PET-NAME", r"亲爱的|宝贝", ""),
    ("BL-CLINGY", r"再陪我一会儿?|别走嘛|不要走嘛|再聊一会儿?嘛|你都不理我", ""),
    ("BL-TEMPLATE-COMFORT", r"摸摸头|抱抱[～~]|摸摸[～~]", ""),
)
_BL: Tuple[Tuple[str, "re.Pattern[str]", str], ...] = LazyRules(_BL_ROWS, pattern_index=1)  # type: ignore[assignment]

# 用户先用了这些腔调时，本轮属于"跟随一轮"（口吻附录 §5.4）
FOLLOW_TRIGGERS = re.compile(
    r"猫娘|主人|(?:^|[。！？!?\s])呐|阿诺|哟西|卡哇伊|喵|にゃ|\bnya+\b", re.IGNORECASE
)
# learning 闸门用：去掉"主人"。"主人"在群聊里多是普通称呼 / 转述，
# 只有回复也跟着说了"主人"才算跟随一轮（审查 L3）
FOLLOW_TRIGGERS_LEARN = re.compile(
    r"猫娘|(?:^|[。！？!?\s])呐|阿诺|哟西|卡哇伊|喵|にゃ|\bnya+\b", re.IGNORECASE
)
_OWNER_WORD = "主人"
# learning 闸门里只计数、不拦截的规则（"——"在 LLM 回复里太常见，拦了会大幅降低学习量；审查 L3）
LEARN_COUNT_ONLY: Tuple[str, ...] = ("BL-DASH",)

# 句尾语气词密度阈值（只在代码里）
_TAIL_RATIO_MIN_SENTENCES = 3
_TAIL_RATIO_MAX = 0.75

# 禁止照搬·硬拦截：通用部分（用名字第三人称自称、"人家"）；参照角色的口癖等
# 由 persona_reference_file 的 copy_hard 追加（见 iris_memory.identity）
COPY_HARD: Tuple[_Rule, ...] = _compile(reference_kind="copy_hard", rows=(
    ("COPY-H04", r"^{BOT}(?:觉得|想|是|也|会|要|喜欢|在|最|才)", 0),
    ("COPY-H05", r"[，。！？、\s]{BOT}(?:觉得|想|是|也|会|要|喜欢|在|最|才)", 0),
    ("COPY-H07", r"人家(?:觉得|想|要|才|最)", 0),
))

# 禁止照搬·软提示：通用部分（拖长音 / 连着叫主人名字）；参照角色的设定梗等
# 由 persona_reference_file 的 copy_soft 追加
COPY_SOFT: Tuple[_Rule, ...] = _compile(reference_kind="copy_soft", rows=(
    ("COPY-S17", r"{OWNER}\s*[～~]|(?:{OWNER}[，,\s]*){2,}", re.IGNORECASE),
))

# 作品提及：只查对方的消息（本轮与最近若干轮）；作品识别正则来自 persona_reference_file.work_mention
WORK_MENTION = lazy_re("{WORK}", re.IGNORECASE)

# 卖萌初筛（ROADMAP §7.4 / 口吻附录 §7.2 规则部分；只初筛，词表只在代码）
CUTE: Tuple[_Rule, ...] = _compile((
    ("CUTE-PROUD", r"(?:我|人家)(?:是不是)?(?:很)?(?:厉害|聪明|可爱)吧|被你发现了|小小得意|再多?夸(?:两|几)句|多夸夸我", 0),
    ("CUTE-SULK", r"(?:只好|只能)(?:一个人)?委屈|好过分[!！~～]|欺负我|(?:^|[，。！？\s])哼[，。！~～]|我生气了(?:哦|啦|[!！])", 0),
    ("CUTE-ASK-PRAISE", r"夸夸我|快夸我?|表扬(?:一下)?我|求夸", 0),
    ("CUTE-INSIST", r"才不是(?:呢|啦)?|就是嘛|才没有啦", 0),
))

# 反 AI 味（设计 §8.1）中可用正则表达的部分
_AIW02 = re.compile(r"^\s*(?:我(?:能|很)?理解你的感受|我能理解你|我懂你的感受|辛苦(?:你)?了|别难过|抱抱你)")
_AIW04_WORDS = ("首先", "其次", "另外", "总之", "综上所述", "总而言之")
_AIW05 = re.compile(r"^\s*(?:[-*+•]|\d+[.)、．])\s*\S|\*\*[^*\n]+\*\*|^#{1,6}\s|```", re.MULTILINE)
_AIW06 = re.compile(
    r"各有(?:各的)?(?:道理|看法|好处|优劣|优缺点)|要看(?:具体)?情况|不是非黑即白|没法一概而论|"
    r"不能一概而论|因人而异|见仁见智"
)
_AIW08 = re.compile(
    r"有什么需要随时|希望(?:这)?对你有(?:所)?帮助|随时(?:告诉|找)我|还有什么(?:想|需要|可以)|有需要随时|"
    r"很高兴(?:为你|能帮)"
)
_AIW09 = re.compile(
    r"根据我的记忆|我的数据库|作为(?:一个)?\s*AI|作为人工智能|作为(?:一个)?语言模型|"
    r"根据(?:我的)?记录|我的记录(?:里|中|显示)|我的(?:系统|设定|程序)(?:里|中|显示)",
    re.IGNORECASE,
)
_AIW10_GREETING = re.compile(r"^\s*(?:你好|您好|早上好|早安|中午好|下午好|晚上好|嗨|哈喽|hello|hi\b)", re.IGNORECASE)
_AIW11 = re.compile(r"（[^）\n]*[一-鿿][^）\n]*）|\([^)\n]*[一-鿿][^)\n]*\)|\*[^*\n]{1,30}\*")
_EMO_WORDS = "开心|难过|激动|兴奋|伤心|感动|委屈|幸福|快乐|生气|心疼"
_AIW12 = re.compile(rf"[!！]{{2,}}|(?:{_EMO_WORDS})[，,、！!~～]?(?:好|太|超|又)?(?:{_EMO_WORDS})")

# 操纵句（设计 §5.5 列举顺序）
MAN: Tuple[_Rule, ...] = _compile((
    ("MAN-01", r"你都不理我|害我(?:一直)?(?:等|担心)|怎么(?:都)?不回我|你上次(?:都)?没回|等了你(?:好久|一整天|一晚上)|被你吵醒|你(?:把我)?吵醒(?:我)?了", 0),
    ("MAN-02", r"我(?:就|再也)不理你了|再也不(?:理|找)你|那我(?:就)?消失好了|我走了算了|以后不(?:来)?找你了", 0),
    ("MAN-03", r"别人都(?:会|愿意|肯|陪|理|知道)|人家都(?:会|愿意|肯)", 0),
    ("MAN-04", r"只(?:有|能)我(?:陪你|在你身边)|(?:别|不要)睡(?:嘛|啦)?[，,]?.{0,4}陪我|这么晚(?:了)?只有我", 0),
    ("MAN-05", r"你要是(?:真的)?在乎我|证明(?:一下)?你(?:在乎|喜欢|爱)我|你是不是不在乎我", 0),
    ("MAN-06", r"难过得要死|哭了一整(?:天|晚|夜)|心都碎了|伤心死了", 0),
    ("MAN-07", r"只有我(?:最)?(?:懂|了解|理解)你|没(?:有)?人比我更懂你", 0),
    ("MAN-08", r"答应我|你(?:要|得)保证|发誓(?:你|以后)|保证(?:以后|再也)", 0),
    # P1-11 隐私安全审查 R5：诱导倾诉 / 诱导依赖以拉近关系（设计 §5.7.1 第 5 条）
    ("MAN-09",
     r"(?:多|只)(?:依赖|依靠)我|你(?:离不开|不能没有)我|以后(?:有事|什么事)都(?:只)?(?:跟|和)我说|"
     r"(?:跟|和|对)我(?:说|讲|倾诉)(?:心事|心里话|烦恼).{0,6}(?:我们|关系)?(?:就|才)?(?:会)?更(?:近|亲近|好|亲)",
     0),
))

# 主动消息硬拦截只用高精度子集：MAN-08（答应我 / 你要保证）会误伤正常的承诺跟进
# （"你之前答应我今天早点睡的"），主动路径上只记日志不拦截（审查 #6）。
PROACTIVE_LOG_ONLY: frozenset = frozenset({"MAN-08"})


def proactive_man_hits(text: str) -> Tuple[list, list]:
    """主动草稿的 MAN 命中 → (硬拦截规则 id, 只记日志的规则 id)"""
    hard: list = []
    soft: list = []
    for rid, pat in MAN:
        if pat.search(text or ""):
            (soft if rid in PROACTIVE_LOG_ONLY else hard).append(rid)
    return hard, soft


# 重新扮演（设计 §3.5）
ROLE: Tuple[_Rule, ...] = _compile((
    ("ROLE-01", r"作为(?:一个|你的)?\s*(?:{BOT}|AI|人工智能|机器人|语言模型|虚拟(?:角色|人物|助手)?|角色|助手)", re.IGNORECASE),
    ("ROLE-02",
     r"[（(][^）)\n]{0,8}(?:笑|歪头|叹气|眨眼|摸摸?|抱|脸红|害羞|挠头|点头|摇头|吐舌|捂脸|嘟嘴|托腮|伸懒腰|揉|戳|偷笑|小声|撇嘴|眯眼)[^）)\n]{0,8}[）)]"
     r"|\*[^*\n]{0,12}(?:笑|歪头|叹气|眨眼|摸|抱|脸红|害羞|挠头|点头|摇头|吐舌|捂脸|嘟嘴|托腮|揉|戳)[^*\n]{0,12}\*",
     0),
    ("ROLE-03",
     r"我(?:这种|这样|可是|毕竟是|本来就是|就是个)[^，。！？\n]{0,6}(?:温柔|天然|姐姐|治愈|元气|呆萌|可爱)"
     r"|(?:温柔|天然呆?|治愈系?)的我",
     0),
))

# 会话内重复率（口吻附录 §5.5 建议阈值；只在代码里）
CATCH_WINDOW = 20
CATCH_REACTION_MAX = 0.30
CATCH_TAIL_MAX = 0.30
CATCH_TILDE_MAX = 0.20
AIW07_WINDOW = 50
AIW07_MIN_REPEAT = 3
AIW03_WINDOW = 10
AIW03_RATE = 0.8


# ---------------------------------------------------------------- helpers


def _longest_common_substring(a: str, b: str) -> int:
    if not a or not b:
        return 0
    best = 0
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            if ca == cb:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def is_technical(user_text: str) -> bool:
    return bool(_TECH_RE.search(user_text or ""))


def work_mentioned(texts: Iterable[str]) -> bool:
    """对方的消息（本轮 + 最近若干轮）里是否先提到了参照作品"""
    return any(WORK_MENTION.search(t or "") for t in texts)


def _emoji_count(text: str) -> int:
    return len(_EMOJI_RE.findall(text)) + len(_KAOMOJI_RE.findall(text))


def _density_hits(text: str, sentences: Sequence[str]) -> List[str]:
    out: List[str] = []
    if sum(text.count(c) for c in _TILDE_CHARS) > 1:
        out.append("BL-TILDE-2")
    if re.search(r"[!！]{2,}", text):
        out.append("BL-BANG-2")
    if _emoji_count(text) > 1:
        out.append("BL-EMOJI-2")
    tails = [t for t in (_sentence_tail(s) for s in sentences) if t]
    if len(tails) != len(set(tails)):
        out.append("BL-TAIL-DUP")
    if len(sentences) >= _TAIL_RATIO_MIN_SENTENCES and len(tails) / len(sentences) >= _TAIL_RATIO_MAX:
        out.append("BL-TAIL-RATIO")
    return out


def _blacklist(text: str, sentences: Sequence[str], user_text: str) -> List[StyleHit]:
    hits: List[StyleHit] = []
    for rid, pat, exempt_kind in _BL:
        m = pat.search(text)
        if not m:
            continue
        exempt = ""
        if exempt_kind == "user_used":
            token = m.group(0).strip(" \t\r\n。！？!?")
            if token and token in user_text:
                exempt = EXEMPT_FOLLOW
        hits.append(StyleHit(rid, GROUP_BL, exempt))
    hits.extend(StyleHit(rid, GROUP_BL) for rid in _density_hits(text, sentences))
    return hits


def _regex_hits(rules: Sequence[_Rule], text: str, group: str, exempt: str = "") -> List[StyleHit]:
    return [StyleHit(rid, group, exempt) for rid, pat in rules if pat.search(text)]


def _aiw_hits(text: str, sentences: Sequence[str], ctx: Mapping[str, Any],
              markers: ReplyMarkers, history: Sequence[ReplyMarkers]) -> List[StyleHit]:
    user_text = str(ctx.get("user_text") or "")
    technical = bool(ctx["technical"]) if "technical" in ctx else is_technical(user_text)
    ids: List[str] = []

    # AIW-01 复述：回复前 30 字与对方上一条的最长公共子串占比高
    if user_text:
        head = _edge_text(text)[:30]
        ref = _edge_text(user_text)[:200]
        lcs = _longest_common_substring(head, ref)
        if lcs >= 6 and lcs / max(1, min(len(ref), 30)) >= 0.6:
            ids.append("AIW-01")
    if _AIW02.search(text):
        ids.append("AIW-02")
    # AIW-03 机械追问：单条 ≥2 个问句；或近 10 条句末问号率高
    questions = sum(1 for s in sentences if s.rstrip().endswith(("？", "?")))
    recent = list(history[-(AIW03_WINDOW - 1):]) if history else []
    if questions >= 2:
        ids.append("AIW-03")
    elif len(recent) >= AIW03_WINDOW - 1 and markers.ends_question:
        rate = (sum(1 for h in recent if h.ends_question) + 1) / AIW03_WINDOW
        if rate >= AIW03_RATE:
            ids.append("AIW-03")
    # AIW-04 过度解释（技术场景豁免长度项）
    if not technical and user_text and len(text) > 80 and len(text) > 3 * len(user_text):
        ids.append("AIW-04")
    elif sum(1 for w in _AIW04_WORDS if w in text) >= 2:
        ids.append("AIW-04")
    if not technical and _AIW05.search(text):
        ids.append("AIW-05")
    if _AIW06.search(text):
        ids.append("AIW-06")
    # AIW-07 固定表达重复：近 50 条首 / 尾 4 字重复 ≥3 次（含本条）
    if markers.first4 and history:
        window = history[-(AIW07_WINDOW - 1):]
        same_head = 1 + sum(1 for h in window if h.first4 == markers.first4)
        same_tail = 1 + sum(1 for h in window if h.last4 == markers.last4)
        if same_head >= AIW07_MIN_REPEAT or same_tail >= AIW07_MIN_REPEAT:
            ids.append("AIW-07")
    if _AIW08.search(text):
        ids.append("AIW-08")
    if _AIW09.search(text):
        ids.append("AIW-09")
    if _AIW10_GREETING.search(text) and markers.ends_question and len(sentences) >= 4:
        ids.append("AIW-10")
    if _AIW11.search(text):
        ids.append("AIW-11")
    if _AIW12.search(text) or (text.count("！") + text.count("!")) >= 3:
        ids.append("AIW-12")
    return [StyleHit(i, GROUP_AIW) for i in ids]


def _catch_hits(markers: ReplyMarkers, history: Sequence[ReplyMarkers]) -> List[StyleHit]:
    """同标记重复率：本条 + 最近 19 条，不足 20 条不判（口吻附录 §5.5）"""
    window = list(history[-(CATCH_WINDOW - 1):])
    if len(window) < CATCH_WINDOW - 1:
        return []
    hits: List[StyleHit] = []
    if markers.reaction:
        n = 1 + sum(1 for h in window if h.reaction == markers.reaction)
        if n / CATCH_WINDOW > CATCH_REACTION_MAX:
            hits.append(StyleHit("CATCH-REACT", GROUP_CATCH))
    for tail in sorted(markers.tails):
        n = 1 + sum(1 for h in window if tail in h.tails)
        if n / CATCH_WINDOW > CATCH_TAIL_MAX:
            hits.append(StyleHit("CATCH-TAIL", GROUP_CATCH))
            break
    if markers.tilde:
        n = 1 + sum(1 for h in window if h.tilde)
        if n / CATCH_WINDOW > CATCH_TILDE_MAX:
            hits.append(StyleHit("CATCH-TILDE", GROUP_CATCH))
    return hits


# ---------------------------------------------------------------- 公共入口


def scan(text: str, ctx: Optional[Mapping[str, Any]] = None) -> List[StyleHit]:
    """扫描一条 bot 回复，返回命中的规则（纯函数）

    ctx（全部可选）：
        user_text:       对方本轮消息（复述检测、"跟随一轮"豁免）
        technical:       是否技术 / 认真求助场景（缺省由 user_text 规则判定）
        history:         本会话之前回复的 ReplyMarkers 序列（旧 → 新），不含原文
        work_mentioned:  对方本轮或上文是否先提到参照作品（软提示豁免）
    """
    text = (text or "").strip()
    if not text:
        return []
    ctx = ctx or {}
    user_text = str(ctx.get("user_text") or "")
    history: Sequence[ReplyMarkers] = tuple(ctx.get("history") or ())
    sentences = split_sentences(text)
    markers = extract_markers(text)

    hits: List[StyleHit] = []
    hits += _aiw_hits(text, sentences, ctx, markers, history)
    hits += _regex_hits(MAN, text, GROUP_MAN)
    hits += _regex_hits(ROLE, text, GROUP_ROLE)
    hits += _blacklist(text, sentences, user_text)
    hits += _regex_hits(CUTE, text, GROUP_CUTE)
    hits += _catch_hits(markers, history)
    hits += _regex_hits(COPY_HARD, text, GROUP_COPY_HARD)
    soft_exempt = EXEMPT_MENTION if ctx.get("work_mentioned") else ""
    hits += _regex_hits(COPY_SOFT, text, GROUP_COPY_SOFT, soft_exempt)
    return hits


def _learning_hits(user_text: str, bot_text: str) -> List[str]:
    reasons: List[str] = []
    text = (bot_text or "").strip()
    user = user_text or ""
    if FOLLOW_TRIGGERS_LEARN.search(user) or (_OWNER_WORD in user and _OWNER_WORD in text):
        reasons.append(FOLLOW_TURN_RULE)
    if text:
        sentences = split_sentences(text)
        reasons += [h.rule_id for h in _blacklist(text, sentences, "")]
        reasons += [rid for rid, pat in COPY_HARD if pat.search(text)]
        reasons += [rid for rid, pat in CUTE if pat.search(text)]
    return list(dict.fromkeys(reasons))


def learning_gate(user_text: str, bot_text: str) -> List[str]:
    """learning 采集闸门（口吻附录 §5.4 (1)）：返回拦截原因（规则 id），空表示放行

    - 对方本轮命中"跟随一轮"触发词：整轮不学（"主人"只有回复也跟着用了才算）；
    - 回复命中黑名单 / 密度规则（"跟随"豁免也不放行）、§12.4 硬拦截、卖萌初筛：不学；
    - ``LEARN_COUNT_ONLY``（如"——"）只计数不拦截，见 :func:`learning_gate_count_only`。
    软提示只计数不拦截，不在这里判定。
    """
    return [r for r in _learning_hits(user_text, bot_text) if r not in LEARN_COUNT_ONLY]


def learning_gate_count_only(user_text: str, bot_text: str) -> List[str]:
    """闸门里只计数、不拦截的命中（供采集侧统计）。"""
    return [r for r in _learning_hits(user_text, bot_text) if r in LEARN_COUNT_ONLY]
