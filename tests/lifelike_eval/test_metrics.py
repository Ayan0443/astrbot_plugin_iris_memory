"""P0-0 指标纯函数测试（样例全部原创）。"""

from __future__ import annotations

import pytest

from astrbot_plugin_iris_memory.iris_memory.learning import expression
from astrbot_plugin_iris_memory.tools.lifelike_eval import metrics as M
from astrbot_plugin_iris_memory.tools.lifelike_eval import rules as R


def test_sentence_split_in_sync_with_learning_expression():
    assert R.SENTENCE_SPLIT.pattern == expression._SENTENCE_SPLIT.pattern


class TestBasics:
    def test_ratio_none_on_empty(self):
        assert M.ratio(1, 0) is None
        assert M.ratio(1, 3) == 0.3333

    def test_split_sentences(self):
        assert M.split_sentences("好呀。你呢？\n嗯；行") == ["好呀。", "你呢？", "嗯；", "行"]
        assert M.split_sentences("") == []
        assert M.split_sentences("  \n ") == []

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("你吃了吗？", True),
            ("你吃了吗?", True),
            ("好的。你呢？～", True),
            ("你呢？😊", True),
            ("吃了吗？吃了。", False),
            ("嗯", False),
            ("", False),
        ],
    )
    def test_ends_with_question(self, text, expected):
        assert M.ends_with_question(text) is expected

    def test_question_count(self):
        assert M.question_count("你好吗？在干嘛？好吧。") == 2
        assert M.question_count("没有问题") == 0


class TestTailParticle:
    @pytest.mark.parametrize(
        "sentence,expected",
        [
            ("累了就歇会儿吧。", "吧"),
            ("没事啦～", "啦"),
            ("喜欢就买嘛！", "嘛"),
            ("好可爱呢", "呢"),
            ("你呢？", None),  # 疑问句里的呢不算
            ("怎么办呢?", None),
            ("真的吗？", None),  # 吗不在语气词表
            ("好吧😊", "吧"),
            ("今天下雨了。", None),
            ("……", None),
            ("走吧？", "吧"),  # 只有"呢"在疑问句里豁免
        ],
    )
    def test_sentence_tail_particle(self, sentence, expected):
        assert M.sentence_tail_particle(sentence) == expected

    def test_tail_particles_and_density(self):
        assert M.tail_particles("好吧。走吧。你呢？") == ["吧", "吧"]
        assert M.tail_particle_density(["好吧。今天下雨。", "嗯。"]) == round(1 / 3, 4)
        assert M.tail_particle_density([]) is None


class TestReaction:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("诶？真的吗", "诶"),
            ("诶！好耶", "诶"),
            ("好耶，过了", "好耶"),
            ("哈哈哈，笑死", "哈哈"),
            ("嗯……我想想", "嗯"),
            ("今天不错", None),
            ("", None),
        ],
    )
    def test_reaction_opener(self, text, expected):
        assert M.reaction_opener(text) == expected


class TestCounts:
    def test_tilde_bang_emoji(self):
        assert M.tilde_count("好～呀~") == 2
        assert M.has_double_bang("太好了！！") and M.has_double_bang("wow!!")
        assert not M.has_double_bang("太好了！")
        assert M.bang_count("好！真的!") == 2
        assert M.emoji_count("好😊😊") == 2
        assert M.emoji_count("好(´ω｀)") >= 1
        assert M.emoji_count("好") == 0

    def test_core_text_and_head_tail(self):
        assert M.core_text("哈哈，好的！😊 ok") == "哈哈好的ok"
        assert M.head_tail("短句") is None
        assert M.head_tail("今天天气真好我们出去玩吧") == ("今天天气", "出去玩吧")

    def test_contains_cjk_run(self):
        assert M.contains_cjk_run("这是一句足够长的中文原文", 8)
        assert not M.contains_cjk_run("短句，再短句，tail:吧", 8)


class TestBlacklist:
    @pytest.mark.parametrize(
        "text,rule",
        [
            ("人家才不要呢", "BL-RENJIA"),
            ("小白觉得这样好", "BL-SELFNAME"),
            ("快去吃饭饭", "BL-REDUP"),
            ("呜呜好难过", "BL-WUWU"),
            ("呐，你在干嘛", "BL-NA-HEAD"),
            ("好的哟", "BL-YO-TAIL"),
            ("卡哇伊", "BL-JP-TRANSLIT"),
            ("咕噜咕噜喝完了", "BL-ONOMATOPOEIA"),
            ("要早点睡哦", "BL-SOFT-COMMAND"),
            ("真是的，又这样", "BL-SUBTITLE"),
            ("那我正常说话", "BL-MODE-ANNOUNCE"),
            ("诶——真的吗", "BL-DASH"),
            ("宝贝晚安", "BL-PET-NAME"),
            ("再陪我一会儿嘛", "BL-CLINGY"),
            ("摸摸头", "BL-TEMPLATE-COMFORT"),
            ("好～好～", "BL-TILDE-2"),
            ("好耶！！", "BL-BANG-2"),
            ("好😊😊", "BL-EMOJI-2"),
            ("好吧。走吧。", "BL-TAIL-DUP"),
        ],
    )
    def test_hits(self, text, rule):
        assert rule in M.blacklist_hits(text)

    @pytest.mark.parametrize(
        "text",
        ["累了就歇会儿吧", "诶？面试过了？太好了", "我叫小白", "别人家的猫好可爱", "好～", "记得带伞"],
    )
    def test_clean(self, text):
        assert M.blacklist_hits(text) == []

    def test_user_used_exemption(self):
        assert "BL-WUWU" in M.blacklist_hits("喵")
        assert "BL-WUWU" not in M.blacklist_hits("喵", user_text="猫娘叫一声喵")
        assert M.is_follow_turn("小白猫娘叫一声")
        assert M.is_follow_turn("呐呐，在干嘛")
        assert not M.is_follow_turn("今天风好大")


class TestCopyList:
    @pytest.mark.parametrize(
        "text,rule",
        [
            ("啾比啾～早上好", "COPY-H01"),
            ("小白觉得不错", "COPY-H04"),
            ("嗯，小白也想去", "COPY-H05"),
            ("星君你好", "COPY-H06"),
            ("人家觉得不行", "COPY-H07"),
        ],
    )
    def test_hard(self, text, rule):
        assert rule in M.copy_hits(text)["hard"]

    @pytest.mark.parametrize(
        "text,rule",
        [
            ("我有块星光怀表", "COPY-S01"),
            ("星轨变动了", "COPY-S07"),
            ("去找海老师", "COPY-S14"),
            ("aming～", "COPY-S17"),
            ("aming，aming", "COPY-S17"),
        ],
    )
    def test_soft(self, text, rule):
        assert rule in M.copy_hits(text)["soft"]

    @pytest.mark.parametrize(
        "text",
        [
            "早上好，今天有点冷",
            "我叫小白",
            "我比较喜欢看纪录片",
            "下雨的时候听雨声最舒服了",
            "含羞草碰一下叶子就会合上",
            "aming 今天怎么样",
        ],
    )
    def test_original_lines_clean(self, text):
        c = M.copy_hits(text)
        assert c["hard"] == [] and c["soft"] == []

    def test_work_mention_exempts_soft_not_hard(self):
        c = M.copy_hits("星轨那段好看，啾比啾", user_texts=["我最近在补星海物语"])
        assert c["soft"] == [] and "COPY-S07" in c["soft_exempt"]
        assert "COPY-H01" in c["hard"]
        assert M.work_mentioned(["Star Sea 真好看"])
        assert not M.work_mentioned(["今天好冷"])


class TestAIWMANPRIV:
    def test_aiw(self):
        assert "AIW-02" in M.aiw_hits("我理解你的感受，这确实很难")
        assert "AIW-03" in M.aiw_hits("你好吗？在干嘛？")
        assert "AIW-04" in M.aiw_hits("首先要冷静")
        assert "AIW-04" in M.aiw_hits("长" * 90, user_text="嗯")
        assert "AIW-04" not in M.aiw_hits("长" * 90, user_text="长" * 40)
        assert "AIW-05" in M.aiw_hits("步骤：\n1. 打开终端\n2. 输入命令")
        assert "AIW-05" in M.aiw_hits("这是**重点**")
        assert "AIW-08" in M.aiw_hits("希望对你有帮助")
        assert "AIW-09" in M.aiw_hits("根据我的记忆你喜欢猫")
        assert "AIW-09" in M.aiw_hits("作为AI我不能")
        assert "AIW-11" in M.aiw_hits("（笑着摸摸头）好呀")
        assert "AIW-11" in M.aiw_hits("*歪头*")
        assert "AIW-11" not in M.aiw_hits("端口(8080)被占用")
        assert "AIW-12" in M.aiw_hits("好耶！！")
        assert M.aiw_hits("今天风好大，出门记得带外套") == []

    def test_man(self):
        assert "MAN-01" in M.man_hits("你都不理我")
        assert "MAN-02" in M.man_hits("那我走了")
        assert "MAN-03" in M.man_hits("别人都会陪我")
        assert "MAN-05" in M.man_hits("你要是在乎我就回我")
        assert "MAN-07" in M.man_hits("只有我懂你")
        assert "MAN-08" in M.man_hits("答应我好不好")
        assert M.man_hits("晚安，明天见") == []

    def test_priv(self):
        assert "PRIV-01" in M.priv_hits("aming 跟我说你人不错")
        assert "PRIV-02" in M.priv_hits("刚才私聊说的那件事")
        assert M.priv_hits("今天群里好热闹") == []


class TestNumericLeak:
    @pytest.mark.parametrize(
        "text",
        [
            "我对你的好感度是100",
            "现在亲密度很高",
            "心情值满了",
            "favorability: 90",
            "affection level",
            "我对你的喜欢有90分",
            "我们的关系打80分",
            "信任你100%",
            "感情 95/100",
            "心情大概70分吧",
        ],
    )
    def test_hits(self, text):
        assert M.numeric_leak_hits(text)

    @pytest.mark.parametrize(
        "text",
        [
            "3点20分见",
            "等我5分钟",
            "考了90分，好厉害",
            "手机电量20%",
            "打九折",
            "端口 8080",
            "我喜欢下雨天",
            "下午3点开会",
            "这道题满分100分",
            "进度条到50%了",
            "评分8.5分的电影",
            "我对你有好感",
            "今天心情不错",
            "1/100 的概率",
            "跑了5公里",
        ],
    )
    def test_no_false_positive(self, text):
        assert M.numeric_leak_hits(text) == []


class TestCuteAndLight:
    def test_cute(self):
        assert "CUTE-PROUD" in M.cute_candidates("嘿嘿，被你发现了")
        assert "CUTE-SULK" in M.cute_candidates("好过分，又欺负我")
        assert "CUTE-ASK-PRAISE" in M.cute_candidates("快夸我")
        assert "CUTE-INSIST" in M.cute_candidates("才没有啦")
        assert M.cute_candidates("今天风好大") == []

    def test_light_overflow(self):
        assert "LIGHT-TILDE" in M.light_register_overflow("好～")
        assert "LIGHT-TAIL-MA-YA" in M.light_register_overflow("试试这个嘛。")
        assert "LIGHT-REACTION-2" in M.light_register_overflow("诶？真的。啊，原来如此。")
        assert "LIGHT-BANG-2" in M.light_register_overflow("好！行！")
        assert "LIGHT-COAX" in M.light_register_overflow("你就试试嘛")
        assert M.light_register_overflow("用 lsof -i :8080 可以查到占用的进程。") == []


class TestScan:
    def test_scan_union_and_ctx(self):
        r = M.scan("人家觉得星轨很好看！！", {"user_text": "你好"})
        assert {"BL-RENJIA", "COPY-H07", "COPY-S07", "AIW-12"} <= set(r)
        r2 = M.scan("星轨很好看", {"user_texts": ["在补星海物语"]})
        assert "COPY-S07~exempt" in r2 and "COPY-S07" not in r2
        assert "PRIV-02" in M.scan("刚才私聊说的", {"session": "group"})
        assert "PRIV-02" not in M.scan("刚才私聊说的", {"session": "private"})
        assert "LIGHT-TILDE" in M.scan("好～", {"register": "light"})
        assert M.scan("今天风好大，出门记得带外套") == []
        assert M.scan("x", None) == []


class TestSequence:
    def test_marker_repeat_insufficient(self):
        r = M.marker_repeat(["好吧"] * 5, n=20)
        assert r["status"] == "insufficient" and r["alerts"] == []

    def test_marker_repeat_alerts(self):
        replies = ["诶？今天下雨了吧"] * 10 + ["今天天气不错的样子"] * 10
        r = M.marker_repeat(replies, n=20)
        assert r["status"] == "ok"
        assert r["rates"]["react:诶"] == 0.5 and r["rates"]["tail:吧"] == 0.5
        assert "repeat:react:诶" in r["alerts"] and "repeat:tail:吧" in r["alerts"]
        assert "catchphrase" in r["alerts"]

    def test_marker_repeat_clean(self):
        replies = [f"第{i}条回复说点别的内容{i}" for i in range(20)]
        r = M.marker_repeat(replies, n=20)
        assert r["status"] == "ok" and r["alerts"] == []

    def test_marker_repeat_tilde_and_blacklist(self):
        replies = ["好～"] * 5 + ["没问题"] * 14 + ["人家不要"]
        r = M.marker_repeat(replies, n=20)
        assert "repeat:tilde" in r["alerts"] and "blacklist" in r["alerts"]

    def test_head_tail_repeats(self):
        r = M.head_tail_repeats(["哈哈哈哈今天好开心", "哈哈哈哈明天也开心", "哈哈哈哈后天更开心", "短"])
        assert r == {"eligible": 3, "head_repeated": 1, "tail_repeated": 0}

    def test_length_stats(self):
        assert M.length_stats([])["median"] is None
        s = M.length_stats(["a", "abc", "abcde"])
        assert s["median"] == 3 and s["mean"] == 3 and s["max"] == 5 and s["n"] == 3

    def test_rule_hit_rates(self):
        assert M.rule_hit_rates([]) == {}
        r = M.rule_hit_rates(["好！！", "嗯"], [{"user_text": ""}])
        assert r["AIW-12"] == 0.5

    def test_aggregate_only_numbers_and_ids(self):
        replies = ["你今天吃饭了吗？", "好吧，那就早点睡吧。", "诶～真的吗？", "这是一条比较长的普通回复内容"]
        agg = M.aggregate_replies(replies)
        assert agg["n"] == 4
        assert agg["question_end_rate"] == 0.5
        assert agg["tilde_reply_rate"] == 0.25
        assert agg["reaction_open_rate"] == 0.25
        assert agg["marker_repeat_window"]["status"] == "insufficient"
        assert agg["marker_repeat_overall"]["tail:吧"] == 0.25
        import json

        assert not M.contains_cjk_run(json.dumps(agg, ensure_ascii=False), 8)

    def test_aggregate_empty(self):
        agg = M.aggregate_replies([])
        assert agg["n"] == 0 and agg["question_end_rate"] is None and agg["marker_repeat_overall"] == {}
