"""午後の芯の敵対レビュー（2026-11-03）で直した物。"""
import json

import pytest

from shiftvoice.names import Masker
from shiftvoice.reader import RecordedReader
from shiftvoice.session import Session


def _reader(sess, table):
    m = Masker(sess.store.staff(), sess.store.facility())
    out = {}
    for text, items in table.items():
        masked, _ = m.mask(text)
        code = {s.name: m.to_code[s.name] for s in sess.store.staff()}
        fixed = [{**it, "staff": code.get(it.get("staff"), it.get("staff"))} for it in items]
        out[masked] = json.dumps({"items": fixed}, ensure_ascii=False)
    return RecordedReader(out)


def test_cancel_offer_stopped_midway_still_opens(sess):
    """日付の無い取り消しの確かめで、帳面に入った後に止まっても、月が開ける。"""
    T = {"岡田さん、10日は休みで": [{"type": "day_off", "staff": "岡田", "days": [10]}],
         "岡田さんの休み、やっぱりなし": [{"type": "cancel", "staff": "岡田", "days": None, "shift": "公"}]}
    sess.reader = _reader(sess, T)
    sess.open()
    r = sess.say("岡田さん、10日は休みで")
    if r.kind == "ask_period":
        sess.answer(r.uid, "once")
    r = sess.say("岡田さんの休み、やっぱりなし")
    assert r.kind == "fix_offer"
    led = sess.store.ledger()
    rid = next(k for k, v in led.rules_at().items() if v.kind == "cell" and v.who == ["岡田"])
    led.append("remove", rule_id=rid, op_id=r.uid, meta={})
    sess.store.save_ledger(led)            # 帳面を保存した所で止まった
    s2 = Session(sess.store, 2026, 11, RecordedReader({}), now=sess.now)
    s2.open()
    assert not any(u["id"] == r.uid for u in sess.store.pending(2026, 11))


def test_leave_word_only_turns_its_own_day_into_leave(sess):
    """一言に有給の言葉があっても、有給と言った人の休みだけを有給にする。"""
    T = {"岡田さん20日休み、加藤さんは有給で21日": [
        {"type": "day_off", "staff": "岡田", "days": [20], "reason": None},
        {"type": "day_off", "staff": "加藤", "days": [21], "reason": "有給"}]}
    sess.reader = _reader(sess, T)
    sess.open()
    r = sess.say("岡田さん20日休み、加藤さんは有給で21日")
    if r.kind == "ask_period":
        r = sess.answer(r.uid, "once")
    t = sess.store.month(2026, 11).table
    assert t["岡田"][19] == "公" and t["加藤"][20] == "有", r.message


def test_not_leave_phrase_is_not_leave(sess):
    T = {"岡田さん、有給じゃなくて公休で22日休み": [{"type": "day_off", "staff": "岡田", "days": [22], "reason": None}]}
    sess.reader = _reader(sess, T)
    sess.open()
    r = sess.say("岡田さん、有給じゃなくて公休で22日休み")
    if r.kind == "ask_period":
        r = sess.answer(r.uid, "once")
    assert sess.store.month(2026, 11).table["岡田"][21] == "公", r.message


@pytest.mark.parametrize("text,unknown", [("利用者の森田さまの通院付き添いで岡田さん12日休み", "森田")])
def test_sama_name_is_not_sent(sess, text, unknown):
    masked, names = Masker(sess.store.staff(), sess.store.facility()).mask(text)
    assert unknown in names


@pytest.mark.parametrize("text", ["休みをちゃんと取らせて", "シフトをくんでほしい", "岡田さん、おばあちゃんの通院で12日休み",
                                  "岡田さん、じいちゃんの法事で12日休み"])
def test_common_words_with_chan_kun_do_not_stop(sess, text):
    _, names = Masker(sess.store.staff(), sess.store.facility()).mask(text)
    assert names == [], names


def test_redaction_keeps_the_date(sess):
    masked, _ = Masker(sess.store.staff(), sess.store.facility()).mask("岡田さん、12日はお子さんの熱で休み")
    assert "12日" in masked and "（家族）" in masked


@pytest.mark.parametrize("text,name", [("明けはともちゃんと話して", "とも"), ("12日はけいくんで決まり", "けい"), ("ともちゃん、12日休み", "とも"),
                                       ("かずやくんと岡田さん、12日休み", "かずや"), ("はるとくんと話して12日休み", "はると"),
                                       ("なおちゃんと岡田さん、12日休み", "なお")])
def test_name_before_chan_to_is_not_skipped(sess, text, name):
    """「〜ちゃんと」「〜くんで」でも、前に名前らしいひらがながあれば聞き返す（名前を送らない）。"""
    masked, names = Masker(sess.store.staff(), sess.store.facility()).mask(text)
    assert names, (text, masked)


def test_redaction_walks_past_kanji_name(sess):
    """漢字の名前の一部（美よ子さん）も置き換えの外に残さない。止まるのは「12日」の日だけ。"""
    masked, names = Masker(sess.store.staff(), sess.store.facility()).mask("岡田さん、12日は美よ子さんの用事で休み")
    assert "美" not in masked or names
