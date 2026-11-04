"""画面の言葉の補足（2026-11-04 はじめて見る人の読み）。休みを移す日の候補の添え書きと、採った月の1行。"""
from shiftvoice.nextflow import NextFlow
from shiftvoice.reader import RecordedReader
from shiftvoice.session import Session

from conftest import FIXED_NOW, REPLIES
from test_act2 import CALL, STANDBY, act2, kojima_rest  # noqa: F401


def _html():
    import pathlib
    import shiftvoice
    return (pathlib.Path(shiftvoice.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")


def test_moves_say_how_they_differ_from_first(act2):
    s = act2
    s.say(STANDBY)
    s.distribute()
    r = s.say(CALL)
    mv = r.extra["moves"]
    assert len(mv) >= 2
    assert mv[0]["vs_first"] == ""                      # 1つ目は添えない
    for shown in mv[1:]:
        t = shown["vs_first"]
        assert t == "1つ目と負担は同じ" or any(w in t for w in ("つ多い", "人多い", "件多い", "連勤が", "なくなる")), t
    print([m["vs_first"] for m in mv])


def test_html_has_the_headline_and_notes():
    h = _html()
    assert "負担の少ない順。負担が同じ時は、電話の日に近い日を先にしています" in h
    assert "赤＝その人の負担が増える所" in h
    assert "vs_first" in h


def test_returned_note_only_when_give_adopted(act2):
    s = act2
    s.say(STANDBY)
    s.distribute()
    s.accept(s.say(CALL).uid)
    kojima_rest(s)
    fl = NextFlow(s.store, 2026, 11)
    v = fl.review()
    fl.answer(v["cards"][0]["rule_id"], "archive")
    for c in v["cards"][1:]:
        fl.answer(c["rule_id"], "keep")
    assert fl.build()["status"] == "ok"

    def dec():
        d = Session(s.store, 2026, 12, RecordedReader.from_file(REPLIES), now=lambda: FIXED_NOW)
        d.open()
        return d.state()["returned_note"]
    assert dec() == ""                                  # まだ採っていない
    assert fl.adopt("base")["ok"]
    assert dec() == ""                                  # 返さない案を採った月には出さない
    assert fl.adopt("give")["ok"]
    assert dec() == "11月に引き受けた人に、有給と短い連勤を返した12月の表です"
    assert s.state()["returned_note"] == ""             # 11月の画面には出ない


def test_vs_first_names_what_is_more():
    from shiftvoice.standby import _vs_first
    a = {"keys": (0, 0, 3, 2, 0, 0, 1, 0), "longer": {}, "requester": "青木"}
    b = {"keys": (0, 0, 5, 2, 0, 1, 2, 0), "longer": {"小島": 5}, "requester": "青木"}
    assert _vs_first(a, b) == "変わるマスが2つ多い、小島さんの連勤が5日になる"
    assert _vs_first(a, {**a, "keys": (1, 1, 3, 3, 1, 0, 4, 0)}) == \
        "相談できるルールの外れが1件多い、青木さんの公休が元の日数でなくなる、関わる人が1人多い、短い並びが1つ多い"
    assert _vs_first(a, {**a, "keys": (0, 0, 3, 2, 0, 0, 5, 0)}) == "1つ目と負担は同じ"
