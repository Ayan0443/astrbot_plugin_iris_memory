"""continuity.db v3 迁移（活人感 P1 批次，ROADMAP §2.2）。

只新增表 / 索引 / 触发器，全部 ``IF NOT EXISTS``；不 DROP、不 ALTER 旧列。
语句来源就是各模块的建表常量（单一来源）；各模块的 ``ensure_*_schema`` 仍保留，
作为幂等兜底（例如测试里直接构造的库、或迁移被跳过的库）。

旧代码（SCHEMA_VERSION=2）打开 v3 库时 ``current >= SCHEMA_VERSION`` 直接跳过迁移，
不读这些表，所以回退代码不需要回退库。

| 模块 | 表 |
|---|---|
| P1-5 生活系统 | life_days / life_events / life_event_log / life_state / life_claims |
| P1-6 / P1-11 关系 | relationship_events / relationship_modes / relationship_digest / bond_transitions |
| P1-7 用户认知 / forget | user_beliefs / belief_history / tombstones |
| P1-13 群友认知 | member_profiles / member_aliases / member_notes |
| P1-8 主动候选池 | initiative_candidates / initiative_decisions |
| P1-12 关心桥 | care_signals / care_hyperbole |
"""

from __future__ import annotations

from typing import List

V3_TABLES = (
    "life_days",
    "life_events",
    "life_event_log",
    "life_state",
    "life_claims",
    "relationship_events",
    "relationship_modes",
    "relationship_digest",
    "bond_transitions",
    "user_beliefs",
    "belief_history",
    "tombstones",
    "member_profiles",
    "member_aliases",
    "member_notes",
    "initiative_candidates",
    "initiative_decisions",
    "care_signals",
    "care_hyperbole",
)


def _split_script(script: str) -> List[str]:
    """把只含 CREATE TABLE/INDEX 的脚本按 ';' 切开（这些脚本里没有触发器）。"""
    return [s.strip() for s in script.split(";") if s.strip()]


def statements() -> List[str]:
    """v3 全部建表语句（逐句执行；触发器语句整句保留，不按 ';' 切）。

    导入放在函数里：store.py 在迁移时才调用，避免模块加载期的循环导入。
    """
    from .care.store import CARE_DDL
    from .initiative.schema import INITIATIVE_DDL
    from .members.store import _SCHEMA as MEMBERS_SCHEMA
    from .memory.beliefs import _SCHEMA as BELIEFS_SCHEMA
    from .memory.tombstones import _SCHEMA as TOMBSTONES_SCHEMA
    from .relation.schema import RELATION_SCHEMA
    from .world.store import WORLD_SCHEMA

    out: List[str] = []
    out.extend(s.strip() for s in WORLD_SCHEMA)
    out.extend(_split_script(RELATION_SCHEMA))
    out.extend(s.strip() for s in TOMBSTONES_SCHEMA)
    out.extend(s.strip() for s in BELIEFS_SCHEMA)
    out.extend(s.strip() for s in MEMBERS_SCHEMA)
    out.extend(_split_script(INITIATIVE_DDL))
    out.extend(_split_script(CARE_DDL))
    for stmt in out:
        head = stmt.upper()
        if not (
            head.startswith("CREATE TABLE IF NOT EXISTS")
            or head.startswith("CREATE INDEX IF NOT EXISTS")
            or head.startswith("CREATE UNIQUE INDEX IF NOT EXISTS")
            or head.startswith("CREATE TRIGGER IF NOT EXISTS")
        ):
            raise RuntimeError("continuity v3 migration contains a non-additive statement")
    return out


__all__ = ["V3_TABLES", "statements"]
