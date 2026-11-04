"""continuity.db v3 之后新表总是由迁移建好：'off 不建表' 的断言改为 'off 不写任何行'。"""

from __future__ import annotations

from typing import Any, Iterable, Set


def written_tables(store: Any, names: Iterable[str]) -> Set[str]:
    """names 中存在且至少有一行的表（不存在的表视为未写入）。"""
    out: Set[str] = set()
    with store._lock:
        conn = store._conn()
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for name in names:
            if name in existing and conn.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone():
                out.add(name)
    return out


def life_tables(store: Any) -> Set[str]:
    with store._lock:
        return {
            r[0]
            for r in store._conn().execute("SELECT name FROM sqlite_master WHERE type='table'")
            if str(r[0]).startswith("life_")
        }
