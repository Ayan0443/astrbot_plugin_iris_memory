"""
Iris Chat Memory - 学习上下文注入组装

从学习库中取本群已通过的表达模式、消息命中的暗语解释、
few-shot 对话样例，组装 `## 群聊用语与表达风格` 分小节文本：
- 单条条目截断 learning_inject_max_item_chars（默认 200）；
- 整体超 learning_inject_max_tokens（默认 600）按
  few_shot → pattern → jargon 顺序裁剪；
- 无内容返回 ""。
"""

import math
import random
from collections import OrderedDict, deque
from typing import Any, Deque, Dict, List, Optional, Sequence, Set, Tuple, TYPE_CHECKING

from ..config import get_config
from ..core import get_logger
from ..platform import get_adapter
from ..utils.token_counter import count_tokens
from .storage import LearningStorage
from .jargon import JargonLearner

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

logger = get_logger("learning.injector")

_SECTION_HEADER = "## 群聊用语与表达风格"

# ---------------------------------------------------------------------------
# P1-3 反自我强化（口吻附录 §5.5）：表达模式加权随机 + 冷却；few-shot 按场景过滤 + 冷却。
# 冷却只在进程内存里（按 群/会话 + persona），重启清空；参数只在代码里。
# ---------------------------------------------------------------------------
_PATTERN_POOL_MIN = 60
_PATTERN_POOL_MAX = 200
_PATTERN_COOLDOWN_ROUNDS = 3
_FEW_SHOT_POOL_MIN = 20
_FEW_SHOT_COOLDOWN_ROUNDS = 2
_TAIL_PARTICLE_QUOTA = 1  # 语气词结尾的模式每轮最多几条
_ROTATION_MAX_KEYS = 256
_TONE_TAILS = ("呀", "啊", "呢", "吧", "嘛", "哦", "哈", "啦", "哩")
_EDGE_PUNCT = "。！？!?~～…．.，,、；; \t\r\n"

_rng = random.Random()
_pattern_rounds: "OrderedDict[Tuple[str, str], Deque[Set[int]]]" = OrderedDict()
_few_shot_rounds: "OrderedDict[Tuple[str, str], Deque[Set[int]]]" = OrderedDict()


def reset_rotation_state(seed: Optional[int] = None) -> None:
    """清空冷却记录（测试 / 重载用）；可选重设随机种子。"""
    _pattern_rounds.clear()
    _few_shot_rounds.clear()
    if seed is not None:
        _rng.seed(seed)


def _rounds_for(
    store: "OrderedDict[Tuple[str, str], Deque[Set[int]]]", key: Tuple[str, str], maxlen: int
) -> Deque[Set[int]]:
    dq = store.get(key)
    if dq is None:
        dq = deque(maxlen=maxlen)
        store[key] = dq
        while len(store) > _ROTATION_MAX_KEYS:
            store.popitem(last=False)
    else:
        store.move_to_end(key)
    return dq


def _cooled_ids(rounds: Deque[Set[int]], effective: int) -> Set[int]:
    if effective <= 0:
        return set()
    out: Set[int] = set()
    for ids in list(rounds)[-effective:]:
        out |= ids
    return out


def _is_reaction_only(text: str) -> bool:
    from .expression import _is_reaction_only as impl

    return impl(text or "")


def _ends_with_tone(text: str) -> bool:
    core = (text or "").rstrip(_EDGE_PUNCT)
    return bool(core) and core[-1] in _TONE_TAILS


def _weighted_sample(items: Sequence[Dict[str, Any]], k: int) -> List[Dict[str, Any]]:
    """不放回加权抽样（Efraimidis-Spirakis）；命中越多权重越低，抑制"越注入越靠前"。"""
    keyed = []
    for item in items:
        hits = max(0, int(item.get("hit_count") or 0))
        weight = 1.0 / (1.0 + math.log1p(hits))
        u = _rng.random() or 1e-12
        keyed.append((u ** (1.0 / weight), item))
    keyed.sort(key=lambda kv: kv[0], reverse=True)
    return [item for _key, item in keyed[:k]]


def _rotate_patterns(
    storage: LearningStorage, group_id: str, persona_id: str, top_n: int
) -> List[Dict[str, Any]]:
    pool_size = min(_PATTERN_POOL_MAX, max(top_n * 12, _PATTERN_POOL_MIN))
    pool = [
        p for p in storage.get_approved_patterns(group_id, pool_size, persona_id)
        if not _is_reaction_only(str(p.get("expression") or ""))
    ]
    rounds = _rounds_for(_pattern_rounds, (group_id, persona_id), _PATTERN_COOLDOWN_ROUNDS)
    # 池子太小时自动缩短冷却，保证每轮仍有 top_n 条可选
    effective = min(_PATTERN_COOLDOWN_ROUNDS, max(0, len(pool) // max(1, top_n) - 1))
    cooled = _cooled_ids(rounds, effective)
    candidates = [p for p in pool if int(p["id"]) not in cooled]
    picked: List[Dict[str, Any]] = []
    tone_used = 0
    for item in _weighted_sample(candidates, len(candidates)):
        if len(picked) >= top_n:
            break
        if _ends_with_tone(str(item.get("expression") or "")):
            if tone_used >= _TAIL_PARTICLE_QUOTA:
                continue
            tone_used += 1
        picked.append(item)
    rounds.append({int(p["id"]) for p in picked})
    return picked


def _rotate_few_shots(
    storage: LearningStorage,
    group_id: str,
    persona_id: str,
    few_shot_max: int,
    user_text: str,
    is_group: bool,
) -> List[Dict[str, Any]]:
    from ..lifelike.style.guard import CUTE
    from ..style import register as R

    pool = storage.get_approved_few_shots(
        group_id, max(_FEW_SHOT_POOL_MIN, few_shot_max * 6), persona_id
    )
    current = R.judge_scene(user_text, is_group=is_group)
    same: List[Dict[str, Any]] = []
    other: List[Dict[str, Any]] = []
    for fs in pool:
        judged = R.judge_scene(str(fs.get("user_text") or ""), is_group=is_group)
        if judged.register > current.register:
            continue  # 淡档场景不注入浓档样例
        if current.cute == R.CUTE_OFF and any(
            pat.search(str(fs.get("bot_text") or "")) for _rid, pat in CUTE
        ):
            continue  # 不卖萌场景不注入卖萌样例
        (same if judged.scene == current.scene else other).append(fs)
    rounds = _rounds_for(_few_shot_rounds, (group_id, persona_id), _FEW_SHOT_COOLDOWN_ROUNDS)
    total = len(same) + len(other)
    effective = min(_FEW_SHOT_COOLDOWN_ROUNDS, max(0, total // max(1, few_shot_max) - 1))
    cooled = _cooled_ids(rounds, effective)
    same = [fs for fs in same if int(fs["id"]) not in cooled]
    other = [fs for fs in other if int(fs["id"]) not in cooled]
    _rng.shuffle(same)
    _rng.shuffle(other)
    picked = (same + other)[:few_shot_max]
    rounds.append({int(fs["id"]) for fs in picked})
    # 展示顺序保持"最新优先"
    order = {int(fs["id"]): i for i, fs in enumerate(pool)}
    picked.sort(key=lambda fs: order.get(int(fs["id"]), 0))
    return picked


def _flag(config: Any, key: str) -> bool:
    try:
        return config.get(key, False) is True
    except Exception:
        return False


def _truncate(text: str, limit: int) -> str:
    """单条条目按字符上限截断"""
    text = (text or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "…"


async def build_learning_context(
    event: "AstrMessageEvent",
    storage: LearningStorage,
    jargon: JargonLearner,
    meta: Optional[Dict[str, Any]] = None,
    persona_id: str = "default",
) -> str:
    """组装学习注入文本

    Args:
        event: AstrBot 消息事件
        storage: 学习存储实例
        jargon: 暗语学习器实例
        meta: 运行日志元信息字典（填充 counts/budget，照 llm_request_hook 风格）

    Returns:
        组装好的注入文本；无内容返回 ""
    """
    config = get_config()
    adapter = get_adapter(event)
    session_id = adapter.get_session_id(event)
    group_id = adapter.get_group_id(event) or session_id

    top_n = config.get_int("learning_pattern_top_n", 5) or 5
    few_shot_max = config.get_int("learning_few_shot_max", 3) or 3
    max_item_chars = config.get_int("learning_inject_max_item_chars", 200) or 200
    max_tokens = config.get_int("learning_inject_max_tokens", 600) or 600

    user_text = getattr(event, "message_str", "") or ""

    # 1. 已通过的表达模式：P1-3 开启时加权随机 + 冷却，否则 top-N（按命中数）；都记录命中
    if _flag(config, "learning_pattern_rotation"):
        try:
            patterns = _rotate_patterns(storage, group_id, persona_id, top_n)
        except Exception as e:
            logger.warning(f"[lifelike.learning_rotation] pattern fallback: {type(e).__name__}")
            patterns = storage.get_approved_patterns(group_id, top_n, persona_id)
    else:
        patterns = storage.get_approved_patterns(group_id, top_n, persona_id)
    if patterns:
        storage.record_pattern_hit([int(p["id"]) for p in patterns])

    # 2. 用户消息命中的已推断暗语（最多 5 条）
    jargon_hits = jargon.match_terms(group_id, user_text, max_items=5)

    # 3. 已通过的 few-shot 对话样例：P1-3 开启时按本轮场景过滤 + 冷却，否则最近 N 条
    if _flag(config, "learning_few_shot_scene_filter"):
        try:
            is_group = bool(adapter.get_group_id(event))
            few_shots = _rotate_few_shots(
                storage, group_id, persona_id, few_shot_max, user_text, is_group
            )
        except Exception as e:
            logger.warning(f"[lifelike.learning_rotation] few-shot fallback: {type(e).__name__}")
            few_shots = storage.get_approved_few_shots(group_id, few_shot_max, persona_id)
    else:
        few_shots = storage.get_approved_few_shots(group_id, few_shot_max, persona_id)

    if not patterns and not jargon_hits and not few_shots:
        if meta is not None:
            meta["skipped"] = "empty"
        return ""

    few_shot_lines: List[str] = []
    for fs in few_shots:
        few_shot_lines.append(
            f"- 用户：{_truncate(fs['user_text'], max_item_chars)}\n"
            f"  回复：{_truncate(fs['bot_text'], max_item_chars)}"
        )
    pattern_lines = [
        f"- [{p['scene']}] {_truncate(p['expression'], max_item_chars)}"
        for p in patterns
    ]
    jargon_lines = [
        f"- {_truncate(j['term'], 20)} = {_truncate(j['meaning'], max_item_chars)}"
        for j in jargon_hits
    ]

    def assemble(
        fs: List[str], pt: List[str], jg: List[str]
    ) -> str:
        """按分小节组装完整文本"""
        parts = [_SECTION_HEADER]
        if jg:
            parts.append("### 圈内暗语\n" + "\n".join(jg))
        if pt:
            parts.append("### 常用表达\n" + "\n".join(pt))
        if fs:
            parts.append("### 对话风格样例\n" + "\n".join(fs))
        return "\n".join(parts)

    # 整体 token 预算裁剪：按 few_shot → pattern → jargon 顺序丢弃
    dropped = 0
    while True:
        text = assemble(few_shot_lines, pattern_lines, jargon_lines)
        if count_tokens(text) <= max_tokens:
            break
        if few_shot_lines:
            few_shot_lines.pop()
        elif pattern_lines:
            pattern_lines.pop()
        elif jargon_lines:
            jargon_lines.pop()
        else:
            break
        dropped += 1

    if not few_shot_lines and not pattern_lines and not jargon_lines:
        if meta is not None:
            meta["skipped"] = "budget_exceeded"
        return ""

    if meta is not None:
        meta["pattern_count"] = len(pattern_lines)
        meta["jargon_count"] = len(jargon_lines)
        meta["few_shot_count"] = len(few_shot_lines)
        meta["dropped_by_budget"] = dropped
        meta["budget_tokens"] = max_tokens
        meta["used_tokens"] = count_tokens(text)
        meta["persona_id"] = persona_id

    return text
