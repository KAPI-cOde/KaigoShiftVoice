"""「公休になってるけど研修だった」を、今の表の勤務の頼みと読んだ時に、言った勤務へ直す（2026-11-06 ふだんの言い方の組A）。"""
from types import SimpleNamespace as T

from shiftvoice.intake import _was_really

FAC = T(shift_types=[T(name="公休", symbol="公", kind="rest"), T(name="研修", symbol="研", kind="work"),
                     T(name="早番", symbol="早", kind="work"), T(name="日勤", symbol="日", kind="work")])
TEXT = "職員05さんの16日、公休になってるけど研修だったわ。直しといて。"


def test_rest_read_as_was():
    got = _was_really([{"type": "set_cell", "staff": "職員05", "days": [16], "shift": "公", "strength": "wish"}], TEXT, FAC)
    assert got[0]["shift"] == "研" and got[0]["days"] == [16]


def test_day_off_read_as_was():
    got = _was_really([{"type": "day_off", "staff": "職員05", "days": [16]}], TEXT, FAC)
    assert got[0]["type"] == "set_cell" and got[0]["shift"] == "研"


def test_right_reading_stays():
    it = [{"type": "set_cell", "staff": "職員05", "days": [16], "shift": "研", "strength": "wish"}]
    assert _was_really(it, TEXT, FAC) == it


def test_other_words_stay():
    it = [{"type": "day_off", "staff": "職員05", "days": [16]}]
    assert _was_really(it, "職員05さん、16日は公休で", FAC) == it
    assert _was_really(it, "職員05さん、16日は早番だけど公休にして", FAC) == it
