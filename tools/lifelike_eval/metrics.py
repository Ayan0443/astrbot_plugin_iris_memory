"""活人感离线指标（纯函数，无 I/O，无全局状态）。

所有函数只接收字符串 / 字符串序列，返回数字、规则 id 或标记名；
聚合结果只含规则 id、标记名（如 ``tail:吧``，单字）和数字，不含任何正文片段，可直接落盘。

口径来源见 ``rules`` 模块文档；切句、反应词、句尾语气词、
"疑问句里的呢不算"等定义严格按口吻附录 §5.5 的编程定义实现。
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

from . import rules as R
from ...iris_memory.identity import LazyRules

_BL = LazyRules(R.BLACKLIST, pattern_index=1)
_COPY_HARD = LazyRules(R.COPY_HARD, pattern_index=1, flags_index=2, drop_flags=True, reference_kind="copy_hard")
_COPY_SOFT = LazyRules(R.COPY_SOFT, pattern_index=1, flags_index=2, drop_flags=True, reference_kind="copy_soft")
_AIW = tuple((rid, re.compile(p, f)) for rid, p, f in R.AIW_PATTERNS)
_MAN = tuple((rid, re.compile(p, f)) for rid, p, f in R.MAN_PATTERNS)
_PRIV = LazyRules(R.PRIV_PATTERNS, pattern_index=1, flags_index=2, drop_flags=True)
_CUTE = tuple((rid, re.compile(p, f)) for rid, p, f in R.CUTE_PATTERNS)
_STRIP_FOR_CORE = re.compile(r"[\W_]+", re.UNICODE)

# 同标记重复率阈值（口吻附录 §5.5 建议值；只在代码里）
REPEAT_WINDOW = 20
REPEAT_MARKER_MAX = 0.30
REPEAT_TILDE_MAX = 0.20
CATCHPHRASE_MIN_COUNT = 3
HEAD_TAIL_LEN = 4


# ---------------------------------------------------------------- 基础


def ratio(numer: float, denom: float) -> float | None:
    """denom 为 0 时返回 None（"无数据"，不是 0）。"""
    if not denom:
        return None
    return round(float(numer) / float(denom), 4)


def split_sentences(text: str) -> list[str]:
    """按 ``。！？!?``、换行、分号切句，去空白；丢掉只有标点 / emoji 的碎片
    （例如"你呢？～"切出的"～"），避免它们稀释语气词密度的分母。"""
    out = []
    for s in R.SENTENCE_SPLIT.split(text or ""):
        s = (s or "").strip()
        if s and core_text(s):
            out.append(s)
    return out


def _strip_trailing(s: str) -> str:
    s = s.rstrip(R.TRAILING_PUNCT)
    # 句末 emoji / 颜文字不影响语气词判断
    while s and (R.EMOJI_RE.match(s[-1]) or R.KAOMOJI_RE.match(s[-1])):
        s = s[:-1].rstrip(R.TRAILING_PUNCT)
    return s


def _is_question(sentence: str) -> bool:
    t = sentence.rstrip(" \t\r\n")
    while t and (R.EMOJI_RE.match(t[-1]) or t[-1] in R.TILDE_CHARS):
        t = t[:-1].rstrip()
    return t.endswith(("？", "?"))


def ends_with_question(text: str) -> bool:
    """整条回复的最后一句是否以问号结尾（AIW-03 的基线指标）。"""
    sents = split_sentences(text)
    return bool(sents) and _is_question(sents[-1])


def question_count(text: str) -> int:
    return sum(1 for s in split_sentences(text) if _is_question(s))


def sentence_tail_particle(sentence: str) -> str | None:
    """句尾语气词；疑问句里的"呢"不算。"""
    core = _strip_trailing(sentence)
    if not core:
        return None
    last = core[-1]
    if last not in R.TAIL_PARTICLES:
        return None
    if last == "呢" and _is_question(sentence):
        return None
    return last


def tail_particles(text: str) -> list[str]:
    """一条回复里每个带句尾语气词的句子对应的语气词（按句序）。"""
    return [p for p in (sentence_tail_particle(s) for s in split_sentences(text)) if p]


def reaction_opener(text: str) -> str | None:
    """第一句开头的反应词（不同写法归一：诶？/诶！/诶， 都算"诶"）。"""
    sents = split_sentences(text)
    if not sents:
        return None
    head = sents[0].lstrip()
    for w in R.REACTION_WORDS:
        if head.startswith(w):
            return w
    return None


def tilde_count(text: str) -> int:
    return sum((text or "").count(c) for c in R.TILDE_CHARS)


def has_double_bang(text: str) -> bool:
    return bool(re.search(r"[!！]{2,}", text or ""))


def bang_count(text: str) -> int:
    return sum((text or "").count(c) for c in "!！")


def emoji_count(text: str) -> int:
    return len(R.EMOJI_RE.findall(text or "")) + len(R.KAOMOJI_RE.findall(text or ""))


def core_text(text: str) -> str:
    """去标点、空白和 emoji 后的文本（用于首尾 4 字口头禅判定）。"""
    t = R.EMOJI_RE.sub("", text or "")
    return _STRIP_FOR_CORE.sub("", t)


def head_tail(text: str, n: int = HEAD_TAIL_LEN) -> tuple[str, str] | None:
    """不足 2n 个字的回复不参与（返回 None）。"""
    c = core_text(text)
    if len(c) < 2 * n:
        return None
    return c[:n], c[-n:]


def contains_cjk_run(text: str, n: int = 8) -> bool:
    """是否含连续 n 个及以上汉字（快照"无原文"自动断言用）。"""
    return bool(R.cjk_run_re(n).search(text or ""))


# ---------------------------------------------------------------- 规则命中


def blacklist_hits(text: str, user_text: str = "") -> list[str]:
    """口吻附录 §2.3 / §5.4 黑名单与计数闸门。"""
    text = text or ""
    hits: list[str] = []
    for rid, rx, exempt in _BL:
        if not rx.search(text):
            continue
        if exempt == "user_used" and user_text and rx.search(user_text):
            continue
        hits.append(rid)
    if tilde_count(text) > 1:
        hits.append("BL-TILDE-2")
    if has_double_bang(text):
        hits.append("BL-BANG-2")
    if emoji_count(text) > 1:
        hits.append("BL-EMOJI-2")
    if any(v >= 2 for v in Counter(tail_particles(text)).values()):
        hits.append("BL-TAIL-DUP")
    return hits


def is_follow_turn(user_text: str) -> bool:
    """对方本轮先用了二次元腔 / 猫娘梗等（"跟随一轮"）。"""
    return bool(R.FOLLOW_TRIGGERS.search(user_text or ""))


def work_mentioned(user_texts: Iterable[str]) -> bool:
    return any(R.WORK_MENTION.search(t or "") for t in user_texts)


def copy_hits(text: str, user_texts: Sequence[str] = ()) -> dict[str, list[str]]:
    """禁止照搬清单（只查 bot 文本）。

    返回 ``hard``（任何情况都算失败）、``soft``（计入软提示命中）、
    ``soft_exempt``（对方先提到作品而豁免、需人工过目的软提示）。
    """
    text = text or ""
    hard = [rid for rid, rx in _COPY_HARD if rx.search(text)]
    soft_all = [rid for rid, rx in _COPY_SOFT if rx.search(text)]
    if soft_all and work_mentioned(user_texts):
        return {"hard": hard, "soft": [], "soft_exempt": soft_all}
    return {"hard": hard, "soft": soft_all, "soft_exempt": []}


def aiw_hits(text: str, user_text: str = "") -> list[str]:
    text = text or ""
    hits = [rid for rid, rx in _AIW if rx.search(text)]
    if question_count(text) >= 2:
        hits.append("AIW-03")
    if user_text and "AIW-04" not in hits:
        n = len(text.strip())
        if n > 80 and n > 3 * len(user_text.strip()):
            hits.append("AIW-04")
    return sorted(set(hits))


def man_hits(text: str) -> list[str]:
    return [rid for rid, rx in _MAN if rx.search(text or "")]


def priv_hits(text: str) -> list[str]:
    return [rid for rid, rx in _PRIV if rx.search(text or "")]


def numeric_leak_hits(text: str) -> list[str]:
    """ROADMAP P0-5 口径：量化词无条件命中；裸分值只在与关系 / 情绪词同句时命中。"""
    text = text or ""
    hits: list[str] = []
    if R.NUMERIC_WORDS.search(text):
        hits.append("NL-WORD")
    for sent in split_sentences(text) or [text]:
        if R.NUMERIC_SCORE.search(sent) and R.RELATION_WORDS.search(sent):
            hits.append("NL-SCORE")
            break
    return hits


def cute_candidates(text: str) -> list[str]:
    """卖萌规则初筛（只初筛；是否卖萌由人工 / LLM 是否题决定）。"""
    return [rid for rid, rx in _CUTE if rx.search(text or "")]


def light_register_overflow(text: str) -> list[str]:
    """口吻附录 §7.2"场景越档（规则部分）"：期望淡档的输出出现浓档手段。"""
    text = text or ""
    hits: list[str] = []
    if tilde_count(text) > 0:
        hits.append("LIGHT-TILDE")
    if any(sentence_tail_particle(s) in R.LIGHT_FORBIDDEN_TAILS for s in split_sentences(text)):
        hits.append("LIGHT-TAIL-MA-YA")
    sents = split_sentences(text)
    n_react = sum(1 for s in sents if any(s.lstrip().startswith(w) for w in R.REACTION_WORDS))
    if n_react > 1:
        hits.append("LIGHT-REACTION-2")
    if bang_count(text) > 1:
        hits.append("LIGHT-BANG-2")
    if re.search(r"嘛[。！!~～]?$", text.strip()) and re.search(r"^(?:你|快|就|再|帮|陪|别)", text.strip()):
        hits.append("LIGHT-COAX")
    return hits


def scan(text: str, ctx: dict[str, Any] | None = None) -> list[str]:
    """一条 bot 回复的全部规则命中（去重、排序）。

    ctx 可含：``user_text``（对方本轮）、``user_texts``（本轮 + 最近若干轮）、
    ``session``（``private`` / ``group`` / ``proactive``）、``register``（``light`` 时检查越档）。
    """
    ctx = ctx or {}
    user_text = str(ctx.get("user_text") or "")
    user_texts = list(ctx.get("user_texts") or ([user_text] if user_text else []))
    hits: list[str] = []
    hits += blacklist_hits(text, user_text)
    c = copy_hits(text, user_texts)
    hits += c["hard"] + c["soft"] + [f"{r}~exempt" for r in c["soft_exempt"]]
    hits += aiw_hits(text, user_text)
    hits += man_hits(text)
    if ctx.get("session") == "group":
        hits += priv_hits(text)
    hits += numeric_leak_hits(text)
    hits += cute_candidates(text)
    if ctx.get("register") == "light":
        hits += light_register_overflow(text)
    return sorted(set(hits))


# ---------------------------------------------------------------- 序列指标


def reply_markers(text: str) -> set[str]:
    """一条回复包含的口吻标记（同标记重复率的分子单位）。"""
    m: set[str] = set()
    r = reaction_opener(text)
    if r:
        m.add(f"react:{r}")
    for p in tail_particles(text):
        m.add(f"tail:{p}")
    if tilde_count(text):
        m.add("tilde")
    return m


def marker_repeat(replies: Sequence[str], n: int = REPEAT_WINDOW) -> dict[str, Any]:
    """口吻附录 §5.5"同标记重复率"：取最近 n 条回复。

    不足 n 条时 ``status=insufficient``、不告警。``rates`` 只含标记名与比例。
    """
    replies = [r for r in replies if (r or "").strip()]
    if len(replies) < n:
        return {"status": "insufficient", "n": len(replies), "window": n, "rates": {}, "alerts": []}
    window = replies[-n:]
    cnt: Counter[str] = Counter()
    for r in window:
        cnt.update(reply_markers(r))
    rates = {k: round(v / n, 4) for k, v in sorted(cnt.items())}
    alerts: list[str] = []
    for k, v in rates.items():
        lim = REPEAT_TILDE_MAX if k == "tilde" else REPEAT_MARKER_MAX
        if v > lim:
            alerts.append(f"repeat:{k}")
    rep = head_tail_repeats(window)
    if rep["head_repeated"] or rep["tail_repeated"]:
        alerts.append("catchphrase")
    if any(blacklist_hits(r) for r in window):
        alerts.append("blacklist")
    return {"status": "ok", "n": len(replies), "window": n, "rates": rates, "alerts": sorted(alerts)}


def head_tail_repeats(replies: Sequence[str], min_count: int = CATCHPHRASE_MIN_COUNT) -> dict[str, int]:
    """首 / 尾 4 字完全相同的短语中，出现 ≥min_count 次的个数（只返回个数，不返回短语）。"""
    heads: Counter[str] = Counter()
    tails: Counter[str] = Counter()
    eligible = 0
    for r in replies:
        ht = head_tail(r)
        if ht is None:
            continue
        eligible += 1
        heads[ht[0]] += 1
        tails[ht[1]] += 1
    return {
        "eligible": eligible,
        "head_repeated": sum(1 for v in heads.values() if v >= min_count),
        "tail_repeated": sum(1 for v in tails.values() if v >= min_count),
    }


def length_stats(replies: Sequence[str]) -> dict[str, float | int | None]:
    lens = [len((r or "").strip()) for r in replies]
    if not lens:
        return {"n": 0, "median": None, "mean": None, "p90": None, "max": None}
    srt = sorted(lens)
    p90 = srt[min(len(srt) - 1, int(round(0.9 * (len(srt) - 1))))]
    return {
        "n": len(lens),
        "median": round(float(statistics.median(lens)), 2),
        "mean": round(float(statistics.fmean(lens)), 2),
        "p90": p90,
        "max": srt[-1],
    }


def tail_particle_density(replies: Sequence[str]) -> float | None:
    """带句尾语气词的句子数 ÷ 总句数。"""
    total = 0
    with_p = 0
    for r in replies:
        sents = split_sentences(r)
        total += len(sents)
        with_p += sum(1 for s in sents if sentence_tail_particle(s))
    return ratio(with_p, total)


def rule_hit_rates(replies: Sequence[str], ctxs: Sequence[dict[str, Any]] | None = None) -> dict[str, float]:
    """每条规则的命中回复比例（命中回复数 ÷ 回复数），只输出命中过的规则。"""
    n = len(replies)
    if not n:
        return {}
    cnt: Counter[str] = Counter()
    for i, r in enumerate(replies):
        ctx = ctxs[i] if ctxs and i < len(ctxs) else None
        cnt.update(set(scan(r, ctx)))
    return {k: round(v / n, 4) for k, v in sorted(cnt.items())}


def aggregate_replies(
    replies: Sequence[str],
    ctxs: Sequence[dict[str, Any]] | None = None,
    *,
    window: int = REPEAT_WINDOW,
) -> dict[str, Any]:
    """一组回复的全部聚合指标（只含数字和规则 id）。"""
    replies = [r for r in replies if r is not None]
    n = len(replies)
    blk = [blacklist_hits(r, (ctxs[i].get("user_text", "") if ctxs and i < len(ctxs) else "")) for i, r in enumerate(replies)]
    copies = [
        copy_hits(r, (ctxs[i].get("user_texts", ()) if ctxs and i < len(ctxs) else ())) for i, r in enumerate(replies)
    ]
    rep = head_tail_repeats(replies)
    return {
        "n": n,
        "length": length_stats(replies),
        "question_end_rate": ratio(sum(ends_with_question(r) for r in replies), n),
        "multi_question_rate": ratio(sum(question_count(r) >= 2 for r in replies), n),
        "tail_particle_sentence_rate": tail_particle_density(replies),
        "tilde_reply_rate": ratio(sum(tilde_count(r) > 0 for r in replies), n),
        "double_bang_count": sum(has_double_bang(r) for r in replies),
        "reaction_open_rate": ratio(sum(reaction_opener(r) is not None for r in replies), n),
        "markdown_rate": ratio(sum("AIW-05" in aiw_hits(r) for r in replies), n),
        "emoji_reply_rate": ratio(sum(emoji_count(r) > 0 for r in replies), n),
        "head4_repeated_ge3": rep["head_repeated"],
        "tail4_repeated_ge3": rep["tail_repeated"],
        "blacklist_hit_rate": ratio(sum(bool(b) for b in blk), n),
        "copy_hard_hit_rate": ratio(sum(bool(c["hard"]) for c in copies), n),
        "copy_soft_hit_rate": ratio(sum(bool(c["soft"]) for c in copies), n),
        "copy_soft_exempt_count": sum(bool(c["soft_exempt"]) for c in copies),
        "numeric_leak_hit_count": sum(bool(numeric_leak_hits(r)) for r in replies),
        "cute_candidate_rate": ratio(sum(bool(cute_candidates(r)) for r in replies), n),
        "marker_repeat_overall": _overall_marker_rates(replies),
        "marker_repeat_window": marker_repeat(replies, window),
        "rule_hit_rates": rule_hit_rates(replies, ctxs),
    }


def _overall_marker_rates(replies: Sequence[str]) -> dict[str, float]:
    """不设窗口的标记出现率（小样本基线用；正式告警看 marker_repeat_window）。"""
    n = len(replies)
    if not n:
        return {}
    cnt: Counter[str] = Counter()
    for r in replies:
        cnt.update(reply_markers(r))
    return {k: round(v / n, 4) for k, v in sorted(cnt.items())}
