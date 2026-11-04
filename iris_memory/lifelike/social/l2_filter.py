"""私聊 L2 检索收口（隐私安全审查 R1，设计 §6.8.2"既有旁路"第 1 条）。

现网私聊检索不按群过滤：私聊会召回任何群的 L2 记忆（含未授权群里第三方的事）。
开关 ``lifelike_private_l2_filter``（默认 true）打开时，私聊注入的 L2 结果：

- 私聊来源（``group_id`` 为空）的记忆：照常保留；
- 群来源的记忆：只保留**授权群**（``lifelike_private_l2_groups``，为空时取
  ``lifelike_member_groups``），并且过 ``PrivacyScrubber``（敏感类别整条丢弃）；
- ``scope=global`` 的记忆：保留，但同样过敏感过滤。

群聊路径不受影响。异常 → 原样返回（旧行为）并记日志。日志只记条数。
"""

from __future__ import annotations

from typing import Any, Iterable, List, Sequence, Set, Tuple

from . import privacy

__all__ = [
    "parse_chat_ids",
    "chat_variants",
    "filter_private_l2",
    "filter_private_graph",
    "private_filter_enabled",
    "allowed_groups_from_config",
    "split_group_tokens",
    "whitelist_group_ids",
    "set_group_resolver",
    "WILDCARD_ALL",
    "WILDCARD_WHITELIST",
    "KEY_ENABLE",
    "KEY_GROUPS",
]

KEY_ENABLE = "lifelike_private_l2_filter"
KEY_GROUPS = "lifelike_private_l2_groups"
KEY_MEMBER_GROUPS = "lifelike_member_groups"

# 群配置里的通配写法（U3）："*" = continuity.observe_chat_ids 中的全部群；
# "*whitelist" = 平台 platform_settings.id_whitelist 里的全部群（动态读取）
WILDCARD_ALL = "*"
WILDCARD_WHITELIST = "*whitelist"
_WILDCARDS = frozenset({WILDCARD_ALL, WILDCARD_WHITELIST})

# 通配解析器：由 LifelikeMemory 注册（value → 授权群 id 列表）；未注册时通配只保留显式 id
_group_resolver: Any = None


def parse_chat_ids(value: Any) -> List[str]:
    """"-100123, -100456" / 列表 → 字符串 id 列表（去重保序）"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items: Iterable[Any] = value
    else:
        items = str(value).replace("，", ",").replace(";", ",").replace(" ", ",").split(",")
    out: List[str] = []
    for it in items:
        s = str(it).strip()
        if s and s not in out:
            out.append(s)
    return out


def split_group_tokens(value: Any) -> Tuple[bool, bool, List[str]]:
    """群配置 → (含 "*", 含 "*whitelist", 显式 id 列表)。大小写不敏感。"""
    all_flag = wl_flag = False
    explicit: List[str] = []
    for g in parse_chat_ids(value):
        low = g.lower()
        if low == WILDCARD_ALL:
            all_flag = True
        elif low == WILDCARD_WHITELIST:
            wl_flag = True
        else:
            explicit.append(g)
    return all_flag, wl_flag, explicit


def whitelist_group_ids(whitelist: Any, exclude: Iterable[Any] = ()) -> List[int]:
    """平台 id_whitelist 里的群/频道 id（负数；去掉 ``#话题`` 后缀），排除私聊 id（正数）与 exclude 中的任何写法。

    whitelist 为 None（白名单未启用或为空 = 全部允许）→ []：通配白名单时不据此扩大范围。
    """
    if not whitelist:
        return []
    blocked: Set[str] = set()
    for x in exclude or ():
        blocked |= chat_variants(str(x).split("#", 1)[0])
    out: List[int] = []
    for raw in whitelist:
        s = str(raw or "").strip().split("#", 1)[0].strip()
        try:
            n = int(s)
        except (TypeError, ValueError):
            continue
        if n >= 0 or (chat_variants(s) & blocked) or n in out:
            continue
        out.append(n)
    return out


def set_group_resolver(fn: Any) -> None:
    """注册通配解析器 ``fn(value) -> Sequence[id]``（None = 取消）"""
    global _group_resolver
    _group_resolver = fn


def chat_variants(chat_id: Any) -> Set[str]:
    """Telegram 群 id 的等价写法：-100xxx / -xxx / xxx"""
    s = str(chat_id or "").strip()
    if not s:
        return set()
    out = {s}
    bare = s
    if bare.startswith("-100"):
        bare = bare[4:]
    elif bare.startswith("-"):
        bare = bare[1:]
    if bare:
        out.update({bare, f"-{bare}", f"-100{bare}"})
    return out


def _allowed_set(groups: Sequence[Any]) -> Set[str]:
    allowed: Set[str] = set()
    for g in groups:
        allowed |= chat_variants(g)
    return allowed


def filter_private_l2(results: Sequence[Any], allowed_groups: Sequence[Any]) -> Tuple[List[Any], dict]:
    """返回 (保留结果, 统计)。results 元素需有 ``.entry.metadata`` / ``.entry.content``。"""
    allowed = _allowed_set(allowed_groups)
    kept: List[Any] = []
    stats = {"unauthorized": 0, "sensitive": 0, "kept": 0}
    for r in results:
        entry = getattr(r, "entry", None)
        meta = getattr(entry, "metadata", None) or {}
        content = str(getattr(entry, "content", "") or "")
        gid = str(meta.get("group_id") or "").strip()
        is_global = meta.get("scope") == "global"
        if not gid and not is_global:
            kept.append(r)  # 私聊来源
            continue
        if gid and not is_global and gid not in allowed:
            stats["unauthorized"] += 1
            continue
        if privacy.is_sensitive(content):
            stats["sensitive"] += 1
            continue
        kept.append(r)
    stats["kept"] = len(kept)
    return kept, stats


def private_filter_enabled(config: Any) -> bool:
    """R1 开关（默认 true；字符串 "false/off/0/no" 视为关）。读取异常 → True（隐私优先）。"""
    try:
        enabled = config.get(KEY_ENABLE, True)
    except Exception:
        return True
    if isinstance(enabled, str):
        return enabled.strip().lower() not in ("0", "false", "off", "no")
    return enabled is not False


def allowed_groups_from_config(config: Any) -> List[str]:
    """授权群：``lifelike_private_l2_groups``，为空时取 ``lifelike_member_groups``"""
    raw = config.get(KEY_GROUPS, "")
    groups = parse_chat_ids(raw)
    if not groups:
        raw = config.get(KEY_MEMBER_GROUPS, "")
        groups = parse_chat_ids(raw)
    if not any(g.lower() in _WILDCARDS for g in groups):
        return groups
    # 通配：显式 id + 解析器给出的授权群；解析失败只保留显式 id（隐私优先，宁可少注入）
    out = [g for g in groups if g.lower() not in _WILDCARDS]
    resolver = _group_resolver
    if resolver is not None:
        try:
            for g in resolver(raw) or ():
                s = str(g)
                if s not in out:
                    out.append(s)
        except Exception:
            pass
    return out


def _node_groups(node: Any) -> Set[str]:
    out: Set[str] = set()
    if not isinstance(node, dict):
        return out
    gid = str(node.get("group_id") or "").strip()
    if gid:
        out.add(gid)
    props = node.get("properties")
    if isinstance(props, str):
        try:
            import json

            props = json.loads(props)
        except Exception:
            props = {}
    if isinstance(props, dict):
        raw = props.get("group_ids") or ""
        if isinstance(raw, (list, tuple, set)):
            items: Iterable[Any] = raw
        else:
            items = str(raw).split(",")
        out.update(s for s in (str(x).strip() for x in items) if s)
    return out


def filter_private_graph(
    nodes: Sequence[Any], edges: Sequence[Any], allowed_groups: Sequence[Any]
) -> Tuple[List[Any], List[Any], dict]:
    """私聊 L3 图谱收口（R1 延伸）：节点的来源群（``group_id`` ∪ ``properties.group_ids``）
    只要有一个不在授权群里就丢弃（宁可少注入）；群来源节点再过敏感过滤；
    端点被丢弃的边一起丢弃。返回 (节点, 边, 统计)。"""
    allowed = _allowed_set(allowed_groups)
    kept_nodes: List[Any] = []
    kept_ids: Set[str] = set()
    stats = {"unauthorized": 0, "sensitive": 0, "kept": 0, "edges_dropped": 0}
    for n in nodes:
        groups = _node_groups(n)
        if groups and not groups <= allowed:
            stats["unauthorized"] += 1
            continue
        if groups:
            text = " ".join(str(n.get(k) or "") for k in ("name", "content")) if isinstance(n, dict) else ""
            if privacy.is_sensitive(text):
                stats["sensitive"] += 1
                continue
        kept_nodes.append(n)
        nid = n.get("id") if isinstance(n, dict) else None
        if nid:
            kept_ids.add(str(nid))
    def _end(e: Any, key: str) -> str:
        v = e.get(key) if isinstance(e, dict) else None
        if isinstance(v, dict):
            v = v.get("id")
        return str(v or "")

    kept_edges: List[Any] = []
    for e in edges:
        src = _end(e, "source")
        dst = _end(e, "target")
        if src in kept_ids and dst in kept_ids:
            kept_edges.append(e)
        else:
            stats["edges_dropped"] += 1
    stats["kept"] = len(kept_nodes)
    return kept_nodes, kept_edges, stats
