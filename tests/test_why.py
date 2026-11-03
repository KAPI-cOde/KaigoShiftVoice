"""なぜこのマスか（why.py・docs/rule-cleanup.md の4）。撮影の11月末の表で、答えが式どおりか。"""
import shutil
from pathlib import Path

import pytest

from shiftvoice.build import month_rules
from shiftvoice.store import Store
from shiftvoice.why import why_cell

END = Path(__file__).resolve().parents[1] / "data" / "runtime" / "states" / "end"


@pytest.fixture
def end(tmp_path):
    if not END.exists():
        pytest.skip("撮影の11月末の状態が手元に無い（data/runtime は公開しない）")
    shutil.copytree(END, tmp_path / "e")
    st = Store(tmp_path / "e")
    m = st.month(2026, 11)
    return st, m, month_rules(st, 2026, 11, with_premise=False)


def _w(end, name, day):
    st, m, rules = end
    return why_cell(rules, st.facility(), st.staff_on(2026, 11), m, m.table, name, day)


def test_w1_request_comes_first(end):
    w = _w(end, "岡田", 15)
    assert w["symbol_name"] == "公休" and w["because"][0]["fact"] == "この日の頼み"


def test_w2_cannot_uses_sign(end):
    """明けを入れる側の式（夜勤の翌日は明け）は、明けにできない理由にしない。"""
    w = _w(end, "小島", 1)
    by = {x["shift"]: x["name"] for x in w["cannot"]}
    assert by.get("夜勤", "").startswith("小島さん 夜勤は")
    assert by.get("明け") == "明けは夜勤の翌日だけ"


def test_w3_limit(end):
    w = _w(end, "斉藤", 4)
    assert len(w["because"]) <= 3 and len(w["cannot"]) <= 3


def test_w4_every_cell_on_seed_is_consistent(sess):
    """公開のデータ（ひだまりの11月の元の表）で、全部のマスについて、理由のルールはそのマスの勤務を数え、
    できない理由のルールはそのマスの勤務を数えていない（式どおり）。session.why の口も通す。"""
    m = sess.store.month(2026, 11)
    rules = {r.id: r for r in month_rules(sess.store, 2026, 11, with_premise=False)}
    n = 0
    for name in m.table:
        for day in range(1, m.days + 1):
            w = sess.why(name, day)
            assert "error" not in w
            for x in w["cannot"]:
                assert w["symbol_name"] not in x["shift"].split("・")
            n += len(w["because"]) + len(w["cannot"])
    assert n > 0
    assert sess.why("いない人", 1) == {"error": "そのマスはありません"}
