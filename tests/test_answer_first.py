"""聞き返し（毎月か今月だけか・置き換えるか別に足すか）が出ている間に届いた一言。

答えそのもの（ボタン、「毎月」「今月だけ」、声で言った答えだけの短い一言）でなければ、表もルールも未反映の発言も
変えず、Claude にも送らずに「先に上の質問に答えてください」と知らせる。前は、新しい一言を黙って捨てるか、
「連勤は4日までにしよう。毎月」の「毎月」を前の問いの答えに使って、連勤のルールを黙って消していた
（2026-11-04 本物の Claude で再現）。
"""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from shiftvoice.server import make_handler

LATE = "遅番の次の日に早番はきついから、やめてあげて"
LATE2 = "遅番の次の日は、早番も日勤もやめて"
FIRST = "先に上の質問に答えてください（毎月／今月だけ）"
FIRST_REPLACE = "先に上の質問に答えてください（置き換える／別に足す）"


def snap(sess):
    """表・帳面・未反映の発言・Claude へ送った回数。"""
    return {"table": json.dumps(sess.store.month(2026, 11).table),
            "rev": sess.store.ledger().rev,
            "pending": json.dumps(sess.store.pending(2026, 11), ensure_ascii=False, sort_keys=True),
            "sent": len(sess.reader.sent)}


def asking(sess):
    r = sess.say(LATE)
    assert r.kind == "ask_period"
    return r


# 1 答えでない一言は、捨てず・変えず・知らせる
@pytest.mark.parametrize("text", ["土日は早番を2人にしてほしい", "千葉さんは新人だから、まだ一人で夜勤は無理", "うーん"])
def test_not_answer_is_told_and_changes_nothing(sess, text):
    r = asking(sess)
    b = snap(sess)
    r2 = sess.answer(r.uid, text)
    assert r2.kind == "ask_period" and r2.uid == r.uid      # 問いは残る（画面の［毎月］［今月だけ］もそのまま）
    assert r2.message == FIRST
    assert r2.extra.get("answer_first") is True               # 画面はこの印を見て、打った文を入力欄に残す
    assert snap(sess) == b                                    # 表も帳面も未反映の発言も変えず、Claude にも送らない
    assert sess.store.pending(2026, 11)[0]["status"] == "ask_period"
    assert sess.answer(r.uid, "rule").kind == "applied"       # その後もボタンで答えられる


# 2 「〜。毎月」のような文を、前の問いの答えに使わない
@pytest.mark.parametrize("text", ["連勤は4日までにしよう。毎月", "毎月遅番なし", "今月は夜勤なしで"])
def test_sentence_with_period_word_is_not_used_as_answer(sess, text):
    r = asking(sess)
    b = snap(sess)
    r2 = sess.answer(r.uid, text)
    assert r2.kind == "ask_period" and r2.message == FIRST and r2.extra.get("answer_first") is True, r2
    assert snap(sess) == b                                    # 遅番のルールも入らず、新しい一言も消えない


# 3 ボタンの答えは今までどおり
@pytest.mark.parametrize("button,scope", [("rule", "rule"), ("once", "once")])
def test_button_answer_works_as_before(sess, button, scope):
    r = asking(sess)
    r2 = sess.answer(r.uid, button)
    assert r2.kind == "applied"
    assert sess.store.ledger().rules_at()[r2.rule_ids[0]].scope == scope
    assert sess.store.pending(2026, 11) == []


# 4 声で答えだけを言った時も今までどおり（文字にすると句読点や相づちが付くことがある）
@pytest.mark.parametrize("heard,scope", [("毎月", "rule"), ("毎月。", "rule"), ("今月だけ", "once"),
                                         ("えー、今月だけです。", "once"), ("毎月で", "rule")])
def test_voice_answer_only_works_as_before(sess, heard, scope):
    r = asking(sess)
    r2 = sess.answer(r.uid, heard)
    assert r2.kind == "applied", r2
    assert sess.store.ledger().rules_at()[r2.rule_ids[0]].scope == scope


def test_answer_only_words():
    """答えそのものかは、句読点を除いた全文で見る。中身の言葉が1字でも残る一言は答えにしない。"""
    from shiftvoice.session import period_answer, replace_answer
    for t, want in [("毎月", "rule"), ("今月だけ", "once"), ("毎月で", "rule"), ("毎月。", "rule"),
                    ("はい、今月だけです", "once"), ("えっと、毎月でお願いします", "rule"), ("今回だけ", "once")]:
        assert period_answer(t) == want, t
    for t in ["連勤は4日までにしよう。毎月", "毎月遅番なし", "今月は夜勤なしで", "土日は早番を2人にしてほしい",
              "うーん", "えっと", "", "置き換える"]:
        assert period_answer(t) is None, t
    for t, want in [("置き換える", "replace"), ("置き換えて。", "replace"), ("別に足す", "add"), ("別に足して", "add")]:
        assert replace_answer(t) == want, t
    for t in ["毎月", "土日は早番を2人にしてほしい", "置き換えて、土日は早番を2人に", ""]:
        assert replace_answer(t) is None, t


# 置き換えるか別に足すかの聞き返しも同じ
def test_not_answer_while_asking_replace(sess):
    a = asking(sess)
    first = sess.answer(a.uid, "毎月").rule_ids[0]
    r = sess.say(LATE2)
    if r.kind == "ask_period":
        r = sess.answer(r.uid, "毎月")
    assert r.kind == "ask_replace"
    b = snap(sess)
    r2 = sess.answer(r.uid, "土日は早番を2人にしてほしい")
    assert r2.kind == "ask_replace" and r2.uid == r.uid and r2.message == FIRST_REPLACE
    assert r2.extra.get("answer_first") is True
    assert "遅番の次の日に早番を置かない" in r2.extra.get("question", "")   # 画面は元の問いの下に知らせを出す
    assert snap(sess) == b
    assert first in sess.store.ledger().rules_at()
    done = sess.answer(r.uid, "置き換える")                  # ボタンの名前どおりに打った時は答えになる
    assert done.kind == "applied" and "置き換えました" in done.summary
    assert first not in sess.store.ledger().rules_at()


# 本物の HTTP。ボタン（/api/answer）と声（/api/voice の X-Answer-Uid）の入口
class FakeVoice:
    """声の文字起こしの代わり。聞こえた文を決めておく。"""
    text = ""

    def transcribe(self, samples):
        return self.text, 0.01


@pytest.fixture
def srv(sess):
    voice = FakeVoice()
    handler = make_handler(sess, threading.Lock(), True, voice)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", voice
    httpd.shutdown()
    httpd.server_close()


def post(base, path, body, ctype="application/json", headers=None):
    data = json.dumps(body).encode() if ctype == "application/json" else body
    req = urllib.request.Request(base + path, data=data, headers={"Content-Type": ctype, **(headers or {})})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def test_http_typed_then_button(srv, sess):
    base, _ = srv
    uid = post(base, "/api/say", {"text": LATE})["reply"]["uid"]
    b = snap(sess)
    out = post(base, "/api/answer", {"uid": uid, "text": "土日は早番を2人にしてほしい"})
    assert out["reply"]["kind"] == "ask_period" and out["reply"]["message"] == FIRST
    assert out["reply"]["extra"]["answer_first"] is True
    assert snap(sess) == b
    out = post(base, "/api/answer", {"uid": uid, "text": "once"})        # ［今月だけ］のボタン
    assert out["reply"]["kind"] == "applied"


def test_http_voice(srv, sess):
    base, voice = srv
    uid = post(base, "/api/say", {"text": LATE})["reply"]["uid"]
    b = snap(sess)
    pcm = b"\0" * 64
    voice.text = "連勤は4日までにしよう。毎月"
    out = post(base, "/api/voice", pcm, "application/octet-stream", {"X-Answer-Uid": uid})
    assert out["text"] == voice.text                                   # 画面はこの文を入力欄に置く
    assert out["reply"]["kind"] == "ask_period" and out["reply"]["extra"]["answer_first"] is True
    assert snap(sess) == b
    voice.text = "毎月"                                                  # 台本の1幕。「毎月」とだけ言う
    out = post(base, "/api/voice", pcm, "application/octet-stream", {"X-Answer-Uid": uid})
    assert out["reply"]["kind"] == "applied"
    assert sess.store.ledger().rules_at()[out["reply"]["rule_ids"][0]].scope == "rule"
