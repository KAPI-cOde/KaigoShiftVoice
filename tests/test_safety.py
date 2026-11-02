"""安全の試験。Claude の返事がアプリの確かめで落ちた時、表もルールも変わらない（完了条件4の c）。
名簿にない名前は送らずに聞き返す（a）は test_utterance.py の test_unknown_name_not_sent と test_t2.py の
test_t2_01_name_not_on_roster、送る文に名簿の実名が入らない（b）は test_r9_no_names_sent が確かめる。"""
import json

import pytest

from shiftvoice.names import Masker
from shiftvoice.reader import RecordedReader

from test_t2 import snap, unchanged

TEXT = "岡田さん、15と16は法事で休み"


def _bad(code):
    """確かめの種類ごとに、型・人・日・勤務・回数のどれか1つだけがおかしい返事。"""
    return {
        "type": [{"type": "teleport", "staff": code, "days": [15]}],
        "person": [{"type": "day_off", "staff": "職員99", "days": [15]}],
        "day": [{"type": "day_off", "staff": code, "days": [40]}],
        "shift": [{"type": "set_cell", "staff": code, "days": [15], "shift": "X"}],
        "count": [{"type": "count_limit", "staff": code, "shift": "夜", "max": -1}],
    }


@pytest.mark.parametrize("what", ["type", "person", "day", "shift", "count"])
def test_bad_reply_changes_nothing(sess, what):
    masked, _ = Masker(sess.store.staff(), sess.store.facility()).mask(TEXT)
    code = masked[:4]
    assert code.startswith("職員")
    sess.reader = RecordedReader({masked: json.dumps({"items": _bad(code)[what]}, ensure_ascii=False)})
    b = snap(sess)
    r = sess.say(TEXT)
    assert r.kind in ("ask", "error"), r
    unchanged(b, snap(sess))
