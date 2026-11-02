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
        # 人を省いた返事。そのままだと「全員 15・16日 休み」になる（レビュー b の指摘）
        "nobody": [{"type": "day_off", "days": [15, 16]}],
        "nobody_count": [{"type": "count_limit", "staff": [], "shift": "夜", "max": 0}],
    }


@pytest.mark.parametrize("what", ["type", "person", "day", "shift", "count", "nobody", "nobody_count"])
def test_bad_reply_changes_nothing(sess, what):
    masked, _ = Masker(sess.store.staff(), sess.store.facility()).mask(TEXT)
    code = masked[:4]
    assert code.startswith("職員")
    sess.reader = RecordedReader({masked: json.dumps({"items": _bad(code)[what]}, ensure_ascii=False)})
    b = snap(sess)
    r = sess.say(TEXT)
    # 型が知らない物なら読み取りの失敗、それ以外はアプリの確かめで聞き返す（録った返事が無くて落ちたのではない）
    assert r.kind == ("error" if what == "type" else "ask"), r
    assert "録った返事" not in r.message
    unchanged(b, snap(sess))


def test_mask_call_and_prefix_before_digits_and_shifts(store):
    """呼び方や名前の頭のすぐ後ろに数字や勤務が続いても伏せる（読点の落ちた声の一言。レビュー c の指摘）。
    ひだまりの名簿は2字で呼び方が無いので、呼び方と3字以上の名前の職員を作り話で足して試す。"""
    from shiftvoice.model import Staff
    staff = store.staff() + [Staff(name="遠藤ゆう", unit=store.facility().units[0], can=["日"], call="ゆうちゃ")]
    m = Masker(staff, store.facility())
    for text in ("遠藤15日休み", "遠藤夜勤なし", "ゆうちゃ20日休み", "ゆうちゃ早番で", "遠藤、二十日休み"):
        masked, _ = m.mask(text)
        assert "遠藤" not in masked and "ゆうちゃ" not in masked, (text, masked)


def _endo(store):
    from shiftvoice.model import Staff
    return store.staff() + [Staff(name="遠藤ゆう", unit=store.facility().units[0], can=["日"], call="ゆうちゃ")]


def test_mask_leftover_is_not_sent(store):
    """区切りが無くて伏せきれない呼び方・名前の頭は、送る前に聞き返しに回す（再レビュー c の指摘）。"""
    m = Masker(_endo(store), store.facility())
    for text, alias in (("ゆうちゃ来週休み", "ゆうちゃ"), ("遠藤午後休み", "遠藤"), ("ゆうちゃから電話", "ゆうちゃ")):
        masked, unknown = m.mask(text)
        assert alias in unknown and m.leftover[alias] == "遠藤ゆう", (text, masked, unknown)
    assert m.mask("遠藤ゆう来週休み")[1] == []          # 名前全体なら区切りなしで伏せる
    assert m.mask("職員の休み")[1] == []
    assert " " in m.mask("ゆうちゃ15日休み")[0]          # 番号と日付がつながらない


def test_nobody_reply_when_called_by_prefix_or_kana(store):
    """名前の頭だけ・かなで呼んでも、人を省いた返事は全員に効かせない。「全員」と言った時は通す（再レビュー a・b）。"""
    from shiftvoice.intake import Clarify, to_proposal
    fac, m = store.facility(), store.month(2026, 11)
    off = [{"type": "day_off", "days": [15, 16]}]
    for text, staff in (("遠藤さん、15と16は休み", _endo(store)), ("おかださん、15と16は休み", store.staff())):
        with pytest.raises(Clarify):
            to_proposal(off, text, fac, staff, m)
    seq = [{"type": "forbid_sequence", "first": "夜", "next": "早"}]
    p = to_proposal(seq, "小島さんも含めて全員、夜勤の次の日に早番を置かない", fac, store.staff(), m)
    assert p.rules and not p.rules[0]["who"]
