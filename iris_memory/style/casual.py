"""随性标点（``style_casual_punctuation``）：闲聊类回复去掉每段末尾的句号。

用户要求："说话太一板一眼了，随性一点，标点符号也不用打全"。

只做一件事：每个分段（按换行切）末尾的"。"去掉。不动问号、感叹号、省略号、引号 / 书名号内的句号
（段末是 。" 。」 之类时引号在最后，不会被处理）、代码块；中间的逗号句号不改（交给模型的随性语气）。

不处理（原样返回）的回复——认真讲事情时好好用标点：
- 含代码块 ``` 或行内代码 `…`、链接（http / www）；
- 有列表行（- / * / • / 1. / 1、/ 第一步 …）或"步骤 / 首先 / 其次 / 最后"式说明；
- 超过 4 段或总长超过 160 字（长段说明）；
- 命中技术 / 求助话题词（与口吻层"认真求助"场景同一判定）。
"""

from __future__ import annotations

import re

KEY_CASUAL = "style_casual_punctuation"

_CODE_RE = re.compile(r"```|`[^`\n]+`|https?://|www\.", re.IGNORECASE)
_LIST_RE = re.compile(r"^\s*(?:[-*•·]\s|\d+[.、)）]\s*|[（(]?\d+[)）]\s*|第[一二三四五六七八九十\d]+步)", re.MULTILINE)
_STEP_RE = re.compile(r"步骤|首先.{0,40}(?:然后|其次|再)|其次|第一[，,、].{0,40}第二")
MAX_LEN = 160
MAX_PARAS = 4


def is_casual_reply(text: str) -> bool:
    s = str(text or "").strip()
    if not s or len(s) > MAX_LEN:
        return False
    if _CODE_RE.search(s) or _LIST_RE.search(s) or _STEP_RE.search(s):
        return False
    if len([p for p in s.split("\n") if p.strip()]) > MAX_PARAS:
        return False
    try:
        from . import register as R

        if R.is_technical(s) or R._HELP_EXTRA_RE.search(s):
            return False
    except Exception:
        pass
    return True


def casualize(text: str) -> str:
    """闲聊回复：每段末尾的单个"。"去掉（"。。"、"……"不动）；其他回复原样返回。"""
    if not is_casual_reply(text):
        return text
    out = []
    for line in str(text).split("\n"):
        stripped = line.rstrip()
        if stripped.endswith("。") and not stripped.endswith("。。"):
            line = stripped[:-1] + line[len(stripped):]
        out.append(line)
    return "\n".join(out)


__all__ = ["KEY_CASUAL", "is_casual_reply", "casualize"]
