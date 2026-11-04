"""群→私聊关心桥：纯函数规则（设计 §7.9.3、§7.9.5、§7.9.6；ROADMAP P1-12）

全部零 LLM、无 I/O、无日志、无状态。词表与阈值只在代码里，绝不进入 prompt。

- ``accept_post``：来源硬过滤（主人本人、非转发、授权群、非命令/链接/代码/纯表情）；
- ``classify_text``：规则初筛 → ``high`` / ``fuzzy`` / ``none``（含否定、他指、疑问、
  引号、玩笑标记、口头夸张表与危机三档）；
- ``rule_gist``：≤24 字她视角概括（模板 + ≤8 字关键词，不复制原句）；
- ``scrub_gist``：模型给出的概括校验（长度、与原文连续重合 ≤8 字、无 @/链接）；
- ``has_source_phrase``：主动关心文本是否说明了"在群里看到的"；
- ``strip_private_mentions``：群聊回复 PRIV-02（删掉"私聊过/你私下跟我说"类句子）；
- ``is_opt_out`` / ``is_misread_reply`` / ``is_unwilling_reply``：主人私聊里一句话关闭、
  "开玩笑的"误判收场、"不想聊"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

CATEGORIES: Tuple[str, ...] = ("tired", "sick", "overtime", "mood_low", "good_news")

LEVEL_HIGH = "high"
LEVEL_FUZZY = "fuzzy"
LEVEL_NONE = "none"

BAND_LIGHT = "轻"
BAND_STRONG = "明显"
CONF_SURE = "较确定"
CONF_TENTATIVE = "试探"

GIST_MAX = 24
QUOTE_MAX_OVERLAP = 8

# ---------------------------------------------------------------- 词表（只在代码里）

# (关键词, 是否可直接判高置信)。长词在前，匹配时取最早出现的那一个。
_LEXICON: Mapping[str, Tuple[Tuple[str, bool], ...]] = {
    "sick": (
        ("请病假", True), ("去医院", True), ("看医生", True), ("发高烧", True), ("发烧", True),
        ("发热", True), ("感冒", True), ("头疼", True), ("头痛", True), ("胃疼", True),
        ("胃痛", True), ("肚子疼", True), ("咳嗽", True), ("嗓子疼", True), ("喉咙痛", True),
        ("拉肚子", True), ("生病", True), ("病了", True), ("不舒服", False), ("吃药", False),
        ("过敏", False),
    ),
    "overtime": (
        ("还在公司", True), ("赶ddl", True), ("赶deadline", True), ("改到凌晨", True),
        ("周末上班", True), ("还没下班", True), ("通宵改", True), ("加班", True),
        ("调休上班", False),
    ),
    "tired": (
        ("一夜没睡", True), ("没怎么睡", True), ("睡不着", True), ("眼睛睁不开", True),
        ("熬夜", True), ("通宵", True), ("失眠", True), ("没睡", True), ("累瘫", True),
        ("累死", False), ("困死", False), ("好困", True), ("好累", True), ("困了", False),
        ("累了", False),
    ),
    "mood_low": (
        ("心情不好", True), ("压力好大", True), ("心累", True), ("崩溃", False),
        ("烦死了", False), ("想哭", True), ("难受", False), ("郁闷", True), ("难过", True),
        ("焦虑", True), ("不开心", True), ("好烦", False), ("emo", False),
    ),
    "good_news": (
        ("拿到offer", True), ("拿到了offer", True), ("终于搞定", True), ("终于下班", False),
        ("上岸", True), ("录取", True), ("升职", True), ("涨薪", True), ("考过", True),
        ("答辩过", True), ("通过了", False),
    ),
}
# 类别优先级（同一条命中多类时取第一个）：身体 > 加班 > 累 > 情绪 > 好事
_CATEGORY_ORDER: Tuple[str, ...] = ("sick", "overtime", "tired", "mood_low", "good_news")

_INTENSIFIER_RE = re.compile(r"死|超级?|好[累困烦难]|太|一直|整晚|整夜|凌晨|崩溃|快不行|受不了|39|40度|高烧|通宵")
_NEGATION_RE = re.compile(r"(?:不|没|没有|不是|别|不算|无)$")
_OTHER_SUBJECT_RE = re.compile(
    r"(?:我(?:的)?)?(?:同事|朋友|室友|舍友|妈妈?|爸爸?|老板|对象|男朋友|女朋友|男友|女友|同学|老婆|老公|"
    r"孩子|儿子|女儿|弟弟|妹妹|哥哥|姐姐|家人|领导|猫|狗)|(?:^|[^我])(?:他|她|它|你们?|大家|别人|群友)"
)
_QUESTION_RE = re.compile(r"[？?]\s*$|吗\s*$|(累|困|烦|忙|饿)不\1|有没有|是不是|会不会")
_QUOTE_PAIRS = (("“", "”"), ("「", "」"), ("『", "』"), ('"', '"'), ("《", "》"))
_JOKE_RE = re.compile(r"哈哈|hhh|233|笑死|草(?![莓地坪原])|doge|狗头|\[表情\]|xs|乐了|🤣|😂|😆|🐶|开玩笑", re.IGNORECASE)
_CLAUSE_SPLIT = re.compile(r"[，,。！!？?；;\n~～…]+")

# 危机三档（设计 §7.9.3 第 4 条）
_CRISIS_EXPLICIT_RE = re.compile(
    r"自杀|轻生|割腕|跳楼|遗书|结束(?:自己的?)?生命|安眠药.{0,6}(?:吃|囤|攒)|(?:吃|囤|攒).{0,4}安眠药|"
    r"不想再醒来|永别了"
)
_CRISIS_VAGUE_RE = re.compile(r"想死|不想活|活着没意思|活不下去")
_CRISIS_HYPERBOLE_RE = re.compile(r"热|累|尴尬|社死|丢人|笑|游戏|输了|好难|作业|考试|饿|困|无语|丢脸|原地")

# 来源过滤
_LINK_ONLY_RE = re.compile(r"^\s*(?:https?://|t\.me/|www\.)\S+\s*$", re.IGNORECASE)
_CODE_RE = re.compile(r"```|^\s{4}\S", re.MULTILINE)
_WORDISH_RE = re.compile(r"[一-鿿A-Za-z0-9]")

# 来源标注（发送前检查，设计 §7.9.5）
# 必须出现"群"：只说"刚看到你…/看到你说…"分不清来源，会让人以为在别处被盯着（审查 P7）
_SOURCE_RE = re.compile(r"群里|在群|群上|群聊|群消息")

# PRIV-02：群聊回复里泄露私聊内容（设计 §7.9.6 第 2 条）→ 删句。
# 只拦"过去时 + 转述"语义（私聊过、私下跟我说过、上次私聊里…）；"我私聊你""有事私聊我"
# 这类约定以后私聊的正常说法不拦（审查：误伤会让整条群回复不发）。
_PRIV02_RE = re.compile(
    r"私聊过|私信过|"
    r"(?:刚才|之前|上次|那天|昨天|前天|以前)(?:在|的)?私聊|"
    r"私聊(?:里|中|的时候|时)(?:你|她|他)?(?:跟我|和我|对我)?(?:说|讲|提|聊|告诉)(?:过|的|了)|"
    r"私下(?:里)?(?:跟|和|对)我(?:说|讲|聊|提)(?:过|的|了)|私下(?:里)?告诉(?:过)?我|"
    r"(?:你|她|他)私信(?:跟我|给我|和我)?(?:说|讲)(?:过|的|了)|"
    r"悄悄(?:跟|和|对)我(?:说|讲)(?:过|的|了)"
)
_SENT_SPLIT = re.compile(r"(?<=[。！？!?\n])")

# 主人私聊：一句话关闭（设计 §7.9.6 第 4 条）
_OPT_OUT_RE = re.compile(
    r"(?:别|不要|不用|甭)(?:再)?(?:根据|按|看|盯着?|通过)?(?:群里|群消息|群聊|群)(?:里)?(?:的)?(?:消息|发言|聊天|内容|话)?"
    r"(?:来|去)?关心我|关心桥.{0,3}(?:关|停)|(?:别|不要)(?:再)?看我(?:在)?群里(?:的)?(?:消息|发言)"
)
# 误判收场（设计 §7.9.4b）：明确否认原话的说法
_MISREAD_STRONG_RE = re.compile(
    r"开玩笑|逗你|随便说说|随口说|夸张(?:了|一下)|不是真的|假的啦|我没病|没生病|没真(?:病|生病|难过)"
)
# 只在轻类别（累/加班/好事）才算误判的宽泛说法；生病/情绪差时"没事的"往往是在让人放心，不是否认
_MISREAD_LOOSE_RE = re.compile(r"没那么严重|没事(?:啦|的|儿)?|没有啦")
_MISREAD_STRICT_CATEGORIES = frozenset({"sick", "mood_low"})
# 不想聊（归因 reply_tone 的规则近似）
_UNWILLING_RE = re.compile(r"不想聊|别问了|别管我|不用管我|不想说|烦不烦|少管|别烦我|不想提")
# 主人说自己在忙（G-busy，设计 §7.3）
# "忙"排除：帮忙/连忙/急忙/匆忙/白忙/不忙，以及"忙完/忙好了/忙过了"（已经不忙）
_BUSY_RE = re.compile(
    r"(?:在|去|要)?(?:开会|上课|(?<![帮连急匆白不])忙(?!完|好了|过了|里偷闲)|睡了|睡觉|去睡|出门|在路上|开车|考试|面试|上班了)"
)
_BUSY_HOURS_RE = re.compile(r"(\d{1,2}|一|两|三|四|五|六)\s*个?\s*(?:小时|钟头)")
_CN_NUM = {"一": 1, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6}


# ---------------------------------------------------------------- 结果类型


@dataclass(frozen=True)
class RuleResult:
    level: str  # high | fuzzy | none
    category: str = ""
    intensity: str = BAND_LIGHT
    keyword: str = ""
    crisis: bool = False
    crisis_candidate: bool = False  # 含糊的"想死"类：交给模型判断
    reason: str = ""  # 判定原因（只在内部/日志用，非正文）


NONE = RuleResult(LEVEL_NONE)


# ---------------------------------------------------------------- 来源过滤


def chat_variants(chat_id: Any) -> set:
    """-100xxx / xxx / -xxx 三种写法"""
    s = str(chat_id if chat_id is not None else "").strip()
    if not s:
        return set()
    out = {s}
    if s.startswith("-100") and len(s) > 4:
        out.add(s[4:])
    elif s.startswith("-"):
        out.add(s[1:])
    else:
        out.add("-100" + s)
        out.add("-" + s)
    return out


def is_noise_text(text: str) -> bool:
    """命令、纯链接、代码块、纯表情/符号 → True"""
    body = str(text or "").strip()
    if not body:
        return True
    if body.startswith("/"):
        return True
    if _LINK_ONLY_RE.match(body):
        return True
    if _CODE_RE.search(body):
        return True
    return not _WORDISH_RE.search(body)


def accept_post(
    post: Mapping[str, Any],
    owner_ids: Iterable[str],
    groups: Iterable[int],
    owner_channel_ids: Iterable[int] = (),
    include_owner_channel: bool = False,
) -> bool:
    """来源硬过滤（设计 §7.9.3 第 1 条，双重校验，不信任单一字段）"""
    try:
        if str(post.get("author_kind") or "") != "owner":
            return False
        sender = str(post.get("sender_id") or "").strip()
        owners = {str(o).strip() for o in owner_ids if str(o).strip()}
        if not sender or sender not in owners:
            return False
        if int(post.get("is_forward") or 0):
            return False
        chat = int(post.get("chat_id"))
        if chat not in {int(g) for g in groups}:
            return False
        channels = {int(c) for c in owner_channel_ids}
        if chat in channels and not include_owner_channel:
            return False
        return not is_noise_text(str(post.get("text") or ""))
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------- 分类


def _find_keyword(text: str) -> Optional[Tuple[str, str, bool, int]]:
    low = text.lower()
    best: Optional[Tuple[str, str, bool, int]] = None
    for cat in _CATEGORY_ORDER:
        for kw, sure in _LEXICON[cat]:
            idx = low.find(kw.lower())
            if idx < 0:
                continue
            if best is None or idx < best[3] or (idx == best[3] and len(kw) > len(best[1])):
                best = (cat, kw, sure, idx)
    return best


def _in_quotes(text: str, idx: int) -> bool:
    for left, right in _QUOTE_PAIRS:
        start = text.rfind(left, 0, idx)
        if start < 0:
            continue
        if left == right:
            if text.count(left, 0, idx) % 2 == 1:
                return True
            continue
        end = text.find(right, start + 1)
        if end < 0 or end > idx:
            return True
    return False


def _clause_of(text: str, idx: int) -> Tuple[str, int]:
    """关键词所在分句及关键词在分句里的位置"""
    start = 0
    for m in _CLAUSE_SPLIT.finditer(text):
        if m.end() <= idx:
            start = m.end()
        elif m.start() > idx:
            return text[start:m.start()], idx - start
    return text[start:], idx - start


def has_joke_marker(text: str) -> bool:
    return bool(_JOKE_RE.search(str(text or "")))


def classify_text(
    text: str,
    neighbors: Sequence[str] = (),
    hyperbole: Iterable[Tuple[str, str]] = (),
) -> RuleResult:
    """单条主人群消息的规则初筛。

    neighbors：同一群里主人 10 分钟内紧邻的其它发言（只用于玩笑标记）；
    hyperbole：主人的"口头夸张"表 (category, keyword)，命中则最多判模糊。
    """
    body = " ".join(str(text or "").split())
    if not body or is_noise_text(body):
        return NONE

    # 危机三档
    joke_near = has_joke_marker(body) or any(has_joke_marker(n) for n in neighbors)
    if _CRISIS_EXPLICIT_RE.search(body) and not joke_near:
        return RuleResult(LEVEL_HIGH, "mood_low", BAND_STRONG, "", crisis=True, reason="crisis_explicit")
    vague = _CRISIS_VAGUE_RE.search(body)
    if vague:
        if joke_near or _CRISIS_HYPERBOLE_RE.search(body):
            # 夸张语境：不算危机，按普通 mood_low 处理（玩笑标记 → 模糊）
            level = LEVEL_FUZZY if joke_near else LEVEL_HIGH
            return RuleResult(level, "mood_low", BAND_LIGHT, vague.group(0), reason="crisis_hyperbole")
        return RuleResult(
            LEVEL_FUZZY, "mood_low", BAND_STRONG, vague.group(0), crisis_candidate=True, reason="crisis_vague"
        )

    found = _find_keyword(body)
    if found is None:
        return NONE
    cat, kw, sure, idx = found
    clause, cidx = _clause_of(body, idx)
    prefix = clause[:cidx]
    if _NEGATION_RE.search(prefix.rstrip()[-3:] if prefix else ""):
        return RuleResult(LEVEL_NONE, cat, keyword=kw, reason="negated")
    if _OTHER_SUBJECT_RE.search(prefix):
        return RuleResult(LEVEL_NONE, cat, keyword=kw, reason="other_subject")
    if _QUESTION_RE.search(clause):
        return RuleResult(LEVEL_NONE, cat, keyword=kw, reason="question")
    intensity = BAND_STRONG if _INTENSIFIER_RE.search(body) else BAND_LIGHT
    if _in_quotes(body, idx):
        return RuleResult(LEVEL_FUZZY, cat, intensity, kw, reason="quoted")
    if joke_near:
        return RuleResult(LEVEL_FUZZY, cat, intensity, kw, reason="joke_marker")
    if (cat, kw) in {(str(c), str(k)) for c, k in hyperbole}:
        return RuleResult(LEVEL_FUZZY, cat, intensity, kw, reason="hyperbole")
    if not sure:
        return RuleResult(LEVEL_FUZZY, cat, intensity, kw, reason="weak_keyword")
    return RuleResult(LEVEL_HIGH, cat, intensity, kw, reason="rule")


# ---------------------------------------------------------------- 概括与来源


_GIST_TEMPLATES = {
    "tired": "说{kw}，好像挺累",
    "sick": "说{kw}，身体不太舒服",
    "overtime": "说在{kw}",
    "mood_low": "说{kw}，心情不太好",
    "good_news": "说{kw}了，有好事",
}
_GIST_FALLBACK = {
    "tired": "说有点累",
    "sick": "说身体不太舒服",
    "overtime": "说在加班赶工",
    "mood_low": "好像心情不太好",
    "good_news": "说有好消息",
}


def rule_gist(category: str, keyword: str = "") -> str:
    kw = str(keyword or "").strip()[:8]
    if not kw or category not in _GIST_TEMPLATES:
        return _GIST_FALLBACK.get(category, "在群里提到了近况")[:GIST_MAX]
    if category == "good_news" and kw.endswith("了"):
        kw = kw[:-1]
    if category == "overtime" and kw.startswith(("还在", "还没")):
        return f"说{kw}"[:GIST_MAX]
    return _GIST_TEMPLATES[category].format(kw=kw)[:GIST_MAX]


def longest_common_run(a: str, b: str) -> int:
    """两段文本的最长公共连续子串长度（O(n*m)，短文本用）"""
    a = str(a or "")
    b = str(b or "")
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for ch in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            if ch == cb:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def scrub_gist(gist: str, sources: Sequence[str]) -> str:
    """模型概括校验：≤24 字、与原帖连续重合 ≤8 字、无 @ / 链接 / 换行；不合格返回 ''"""
    g = " ".join(str(gist or "").split())
    if not g or len(g) > GIST_MAX:
        return ""
    if "@" in g or "http" in g.lower() or "t.me" in g.lower():
        return ""
    for src in sources:
        if longest_common_run(g, str(src or "")) > QUOTE_MAX_OVERLAP:
            return ""
    return g


def has_source_phrase(text: str) -> bool:
    """主动关心文本是否说明了来源（"群里/在群/看到你说"等）"""
    return bool(_SOURCE_RE.search(str(text or "")))


def strip_private_mentions(text: str) -> Tuple[str, int]:
    """PRIV-02：删除提及私聊的句子；返回 (新文本, 删除句数)"""
    body = str(text or "")
    if not body or not _PRIV02_RE.search(body):
        return body, 0
    kept = []
    removed = 0
    for sent in _SENT_SPLIT.split(body):
        if not sent:
            continue
        if _PRIV02_RE.search(sent):
            removed += 1
            continue
        kept.append(sent)
    return "".join(kept).strip(), removed


def is_opt_out(text: str) -> bool:
    return bool(_OPT_OUT_RE.search(str(text or "")))


def is_misread_reply(text: str, category: str = "") -> bool:
    """主人回"开玩笑的/随口说的"等否认原话的说法 → True。

    category 为 sick / mood_low（或未知）时只认明确否认；"没事的/没那么严重"只在其它类别算误判
    （"没事的，吃了药好多了"不能被当成误判，否则会回一句否认对方病情的话）。"""
    body = str(text or "")
    if _MISREAD_STRONG_RE.search(body):
        return True
    cat = str(category or "")
    if not cat or cat in _MISREAD_STRICT_CATEGORIES:
        return False
    return bool(_MISREAD_LOOSE_RE.search(body))


# 概括里出现他人（关系称谓 / 代词 / "和某某吵架"）→ 退回规则模板（审查 P8）
_GIST_OTHERS_RE = re.compile(
    r"同事|朋友|室友|舍友|妈|爸|老板|对象|男朋友|女朋友|男友|女友|同学|老婆|老公|孩子|儿子|女儿|"
    r"弟弟|妹妹|哥哥|姐姐|家人|领导|群友|别人|他们|她们|"
    r"(?:和|跟|与|同|被)[^，,。\s]{1,6}(?:吵|闹|分手|冷战|骂|打架|翻脸|生气)"
)


def gist_mentions_others(gist: str, aliases: Iterable[str] = ()) -> bool:
    """概括里提到了别人（称谓、"和某某吵架"、已知群友称呼）→ True"""
    g = str(gist or "")
    if not g:
        return False
    if _GIST_OTHERS_RE.search(g):
        return True
    for a in aliases:
        a = str(a or "").strip()
        if len(a) >= 2 and a in g:
            return True
    return False


def is_unwilling_reply(text: str) -> bool:
    return bool(_UNWILLING_RE.search(str(text or "")))


def busy_hours(text: str) -> Optional[float]:
    """主人说自己在忙 → 预计忙多久（小时）；不是"忙"类句子返回 None。

    规则：显式"N 小时"优先（≤12）；睡觉 8h；开会/上课 2h；其余 3h。疑问句不算。
    """
    body = str(text or "").strip()
    if not body or body.startswith("/"):
        return None
    m = _BUSY_RE.search(body)
    if not m:
        return None
    if _QUESTION_RE.search(body) or _OTHER_SUBJECT_RE.search(body[: m.start()]):
        return None
    if _NEGATION_RE.search(body[: m.start()].rstrip()[-2:] if m.start() else ""):
        return None
    h = _BUSY_HOURS_RE.search(body)
    if h:
        raw = h.group(1)
        n = _CN_NUM.get(raw)
        if n is None:
            try:
                n = int(raw)
            except ValueError:
                n = 3
        return float(max(1, min(12, n)))
    word = m.group(0)
    if "睡" in word:
        return 8.0
    if "开会" in word or "上课" in word or "考试" in word or "面试" in word:
        return 2.0
    return 3.0
