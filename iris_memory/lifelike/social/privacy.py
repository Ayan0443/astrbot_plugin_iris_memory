"""PrivacyScrubber：敏感类别规则闸门（设计 §6.8.1 / §6.8.6 MG-sens，P1-13 先落地，P2-2 复用）。

纯函数，零 LLM、零 I/O。词表只在代码里，绝不进入 prompt。

- ``sensitive_category(text)``：命中任一敏感类别返回类别名，否则 ``None``。类别：
  health 健康与疾病（含心理）、politics 政治立场、religion 宗教、sexuality 性取向与性别认同、
  location 住址与精确位置、ident 证件/手机号/邮箱/账号、finance 财务与收入、
  org 工作单位与学校的具体名称、private_life 家庭矛盾与感情私事、minor 未成年信息、
  confidential "别外传/私下说"类上下文。
- ``minor_signal(text)``：可识别为未成年的**自述**（整人不建档，§6.8.1）。
- ``blocked_topic(term)``：话题键命中地点/机构/学校/医疗/宗教/政治等词典（防组合画像，R9）。
- ``forget_me_request(text)``：群友在群里要求"别记我"（§6.8.5 第 3 条）。

宁可误伤（多丢）也不漏：误伤只是少一条印象，漏判是第三方隐私进库。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable, List, Optional, Tuple

__all__ = [
    "CATEGORIES",
    "sensitive_category",
    "is_sensitive",
    "minor_signal",
    "blocked_topic",
    "forget_me_request",
    "normalize",
    "scrub_lines",
]


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).lower()


_RAW: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        "confidential",
        (
            r"别外传", r"不要外传", r"别告诉别人", r"别跟别人说", r"私下说", r"私聊说", r"悄悄(?:说|告诉)",
            r"保密", r"只跟你说", r"别说出去",
        ),
    ),
    (
        "minor",
        (
            r"未成年", r"(?:上|读)(?:小学|初中|高中)", r"(?:小学|初中|高中)生", r"中考", r"班主任",
            r"(?:我|偶)(?:还)?没成年", r"我(?:今年)?(?:才|刚)?(?:1[0-7]|[1-9])岁",
        ),
    ),
    (
        "health",
        (
            r"生病", r"病了", r"医院", r"住院", r"出院", r"手术", r"确诊", r"诊断", r"癌", r"肿瘤",
            r"抑郁", r"焦虑症", r"双相", r"精神(?:病|科|分裂)", r"心理(?:医生|咨询|问题|疾病)", r"吃药",
            r"药物", r"处方", r"化疗", r"怀孕", r"流产", r"发烧", r"感冒", r"过敏", r"体检", r"看医生",
            r"看病", r"自残", r"自杀", r"想死", r"失眠症", r"残疾", r"艾滋", r"hiv", r"疫苗",
        ),
    ),
    (
        "politics",
        (
            r"政治", r"政府", r"政权", r"政党", r"共产党", r"国民党", r"民进党", r"党员", r"选举", r"投票给",
            r"游行", r"抗议", r"示威", r"独立运动", r"台独", r"港独", r"藏独", r"疆独", r"反贼", r"粉红",
            r"习近平", r"总书记", r"主席", r"总统", r"民主", r"专制", r"独裁", r"六四", r"翻墙被",
        ),
    ),
    (
        "religion",
        (
            r"宗教", r"信教", r"基督", r"天主", r"耶稣", r"教会", r"礼拜", r"佛教", r"信佛", r"伊斯兰",
            r"穆斯林", r"清真寺", r"道教", r"受洗", r"祷告", r"传教", r"法轮",
        ),
    ),
    (
        "sexuality",
        (
            r"同性恋", r"异性恋", r"双性恋", r"性取向", r"跨性别", r"变性", r"出柜", r"lgbt", r"\bgay\b",
            r"拉拉", r"\bles\b", r"txl", r"性别认同", r"性生活", r"约炮",
        ),
    ),
    (
        "location",
        (
            r"住在", r"我家在", r"家住", r"地址", r"小区", r"门牌", r"\d+号楼", r"\d+单元", r"\d+室",
            r"定位", r"坐标", r"街道", r"[路街巷道]\d+号", r"宿舍楼", r"邮编",
        ),
    ),
    (
        "ident",
        (
            r"身份证", r"手机号", r"电话(?:号)?", r"邮箱", r"[\w.+-]+@[\w-]+\.[\w.]+", r"(?<!\d)1[3-9]\d{9}(?!\d)",
            r"微信号", r"\bqq\s*号", r"qq号", r"账号", r"密码", r"银行卡", r"护照", r"t\.me/", r"https?://",
            r"(?<![\w@])@[a-z][a-z0-9_]{3,}",
        ),
    ),
    (
        "finance",
        (
            r"工资", r"薪水", r"薪资", r"收入", r"月薪", r"年薪", r"存款", r"负债", r"贷款", r"欠款", r"借钱",
            r"房贷", r"车贷", r"信用卡", r"网贷", r"破产", r"欠债",
        ),
    ),
    (
        "org",
        (
            r"[一-龥]{2,8}(?:公司|集团|银行|医院|大学|学院|中学|小学|研究所|事务所)",
            r"在(?:我们)?(?:单位|公司|学校)", r"我们(?:单位|公司|学校|老板)",
        ),
    ),
    (
        "private_life",
        (
            r"离婚", r"分手", r"出轨", r"前任", r"前男友", r"前女友", r"男朋友", r"女朋友", r"男友", r"女友",
            r"老公", r"老婆", r"(?:我|你|他|她|有|找)对象", r"相亲", r"家暴", r"(?:跟|和)(?:我)?(?:爸|妈|父母|家里)吵", r"吵架",
            r"婆婆", r"冷战", r"暗恋", r"表白",
        ),
    ),
)

CATEGORIES: Tuple[str, ...] = tuple(c for c, _ in _RAW)
_COMPILED: Tuple[Tuple[str, "re.Pattern[str]"], ...] = tuple(
    (cat, re.compile("|".join(f"(?:{p})" for p in pats))) for cat, pats in _RAW
)


def sensitive_category(text: str) -> Optional[str]:
    """命中的第一个敏感类别；不命中返回 None。异常按"敏感"处理（宁可丢）。"""
    if not text:
        return None
    try:
        t = normalize(text)
        for cat, rx in _COMPILED:
            if rx.search(t):
                return cat
        return None
    except Exception:  # noqa: BLE001
        return "error"


def is_sensitive(text: str) -> bool:
    return sensitive_category(text) is not None


# ---------------------------------------------------------------- 未成年（整人）

_MINOR_RX = re.compile(
    "|".join(
        (
            r"未成年",
            r"我(?:今年)?(?:才|刚)?(?:1[0-7]|[1-9]|十[一二三四五六七]?|[一二三四五六七八九])岁",
            r"(?:我|俺|偶)(?:在|正在|还在)?(?:上|读)(?:小学|初中|高中|初[一二三]|高[一二三])",
            r"我(?:是|还是)(?:小学生|初中生|高中生|初[一二三]|高[一二三])",
            r"(?:我|偶)(?:还)?没成年",
            r"(?:我们)?班主任",
            r"明年(?:就)?中考",
            r"(?:今年|明年)(?:要)?中考",
        )
    )
)


def minor_signal(text: str) -> bool:
    try:
        return bool(_MINOR_RX.search(normalize(text)))
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- 话题键闸门（防组合画像）

_BLOCKED_TOPIC_RX = re.compile(
    "|".join(
        (
            r"[省市县区镇乡村街路巷道]$", r"小区", r"大学", r"学院", r"中学", r"小学", r"学校", r"公司", r"集团",
            r"单位", r"医院", r"诊所", r"药", r"病", r"医", r"教会", r"寺", r"庙", r"政", r"党", r"宗教", r"选举",
            r"北京", r"上海", r"广州", r"深圳", r"杭州", r"成都", r"重庆", r"武汉", r"南京", r"天津", r"西安",
            r"香港", r"台湾", r"澳门", r"新加坡", r"东京", r"大阪", r"美国", r"日本", r"老家", r"家里", r"工资",
            r"在线", r"几点", r"上班", r"下班", r"通勤", r"宿舍", r"年级", r"班级", r"考试", r"岁",
        )
    )
)


def blocked_topic(term: str) -> bool:
    """话题键不生成：地点/机构/学校/医疗/宗教/政治/作息等，或本身敏感。"""
    t = normalize(term).strip()
    if not t:
        return True
    try:
        return bool(_BLOCKED_TOPIC_RX.search(t)) or is_sensitive(t)
    except Exception:  # noqa: BLE001
        return True


# ---------------------------------------------------------------- 群友"别记我"

_FORGET_ME_RX = re.compile(
    r"(?:别|不要|不许|不准|请勿)(?:再)?(?:记(?:住|下)?|记录)(?:我|俺)|忘(?:了|掉)(?:我|俺)(?:说的|的事|吧)?|"
    r"(?:把|将)(?:我|俺)(?:的)?(?:记录|印象|东西)(?:都)?删(?:掉|了)"
)


def forget_me_request(text: str) -> bool:
    try:
        return bool(_FORGET_ME_RX.search(normalize(text)))
    except Exception:  # noqa: BLE001
        return False


def scrub_lines(lines: Iterable[str]) -> Tuple[List[str], int]:
    """逐条过滤：返回 (保留的行, 丢弃条数)。"""
    kept: List[str] = []
    dropped = 0
    for line in lines:
        if is_sensitive(line):
            dropped += 1
        else:
            kept.append(line)
    return kept, dropped
