"""
Iris Chat Memory - 表达模式提取（规则，零 LLM）

从 user→bot 对话对中按规则提取"场景→表达"映射：
- 场景：按触发消息的标点与关键词规则分类（问句/指令/感叹/闲聊）；
- 表达：bot 回复中的口语化句式（开头称谓、语气词结尾、短句）。

同时负责表达模式的衰减淘汰（委托 LearningStorage）。
"""

import re
from typing import List

from ..core import get_logger
from .storage import LearningStorage

logger = get_logger("learning.expression")

# pending 学习条目超龄清理阈值（天）：审查长期失败时兜底，防止无界增长
_STALE_PENDING_DAYS = 30

# 语气词结尾（判定口语化句式）
_TONE_SUFFIXES = ("呀", "啊", "呢", "吧", "嘛", "哦", "哈", "啦", "哩")

# 问句特征词
_QUESTION_WORDS = ("吗", "呢", "什么", "怎么", "哪", "谁", "为啥", "为什么", "多少", "几")

# 指令特征词（祈使语气）
_COMMAND_WORDS = ("帮", "给", "发", "查", "搜", "打", "叫", "算", "翻译", "总结")

# 句子切分
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?\n])|(?<=[；;])")

# 常见动词开头（粗判指令句）
_VERB_START = re.compile(r"^[帮给发查搜打叫算请去把将拿看写说讲唱]")


def classify_scene(user_text: str) -> str:
    """按规则分类触发消息的场景类型

    Args:
        user_text: 用户消息文本

    Returns:
        场景标签：question / command / exclaim / chat
    """
    text = user_text.strip()
    if not text:
        return "chat"

    # 问句：问号结尾或含疑问词
    if "?" in text or "？" in text:
        return "question"
    if any(w in text for w in _QUESTION_WORDS) and (
        text.endswith(("吗", "呢", "么")) or "什么" in text or "怎么" in text
    ):
        return "question"

    # 指令：动词开头或含祈使特征词
    if _VERB_START.match(text) or any(w in text[:4] for w in _COMMAND_WORDS):
        return "command"

    # 感叹：叹号结尾
    if "!" in text or "！" in text:
        return "exclaim"

    return "chat"


# P1-3（口吻附录 §5.5）：反应词与句末标点，用于新抽取规则
_REACTION_CHARS = frozenset("诶欸啊嗯哦噢哇唔哈嘿呀耶呃额喔咦哎唉嘻")
_REACTION_WORDS = ("好耶",)
_EDGE_PUNCT = "。！？!?~～…．.，,、；; \t\r\n"
_REACTION_SHORT_MAX = 8


def _is_reaction_only(text: str) -> bool:
    """是否只由反应词和标点组成（"哈哈哈""嗯……啊""好耶"）"""
    rest = text
    for w in _REACTION_WORDS:
        rest = rest.replace(w, "")
    rest = "".join(ch for ch in rest if ch not in _REACTION_CHARS and ch not in _EDGE_PUNCT)
    return not rest.strip()


def _strip_tail_particle(core: str) -> str:
    """剥掉句尾语气词（连同其后的标点）"""
    core = core.rstrip(_EDGE_PUNCT)
    while core and core[-1] in _TONE_SUFFIXES:
        core = core[:-1].rstrip(_EDGE_PUNCT)
    return core


def _starts_with_reaction(core: str) -> bool:
    return bool(core) and (core[0] in _REACTION_CHARS or core.startswith(_REACTION_WORDS))


def _extract_filtered(bot_text: str, max_items: int) -> List[str]:
    """新抽取规则：纯反应词开头不收；反应词开头的短句不收；剥掉句尾语气词后不足 4 字不收。

    "语气词结尾句"不再有专门的收录分支：两条规则共用的入口统一剥掉句尾语气词再存
    （附录 §5.5 工程评审：只删 104-109 行没有效果）。
    """
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(bot_text) if s.strip()]
    candidates: List[str] = []
    for sent in sentences:
        if len(candidates) >= max_items:
            break
        m = re.match(r"^([^，。！？!?,]{1,12})[，,]", sent)
        if m:
            opening = m.group(1).strip()
            if _is_reaction_only(opening):
                # 去掉反应词开头，余下部分按普通句处理
                sent = sent[m.end():].strip()
            elif 2 <= len(opening) <= 12 and opening not in candidates:
                candidates.append(opening)
                continue
        core = sent.rstrip(_EDGE_PUNCT)
        if not core or _is_reaction_only(core):
            continue
        if _starts_with_reaction(core) and len(core) <= _REACTION_SHORT_MAX:
            continue
        plain = _strip_tail_particle(core)
        if 4 <= len(plain) <= 40 and plain not in candidates:
            candidates.append(plain)
    return candidates[:max_items]


def extract_expressions(
    bot_text: str, max_items: int = 2, *, legacy: bool = True
) -> List[str]:
    """从 bot 回复中提取口语化句式候选

    规则（``legacy=True``，旧行为）：
    - 开头称谓短句（第一个分句长度 ≤12 且以逗号结尾）；
    - 语气词结尾句（呀/啊/呢/吧/嘛/哦/哈）；
    - 长度 4-40 字符的完整短句；
    每条回复最多取 max_items 条。

    ``legacy=False``（P1-3，``learning_expression_tail_particle_rule=false``）见
    :func:`_extract_filtered`。

    Args:
        bot_text: bot 回复文本
        max_items: 最多提取条数
        legacy: 是否使用旧规则

    Returns:
        表达句式候选列表
    """
    if not bot_text:
        return []
    if not legacy:
        return _extract_filtered(bot_text, max_items)

    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(bot_text) if s.strip()]
    candidates: List[str] = []

    for sent in sentences:
        if len(candidates) >= max_items:
            break

        # 开头称谓短句："xxx，" 形式
        m = re.match(r"^([^，。！？!?,]{1,12})[，,]", sent)
        if m:
            opening = m.group(1).strip()
            if 2 <= len(opening) <= 12 and opening not in candidates:
                candidates.append(opening)
                continue

        # 语气词结尾句
        core = sent.rstrip("。！？!?~…")
        if core and core[-1] in _TONE_SUFFIXES and 4 <= len(core) <= 40:
            if core not in candidates:
                candidates.append(core)
                continue

        # 普通完整短句（剥离句末标点）
        plain = sent.rstrip("。！？!?~…")
        if 4 <= len(plain) <= 40 and plain not in candidates:
            candidates.append(plain)

    return candidates[:max_items]


# P1-3：注入轮换开启时，"未使用"淘汰窗口 = decay_days × 本系数（只在代码里）
ROTATION_DISUSE_FACTOR = 4


def decay(
    storage: LearningStorage, decay_days: int, max_count: int, *, rotation: bool = False
) -> int:
    """衰减淘汰表达模式（委托存储层）

    Args:
        storage: 学习存储实例
        decay_days: 衰减天数阈值
        max_count: 模式总量上限
        rotation: 注入是否为加权随机轮换（P1-3）；是则放宽"未使用"窗口

    Returns:
        删除的条数
    """
    if rotation:
        removed = storage.decay_patterns(
            decay_days, max_count, disuse_days=decay_days * ROTATION_DISUSE_FACTOR
        )
    else:
        removed = storage.decay_patterns(decay_days, max_count)
    stale = storage.cleanup_stale_pending(_STALE_PENDING_DAYS)
    if removed:
        logger.info(f"表达模式衰减淘汰 {removed} 条")
    if stale:
        logger.info(f"清理超龄待审学习条目 {stale} 条")
    return removed
