"""受け止めの読み直し（2026-11-04 夜）で2人以上・2回挙がった所。
- 配った表から何マス直したかを、画面の「配った後」と印刷の紙の1行で言う（主任役が2回）
- 返す案を採った月は、名前の横に返した物を出す（採った後の表では黄色が消えて、誰に何を返したか見えない。2人）
- 休みの電話の案のカードに「待機」の一言の説明（審査員役）"""
import pathlib

import shiftvoice
from shiftvoice.nextflow import NextFlow
from shiftvoice.reader import RecordedReader
from shiftvoice.session import Session

from conftest import FIXED_NOW, REPLIES
from test_act2 import CALL, STANDBY, act2, kojima_rest  # noqa: F401


def html():
    return (pathlib.Path(shiftvoice.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")


def test_配った表から何マス直したかを言う(act2):
    s = act2
    s.say(STANDBY)
    assert s.state()["dist_changed"] is None              # まだ配っていない
    s.distribute()
    st = s.state()
    assert st["dist_changed"] == 0 and st["dist_at"]
    r = s.accept(s.say(CALL).uid)
    assert r.kind == "applied"
    assert s.state()["dist_changed"] == len(r.changes) > 0
    s.undo()
    assert s.state()["dist_changed"] == 0                  # 戻せば配った表のまま


def test_採った月は名前の横に返した物を出す(act2):
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
        return d.state()["returned_people"]
    assert fl.adopt("base")["ok"]
    assert dec() == {}                                     # 返さない案には出さない
    assert fl.adopt("give")["ok"]
    got = dec()
    assert got and all(("有給" in v or "連勤" in v) for v in got.values()), got
    assert s.state()["returned_people"] == {}              # 11月の画面には出ない


def test_画面に紙の1行と待機の説明がある():
    h = html()
    assert 'id="printnote"' in h and "beforeprint" in h and "マス直した表です" in h
    assert "待機＝その日休みの人の中から決めておく、急な休みの時に最初に頼む人" in h
    assert "S.returned_people" in h
