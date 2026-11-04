"""风格示例库加载与校验（ROADMAP P1-2，口吻附录 §5.2）。

- 种子库在代码目录 ``iris_memory/style/style_examples_v1.json``，跟代码一起评审、回滚；
- ``style_examples_path`` 非空时改读该路径（同样的 JSON 结构）；读失败回退到种子库；
- 每条 ``styled`` 加载时过一遍 P0-6 ``StyleGuard`` 的阻断类规则（黑名单 / 密度、
  操纵句、重新扮演、§12.4 硬拦截与软提示），命中的条目直接丢弃，不能用违规示例教模型；
  ``anti`` 只检查 §12.4 照搬清单（反例本身就是"别这样"，但不能借反例把作品词带进 prompt）；
- 按 (路径, mtime) 缓存；任何异常只记日志（不含正文）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core import get_logger
from ..lifelike.style import guard
from . import register as R

logger = get_logger("style.examples")

SEED_PATH = Path(__file__).resolve().parent / "style_examples_v1.json"

CHANNELS = ("private", "group", "proactive")
_BLOCKING_GROUPS = frozenset({
    guard.GROUP_BL, guard.GROUP_MAN, guard.GROUP_ROLE,
    guard.GROUP_COPY_HARD, guard.GROUP_COPY_SOFT,
})
_MAX_FIELD_CHARS = 120


@dataclass(frozen=True)
class StyleExample:
    id: str
    scene: str
    channels: Tuple[str, ...]
    register: int
    user: str
    styled: str
    anti: str = ""
    anti_reason: str = ""
    cute: bool = False


def blocking_hits(styled: str, *, technical: bool = False) -> List[str]:
    """示例正文命中的阻断类规则 id（"跟随"豁免的不算）"""
    hits = guard.scan(styled, {"technical": technical})
    return [h.rule_id for h in hits if h.group in _BLOCKING_GROUPS and not h.exempt]


def copy_hits(text: str) -> List[str]:
    """§12.4 照搬清单（硬拦截 + 软提示）命中"""
    return [rid for rid, pat in guard.COPY_HARD + guard.COPY_SOFT if pat.search(text or "")]


def _parse_entry(raw: Dict[str, Any]) -> Optional[StyleExample]:
    if not isinstance(raw, dict):
        return None
    eid = str(raw.get("id") or "").strip()
    scene = str(raw.get("scene") or "").strip()
    user = str(raw.get("user") or "").strip()
    styled = str(raw.get("styled") or "").strip()
    reg = R.parse_register(raw.get("register"), default=R.REG_OFF)
    channels_raw = raw.get("channel") or []
    if isinstance(channels_raw, str):
        channels_raw = [channels_raw]
    channels = tuple(c for c in (str(x).strip() for x in channels_raw) if c in CHANNELS)
    if not eid or not scene or not user or not styled or reg == R.REG_OFF or not channels:
        return None
    if raw.get("reviewed") is not True:
        return None
    if len(user) > _MAX_FIELD_CHARS or len(styled) > _MAX_FIELD_CHARS:
        return None
    anti = str(raw.get("anti") or "").strip()
    anti_reason = str(raw.get("anti_reason") or "").strip()
    if anti and (len(anti) > _MAX_FIELD_CHARS or copy_hits(anti)):
        anti, anti_reason = "", ""
    return StyleExample(
        id=eid,
        scene=scene,
        channels=channels,
        register=reg,
        user=user,
        styled=styled,
        anti=anti,
        anti_reason=anti_reason[:40],
        cute=bool(raw.get("cute")),
    )


def parse_library(data: Any) -> Tuple[List[StyleExample], int]:
    """解析 + 校验；返回 (可用条目, 丢弃条数)"""
    if not isinstance(data, dict) or not isinstance(data.get("examples"), list):
        raise ValueError("style examples: missing 'examples' list")
    out: List[StyleExample] = []
    seen: set = set()
    dropped = 0
    for raw in data["examples"]:
        ex = _parse_entry(raw)
        if ex is None or ex.id in seen:
            dropped += 1
            continue
        if blocking_hits(ex.styled, technical=ex.scene == R.SCENE_HELP):
            dropped += 1
            continue
        seen.add(ex.id)
        out.append(ex)
    return out, dropped


_cache: Dict[str, Tuple[float, List[StyleExample]]] = {}


def _load_file(path: Path) -> List[StyleExample]:
    key = str(path)
    mtime = os.path.getmtime(path)
    cached = _cache.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    examples, dropped = parse_library(data)
    if dropped:
        logger.warning(f"[lifelike.style_examples] dropped={dropped} kept={len(examples)} source={'seed' if path == SEED_PATH else 'override'}")
    _cache[key] = (mtime, examples)
    return examples


def load_examples(override_path: str = "") -> List[StyleExample]:
    """读示例库：覆盖路径优先，失败回退种子库；种子库也读不到时返回空表。"""
    override = (override_path or "").strip()
    if override:
        try:
            return _load_file(Path(override).expanduser())
        except Exception as e:
            logger.warning(f"[lifelike.style_examples] override unreadable, fallback to seed: {type(e).__name__}")
    try:
        return _load_file(SEED_PATH)
    except Exception as e:
        logger.warning(f"[lifelike.style_examples] seed unreadable: {type(e).__name__}")
        return []


def clear_cache() -> None:
    _cache.clear()
