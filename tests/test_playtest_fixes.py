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


# 3 声の「日8」 -----------------------------------------------------------------------

def added_8(sess):
    assert sess.say("8時から17時の日8を足して").kind == "applied"


@pytest.mark.parametrize("said", ["10日の小島さん、日八に", "10日の小島さん非8に", "10日の小島さん、非八に", "10日の小島さん、日８に",
                                  "10日の小島さん、にちはちに", "10日の小島さん、ひはちに", "10日の小島さん、ニチハチに"])
def test_near_names_become_the_added_shift(tmp_path, sess, said):
    from shiftvoice.addshift import fix_spoken
    added_8(sess)
    assert fix_spoken(said, sess.store.facility()) == "10日の小島さん、日8に".replace("、", "、" if "、" in said else "")


def test_heard_name_reaches_the_table(sess):
    added_8(sess)
    sess.reader = Fake({"10日の職員11さん、勤務Aに": [{"type": "set_cell", "staff": "職員11", "days": [10], "shift": "勤務A",
                                                  "strength": "wish", "reason": None}]})
    r = sess.say("10日の小島さん、日八に")
    assert r.kind == "applied" and sess.store.month(2026, 11).table["小島"][9] == "日8"
    assert sess.reader.sent[0].messages[-1]["content"].count("勤務A") >= 1


@pytest.mark.parametrize("said", ["10日8時に集合", "十日八時から", "平日8人", "毎日八回", "10日の小島さん、日勤に"])
def test_numbers_are_not_the_added_shift(sess, said):
    from shiftvoice.addshift import fix_spoken
    added_8(sess)
    assert fix_spoken(said, sess.store.facility()) == said


def test_nothing_changes_without_an_added_shift(sess):
    from shiftvoice.addshift import fix_spoken
    assert fix_spoken("10日の小島さん、日八に", sess.store.facility()) == "10日の小島さん、日八に"


# 4 聞き返しに答えて話をつなぐ ---------------------------------------------------------

LEAVE = "篠原さん、今月有給ほしいそうです"
LEAVE_M = "職員13さん、今月有給ほしいそうです"
LEAVE_ITEM = [{"type": "leave_count", "staff": "職員13", "count": None, "reason": None}]


@pytest.mark.parametrize("answer,n", [("2日分です", 2), ("2日分です。日はいつでもいいです", 2), ("2日", 2), ("２日分", 2),
                                      ("二日分、日はいつでもいいそうです", 2), ("有給は1日だけ", 1),
                                      ("2日分って聞いてます。日はいつでもいいそうです。", 2)])
def test_leave_days_answer_joins_the_request(sess, answer, n):
    sess.reader = Fake({LEAVE_M: LEAVE_ITEM})
    r = sess.say(LEAVE)
    assert r.kind == "ask" and r.message == "篠原さんの有給は何日ですか"
    r = sess.say(answer)
    assert r.kind == "applied" and r.sent is None, r.message      # 「今月」は前の一言で言っているので聞かない
    assert r.summary.startswith(f"篠原さん 有給{n}日")
    assert sum(1 for x in sess.store.month(2026, 11).table["篠原"] if x == "有") >= n
    assert len(sess.reader.sent) == 1                             # 答えは Claude へ送らない
    assert answer in next(c for c in sess.state()["cards"] if c["id"] in r.rule_ids)["meta"]["said"]


def test_leave_dates_answer_sets_those_days(sess):
    sess.reader = Fake({LEAVE_M: LEAVE_ITEM})
    sess.say(LEAVE)
    r = sess.say("20日と21日")
    t = sess.store.month(2026, 11).table["篠原"]
    assert r.kind == "applied" and t[19] == "有" and t[20] == "有", r.message


def test_other_request_after_the_question_is_read_as_itself(sess):
    sess.reader = Fake({LEAVE_M: LEAVE_ITEM, "職員07さん、15日は休み": [{"type": "day_off", "staff": "職員07", "days": [15]}]})
    sess.say(LEAVE)
    r = sess.say("岡田さん、15日は休み")
    assert r.summary == "岡田さん 15日 休み"
    r = sess.say("2日分です")      # 聞き返しは1回の一言だけ。前の問いに戻らない
    assert r.kind != "applied"


def test_missing_day_answer_joins_the_request(sess):
    sess.reader = Fake({"職員07さんを休みにして": [{"type": "day_off", "staff": "職員07", "days": None}]})
    assert "何日か" in sess.say("岡田さんを休みにして").message
    r = sess.say("15日です")
    assert r.kind == "applied" and sess.store.month(2026, 11).table["岡田"][14] in ("公", "有")


def test_shift_name_answer_adds_the_shift(sess):
    sess.say("8時から17時の勤務を新しく作って")
    r = sess.say("名前は日8")
    assert r.kind == "applied" and sess.store.facility().shift("日8").hours == 9, r.message


# 5 言い直し ---------------------------------------------------------------------------

def tamura_work_days(sess):
    t = sess.store.month(2026, 11).table["田村"]
    d = next(d for d in range(5, 27) if t[d - 1] in "早日遅" and t[d] in "早日遅")
    return d, d + 1, t[d - 1]


def said_off(sess, d):
    sess.reader = Fake({f"職員15さん、{d}日を休みにして": [{"type": "day_off", "staff": "職員15", "days": [d], "reason": None}]})
    r = sess.say(f"田村さん、{d}日を休みにして")
    assert r.kind == "applied", r.message


def redo_shapes(a, b):
    """本物の Haiku が言い直しを読んだ形（2026-11-06）。"""
    t = "職員15"
    return {f"職員15さん、{a}日は休みじゃなくて元の勤務に戻して。{b}日を休みにして":
            [{"type": "cancel", "staff": t, "days": [a], "shift": "公", "reason": None},
             {"type": "day_off", "staff": t, "days": [b], "reason": None}],
            f"職員15さんの休み、{a}日じゃなくて{b}日にして": [{"type": "move", "staff": t, "shift": "公", "from_day": a, "to_day": b, "reason": None}],
            f"職員15さん、{a}日じゃなくて{b}日": [{"type": "move", "staff": t, "shift": None, "from_day": a, "to_day": b, "reason": None}],
            f"職員15さんの件、{a}日は元の勤務に戻して、{b}日を休みにして":
            [{"type": "set_cell", "staff": t, "days": [a], "shift": None, "reason": None},
             {"type": "day_off", "staff": t, "days": [b], "reason": None}]}


@pytest.mark.parametrize("n,said", list(enumerate(["田村さん、{a}日は休みじゃなくて元の勤務に戻して。{b}日を休みにして", "田村さんの休み、{a}日じゃなくて{b}日にして",
                                              "田村さん、{a}日じゃなくて{b}日", "田村さんの件、{a}日は元の勤務に戻して、{b}日を休みにして", "{a}日じゃなくて{b}日"])))
def test_redo_moves_the_day_and_restores_the_cell(sess, n, said):
    a, b, was = tamura_work_days(sess)
    said_off(sess, a)
    sess.reader = Fake(redo_shapes(a, b))
    r = sess.say(said.format(a=a, b=b))
    assert r.kind == "fix_offer", r.message
    assert f"{a}日の休みを外して、{b}日を休みにします" in r.message and "元の" in r.message
    r = sess.accept(r.uid)
    t = sess.store.month(2026, 11).table["田村"]
    assert t[b - 1] in ("公", "有") and t[a - 1] == was
    cells = [c for c in sess.state()["cards"] if c["kind"] == "cell" and c["who"] == ["田村"]]
    assert [c["when"] for c in cells] == [[b]]
    sess.undo()
    assert sess.store.month(2026, 11).table["田村"][a - 1] in ("公", "有")
    assert [c["when"] for c in sess.state()["cards"] if c["kind"] == "cell" and c["who"] == ["田村"]] == [[a]]


def test_moving_training_without_a_request_stays_a_move(sess):
    from shiftvoice.redo import as_redo
    it = [{"type": "move", "staff": "田村", "shift": "研", "from_day": 11, "to_day": 13, "reason": None}]
    assert as_redo(it, sess.store.ledger().rules_at()) == it


def test_no_machine_word_on_screen(sess):
    sess.reader = Fake({"職員07さんの15日を変えて": [{"type": "set_cell", "staff": "職員07", "days": [15], "shift": None, "reason": None}]})
    r = sess.say("岡田さんの15日を変えて")
    assert r.kind == "ask" and "None" not in r.message and "どの勤務" in r.message
    assert sess.say("早番です").kind == "applied"
    assert sess.store.month(2026, 11).table["岡田"][14] == "早"


def test_failed_table_does_not_ask_what_it_cannot_take():
    from shiftvoice.session import NO_TABLE
    assert "ますか" not in NO_TABLE and "ゆるめますか" not in NO_TABLE


# 6 同じマスの2度目の頼み --------------------------------------------------------------

def test_second_request_replaces_the_first(sess):
    sess.reader = Fake({"職員09さん、12日は休み": [{"type": "day_off", "staff": "職員09", "days": [12], "reason": None}],
                        "職員09さん、12日は有給": [{"type": "set_cell", "staff": "職員09", "days": [12], "shift": "有", "reason": None}],
                        "職員07さん、15日は休み": [{"type": "day_off", "staff": "職員07", "days": [15], "reason": None}]})
    assert sess.say("菊池さん、12日は休み").kind == "applied"
    r = sess.say("菊池さん、12日は有給")
    assert r.kind == "applied" and not r.warning and "置き換えました" in r.summary
    assert [c["name"] for c in sess.state()["cards"] if c["kind"] == "cell" and c["who"] == ["菊池"]] == ["菊池さん 12日 有給"]
    assert sess.store.month(2026, 11).table["菊池"][11] == "有"
    assert not sess.say("岡田さん、15日は休み").warning           # 前は以後の直しのたびに赤字が出た
    sess.undo(); sess.undo()
    assert [c["name"] for c in sess.state()["cards"] if c["kind"] == "cell" and c["who"] == ["菊池"]] == ["菊池さん 12日 休み"]


def test_leave_can_be_replaced_by_a_day_off(sess):
    sess.reader = Fake({"職員09さん、12日は有給": [{"type": "set_cell", "staff": "職員09", "days": [12], "shift": "有", "reason": None}],
                        "職員09さん、12日は休み": [{"type": "day_off", "staff": "職員09", "days": [12], "reason": None}]})
    sess.say("菊池さん、12日は有給")
    r = sess.say("菊池さん、12日は休み")
    assert r.kind == "applied" and not r.warning and sess.store.month(2026, 11).table["菊池"][11] == "公"


def test_spoken_shift_then_another_replaces(sess):
    added_8(sess)
    sess.reader = Fake({"10日の職員11さん、勤務Aに": [{"type": "set_cell", "staff": "職員11", "days": [10], "shift": "勤務A", "reason": None}],
                        "10日の職員11さん、勤務Aじゃなくて早番に": [{"type": "set_cell", "staff": "職員11", "days": [10], "shift": "早", "reason": None}]})
    sess.say("10日の小島さん、日8に")
    r = sess.say("10日の小島さん、日8じゃなくて早番に")
    assert r.kind == "applied" and not r.warning, r.message
    assert sess.store.month(2026, 11).table["小島"][9] == "早"
    assert [c["name"] for c in sess.state()["cards"] if c["kind"] == "cell" and c["who"] == ["小島"]] == ["小島さん 10日 早番"]


def test_other_days_of_the_first_request_stay(sess):
    sess.reader = Fake({"職員07さん、15日と16日は休み": [{"type": "day_off", "staff": "職員07", "days": [15, 16], "reason": None}],
                        "職員07さん、16日は有給": [{"type": "set_cell", "staff": "職員07", "days": [16], "shift": "有", "reason": None}]})
    sess.say("岡田さん、15日と16日は休み")
    sess.say("岡田さん、16日は有給")
    assert sorted(c["name"] for c in sess.state()["cards"] if c["kind"] == "cell" and c["who"] == ["岡田"]) == ["岡田さん 15日 休み", "岡田さん 16日 有給"]


def test_avoid_keeps_a_request_it_does_not_touch(sess):
    from shiftvoice.redo import same_cell_drops
    sess.reader = Fake({"職員09さん、12日は休み": [{"type": "day_off", "staff": "職員09", "days": [12], "reason": None}]})
    sess.say("菊池さん、12日は休み")
    rules = sess.store.ledger().rules_at()
    new = {"kind": "cell", "who": ["菊池"], "when": [12], "params": {"not_in": ["早"]}}
    assert same_cell_drops([new], rules) == []


# 7・8 回数の言い直しと「毎月のルールとして残しました」 ----------------------------------

CAP3 = "職員11さん、29日までは夜勤を3回まで。今月だけ"
NIGHT = {"type": "count_limit", "staff": "職員11", "shift": "夜", "reason": None}


def counts(sess):
    return [c for c in sess.state()["cards"] if c["kind"] == "count" and c["who"] == ["小島"] and c["params"].get("shifts") == ["夜"]]


def test_count_replaced_by_the_new_number(sess):
    sess.reader = Fake({CAP3: [{**NIGHT, "max": 3, "until_day": 29}],
                        "職員11さんの夜勤、29日までは2回まで": [{**NIGHT, "max": 2, "until_day": 29}]})
    sess.say("小島さん、29日までは夜勤を3回まで。今月だけ")
    r = sess.say("小島さんの夜勤、29日までは2回まで")
    assert r.kind == "applied", r.message
    assert [c["params"]["max"] for c in counts(sess)] == [2]
    sess.undo()
    assert [c["params"]["max"] for c in counts(sess)] == [3]


def test_count_for_the_month_replaces_the_dated_one(sess):
    sess.reader = Fake({CAP3: [{**NIGHT, "max": 3, "until_day": 29}],
                        "職員11さんの夜勤、今月は2回まで": [{**NIGHT, "max": 2}]})
    sess.say("小島さん、29日までは夜勤を3回まで。今月だけ")
    assert sess.say("小島さんの夜勤、今月は2回まで").kind == "applied"
    assert [c["params"]["max"] for c in counts(sess)] == [2]


def test_count_can_be_loosened(sess):
    sess.reader = Fake({"職員11さんの夜勤、今月は2回まで": [{**NIGHT, "max": 2}], "職員11さんの夜勤、今月は3回まで": [{**NIGHT, "max": 3}]})
    sess.say("小島さんの夜勤、今月は2回まで")
    r = sess.say("小島さんの夜勤、今月は3回まで")
    assert r.kind == "applied" and "もう守られて" not in r.message, r.message
    assert [c["params"]["max"] for c in counts(sess)] == [3]


def test_monthly_count_is_not_removed_by_this_month_only(sess):
    sess.reader = Fake({"職員11さんの夜勤、毎月4回まで": [{**NIGHT, "max": 4}], "職員11さんの夜勤、今月は2回まで": [{**NIGHT, "max": 2}]})
    sess.say("小島さんの夜勤、毎月4回まで")
    sess.say("小島さんの夜勤、今月は2回まで")
    assert sorted(c["params"]["max"] for c in counts(sess)) == [2, 4]


@pytest.mark.parametrize("n,left", [(4, [3, 4]), (2, [2])])
def test_this_month_count_stays_under_a_looser_monthly_one(sess, n, left):
    sess.reader = Fake({CAP3: [{**NIGHT, "max": 3, "until_day": 29}],
                        f"職員11さんの夜勤、毎月{n}回まで": [{**NIGHT, "max": n}]})
    sess.say("小島さん、29日までは夜勤を3回まで。今月だけ")
    sess.say(f"小島さんの夜勤、毎月{n}回まで")
    assert sorted(c["params"]["max"] for c in counts(sess)) == left


def test_dated_kept_is_not_called_every_month(sess):
    sess.reader = Fake({"職員11さんの夜勤、毎月2回まで": [{**NIGHT, "max": 2}],
                        "職員11さんの夜勤、29日までは2回まで": [{**NIGHT, "max": 2, "until_day": 29}]})
    sess.say("小島さんの夜勤、毎月2回まで")
    rev = sess.store.ledger().rev
    r = sess.say("小島さんの夜勤、29日までは2回まで")
    assert "毎月のルールとして残しました" not in r.message and "もう守られています" in r.message
    assert sess.store.ledger().rev == rev


# 9 元の勤務に戻す ---------------------------------------------------------------------

def test_back_restores_the_cell(sess):
    a, _, was = tamura_work_days(sess)
    said_off(sess, a)
    sess.reader = Fake({f"職員15さんの{a}日を元の勤務に戻して": [{"type": "cancel", "staff": "職員15", "days": [a], "shift": None, "reason": None}]})
    r = sess.say(f"田村さんの{a}日を元の勤務に戻して")
    assert r.kind == "applied" and "元の" in r.message and "戻しました" in r.message, r.message
    assert sess.store.month(2026, 11).table["田村"][a - 1] == was
    assert not [c for c in sess.state()["cards"] if c["kind"] == "cell" and c["who"] == ["田村"]]
    sess.undo()
    assert sess.store.month(2026, 11).table["田村"][a - 1] in ("公", "有")
    assert [c for c in sess.state()["cards"] if c["kind"] == "cell" and c["who"] == ["田村"]]


def test_back_after_two_requests_goes_to_before_the_first(sess):
    a, _, was = tamura_work_days(sess)
    said_off(sess, a)
    sess.reader = Fake({f"職員15さん、{a}日は有給": [{"type": "set_cell", "staff": "職員15", "days": [a], "shift": "有", "reason": None}],
                        f"職員15さんの{a}日を元の勤務に戻して": [{"type": "cancel", "staff": "職員15", "days": [a], "shift": None, "reason": None}]})
    sess.say(f"田村さん、{a}日は有給")
    sess.say(f"田村さんの{a}日を元の勤務に戻して")
    assert sess.store.month(2026, 11).table["田村"][a - 1] == was


def test_back_asks_when_it_does_not_know(sess):
    a, _, _ = tamura_work_days(sess)
    table = sess.store.month(2026, 11).table
    sess.reader = Fake({f"職員15さんの{a}日を元の勤務に戻して": [{"type": "cancel", "staff": "職員15", "days": [a], "shift": None, "reason": None}]})
    r = sess.say(f"田村さんの{a}日を元の勤務に戻して")
    assert r.kind == "ask" and "頼む前は何の勤務でしたか" in r.message
    assert sess.store.month(2026, 11).table == table
    r = sess.say("早番です")
    assert r.kind == "applied" and sess.store.month(2026, 11).table["田村"][a - 1] == "早", r.message


def test_plain_cancel_still_leaves_the_cell(sess):
    a, _, _ = tamura_work_days(sess)
    said_off(sess, a)
    sess.reader = Fake({f"職員15さんの{a}日の休みはなしで": [{"type": "cancel", "staff": "職員15", "days": [a], "shift": "公", "reason": None}]})
    r = sess.say(f"田村さんの{a}日の休みはなしで")
    assert r.kind == "applied" and "表のマスはそのまま" in r.message


def test_back_after_distribution_waits_for_the_button(sess):
    a, _, was = tamura_work_days(sess)
    said_off(sess, a)
    sess.distribute()
    table = sess.store.month(2026, 11).table
    sess.reader = Fake({f"職員15さんの{a}日を元の勤務に戻して": [{"type": "cancel", "staff": "職員15", "days": [a], "shift": None, "reason": None}]})
    r = sess.say(f"田村さんの{a}日を元の勤務に戻して")
    assert r.kind == "fix_offer" and sess.store.month(2026, 11).table == table, r.message
    sess.accept(r.uid)
    assert sess.store.month(2026, 11).table["田村"][a - 1] == was
    assert not [x for x in sess.store.acceptances() if x.staff == "田村" and x.note == "配った後の直しで勤務が入った"]


def test_back_without_a_day_confirms_then_restores(sess):
    a, _, was = tamura_work_days(sess)
    said_off(sess, a)
    sess.reader = Fake({"職員15さんの休み、元に戻して": [{"type": "cancel", "staff": "職員15", "days": None, "shift": "公", "reason": None}]})
    r = sess.say("田村さんの休み、元に戻して")
    assert r.kind == "fix_offer" and "頼む前の勤務に戻します" in r.message, r.message
    sess.accept(r.uid)
    assert sess.store.month(2026, 11).table["田村"][a - 1] == was


# 10 今月だけのルールを右の欄のいちばん上に --------------------------------------------

@pytest.mark.parametrize("card,want", [({"kind": "count", "scope": "once", "meta": {}}, True),
                                       ({"kind": "count", "scope": "rule", "meta": {"until": "2026-11-29"}}, True),
                                       ({"kind": "count", "scope": "rule", "meta": {}}, False),
                                       ({"kind": "count", "scope": "rule", "meta": {"until": "2026-12-31"}}, False),
                                       ({"kind": "cell", "scope": "once", "meta": {"until": "2026-11-30"}}, False)])
def test_month_only_cards(card, want):
    import re
    import shutil
    import subprocess
    from pathlib import Path
    if not shutil.which("node"):
        pytest.skip("node が無い")
    html = (Path(__file__).parents[1] / "src/shiftvoice/static/index.html").read_text(encoding="utf-8")
    fn = re.search(r"function monthOnly\(c\) \{.*?\n\}", html, re.S)[0]
    js = f"var S={{year:2026,month:11}};{fn};console.log(JSON.stringify(monthOnly({json.dumps(card)})))"
    assert json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout) is want
    top = html[html.index("function renderCards()"):]
    assert top.index("only.forEach") < top.index("cleanEntry(box)")     # 毎月のルールの上に出す
    assert '"今月だけ"' in html[html.index("function makeCard"):html.index("function renderSaid")]


def test_half_month_count_keeps_the_whole_month_count(sess):
    sess.reader = Fake({"職員11さん、今月は夜勤3回まで": [{**NIGHT, "max": 3}],
                        "職員11さん、15日までは夜勤2回まで。今月だけ": [{**NIGHT, "max": 2, "until_day": 15}]})
    sess.say("小島さん、今月は夜勤3回まで")
    assert sess.say("小島さん、15日までは夜勤2回まで。今月だけ").kind == "applied"
    assert sorted(c["params"]["max"] for c in counts(sess)) == [2, 3]
