"""3幕で翌月を採った後、その翌月を開ける（2026-11-04 敵対レビューの線超え。翌月の layers の _plans・_adopted を
当番として読み、state() が KeyError で落ち、採った翌月の表を印刷も Excel へ貼ることもできなかった）。"""
from shiftvoice.nextflow import NextFlow
from shiftvoice.server import table_tsv
from shiftvoice.session import Session

from conftest import FIXED_NOW, REPLIES
from shiftvoice.reader import RecordedReader
from test_act2 import CALL, STANDBY, act2, kojima_rest  # noqa: F401


def test_adopted_next_month_opens_and_exports(act2):
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
    assert fl.adopt("give")["ok"]
    dec = Session(s.store, 2026, 12, RecordedReader.from_file(REPLIES), now=lambda: FIXED_NOW)
    dec.open()
    st = dec.state()
    assert "_plans" not in st["layers"] and "_adopted" not in st["layers"]
    rows = table_tsv(st).splitlines()
    assert len(rows) >= 2 + 16 and rows[0].split("\t")[-1] == "31"     # 12月は31日まで
