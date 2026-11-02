"""毎月か今月だけかを、言葉から手元の規則で読む（8章。AI に決めさせない）。

返す値は "rule"（毎月）・"once"（今月だけ）・None（言っていない → その場で聞く）。
- 毎月・これからずっと・ずっと・今月から・今月以降・来月から（「元に戻して」を除く）は毎月
- 今月だけ・今回だけ・今回のみ・今月は・今月・「来月から元に戻して」は今月だけ
- 打ち消し（毎月じゃなくて・毎月ではなく）は今月だけ
- 「今月中に」「今月の希望休」は数えない
"""
from __future__ import annotations

import re

NEG_RULE = r"毎月(じゃ|では|でな)"
RULE_WORDS = r"(毎月|これからずっと|ずっと|今月から|今月以降|来月以降|これから毎|いつも)"
ONCE_WORDS = r"(今月だけ|今回だけ|今回のみ|今月のみ|今月は|今月限り|来月から元に戻|来月は元に戻)"


def read_period(text: str) -> str | None:
    t = text
    if re.search(NEG_RULE, t):
        return "once"
    if re.search(ONCE_WORDS, t):
        return "once"
    if re.search(r"来月から(?!.*元に戻)", t):
        return "rule"
    if re.search(RULE_WORDS, t):
        return "rule"
    t2 = re.sub(r"今月(中に|の希望休|の表)", "", t)
    if "今月" in t2:
        return "once"
    return None


def read_answer(text: str) -> str | None:
    """「毎月ですか、今月だけですか」への返事。読めなければ None（もう一度聞く）。"""
    t = text.strip()
    if re.search(NEG_RULE, t) or re.search(r"(今月|今回|一回|1回)", t):
        return "once"
    if re.search(r"(毎月|ずっと|これから|いつも)", t):
        return "rule"
    return None
