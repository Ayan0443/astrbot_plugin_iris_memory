"""
角色身份：她的名字与主人的称呼（全部来自配置，代码里不写死任何名字）

- ``identity_bot_names``：她被叫的名字（逗号分隔，如 ``小白,xiaobai``）。用于"这句话是不是在说她"、
  "她是不是在用名字自称"之类的规则。
- ``identity_owner_names``：主人的称呼（逗号分隔，如 ``阿明,小明``）。用于回怼时识别"冲主人"、
  称呼规则提示、风格守卫等；``owner_display()`` 取第一个，未配置时为"主人"。

规则里的正则用占位符 ``{BOT}`` / ``{OWNER}`` 书写，经 :func:`lazy_re` / :class:`LazyRules`
在每次使用时按当前名字编译（按名字缓存）。名字为空时占位符替换为永不匹配的 ``(?!)``，
同一条规则里的其他分支照常生效。

名字来源：插件启动时 :func:`set_source` 注入配置读取函数（热修改即时生效）；
测试或离线工具可用 :func:`configure` 直接指定（优先于配置）。

可选的"参照角色"文件（``persona_reference_file``，JSON，相对路径相对插件数据目录）：
人设若以某部作品的角色为性格参照、但不许照搬，可在这里写该作品的识别正则与"禁止照搬"规则：

    {
      "work_mention": "作品名|别名",                     # 对方提到这部作品（正则）
      "copy_hard": [["REF-H01", "招牌口癖", false]],      # 出现即拦截：[规则 id, 正则, 忽略大小写]
      "copy_soft": [["REF-S01", "设定梗", true]],         # 软提示
      "reviewer_note": "尤其是某作品某角色的招牌问候。"     # 追加给学习审查的一句说明
    }

未配置或读取失败时，以上内容都为空，相关规则只剩通用部分。
"""

from __future__ import annotations

import json
import os
import re
import threading
from functools import lru_cache
from typing import Any, Callable, Iterable, Iterator, Optional, Sequence, Tuple

try:  # AstrBot 运行时
    from astrbot.api import logger
except Exception:  # pragma: no cover - 离线工具 / 无 AstrBot 环境
    import logging

    logger = logging.getLogger("iris_memory.identity")

BOT_KEY = "identity_bot_names"
OWNER_KEY = "identity_owner_names"
REFERENCE_KEY = "persona_reference_file"
DEFAULT_OWNER_DISPLAY = "主人"
_NEVER = "(?!)"

_lock = threading.Lock()
_getter: Optional[Callable[[str, Any], Any]] = None
_data_dir: Optional[str] = None
_override_reference: Optional[dict] = None
_override_bot: Optional[Tuple[str, ...]] = None
_override_owner: Optional[Tuple[str, ...]] = None


def split_names(raw: Any) -> Tuple[str, ...]:
    """逗号（含全角）分隔的字符串或列表 → 去空白、去重、保序的名字元组。"""
    if raw is None:
        return ()
    items: Iterable[Any]
    if isinstance(raw, str):
        items = raw.replace("，", ",").split(",")
    elif isinstance(raw, (list, tuple, set, frozenset)):
        items = raw
    else:
        items = str(raw).replace("，", ",").split(",")
    out: list[str] = []
    seen: set[str] = set()
    for x in items:
        s = str(x).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return tuple(out)


def set_source(getter: Optional[Callable[[str, Any], Any]], data_dir: Any = None) -> None:
    """注入配置读取函数 ``getter(key, default)`` 与插件数据目录；传 None 解除。"""
    global _getter, _data_dir
    with _lock:
        _getter = getter
        _data_dir = None if data_dir is None else str(data_dir)


def configure(bot_names: Any = None, owner_names: Any = None, reference: Optional[dict] = None) -> None:
    """直接指定名字 / 参照角色（优先于配置）；传 None 表示该项改回读配置。"""
    global _override_bot, _override_owner, _override_reference
    with _lock:
        _override_bot = None if bot_names is None else split_names(bot_names)
        _override_owner = None if owner_names is None else split_names(owner_names)
        _override_reference = None if reference is None else dict(reference)


def reset() -> None:
    """清除覆盖与配置来源（测试用）。"""
    global _getter, _data_dir, _override_bot, _override_owner, _override_reference
    with _lock:
        _getter = None
        _data_dir = None
        _override_bot = None
        _override_owner = None
        _override_reference = None


def _read(key: str) -> Tuple[str, ...]:
    g = _getter
    if g is None:
        return ()
    try:
        return split_names(g(key, ""))
    except Exception:
        return ()


def bot_names() -> Tuple[str, ...]:
    o = _override_bot
    return o if o is not None else _read(BOT_KEY)


def owner_names() -> Tuple[str, ...]:
    o = _override_owner
    return o if o is not None else _read(OWNER_KEY)


def setting(key: str, default: Any = "") -> Any:
    """读任意配置项（插件启动前或无配置来源时返回 default）。"""
    g = _getter
    if g is None:
        return default
    try:
        v = g(key, default)
    except Exception:
        return default
    return default if v is None else v


def owner_display(default: str = DEFAULT_OWNER_DISPLAY) -> str:
    names = owner_names()
    return names[0] if names else default


def owner_names_phrase(sep: str = " 或 ") -> str:
    """"阿明 或 小明"；未配置时返回空串。"""
    return sep.join(owner_names())


def _reference_path() -> Optional[str]:
    g = _getter
    if g is None:
        return None
    try:
        raw = str(g(REFERENCE_KEY, "") or "").strip()
    except Exception:
        return None
    if not raw:
        return None
    if not os.path.isabs(raw) and _data_dir:
        raw = os.path.join(_data_dir, raw)
    return raw


@lru_cache(maxsize=8)
def _load_reference(path: str, mtime: float) -> dict:
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"[identity] persona_reference_file unreadable: {type(e).__name__}")
        return {}


def reference() -> dict:
    """参照角色配置（见模块说明）；未配置 → {}。"""
    o = _override_reference
    if o is not None:
        return o
    path = _reference_path()
    if not path:
        return {}
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        logger.warning("[identity] persona_reference_file not found")
        return {}
    return _load_reference(path, mtime)


def reference_rows(kind: str) -> Tuple[Tuple[str, str, int], ...]:
    """``copy_hard`` / ``copy_soft`` → ((rule_id, pattern, flags), ...)；坏行跳过。"""
    rows = []
    for item in reference().get(kind) or ():
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            rid, pat = str(item[0]), str(item[1])
            ic = bool(item[2]) if len(item) > 2 else False
            re.compile(pat)
        except Exception:
            continue
        rows.append((rid, pat, re.IGNORECASE if ic else 0))
    return tuple(rows)


def reference_text(key: str) -> str:
    v = reference().get(key)
    return str(v).strip() if isinstance(v, str) else ""


def work_pattern() -> str:
    pat = reference_text("work_mention")
    if not pat:
        return ""
    try:
        re.compile(pat)
    except re.error:
        return ""
    return pat


def alternation(names: Sequence[str]) -> str:
    """名字 → 正则分支（长的在前，转义）；空 → 永不匹配。"""
    if not names:
        return _NEVER
    return "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))


def substitute(pattern: str, bots: Sequence[str], owners: Sequence[str], work: str = "") -> str:
    return (
        pattern.replace("{BOT}", "(?:" + alternation(bots) + ")")
        .replace("{OWNER}", "(?:" + alternation(owners) + ")")
        .replace("{WORK}", "(?:" + (work or _NEVER) + ")")
    )


@lru_cache(maxsize=512)
def _compile(pattern: str, flags: int, bots: Tuple[str, ...], owners: Tuple[str, ...], work: str = "") -> "re.Pattern[str]":
    return re.compile(substitute(pattern, bots, owners, work), flags)


def compile_now(pattern: str, flags: int = 0) -> "re.Pattern[str]":
    return _compile(pattern, flags, bot_names(), owner_names(), work_pattern() if "{WORK}" in pattern else "")


class LazyRegex:
    """行为同 ``re.Pattern``；每次使用时按当前名字取（缓存的）编译结果。"""

    __slots__ = ("_pattern", "_flags")

    def __init__(self, pattern: str, flags: int = 0) -> None:
        self._pattern = pattern
        self._flags = flags

    @property
    def compiled(self) -> "re.Pattern[str]":
        return compile_now(self._pattern, self._flags)

    @property
    def pattern(self) -> str:
        return self.compiled.pattern

    @property
    def flags(self) -> int:
        return self.compiled.flags

    def __getattr__(self, name: str) -> Any:
        return getattr(self.compiled, name)

    def __repr__(self) -> str:
        return f"LazyRegex({self._pattern!r})"


def lazy_re(pattern: str, flags: int = 0) -> LazyRegex:
    return LazyRegex(pattern, flags)


class LazyRules(Sequence):
    """规则表：行的第 ``pattern_index`` 列是含占位符的正则源串，使用时替换为编译结果。

    其余列原样保留，所以 ``for rid, rx, extra in rules`` 这类遍历写法不用改。
    """

    def __init__(self, rows: Sequence[Tuple[Any, ...]], *, pattern_index: int = 1, flags_index: Optional[int] = None,
                 default_flags: int = 0, drop_flags: bool = False, reference_kind: str = "") -> None:
        self._rows = tuple(rows)
        self._pi = pattern_index
        self._fi = flags_index
        self._df = default_flags
        self._drop_flags = drop_flags
        self._ref = reference_kind

    def _all_rows(self) -> Tuple[Tuple[Any, ...], ...]:
        return self._rows + (reference_rows(self._ref) if self._ref else ())

    def _build(self) -> Tuple[Tuple[Any, ...], ...]:
        return _build_rules(self._all_rows(), self._pi, self._fi, self._df, self._drop_flags, bot_names(), owner_names())

    def __iter__(self) -> Iterator[Tuple[Any, ...]]:
        return iter(self._build())

    def __len__(self) -> int:
        return len(self._all_rows())

    def __getitem__(self, i):  # type: ignore[override]
        return self._build()[i]

    def __add__(self, other: Any) -> Tuple[Tuple[Any, ...], ...]:
        return tuple(self) + tuple(other)

    def __radd__(self, other: Any) -> Tuple[Tuple[Any, ...], ...]:
        return tuple(other) + tuple(self)


@lru_cache(maxsize=64)
def _build_rules(rows, pi, fi, df, drop_flags, bots, owners):
    out = []
    for row in rows:
        flags = row[fi] if fi is not None else df
        rx = _compile(row[pi], flags, bots, owners)
        new = list(row)
        new[pi] = rx
        if fi is not None and drop_flags:
            del new[fi]
        out.append(tuple(new))
    return tuple(out)
