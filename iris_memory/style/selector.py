"""``<iris:style>`` 段组装（ROADMAP P1-2 / P1-4，口吻附录 §4.4、§4.5、§5.3）。

开关（HiddenConfig 平铺键，不受 ``lifelike_enable`` 控制）：

- ``style_anime_register``：``off / light / medium / strong``，口吻档位上限；``off`` = 不注入（现网行为）；
- ``style_group_register_cap``：``off / light / medium``，群聊再取一次上限；``off`` = 群聊不注入；
- ``style_cute_cap``：``off / rare / occasional``，卖萌档上限，和场景卖萌档取较低者；
- ``style_examples_path``：示例库覆盖路径，空 = 代码自带种子库；
- ``style_downgrade_on_feedback``：被说做作后本段对话保持淡档；
- ``style_inject_proactive``：主动消息拼 system 时追加 style 段（固定"中"档，P1-4）。

每轮流程：判场景 → 叠加上限 → 卖萌上限（按本段对话里已观察到的卖萌回复再收一档）→
在 scene / channel 匹配、档位不浓于本轮的条目里随机抽 1～2 条正例（冷却中的 id 不抽）+
可选 1 条反例 → 拼文字（只有 淡/中/浓、不卖萌/很少/偶尔 这些字，不含数字）。

状态只在进程内存里（每个会话：最近用过的示例 id、本段对话的卖萌标记、做作降档标志），
重启清空，不落库。不读 learning.db、不拿 ``_db_lock``、不读任何关系数值。

P1-11 联动（唯一与关系有关的一处）：``set_cute_relax_provider`` 注册的回调对本会话返回 True 时
（主人私聊、关系阶段≥"熟悉的朋友"、不在冷静期），私聊"分享开心的事""普通问答"两格的场景卖萌档由
"很少"放宽到"偶尔"；上限仍是 ``style_cute_cap``，"不卖萌"场景与群聊不变。没有注册 = 现网行为。
任何异常返回空串（= 不注入，等同现网），只记类型名；连续 5 次失败熔断 1 小时。
"""

from __future__ import annotations

import random
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence, Tuple

from ..core import get_logger
from ..lifelike.style import guard
from . import examples as EX
from . import register as R

logger = get_logger("style.selector")

_LOG = "[lifelike.style_inject]"

# 内部参数（只在代码里，不进 prompt）
_MAX_SCOPES = 256
_SEGMENT_GAP_SECONDS = 30 * 60  # 超过这么久没有对方消息 = 新的一段对话
_EXAMPLE_COOLDOWN = 6  # 每个会话最近用过的示例 id 个数
_CUTE_WINDOW = 4  # "最近一段回复"
_STYLE_MAX_TOKENS = 300  # 实测：档位描述 + 卖萌行约 100 token，单条示例约 50～70 token
_FAIL_LIMIT = 5
_PAUSE_SECONDS = 3600.0

# 场景 → 可抽示例的 scene 标签（按顺序；没有条目时只注入档位描述）
_POOL_SCENES: Dict[str, Tuple[str, ...]] = {
    R.SCENE_PROACTIVE: (R.SCENE_PROACTIVE, R.SCENE_GREET),
    R.SCENE_FOLLOW: (),  # 跟随一轮不给示例，避免把二次元腔教成常态
    R.SCENE_IDENTITY: (),
    R.SCENE_UNKNOWN: (),
}

_REGISTER_DESC: Dict[int, str] = {
    R.REG_LIGHT: "正常好好说话。最多开头一个小反应词，句尾基本不加语气词，不用“～”，不跑题，不撒娇。",
    R.REG_MEDIUM: "可以有一个反应词或一处句尾软化，情绪可以直接说出来；不撒娇，不连用语气词。",
    R.REG_STRONG: "可以俏皮一点：撒娇式商量、真心开心、小坚持、偶尔一个“～”都可以用；但仍然好懂，仍然不说教。",
}
_CUTE_LINE: Dict[int, str] = {
    R.CUTE_OFF: "卖萌：这一轮不卖萌。",
    R.CUTE_RARE: "卖萌：很少。一段对话里最多一次，其余正常说话。",
    R.CUTE_OCCASIONAL: "卖萌：偶尔，大多数时候正常说话，不撒娇留人。",
}
# 只有被夸 / 开玩笑场景才写具体做法；其余场景写具体桥段会把模型推向固定的"假装委屈"套路（审查 M8）
_CUTE_SCENE_LINE: Dict[Tuple[str, int], str] = {
    (R.SCENE_PRAISED, R.CUTE_OCCASIONAL): "卖萌：偶尔。被夸可以小小得意一下；大多数时候正常说话，不撒娇留人。",
    (R.SCENE_JOKE, R.CUTE_OCCASIONAL): "卖萌：偶尔。玩笑里可以假装委屈一下；大多数时候正常说话，不撒娇留人。",
}
_HEADER = "## 本轮口吻"
_REF_HEADER = "参考（只学语气，不照抄内容和句式）："


def _cfg_get(key: str, fallback: Any) -> Any:
    """读 Iris 配置；未注册的键（测试桩）回落到 fallback（各开关的 fallback 都是"关闭"方向）。"""
    try:
        from ..config import get_config

        value = get_config().get(key, fallback)
    except Exception:
        return fallback
    return fallback if value is None else value


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off", ""):
            return False
    return default


# P1-11：卖萌两格放宽的回调 fn(scope) -> bool（同步、只读进程内缓存）；None = 不放宽
_cute_relax_provider: Optional[Callable[[str], bool]] = None
_RELAX_SCENES = frozenset({R.SCENE_GOOD_NEWS, R.SCENE_QUESTION})


def set_cute_relax_provider(fn: Optional[Callable[[str], bool]]) -> None:
    global _cute_relax_provider
    _cute_relax_provider = fn


def get_cute_relax_provider() -> Optional[Callable[[str], bool]]:
    return _cute_relax_provider


def _counter_hostile(user_text: str) -> bool:
    """lifelike_counter_mode=on 且对方消息初筛带难听话 → True；关闭 / 异常 → False（= 旧行为）。"""
    try:
        if str(_cfg_get("lifelike_counter_mode", "off") or "").strip().lower() not in ("on", "true", "1"):
            return False
        from ..proactive.hostility import classify

        return bool(classify(user_text or ""))
    except Exception:
        return False


def _cute_relaxed(scope: str, judgement: "R.SceneJudgement") -> bool:
    fn = _cute_relax_provider
    if fn is None or judgement.is_group or judgement.scene not in _RELAX_SCENES:
        return False
    try:
        return bool(fn(scope))
    except Exception:
        return False


@dataclass(frozen=True)
class StyleSettings:
    register_cap: int = R.REG_OFF
    group_cap: int = R.REG_OFF
    cute_cap: int = R.CUTE_OFF
    examples_path: str = ""
    downgrade_on_feedback: bool = True
    inject_proactive: bool = False
    # 群聊与私聊同样按场景判断口吻 / 卖萌（style_group_unified；false = 群聊用更收敛的一列）
    group_unified: bool = False

    @property
    def enabled(self) -> bool:
        return self.register_cap > R.REG_OFF


def _examples_path() -> str:
    """style_examples_path 优先；未设置且 style_casual_punctuation 打开 → 随性语气版示例库。"""
    path = str(_cfg_get("style_examples_path", "") or "")
    if path.strip():
        return path
    if _as_bool(_cfg_get("style_casual_punctuation", False), False):
        return str(EX.SEED_PATH.with_name("style_examples_v2_casual.json"))
    return path


def read_settings() -> StyleSettings:
    """读开关；异常时返回全关（= 现网行为）。"""
    try:
        return StyleSettings(
            register_cap=R.parse_register(_cfg_get("style_anime_register", "off")),
            group_cap=min(R.REG_MEDIUM, R.parse_register(_cfg_get("style_group_register_cap", "off"))),
            cute_cap=R.parse_cute(_cfg_get("style_cute_cap", "off")),
            examples_path=_examples_path(),
            downgrade_on_feedback=_as_bool(_cfg_get("style_downgrade_on_feedback", True), True),
            inject_proactive=_as_bool(_cfg_get("style_inject_proactive", False), False),
            group_unified=_as_bool(_cfg_get("style_group_unified", False), False),
        )
    except Exception:
        return StyleSettings()


@dataclass
class _ScopeState:
    recent_examples: Deque[str] = field(default_factory=lambda: deque(maxlen=_EXAMPLE_COOLDOWN))
    cute_flags: Deque[bool] = field(default_factory=lambda: deque(maxlen=_CUTE_WINDOW))
    segment_cute: int = 0
    sticky_light: bool = False
    last_user_ts: float = 0.0


@dataclass(frozen=True)
class StylePlan:
    scene: str
    label: str
    register: int
    cute: int
    examples: Tuple[EX.StyleExample, ...]
    anti: Optional[EX.StyleExample]
    sticky: bool


class StyleSelector:
    """每进程一个；所有入口都不抛异常。"""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        rng: Optional[random.Random] = None,
        token_counter: Optional[Callable[[str], int]] = None,
    ) -> None:
        self._clock = clock
        self._rng = rng or random.Random()
        self._count_tokens = token_counter
        self._scopes: "OrderedDict[str, _ScopeState]" = OrderedDict()
        self._fail = 0
        self._paused_until = 0.0

    # ------------------------------------------------------------ 状态

    def _state(self, scope: str) -> _ScopeState:
        st = self._scopes.get(scope)
        if st is None:
            st = _ScopeState()
            self._scopes[scope] = st
            while len(self._scopes) > _MAX_SCOPES:
                self._scopes.popitem(last=False)
        else:
            self._scopes.move_to_end(scope)
        return st

    def _touch_segment(self, st: _ScopeState, now: float) -> None:
        if st.last_user_ts and now - st.last_user_ts > _SEGMENT_GAP_SECONDS:
            st.sticky_light = False
            st.cute_flags.clear()
            st.segment_cute = 0
        st.last_user_ts = now

    def _ok(self) -> None:
        self._fail = 0

    def _failed(self, where: str, exc: BaseException) -> None:
        self._fail += 1
        logger.warning(f"{_LOG} {where} failed: {type(exc).__name__}")
        if self._fail >= _FAIL_LIMIT:
            self._paused_until = self._clock() + _PAUSE_SECONDS
            self._fail = 0
            logger.warning(f"{_LOG} paused_error for 1h after repeated failures")

    def _paused(self) -> bool:
        return self._clock() < self._paused_until

    # ------------------------------------------------------------ 卖萌上限

    @staticmethod
    def _cute_after_history(cute: int, st: _ScopeState) -> int:
        if cute == R.CUTE_RARE and st.segment_cute >= 1:
            return R.CUTE_OFF
        if cute == R.CUTE_OCCASIONAL:
            flags = list(st.cute_flags)
            if flags and flags[-1]:
                return R.CUTE_OFF
            if sum(1 for f in flags if f) * 2 >= _CUTE_WINDOW:
                return R.CUTE_OFF
        return cute

    # ------------------------------------------------------------ 选择

    def _pick(
        self,
        library: Sequence[EX.StyleExample],
        scene: str,
        channel: str,
        register: int,
        cute: int,
        st: _ScopeState,
    ) -> Tuple[Tuple[EX.StyleExample, ...], Optional[EX.StyleExample]]:
        scenes = _POOL_SCENES.get(scene, (scene,))
        if not scenes:
            return (), None
        eligible = [
            ex for ex in library
            if ex.scene in scenes
            and channel in ex.channels
            and ex.register <= register
            and (not ex.cute or cute >= R.CUTE_OCCASIONAL)
        ]
        cooled = set(st.recent_examples)
        fresh = [ex for ex in eligible if ex.id not in cooled]
        want = 1 if register <= R.REG_LIGHT else 2
        chosen: List[EX.StyleExample] = []
        for sc in scenes:  # 本场景优先，其次回退场景
            pool = [ex for ex in fresh if ex.scene == sc and ex not in chosen]
            self._rng.shuffle(pool)
            chosen.extend(pool[: want - len(chosen)])
            if len(chosen) >= want:
                break
        if not chosen and eligible:
            # 冷却把正例池耗尽：重用冷却队列里最早用过的一条（审查 M8，避免之后只剩反例）
            order = {eid: i for i, eid in enumerate(st.recent_examples)}
            reuse = min(eligible, key=lambda ex: (order.get(ex.id, -1), ex.id))
            chosen.append(reuse)
        if not chosen:
            return (), None  # 没有正例时不单独注入反例
        anti = next((ex for ex in chosen if ex.anti), None)
        if anti is None:
            antis = [ex for ex in eligible if ex.anti]
            anti = self._rng.choice(antis) if antis else None
        return tuple(chosen), anti

    def plan(
        self,
        scope: str,
        user_text: str,
        *,
        is_group: bool,
        settings: StyleSettings,
        library: Optional[Sequence[EX.StyleExample]] = None,
        scene: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> Optional[StylePlan]:
        """计算本轮计划并更新冷却（不拼文字）；档位为 off 时返回 None。"""
        now = self._clock()
        st = self._state(scope)
        if scene is None:
            self._touch_segment(st, now)
            if settings.group_unified:
                judgement = R.judge_scene(user_text, is_group=is_group, unified=True)
            else:
                judgement = R.judge_scene(user_text, is_group=is_group)
            if settings.downgrade_on_feedback and judgement.scene == R.SCENE_FEEDBACK:
                st.sticky_light = True
        else:
            if settings.group_unified:
                judgement = R.scene_judgement(scene, is_group=is_group, unified=True)
            else:
                judgement = R.scene_judgement(scene, is_group=is_group)
        if scene is None and judgement.cute > R.CUTE_OFF and _counter_hostile(user_text):
            # 被恶意对待时（反击开关打开且初筛命中）不卖萌
            judgement = replace(judgement, cute=R.CUTE_OFF)
        sticky = bool(settings.downgrade_on_feedback and st.sticky_light and scene is None)
        if scene is None and judgement.cute == R.CUTE_RARE and _cute_relaxed(scope, judgement):
            # P1-11：两格由"很少"放宽到"偶尔"；仍和 style_cute_cap 取较低者，淡档仍不卖萌
            judgement = replace(judgement, cute=R.CUTE_OCCASIONAL)
        reg, cute = R.effective_levels(
            judgement,
            register_cap=settings.register_cap,
            group_cap=settings.group_cap,
            cute_cap=settings.cute_cap,
            force_light=sticky,
        )
        if reg <= R.REG_OFF:
            return None
        if scene is None:
            cute = self._cute_after_history(cute, st)
        lib = library if library is not None else EX.load_examples(settings.examples_path)
        ch = channel or ("group" if is_group else "private")
        picked, anti = self._pick(lib, judgement.scene, ch, reg, cute, st)
        for ex in picked:
            st.recent_examples.append(ex.id)
        return StylePlan(
            scene=judgement.scene,
            label=judgement.label,
            register=reg,
            cute=cute,
            examples=picked,
            anti=anti,
            sticky=sticky,
        )

    # ------------------------------------------------------------ 文字

    def _tokens(self, text: str) -> int:
        if self._count_tokens is not None:
            return self._count_tokens(text)
        from ..utils.token_counter import count_tokens

        return count_tokens(text)

    def render(self, plan: StylePlan) -> str:
        head = [_HEADER, f"场景：{plan.label}。口吻：{R.REGISTER_WORD[plan.register]}。{_REGISTER_DESC[plan.register]}"]
        hint = R.SCENE_HINT.get(plan.scene)
        if plan.sticky and plan.scene != R.SCENE_FEEDBACK:
            hint = "这段对话里对方觉得你之前说话有点做作，保持正常说话，不提这件事。"
        if hint:
            head.append(hint)
        head.append(_CUTE_SCENE_LINE.get((plan.scene, plan.cute), _CUTE_LINE[plan.cute]))
        examples = list(plan.examples)
        anti = plan.anti

        def assemble() -> str:
            lines = list(head)
            if examples:
                lines.append(_REF_HEADER)
                lines.extend(_example_line(ex) for ex in examples)
            if anti is not None:
                reason = f"（{anti.anti_reason}）" if anti.anti_reason else ""
                lines.append(f"别这样：{anti.anti}{reason}")
            return "\n".join(lines)

        text = assemble()
        while self._tokens(text) > _STYLE_MAX_TOKENS and (anti is not None or examples):
            if anti is not None:
                anti = None
            else:
                examples.pop()
            text = assemble()
        return text

    # ------------------------------------------------------------ 公共入口

    def build_passive(self, scope: str, user_text: str, *, is_group: bool,
                      meta: Optional[Dict[str, Any]] = None,
                      settings: Optional[StyleSettings] = None) -> str:
        """被动回复的 style 段文本；不注入时返回 ""。"""
        if not scope:
            return ""
        settings = settings or read_settings()
        if not settings.enabled or self._paused():
            if meta is not None:
                meta["skipped"] = "paused" if settings.enabled else "disabled"
            return ""
        try:
            plan = self.plan(scope, user_text, is_group=is_group, settings=settings)
            if plan is None:
                if meta is not None:
                    meta["skipped"] = "register_off"
                self._ok()
                return ""
            text = self.render(plan)
            self._ok()
            if meta is not None:
                meta.update(_plan_meta(plan))
            logger.debug(
                f"{_LOG} scene={plan.scene} reg={R.REGISTER_NAME[plan.register]} "
                f"cute={R.CUTE_NAME[plan.cute]} ex={','.join(e.id for e in plan.examples) or '-'} "
                f"sticky={int(plan.sticky)}"
            )
            return text
        except Exception as e:
            self._failed("passive", e)
            if meta is not None:
                meta["error"] = type(e).__name__
            return ""

    def build_proactive(self, group_id: str, *, is_group: bool,
                        settings: Optional[StyleSettings] = None,
                        care_category: str = "") -> str:
        """主动消息的 style 段（固定"中"档，群聊再取群上限）；不注入时返回 ""。

        care_category 非空（群→私聊关心）：淡档、不给示例/反例；生病/情绪差/累/加班/危机不卖萌，
        好事最多"很少"——不让轻快的跟进式参考句压过关心的语气（审查 M2）。"""
        settings = settings or read_settings()
        if not (settings.enabled and settings.inject_proactive) or self._paused():
            return ""
        try:
            care = str(care_category or "")
            plan = self.plan(
                f"proactive:{group_id}", "", is_group=is_group, settings=settings,
                scene=R.SCENE_PROACTIVE, channel="proactive",
                library=() if care else None,
            )
            if plan is None:
                self._ok()
                return ""
            if care:
                cute = min(plan.cute, R.CUTE_RARE) if care == "good_news" else R.CUTE_OFF
                plan = StylePlan(
                    scene=plan.scene, label=plan.label, register=min(plan.register, R.REG_LIGHT),
                    cute=cute, examples=(), anti=None, sticky=plan.sticky,
                )
            text = self.render(plan)
            self._ok()
            logger.debug(
                f"{_LOG} proactive reg={R.REGISTER_NAME[plan.register]} "
                f"cute={R.CUTE_NAME[plan.cute]} ex={','.join(e.id for e in plan.examples) or '-'}"
            )
            return text
        except Exception as e:
            self._failed("proactive", e)
            return ""

    def observe_reply(self, scope: str, text: str) -> None:
        """被动回复定稿后记一次"这条是不是卖萌"（只记布尔，不存原文）。"""
        if not scope or not text:
            return
        try:
            st = self._state(scope)
            cute = any(pat.search(text) for _rid, pat in guard.CUTE)
            st.cute_flags.append(cute)
            if cute:
                st.segment_cute += 1
        except Exception as e:
            logger.debug(f"{_LOG} observe failed: {type(e).__name__}")

    def reset(self) -> None:
        self._scopes.clear()
        self._fail = 0
        self._paused_until = 0.0


def _example_line(ex: EX.StyleExample) -> str:
    user = ex.user
    if user.startswith("（") and user.endswith("）"):
        # 情形描述（主动开口、群里在聊什么），不当成"对方说的话"
        return f"- {user} / 回复：{ex.styled}"
    return f"- 对方：{user} / 回复：{ex.styled}"


def _plan_meta(plan: StylePlan) -> Dict[str, Any]:
    return {
        "scene": plan.scene,
        "register": R.REGISTER_NAME[plan.register],
        "cute": R.CUTE_NAME[plan.cute],
        "examples": [e.id for e in plan.examples],
        "anti": plan.anti.id if plan.anti is not None else "",
        "sticky": plan.sticky,
    }


_selector: Optional[StyleSelector] = None


def get_selector() -> StyleSelector:
    global _selector
    if _selector is None:
        _selector = StyleSelector()
    return _selector


def scope_key(event: Any) -> str:
    """会话键：群聊用群号，私聊用会话 id（与 learning 的分组键一致）。"""
    try:
        from ..platform import get_adapter

        adapter = get_adapter(event)
        return str(adapter.get_group_id(event) or adapter.get_session_id(event) or "")
    except Exception:
        return ""


def is_group_event(event: Any) -> bool:
    try:
        from ..platform import get_adapter

        return bool(get_adapter(event).get_group_id(event))
    except Exception:
        return False


def style_enabled() -> bool:
    return read_settings().enabled


def proactive_style_enabled() -> bool:
    s = read_settings()
    return s.enabled and s.inject_proactive


def observe_event_reply(event: Any, text: str) -> None:
    """main.py 装饰阶段调用：开关关闭时什么都不做。"""
    if not text or not style_enabled():
        return
    scope = scope_key(event)
    if scope:
        get_selector().observe_reply(scope, text)
