"""只读快照导出 + 基线采集（P0-0，ROADMAP §7）。

安全约束（全部由代码断言，不靠自觉）：
- 只用 ``file:<path>?mode=ro`` URI 打开 SQLite，并设 ``PRAGMA query_only=ON``；
- 路径必须落在允许的数据根目录之下（白名单），且文件必须已存在（不会意外新建库）；
- 正文只在内存里参与规则计算，输出只含聚合数字、规则 id、单字标记名；
- 输出前自动断言：任何字符串值都不含连续 ≥8 个汉字、不含 ≥7 位数字串（防 id / 原文外泄）；
- 输出文件权限 600，且拒绝写进生产数据目录（``<data_root>/dev`` 下的开发目录除外）。

用法（容器内，插件根目录下）::

    python -m tools.lifelike_eval.snapshot --data-root /AstrBot/data --stdout > baseline.json
    python -m tools.lifelike_eval.snapshot --data-root /AstrBot/data --out /path/baseline.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from . import metrics as M

TOOL_VERSION = "lifelike_snapshot/1"
TZ = ZoneInfo("Asia/Shanghai")

PLUGIN_DATA = Path("plugin_data") / "astrbot_plugin_iris_memory"
DB_PATHS: dict[str, Path] = {
    "continuity": PLUGIN_DATA / "continuity" / "continuity.db",
    "learning": PLUGIN_DATA / "learning" / "learning.db",
    "persona_evolution": PLUGIN_DATA / "persona_evolution" / "persona_evolution.db",
    "main": Path("data_v4.db"),
}

_FORBIDDEN_STR_DIGITS = re.compile(r"\d{7,}")
NO_TEXT_CJK_RUN = 8


class SnapshotSafetyError(RuntimeError):
    """路径越界、非只读打开、输出疑似含原文等。"""


# ---------------------------------------------------------------- 只读打开


def ro_uri(path: Path) -> str:
    return f"file:{quote(str(path))}?mode=ro"


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def open_ro(path: Path | str, allowed_roots: list[Path | str]) -> sqlite3.Connection:
    """以只读方式打开白名单内已存在的 SQLite 文件。"""
    p = Path(path).resolve()
    roots = [Path(r).resolve() for r in allowed_roots]
    if not any(_under(p, r) for r in roots):
        raise SnapshotSafetyError("path outside allowed roots")
    if not p.is_file():
        raise SnapshotSafetyError("database file missing")
    uri = ro_uri(p)
    if not uri.endswith("?mode=ro"):
        raise SnapshotSafetyError("non read-only uri")
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _tables(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in _tables(conn):
        if t.startswith("sqlite_"):
            continue
        out[t] = int(conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
    return out


def _day(ts: float | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), TZ).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return None


def _channel(scope_key: str) -> str:
    return "private" if str(scope_key).startswith("private:") else "group"


def _dist(rows: list[tuple[Any, ...]]) -> dict[str, int]:
    """[(key..., count)] → {"k1|k2": count}，键只用于枚举值（状态、来源等 ASCII 枚举）。"""
    out: dict[str, int] = {}
    for *keys, n in rows:
        out["|".join(str(k) for k in keys)] = int(n)
    return out


# ---------------------------------------------------------------- continuity.db


def _bot_turns(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """已发送的 bot 出站按 turn 合并（多段按 part_index 拼接）。"""
    rows = conn.execute(
        "SELECT id, scope_key, COALESCE(turn_id,''), part_index, text, origin, created_at "
        "FROM outbound WHERE author='bot' AND status='sent' ORDER BY created_at, id"
    ).fetchall()
    turns: dict[tuple[str, str], dict[str, Any]] = {}
    for oid, scope, turn_id, part, text, origin, created in rows:
        key = (scope, turn_id or f"#{oid}")
        t = turns.setdefault(
            key, {"scope": scope, "origin": origin, "ts": float(created), "parts": [], "id": int(oid)}
        )
        t["parts"].append((int(part or 0), text or ""))
    out = []
    for t in turns.values():
        t["text"] = "\n".join(p[1] for p in sorted(t["parts"]))
        del t["parts"]
        out.append(t)
    out.sort(key=lambda t: (t["ts"], t["id"]))
    return out


def _user_ctx_for(turns: list[dict[str, Any]], inbound: list[tuple[str, float, str]]) -> list[dict[str, Any]]:
    """私聊回复配上之前最近 3 条对方消息（只在内存里用于豁免判断）。"""
    by_scope: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for scope, ts, text in inbound:
        by_scope[scope].append((float(ts), text or ""))
    for v in by_scope.values():
        v.sort()
    ctxs = []
    for t in turns:
        prev = [x[1] for x in by_scope.get(t["scope"], []) if x[0] <= t["ts"]][-3:]
        ctxs.append(
            {
                "user_text": prev[-1] if prev else "",
                "user_texts": prev,
                "session": _channel(t["scope"]),
            }
        )
    return ctxs


def collect_continuity(conn: sqlite3.Connection) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    out: dict[str, Any] = {"tables": _table_counts(conn)}
    tables = set(out["tables"])
    rows_export: list[dict[str, Any]] = []

    inbound: list[tuple[str, float, str]] = []
    if "inbound" in tables:
        inbound = [(s, float(ts), t) for s, ts, t in conn.execute("SELECT scope_key, msg_ts, text FROM inbound")]
        status = conn.execute("SELECT scope_key, extract_status FROM inbound").fetchall()
        inb: dict[str, Any] = {}
        for ch in ("private", "group"):
            sel = [(s, ts, t) for s, ts, t in inbound if _channel(s) == ch]
            st = Counter(es for s, es in status if _channel(s) == ch)
            lens = [len((t or "").strip()) for _, _, t in sel]
            inb[ch] = {
                "n": len(sel),
                "extract_status": dict(sorted(st.items())),
                "len_mean": round(sum(lens) / len(lens), 2) if lens else None,
                "days_covered": len({_day(ts) for _, ts, _ in sel}),
            }
        out["inbound"] = inb
        for s, ts, t in inbound:
            rows_export.append({"kind": "in", "ch": _channel(s), "day": _day(ts), "len": len((t or "").strip())})

    if "outbound" in tables:
        turns = _bot_turns(conn)
        ctxs = _user_ctx_for(turns, inbound)
        ob: dict[str, Any] = {
            "rows_by_origin": _dist(
                conn.execute(
                    "SELECT CASE WHEN scope_key LIKE 'private:%' THEN 'private' ELSE 'group' END, "
                    "origin, author, status, COUNT(*) FROM outbound GROUP BY 1,2,3,4 ORDER BY 1,2,3,4"
                ).fetchall()
            )
        }
        for ch in ("private", "group"):
            idx = [i for i, t in enumerate(turns) if _channel(t["scope"]) == ch]
            texts = [turns[i]["text"] for i in idx]
            cx = [ctxs[i] for i in idx]
            agg = M.aggregate_replies(texts, cx)
            agg["days_covered"] = len({_day(turns[i]["ts"]) for i in idx})
            agg["origin"] = dict(sorted(Counter(turns[i]["origin"] for i in idx).items()))
            # 按会话的正式重复率窗口（不足窗口的会话只计数，不告警）
            per_scope: dict[str, list[str]] = defaultdict(list)
            for i in idx:
                per_scope[turns[i]["scope"]].append(turns[i]["text"])
            sess = [M.marker_repeat(v) for v in per_scope.values()]
            agg["sessions"] = {
                "n": len(sess),
                "insufficient": sum(1 for s in sess if s["status"] == "insufficient"),
                "alerts": dict(sorted(Counter(a for s in sess for a in s["alerts"]).items())),
            }
            ob[ch] = agg
        out["outbound"] = ob
        for t, cx in zip(turns, ctxs):
            rows_export.append(
                {
                    "kind": "out",
                    "ch": _channel(t["scope"]),
                    "origin": t["origin"],
                    "day": _day(t["ts"]),
                    "len": len(t["text"].strip()),
                    "rules": M.scan(t["text"], cx),
                }
            )

    if "items" in tables:
        out["items"] = {
            "status_kind": _dist(conn.execute("SELECT status, kind, COUNT(*) FROM items GROUP BY 1,2 ORDER BY 1,2").fetchall())
        }
    if "proactive_turns" in tables:
        out["proactive_turns"] = {
            "state_reason": _dist(
                conn.execute("SELECT state, reason, COUNT(*) FROM proactive_turns GROUP BY 1,2 ORDER BY 1,2").fetchall()
            ),
            "parts_sent": int(conn.execute("SELECT COALESCE(SUM(parts_sent),0) FROM proactive_turns").fetchone()[0]),
        }
    if "closures" in tables:
        out["closures"] = _dist(conn.execute("SELECT state, COUNT(*) FROM closures GROUP BY 1 ORDER BY 1").fetchall())
    if "private_context" in tables:
        out["private_context_len"] = {
            str(sec): int(n) for sec, n in conn.execute("SELECT section, LENGTH(content) FROM private_context ORDER BY section")
        }
    if "observed_posts" in tables:
        per_chat: dict[Any, Counter[str]] = defaultdict(Counter)
        owner_days: dict[Any, set[str]] = defaultdict(set)
        for chat, kind, ts in conn.execute("SELECT chat_id, author_kind, posted_at FROM observed_posts"):
            per_chat[chat][kind] += 1
            if kind == "owner":
                owner_days[chat].add(_day(ts) or "")
        ranked = sorted(per_chat, key=lambda c: (-sum(per_chat[c].values()), str(c)))
        out["observed_posts"] = {
            "author_kind": dict(sorted(sum(per_chat.values(), Counter()).items())),
            # 聊天 id 不输出，只按帖子数排序编号
            "per_chat": [
                {"chat": f"chat_{i + 1}", **dict(sorted(per_chat[c].items())), "owner_days": len(owner_days[c])}
                for i, c in enumerate(ranked)
            ],
        }
    if "counters" in tables:
        out["counters"] = {
            str(n): int(v)
            for n, v in conn.execute("SELECT name, SUM(value) FROM counters GROUP BY name ORDER BY name")
            if re.fullmatch(r"[A-Za-z0-9_.:\-]+", str(n))
        }
    return out, rows_export


# ---------------------------------------------------------------- learning.db


def collect_learning(conn: sqlite3.Connection) -> dict[str, Any]:
    out: dict[str, Any] = {"tables": _table_counts(conn)}
    tables = set(out["tables"])
    if "few_shot" in tables:
        out["few_shot_status"] = _dist(conn.execute("SELECT status, COUNT(*) FROM few_shot GROUP BY 1 ORDER BY 1").fetchall())
        approved = [t for (t,) in conn.execute("SELECT bot_text FROM few_shot WHERE status='approved'")]
        out["few_shot_approved"] = {
            "n": len(approved),
            "blacklist_hit_count": sum(bool(M.blacklist_hits(t)) for t in approved),
            "copy_hard_hit_count": sum(bool(M.copy_hits(t)["hard"]) for t in approved),
            "rule_hit_rates": M.rule_hit_rates(approved),
        }
    if "expression_pattern" in tables:
        out["expression_status"] = _dist(
            conn.execute("SELECT status, COUNT(*) FROM expression_pattern GROUP BY 1 ORDER BY 1").fetchall()
        )
        rows = conn.execute("SELECT expression, COALESCE(hit_count,0) FROM expression_pattern WHERE status='approved'").fetchall()
        hits = sorted((int(h) for _, h in rows), reverse=True)
        out["expression_approved"] = {
            "n": len(rows),
            "tail_particle_ending": sum(1 for e, _ in rows if M.sentence_tail_particle(e or "")),
            "reaction_opening": sum(1 for e, _ in rows if M.reaction_opener(e or "")),
            "blacklist_hit_count": sum(bool(M.blacklist_hits(e or "")) for e, _ in rows),
            "hit_count_top5": hits[:5],
            "hit_count_zero": sum(1 for h in hits if h == 0),
        }
    return out


# ---------------------------------------------------------------- data_v4.db（AstrBot 主库）


def _content_roles(content: Any) -> Counter[str]:
    try:
        data = json.loads(content) if isinstance(content, str) else content
    except (TypeError, ValueError):
        return Counter()
    if not isinstance(data, list):
        return Counter()
    return Counter(str(m.get("role")) for m in data if isinstance(m, dict))


def collect_main(conn: sqlite3.Connection, days: int = 14) -> dict[str, Any]:
    out: dict[str, Any] = {}
    tables = set(_tables(conn))
    if "conversations" in tables:
        conv: dict[str, Counter[str]] = defaultdict(Counter)
        for user_id, content in conn.execute("SELECT user_id, content FROM conversations"):
            kind = "private" if ":FriendMessage:" in str(user_id) else "group"
            conv[kind]["conversations"] += 1
            roles = _content_roles(content)
            conv[kind]["user_turns"] += roles.get("user", 0)
            conv[kind]["assistant_turns"] += roles.get("assistant", 0)
        out["conversations"] = {k: dict(sorted(v.items())) for k, v in sorted(conv.items())}
    if "provider_stats" in tables:
        rows = conn.execute(
            "SELECT date(created_at), COUNT(*), COALESCE(SUM(token_input_other),0), "
            "COALESCE(SUM(token_input_cached),0), COALESCE(SUM(token_output),0) "
            "FROM provider_stats GROUP BY 1 ORDER BY 1 DESC LIMIT ?",
            (int(days),),
        ).fetchall()
        out["provider_stats_daily"] = [
            {"day": d, "calls": int(c), "input_other": int(i), "input_cached": int(ic), "output": int(o)}
            for d, c, i, ic, o in reversed(rows)
        ]
    if "personas" in tables:
        ps = []
        for (prompt,) in conn.execute("SELECT system_prompt FROM personas ORDER BY id"):
            prompt = prompt or ""
            ps.append(
                {
                    "len": len(prompt),
                    "numeric_leak": M.numeric_leak_hits(prompt),
                    "copy_hard": M.copy_hits(prompt)["hard"],
                }
            )
        out["personas"] = ps
    return out


# ---------------------------------------------------------------- 汇总、自检、落盘


def assert_no_text(obj: Any, cjk_run: int = NO_TEXT_CJK_RUN) -> None:
    """递归检查所有字符串（含字典键）：不得含连续 ≥cjk_run 汉字或 ≥7 位数字串。"""

    def walk(x: Any) -> None:
        if isinstance(x, str):
            if M.contains_cjk_run(x, cjk_run):
                raise SnapshotSafetyError("output contains a CJK run")
            if _FORBIDDEN_STR_DIGITS.search(x):
                raise SnapshotSafetyError("output contains a long digit string")
        elif isinstance(x, dict):
            for k, v in x.items():
                walk(str(k))
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)

    walk(obj)


def take_snapshot(
    data_root: Path | str,
    *,
    allowed_roots: list[Path | str] | None = None,
    include_rows: bool = False,
    clock=time.time,
) -> dict[str, Any]:
    root = Path(data_root).resolve()
    roots = allowed_roots if allowed_roots is not None else [root]
    snap: dict[str, Any] = {
        "tool": TOOL_VERSION,
        "generated_day": _day(clock()),
        "sources": {},
    }
    rows: list[dict[str, Any]] = []
    collectors = {
        "continuity": collect_continuity,
        "learning": collect_learning,
        "persona_evolution": lambda c: {"tables": _table_counts(c)},
        "main": collect_main,
    }
    for name, rel in DB_PATHS.items():
        path = root / rel
        if not path.is_file():
            snap["sources"][name] = "missing"
            continue
        conn = open_ro(path, roots)
        try:
            res = collectors[name](conn)
            if name == "continuity":
                res, rows = res
            snap[name] = res
            snap["sources"][name] = "ok"
        except sqlite3.Error as e:
            snap["sources"][name] = f"error:{type(e).__name__}"
        finally:
            conn.close()
    if include_rows:
        snap["rows"] = rows
    assert_no_text(snap)
    return snap


def write_private_json(path: Path | str, obj: Any, *, data_root: Path | str | None = None) -> Path:
    """以 600 权限写 JSON；拒绝写进生产数据目录（<data_root>/dev 除外）。"""
    p = Path(path).resolve()
    if data_root is not None:
        root = Path(data_root).resolve()
        if _under(p, root) and not _under(p, root / "dev"):
            raise SnapshotSafetyError("refuse to write inside production data dir")
    assert_no_text(obj)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Iris lifelike read-only snapshot / baseline")
    ap.add_argument("--data-root", required=True, help="AstrBot data 目录（容器内 /AstrBot/data）")
    ap.add_argument("--out", help="输出 JSON 路径（权限 600）")
    ap.add_argument("--stdout", action="store_true", help="输出到标准输出")
    ap.add_argument("--rows", action="store_true", help="附带逐条脱敏行（只有 day/len/类别/规则 id）")
    ap.add_argument("--label", default="", help="写入结果的标签（ASCII）")
    args = ap.parse_args(argv)
    if not args.out and not args.stdout:
        ap.error("need --out or --stdout")
    snap = take_snapshot(args.data_root, include_rows=args.rows)
    if args.label:
        snap["label"] = re.sub(r"[^A-Za-z0-9_.\-]", "", args.label)[:64]
    if args.out:
        write_private_json(args.out, snap, data_root=args.data_root)
    if args.stdout:
        json.dump(snap, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
