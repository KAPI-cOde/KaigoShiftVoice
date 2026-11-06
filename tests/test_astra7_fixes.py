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


def test_self_correction_is_not_a_move():
    from shiftvoice.intake import _self_fix
    said = "職員07です。25、あ、ごめん26日。早番になってるとこ、研修なんです。"
    got = _self_fix([{"type": "move", "staff": "職員07", "shift": "研", "from_day": 25, "to_day": 26}], said)
    assert got[0]["type"] == "set_cell" and got[0]["days"] == [26] and got[0]["shift"] == "研"
    moved = [{"type": "move", "staff": "職員07", "shift": "研", "from_day": 13, "to_day": 20}]
    assert _self_fix(moved, "職員07さんの研修、13日から20日に移った") == moved


def test_leave_said_is_not_rest():
    from shiftvoice.intake import _tidy_leave
    it = [{"type": "set_cell", "staff": "職員10", "days": [7], "shift": "公"}]
    assert _tidy_leave(it, "職員10さん、7日は有給取るって。日勤のままになってたから直しといて。")[0]["shift"] == "有"
    assert _tidy_leave(it, "職員10さん、7日は有給じゃなくて公休で")[0]["shift"] == "公"


def test_self_fix_uses_only_numbers():
    from shiftvoice.intake import _self_fix
    odd = [{"type": "move", "staff": "職員07", "shift": "研", "from_day": "(a+)+", "to_day": [26]}]
    assert _self_fix(odd, "職員07です。25、あ、ごめん26日") == odd
