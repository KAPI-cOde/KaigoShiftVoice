"""テストプレイの直しの敵対レビューで線を超えた所（2026-11-06）。"""
from types import SimpleNamespace as T

from shiftvoice.addshift import fix_spoken
from shiftvoice.redo import fill_answer

FAC = T(shift_types=[T(name="日8", symbol="日8", spoken=True, on_request_only=True, kind="work")])


def test_other_persons_day_is_kept():
    items = [{"type": "day_off", "staff": "職員07", "days": [15]}, {"type": "day_off", "staff": "職員02", "days": []}]
    got = fill_answer({"need": "days", "items": items}, "20日です", FAC)
    assert got[0]["days"] == [15] and got[1]["days"] == [20]


def test_plain_words_are_not_changed():
    for s in ("その日はちょっと無理です", "当日はちょうど休みです"):
        assert fix_spoken(s, FAC) == s


def test_near_names_are_still_fixed():
    assert fix_spoken("10日の小島さん、日はちに", FAC) == "10日の小島さん、日8に"
    assert fix_spoken("10日の小島さん、非8に", FAC) == "10日の小島さん、日8に"
