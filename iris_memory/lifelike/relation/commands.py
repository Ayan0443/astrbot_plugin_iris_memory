"""``/iris_rel`` 命令（仅主人、仅主人私聊；设计 §9.9）。

- ``why``（默认）：当前基调、生效中的模式、最近的关系事件（id / 时间 / 种类 / 来源）。
- ``undo <事件id>``：撤回一条关系事件（另写 undo 事件），解除由它引起的模式，必要时撤销晋升。
- ``reset``：解除所有模式（写一条 owner_request 作为原因事件）。
- ``bond``：当前阶段与描述、是否冻结、最近一次变化依据的事件摘要、待批准的变化。
  **不显示门槛与进度**（也不显示规则编号）。
- ``bond approve [id]`` / ``bond reject <id>`` / ``bond undo <id>`` / ``bond freeze`` /
  ``bond unfreeze`` / ``bond set <阶段>``。

回执只给主人本人看；不经 LLM、不写对话历史。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from ...continuity import models as cm
from . import bond as bf
from . import fold as fd
from . import models as rm

__all__ = ["handle_rel_command"]

_KIND_LABEL = {
    rm.K_WARM: "暖心", rm.K_SHARED: "共同经历", rm.K_SUPPORT: "陪伴", rm.K_HURT: "不舒服",
    rm.K_BOUNDARY_CROSSED: "越过边界", rm.K_BOUNDARY_STATED: "说出边界", rm.K_REPAIR: "和解",
    rm.K_OWNER_REQUEST: "相处要求", rm.K_OWNER_FEEDBACK: "说话反馈", rm.K_CONFIDED: "倾诉",
    rm.K_RELIED: "托付", rm.K_PROMISE_KEPT: "兑现承诺", rm.K_DISTRESS: "主人状态不好", rm.K_UNDO: "撤回",
}
_MODE_LABEL = {rm.MODE_SOFTEN: "语气收一点", rm.MODE_BOUNDARY: "边界（不反复提）"}
_T_LABEL = {
    "promote": "更近了一步", "demote": "退回一步", "owner_request": "按你的要求调整", "undo": "撤销",
    "set": "你指定", "freeze": "冻结", "unfreeze": "解冻",
}
HELP = (
    "用法：/iris_rel [why] | undo <事件id> | reset | bond | bond approve [id] | bond reject <id>"
    " | bond undo <id> | bond freeze | bond unfreeze | bond set <阶段>"
)


def _fmt(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts), cm.TZ_SHANGHAI).strftime("%m-%d %H:%M")
    except Exception:
        return "?"


def _tokens(raw: str) -> list[str]:
    parts = str(raw or "").strip().split()
    while parts and parts[0].lstrip("/").lower() in ("iris_rel", "iris-rel"):
        parts = parts[1:]
    return parts


async def handle_rel_command(svc: Any, scope_key: str, raw: str, msg_id: Any) -> str:
    if svc.mode() == rm.MODE_OFF:
        return "关系模块没有开启（lifelike_relation_mode=off）。"
    args = _tokens(raw)
    ref = f"cmd:{int(msg_id)}" if str(msg_id or "").isdigit() else ""
    return await asyncio.to_thread(_dispatch, svc, scope_key, args, ref)


def _dispatch(svc: Any, scope: str, args: list[str], ref: str) -> str:
    now = float(svc._clock())  # noqa: SLF001
    head = args[0].lower() if args else "why"
    prefix = "（shadow：只记录，不影响她说话）\n" if svc.mode() == rm.MODE_SHADOW else ""
    if head == "why":
        return prefix + _why(svc, scope, now)
    if head in ("undo", "reset") and not ref:
        return "拿不到这条命令的消息 id，没有执行。"
    if head == "undo":
        if len(args) < 2:
            return HELP
        return prefix + _undo(svc, scope, args[1], ref, now)
    if head == "reset":
        ev = svc.repo.make_event(scope, rm.K_OWNER_REQUEST, [ref], now, rm.DETECTOR_CMD, context=rm.CTX_RESET)
        if not svc._append(ev):  # noqa: SLF001
            return "没有写成功，什么都没改。"
        n = svc._release(scope, rm.REL_RESET, f"rel:{ev.rel_event_id}", now)  # noqa: SLF001
        return prefix + (f"已解除生效中的模式（{n} 个）。" if n else "现在没有生效中的模式。")
    if head == "bond":
        return prefix + _bond(svc, scope, args[1:], ref, now)
    return HELP


def _why(svc: Any, scope: str, now: float) -> str:
    events = svc.repo.events(scope, now - 14 * rm.DAY)
    modes = svc.repo.modes(scope)
    view = fd.fold_tone(events, modes, now)
    lines = [f"近期基调：{view.stage}"]
    if view.reasons:
        lines.append(f"原因：{view.reasons[0]}")
    live = fd.active_modes(modes, now)
    for md in live:
        lines.append(
            f"模式 {md['mode_id']}：{_MODE_LABEL.get(md['kind'], md['kind'])}，"
            f"{_fmt(md['started_at'])} 起，最晚 {_fmt(md['expires_at'])} 自动解除（原因事件 {md['cause_event_id']}）"
        )
    if live:
        lines.append("（冷静期只影响她说话的语气和内容，不会因此少找你、不冷落）")
    recent = [e for e in events if e.get("kind") != rm.K_UNDO][-10:]
    if recent:
        lines.append("最近的关系事件：")
        for e in recent:
            flag = "" if int(e.get("active") or 0) else "（已撤回）"
            lines.append(
                f"- {e['rel_event_id']} {_fmt(e['ts'])} {_KIND_LABEL.get(e['kind'], e['kind'])}"
                f"（{e['detector']}）{flag}"
            )
    else:
        lines.append("最近两周没有关系事件。")
    return "\n".join(lines)


def _undo(svc: Any, scope: str, rel_id: str, ref: str, now: float) -> str:
    uid = svc.repo.undo(scope, rel_id, ref, now)
    if not uid:
        return "没找到这条可撤回的事件。"
    modes = [md for md in fd.active_modes(svc.repo.modes(scope), now) if md.get("cause_event_id") == rel_id]
    svc.repo.release_modes([md["mode_id"] for md in modes], rm.REL_UNDO, f"rel:{uid}", now)
    tid = svc.bond_evaluate_sync(scope, now, only_undo=True)
    tail = "；依据它的那次阶段变化也撤销了" if tid else ""
    return f"已撤回 {rel_id}{tail}。"


def _bond(svc: Any, scope: str, args: list[str], ref: str, now: float) -> str:
    if svc.bond_mode() == rm.MODE_OFF:
        return "关系渐进没有开启（lifelike_bond_mode=off）。"
    sub = args[0].lower() if args else ""
    start = svc.start_stage()
    rows = svc.repo.transitions(scope)
    st_on = bf.bond_state(rows, start, rm.MODE_ON)
    if not sub:
        return _bond_show(svc, scope, rows, st_on, start)
    if not ref:
        return "拿不到这条命令的消息 id，没有执行。"
    cur = st_on.stage

    def write(t: rm.BondTransition) -> str | None:
        t.actor = "owner_cmd"
        t.track = rm.MODE_ON
        return svc._write_transition(scope, t, now)  # noqa: SLF001

    if sub in ("approve", "reject"):
        target_id = args[1] if len(args) > 1 else ""
        cands = [
            r for r in rows
            if r.get("status") == "proposed" and r.get("from_stage") == cur
            and str(r.get("transition_id")) not in {str(x.get("ref_id")) for x in rows}
        ]
        if target_id:
            cands = [r for r in cands if r.get("transition_id") == target_id]
        elif sub == "reject":
            return "要拒绝哪一条？/iris_rel bond reject <id>"
        if not cands:
            return "没有可以处理的待批准变化。"
        r = cands[-1]
        refs = [ref, f"bt:{r['transition_id']}"]
        if sub == "approve":
            refs += [str(x) for x in (r.get("cause_refs") or []) if str(x).startswith("re_")]
            t = rm.BondTransition(cur, r["to_stage"], r["kind"], refs, rule_id=r.get("rule_id") or "",
                                  status="applied", ref_id=r["transition_id"])
            ok = write(t)
            return f"已批准：现在是「{rm.STAGE_NAMES[r['to_stage']]}」。" if ok else "没有写成功。"
        t = rm.BondTransition(cur, cur, "reject", refs, status="rejected", ref_id=r["transition_id"])
        return "已拒绝这条变化。" if write(t) else "没有写成功。"
    if sub == "undo":
        if len(args) < 2:
            return "要撤销哪一条？/iris_rel bond undo <id>"
        lc = st_on.last_change
        if lc is None or lc.get("transition_id") != args[1]:
            return "只能撤销最近一次生效的阶段变化。"
        t = rm.BondTransition(cur, lc["from_stage"], "undo", [ref, f"bt:{args[1]}"], status="applied",
                              ref_id=args[1])
        return f"已撤销：回到「{rm.STAGE_NAMES[lc['from_stage']]}」。" if write(t) else "没有写成功。"
    if sub in ("freeze", "unfreeze"):
        if (sub == "freeze") == st_on.frozen:
            return "已经是这个状态了。"
        ok = write(rm.BondTransition(cur, cur, sub, [ref], status="applied"))
        return ("已冻结：不会再自动变近。" if sub == "freeze" else "已解冻。") if ok else "没有写成功。"
    if sub == "set":
        if len(args) < 2:
            return "要设成哪个阶段？" + "、".join(rm.STAGE_NAMES.values())
        code = rm.parse_stage(" ".join(args[1:]), default="")
        if code not in rm.STAGE_CODES:
            return "不认识这个阶段。可选：" + "、".join(rm.STAGE_NAMES.values())
        # 往下设 = 主人要求的回退：立即生效并冻结（§5.7.4 第 2 种）
        kind = "owner_request" if rm.stage_index(code) < rm.stage_index(cur) else "set"
        ok = write(rm.BondTransition(cur, code, kind, [ref], status="applied"))
        tail = "，并冻结了自动变近" if kind == "owner_request" else ""
        return f"已设为「{rm.STAGE_NAMES[code]}」{tail}。" if ok else "没有写成功。"
    return HELP


def _bond_show(svc: Any, scope: str, rows: list[dict], st_on: bf.BondState, start: str) -> str:
    lines = [f"现在：{rm.STAGE_NAMES[st_on.stage]}（{rm.STAGE_DESC[st_on.stage]}）"]
    if st_on.frozen:
        lines.append("已冻结：不会自动变近。")
    lc = st_on.last_change
    if lc is not None:
        lines.append(f"最近一次变化：{_fmt(lc['created_at'])} {_T_LABEL.get(lc['kind'], lc['kind'])}（{lc['transition_id']}）")
        sums = []
        for r in list(lc.get("cause_refs") or [])[-3:]:
            ev = svc.repo.get_event(str(r))
            if ev:
                sums.append(_KIND_LABEL.get(str(ev.get("kind")), ""))
        if sums:
            lines.append("依据的事件类型：" + "、".join(s for s in sums if s))
    for r in st_on.pending:
        lines.append(
            f"待批准：{r['transition_id']} {rm.STAGE_NAMES[r['from_stage']]} → {rm.STAGE_NAMES[r['to_stage']]}"
            f"（{_fmt(r['created_at'])}）"
        )
    if svc.bond_mode() == rm.MODE_SHADOW:
        st_sh = bf.bond_state(rows, start, rm.MODE_SHADOW)
        lines.append(f"shadow 轨迹：{rm.STAGE_NAMES[st_sh.stage]}")
        shadow_rows = [r for r in rows if r.get("track") == rm.MODE_SHADOW][-5:]
        for r in shadow_rows:
            lines.append(
                f"- {r['transition_id']} {_fmt(r['created_at'])} {_T_LABEL.get(r['kind'], r['kind'])}："
                f"{rm.STAGE_NAMES[r['from_stage']]} → {rm.STAGE_NAMES[r['to_stage']]}"
            )
    return "\n".join(lines)
