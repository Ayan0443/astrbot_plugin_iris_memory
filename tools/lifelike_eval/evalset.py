"""原创评测集加载与校验。

数据文件 ``data/evalset_v1.json``：S01–S30（口吻附录 §7.1）、P1–P12（认出测试探针）、
C1–C7（卖萌探针）以及设计 §12.3 的 E 系列原创样例子集。E5（主动回放）不在本文件，
由 ``snapshot --rows`` 导出的脱敏时间轴驱动。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_PATH = DATA_DIR / "evalset_v1.json"

SESSIONS = {"private", "group", "proactive"}
REGISTERS = {"strong", "medium", "light", "light_or_medium", "follow", "na"}
CUTE_TIERS = {"occasional", "rare", "none", "na"}
REQUIRED = ("id", "set", "session", "scene", "register", "cute_tier", "turns")


class EvalSetError(ValueError):
    pass


@dataclass(frozen=True)
class Case:
    id: str
    set: str
    session: str
    scene: str
    register: str
    cute_tier: str
    turns: tuple[str, ...]
    raw: dict[str, Any] = field(repr=False, compare=False, hash=False, default_factory=dict)

    @property
    def checks(self) -> dict[str, Any]:
        return dict(self.raw.get("checks") or {})

    @property
    def requires(self) -> str:
        return str(self.raw.get("requires") or "")


def _turn_text(t: Any) -> str:
    if isinstance(t, str):
        return t
    if isinstance(t, dict) and isinstance(t.get("text"), str):
        return t["text"]
    raise EvalSetError("turn must be str or {text: str}")


def validate(data: dict[str, Any]) -> list[Case]:
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise EvalSetError("missing cases")
    seen: set[str] = set()
    out: list[Case] = []
    for raw in data["cases"]:
        for k in REQUIRED:
            if k not in raw:
                raise EvalSetError(f"case missing field {k}")
        cid = str(raw["id"])
        if cid in seen:
            raise EvalSetError(f"duplicate id {cid}")
        seen.add(cid)
        if raw["session"] not in SESSIONS:
            raise EvalSetError(f"{cid}: bad session")
        if raw["register"] not in REGISTERS:
            raise EvalSetError(f"{cid}: bad register")
        if raw["cute_tier"] not in CUTE_TIERS:
            raise EvalSetError(f"{cid}: bad cute_tier")
        if not isinstance(raw["turns"], list):
            raise EvalSetError(f"{cid}: turns must be a list")
        turns = tuple(_turn_text(t) for t in raw["turns"])
        if not turns and raw["session"] != "proactive":
            raise EvalSetError(f"{cid}: only proactive cases may have no turns")
        out.append(
            Case(
                id=cid,
                set=str(raw["set"]),
                session=raw["session"],
                scene=str(raw["scene"]),
                register=raw["register"],
                cute_tier=raw["cute_tier"],
                turns=turns,
                raw=dict(raw),
            )
        )
    return out


def load(path: Path | str = DEFAULT_PATH) -> list[Case]:
    with open(path, encoding="utf-8") as f:
        return validate(json.load(f))


def by_set(cases: list[Case], *names: str) -> list[Case]:
    want = set(names)
    return [c for c in cases if c.set in want]
