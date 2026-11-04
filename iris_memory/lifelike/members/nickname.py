"""群友常用称呼（``lifelike_member_nickname``）。

用户要求："要学会叫群友的昵称，不能老是叫全名，这很怪"；同时不能乱起外号。

1. 学习（ledger 维护周期里做，每人每天最多重算一次，零模型调用）：
   - 候选只取该群友名字本身的片段（不生成叠字、不起外号），去掉常用词（停用词表）；
   - 在授权群近 30 天**其他人**的公开发言里，只数"称呼语境"里的出现：@ 后面、"叫 / 喊"后面，
     或在消息开头 / 标点 / 空格之后、且后面紧跟标点 / 语气词 / 结尾（"小林，晚上打吗""@阿杰 在吗""好的，小雨～"）；
   - 学到的称呼须被 ≥2 个不同的人用过、共 ≥ ``MIN_USES`` 次；在全部发言里出现很多次却很少用作称呼的
     （泛词）不学；取次数最多、最近用过的 → ``member_aliases(source='nickname')``，用量记在 ``member_nick_usage``；
   - 跨群：档案按 Telegram 账号一人一份，各群合并计数；墓碑 / 未成年 / "别记我"的人不学；敏感词不学。
2. 学不到时用规则从显示名生成简称 ``natural_short``；不像名字（"好想睡觉""大家好"）或像 Telegram 用户名
   （"jay_dev"）→ ""（不叫名字）。

规则（样例见测试）：
- 去掉表情、装饰符号、括号 / 方括号 / 竖线后的标签（"小林🌸【摸鱼中】" → "小林"）；
- 中文：去掉"我是 / 一只 / 今天也"类前缀、"××的××"取"的"后面；1 个字 → 不叫；2–3 个字 → 原样；
  ≥4 个字 → 取前 2 个字；结果含常用虚词 / 是常用词 → 不叫；
- 英文：取第一个词（按空格、连字符、驼峰切分）；>8 个字母取前 4 个（"Alexandria Wong" → "Alex"）；
- 像用户名（字母开头、只有字母数字下划线，且带下划线 / 数字或全小写）、纯数字、泛词 → ""。
"""


from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

KEY_NICKNAME = "lifelike_member_nickname"
MIN_USES = 2
SOURCE_NICK = "nickname"

_BRACKETS = re.compile(r"[\(\[（【「『<《][^\)\]）】」』>》]*[\)\]）】」』>》]")
_TAIL_TAG = re.compile(r"\s*[|｜丨/\\·•~～\-—_]{1,2}\s*[^|｜丨/\\·•]*$")
_CJK = re.compile(r"[一-鿿㐀-䶿]")
_CN_PREFIX = re.compile(r"^(?:我是|是个|一只|一个|一条|今天也|今天|每天都|永远的|你的|爱吃|超级|小小的)")
_WORD_SPLIT = re.compile(r"[\s_\-.·]+|(?<=[a-z])(?=[A-Z])")
_GENERIC = frozenset({"用户", "群主", "管理员", "匿名", "admin", "user", "bot", "test", "the", "official"})


def _keep_char(ch: str) -> bool:
    if _CJK.match(ch):
        return True
    cat = unicodedata.category(ch)
    return cat[0] in ("L", "N") or ch in " _-.·|｜丨/\\·•~～—"


def clean_display(name: str) -> str:
    """去表情 / 装饰符 / 括号标签 / 竖线后缀，返回干净的名字（可能为空）。"""
    s = str(name or "").strip()
    s = _BRACKETS.sub(" ", s)
    s = "".join(ch for ch in s if _keep_char(ch))
    prev = None
    while prev != s:
        prev = s
        cut = _TAIL_TAG.sub("", s).strip()
        if cut:
            s = cut
        else:
            break
    s = re.sub(r"[_\-.·|｜丨/\\·•~～—]+$", "", s).strip()
    s = re.sub(r"^[_\-.·|｜丨/\\·•~～—]+", "", s).strip()
    return re.sub(r"\s+", " ", s)


# 常用词：不当作称呼候选（学习）也不当作规则简称
STOPWORDS = frozenset("""
什么 大家 睡觉 今天 明天 昨天 现在 好想 不知 知道 我们 你们 他们 她们 这个 那个 怎么 为什么 可以 没有
就是 还是 时候 一下 一起 真的 好的 哈哈 喜欢 觉得 已经 然后 因为 所以 但是 如果 晚安 早安 谢谢 开心
快乐 摸鱼 不是 是不 吃饭 吃什么 一个 一只 自己 东西 事情 工作 上班 下班 学习 游戏 不要 不想 想要 好吗
可爱 厉害 加油 无聊 睡了 起床 在吗 你好 大家好 好想睡觉 不知道 什么时候 小可爱 宝宝 老师 同学 朋友
群主 管理 用户 匿名 老板 哥哥 姐姐 弟弟 妹妹 帅哥 美女 先生 小姐
""".split())
_FUNCTION_CHARS = frozenset("我你他她它的了是在有没要会去来吃睡想不就也都很好吗呢吧啊哦嗯这那哪什么么")
_EN_STOP = frozenset({"the", "hello", "hi", "good", "my", "your", "i", "im", "a", "an", "mr", "ms", "official"})
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


def looks_like_username(name: str) -> bool:
    """像 Telegram 用户名（适配器在没有显示名时用 username 当名字）：字母开头、只有字母数字下划线，
    且带下划线 / 数字或全小写。"""
    s = str(name or "").strip().lstrip("@")
    if not _USERNAME_RE.match(s):
        return False
    return "_" in s or any(ch.isdigit() for ch in s) or s.islower()


def _ok(nick: str) -> bool:
    n = str(nick or "").strip()
    if not n or n.isdigit() or n.lower() in _GENERIC or n in STOPWORDS or n.lower() in _EN_STOP:
        return False
    if _CJK.search(n):
        if len(n) < 2 or any(ch in _FUNCTION_CHARS for ch in n):
            return False
    elif len(n) < 2:
        return False
    return len(n) <= 8


def _cn_core(cjk: str) -> str:
    """去掉"我是 / 一只 …"类前缀（可叠加），"快乐的咸鱼"取"的"后面的部分。"""
    core = cjk
    prev = None
    while prev != core:
        prev = core
        core = _CN_PREFIX.sub("", core) or core
    if "的" in core:
        tail = core.rsplit("的", 1)[-1]
        if tail:
            core = tail
    return core


def natural_short(name: str) -> str:
    """规则生成自然简称；不像名字 / 像用户名 → ""（不叫名字）。"""
    if looks_like_username(name):
        return ""
    s = clean_display(name)
    if not s or s in STOPWORDS:
        return ""
    cjk = "".join(ch for ch in s if _CJK.match(ch))
    if cjk and len(cjk) >= len(re.sub(r"\s", "", s)) / 2:
        if cjk in STOPWORDS:
            return ""
        core = _cn_core(cjk)
        if len(core) <= 1:
            return ""
        out = core if len(core) <= 3 else core[:2]
        return out if _ok(out) else ""
    words = [w for w in _WORD_SPLIT.split(s) if w and re.search(r"[A-Za-z]", w)]
    if not words:
        return ""
    w = re.sub(r"[^A-Za-z0-9]", "", words[0])
    if len(w) > 8:
        w = w[:4]
    w = w[:1].upper() + w[1:] if w else w
    return w if _ok(w) else ""


def candidates(names: Iterable[str]) -> List[str]:
    """显示名 → 候选叫法（名字本身的片段；不生成叠字；去掉常用词；按自然程度排序，去重）。"""
    out: List[str] = []

    def add(x: str) -> None:
        x = str(x or "").strip()
        if _ok(x) and x not in out:
            out.append(x)

    for raw in names or ():
        if looks_like_username(raw):
            # 用户名只在群里确实有人这么叫时才学（照样要过称呼语境与 2 人门槛）
            u = str(raw).strip().lstrip("@")
            add(u)
            head = re.split(r"[_\d]", u)[0]
            if len(head) >= 3:
                add(head)
            continue
        s = clean_display(raw)
        if not s:
            continue
        add(natural_short(raw))
        cjk = "".join(ch for ch in s if _CJK.match(ch))
        if len(cjk) >= 2:
            core = _cn_core(cjk)
            if len(core) <= 4:
                add(core)
            add(core[-2:])
            if len(core) >= 4:
                add(core[:2])
        for w in _WORD_SPLIT.split(s):
            w = re.sub(r"[^A-Za-z0-9]", "", w)
            if re.search(r"[A-Za-z]", w):
                add(w[:1].upper() + w[1:])
                if len(w) > 5:
                    add(w[:1].upper() + w[1:4])
    return out


_EDGE = r"\s，,。.！!？?~～：:、…"
_AFTER = _EDGE + r"呀啊呢吧哦嘛哈啦"


def _address_re(cand: str) -> "re.Pattern[str]":
    c = re.escape(cand)
    return re.compile(
        rf"@{c}|(?:^|[{_EDGE}]){c}(?=$|[{_AFTER}])|(?:叫|喊)(?:他|她)?{c}",
        re.IGNORECASE,
    )


def count_usage(
    rows: Iterable[Tuple[str, float, str]], cands: Sequence[str]
) -> Dict[str, Tuple[int, float, int]]:
    """其他人发言里每个候选**作为称呼**出现的次数、最近时间、不同发言人数（同一条消息只计一次）。

    rows: (text, ts, sender_id)。另外会把"出现很多次却很少用作称呼"的候选剔除（泛词）。"""
    pats = [(c, _address_re(c), c.lower()) for c in cands]
    hits: Counter = Counter()
    anywhere: Counter = Counter()
    last: Dict[str, float] = {}
    senders: Dict[str, set] = {}
    for text, ts, sender in rows or ():
        body = str(text or "")
        low = body.lower()
        for c, rx, cl in pats:
            if cl and cl in low:
                anywhere[c] += 1
                if rx.search(body):
                    hits[c] += 1
                    last[c] = max(last.get(c, 0.0), float(ts or 0.0))
                    senders.setdefault(c, set()).add(str(sender or ""))
    out: Dict[str, Tuple[int, float, int]] = {}
    for c, n in hits.items():
        if anywhere[c] >= 20 and n / anywhere[c] < 0.3:
            continue  # 泛词：经常出现，但很少用来称呼人
        out[c] = (n, last.get(c, 0.0), len(senders.get(c, ())))
    return out


def choose(usage: Dict[str, Tuple], min_uses: int = MIN_USES, min_speakers: int = 2) -> Optional[str]:
    best = []
    for c, val in usage.items():
        n, t = int(val[0]), float(val[1])
        speakers = int(val[2]) if len(val) > 2 else 0
        if n >= min_uses and speakers >= min_speakers:
            best.append((n, t, c))
    if not best:
        return None
    best.sort(key=lambda x: (x[0], x[1], -len(x[2])), reverse=True)
    return best[0][2]


def address_for(display_name: str, learned: Optional[str]) -> str:
    """群友的常用称呼：学到的优先，否则规则简称；都没有 → ""（不叫名字）。"""
    if learned and _ok(learned):
        return learned
    return natural_short(display_name)


NICKNAME_NOTE_GROUP = (
    "[称呼提示] 群里称呼别人时，用下面的常用称呼，或者干脆不称呼；不要用完整的显示名，也不要每句都点名；"
    "跟群里大家的叫法保持一致，不要自己起外号。{owner_line}\n{table}"
)


def owner_address_line() -> str:
    """"称呼 阿明 时可以叫 阿明 或 小明。"；未配置主人称呼 → ""。"""
    from ...identity import owner_display, owner_names_phrase

    names = owner_names_phrase()
    return f"称呼 {owner_display()} 时可以叫 {names}。" if names else ""
NICKNAME_NOTE_PRIVATE = (
    "[称呼提示] 提到群里的人时，用下面的常用称呼，不要用完整的显示名，也不要自己起外号。\n{table}"
)


def render_table(pairs: Sequence[Tuple[str, str]], limit: int = 12) -> str:
    """[(显示名, 称呼)] → 多行文本；称呼为空的写"不用叫名字"。"""
    lines = []
    seen = set()
    for display, nick in pairs:
        d = str(display or "").strip()
        if not d or d in seen:
            continue
        seen.add(d)
        lines.append(f"- {d[:24]} → {nick if nick else '（不用叫名字）'}")
        if len(lines) >= limit:
            break
    return "\n".join(lines)


__all__ = [
    "KEY_NICKNAME",
    "MIN_USES",
    "SOURCE_NICK",
    "clean_display",
    "looks_like_username",
    "STOPWORDS",
    "natural_short",
    "candidates",
    "count_usage",
    "choose",
    "address_for",
    "render_table",
    "NICKNAME_NOTE_GROUP",
    "NICKNAME_NOTE_PRIVATE",
]
