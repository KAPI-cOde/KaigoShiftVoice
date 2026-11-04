"""返す案を採った後に翌月の表を作り直しても、同じ引き受けを数えて返す案を作れる。採り直すと印を付け直し、
返さない案を採ったら印を外して次の月へ持ち越す（2026-11-05 敵対レビュー 3×8。前は作り直すと「引き受けた人は
いません」になり、採り直すと有給も短い連勤も返していない表に置き換わり、返した印だけが残った）。"""
from shiftvoice.nextflow import NextFlow

from test_act2 import CALL, STANDBY, act2, kojima_rest  # noqa: F401


def _flow(s):
    s.say(STANDBY)
    s.distribute()
    s.accept(s.say(CALL).uid)
    kojima_rest(s)
    fl = NextFlow(s.store, 2026, 11)
    v = fl.review()
    fl.answer(v["cards"][0]["rule_id"], "archive")
    for c in v["cards"][1:]:
        fl.answer(c["rule_id"], "keep")
    return fl


def _marked(store):
    return sorted(x.id for x in store.acceptances() if x.returned_in == "2026-12")


def test_採った後に作り直しても返す案を作り採り直しで印を付け直す(act2):
    s = act2
    fl = _flow(s)
    first = fl.build()
    assert first["status"] == "ok" and first["who"] == "小島"
    assert fl.adopt("give")["ok"]
    marked = _marked(s.store)
    assert marked
    dec_leave = s.store.month(2026, 12).table["小島"].count("有")

    again = fl.build()                       # 採った後に作り直す
    assert again["status"] == "ok" and again["who"] == "小島", again.get("main")
    assert again["main"] == first["main"]
    assert fl.adopt("give")["ok"]            # 採り直す
    assert _marked(s.store) == marked
    assert s.store.month(2026, 12).table["小島"].count("有") == dec_leave >= 1


def test_返さない案を採り直したら印を外して次の月へ持ち越す(act2):
    s = act2
    fl = _flow(s)
    assert fl.build()["status"] == "ok"
    assert fl.adopt("give")["ok"] and _marked(s.store)
    assert fl.adopt("base")["ok"]
    assert _marked(s.store) == []
