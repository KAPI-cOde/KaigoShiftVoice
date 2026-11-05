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


def relative_days(text: str, today: dt.date, year: int, month: int) -> tuple[str, list[tuple[str, dt.date]]]:
    """「明日」「あさって」「来週の水曜」「今度の日曜」などを、表の月の「〇日」に置き換える（Claude へ送る前に手元で）。
    現場の人は日付より「明日の早番」「来週の水曜」と言うことが多い（2026-11-05、作者の書いた言い方と、別の系統の AI が
    書いた30件のうち4件で、Claude が日付を読めずに聞き返した）。返すのは、置き換えた文と、表の月の外になった言い方。
    - 週は月曜から。今週の〜曜・来週の〜曜・再来週の〜曜。今度の〜曜・次の〜曜は、今日より後のいちばん近いその曜日
    - 曜日は「曜」が付く時だけ（「次の日」は翌日の意味で、日曜ではない）
    - 置き換えないもの（Claude がそのまま読み、分からなければ聞き返す）。すぐ後に日付が言われている時（「明日の30日」
      「来週の水曜日の11日」）。「今日」（その日の出来事を記録する一言に使う）。週の始まりが人によって違う
      「今週・来週・再来週の日曜」。今日と同じ曜日の「今度の〜曜」（今日のことか来週のことか分からない）
    - 表の月の外になった時は置き換えず、言い方と日を返す（呼ぶ側が聞き返す）"""
    outside: list[tuple[str, dt.date]] = []

    def put(pattern: str, when) -> None:
        nonlocal text

        def rep(m):
            rest = m.string[m.end():]
            if re.match(r"日?の?[0-9０-９]+日", rest):   # すぐ後に日付がある（「来週の水曜日の11日」で途中の「曜」まで取らない）
                return m.group(0)
            d = when(m)
            if d is None:
                return m.group(0)
            if (d.year, d.month) != (year, month):
                outside.append((m.group(0), d))
                return m.group(0)
            return f"{d.day}日"
        text = re.sub(pattern, rep, text)

    put(r"(?:明後日|あさって)", lambda m: today + dt.timedelta(days=2))
    put(r"(?:明日(?![" + _NAME_TAIL + r"])|あした)", lambda m: today + dt.timedelta(days=1))
    monday = today - dt.timedelta(days=today.weekday())

    def week(m):
        wd = WEEKDAYS.index(m.group(2))
        which = m.group(1)
        if which in ("今度", "次"):
            if wd == today.weekday():
                return None   # 今日のことか来週のことか分からない
            return today + dt.timedelta(days=(wd - today.weekday()) % 7)
        if m.group(2) == "日":
            return None       # 週を日曜から数える人もいて、どの日曜か分からない
        return monday + dt.timedelta(days=wd + {"今週": 0, "来週": 7, "再来週": 14}[which])
    put(r"(再来週|来週|今週|今度|次)の?([" + WEEKDAYS + r"])曜日?", week)
    return text, outside


def resolve_relative_days(text: str, today: dt.date, year: int, month: int) -> str:
    """relative_days の置き換えた文だけ（試験で、送る文と同じ文を作るのに使う）。"""
    return relative_days(text, today, year, month)[0]
