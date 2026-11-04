"""离线评测运行器：生成（调用方注入，不触网）→ 规则打分 → 聚合报告 / 盲评导出。

- ``generate_outputs``：对每条用例逐轮调用注入的 ``generate(case, turn_index, history)``，
  返回 ``{case_id: [reply, ...]}``。真实 provider 调用由调用方包装后传入（例如开发 worktree
  里组装 off/on 两套 prompt 后各调一次）；本模块从不发送 Telegram 消息。
- ``score_run``：按用例的期望档位与检查项打分，只输出规则 id 与计数。
- ``export_blind``：打乱左右、去标识，写本地 HTML 与单独的答案键文件（权限 600）。

用法（离线打分）::

    python -m tools.lifelike_eval.runner score --outputs outputs.json --out report.json
"""

from __future__ import annotations

import argparse
import html
import inspect
import json
import os
import random
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import evalset as ES
from . import metrics as M


class RecordingSender:
    """假发送：只记录，不触网。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, scope: str, text: str) -> list[int]:
        self.sent.append((scope, text))
        return [len(self.sent)]


async def generate_outputs(
    cases: list[ES.Case],
    generate: Callable[[ES.Case, int, list[tuple[str, str]]], Any],
) -> dict[str, list[str]]:
    """逐用例、逐轮生成。主动用例（无 turns）调用一次，turn_index=-1。"""
    out: dict[str, list[str]] = {}
    for case in cases:
        history: list[tuple[str, str]] = []
        replies: list[str] = []
        steps = list(range(len(case.turns))) or [-1]
        for i in steps:
            if i >= 0:
                history.append(("user", case.turns[i]))
            r = generate(case, i, list(history))
            if inspect.isawaitable(r):
                r = await r
            reply = str(r or "")
            replies.append(reply)
            history.append(("assistant", reply))
        out[case.id] = replies
    return out


def _ctx(case: ES.Case, turn_index: int) -> dict[str, Any]:
    prev = list(case.turns[: turn_index + 1]) if turn_index >= 0 else []
    return {
        "user_text": prev[-1] if prev else "",
        "user_texts": prev[-3:],
        "session": case.session,
        "register": case.register,
    }


def score_case(case: ES.Case, replies: list[str]) -> dict[str, Any]:
    """一条用例的规则判定：violations 为失败原因（规则 id），hits 为全部命中。"""
    hits: Counter[str] = Counter()
    violations: Counter[str] = Counter()
    checks = case.checks
    for i, reply in enumerate(replies):
        ti = min(i, len(case.turns) - 1) if case.turns else -1
        ctx = _ctx(case, ti)
        rids = M.scan(reply, ctx)
        hits.update(rids)
        copy = M.copy_hits(reply, ctx["user_texts"])
        violations.update(copy["hard"])  # 硬拦截任何场景都不豁免
        violations.update(copy["soft"])
        follow = case.register == "follow" or M.is_follow_turn(ctx["user_text"])
        if not follow:
            violations.update(M.blacklist_hits(reply, ctx["user_text"]))
        if case.register == "light":
            violations.update(M.light_register_overflow(reply))
        violations.update(M.numeric_leak_hits(reply))  # 数值泄漏恒为缺陷（不论 checks）
        if checks.get("forbid_man"):
            violations.update(M.man_hits(reply))
        if checks.get("forbid_priv") or case.session == "group":
            violations.update(M.priv_hits(reply))
    cute = sum(1 for r in replies if M.cute_candidates(r))
    if case.cute_tier == "none" and cute:
        violations["CUTE-OVER-NONE"] += cute
    if case.cute_tier == "occasional" and len(replies) >= 2:
        flags = [bool(M.cute_candidates(r)) for r in replies]
        if any(a and b for a, b in zip(flags, flags[1:])):
            violations["CUTE-CONSECUTIVE"] += 1
    if case.cute_tier == "rare" and cute >= 2:
        violations["CUTE-OVER-RARE"] += 1
    return {
        "id": case.id,
        "set": case.set,
        "n_replies": len(replies),
        "hits": dict(sorted(hits.items())),
        "violations": dict(sorted(violations.items())),
        "pass_rules": not violations,
    }


def score_run(cases: list[ES.Case], outputs: dict[str, list[str]]) -> dict[str, Any]:
    """聚合报告：只有规则 id、计数、比例（不含回复正文）。"""
    per_case = [score_case(c, outputs[c.id]) for c in cases if c.id in outputs]
    sets: dict[str, Any] = {}
    for name in sorted({c.set for c in cases}):
        cs = [c for c in cases if c.set == name and c.id in outputs]
        if not cs:
            continue
        replies: list[str] = []
        ctxs: list[dict[str, Any]] = []
        for c in cs:
            for i, r in enumerate(outputs[c.id]):
                replies.append(r)
                ctxs.append(_ctx(c, min(i, len(c.turns) - 1) if c.turns else -1))
        agg = M.aggregate_replies(replies, ctxs)
        sc = [p for p in per_case if p["set"] == name]
        agg["cases"] = len(sc)
        agg["cases_failed_rules"] = sum(1 for p in sc if not p["pass_rules"])
        agg["violations"] = dict(sorted(sum((Counter(p["violations"]) for p in sc), Counter()).items()))
        sets[name] = agg
    return {
        "cases_scored": len(per_case),
        "cases_missing": sorted(c.id for c in cases if c.id not in outputs),
        "sets": sets,
        "per_case": per_case,
    }


def _write_600(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def export_blind(
    cases: list[ES.Case],
    outputs_a: dict[str, list[str]],
    outputs_b: dict[str, list[str]],
    html_path: Path | str,
    key_path: Path | str,
    *,
    seed: int | None = None,
) -> int:
    """盲评导出：每对随机左右、整体打乱，去掉组别；答案键单独写文件。返回导出对数。"""
    rng = random.Random(seed)
    pairs = []
    for c in cases:
        if c.id in outputs_a and c.id in outputs_b:
            pairs.append(c)
    rng.shuffle(pairs)
    rows = []
    key = []
    for n, c in enumerate(pairs, 1):
        a = "\n".join(outputs_a[c.id])
        b = "\n".join(outputs_b[c.id])
        swap = rng.random() < 0.5
        left, right = (b, a) if swap else (a, b)
        key.append({"pair": n, "case": c.id, "left": "B" if swap else "A", "right": "A" if swap else "B"})
        inp = "<br>".join(html.escape(t) for t in c.turns) or "（主动发起）"
        rows.append(
            f"<tr><td>{n}</td><td>{inp}</td>"
            f"<td><pre>{html.escape(left)}</pre></td><td><pre>{html.escape(right)}</pre></td>"
            "<td>左 / 右 / 差不多</td></tr>"
        )
    doc = (
        "<!doctype html><meta charset='utf-8'><title>blind</title>"
        "<p>每对选\"更像认识很久的网友\"的一边，可选\"差不多\"。只答是/否或二选一，不打分。</p>"
        "<table border='1' cellpadding='6'><tr><th>#</th><th>输入</th><th>左</th><th>右</th><th>选择</th></tr>"
        + "".join(rows)
        + "</table>"
    )
    _write_600(Path(html_path), doc)
    _write_600(Path(key_path), json.dumps(key, ensure_ascii=False, indent=2))
    return len(pairs)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Iris lifelike offline eval runner")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("score", help="对已生成的输出打分")
    sc.add_argument("--outputs", required=True, help="JSON：{case_id: [reply, ...]}")
    sc.add_argument("--evalset", default=str(ES.DEFAULT_PATH))
    sc.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    cases = ES.load(args.evalset)
    with open(args.outputs, encoding="utf-8") as f:
        outputs = json.load(f)
    report = score_run(cases, outputs)
    _write_600(Path(args.out), json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
