"""
Iris Chat Memory - NumericLeakGuard（ROADMAP P0-5）

用户偏好：人格 / 关系 / 情绪不得以数值进入 prompt。本模块对每个注入段做
正则检查，命中的**行**整行丢弃（注入段里一行即一条画像字段 / 一条记忆 /
一条消息，是可独立丢弃的最小单位），并 WARN 记一次（只记来源、规则 id、
行数，不记正文）。

判定规则（词表只在代码里，不进 prompt）：

- ``metric_number``：关系 / 情绪指标词（好感度、亲密度、心情值、favorability……）
  与数字同句。先剔除时间、时长、日期、计量单位（"3点20分""5分钟""3年"），
  剩下的数字才算分值。
- ``metric_delta``：``好感+5`` / ``+5好感`` 这类增减量写法。
- ``score_with_relation``：裸分值（``90分``、``80%``、``8/10``、``Lv3``、``3级``、
  ``满分``、``百分之八十``）只在与关系词相距很近（同句、±16 字内）时命中，
  避免误拦"考了 90 分""电量 20%"。
- ``metric_word_strict``：仅 strict 模式（画像等系统生成段）——指标词的字段名形态
  （"好感度: …""好感度很高""favorability"）即命中；"刷好感度"之类兴趣描述不算。

原始对话段（L1 上下文、学习样例、主动窗口）用 ``lenient``：只有指标词+数字、增减量
两类强规则删行，裸分值+关系词只记观测（避免"关系型数据库 80%"把主人的话删掉）。

开关 ``lifelike_numeric_guard``（HiddenConfig，默认 true）关闭时 ``guard_text`` 原样
返回、``emotion_hint`` 返回旧格式，行为与现网一致；守卫自身异常时同样回退原文并
记日志。
"""

from __future__ import annotations

import re
import threading

from astrbot.api import logger

__all__ = [
    "scan",
    "filter_lines",
    "guard_enabled",
    "guard_text",
    "emotion_hint",
    "drop_count",
]

LOG_PREFIX = "[lifelike.numeric]"

# ---------------------------------------------------------------- 词表与正则

# 关系 / 情绪指标词：一旦与数字同句即为数值化关系表达
_METRIC = re.compile(
    r"好感(?:度|值|指数|等级)|亲密(?:度|值|指数|等级)|亲近度|"
    r"心情(?:值|指数)|情绪(?:值|指数)|精力值|体力值|"
    r"信任(?:值|度|指数)|依赖(?:度|值)|羁绊(?:值|等级)|爱意值|爱心值|心动值|"
    r"关系(?:等级|值)|"
    # 英文词带 ASCII 词边界（中文紧邻也成立）；affinity / intimacy 只认带 level/score 的字段形态
    # （裸 "CPU affinity" "intimacy" 在技术 / 日常语境里常见，审查 M3）
    r"(?<![A-Za-z])favou?rability(?![A-Za-z])|(?<![A-Za-z])affection(?:\s*(?:level|score|points?|value))?(?![A-Za-z])|"
    r"(?<![A-Za-z])(?:intimacy|affinity)\s*(?:level|score|points?|value)(?![A-Za-z])|"
    r"(?<![A-Za-z])trust\s*(?:level|score|value)(?![A-Za-z])|(?<![A-Za-z])mood\s*(?:value|score|level)(?![A-Za-z])|"
    r"(?<![A-Za-z])relationship\s*(?:level|score|value)(?![A-Za-z])",
    re.IGNORECASE,
)

# strict 模式：只认"字段名形态"（审查 L6：画像兴趣里的"刷好感度""affectionate"不算）
#  - 指标词后接 :：= （"好感度: …"）
#  - 指标词后接高低 / 增减描述（"好感度 很高""亲密度上升"）
#  - 英文 favorability 整词
_STRICT_NAME = r"(?:好感(?:度|值|指数|等级)|亲密(?:度|值|指数)|心情值|精力值|信任值|affection\s*(?:level|score|points?|value)?)"
_STRICT_WORD = re.compile(
    _STRICT_NAME + r"\s*[:：=]"
    r"|" + _STRICT_NAME + r"\s*(?:很|较|比较|非常|极|最|超|已经|已)?\s*"
    r"(?:高|低|满|爆表|上升|下降|提升|提高|降低|增加|减少|变高|变低)"
    r"|(?<![A-Za-z])favou?rability(?![A-Za-z])",
    re.IGNORECASE,
)

# 增减量写法
_DELTA = re.compile(
    r"(?:好感|亲密|信任|羁绊|爱意)\s*[+\-＋－]\s*\d|[+\-＋－]\s*\d+\s*点?\s*(?:好感|亲密|信任|羁绊|爱意)"
)

# 关系语境词（裸分值只在其附近才算关系分值）
_RELATION = re.compile(
    r"好感|亲密|亲近|信任|羁绊|爱意|心动值|(?<![没无有])关系|"
    r"依赖(?:度|值|感|程度)|感情(?:值|度|指数|分)|喜欢(?:程度|值|度|指数)|"
    r"心情(?:值|指数|分)|情绪(?:值|指数|分)|"
    r"对(?:你|我|他|她|ta|TA|主人)的?(?:喜欢|爱|感觉|感情|好感|态度)|"
    r"(?<![A-Za-z])(?:affection|favou?rab\w*|intimacy|trust|mood|relationship|bond)(?![A-Za-z])",
    re.IGNORECASE,
)

# 关系词误报排除（审查 M3）：先把这些词组替换成空白，再找关系词
_RELATION_EXCLUDE = re.compile(
    r"关系型|关系数据库|关系表|关系式|关系图|因果关系|对应关系|映射关系|依赖关系|"
    r"函数关系|线性关系|比例关系|父子关系|继承关系|上下级关系|供需关系|拓扑关系|"
    r"信任链|信任根|信任证书|零信任|(?<![A-Za-z])trust(?:ed)?\s+(?:store|root|chain|anchor)(?![A-Za-z])",
    re.IGNORECASE,
)

# 裸分值记号；前瞻 / 后顾排除时间（"3点20分"）与时长（"5分钟"）
_SCORE = re.compile(
    r"(?<![\d.:：点])\d+(?:\.\d+)?\s*(?:分(?![钟之])|%|％|/\s*(?:100|10|5)(?!\d)|颗星|级(?!别))"
    r"|百分之[零一二两三四五六七八九十百\d]+"
    r"|满分|满级|满格"
    r"|\blv\.?\s*\d+",
    re.IGNORECASE,
)

# 句子边界（"同句"判定）
_SENTENCE_SPLIT = re.compile(r"[。！？!?；;\n]+")

# 需要先剔除的时间 / 时长 / 日期 / 计量表达，剩下的数字才可能是分值
_NON_SCORE_NUMBER = re.compile(
    r"\d{4}\s*[-/年.]\s*\d{1,2}(?:\s*[-/月.]\s*\d{1,2}\s*[日号]?)?"
    r"|\d{1,2}\s*[:：]\s*\d{2}(?:\s*[:：]\s*\d{2})?"
    r"|\d+\s*点\s*\d+\s*分(?:\s*\d+\s*秒)?"
    r"|\d+\s*点(?=钟|半|左右|多|前|后|见|到|起|开始|以前|以后|之前|之后)"
    r"|(?<=早上|上午|中午|下午|晚上|凌晨|今晚|明早|明天|今天|昨天)\d+\s*点"
    r"|(?<=[晚早])\d+\s*点"
    r"|\d+(?:\.\d+)?\s*(?:分钟|秒钟?|小时|个?钟头|天|日|号|周|星期|礼拜|个月|月|年|岁|"
    r"次|遍|回|块钱?|元|角|毛|个|只|条|章|集|页|楼|层|人|位|部|本|首|张|杯|碗|公斤|斤|"
    r"千米|公里|米|kg|km|cm|mm|gb|mb|kb|g|k|w|万|千|百)",
    re.IGNORECASE,
)
_DIGIT = re.compile(r"\d|[零一二两三四五六七八九十百]{2,}\s*分|百分之|满分|满级|满格|满了|max\b", re.IGNORECASE)

# 段标题行（如 "【待续事项】"）：内容行全被丢弃时标题也不留
_HEADER_LINE = re.compile(r"^\s*【[^】]{1,20}】\s*$")

_RELATION_WINDOW = 16

# ---------------------------------------------------------------- 纯函数


def _has_metric_number(sentence: str) -> bool:
    if not _METRIC.search(sentence):
        return False
    rest = _NON_SCORE_NUMBER.sub(" ", sentence)
    return bool(_DIGIT.search(rest))


def _has_score_with_relation(sentence: str) -> bool:
    sentence = _RELATION_EXCLUDE.sub(lambda m: " " * len(m.group(0)), sentence)
    for m in _SCORE.finditer(sentence):
        lo = max(0, m.start() - _RELATION_WINDOW)
        hi = min(len(sentence), m.end() + _RELATION_WINDOW)
        if _RELATION.search(sentence[lo:hi]):
            return True
    return False


def scan(text: str, *, strict: bool = False) -> list[str]:
    """返回命中的规则 id（去重、按固定顺序）；无命中返回空列表。纯函数。"""
    if not text:
        return []
    hits: list[str] = []
    if strict and _STRICT_WORD.search(text):
        hits.append("metric_word_strict")
    if _DELTA.search(text):
        hits.append("metric_delta")
    metric = False
    relation = False
    for sentence in _SENTENCE_SPLIT.split(text):
        if not sentence.strip():
            continue
        if not metric and _has_metric_number(sentence):
            metric = True
        if not relation and _has_score_with_relation(sentence):
            relation = True
        if metric and relation:
            break
    if metric:
        hits.append("metric_number")
    if relation:
        hits.append("score_with_relation")
    return hits


# lenient 模式下仍会删行的强规则：指标词 + 数字、增减量写法（与日常 / 技术话题几乎不重叠）
STRONG_RULES: tuple[str, ...] = ("metric_word_strict", "metric_delta", "metric_number")


def _filter(text: str, *, strict: bool, lenient: bool) -> tuple[str, list[str], int, list[str], int]:
    """返回 ``(清洗后文本, 删除规则, 删除行数, 仅观测规则, 仅观测行数)``。"""
    if not text:
        return text, [], 0, [], 0
    kept: list[str] = []
    rules: list[str] = []
    observed: list[str] = []
    dropped = 0
    seen = 0
    for line in text.split("\n"):
        hit = scan(line, strict=strict)
        if hit and lenient and not any(r in STRONG_RULES for r in hit):
            # 原始对话段（L1 / 学习样例 / 主动窗口）：弱规则只记观测，不删主人自己的话
            seen += 1
            for r in hit:
                if r not in observed:
                    observed.append(r)
            kept.append(line)
            continue
        if hit:
            dropped += 1
            for r in hit:
                if r not in rules:
                    rules.append(r)
            continue
        kept.append(line)
    if not dropped:
        return text, [], 0, observed, seen
    if all(not ln.strip() or _HEADER_LINE.match(ln) for ln in kept):
        return "", rules, dropped, observed, seen
    return "\n".join(kept), rules, dropped, observed, seen


def filter_lines(text: str, *, strict: bool = False, lenient: bool = False) -> tuple[str, list[str], int]:
    """逐行检查，丢弃命中行。返回 ``(清洗后文本, 命中规则 id, 丢弃行数)``。纯函数。

    没有任何命中时原样返回输入（逐字节一致）。剩余内容只剩段标题行时返回 ''。
    ``lenient=True``（原始对话段）：只有 ``STRONG_RULES`` 命中的行会被丢弃。
    """
    cleaned, rules, dropped, _obs, _seen = _filter(text, strict=strict, lenient=lenient)
    return cleaned, rules, dropped


# ---------------------------------------------------------------- 开关感知包装

_lock = threading.Lock()
_drops = 0


def drop_count() -> int:
    """进程内累计丢弃行数（M17 缺陷计数，目标 0）。"""
    return _drops


def _cfg(key: str, default: object) -> object:
    try:
        from ...config import get_config

        value = get_config().get(key, default)
    except Exception:
        return default
    return default if value is None else value


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "on", "yes"):
            return True
        if v in ("0", "false", "off", "no"):
            return False
    return default


def guard_enabled() -> bool:
    """``lifelike_numeric_guard``（默认 true，P0 修复项，不受 lifelike_enable 控制）。"""
    return _as_bool(_cfg("lifelike_numeric_guard", True), True)


def guard_text(text: str, *, source: str, strict: bool = False, lenient: bool = False) -> str:
    """开关打开时丢弃注入段中的数值化关系行；关闭或异常时原样返回。

    ``lenient=True`` 用于原始对话段（L1 上下文、学习样例、主动窗口）：弱规则命中
    只记 DEBUG 观测，不删行、不计入缺陷计数（审查 M3）。
    """
    global _drops
    if not text:
        return text
    try:
        if not guard_enabled():
            return text
        cleaned, rules, dropped, observed, seen = _filter(text, strict=strict, lenient=lenient)
    except Exception as e:
        logger.warning(f"{LOG_PREFIX} guard failed source={source}: {type(e).__name__}")
        return text
    if seen:
        logger.debug(
            f"{LOG_PREFIX} observed source={source} lines={seen} rules={','.join(observed)}"
        )
    if dropped:
        with _lock:
            _drops += dropped
        logger.warning(
            f"{LOG_PREFIX} dropped source={source} lines={dropped} "
            f"rules={','.join(rules)} section_emptied={not cleaned}"
        )
    return cleaned


# ---------------------------------------------------------------- 情绪推测标注

_HEDGES = ("可能", "也许", "或许", "似乎", "好像", "大概", "看起来")
EMOTION_HEDGE_SUFFIX = "（推测，未确认，别当成事实说出口）"
_EMOTION_MODES = ("auto", "off", "hedged", "raw")


def _emotion_mode() -> str:
    """解析 ``lifelike_emotion_hint``：auto/off/hedged/raw。

    守卫总开关关闭 → raw（现网行为）。auto：``lifelike_memory_mode == on`` 时 hedged，
    否则 off（ROADMAP P0-5 ②"二选一，默认后者"；DECISIONS D8 推测须有置信度且带
    "可能"，画像情感基线没有置信度，故在认知模块上线前不注入）。
    """
    if not guard_enabled():
        return "raw"
    mode = str(_cfg("lifelike_emotion_hint", "auto") or "auto").strip().lower()
    if mode not in _EMOTION_MODES:
        mode = "auto"
    if mode == "auto":
        memory_mode = str(_cfg("lifelike_memory_mode", "off") or "off").strip().lower()
        mode = "hedged" if memory_mode == "on" else "off"
    return mode


def emotion_hint(baseline: str) -> tuple[str, str] | None:
    """画像"情感"字段的注入形式：返回 ``(标签后缀, 值)``；``None`` 表示不注入。

    - raw：``("", 原值)``——与现网 ``情感: <emotional_baseline>`` 一致；
    - hedged：``("（推测，未确认…）", "可能<值>")``；值里带数值化表达时不注入；
    - off：``None``。
    异常时回退 raw（旧行为）。
    """
    value = str(baseline or "").strip()
    if not value:
        return None
    try:
        mode = _emotion_mode()
    except Exception as e:
        logger.warning(f"{LOG_PREFIX} emotion mode failed: {type(e).__name__}")
        return "", baseline
    if mode == "raw":
        return "", baseline
    if mode == "off":
        return None
    if scan(value, strict=True):
        logger.warning(f"{LOG_PREFIX} dropped source=emotion_baseline lines=1 rules=numeric")
        return None
    if not value.startswith(_HEDGES):
        value = f"可能{value}"
    return EMOTION_HEDGE_SUFFIX, value
