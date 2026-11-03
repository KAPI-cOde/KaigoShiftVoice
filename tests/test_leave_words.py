"""日付を言った有給の一言は、Claude の読みが揺れても、その日の有給のマスになる（2026-11-03、本物の Haiku が
同じ一言を「休み」「有給の日数」「その両方」のどれにも読み、両方の時は「頼みが混ざっています」で止まった）。"""
import json

import pytest

from shiftvoice.names import Masker
from shiftvoice.reader import RecordedReader


def _say(sess, text, items):
    masked, _ = Masker(sess.store.staff(), sess.store.facility()).mask(text)
    code = masked.split("さん")[0]
    for it in items:
        it.setdefault("staff", code)
    sess.reader = RecordedReader({masked: json.dumps({"items": items}, ensure_ascii=False)})
    r = sess.say(text)
    if r.kind == "ask_period":
        r = sess.answer(r.uid, "once")
    return r


@pytest.mark.parametrize("items", [
    [{"type": "day_off", "days": [20], "reason": "有給"}],
    [{"type": "leave_count", "count": None, "reason": None}],
    [{"type": "day_off", "days": [20], "reason": "有給"}, {"type": "leave_count", "count": None, "reason": None}],
], ids=["休みと読む", "日数と読む", "両方と読む"])
def test_dated_leave_becomes_leave_cell(sess, items):
    sess.open()
    r = _say(sess, "岡田さん、20日は有給で", [dict(i) for i in items])
    assert r.kind == "applied", r.message
    assert sess.store.month(2026, 11).table["岡田"][19] == "有"


def test_leave_count_without_date_is_unchanged(sess):
    """日付を言わない有給（日は任せる）は、これまでどおり有給の日数の頼み（1幕の撮影の一言の形）。"""
    sess.open()
    r = _say(sess, "岡田さん、有給を2日とりたいって", [{"type": "leave_count", "count": 2, "reason": None}])
    assert r.kind == "applied", r.message
    assert sess.store.month(2026, 11).table["岡田"].count("有") == 2
