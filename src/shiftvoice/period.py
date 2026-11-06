"""毎月か今月だけかを、言葉から手元の規則で読む（8章。AI に決めさせない）。

返す値は "rule"（毎月）・"once"（今月だけ）・None（言っていない → その場で聞く）。
- 毎月・これからずっと・今後・今月から・今月以降・来月から（「元に戻して」を除く）は毎月
- 今月だけ・今回だけ・今回のみ・今月は・今月・「来月から元に戻して」は今月だけ
- 打ち消し（毎月じゃなくて・毎月ではなく）は今月だけ
- 「今月だけじゃなくて」は、ほかに続く言葉（毎月・これからも・ずっと など）がある時は毎月、無ければ聞く
- 「今月中に」「今月の希望休」は数えない
"""
from __future__ import annotations

import datetime as dt
import re

NEG_RULE = r"毎月(じゃ|では|でな)"
# 「いつも」「ずっと」は理由の言い方にも出る（「いつも早番が多くてつらい」）ので、期間の言葉に数えない（Astra 2026-11-02 の指摘3）
RULE_WORDS = r"(毎月|これからずっと|今月から|今月以降|来月以降|これから毎|今後)"
ONCE_WORDS = r"(今月だけ|今回だけ|今回のみ|今月のみ|今月は|今月限り|来月から元に戻|来月は元に戻)"
# 「今月だけじゃなくて毎月」「今月だけでなく、これからも」。ONCE_WORDS の「今月だけ」に当たって今月だけと読んでいた
# （2026-11-06 AI のテストプレイ）。「今月だけじゃなくて来月も」のように毎月と言っていない時は聞く
NOT_ONLY = r"(?:今月|今回)(?:だけ|のみ|限り)(?:じゃ|では|でな|でも?な|ならず)|今月に限らず"
GOES_ON = r"毎月|これから|ずっと|今後|以降|この先"


def _not_only(t: str) -> str | None:
    """「今月だけじゃなくて」を言っていれば "rule" か "keep_asking"、言っていなければ None。"""
    m = re.search(NOT_ONLY, t)
    if m is None:
        return None
    return "rule" if re.search(GOES_ON, t[:m.start()] + t[m.end():]) else "keep_asking"


def read_period(text: str) -> str | None:
    t = text
    if re.search(NEG_RULE, t):
        return "once"
    said = _not_only(t)
    if said is not None:
        return "rule" if said == "rule" else None
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
    said = _not_only(t)
    if said is not None:
        return "rule" if said == "rule" else None
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


_ZEN = str.maketrans("０１２３４５６７８９／", "0123456789/")
# 「12月の15日」「12月15日」「12月は15と16」。「11月」の中の「1月」は拾わない。「12月から」「12月いっぱい」「1月31日まで」
# 「12月1日から」は、ほかの月からの変更や期限の言い方なので数えない。「12/15」「2026/12/15」も同じ読み方
_MD = r"(\d{1,2})(?:月(?:の|は|に)?\s*|/)(\d{1,2})"
_MONTH_DAY = re.compile(r"(?<![0-9/])(?:\d{4}[/年])?" + _MD + r"(?:日)?(?![0-9/])(?!日?(?:まで|から|以降|より|迄))")
# 「12月1日から3日まで」「12/1〜12/3」は、始めも終わりも言った期間。上の「から」「まで」の例外に当たるが、
# 終わりの日まで言っているので、ほかの月からの変更ではなく、その月の日付の頼み（Astra 2026-11-06 の指摘6）。
# 終わりの数の後が日以外の単位の時（「12月1日から3人」「12月1日から3か月」）は期間と読まない
_PERIOD = re.compile(r"(?<![0-9/])(?:\d{4}[/年])?" + _MD + r"日?\s*(?:から|より|[〜～~ー－-])\s*"
                     r"(?:(\d{1,2})(?:月(?:の)?\s*|/))?(\d{1,2})(?![0-9]|[人回名時分週月年番度ヶケカかヵ])")


def other_month(text: str, month: int) -> int | None:
    """一言が、開いている月（month）と違う月の日付を言っていれば、その月。言っていなければ None。
    11月の表を開いたまま「12月の15日と16日は希望休」と言うと、Claude は日だけを返し、11月の表に入っていた
    （2026-11-06 の確かめ。16マス動いた）。Claude へ送る前に手元で見つけて聞き返す。
    期間は、始めも終わりも開いている月の外の時だけ数える（「11月20日から12月5日まで」は開いている月にかかる）"""
    text = text.translate(_ZEN)
    for m in _PERIOD.finditer(text):
        mo, end = int(m.group(1)), int(m.group(3) or m.group(1))
        if 1 <= mo <= 12 and mo != month and end != month:
            return mo
    for m in _MONTH_DAY.finditer(text):
        mo = int(m.group(1))
        if 1 <= mo <= 12 and mo != month:
            return mo
    return None


def other_month_message(said: int, month: int, next_month: int | None = None, *, what: str = "表もルールも変えていません") -> str:
    """聞き返しの文。言った月が翌月なら、画面のボタン［〇月へ］で開く道を言う。"""
    if next_month is not None and said == next_month:
        return f"{said}月の頼みです。［{said}月へ］で{said}月を開いてから言ってください。{what}"
    return f"{said}月の頼みです。いま開いているのは{month}月です。{said}月の表で言ってください。{what}"
