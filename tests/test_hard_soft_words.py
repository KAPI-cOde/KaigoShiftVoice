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


def test_名指しの続く日の上限は_ソフトルールと言えば相談できるのまま(store):
    """名指しの続く日の上限は「できるだけ」でも運用の都合へ上げるが、作者が「ソフトルールで」と強さを言った時まで上げていた"""
    fac, staff, m = store.facility(), store.staff(), store.month(2026, 11)

    def flex(text):
        items = [{"type": "consecutive_limit", "staff": ["千葉"], "shift": "夜", "max": 1, "except_staff": None,
                  "strength": "negotiable", "reason": None}]
        return to_proposal(items, text, fac, staff, m).rules[0]["flex"]
    assert flex("千葉さん、ソフトルールで夜勤は単発で") == "negotiable"
    assert flex("千葉さんの夜勤はソフトで2日続けない") == "negotiable"
    assert flex("千葉さんの夜勤はできるだけ単発で") == "operational"
    assert flex("千葉さん、強めのソフトで夜勤は単発で") == "operational"
    assert flex("千葉さん、ハードルールで夜勤は単発で") == "fixed"
