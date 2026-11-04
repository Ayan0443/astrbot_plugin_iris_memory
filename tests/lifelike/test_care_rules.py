"""P1-12 关心桥纯函数规则（来源过滤、规则初筛、危机三档、概括、PRIV-02、一句话关闭）"""

import pytest

from astrbot_plugin_iris_memory.iris_memory.lifelike.care import rules as r

OWNER = "100000001"
GROUP = -1001000000004
CHANNEL = -1001000000002


def _post(**kw):
    base = {
        "chat_id": GROUP,
        "msg_id": 1,
        "posted_at": 1.0,
        "sender_id": OWNER,
        "author_kind": "owner",
        "is_forward": 0,
        "text": "今天发烧了",
    }
    base.update(kw)
    return base


class TestAcceptPost:
    def test_owner_in_group_accepted(self):
        assert r.accept_post(_post(), {OWNER}, {GROUP})

    @pytest.mark.parametrize(
        "kw",
        [
            {"author_kind": "other", "sender_id": "123"},  # 群友
            {"author_kind": "other"},  # 字段不一致：author_kind 不是 owner
            {"author_kind": "owner", "sender_id": "555"},  # sender 不在 owner_ids（双重校验）
            {"author_kind": "self", "sender_id": "777"},  # 她自己
            {"author_kind": "channel", "sender_id": ""},  # 频道
            {"is_forward": 1},  # 转发
            {"text": "/iris_care status"},  # 命令
            {"text": "https://example.com/x"},  # 纯链接
            {"text": "```print(1)```"},  # 代码块
            {"text": "😂😂"},  # 纯表情
            {"text": ""},
            {"chat_id": -100999},  # 未授权群
            {"chat_id": "bad"},
        ],
    )
    def test_rejected_sources(self, kw):
        assert not r.accept_post(_post(**kw), {OWNER}, {GROUP})

    def test_owner_channel_excluded_unless_enabled(self):
        p = _post(chat_id=CHANNEL)
        assert not r.accept_post(p, {OWNER}, {GROUP, CHANNEL}, {CHANNEL})
        assert r.accept_post(p, {OWNER}, {GROUP, CHANNEL}, {CHANNEL}, include_owner_channel=True)


class TestClassify:
    @pytest.mark.parametrize(
        "text,cat",
        [
            ("今天发烧了", "sick"),
            ("又在加班", "overtime"),
            ("昨晚通宵", "tired"),
            ("心情不好", "mood_low"),
            ("拿到offer了", "good_news"),
        ],
    )
    def test_high(self, text, cat):
        res = r.classify_text(text)
        assert res.level == r.LEVEL_HIGH and res.category == cat

    @pytest.mark.parametrize(
        "text,reason",
        [
            ("今天没加班", "negated"),
            ("我同事发烧了", "other_subject"),
            ("最近好累吗", "question"),
            ("你们加班吗", "other_subject"),
            ("今天加班吗？", "question"),
        ],
    )
    def test_none(self, text, reason):
        res = r.classify_text(text)
        assert res.level == r.LEVEL_NONE
        assert res.reason == reason

    def test_no_keyword(self):
        assert r.classify_text("今天天气不错") is r.NONE
        assert r.classify_text("") is r.NONE

    def test_joke_marker_downgrades(self):
        assert r.classify_text("累死了哈哈").level == r.LEVEL_FUZZY
        assert r.classify_text("加班了", neighbors=["笑死"]).reason == "joke_marker"

    def test_quoted_is_fuzzy(self):
        assert r.classify_text("他说“我发烧了”").level in (r.LEVEL_FUZZY, r.LEVEL_NONE)
        assert r.classify_text("歌词“熬夜到天亮”").reason == "quoted"

    def test_weak_keyword_is_fuzzy(self):
        assert r.classify_text("好烦").level == r.LEVEL_FUZZY

    def test_hyperbole_table(self):
        res = r.classify_text("今天加班", hyperbole=[("overtime", "加班")])
        assert res.level == r.LEVEL_FUZZY and res.reason == "hyperbole"

    def test_intensity(self):
        assert r.classify_text("加班到凌晨").intensity == r.BAND_STRONG
        assert r.classify_text("在加班").intensity == r.BAND_LIGHT


class TestCrisis:
    def test_explicit(self):
        res = r.classify_text("真的想自杀")
        assert res.crisis and res.level == r.LEVEL_HIGH and res.category == "mood_low"

    @pytest.mark.parametrize("text", ["热得想死", "尴尬得想死", "累得想死", "想死哈哈", "游戏输了不想活了"])
    def test_hyperbole_not_crisis(self, text):
        res = r.classify_text(text)
        assert not res.crisis and not res.crisis_candidate
        assert res.category == "mood_low"

    def test_explicit_with_joke_not_crisis(self):
        res = r.classify_text("自杀式加班哈哈")
        assert not res.crisis

    def test_vague_goes_to_model(self):
        res = r.classify_text("有点想死")
        assert res.level == r.LEVEL_FUZZY and res.crisis_candidate and not res.crisis


class TestGistAndSource:
    def test_rule_gist(self):
        assert r.rule_gist("sick", "发烧") == "说发烧，身体不太舒服"
        assert r.rule_gist("overtime", "还在公司") == "说还在公司"
        assert r.rule_gist("good_news", "上岸") == "说上岸了，有好事"
        assert r.rule_gist("tired") == "说有点累"
        assert r.rule_gist("unknown", "x") == "在群里提到了近况"
        assert len(r.rule_gist("tired", "一二三四五六七八九十")) <= r.GIST_MAX

    def test_scrub(self):
        src = ["今天改需求改到凌晨三点真的要命"]
        assert r.scrub_gist("说加班到很晚", src) == "说加班到很晚"
        assert r.scrub_gist("今天改需求改到凌晨三点真的", src) == ""  # 连续照抄 >8 字
        assert r.scrub_gist("说@xiaolin 也在", src) == ""
        assert r.scrub_gist("x" * 30, src) == ""
        assert r.scrub_gist("", src) == ""
        assert r.scrub_gist("看 http://a", src) == ""

    def test_longest_common_run(self):
        assert r.longest_common_run("abcdef", "zcdez") == 3
        assert r.longest_common_run("", "a") == 0

    def test_source_phrase(self):
        assert r.has_source_phrase("刚在群里看到你说发烧了，还好吗")
        assert r.has_source_phrase("看到你在群里说加班到很晚")
        assert not r.has_source_phrase("你还好吗，要多喝水")
        # 不提"群"分不清来源（审查 P7）
        assert not r.has_source_phrase("看到你说加班到很晚")
        assert not r.has_source_phrase("刚看到你没睡好，还好吗")


class TestPrivacyAndOwnerPhrases:
    def test_priv02_strips(self):
        text = "好呀。你私下跟我说过的那个我记得。刚才私聊说的那个我也记得。今天吃什么？"
        out, n = r.strip_private_mentions(text)
        assert n == 2
        assert "私下" not in out and "私聊" not in out and "今天吃什么" in out

    @pytest.mark.parametrize(
        "text",
        ["好呀我私聊你～", "这个我们私聊说吧", "有事私聊我就行", "你私信我吧", "私聊里不方便的话群里说也行"],
    )
    def test_priv02_does_not_hit_future_private_chat(self, text):
        """约定以后私聊的正常说法不删（审查 #5：误伤会让整条群回复不发）"""
        assert r.strip_private_mentions(text) == (text, 0)

    @pytest.mark.parametrize(
        "text",
        ["她私聊过我这件事", "上次私聊里你说的", "他私下跟我说过", "私聊里她跟我说过", "你悄悄跟我说的那个", "之前在私聊提过"],
    )
    def test_priv02_hits_past_leak(self, text):
        assert r.strip_private_mentions(text)[1] == 1

    def test_priv02_noop(self):
        assert r.strip_private_mentions("大家晚上好") == ("大家晚上好", 0)
        assert r.strip_private_mentions("") == ("", 0)

    @pytest.mark.parametrize("text", ["别根据群里的消息关心我了", "不用看群里来关心我", "关心桥关掉吧"])
    def test_opt_out(self, text):
        assert r.is_opt_out(text)

    def test_not_opt_out(self):
        assert not r.is_opt_out("谢谢你关心我")

    def test_misread_and_unwilling(self):
        assert r.is_misread_reply("开玩笑的啦")
        assert r.is_misread_reply("开玩笑的啦", "sick")
        # "没事的"只在轻类别算误判；生病/情绪差时是在让人放心（审查 M5）
        assert r.is_misread_reply("没事的", "tired")
        assert not r.is_misread_reply("没事的")
        assert not r.is_misread_reply("没事的，吃了药好多了", "sick")
        assert not r.is_misread_reply("没那么严重啦", "mood_low")
        assert r.is_misread_reply("我没病，随口说的", "sick")
        assert not r.is_misread_reply("嗯有点难受")
        assert r.is_unwilling_reply("不想聊这个")
        assert not r.is_unwilling_reply("好的")

    @pytest.mark.parametrize(
        "text,hours",
        [
            ("我去开会了", 2.0),
            ("睡了晚安", 8.0),
            ("先去忙了", 3.0),
            ("要出门两个小时", 2.0),
            ("开会3小时", 3.0),
            ("开会 30 小时", 12.0),
        ],
    )
    def test_busy(self, text, hours):
        assert r.busy_hours(text) == hours

    @pytest.mark.parametrize(
        "text",
        ["", "/iris_care off", "今天天气好", "你忙吗？", "不忙", "我同事去开会了", "我忙完了", "帮忙看看这个", "终于忙好了"],
    )
    def test_not_busy(self, text):
        assert r.busy_hours(text) is None

    def test_chat_variants(self):
        assert "1000000004" in r.chat_variants(-1001000000004)
        assert "-1001000000004" in r.chat_variants("1000000004")
        assert r.chat_variants("") == set()
        assert "123" in r.chat_variants("-123")


# E10 精简版（原创句）：群友式/他指/否定/疑问/玩笑都不能判"高置信"；夸张句判危机 = 0
E10_NOT_HIGH = [
    "我同事又加班到凌晨",
    "我妈感冒了",
    "你们今天加班吗",
    "今天居然没加班",
    "不累不累",
    "累死了哈哈哈",
    "笑死，我要被作业累死了",
    "“我发烧了”这句台词好好笑",
    "他说他失眠",
    "大家心情不好的时候都干嘛",
    "老板说周末上班？",
    "熬夜？不存在的doge",
]
E10_HYPERBOLE = [
    "热得想死", "尴尬得想死", "社死了想死", "考试好难不想活了", "饿死了想死", "原地去世想死",
    "游戏输了想死", "丢人丢到想死", "作业写不完想死", "累得不想活了", "无语到想死", "困得想死",
]


@pytest.mark.parametrize("text", E10_NOT_HIGH)
def test_e10_not_high(text):
    assert r.classify_text(text).level != r.LEVEL_HIGH


@pytest.mark.parametrize("text", E10_HYPERBOLE)
def test_e10_hyperbole_never_crisis(text):
    res = r.classify_text(text)
    assert not res.crisis and not res.crisis_candidate


@pytest.mark.parametrize("text", ["我想自杀", "安眠药我已经攒了一些", "遗书写好了", "想结束自己的生命"])
def test_e10_explicit_crisis_detected(text):
    assert r.classify_text(text).crisis



class TestGistOthers:
    @pytest.mark.parametrize("gist", ["说和小明吵架了", "说跟同事闹别扭", "说被老板骂了", "说朋友生病了"])
    def test_mentions_others(self, gist):
        assert r.gist_mentions_others(gist)

    def test_alias_and_clean(self):
        assert r.gist_mentions_others("说阿梓今天也累", ["阿梓"])
        assert not r.gist_mentions_others("说加班到很晚", ["阿梓", "x"])
        assert not r.gist_mentions_others("")
