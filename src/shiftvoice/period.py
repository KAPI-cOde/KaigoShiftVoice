"""毎月か今月だけかを、言葉から手元の規則で読む（8章。AI に決めさせない）。

返す値は "rule"（毎月）・"once"（今月だけ）・None（言っていない → その場で聞く）。
- 毎月・これからずっと・今後・今月から・今月以降・来月から（「元に戻して」を除く）は毎月
- 今月だけ・今回だけ・今回のみ・今月は・今月・「来月から元に戻して」は今月だけ
- 打ち消し（毎月じゃなくて・毎月ではなく）は今月だけ
- 「今月中に」「今月の希望休」は数えない
"""
from __future__ import annotations

import datetime as dt
import re

NEG_RULE = r"毎月(じゃ|では|でな)"
# 「いつも」「ずっと」は理由の言い方にも出る（「いつも早番が多くてつらい」）ので、期間の言葉に数えない（Astra 2026-11-02 の指摘3）
RULE_WORDS = r"(毎月|これからずっと|今月から|今月以降|来月以降|これから毎|今後)"
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


WEEKDAYS = "月火水木金土日"
# 「明日香」「明日美」のような名前の頭を日付にしない
_NAME_TAIL = "香美菜奈花子実海"


def resolve_relative_days(text: str, today: dt.date, year: int, month: int) -> str:
    """「明日」「あさって」「来週の水曜」「今度の日曜」などを、表の月の「〇日」に置き換える（Claude へ送る前に手元で）。
    現場の人は日付より「明日の早番」「来週の水曜」と言うことが多い（2026-11-05、作者の書いた言い方と、別の系統の AI が
    書いた30件のうち4件で、Claude が日付を読めずに聞き返した）。決まりは次のとおり。
    - 週は月曜から。今週の〜曜・来週の〜曜・再来週の〜曜。今度の〜曜・次の〜曜は、今日より後のいちばん近いその曜日
    - 曜日は「曜」が付く時だけ（「次の日」は翌日の意味で、日曜ではない）
    - 置き換えた日が表の月の外なら置き換えない（Claude が聞き返す）。すぐ後に日付が言われている時（「明日の30日」）も
      置き換えない。「今日」は置き換えない（その日の出来事を記録する一言に使うため）"""
    def day(d: dt.date) -> str | None:
        return f"{d.day}日" if (d.year, d.month) == (year, month) else None

    def put(pattern: str, when) -> None:
        nonlocal text
        def rep(m):
            got = day(when(m))
            return got if got else m.group(0)
        text = re.sub(pattern, rep, text)

    explicit = r"(?!の?[0-9０-９]+日)"
    put(r"(?:明後日|あさって)" + explicit, lambda m: today + dt.timedelta(days=2))
    put(r"(?:明日(?![" + _NAME_TAIL + r"])|あした)" + explicit, lambda m: today + dt.timedelta(days=1))
    monday = today - dt.timedelta(days=today.weekday())

    def week(m):
        wd = WEEKDAYS.index(m.group(2))
        which = m.group(1)
        if which in ("今度", "次"):
            ahead = (wd - today.weekday()) % 7 or 7
            return today + dt.timedelta(days=ahead)
        return monday + dt.timedelta(days=wd + {"今週": 0, "来週": 7, "再来週": 14}[which])
    put(r"(再来週|来週|今週|今度|次)の?([" + WEEKDAYS + r"])曜日?" + explicit, week)
    return text
