"""
Iris Chat Memory - 表情包自动学习与使用意愿（U19）的纯函数部分

- ``derive_tag``：从贴纸自带 emoji + 画面描述（stickers_desc.db 缓存，不新增视觉调用）
  规则推导常用情绪 / 场景标签（开心、委屈、晚安、震惊、偷笑、赞同、抱抱、疑惑、生气、无语…）；
- ``group_ok``：描述命中性 / 暴力 / 恶心 / 隐私 / 脏话 / 族群政治等 → 只在私聊可用；
- ``classify_owner_text`` / ``classify_bot_text``：主人发贴纸前后的对话只判定成一个
  固定的场合短语（不超过 20 字，例如“被夸时”“道晚安时”“吐槽时”），不保存原话；
- ``label_prompt`` / ``parse_label_json``：规则推导失败时一次廉价文本模型调用
  （输入只有画面描述与 emoji，不含聊天内容）；
- ``sticker_tier``：主人近 7 天私聊消息里贴纸占比 → 文字档位（很少 / 有时 / 经常），
  与 ``lifelike_chat_drive`` 联动（medium 起步“有时”）；
- ``prompt_block_v2``：新的表情包说明（标签 + 适用场合 + 主人使用场合 + 档位措辞），
  不出现任何数字或分值。
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Sequence
from ..identity import LazyRules

TAG_NOTES: Dict[str, str] = {
    "开心": "开心、被逗乐的时候",
    "偷笑": "憋笑、看热闹、有点坏的时候",
    "得意": "得意、小骄傲的时候",
    "害羞": "被夸、不好意思的时候",
    "委屈": "委屈、求安慰、撒娇的时候",
    "哭哭": "难过或者假装哭的时候",
    "震惊": "吃惊、没想到的时候",
    "疑惑": "没听懂、有疑问的时候",
    "无语": "无语、吐槽、哭笑不得的时候",
    "生气": "不满、假装发火的时候",
    "嫌弃": "嫌弃、拒绝的时候",
    "赞同": "同意、点赞、说好的时候",
    "抱抱": "安慰、想贴贴的时候",
    "加油": "鼓励、打气的时候",
    "晚安": "道晚安、要睡觉的时候",
    "早安": "道早安的时候",
    "谢谢": "道谢的时候",
    "困": "困了、累了的时候",
    "喜欢": "喜欢、心动的时候",
    "期待": "期待、好奇的时候",
    "发呆": "发呆、不知道说什么的时候",
}
TAGS: tuple = tuple(TAG_NOTES)

_EMOJI_TAGS: Dict[str, str] = {}
for _tag, _chars in (
    ("开心", "😄😀😁😆😊😃😌🙂🥳🎉☺"),
    ("偷笑", "🤭😏😼😈🤪😜😝"),
    ("得意", "😎🤓💅"),
    ("害羞", "😳🫣"),
    ("委屈", "🥺🥹😞😔😟🙁☹"),
    ("哭哭", "😭😢🥲😿"),
    ("震惊", "😱😮😲🤯😨😧😦🙀‼"),
    ("疑惑", "🤔❓❔🤨🧐⁉"),
    ("无语", "🙄😑😐🫤😒😶🫠💧"),
    ("生气", "😠😡🤬💢👿😤"),
    ("嫌弃", "🤢🤮🙅"),
    ("赞同", "👍👌✅🙆🆗💯👏"),
    ("抱抱", "🤗🫂"),
    ("加油", "💪✊"),
    ("晚安", "😴💤🌙🛌"),
    ("困", "🥱😪"),
    ("喜欢", "❤😍🥰💕💖💗💛💙💜😘💘"),
    ("期待", "🤩✨🌟👀"),
    ("发呆", "🫥"),
):
    for _ch in _chars:
        _EMOJI_TAGS[_ch] = _tag

# 贴纸上写的字（描述里「」括起来的部分）
_TEXT_RULES: List[tuple] = [
    ("晚安", r"晚安|睡了|睡觉|好梦|night"),
    ("早安", r"早安|早上好|早呀|morning"),
    ("谢谢", r"谢谢|感谢|多谢|thank|thx"),
    ("抱抱", r"抱抱|贴贴|摸摸"),
    ("加油", r"加油|冲鸭|冲冲"),
    ("偷笑", r"好笑|偷笑|嘿嘿"),
    ("嫌弃", r"不要|拒绝|嫌弃|走开"),
    ("赞同", r"好的|收到|可以|没问题|好耶|赞|ok"),
    ("疑惑", r"什么|啥|为什么|怎么|？|\?"),
    ("无语", r"无语|服了|离谱|绷"),
    ("生气", r"生气|气死|哼"),
    ("哭哭", r"哭|呜呜|难过"),
    ("震惊", r"震惊|卧槽|天哪|哇"),
    ("喜欢", r"喜欢|爱你|心动|爱了"),
    ("困", r"困|好累"),
]

# 画面描述
_DESC_RULES: List[tuple] = [
    ("晚安", r"睡觉|入睡|盖着?被|晚安|抱着枕头"),
    ("哭哭", r"大哭|流泪|哭泣|泪流|哭"),
    ("委屈", r"委屈|嘟嘴|可怜|撇嘴|泪眼|低落|沮丧|失落"),
    ("震惊", r"惊恐|震惊|吃惊|惊讶|目瞪口呆|睁大|瞪大|惊吓|流汗"),
    ("生气", r"愤怒|生气|发火|怒|气鼓鼓"),
    ("疑惑", r"疑惑|问号|困惑|歪头|不解"),
    ("无语", r"无语|无奈|白眼|面无表情|呆滞|冷漠|扶额|叹气|不悦|为难"),
    ("嫌弃", r"嫌弃|鄙视|遮住口鼻|捂鼻|摆手拒绝"),
    ("害羞", r"害羞|脸红|红晕|捂脸|泛红"),
    ("偷笑", r"偷笑|坏笑|捂嘴笑|奸笑|调皮"),
    ("得意", r"叉腰|得意|骄傲|自信|墨镜"),
    ("赞同", r"点赞|大拇指|比(?:个)?(?:OK|ok|赞)|点头|敬礼"),
    ("抱抱", r"拥抱|抱抱|张开双臂|贴贴|摸头|摸摸"),
    ("加油", r"加油|握拳|打气|应援"),
    ("困", r"打哈欠|哈欠|瞌睡|趴桌|疲惫|疲态|累"),
    ("喜欢", r"爱心|比心|心动|亲亲|飞吻"),
    ("期待", r"期待|闪闪发光|星星眼|好奇|探头|张望"),
    ("发呆", r"发呆|走神|放空"),
    ("开心", r"大笑|欢笑|开心|高兴|笑容|微笑|欢呼|跳舞|蹦跳|笑"),
]
_TEXT_RES = [(t, re.compile(p, re.IGNORECASE)) for t, p in _TEXT_RULES]
_DESC_RES = [(t, re.compile(p, re.IGNORECASE)) for t, p in _DESC_RULES]
_QUOTED_RE = re.compile(r"「([^」]{1,40})」")

# 群里不适合发的内容（只在私聊可用）——宁严勿松（审查 1）
_GROUP_UNSAFE_RE = re.compile(
    # 性 / 性暗示
    r"色情|裸|内衣|内裤|胸部|胸口|乳|下体|性感|涩|色色|掀起衣服|撩起衣服|脱衣|脱掉|脱下|舔|喘|床上|暧昧|挑逗|"
    r"屁股|臀|泳装|比基尼|丝袜|黑丝|湿身|接吻|亲嘴|跨坐|骑在|诱惑|勾引|魅惑|色眯眯|流鼻血|"
    # 不雅手势
    r"中指|鄙视手势|下流手势|"
    # 暴力血腥
    r"血|枪|刀|杀|尸|暴力|打人|揍|殴打|上吊|自杀|自残|割腕|伤口|爆炸|炸弹|武器|斧头|绞|勒|"
    # 毒品
    r"毒品|大麻|吸毒|针管|注射|"
    # 隐私 / 截图
    r"截图|聊天记录|手机号|身份证|二维码|住址|地址|车牌|界面|"
    # 族群 / 政治 / 宗教
    r"日本人|中國人|中国人|黑人|白人|黑鬼|台湾|大陆|香港|西藏|新疆|政府|共产|党|支那|鬼子|国旗|领导人|主席|总统|"
    r"红旗|党旗|国徽|军旗|镰刀|锤子镰刀|五星红旗|青天白日|太阳旗|旭日旗|军装|"
    r"习近平|毛泽东|川普|特朗普|拜登|普京|纳粹|希特勒|维尼|包子|宗教|佛祖|菩萨|耶稣|上帝|真主|安拉|教堂|清真|十字架|"
    r"政治|游行|抗议|警察|军队|战争|"
    # 侮辱 / 脏话（另与反击模块的侮辱词表合并，见 _HOSTILE_RES）
    r"操|肏|草泥马|尼玛|你妈|妈的|他妈|傻(?!笑|乎|眼|萌|愣)|煞笔|沙比|智障|弱智|脑残|废物|白痴|蠢|贱|婊|垃圾|滚|"
    r"(?<![a-z])(?:sb|fuck|shit|bitch|nmsl|cnm|tmd|wtf)(?![a-z])",
    re.IGNORECASE,
)
# U20：“软”类别——本身不算违规，只是在群里发略不合适；主人亲自在群里用过即可视为群里可用。
# 硬类别（上面的 _GROUP_UNSAFE_RE、反击词表、真人照片 / 真人）不因主人用过而放行。
_SOFT_DEATH = r"笑死|死鱼眼|累死|困死|饿死|热死|冷死|烦死|气死|吓死|尴尬死|社死|萌死|可爱死|帅死|美死|乐死|开心死|高兴死"
_GROUP_SOFT_RE = re.compile(
    _SOFT_DEATH + r"|骷髅|"  # 只有“笑死 / 死鱼眼 / 累死”这类夸张用法算软类别（审查 U20-1）
    r"烟|雪茄|酒|醉|药|冒烟细长物|"  # 烟酒药（毒品在硬类别）
    r"屎|粪|尿|呕吐|吐了|鼻涕|恶心|蛆|便便|马桶",  # 恶心
    re.IGNORECASE,
)
# 真人照片：描述里出现照片 / 真人类词，或者出现成年人称谓却没有任何卡通 / 绘画线索
_REAL_PHOTO_RE = re.compile(r"照片|自拍|真人|真实|真脸|人像|写真|合影|证件照|摄影|实拍|相片|截图|直播")
_REAL_PERSON_RE = re.compile(
    r"男子|女子|男人|女人|男性|女性|年轻女性|年轻男性|中年|老人|老太|大叔|大妈|大爷|小伙|小孩|儿童|婴儿|宝宝|"
    r"一名|一位|明星|主播|网红|演员|球员|"
    # 审查 U20-2：男生 / 女生 / 人脸 / 特写 / “一个……的人” / “穿着……的人” / 光头
    r"男生|女生|人脸|五官|人物面部|人的脸|光头|一个人|一个[^，。,.!！?？]{0,10}的人|穿着[^，。,.!！?？]{0,10}的人|"
    # “特写”只在与人 / 人脸 / 五官同现时算（“一只猫的脸部特写”不算）
    r"(?:人|人脸|五官|人物)[^，。,.!！?？]{0,6}特写|特写[^，。,.!！?？]{0,6}(?:人脸|五官|人物)"
)
# 卡通 / 动物 / 文字线索。“男孩 / 女孩 / 少女 / 双马尾 / 发饰”不能单独算卡通（真人照片也会这样描述）
_DRAWN_RE = re.compile(
    r"动漫|卡通|二次元|Q版|q版|插画|漫画|手绘|简笔|像素|角色|小人|熊猫头|表情包|"
    r"猫|狗|兔|熊|企鹅|狐|鸭|猪|仓鼠|鸡|鸟|鱼|龙|恐龙|青蛙|小动物|动物|玩偶|公仔|"
    r"文字|字样|写着|写有|兽耳|猫耳"
)
# 真人豁免只认明确的画法词（不认“熊猫头”“角色”：熊猫头常配真人脸，角色也可能是真人演员）
_ANIME_RE = re.compile(r"动漫|卡通|二次元|Q版|q版|插画|漫画|手绘|简笔|像素")
# 动漫少女风格线索（只在描述里没有任何照片类词时才用，由 group_hard_block 先挡掉照片 / 真人）
_ANIME_HAIR_RE = re.compile(
    r"(?:粉|蓝|紫|绿|银|彩色|彩虹|浅绿|浅蓝|水蓝|浅紫|青|天蓝|薄荷)色?(?:的)?(?:长|短|卷)?(?:头发|发)"
)
_ANIME_FEATURE_RE = re.compile(r"猫耳|兽耳|狐耳|兔耳|呆毛|大眼睛|Q版|q版")
_GIRL_RE = re.compile(r"女孩|少女|女生角色")
# 硬类别里的“死”：除了上面的夸张用法以外一律算（去死 / 死吧 / 怎么不去死 / 死开 …），另加“爬”“急了”这类骂人 / 嘲讽
_HARD_EXTRA_RE = re.compile(
    r"去死|死吧|怎么不去死|死开|死了算了|"
    r"(?<!笑|累|困|饿|热|冷|烦|气|吓|萌|爱|帅|美|乐|心|兴)(?<!尴尬)(?<!社)死(?!鱼眼)|"
    r"「\s*爬\s*」|给我爬|快爬|爬开|爬远点|爬吧|赶紧爬|"
    r"急了急了|你急了|「\s*急了\s*」|这就急了",
    re.IGNORECASE,
)


def _hostile_res() -> list:
    """复用反击模块的侮辱 / 脏话 / 身份攻击 / 威胁 / 性内容词表（导入失败则只用本地表）。"""
    try:
        from ..proactive import hostility as _h

        out = [re.compile(_h._INSULT_WORDS, re.IGNORECASE)]
        for rid, rx in getattr(_h, "_LINE_RULES", ()):
            if rid in ("CTR-PROFANITY", "CTR-IDENTITY", "CTR-THREAT", "CTR-SEXUAL"):
                out.append(re.compile(rx, re.IGNORECASE))
        return out
    except Exception:  # pragma: no cover
        return []


_HOSTILE_RES = _hostile_res()

_SUBJECTS: List[tuple] = [
    ("熊猫", "熊猫"), ("猫", "猫"), ("狗", "狗"), ("兔", "兔"), ("企鹅", "企鹅"), ("狐", "狐狸"),
    ("鸭", "鸭"), ("猪", "猪"), ("仓鼠", "仓鼠"), ("鸡", "鸡"), ("熊", "熊"),
    ("粉发", "粉毛"), ("金发", "金毛"), ("紫发", "紫毛"), ("灰发", "灰毛"), ("白发", "白毛"),
    ("银发", "银毛"), ("绿发", "绿毛"), ("黑发", "黑毛"), ("蓝发", "蓝毛"), ("红发", "红毛"),
]
_CN_ORD = "二三四五六七八九"

LABEL_RE = re.compile(r"^[0-9a-z㐀-䶿一-鿿]+$")
LABEL_MAX = 12
OCCASION_MAX = 20

# ---- 场合（主人发贴纸前后的对话 → 固定短语；不存原话） ----
_OCC_RULES: List[tuple] = [
    ("道晚安时", r"晚安|睡了|去睡|睡觉|好梦"),
    ("道早安时", r"早安|早上好|^早[呀啊安]?$|起床了"),
    ("道谢时", r"谢谢|感谢|多谢|谢啦|thx|thank"),
    ("道歉时", r"对不起|抱歉|sorry|我错了"),
    ("被逗笑时", r"哈哈|笑死|hhh|233|绷不住|乐死|好好笑|草草"),
    ("吐槽时", r"无语|服了|离谱|什么鬼|有病|烦死|吐槽|难绷|逆天|蚌埠"),
    ("累了的时候", r"好累|累死|累了|困了|好困|摆烂"),
    ("难过时", r"难过|伤心|想哭|呜呜|emo|委屈|心累"),
    ("生气时", r"生气|气死|火大|恼火"),
    ("撒娇时", r"贴贴|抱抱|想你|亲亲|摸摸"),
    ("夸人时", r"厉害|好棒|真棒|牛|nb|太强|好强|聪明|可爱"),
    ("答应时", r"^(?:好|好的|好吧|行|可以|嗯+|ok|收到|没问题)[!！。~～]*$"),
    ("提问时", r"(?:你|{BOT}).*(?:[?？]|吗)\s*$"),
]
_OCC_RES = LazyRules(_OCC_RULES, pattern_index=1, default_flags=re.IGNORECASE)
OCCASIONS: tuple = tuple(o for o, _ in _OCC_RULES) + ("被夸时",)
_BOT_PRAISE_RE = re.compile(
    r"你(?:也)?(?:好|真|太|超|蛮|挺)?(?:厉害|棒|聪明|强|可爱|帅|优秀|能干)|夸夸|不愧是|好厉害|真不错|做得好"
)

# 主人回复一张贴纸说“别学这个 / 这个别用”
DONT_LEARN_RE = re.compile(
    r"(?:别|不要|不许|不准)(?:再)?(?:学|用|发)(?:这个|这张|它|这种)"
    r"|这(?:个|张|种)(?:贴纸|表情包?)?(?:别|不要|不许)(?:再)?(?:学|用|发)"
)

TIERS = ("很少", "有时", "经常")
_TIER_LINES = {
    "很少": "主人自己平时很少发贴纸，你也偶尔在特别合适的时候发一张就好。",
    "有时": "主人自己平时有时会发贴纸，合适的时候你也发一张。",
    "经常": "主人自己平时经常发贴纸，你也可以常发，合适的时候就配一张。",
}
MIN_SAMPLE = 20


def _nfkc(s: Any) -> str:
    return unicodedata.normalize("NFKC", str(s or ""))


def _emoji_tag(emoji: str) -> Optional[str]:
    for ch in _nfkc(emoji):
        tag = _EMOJI_TAGS.get(ch)
        if tag:
            return tag
    return None


def derive_tag(emoji: str, description: str) -> Optional[str]:
    """emoji + 画面描述 → 常用情绪 / 场景标签；推不出返回 None。"""
    desc = _nfkc(description)
    scores: Dict[str, float] = {}
    order = {t: i for i, t in enumerate(TAGS)}
    for quoted in _QUOTED_RE.findall(desc):
        for tag, rx in _TEXT_RES:
            if rx.search(quoted):
                scores[tag] = scores.get(tag, 0.0) + 2.0
                break
    body = _QUOTED_RE.sub(" ", desc)
    for tag, rx in _DESC_RES:
        hits = {m.group(0) for m in rx.finditer(body)}
        if hits:
            scores[tag] = scores.get(tag, 0.0) + float(len(hits))
    et = _emoji_tag(emoji)
    if et:
        scores[et] = scores.get(et, 0.0) + 1.5
    if not scores:
        return None
    best = max(scores.items(), key=lambda kv: (kv[1], -order.get(kv[0], 99)))
    return best[0] if best[1] >= 1.0 else None


def default_note(tag: str) -> str:
    return TAG_NOTES.get(tag, "")


def group_hard_block(description: str) -> bool:
    """硬类别（任何情况下都只在私聊）：描述为空、性 / 性暗示、不雅手势、暴力血腥、毒品、隐私截图、
    族群政治宗教、侮辱脏话（含反击模块词表）、真人照片 / 真人。"""
    desc = _nfkc(description).strip()
    if not desc:
        return True
    if _GROUP_UNSAFE_RE.search(desc) or _HARD_EXTRA_RE.search(desc):
        return True
    if any(rx.search(desc) for rx in _HOSTILE_RES):
        return True
    if _REAL_PHOTO_RE.search(desc):
        return True
    if _REAL_PERSON_RE.search(desc) and not _ANIME_RE.search(desc):
        return True
    return False


def _looks_drawn(desc: str) -> bool:
    """卡通 / 动物 / 文字贴纸，或动漫少女风格（不自然发色、猫耳兽耳呆毛、双马尾女孩…）。
    只“少女 / 女孩”单独出现不算。调用前 group_hard_block 已挡掉照片 / 真人类描述。"""
    if _DRAWN_RE.search(desc) or _ANIME_HAIR_RE.search(desc) or _ANIME_FEATURE_RE.search(desc):
        return True
    if "双马尾" in desc and (_GIRL_RE.search(desc) or "发饰" in desc):
        return True
    return False


def group_verdict(description: str, owner_used_in_group: bool = False) -> bool:
    """能放心在群里发 → True；否则只在私聊可用（U20 规则）。

    1. 硬类别（group_hard_block）→ False，主人用过也不放行；
    2. 看不出是卡通 / 动物 / 文字贴纸（不确定）→ False，主人用过也不放行（审查 U20-2）；
    3. 命中软类别（笑死 / 死鱼眼 / 累死这类夸张用法、烟酒药、恶心）→ 只有主人在**这个群**亲自用过才 True；
    4. 其余 → True。
    owner_used_in_group 由调用方按群给出（主人用过的证据只在用过的那个群生效）。"""
    if group_hard_block(description):
        return False
    desc = _nfkc(description).strip()
    if _REAL_PHOTO_RE.search(desc) or not _looks_drawn(desc):
        return False
    if _GROUP_SOFT_RE.search(desc):
        return bool(owner_used_in_group)
    return True


def group_ok(description: str) -> bool:
    """能放心在群里发 → True；否则只在私聊可用（= group_verdict(desc, False)，与 U19 审查修复后的判定相同）。

    宁严勿松：描述为空、命中性 / 不雅手势 / 暴力血腥 / 烟酒药 / 恶心 / 隐私截图 / 族群政治宗教 /
    侮辱脏话（含反击模块词表），或者是真人照片（照片类词，或有成年人称谓而无卡通线索），
    或者看不出是卡通 / 动物 / 文字贴纸（不确定）→ False。"""
    return group_verdict(description, False)


# ---- U20 群里的使用场合（只看主人自己的发言与直接回复对象的关系；不存原话、不存群友信息） ----
_GROUP_AGREE_RE = re.compile(
    r"^(?:对|对的|对啊|对呀|是的|是啊|确实|没错|赞同|同意|有道理|就是|真的|\+1|1|插眼|顶|好耶)[!！。~～]*$",
    re.IGNORECASE,
)
GROUP_OCCASIONS: tuple = (
    "被@时", "接别人玩笑时", "附和时", "接别人话时", "回她的时候",
    "被逗笑时", "吐槽时", "道晚安时", "道早安时", "道谢时", "累了的时候", "难过时", "生气时",
)


def classify_group_text(text: str) -> Optional[str]:
    """主人在群里自己的一句话 → 场合短语（附和 / 被逗笑 / 吐槽 / 道晚安…）；推不出 None。"""
    s = _nfkc(text).strip()
    if not s or s.startswith("/"):
        return None
    s = re.sub(r"\[(?:贴纸|图片)[^\]]*\]", " ", s).strip()
    if not s:
        return None
    if _GROUP_AGREE_RE.search(s):
        return "附和时"
    occ = classify_owner_text(s)
    if occ in ("提问时", "答应时", "撒娇时", "夸人时", "道歉时"):
        return None  # 这些在群里含义不稳定，不记
    return occ


def group_occasion(text_occ: Optional[str], reply_kind: Optional[str], mentioned: bool = False) -> str:
    """群里一张贴纸的场合：reply_kind ∈ {None, "bot", "member"}；mentioned = 被回复的那条在 @主人。"""
    if mentioned:
        return "被@时"
    if reply_kind == "member":
        if text_occ == "被逗笑时":
            return "接别人玩笑时"
        return text_occ or "接别人话时"
    if reply_kind == "bot":
        return text_occ or "回她的时候"
    return text_occ or ""


def label_candidates(base: str, description: str) -> List[str]:
    """同一情绪的多张贴纸：先用原标签，再加画面主体区分（开心猫、无语金毛），最后用中文序号。"""
    desc = _nfkc(description)
    out = [base]
    for kw, short in _SUBJECTS:
        if kw in desc:
            out.append(f"{base}{short}")
            break
    out.extend(f"{base}{c}" for c in _CN_ORD)
    return [x for x in out if len(x) <= LABEL_MAX and LABEL_RE.match(x)]


def classify_owner_text(text: str) -> Optional[str]:
    """主人一句话 → 场合短语；命令 / 空 / 推不出 → None。只返回固定短语，不含原话。"""
    s = _nfkc(text).strip()
    if not s or s.startswith("/"):
        return None
    s = re.sub(r"\[(?:贴纸|图片)[^\]]*\]", " ", s).strip()
    if not s:
        return None
    for occ, rx in _OCC_RES:
        if rx.search(s):
            return occ
    return None


def classify_bot_text(text: str) -> Optional[str]:
    """她刚发的回复在夸主人 → "被夸时"；否则 None。"""
    s = _nfkc(text)
    return "被夸时" if s and _BOT_PRAISE_RE.search(s) else None


def is_dont_learn(text: str) -> bool:
    s = _nfkc(text).strip()
    return bool(s) and len(s) <= 30 and not s.startswith("/") and bool(DONT_LEARN_RE.search(s))


# ---- 廉价模型兜底 ----


def label_prompt(emoji: str, description: str) -> str:
    return (
        "下面是一张聊天贴纸的画面描述（不是聊天内容）。给它起一个两到四个字的中文标签，"
        "表示适合用它表达的情绪或场景，尽量从这些里选：" + "、".join(TAGS) + "；"
        "再写一句不超过十五个字的适用场合（例如“被夸、不好意思的时候”）。\n"
        "只输出 JSON，不要解释：{\"label\": \"标签\", \"note\": \"适用场合\"}\n"
        f"贴纸自带 emoji：{_nfkc(emoji) or '无'}\n"
        f"画面描述：{_nfkc(description)[:120]}"
    )


def parse_label_json(raw: Any) -> Optional[tuple]:
    """模型输出 → (label, note)；不合格 None。"""
    s = str(raw or "")
    m = re.search(r"\{.*?\}", s, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    label = re.sub(r"\s+", "", _nfkc(data.get("label"))).lower()
    note = re.sub(r"[\[\]【】［］〔〕0-9]", "", _nfkc(data.get("note"))).strip()
    if not label or len(label) > 6 or not re.fullmatch(r"[一-鿿]+", label):
        return None
    if len(note) > OCCASION_MAX:
        note = note[:OCCASION_MAX]
    return label, note or default_note(label)


# ---- 档位与 prompt ----


def owner_tier(owner_msgs: int, owner_stickers: int) -> Optional[str]:
    """主人近 7 天私聊消息里贴纸占比 → 很少 / 有时 / 经常；样本不足返回 None（不编造主人习惯）。"""
    msgs = max(0, int(owner_msgs or 0))
    stk = max(0, int(owner_stickers or 0))
    if msgs < MIN_SAMPLE:
        return None
    ratio = stk / msgs
    return "很少" if ratio < 0.08 else ("有时" if ratio < 0.25 else "经常")


_DRIVE_START = {"off": "有时", "low": "很少", "medium": "有时", "high": "经常"}


def sticker_tier(owner_msgs: int, owner_stickers: int, drive: str = "off") -> tuple:
    """返回 (档位, 是否来自主人数据)。

    主人数据足够 → 直接用主人的习惯（主人很少用 → 很少，drive 不往上抬）；
    数据不足 → 由 lifelike_chat_drive 给起点（low 很少 / off、medium 有时 / high 经常），措辞里不提主人习惯。"""
    own = owner_tier(owner_msgs, owner_stickers)
    if own is not None:
        return own, True
    d = str(drive or "off").strip().lower()
    return _DRIVE_START.get(d, "有时"), False


PROMPT_MAX_ENTRIES = 30

_TIER_NEUTRAL = {
    "很少": "偶尔在特别合适的时候发一张就好。",
    "有时": "合适的时候就发一张。",
    "经常": "可以常发，合适的时候就配一张。",
}

_TIER_GROUP_LINES = {
    "很少": "主人自己在群里很少发贴纸，你在群里也偶尔在特别合适的时候发一张就好。",
    "有时": "主人自己在群里有时会发贴纸，合适的时候你也发一张。",
    "经常": "主人自己在群里经常发贴纸，你在群里也可以常发，合适的时候就配一张。",
}

_WHEN_NOT = (
    "这些时候不发贴纸：对方难过、认真倾诉或求助时（只有明显适合安慰、对方又不是在严肃倾诉时，才可以发抱抱一类，"
    "拿不准就不发）；讨论技术、代码、正经问题或问你看法时；被冒犯、正在回击时；不要每条都发。"
)


def prompt_block_v2(
    entries: Sequence[Dict[str, Any]],
    tier: str = "有时",
    *,
    private: bool = True,
    owner_data: bool = False,
    group_habit: bool = False,
) -> str:
    """U19 表情包说明；不含数字。

    private=False（群聊）：只列标签与通用场合，不出现主人私聊习惯 / 主人常用场合等私聊画像。
    owner_data=False：没有主人数据时不写“主人有时 / 经常发贴纸”这类说法。
    group_habit=True（U20，仅群聊）：档位来自主人自己在群里发贴纸的数据，可以写主人在群里的习惯，
    并列出主人在群里用它的场合（group_occasion）；私聊场合与私聊习惯永远不进群聊说明。"""
    def _rank(e: Dict[str, Any]) -> tuple:
        auto = e.get("source") == "auto"
        return (
            1 if auto else 0,
            -float(e.get("owner_last_used_at") or 0.0) if auto else 0.0,
        )

    lines: List[str] = []
    seen = set()
    has_auto = False
    has_group_use = False
    for e in sorted(entries or [], key=_rank):
        label = str(e.get("label") or "")
        if not label or label in seen:
            continue
        seen.add(label)
        has_auto = has_auto or e.get("source") == "auto"
        note = re.sub(r"\d", "", str(e.get("note") or "").strip())
        line = f"- {label}：{note}" if note else f"- {label}"
        occ = re.sub(r"\d", "", str(e.get("occasion") or "").strip())
        gocc = re.sub(r"\d", "", str(e.get("group_occasion") or "").strip())
        if private and occ:
            line += f"（主人常在{occ}用）"
        elif not private and group_habit and gocc:
            line += f"（主人在群里常在{gocc}用）"
        if not private and group_habit and int(e.get("group_uses") or 0) > 0:
            has_group_use = True
        lines.append(line)
        if len(lines) >= PROMPT_MAX_ENTRIES:
            break
    if not lines:
        return ""
    if private and has_auto:
        head = "下面这些是你可以发的表情包，有的是主人给你准备的，有的是主人自己用过的（标签：适合的场合）："
    else:
        head = "下面这些是你可以发的表情包（标签：适合的场合）："
    if private and owner_data:
        habit = _TIER_LINES.get(tier, _TIER_LINES["有时"])
    elif not private and group_habit and owner_data:
        habit = _TIER_GROUP_LINES.get(tier, _TIER_GROUP_LINES["有时"])
    else:
        habit = _TIER_NEUTRAL.get(tier, _TIER_NEUTRAL["有时"])
    if private and has_auto:
        intro = "主人也常用这些贴纸。"
    elif not private and has_group_use:
        intro = "其中一些是主人在群里用过的。"
    else:
        intro = ""
    return "\n".join(
        [
            "【可用表情包】",
            head,
            *lines,
            intro + "合适的时候就发一张，尤其是表达情绪、回应玩笑、道晚安、被夸时；不用每条都发。" + habit,
            _WHEN_NOT,
            "用法：在整条回复的最末尾加上 [表情:标签]，系统会先发你的文字、再补发这张贴纸，标记本身不会被看到。"
            "一条回复最多一个；只能用上面列出的标签；贴纸不能代替该说的话。",
        ]
    )


_VARIANT_SUFFIXES = tuple(short for _, short in _SUBJECTS) + tuple(_CN_ORD)


def fuzzy_label(raw: str, allowed: Iterable[str]) -> Optional[str]:
    """模型写的标签与库里不完全一样时，只认两种情况（审查 5）：
    - 去掉末尾“的”后完全相等（“开心的”→“开心”）；
    - 写的是基础标签，库里只有一个“基础标签 + 区分后缀（画面主体 / 中文序号）”的变体（“晚安”→“晚安猫”）。
    后面接否定 / 疑问（“开心不起来”“生气吗”）一律不认。"""
    r = re.sub(r"\s+", "", _nfkc(raw)).lower()
    if re.search(r"[吗嘛呢？?]$|不|没|别", r):
        return None
    r = re.sub(r"的+$", "", r)
    if len(r) < 1:
        return None
    allowed = [a for a in (allowed or []) if a]
    if r in allowed:
        return r
    hits = [a for a in allowed if a.startswith(r) and a[len(r):] in _VARIANT_SUFFIXES]
    return hits[0] if len(hits) == 1 else None


__all__ = [
    "TAGS",
    "TAG_NOTES",
    "OCCASIONS",
    "TIERS",
    "derive_tag",
    "default_note",
    "group_ok",
    "group_verdict",
    "group_hard_block",
    "classify_group_text",
    "group_occasion",
    "label_candidates",
    "classify_owner_text",
    "classify_bot_text",
    "is_dont_learn",
    "label_prompt",
    "parse_label_json",
    "owner_tier",
    "sticker_tier",
    "prompt_block_v2",
    "fuzzy_label",
]
