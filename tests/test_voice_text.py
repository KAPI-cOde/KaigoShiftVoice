"""声の文字の漢数字の直し（声のモデルは要らない）。"""
from shiftvoice.voice import kanji_digits


def test_kanji_digits():
    assert kanji_digits("小島さん、二十九日に試験だから、それまで夜勤三回まで") == "小島さん、29日に試験だから、それまで夜勤3回まで"
    assert kanji_digits("十一月一日の日曜") == "11月1日の日曜"
    # 言い回しは壊さない・読めない並びで落ちない（2026-11-02 のレビューの指摘E）
    for t in ("一時的に", "一日中", "一人ずつ", "二十二十日", "一言で"):
        assert kanji_digits(t) == t
