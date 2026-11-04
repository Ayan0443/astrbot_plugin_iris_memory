"""P1-8 硬闸门纯函数：每个分支都有用例（ROADMAP P1-8 验收①）"""

from dataclasses import replace

import pytest

from astrbot_plugin_iris_memory.iris_memory.lifelike.initiative import gate as g

NOW = 1_800_000_000.0


def ctx(**kw):
    base = g.GateContext(now=NOW, ctype="owner_post")
    return replace(base, **kw)


def care(**kw):
    kw.setdefault("ctype", g.CTYPE_GROUP_CARE)
    kw.setdefault("care_category", "sick")
    return ctx(**kw)


def test_ok_default():
    assert g.check(ctx()) == g.GateResult(True, g.G_OK)


@pytest.mark.parametrize(
    "kw,reason",
    [
        ({"scope_is_owner_private": False}, g.G_PRIVACY),
        ({"ctype": g.CTYPE_GROUP_CARE, "privacy": g.PRIVACY_TOPIC_ONLY}, g.G_PRIVACY),
        ({"privacy": g.PRIVACY_TOPIC_ONLY, "privacy_ok": False}, g.G_PRIVACY),
        ({"delivery": g.DELIVERY_PASSIVE}, g.G_DELIVERY),
        ({"expires_at": NOW}, g.G_EXPIRED),
        ({"not_before": NOW + 1}, g.G_NOT_YET),
        ({"busy_until": NOW + 10}, g.G_BUSY),
        ({"budget_exhausted": True}, g.G_BUDGET),
        ({"dedupe_used": True}, g.G_DUP),
        ({"same_ctype_sent_24h": True}, g.G_DUP),
        ({"ctype_cooldown_until": NOW + 5}, g.G_COOLDOWN),
        ({"last_push_unreplied": True, "last_push_ctype": "owner_post"}, g.G_BACKOFF),
        ({"push_sent_24h": 3}, g.G_CAP),
        ({"push_daily_cap": 0}, g.G_CAP),
        ({"last_push_at": NOW - 3600}, g.G_GAP),
    ],
)
def test_blocks(kw, reason):
    assert g.check(ctx(**kw)) == g.GateResult(False, reason)


def test_privacy_topic_only_ok():
    assert g.check(ctx(privacy=g.PRIVACY_TOPIC_ONLY, privacy_ok=True)).ok


@pytest.mark.parametrize("mode", [g.RELATION_PAUSE, g.RELATION_SOFTEN, "boundary"])
@pytest.mark.parametrize("ctype", [g.CTYPE_FOLLOWUP, "life_share", "owner_post", "moment_followup"])
def test_relation_mode_never_blocks_u8(mode, ctype):
    """U8：关系模式 / 冷静期不减少主动（不冷落），任何类型都不被 G-mode 拦"""
    assert g.check(ctx(ctype=ctype, relation_mode=mode)).ok


def test_soften_allows_followup():
    assert g.check(ctx(ctype=g.CTYPE_FOLLOWUP, relation_mode=g.RELATION_SOFTEN)).ok
    assert g.check(ctx(ctype=g.CTYPE_FOLLOWUP, budget_exhausted=True)).ok


def test_busy_expired_passes():
    assert g.check(ctx(busy_until=NOW - 1)).ok


def test_gap_doubles_after_unreplied():
    # 3h 间隔，上次未回复 → 需要 6h；换类型不触发 backoff
    c = ctx(last_push_at=NOW - 4 * 3600, last_push_unreplied=True, last_push_ctype="life_share")
    assert g.check(c) == g.GateResult(False, g.G_GAP)
    assert g.check(replace(c, last_push_at=NOW - 7 * 3600)).ok
    assert g.check(replace(c, last_push_unreplied=False)).ok


def test_backoff_needs_ctype():
    assert g.check(ctx(last_push_unreplied=True, last_push_ctype="")).ok


class TestCareRelease:
    @pytest.mark.parametrize("mode", [g.RELATION_PAUSE, g.RELATION_SOFTEN])
    def test_sick_released(self, mode):
        assert g.check(care(relation_mode=mode)).ok

    def test_mood_low_strong_released(self):
        assert g.check(care(care_category="mood_low", care_intensity="明显", relation_mode=g.RELATION_PAUSE)).ok

    def test_mood_low_light_not_blocked_by_relation(self):
        res = g.check(care(care_category="mood_low", care_intensity="轻", relation_mode=g.RELATION_PAUSE))
        assert res.ok and not g.care_release(care(care_category="mood_low", care_intensity="轻"))

    def test_good_news_not_blocked_by_relation(self):
        assert g.check(care(care_category="good_news", relation_mode=g.RELATION_SOFTEN)).ok

    def test_crisis_released_and_helper(self):
        c = care(care_category="mood_low", crisis=True)
        assert g.care_release(c)
        assert not g.care_release(ctx())


class TestCareCaps:
    def test_daily(self):
        assert g.check(care(care_sent_today=1)) == g.GateResult(False, g.G_CARE_CAP)

    def test_weekly(self):
        assert g.check(care(care_sent_week=3)) == g.GateResult(False, g.G_CARE_CAP)

    def test_category_cooldown(self):
        assert g.check(care(care_category_cooldown_until=NOW + 1)) == g.GateResult(False, g.G_CARE_COOLDOWN)
        assert g.check(care(care_category_cooldown_until=NOW - 1)).ok


class TestCrisis:
    def test_crisis_bypasses_caps_but_not_privacy_or_dedupe(self):
        c = care(
            crisis=True, care_category="mood_low", push_sent_24h=9, care_sent_today=5, last_push_at=NOW - 1,
            same_ctype_sent_24h=True, busy_until=NOW + 100, budget_exhausted=True,
            last_push_unreplied=True, last_push_ctype=g.CTYPE_GROUP_CARE,
        )
        assert g.check(c).ok
        assert g.check(replace(c, scope_is_owner_private=False)) == g.GateResult(False, g.G_PRIVACY)
        assert g.check(replace(c, dedupe_used=True)) == g.GateResult(False, g.G_DUP)

    def test_crisis_once_per_24h(self):
        assert g.check(care(crisis=True, crisis_sent_24h=True)) == g.GateResult(False, g.G_CRISIS)


def test_all_reasons_listed():
    assert len(set(g.ALL_REASONS)) == len(g.ALL_REASONS)
