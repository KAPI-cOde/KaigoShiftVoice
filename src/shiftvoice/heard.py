"""声で届いた一言の書き方を、送る前にそろえる。

声の文字起こしは、勤務の名前をひらがなで返したり（おそばん・はやばん）、同じ音の別の字にしたり（高級→公休、連金→連勤）、
数をひらがなで返したりする（はっかい）。本物の Haiku は「おそばん」を夜勤と読み、「高級」を日勤と当て推量し、
「はっかい」を数に読めずに回数を空にした（2026-11-08 の言い回しの測り）。
勤務の名前は、その施設にその名前の勤務がある時だけ直す（「早出」の施設で「はやばん」を早番にしない）。
"""
from __future__ import annotations

import re

# 勤務の名前のひらがなの読みと、声で取り違えやすい字
READINGS = {
    "早番": ("はやばん", "はや版", "早版"),
    "日勤": ("にっきん", "日輪"),
    "遅番": ("おそばん", "おそ版", "遅版"),
    "夜勤": ("やきん",),
    "公休": ("こうきゅう", "高級"),
    "有給": ("ゆうきゅう", "悠久"),
    "研修": ("けんしゅう",),
}
# 勤務の名前でなく、施設に無くても使う言葉
WORDS = {"連勤": ("れんきん", "連金", "錬金"), "希望休": ("希望給", "希望求"), "絶対": ("絶大",)}

_ONES = {"いち": 1, "いっ": 1, "に": 2, "さん": 3, "よん": 4, "よ": 4, "ご": 5, "ろく": 6, "ろっ": 6,
         "なな": 7, "しち": 7, "はち": 8, "はっ": 8, "きゅう": 9}
_ONE_ALT = "|".join(sorted(_ONES, key=len, reverse=True))
_UNIT = {"かい": "回", "にん": "人", "にち": "日"}
_NUM = re.compile(rf"(?:(に|さん)?(じゅう|じゅっ|じっ))?({_ONE_ALT})?(かい|にん|にち)")


def _value(m: re.Match) -> int | None:
    pre, ten, one, unit = m.groups()
    if not ten and not one:
        return None
    n = (({"に": 2, "さん": 3}[pre] if pre else 1) * 10 if ten else 0) + (_ONES[one] if one else 0)
    small = one or ten   # 数の終わりの書き方。「っ」で終わるのは「かい」の前だけ、「よ」は「にん」の前だけ
    if small.endswith("っ") and unit != "かい":
        return None
    if one == "よ" and unit != "にん":
        return None
    if one == "よん" and unit == "にん":
        return None
    if unit == "にち" and n < 11:   # 「いちにち」は1日間。10日までの日付はひらがなで別の読み（ふつか など）なので触らない
        return None
    if unit == "にん" and one in ("いち", "に") and not ten:   # 1人・2人は「ひとり」「ふたり」と言う
        return None
    return n


def _numbers(text: str) -> str:
    def put(m: re.Match) -> str:
        n = _value(m)
        return m[0] if n is None else f"{n}{_UNIT[m[4]]}"
    return _NUM.sub(put, text)


def fix_heard(text: str, facility) -> str:
    """勤務の名前のひらがな・取り違えた字と、ひらがなの回数・人数・日付を直す。当たる所が無ければ文はそのまま。"""
    names = {st.name for st in facility.shift_types}
    pairs = [(w, name) for name, ws in READINGS.items() if name in names for w in ws]
    pairs += [(w, name) for name, ws in WORDS.items() for w in ws]
    for w, name in sorted(pairs, key=lambda p: len(p[0]), reverse=True):
        text = text.replace(w, name)
    # 「年金」は、後ろに日数が続く時だけ連勤の聞き違い（「年金は4日まで」）。「年金の手続き」は変えない
    text = re.sub(r"年金(?=\s*(?:は|が|も|を)?\s*[0-9０-９一二三四五六七八九十]+\s*日)", "連勤", text)
    text = re.sub(r"(続く[のん]?)早めて", r"\1やめて", text)   # 「続くのやめて」の聞き違い
    text = re.sub(r"[？?]+\s*$", "", text)   # 文末の「？」は、頼みを確かめの問いに変えてしまう（音声認識が付ける）
    return _numbers(text)
