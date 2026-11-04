"""WorldProjection：``<她的今天>`` 只读投影（设计 §4.7、附录 B）。

只读 life_state / 当天与昨天的 life_events / 待提示的 conflict 声明；固定格式、全部文字化
（时段用"上午/下午"等词，不出现钟点、计数或分值）。

- 主人私聊：现在 / 已经做了（最近 1–2 件）/ 打算（下一件）/ 还在弄 / 没做成 + 尾句；
- 群聊：只给"现在：在家/外出"一句（不讲私人日常）；
- 低密度：24 小时内已提过 ≥2 次的事件降权；
- 在线一致性：她在"睡觉"时被消息叫醒 → 改写为"本来在睡，被消息吵醒了"。

调用方再经 NumericLeakGuard（continuity.context 的 run() 统一处理）。
"""

from __future__ import annotations

from typing import Optional

from . import models as wm
from . import rules

OPEN = "<她的今天>"
CLOSE = "</她的今天>"
MAX_CHARS = 260
ITEM_MAX = 40
CONFLICT_WINDOW_S = 12 * 3600
TAIL = (
    "这些是你设定好的虚拟日常，只能讲上面有的事，没写到的生活细节不要编；"
    "对方没问起就不用主动讲。被认真问是不是真的，就说这是你的虚拟日常。"
)
# 群聊精简尾句（D7：群里同样不编细节，被认真问就承认是虚拟设定；审查 P4）
GROUP_TAIL = "这是设定好的虚拟日常：不展开细节、不编没写到的事；被认真问就承认是虚拟的。"


def _clip(s: str, n: int = ITEM_MAX) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _deprioritized(ev: wm.LifeEvent, now: float) -> bool:
    return (
        ev.mention_count >= 2
        and ev.last_mentioned_at is not None
        and now - float(ev.last_mentioned_at) < 86400
    )


def _when(ev: wm.LifeEvent, today: str) -> str:
    p = wm.period_text(ev.slot_start or ev.due_at or ev.settled_at)
    if ev.day != today:
        return "昨天" + p
    return p


def render_projection(
    events: list[wm.LifeEvent],
    now: float,
    *,
    owner_private: bool,
    conflicts: Optional[list[str]] = None,
    canon: Optional[dict] = None,
) -> str:
    evs = [e for e in events if not e.retracted and e.shareable != "none"]
    state = rules.derive_state(evs, now, canon)
    today = wm.local_day(now)
    lines: list[str] = []
    if not owner_private:
        # 群里只给场景态一句（不讲事件内容，私人日常不进群）
        scene = "外出" if state.scene in (wm.SCENE_OUT, wm.SCENE_ROAD) else "在家"
        return "\n".join([OPEN, f"现在：{scene}。", GROUP_TAIL, CLOSE])

    cur = next((e for e in evs if e.event_id == state.current_event_id), None)
    if not state.awake:
        # 不给"回得短一点"的许可、不提"被吵醒"：不能因为生活设定降低对主人的回应质量（审查 P3）
        lines.append("现在：本来在睡觉，刚醒。照常认真回应；不抱怨被吵醒，不让对方觉得打扰了你。")
    else:
        now_line = f"现在：{state.scene}，{state.energy}"
        if cur is not None:
            now_line += f"，正在{_clip(cur.title)}"
        if state.mood and state.mood != "平常" and state.mood_cause:
            now_line += f"，{_clip(state.mood, wm.MOOD_MAX)}"
        lines.append(now_line + "。")

    done = [e for e in evs if e.status == wm.ST_DONE]
    done.sort(key=lambda e: float(e.settled_at or e.due_at or 0), reverse=True)
    fresh = [e for e in done if not _deprioritized(e, now)]
    picked = (fresh or done)[:2] if done else []
    if picked:
        parts = []
        for e in picked:
            s = f"{_when(e, today)}{_clip(e.title)}"
            if e.outcome:
                s += f"，{_clip(e.outcome)}"
            parts.append(s)
        lines.append("已经做了：" + "；".join(parts) + "。")

    doing = [e for e in evs if e.status == wm.ST_IN_PROGRESS and (cur is None or e.event_id != cur.event_id)]
    if doing:
        lines.append("还在弄：" + _clip(doing[0].title) + "（还没弄完）。")

    planned = [e for e in evs if e.status == wm.ST_PLANNED and e.day == today]
    planned.sort(key=lambda e: float(e.slot_start or e.due_at or 0))
    if planned:
        nxt = planned[0]
        lines.append(f"打算：{wm.period_text(nxt.slot_start or nxt.due_at)}{_clip(nxt.title)}（还没开始）。")

    skipped = [e for e in evs if e.status == wm.ST_CANCELLED and e.day == today]
    if skipped:
        e = skipped[-1]
        lines.append(f"没做成：本来想{_clip(e.title)}，{_clip(e.outcome or '后来没做')}。")

    for span in (conflicts or [])[:1]:
        lines.append(f"你上次随口说了『{_clip(span, 30)}』，和你今天的实际不一样；被问到就自然圆回来，或者承认记岔了。")

    body = "\n".join(lines)
    if len(body) > MAX_CHARS:
        body = body[: MAX_CHARS - 1] + "…"
    return "\n".join([OPEN, body, TAIL, CLOSE])
