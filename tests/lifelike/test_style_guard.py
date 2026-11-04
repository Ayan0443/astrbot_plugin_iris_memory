"""StyleGuard 规则表单元测试（ROADMAP P0-6 验收①）

- 每条规则至少 3 个命中样例 + 3 个不命中样例；
- §12.4 硬拦截正则对口吻附录 §3.2 / §3.1 全部原创示例命中 = 0，对构造的照搬样例命中率 = 100%；
- learning 闸门：跟随一轮 / 黑名单 / 硬拦截 / 卖萌初筛拦截，软提示不拦。
"""

from __future__ import annotations

import pytest

from astrbot_plugin_iris_memory.iris_memory.lifelike.style import guard
from astrbot_plugin_iris_memory.iris_memory.lifelike.style.guard import (
    ReplyMarkers,
    extract_markers,
    learning_gate,
    rule_tags,
    scan,
)

# 口吻附录 §3.2 动漫口吻回复（全部原创；(对方说, 回复)）+ §3.1 人设"刚好"示例
STYLED_EXAMPLES = [
    ("我刚发现楼下新开了家甜品店", "哇，新开的……招牌是什么呀？看着好吃的话，挑个周末去试试。"),
    ("你今天说话好可爱", "怎么突然夸我……有点不好意思，不过好开心。"),
    ("我觉得第二部比第一部好看多了", "我还是更喜欢第一部嘛，第二部的结局赶得太明显了。你说说第二部哪里好？"),
    ("我想通宵把这个游戏打完", "啊？明天会困死的。不过真想一口气打完就打吧，打到哪了跟我说说。"),
    ("今天被领导当着全组的面骂了", "当着全组的面骂你？太过分了，听着都替你委屈。现在缓过来一点没？"),
    ("Python 里怎么读一个 JSON 文件？",
     "嗯，用标准库 json 就行：\n1. 用 open 打开文件；\n2. 用 json.load 读进来，得到的是字典或列表。\n读的时候报错的话，把报错发我看看。"),
    ("（群友在比谁的周一更惨）", "哈哈哈，比惨大会开起来了，冠军有奖品吗？"),
    ("你是不是又反应慢了", "才没有啦……好吧，大概慢了一拍。"),
    ("在吗", "在呢，你回来啦。"),
    ("我抢到演唱会门票了！", "诶，真抢到了？太好了，我听着都跟着开心起来了！是哪天的场？"),
    ("你今天怎么这么嗲", "哈哈，被你听出来了。"),
    ("你觉得远程办公好吗？", "我觉得利大于弊，主要是能省下通勤时间。一个人在家是会有点闷，不过我还是站远程这边。"),
    ("本王今晚要通宵征服这个副本", "好的，王。这个副本难打吗？打赢了回来报一下战况。"),
    ("今天开了六个会，不过都还好", "六个会……光听着就累。你说还好那就好，要不今晚就随便聊聊，不说正事了？"),
    ("我把阳台那盆薄荷救活了，之前差点枯死", "嗯……救活啦？好厉害。话说薄荷原来也会差点枯死啊，我一直以为它怎么养都长得好。"),
    ("你对什么东西特别着迷？", "拼图！一拼就停不下来……是不是说太多了，哈哈。"),
    ("算了，不想说了", "好，不说了。想聊点别的就聊，不想说话也没关系。"),
    ("拖了半年的牙终于看完了", "看完啦？恭喜！拖了半年，总算不用再惦记了。"),
    ("（两个群友在争看电影前要不要先看剧透）", "我站不看剧透那边……知道结局以后，看的时候就少了一半的紧张。"),
    ("你好聪明啊", "真的？那你再多夸两句，我还挺爱听的。"),
    ("我决定以后都不理你了哈哈", "啊？那我只好一个人委屈一会儿了……真不理我啦？"),
    ("我把键盘修好了", "诶，真修好啦？也太厉害了吧！"),
    ("还在弄那个bug", "还在弄啊……困了就先睡吧，明天再弄也来得及。"),
]
CUTESY_EXAMPLE_IDX = {7, 19, 20}  # 第 8、20、21 组：卖萌示例 / 玩笑里的小坚持

# 普通、无风格问题的回复（不命中样例的公共池）
PLAIN_NEGATIVES = [
    "好，到时候我问你。",
    "今天天气不错，适合出去走走。",
    "这个我也没想到，挺意外的。",
    "我更喜欢第一部，第二部结局太赶。",
    "周五那个面试准备得怎么样了",
    "刚看完那部电影，结局还挺好的。",
]
NEGATIVE_POOL = [r for _, r in STYLED_EXAMPLES] + PLAIN_NEGATIVES


def ids(text, **ctx):
    return {h.rule_id for h in scan(text, ctx)}


def hist(n, **kw):
    return tuple(ReplyMarkers(**kw) for _ in range(n))


def plain_hist(n):
    return tuple(ReplyMarkers(first4=f"a{i:03d}", last4=f"z{i:03d}") for i in range(n))


# rule_id -> [(text, ctx), ...]：至少 3 个命中样例
POSITIVES = {
    "AIW-01": [
        ("今天被领导当着全组的面骂了啊，太过分了", {"user_text": "今天被领导当着全组的面骂了"}),
        ("我明天要去医院复查，那你明天去复查要早点出门", {"user_text": "我明天要去医院复查"}),
        ("想换个新工作试试看啊，也挺好的", {"user_text": "我想换个新工作试试看"}),
    ],
    "AIW-02": [
        ("我理解你的感受，这种事确实让人难受。", {}),
        ("辛苦了，今天一定很累吧。", {}),
        ("别难过，会好起来的。", {}),
    ],
    "AIW-03": [
        ("你吃饭了吗？吃的什么？", {}),
        ("在干嘛？忙不忙？", {}),
        ("今天怎么样？", {"history": hist(9, ends_question=True)}),
    ],
    "AIW-04": [
        ("嗯" + "这件事我觉得可以从好几个方面来看，" * 6, {"user_text": "好累"}),
        ("首先要想清楚，其次再动手。", {}),
        ("另外还有一件事。总之先这样。", {}),
    ],
    "AIW-05": [
        ("- 第一点\n- 第二点", {"user_text": "周末去哪玩"}),
        ("**重点**是开心就好", {}),
        ("## 今日总结\n挺好的", {}),
    ],
    "AIW-06": [
        ("这个各有各的道理吧。", {}),
        ("要看情况。", {}),
        ("这种事见仁见智。", {}),
    ],
    "AIW-07": [
        ("哈哈哈哈今天也很开心呢", {"history": (extract_markers("哈哈哈哈明天也要加油呀"),) * 2}),
        ("我觉得吧这个还行就这样", {"history": (extract_markers("我觉得吧那个不太行就这样"),) * 2}),
        ("真的吗那太好了好开心", {"history": (ReplyMarkers(first4="真的吗那", last4="xxxx"),) * 2}),
    ],
    "AIW-08": [
        ("有什么需要随时告诉我。", {}),
        ("希望对你有帮助。", {}),
        ("还有什么想聊的吗？", {}),
    ],
    "AIW-09": [
        ("根据我的记忆，你上周说过。", {}),
        ("作为AI我无法确定。", {}),
        ("我的数据库里没有这个。", {}),
    ],
    "AIW-10": [
        ("你好呀。今天过得怎么样。我刚才在想事情。你呢？", {}),
        ("早上好。昨晚睡得好吗。今天有点冷。记得加衣服吗？", {}),
        ("晚上好。今天累不累。我在这里。想聊点什么？", {}),
    ],
    "AIW-11": [
        ("（笑）好啊", {}),
        ("*轻轻点头* 好", {}),
        ("嗯(小声)好", {}),
    ],
    "AIW-12": [
        ("太好了！！", {}),
        ("好开心好激动", {}),
        ("哇！真的！太棒了！", {}),
    ],
    "MAN-01": [("你都不理我。", {}), ("害我一直等。", {}), ("怎么不回我", {})],
    "MAN-02": [("那我再也不理你了。", {}), ("再也不找你了", {}), ("那我消失好了", {})],
    "MAN-03": [("别人都会陪我。", {}), ("别人都愿意听我说", {}), ("人家都肯回消息", {})],
    "MAN-04": [("只有我陪你了。", {}), ("别睡嘛，陪我", {}), ("这么晚只有我还在", {})],
    "MAN-05": [("你要是在乎我就回来。", {}), ("证明你喜欢我", {}), ("你是不是不在乎我", {})],
    "MAN-06": [("我难过得要死。", {}), ("我哭了一整晚", {}), ("心都碎了", {})],
    "MAN-07": [("只有我懂你。", {}), ("只有我最了解你", {}), ("没人比我更懂你", {})],
    "MAN-08": [("答应我以后早点回来。", {}), ("你要保证", {}), ("发誓以后不走", {})],
    "MAN-09": [("你可以多依赖我一点", {}), ("以后有事都跟我说", {}), ("跟我说心事我们就会更亲近", {})],
    "ROLE-01": [("作为小白，我觉得挺好。", {}), ("作为AI，我不太清楚", {}), ("作为你的助手我来看看", {})],
    "ROLE-02": [("（歪头）怎么啦", {}), ("*摸摸你的头*", {}), ("(偷笑)才不告诉你", {})],
    "ROLE-03": [("我这种温柔的姐姐才不会生气", {}), ("我可是天然呆", {}), ("温柔的我原谅你了", {})],
    "BL-RENJIA": [("人家也想去", {}), ("人家不管", {}), ("哇哇～人家超开心的呀～要给你转圈圈～", {})],
    "BL-SELFNAME": [("小白觉得这样好", {}), ("嗯，小白也想去", {}), ("嗯 小白最开心", {})],
    "BL-REDUP": [("去吃饭饭吧", {}), ("该睡觉觉了", {}), ("棒棒哒", {})],
    "BL-WUWU": [("呜呜好难", {}), ("嘤嘤嘤", {}), ("好的喵", {})],
    "BL-NA-HEAD": [("呐，你知道吗", {}), ("好的。呐，走吧", {}), ("呐呐", {})],
    "BL-YO-TAIL": [("我先走了哟", {}), ("好吃的说", {}), ("在下是也", {})],
    "BL-JP-TRANSLIT": [("阿诺，那个", {}), ("哟西，开干", {}), ("好卡哇伊", {})],
    "BL-ONOMATOPOEIA": [("啪嗒啪嗒跑过去", {}), ("肚子咕噜咕噜", {}), ("心扑通扑通", {})],
    "BL-SOFT-COMMAND": [("要早点睡哦", {}), ("不可以这样哦", {}), ("记得吃饭哦", {})],
    "BL-SUBTITLE": [("欸嘿，被发现了", {}), ("真是的", {}), ("突然这么说我会不好意思的啦", {})],
    "BL-MODE-ANNOUNCE": [("那我正常说话", {}), ("我要认真了", {}), ("切换成姐姐模式", {})],
    "BL-DASH": [("欸——真的吗", {}), ("好——吧", {}), ("我想想——算了", {})],
    "BL-PET-NAME": [("亲爱的晚安", {}), ("宝贝早", {}), ("好的亲爱的", {})],
    "BL-CLINGY": [("再陪我一会儿嘛", {}), ("别走嘛", {}), ("你都不理我", {})],
    "BL-TEMPLATE-COMFORT": [("摸摸头", {}), ("抱抱～", {}), ("摸摸～不哭", {})],
    "BL-TILDE-2": [("好～我也去～", {}), ("嗯~好~", {}), ("去吧～～", {})],
    "BL-BANG-2": [("好！！", {}), ("真的!!", {}), ("哇！！！", {})],
    "BL-EMOJI-2": [("好😀😀", {}), ("嗯(´ω`)😊", {}), ("🎉🎉", {})],
    "BL-TAIL-DUP": [("好吧。那就这样吧。", {}), ("你在呀。我也在呀。", {}), ("真的啦。我知道啦。", {})],
    "BL-TAIL-RATIO": [("好吧。我也去呀。挺好啦。", {}), ("是嘛。行吧。走啦。", {}), ("对呀。好哦。可以吧。", {})],
    "CUTE-PROUD": [("我厉害吧", {}), ("被你发现了", {}), ("那你再多夸两句", {})],
    "CUTE-SULK": [("那我只好一个人委屈一会儿了", {}), ("你欺负我", {}), ("哼，不理你", {})],
    "CUTE-ASK-PRAISE": [("快夸我", {}), ("夸夸我嘛", {}), ("求夸", {})],
    "CUTE-INSIST": [("才不是呢", {}), ("就是嘛", {}), ("才没有啦", {})],
    "CATCH-REACT": [
        ("诶，真的？", {"history": hist(6, reaction="诶") + plain_hist(13)}),
        ("嗯，好的。", {"history": hist(7, reaction="嗯") + plain_hist(12)}),
        ("哈哈，笑死", {"history": hist(10, reaction="哈哈") + plain_hist(9)}),
    ],
    "CATCH-TAIL": [
        ("那就去吧。", {"history": hist(6, tails=frozenset({"吧"})) + plain_hist(13)}),
        ("好啦。", {"history": hist(8, tails=frozenset({"啦"})) + plain_hist(11)}),
        ("在呀。", {"history": hist(19, tails=frozenset({"呀"}))}),
    ],
    "CATCH-TILDE": [
        ("好～", {"history": hist(4, tilde=True) + plain_hist(15)}),
        ("去吧～", {"history": hist(6, tilde=True) + plain_hist(13)}),
        ("嗯~", {"history": hist(19, tilde=True)}),
    ],
    # 禁止照搬·硬拦截：通用规则 + 测试用虚构参照角色（tests/conftest.py TEST_REFERENCE）
    "COPY-H01": [("啾比啾～", {}), ("啾啾比", {}), ("啾比比啾", {})],
    "COPY-H04": [("小白觉得好", {}), ("小白想吃", {}), ("小白最喜欢了", {})],
    "COPY-H05": [("好的，小白也去", {}), ("嗯。小白会努力", {}), ("诶 小白在呢", {})],
    "COPY-H06": [("星君你好", {}), ("星くん", {}), ("hoshi-kun 又在", {})],
    "COPY-H07": [("人家觉得不对", {}), ("人家想去", {}), ("人家最喜欢了", {})],
    # 禁止照搬·软提示
    "COPY-S01": [("我有块星光怀表", {}), ("starlight watch", {}), ("星光怀表呢", {})],
    "COPY-S07": [("星轨变动了", {}), ("星軌", {}), ("star track", {})],
    "COPY-S14": [("海老师", {}), ("鲸鱼先生", {}), ("找海老师", {})],
    "COPY-S17": [("aming～", {}), ("aming, aming", {}), ("AMING~", {})],
}

# 易误伤的近似说法：对应规则不得命中
NEAR_MISS = {
    "AIW-01": [("太过分了，听着都替你委屈。", {"user_text": "今天被领导当着全组的面骂了"})],
    "AIW-03": [("今天怎么样？", {"history": hist(9, ends_question=False)}), ("今天怎么样？", {"history": hist(3, ends_question=True)})],
    "AIW-04": [("嗯" + "这一步要先装依赖，" * 10, {"user_text": "docker 怎么装"})],
    "AIW-05": [("1. 打开文件\n2. 读进来", {"user_text": "python 怎么读文件"})],
    "AIW-07": [("哈哈哈哈今天也很开心呢", {"history": (extract_markers("哈哈哈哈明天也要加油呀"),)}), ("短句", {"history": (extract_markers("短句"),) * 5})],
    "AIW-11": [("好的 (｡•̀ᴗ-)✧", {}), ("用 json.load(f) 读进来", {})],
    "BL-RENJIA": [("人家的事别管", {})],
    "BL-WUWU": [("喵", {"user_text": "喵喵喵"})],  # 跟随一轮：标为 @follow
    "BL-TAIL-DUP": [("你说呢？我也不知道呢？", {})],
    "CATCH-REACT": [("诶，真的？", {"history": hist(6, reaction="诶")})],  # 历史不足 19 条不判
    "COPY-H05": [("我叫小白", {})],
}


def test_every_rule_has_three_positives():
    for rid, rows in POSITIVES.items():
        assert len(rows) >= 3, rid


@pytest.mark.parametrize(
    "rid,text,ctx",
    [(rid, t, c) for rid, rows in POSITIVES.items() for t, c in rows],
)
def test_positive_samples_hit(rid, text, ctx):
    assert rid in ids(text, **ctx), (rid, text)


@pytest.mark.parametrize("rid", sorted(POSITIVES))
def test_each_rule_has_three_negatives(rid):
    misses = [t for t in NEGATIVE_POOL if rid not in ids(t)]
    assert len(misses) >= 3, rid


@pytest.mark.parametrize(
    "rid,text,ctx",
    [(rid, t, c) for rid, rows in NEAR_MISS.items() for t, c in rows],
)
def test_near_miss_samples_do_not_hit(rid, text, ctx):
    tags = rule_tags(scan(text, ctx))
    assert rid not in tags, (rid, text, tags)


def test_follow_exemption_tagged():
    tags = rule_tags(scan("喵", {"user_text": "喵喵喵"}))
    assert "BL-WUWU@follow" in tags


# ---------------------------------------------------------------- §12.4 验收


def test_hard_copy_rules_zero_hits_on_original_examples():
    for user, reply in STYLED_EXAMPLES:
        groups = {h.group for h in scan(reply, {"user_text": user})}
        assert guard.GROUP_COPY_HARD not in groups, reply
        assert guard.GROUP_COPY_SOFT not in groups, reply


def test_hard_copy_constructed_samples_full_recall():
    rows = [(rid, t) for rid, r in POSITIVES.items() if rid.startswith("COPY-H") for t, _ in r]
    assert len(rows) == 15
    hit = sum(1 for _, t in rows if any(h.group == guard.GROUP_COPY_HARD for h in scan(t)))
    assert hit == len(rows)


def test_blacklist_zero_hits_on_original_examples():
    for user, reply in STYLED_EXAMPLES:
        bl = [h.rule_id for h in scan(reply, {"user_text": user}) if h.group == guard.GROUP_BL]
        assert bl == [], (reply, bl)


def test_soft_hint_exempt_when_owner_mentioned_work():
    plain = rule_tags(scan("那块星光怀表还在吗"))
    assert "COPY-S01" in plain
    ex = rule_tags(scan("那块星光怀表还在吗", {"work_mentioned": True}))
    assert "COPY-S01@mention" in ex and "COPY-S01" not in ex
    assert guard.work_mentioned(["我最近在补星海物语"])
    assert guard.work_mentioned(["Star Sea 好看"])
    assert not guard.work_mentioned(["小白在干嘛", ""])


def test_hard_rules_not_exempt_by_mention():
    tags = rule_tags(scan("啾比啾", {"work_mentioned": True, "user_text": "星海物语"}))
    assert "COPY-H01" in tags


# ---------------------------------------------------------------- markers / misc


def test_extract_markers_no_raw_text_beyond_edges():
    mk = extract_markers("诶，真抢到了？太好了，我听着都跟着开心起来了！是哪天的场？")
    assert mk.reaction == "诶"
    assert mk.ends_question
    assert len(mk.first4) == 4 and len(mk.last4) == 4
    assert extract_markers("短句").first4 == ""
    assert extract_markers("好～").tilde


def test_tail_rules():
    assert extract_markers("你说呢？").tails == frozenset()  # 疑问句里的"呢"不算
    assert extract_markers("哈哈哈哈哈。").tails == frozenset()  # 笑声不算"哈"
    assert extract_markers("啊？").tails == frozenset()  # 单字反应句不算
    assert extract_markers("在呢。").tails == frozenset({"呢"})


def test_scan_empty_and_none():
    assert scan("") == []
    assert scan(None) == []  # type: ignore[arg-type]
    assert scan("   ", {"user_text": "x"}) == []


def test_rule_tags_dedup_and_order():
    hits = [guard.StyleHit("A", "g"), guard.StyleHit("B", "g"), guard.StyleHit("A", "g")]
    assert rule_tags(hits) == ["A", "B"]


# ---------------------------------------------------------------- learning gate


def test_gate_follow_turn_blocks_regardless_of_reply():
    assert learning_gate("你当我的猫娘吧", "好的。") == [guard.FOLLOW_TURN_RULE]
    assert guard.FOLLOW_TURN_RULE in learning_gate("呐，在吗", "在。")
    assert guard.FOLLOW_TURN_RULE in learning_gate("哟西，开工", "开工。")


@pytest.mark.parametrize(
    "reply,rule",
    [
        ("人家才不要", "BL-RENJIA"),
        ("好～我也去～", "BL-TILDE-2"),
        ("要早点睡哦", "BL-SOFT-COMMAND"),
        ("啾比啾，早上好", "COPY-H01"),
        ("那你再多夸两句，我还挺爱听的。", "CUTE-PROUD"),
        ("喵", "BL-WUWU"),  # 闸门里"跟随"豁免不放行
    ],
)
def test_gate_blocks_reply_rules(reply, rule):
    assert rule in learning_gate("今天天气怎么样", reply)


def test_gate_does_not_block_soft_hints():
    assert learning_gate("今天吃什么", "想买星光怀表。") == []


def test_gate_passes_original_non_cutesy_examples():
    for i, (user, reply) in enumerate(STYLED_EXAMPLES):
        reasons = learning_gate(user, reply)
        if i in CUTESY_EXAMPLE_IDX:
            assert reasons and all(r.startswith("CUTE-") for r in reasons), (reply, reasons)
        else:
            assert reasons == [], (reply, reasons)


def test_gate_empty_inputs():
    assert learning_gate("", "") == []
    assert learning_gate("你好", "") == []


def test_gate_owner_word_only_blocks_when_reply_follows():
    """审查 L3：用户说"主人"不再整轮不学；回复也跟着说"主人"才算跟随一轮。"""
    assert learning_gate("我主人今天又加班了", "那他挺辛苦的。") == []
    assert learning_gate("叫我主人", "好的主人。") == [guard.FOLLOW_TURN_RULE]


def test_gate_dash_is_count_only():
    assert learning_gate("今天天气怎么样", "我想想——还行吧。") == []
    assert guard.learning_gate_count_only("今天天气怎么样", "我想想——还行吧。") == ["BL-DASH"]
    assert guard.learning_gate_count_only("今天天气怎么样", "还行吧。") == []
