"""読み直し（最初の読みが型に当てはまらずに止まった時だけ、文脈つきで Sonnet に読ませ直す）。
録った返事（replies.json の最初の読み・rereads.json の読み直し）で回す。鍵は要らない。"""
import json

from shiftvoice.reader import RecordedReader, parse_reread
from shiftvoice.session import Session

AOKI = "青木さんは有給消化。夜勤は単発で1〜3回なら手伝える。24日と25日と年末は夜勤に入れない"
IINKAI = "委員会の日は、できるだけ委員の3人を午前の勤務に。全員が無理なら2人いればいい"
NOT_YET = "この形の頼みは、まだ扱えません。別の言い方で言ってください"


def _sent_text(reader) -> str:
    return json.dumps([[s.system, s.messages] for s in reader.sent + reader.reread_sent], ensure_ascii=False)


def _roster_words(sess) -> set[str]:
    out = set()
    for s in sess.store.staff():
        out.add(s.name)
        if s.call:
            out.add(s.call)
    return out


def test_first_read_that_passes_does_not_reread(sess):
    r = sess.say("岡田さん、15と16は法事で休み")
    assert r.kind != "error"
    assert sess.reader.reread_sent == []


def test_stuck_reread_asks_only_what_is_missing(sess):
    r = sess.say(IINKAI)
    assert len(sess.reader.reread_sent) == 1
    assert r.kind == "ask" and r.message == "委員の3人は誰で、委員会は何日ですか"


def test_mixed_reread_keeps_rules_and_points_to_dates(sess):
    r = sess.say(AOKI)
    assert r.kind == "ask"
    assert r.message.startswith("青木さんの有給は何日ですか")
    assert "続けて「青木さん、24日と25日は夜勤にしない」" in r.message
    r2 = sess.say("20日")   # 聞き返しへの答え。読み直した3件のルールにつながる（Claude へは送らない）
    assert r2.kind == "ask_period"
    assert "有給20日" in r2.summary and "夜勤" in r2.summary


def test_reread_sends_no_roster_names(sess):
    sess.say(IINKAI)
    sess.say(AOKI)
    text = _sent_text(sess.reader)
    assert sess.reader.reread_sent, "読み直しを送っていない"
    leaked = [w for w in _roster_words(sess) if w in text]
    assert leaked == []
    ctx = sess.reader.reread_sent[-1].messages[0]["content"]
    assert "職員16" in ctx and "ユニット2" in ctx and "r012" in ctx   # 名簿と今のルールは番号で渡している
    assert "職員12" in ctx   # 人の名前の入ったルール（斉藤さんの水曜）は番号に伏せてある


def test_name_left_in_context_is_not_sent(sess, monkeypatch):
    real = Session._reread_context

    def leaky(self, masker, tokens):
        ctx = real(self, masker, tokens)
        ctx["rules"].append({"id": "x", "name": "岡田さんの夜勤", "kind": "count"})
        return ctx
    monkeypatch.setattr(Session, "_reread_context", leaky)
    r = sess.say(IINKAI)
    assert sess.reader.reread_sent == []
    assert r.kind == "ask" and r.message == NOT_YET   # 最初の読みの聞き返しのまま


def test_without_recorded_reread_falls_back_to_first_answer(sess):
    sess.reader.rereads = {}
    r = sess.say(IINKAI)
    assert r.kind == "ask" and r.message == NOT_YET


def test_reader_without_reread_keeps_old_behavior(sess):
    class Plain:   # 公開版の包みなど、読み直しを持たない読み手
        def __init__(self, inner):
            self.inner, self.sent = inner, inner.sent

        def read(self, masked_text, history=None):
            return self.inner.read(masked_text, history)
    inner = sess.reader
    sess.reader = Plain(inner)
    r = sess.say(IINKAI)
    assert r.kind == "ask" and r.message == NOT_YET
    assert inner.reread_sent == []


def test_reread_replace_hint_asks_to_replace(sess):
    masked = "職員16さんの夜勤、もう少し減らして"
    sess.reader = RecordedReader(
        {masked: '{"items":[{"type":"unclear","question":"何回までですか"}]}'},
        {masked: json.dumps({"items": [{"type": "count_limit", "staff": ["職員16"], "shift": "夜", "min": None,
                                        "max": 4, "until_day": None, "strength": "operational", "reason": None}],
                             "replace": ["r003", "r999"]}, ensure_ascii=False)})
    r = sess.say("千葉さんの夜勤、もう少し減らして")
    assert r.kind == "ask_period"
    r2 = sess.answer(r.uid, "rule")
    assert r2.kind == "ask_replace" and "夜勤の人数" in r2.message and "r999" not in r2.message


def test_parse_reread_reads_fenced_json_and_drops_unusable_types():
    got = parse_reread('説明です。\n```json\n{"items":[{"type":"custom","summary":"x"},'
                       '{"type":"day_off","staff":"職員01","days":[3]}],"replace":"r1","next":" "}\n```')
    assert [it["type"] for it in got["items"]] == ["day_off"]
    assert got["replace"] == [] and got["next"] is None
