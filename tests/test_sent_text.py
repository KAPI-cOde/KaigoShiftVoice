"""画面に出す「Claude へ送った文」。伏せた後の文が返事に付き、呼ばなかった時は None。"""
from shiftvoice.reader import RecordedReader, ReadError

from conftest import REPLIES


def test_sent_is_masked_text(sess):
    names = [s.name for s in sess.store.staff()]
    r = sess.say("岡田さん、15と16は法事で休み")
    assert r.sent_known and r.sent
    assert not any(n in r.sent for n in names)
    assert r.sent == sess.reader.sent[-1].messages[-1]["content"] or r.sent in str(sess.reader.sent[-1].messages)


def test_sent_kept_on_period_answer(sess):
    r = sess.say("遅番の次の日に早番はきついから、やめてあげて")
    assert r.kind == "ask_period" and r.sent_known and r.sent
    r2 = sess.answer(r.uid, "毎月")
    assert r2.sent == r.sent


def test_unknown_name_not_sent(sess):
    r = sess.say("山本さん、15日は休み")
    assert r.kind == "ask" and r.sent_known and r.sent is None


def test_no_key_not_sent(sess):
    from shiftvoice.server import NoKeyReader
    sess.reader = NoKeyReader()
    r = sess.say("岡田さん、15と16は法事で休み")
    assert r.kind == "error" and r.sent_known and r.sent is None
