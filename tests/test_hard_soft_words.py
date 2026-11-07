"""「ハードルール」「ソフトルール」「強めのソフト」は強さの言葉。理由の欄に入れない（作者 2026-11-07「ハードルールで出勤を3回まで
にしてください」と言うと、理由に「ハードル」と出た）。アプリの強さは 動かせない・運用の都合・本人の希望・相談できる。"""
from shiftvoice.intake import _count_strength, to_proposal


def test_strength_words():
    assert _count_strength("operational", "小島さん、ハードルールで夜勤は3回まで", True) == "fixed"
    assert _count_strength("operational", "小島さん、ソフトルールで夜勤は3回まで", True) == "negotiable"
    assert _count_strength("negotiable", "小島さん、強めのソフトで夜勤は3回まで", True) == "operational"


def test_strength_words_are_not_a_reason(store):
    fac, staff, m = store.facility(), store.staff(), store.month(2026, 11)
    items = [{"type": "count_limit", "staff": ["小島"], "shift": "夜", "min": None, "max": 3, "until_day": None,
              "strength": "fixed", "reason": "ハードル"}]
    p = to_proposal(items, "小島さん、ハードルールで夜勤は3回までにしてください", fac, staff, m)
    assert not p.reason
    assert p.rules[0]["flex"] == "fixed"
