"""声で届いた一言の書き方（勤務の名前のひらがな・取り違えた字・ひらがなの回数）と、言い回しの測り（2026-11-08）で
止まった・違う読みになった一言の試験。失敗した一言をそのまま書く。"""
import json
import os
from types import SimpleNamespace

import pytest

from conftest import FIXED_NOW
from shiftvoice.heard import fix_heard
from shiftvoice.names import Masker
from shiftvoice.reader import RecordedReader
from shiftvoice.redo import bare_redo
from shiftvoice.session import Session


def _fac(*names):
    return SimpleNamespace(shift_types=[SimpleNamespace(name=n) for n in names])


HIDAMARI = _fac("早番", "日勤", "遅番", "夜勤", "明け", "公休", "有給", "研修")


@pytest.mark.parametrize("said,want", [
    ("おそばんのつぎのひにはやばんいれないで", "遅番のつぎのひに早番いれないで"),
    ("石田さん12日高級で", "石田さん12日公休で"),
    ("にっきんとやきんをあわせて", "日勤と夜勤をあわせて"),
    ("ゆうきゅうとけんしゅう", "有給と研修"),
    ("連金は4日まで", "連勤は4日まで"),
    ("れんきんは4日まで", "連勤は4日まで"),
    ("はやばんはつきはっかいまで", "早番はつき8回まで"),
    ("よるはにじゅっかいまで", "よるは20回まで"),
    ("さんじゅうにんいる", "30人いる"),
    ("じゅうごにちからにじゅうにちまで", "15日から20日まで"),
])
def test_heard_names_and_numbers_are_fixed(said, want):
    assert fix_heard(said, HIDAMARI) == want


@pytest.mark.parametrize("said", ["ひとりで夜勤", "ふたりずつ", "いちにちだけ", "にんじんを買う", "かいぎの日",
                                  "10日の日勤", "8回まで", "夜勤は月8回まで", "ここのかかん"])
def test_ordinary_words_are_left_alone(said):
    assert fix_heard(said, HIDAMARI) == said


def test_name_is_fixed_only_when_the_facility_has_that_shift():
    fac = _fac("早出", "日勤", "遅出")   # 「早番」の無い施設で「はやばん」を早番にしない
    assert fix_heard("はやばんは月4回まで", fac) == "はやばんは月4回まで"
    assert fix_heard("にっきんは月4回まで", fac) == "日勤は月4回まで"
    assert fix_heard("高級は4日まで", fac) == "高級は4日まで"   # 公休の無い施設では高級のまま
    assert fix_heard("連金は4日まで", fac) == "連勤は4日まで"     # 連勤は勤務の名前ではないので施設に関わらず


def test_numbers_that_are_not_a_count_are_left_alone():
    assert fix_heard("ごかいまで", HIDAMARI) == "5回まで"


# 一言を言わせる ---------------------------------------------------------------

def _session(sess, said: dict[str, list[dict]]) -> Session:
    """言う一言ごとに用意した返事で読む（返事の名前は伏せた番号で書く）。録っていない一言は読み取りが失敗する。"""
    store = sess.store
    masker = Masker(store.staff(), store.facility())
    replies = {}
    for text, items in said.items():
        items = [{k: (masker.to_code.get(v, v) if k == "staff" and isinstance(v, str) else v) for k, v in it.items()}
                 for it in items]
        replies[masker.mask(text)[0]] = json.dumps({"items": items}, ensure_ascii=False)
    s = Session(store, 2026, 11, RecordedReader(replies), now=lambda: FIXED_NOW)
    s.open()
    return s


def _sent(s) -> str:
    return "\n".join(m["content"] for x in s.reader.sent for m in x.messages[-1:])


def test_hiragana_shift_names_reach_the_reader_as_shift_names(sess):
    s = _session(sess, {"遅番のつぎのひに早番いれないで": [
        {"type": "forbid_sequence", "staff": None, "first": "遅", "next": "早", "strength": "negotiable", "reason": None}]})
    r = s.say("おそばんのつぎのひにはやばんいれないで")
    assert r.kind == "ask_period", r
    assert "遅番の次の日に早番を置かない" in r.summary
    assert "おそばん" not in _sent(s)


def test_heard_wrong_char_for_day_off_is_read_as_day_off(sess):
    s = _session(sess, {"石田さん12日公休で": [{"type": "day_off", "staff": "石田", "days": [12], "reason": None}]})
    r = s.say("石田さん12日高級で")
    assert r.kind != "error", r
    assert "12日" in r.summary and "休み" in r.summary
    assert "高級" not in _sent(s)


def test_hiragana_count_reaches_the_reader_as_a_number(sess):
    s = _session(sess, {"小川さんははやばんはつき8回まで".replace("はやばん", "早番"): [
        {"type": "count_limit", "staff": ["小川"], "shift": "早", "min": None, "max": 8, "until_day": None,
         "strength": "operational", "reason": None}]})
    r = s.say("小川さんははやばんはつきはっかいまで")
    assert r.kind == "ask_period", r
    assert "早番は月8回まで" in r.summary


# 日勤と遅番を合わせた回数 -------------------------------------------------------------

@pytest.mark.parametrize("shift", [None, "日・遅", "日と遅"])
def test_together_count_survives_how_the_reader_writes_the_shift(sess, shift):
    said = "小川さんは日勤と遅番合わせて月12回まで"
    s = _session(sess, {said: [{"type": "count_limit", "staff": ["小川"], "shift": shift, "min": None, "max": 12,
                               "until_day": None, "strength": "operational", "reason": None}]})
    r = s.say(said)
    assert r.kind == "ask_period", r
    assert "日勤と遅番は合わせて月12回まで" in r.summary


def test_together_without_the_word_together_still_asks_which_shift(sess):
    said = "小川さんは日勤と遅番を月12回まで"   # 「合わせて」が無い時は、勤務を空で返されても勝手に2つの合計にしない
    s = _session(sess, {said: [{"type": "count_limit", "staff": ["小川"], "shift": None, "min": None, "max": 12,
                               "until_day": None, "strength": "operational", "reason": None}]})
    r = s.say(said)
    assert r.kind == "ask" and "勤務が読めませんでした" in r.message


# 勤務の記号でない字を書いて止まった時は、読み直す --------------------------------------------

def test_shift_symbol_the_reader_made_up_is_reread_not_asked(sess):
    said = "全員、連勤は4日まで"
    first = [{"type": "consecutive_limit", "staff": None, "shift": "連", "max": 4, "except_staff": None,
              "strength": "operational", "reason": None}]
    reread = {"items": [{"type": "consecutive_limit", "staff": None, "shift": None, "max": 4, "except_staff": None,
                         "strength": "operational", "reason": None, "everyone": True}], "replace": [], "next": None,
              "how": None}
    s = _session(sess, {said: first})
    s.reader.rereads[said] = json.dumps(reread, ensure_ascii=False)
    r = s.say(said)
    assert s.reader.reread_sent, "読み直しに回っていない"
    assert r.kind == "ask_period", r


def test_blank_shift_question_is_still_asked_not_reread(sess):
    said = "職員なしの夜勤の話ではなく、小川さんの回数"
    s = _session(sess, {said: [{"type": "count_limit", "staff": ["小川"], "shift": None, "min": None, "max": 3,
                               "until_day": None, "strength": "operational", "reason": None}]})
    r = s.say(said)
    assert r.kind == "ask" and "勤務が読めませんでした" in r.message
    assert s.reader.reread_sent == []


def test_cancel_with_nothing_to_cancel_is_reread_with_the_rules(sess):
    said = "早番は4回もいらない"   # 取り消しと読まれた。取り消せる話した頼みが無いので、聞き返す前に読み直す
    reread = {"items": [{"type": "count_limit", "staff": None, "shift": "早", "min": None, "max": 7, "until_day": None,
                         "strength": "operational", "reason": None, "everyone": True}], "replace": [], "next": None,
              "how": "全員の早番を月7回までと読みました"}
    s = _session(sess, {said: [{"type": "cancel", "staff": None, "days": None, "shift": "早"}]})
    s.reader.rereads[said] = json.dumps(reread, ensure_ascii=False)
    r = s.say(said)
    assert s.reader.reread_sent, "読み直しに回っていない"
    assert r.kind == "ask_period" and "早番は月7回まで" in r.summary, r


def test_cancel_of_a_spoken_day_is_not_reread(sess):
    first = "石田さん、12日は休みで"
    s = _session(sess, {first: [{"type": "day_off", "staff": "石田", "days": [12], "reason": None}],
                        "石田さんの休み、やっぱりなし": [{"type": "cancel", "staff": "石田", "days": None, "shift": "公"}]})
    r = s.say(first)
    if r.kind == "ask_period":
        s.answer(r.uid, "once")
    s.say("石田さんの休み、やっぱりなし")
    assert s.reader.reread_sent == []


# 人を言った言い直し --------------------------------------------------------------------

def _spoken_rules(s):
    return s.store.ledger().rules_at(between=s._span())


def test_named_correction_replaces_the_day_that_person_spoke(sess):
    first = "石田さん、12日は休みで"
    s = _session(sess, {first: [{"type": "day_off", "staff": "石田", "days": [12], "reason": None}]})
    r = s.say(first)
    assert r.kind in ("applied", "ask_period"), r
    if r.kind == "ask_period":
        r = s.answer(r.uid, "once")
    assert r.kind == "applied", r
    sent_before = len(s.reader.sent)
    r = s.say("石田さん12日じゃなくて14日だった")   # 読み取りには送らない（録った返事も無い）
    assert len(s.reader.sent) == sent_before
    assert r.kind != "error", r
    assert r.kind == "fix_offer" and "12日の休みを外して、14日を休みにします" in r.message, r
    r = s.accept(r.uid)
    assert r.kind == "applied", r
    cells = [x for x in _spoken_rules(s).values() if x.kind == "cell" and x.who == ["石田"]]
    days = sorted(d for x in cells for d in x.when)
    assert 14 in days and 12 not in days, days


def test_named_correction_without_that_day_spoken_goes_to_the_reader(sess):
    masker = Masker(sess.store.staff(), sess.store.facility())
    code = masker.to_code["石田"]
    rules = _spoken_rules(sess)
    assert bare_redo(f"{code}さん12日じゃなくて14日だった", rules, masker.to_name) is None
    assert bare_redo(f"{code}さん12日じゃなくて14日だった", rules) is None   # 番号と名前の対応が無い時も読まない


def test_named_correction_to_the_same_day_is_not_read_locally(sess):
    masker = Masker(sess.store.staff(), sess.store.facility())
    code = masker.to_code["石田"]
    assert bare_redo(f"{code}さん12日じゃなくて12日だった", _spoken_rules(sess), masker.to_name) is None


# 「来月から」 -------------------------------------------------------------------------

def _from_of(s, who="杉山"):
    return [c["from"] for st in s.store.staff() if st.name == who for c in st.changes][-1]


@pytest.mark.parametrize("said,want", [("杉山さんは来月から日勤だけ", "2026-12"), ("杉山さんは再来月から日勤だけ", "2027-01")])
def test_next_month_in_words_when_the_reader_leaves_the_month_empty(sess, said, want):
    s = _session(sess, {said: [{"type": "staff_info", "staff": "杉山", "from_month": None, "can_add": ["日"],
                               "can_remove": ["早", "遅", "夜"]}]})
    r = s.say(said)
    assert r.kind == "applied", r
    assert _from_of(s) == want


def test_no_month_word_still_means_this_month(sess):
    said = "杉山さんは日勤だけ"
    s = _session(sess, {said: [{"type": "staff_info", "staff": "杉山", "from_month": None, "can_add": ["日"],
                               "can_remove": ["早", "遅", "夜"]}]})
    r = s.say(said)
    assert r.kind == "applied", r
    assert _from_of(s) == "2026-11"


def test_month_the_reader_wrote_is_kept(sess):
    said = "杉山さんは来月から日勤だけ"
    s = _session(sess, {said: [{"type": "staff_info", "staff": "杉山", "from_month": 1, "can_add": ["日"],
                               "can_remove": ["早", "遅", "夜"]}]})
    assert s.say(said).kind == "applied"
    assert _from_of(s) == "2027-01"


# 日にちを言っていない「〇〇さんは早番には入れないで」 ----------------------------------------

def _avoid(day=None):
    return [{"type": "avoid_cell", "staff": "千葉", "days": day, "shift": "早", "reason": None}]


def test_avoid_a_shift_without_a_day_is_zero_times_a_month(sess):
    said = "千葉さんは早番には入れないで"
    s = _session(sess, {said: _avoid()})
    r = s.say(said)
    assert r.kind == "ask_period", r
    assert "千葉さん 早番は月0回まで" in r.summary


@pytest.mark.parametrize("first", ["早", None])
def test_the_same_phrase_read_as_a_sequence_ban_is_still_zero_times(sess, first):
    said = "千葉さんは早番には入れないで"
    s = _session(sess, {said: [{"type": "forbid_sequence", "staff": ["千葉"], "first": first, "next": "早",
                               "strength": "wish", "reason": None}]})
    r = s.say(said)
    assert r.kind == "ask_period", r
    assert "千葉さん 早番は月0回まで" in r.summary


def test_a_real_sequence_ban_with_a_named_person_is_kept(sess):
    said = "千葉さんは早番の次の日は早番に入れないで"
    s = _session(sess, {said: [{"type": "forbid_sequence", "staff": ["千葉"], "first": "早", "next": "早",
                               "strength": "wish", "reason": None}]})
    r = s.say(said)
    assert "月0回" not in (r.summary or "") + (r.message or "")


@pytest.mark.parametrize("said", ["千葉さんは15日は早番には入れないで", "千葉さんは土日は早番には入れないで",
                                  "千葉さんは明日は早番には入れないで", "千葉さんは今度の水曜は早番には入れないで",
                                  "千葉さんは月末は早番には入れないで"])
def test_avoid_with_a_day_word_is_not_turned_into_a_count(sess, said):
    s = _session(sess, {said: _avoid()})
    r = s.say(said)
    assert "月0回" not in (r.summary or "") + (r.message or "")


# 言い回しの測りで直した読み取りの指示（本物の Claude を呼ぶ。SHIFTVOICE_LIVE=1 の時だけ） -----------------

LIVE = pytest.mark.skipif(not os.environ.get("SHIFTVOICE_LIVE"), reason="本物の Claude を呼ぶ（SHIFTVOICE_LIVE=1）")


@LIVE
@pytest.mark.parametrize("masked,types", [
    ("職員02さん12日休ましたって", {"day_off", "set_cell"}),
    ("やっぱり連勤は5日に戻して", {"consecutive_limit"}),
])
def test_live_reader_does_not_read_these_as_report_or_cancel(masked, types):
    from shiftvoice.reader import HaikuReader
    items = HaikuReader().read(masked)
    assert {it["type"] for it in items} <= types, items


@pytest.mark.parametrize("heard, want", [
    ("連勤は錬金4日まで", "連勤は連勤4日まで"),
    ("年金は4日までにして", "連勤は4日までにして"),
    ("日輪は月10回まで", "日勤は月10回まで"),
    ("小川さん12日悠久", "小川さん12日有給"),
    ("絶大に遅番の次の日に早番はダメ", "絶対に遅番の次の日に早番はダメ"),
    ("石田さん9日希望給", "石田さん9日希望休"),
    ("石田さん9日希望求", "石田さん9日希望休"),
    ("はや版は2日まで", "早番は2日まで"),
    ("遅版の次の日", "遅番の次の日"),
    ("菊池さん9日休み？", "菊池さん9日休み"),
    ("遅番が3日も続くの早めて", "遅番が3日も続くのやめて"),
])
def test_声の測りで集まった聞き違いをそろえる(heard, want):
    """AI の声（作者の声の複製・Mac の読み上げ）を音声認識に通した時の聞き違い（2026-11-08 の声の測り）"""
    assert fix_heard(heard, HIDAMARI) == want


@pytest.mark.parametrize("text", ["年金の話は来月", "小川さんの年金の手続きで12日休み", "日勤は月10回まで", "本当に続くの？の件は別"])
def test_聞き違いでない言葉は変えない(text):
    want = text[:-1] if text.endswith("？") else text
    assert fix_heard(text, HIDAMARI) == want


def test_施設に無い勤務の聞き違いは直さない():
    assert fix_heard("日輪は月10回まで", _fac("早番", "遅番", "夜勤")) == "日輪は月10回まで"
