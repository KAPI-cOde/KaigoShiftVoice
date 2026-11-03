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


@pytest.mark.parametrize("text", ["あおき、15と16は休み", "岡田さん、15と16は休み。みんなに伝えておいて", "皆勤の岡田、15と16は休み"])
def test_nobody_cells_never_go_to_everyone(store, text):
    """休みのようなマスの頼みは、人が無いまま全員に効かせない（さんの無いかなの名前・「皆」を含む一言。再レビュー2回目 b）。"""
    from shiftvoice.intake import Clarify, to_proposal
    with pytest.raises(Clarify):
        to_proposal([{"type": "day_off", "days": [15, 16]}], text, store.facility(), store.staff(), store.month(2026, 11))


@pytest.mark.parametrize("text", ["利用者さんが落ち着かないから、夜勤の次の日に早番は置かないで",
                                  "みなさん、夜勤の次の日に早番はやめてあげて"])
def test_everyone_rule_with_san_word_is_not_blocked(store, text):
    """名簿の人が出ない全員向けのルールは、「利用者さん」「みなさん」があっても聞き返さない（再レビュー2回目 a）。"""
    from shiftvoice.intake import to_proposal
    p = to_proposal([{"type": "forbid_sequence", "first": "夜", "next": "早"}], text, store.facility(),
                    store.staff(), store.month(2026, 11))
    assert p.rules and not p.rules[0]["who"]


@pytest.mark.parametrize("text,item", [
    ("おかださん、夜勤は月4回まで", {"type": "count_limit", "shift": "夜", "max": 4}),
    ("おかださん、連勤は4日まで", {"type": "consecutive_limit", "max": 4}),
    ("皆勤の岡田さん、夜勤は月4回まで", {"type": "count_limit", "shift": "夜", "max": 4}),
])
def test_person_rule_by_kana_or_kaikin_never_goes_to_everyone(store, text, item):
    """かなの名前にさんを付けた人ごとのルールや「皆勤」を含む一言は、返事が人を省いても全員のルールにしない（周8 レビュー b）。"""
    from shiftvoice.intake import Clarify, to_proposal
    with pytest.raises(Clarify):
        to_proposal([item], text, store.facility(), store.staff(), store.month(2026, 11))


@pytest.mark.parametrize("text", ["看護さんが少ない日だから、夜勤の次の日に早番は置かないで",
                                  "職員さんの負担が大きいから、夜勤の次の日に早番は置かないで"])
def test_everyone_rule_with_job_word_is_not_blocked(store, text):
    """「看護さん」「職員さん」のような名前でない語を含む全員向けのルールは止めない（周8 再レビュー a・b）。"""
    from shiftvoice.intake import to_proposal
    p = to_proposal([{"type": "forbid_sequence", "first": "夜", "next": "早"}], text, store.facility(),
                    store.staff(), store.month(2026, 11))
    assert p.rules and not p.rules[0]["who"]


def test_nobody_clarify_tells_how_to_say_everyone(store):
    """人が読み取れない時の聞き返しは、全員のルールなら「全員」と付ければよいことを伝える（周8 再レビュー a）。"""
    from shiftvoice.intake import Clarify, to_proposal
    with pytest.raises(Clarify, match="全員"):
        to_proposal([{"type": "count_limit", "shift": "夜", "max": 4}], "おかださん、夜勤は月4回まで",
                    store.facility(), store.staff(), store.month(2026, 11))


@pytest.mark.parametrize("text", ["たくくん、夜勤は月4回まで", "おかだリーダーさん、連勤は4日まで"])
def test_nickname_or_title_rule_never_goes_to_everyone(store, text):
    """名簿に無い呼び名（たくくん）や、かなの名前に役職を付けた呼び方は、返事が人を省いても全員にしない（周10 再レビュー b）。"""
    from shiftvoice.intake import Clarify, to_proposal
    item = {"type": "count_limit", "shift": "夜", "max": 4} if "夜勤" in text else {"type": "consecutive_limit", "max": 4}
    with pytest.raises(Clarify):
        to_proposal([item], text, store.facility(), store.staff(), store.month(2026, 11))


@pytest.mark.parametrize("text,item", [
    ("おかだ君、夜勤は月4回まで", {"type": "count_limit", "shift": "夜", "max": 4}),
    ("おかだ、連勤は4日まで", {"type": "consecutive_limit", "max": 4}),
    ("岡田さん、連勤は4日まで。みんなに伝えて", {"type": "consecutive_limit", "max": 4}),
])
def test_count_and_run_without_person_need_everyone_word(store, text, item):
    """回数と連勤のルールで返事が人を省いた時は、一言に「全員」「みんな」が無いか、名前が出ていれば聞き返す（作者の答え 2026-11-03）。"""
    from shiftvoice.intake import Clarify, to_proposal
    with pytest.raises(Clarify):
        to_proposal([item], text, store.facility(), store.staff(), store.month(2026, 11))


@pytest.mark.parametrize("text", ["全員、夜勤は月4回まで", "みんな夜勤は月4回までにして"])
def test_count_with_everyone_word_goes_to_everyone(store, text):
    """「全員」「みんな」と言った回数のルールは、全員に効かせる（作者の答え 2026-11-03）。"""
    from shiftvoice.intake import to_proposal
    p = to_proposal([{"type": "count_limit", "shift": "夜", "max": 4}], text, store.facility(), store.staff(),
                    store.month(2026, 11))
    assert p.rules and not p.rules[0]["who"]


@pytest.mark.parametrize("text", ["岡田さん、12日はお子さんの熱で急に休み", "岡田さん、15日はお孫さんの運動会で休み",
                                  "岡田さん、20日は旦那さんの通院の付き添いで休み"])
def test_family_words_are_not_unknown_names(sess, text):
    """家族の言葉は名簿にない名前として止めない。岡田さんは伏せて送る。"""
    m = Masker(sess.store.staff(), sess.store.facility())
    masked, unknown = m.mask(text)
    assert unknown == [] and "岡田" not in masked


def test_unknown_person_still_stops_next_to_family_word(sess):
    """家族の言葉があっても、名簿にない人の名前は今どおり止める。"""
    m = Masker(sess.store.staff(), sess.store.facility())
    _, unknown = m.mask("森田さん、12日はお子さんの熱で休み")
    assert unknown == ["森田"]


@pytest.mark.parametrize("text", ["よし子さん、12日は休み", "岡田さんとはな子さん、12日は休み", "まご孫さん、12日は休み", "なお子さん、12日は休み", "旦那さんのなお子さん、12日休み",
                                  "とも子さん、12日は休み", "もも子さん、12日は休み", "かの子さん、12日は休み",
                                  "とお子さん、12日は休み", "12日はとも子さんが休み"])
def test_hiragana_name_ending_in_family_word_still_stops(sess, text):
    """ひらがなに家族の言葉が続く名前（よし子さん）は名前の一部かもしれない。止めるか、名前の頭ごと置き換えて、
    名前の頭を Claude へ送らない。"""
    import re
    masked, unknown = Masker(sess.store.staff(), sess.store.facility()).mask(text)
    head = re.search(r"([ぁ-ゖ]+)(?:子|孫)さん", text).group(1)[-2:]
    assert unknown or head + "子" not in masked and head + "孫" not in masked, (text, masked)


def test_every_not_name_word_after_hiragana_still_stops(sess):
    """名前でない語の一覧（NOT_NAMES・FAMILY_WORDS）のどの語でも、前にひらがななどの名前の頭が付いた形は、
    名簿にない名前として止めるか、名前の頭ごと置き換えて送る。どちらでも名前の頭は Claude へ送らない。
    一覧を広げるたびに抜け道ができた（失敗の一覧14・18）。一覧に語を足すと、この試験が全部の語で確かめる。"""
    from shiftvoice.names import FAMILY_WORDS, NOT_NAMES
    m = Masker(sess.store.staff(), sess.store.facility())
    leaks = []
    for w in sorted(NOT_NAMES | FAMILY_WORDS):
        for head in ("よし", "なお", "まご", "とも", "かの", "とお", "はお", "ゆー", "ｱｲ", "あいうえおかきくけこ"):
            for before in ("", "12日", "12日は", "岡田さんの"):
                masked, unknown = m.mask(f"{before}{head}{w}さん、休み")
                if not unknown and head in masked:
                    leaks.append(before + head + w)
    assert leaks == [], leaks


@pytest.mark.parametrize("text,name", [("あかさかちゃん、12日は休み", "あかさか"),
                                       ("12日はゆうちゃんが休み", "ゆう"),
                                       ("とおるくん、夜勤は3回まで", "とおる")])
def test_kana_nickname_with_chan_is_not_sent(sess, text, name):
    """ひらがなの呼び名に「ちゃん」「くん」が付いた形は、名簿の呼び方で伏せきれなければ送らずに聞き返す。"""
    _, unknown = Masker(sess.store.staff(), sess.store.facility()).mask(text)
    assert name in unknown


def test_baby_word_is_not_a_nickname(sess):
    _, unknown = Masker(sess.store.staff(), sess.store.facility()).mask("岡田さん、12日はあかちゃんの健診で休み")
    assert unknown == []
