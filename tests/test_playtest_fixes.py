"""AI のテストプレイ（2026-11-06）で見つけた不具合の試験。失敗した一言をそのまま書く。"""
import json

import pytest

from conftest import REPLIES
from shiftvoice.period import read_answer, read_period
from shiftvoice.reader import RecordedReader
from shiftvoice.session import period_answer


class Fake(RecordedReader):
    """録った返事に、形だけ確かめる返事を足した読み手。"""

    def __init__(self, more):
        super().__init__({**json.loads(REPLIES.read_text(encoding="utf-8")),
                          **{k: json.dumps({"items": v}, ensure_ascii=False) for k, v in more.items()}})


# 1 今月だけじゃなくて毎月 ------------------------------------------------------------

@pytest.mark.parametrize("said", ["遅番の次の日は早番にしない。今月だけじゃなくて毎月",
                                  "今月だけじゃなく、毎月ずっと効くようにお願いします",
                                  "平日は日8を1人。今月だけでなく、これからも",
                                  "今月だけではなくて、これからずっと",
                                  "今回だけじゃなくて今後も",
                                  "今月に限らず、ずっとそうして"])
def test_not_only_this_month_is_every_month(said):
    assert read_period(said) == "rule"


def test_not_only_without_every_month_asks():
    assert read_period("今月だけじゃなくて来月も") is None


@pytest.mark.parametrize("said,want", [("毎月じゃなくて今回だけ", "once"), ("今月だけ", "once"), ("今月は夜勤0で", "once"),
                                       ("毎月、夜勤は2回まで", "rule")])
def test_period_words_stay(said, want):
    assert read_period(said) == want


def test_answer_not_only_this_month():
    assert read_answer("今月だけじゃなくて毎月") == "rule"
    assert period_answer("今月だけじゃなくて、毎月で") == "rule"
    assert period_answer("今月だけ") == "once"


# 2 日8の足し方 -----------------------------------------------------------------------

@pytest.mark.parametrize("said", [
    "8時から17時の勤務を新しく作って。名前は日8",
    "施設長から言われたんですが、8時から17時の勤務を新しく作ってください。名前は日8でお願いします。",
    "施設長から言われたんですが、8時から17時までの勤務を新しく作りたいです。名前は「日8」でお願いします。",
    "8時から17時の勤務を足して。名前は日8",
    "8時から17時の勤務を追加して。名前は日8です",
    "名前は日8で、8時から17時の勤務を足して",
    "日8という名前で8時から17時の勤務を作って",
    "8時から17時の勤務、日8を作って",
    "新しいシフトを足したい。名前は日8、時間は8時から17時",
    "日8を新しく作って。8時から17時",
    "日8の勤務を8時から17時で作って",
    "新しい勤務は名前が日8で、8時から17時",
    "足して。名前は日八。8時から17時"])
def test_add_said_in_more_ways(sess, said):
    r = sess.say(said)
    assert r.kind == "applied" and sess.reader.sent == [], r.message
    st = sess.store.facility().shift("日8")
    assert (st.start, st.hours, st.spoken) == ("08:00", 9, True)


def test_add_name_only_asks_times(sess):
    r = sess.say("足して。名前は日8")
    assert r.kind == "ask" and "何時から何時まで" in r.message and sess.reader.sent == []
    assert sess.say("8時から17時です").kind == "applied"
    assert sess.store.facility().shift("日8").hours == 9


def test_add_without_name_asks_name(sess):
    r = sess.say("8時から17時の勤務を新しく作って")
    assert r.kind == "ask" and "名前は何にしますか" in r.message and sess.reader.sent == []
    assert "日8" not in sess.store.facility().symbols


def test_new_kind_without_name_or_times_says_how(sess):
    r = sess.say("新しい勤務を作りたい")
    assert r.kind == "ask" and "8時から17時の日8を足して" in r.message and sess.reader.sent == []


@pytest.mark.parametrize("said", ["早番を足して", "10日に夜勤を足して", "日勤を1人足して", "夜の待機当番を作って。名前は夜待ち",
                                  "勤務表を作って"])
def test_add_keeps_other_requests_for_the_reader(sess, said):
    from shiftvoice.addshift import add_shift_items
    assert add_shift_items(said, sess.store.facility()) is None
