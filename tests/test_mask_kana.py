"""名前の伏せ方。声の読み取りは名前をひらがな・カタカナや別の字体（齋藤）で返すことがある。
名簿に読みがあればその読みでも伏せ、読みが分からないかなの名前は Claude へ送らずに聞き返す。
名前でない言葉（続くんは・いちばん・たくさん）では止めない。"""
import json
import re

import pytest

from shiftvoice.model import Staff
from shiftvoice.names import Masker


def _no_yomi(staff):
    return [Staff.from_dict({k: v for k, v in s.to_dict().items() if k != "yomi"}) for s in staff]


def _roster(store, yomi=True):
    staff = []
    for s in _no_yomi(store.staff()):
        d = s.to_dict()
        if yomi and s.name in YOMI:
            d["yomi"] = YOMI[s.name]
        staff.append(Staff.from_dict(d))
    unit = store.facility().units[0]
    staff.append(Staff.from_dict({"name": "遠藤ゆう", "unit": unit, "can": ["日"], "call": "ゆうちゃ",
                                  **({"yomi": "えんどうゆう"} if yomi else {})}))
    return staff


YOMI = {"千葉": "ちば", "石田": "いしだ", "菊池": "きくち", "斉藤": "さいとう", "小川": "おがわ"}


def _kata(s):
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in s)


def _forms(staff):
    out = set()
    for s in staff:
        for w in (s.name, s.call, getattr(s, "yomi", None)):
            if w:
                out |= {w, _kata(w)}
    return out


SAID = [
    "千葉さんの夜勤は4回まで",
    "ちばさんの夜勤は4回まで",
    "チバさんの夜勤は4回まで",
    "いしださん じゅうににち やすみ",
    "らいげつはちばさんが早番",
    "キクチさん9日休み",
    "きくちの夜勤なし",
    "齋藤さん水曜は早番なしで",
    "斎藤の夜勤は3回まで",
    "おがわさんとちばさんは同じ日に夜勤にしない",
    "ゆうちゃ20日休み",
    "エンドウユウさんは早番だけ",
]


def test_no_roster_name_in_any_script_is_sent(store):
    """送る文に名簿の名前（漢字・ひらがな・カタカナ・呼び名・別の字体）が1つも残らず、聞き返しにもならない。"""
    staff = _roster(store)
    m = Masker(staff, store.facility())
    forms = _forms(staff) | {"齋藤", "斎藤"}
    for text in SAID:
        masked, unknown = m.mask(text)
        assert not unknown, (text, masked, unknown)
        left = [w for w in forms if w in masked]
        assert not left, (text, masked, left)
        assert re.search(r"職員\d\d", masked), (text, masked)


def test_kana_name_without_yomi_is_not_sent(store):
    """名簿に読みが無ければ、ひらがな・カタカナの名前は伏せられない。その時は送らずに聞き返す。"""
    m = Masker(_roster(store, yomi=False), store.facility())
    for text in ("ちばさんの夜勤は4回まで", "いしださん じゅうににち やすみ", "らいげつはちばさんが早番",
                 "チバさんの夜勤は4回まで", "おがわさまの早番は5回まで", "おかだ君、夜勤は月4回まで",
                 "おかだ様の早番は5回まで"):
        masked, unknown = m.mask(text)
        assert unknown, (text, masked)


def test_kana_name_without_yomi_does_not_reach_claude(sess):
    sess.store.save_staff(_no_yomi(sess.store.staff()))   # 取り込んで作った施設のように、名簿に読みが無い
    n = len(sess.reader.sent)
    r = sess.say("ちばさんの夜勤は4回まで")
    assert r.kind == "ask" and "名簿" in r.message, r
    assert len(sess.reader.sent) == n


def test_katakana_and_variant_get_a_candidate(store):
    m = Masker(_roster(store), store.facility())
    assert m.suggest("キクチ") == ["菊池"]
    assert m.suggest("チバ") == ["千葉"]
    assert m.suggest("齋藤") == ["斉藤"]


def test_variant_is_not_guessed_when_two_people_share_it(store):
    """斉藤と斎藤が両方名簿にいる時、齋藤はどちらとも決めずに聞き返す。"""
    staff = _roster(store) + [Staff(name="斎藤", unit=store.facility().units[0], can=["日"])]
    m = Masker(staff, store.facility())
    masked, unknown = m.mask("齋藤さん水曜は早番なしで")
    assert unknown == ["齋藤"], (masked, unknown)
    assert m.suggest("齋藤") == ["斉藤", "斎藤"]
    for text, who in (("斉藤さん休み", "斉藤"), ("斎藤さん休み", "斎藤")):
        masked, unknown = m.mask(text)
        assert not unknown and masked.startswith(m.to_code[who]), (text, masked)


def test_words_that_are_not_names_do_not_stop(store):
    m = Masker(_roster(store), store.facility())
    for text in ("遅番ばっかり続くんはしんどいから3日までにしたって",
                 "夜勤が続くんやったら間に休みを入れて",
                 "早番に行くんは週3回まで",
                 "夜勤がいちばん多い人を減らして",
                 "みなさんの早番は5回まで",
                 "夜勤がたくさん続かないように",
                 "おつかれさまです、早番は5回まで",
                 "じゅうさんにちは日勤を3人"):
        masked, unknown = m.mask(text)
        assert not unknown and not m.leftover, (text, masked, unknown)


def test_names_not_on_roster_still_stop(store):
    m = Masker(_roster(store), store.facility())
    for text in ("森くんは夜勤なし", "関くんは12日休み", "健くんの早番は3回まで", "ヤマモトさん休み"):
        masked, unknown = m.mask(text)
        assert unknown, (text, masked)


def test_yomi_inside_other_words(store):
    """読みが普通の言葉の中にある時（いちばん）は通し、区切りの無い一言に名前の読みが残った時は送らない。"""
    m = Masker(_roster(store), store.facility())
    masked, unknown = m.mask("らいげつちば15日休み")
    assert unknown and "ちば" in m.leftover, (masked, unknown)
    assert m.leaks("夜勤がいちばん多い") == []
    assert m.leaks("ちばの夜勤") == ["千葉"] and m.leaks("齋藤の早番") == ["斉藤"]


def test_seed_roster_masks_kana_and_katakana(store):
    """見本の施設の名簿には読みがある。測りで送られた・候補が出なかった一言（ちば・いしだ・チバ・キクチ）を伏せる。"""
    staff = store.staff()
    assert all(getattr(s, "yomi", None) for s in staff)
    m = Masker(staff, store.facility())
    forms = _forms(staff)
    for text in ("ちばさんの夜勤は4回まで", "いしださん じゅうににち やすみ", "チバさんの夜勤は4回まで",
                 "キクチさん9日休み", "齋藤さん水曜は早番なしで"):
        masked, unknown = m.mask(text)
        assert not unknown, (text, masked, unknown)
        assert not [w for w in forms | {"齋藤"} if w in masked], (text, masked)


def _hanako(store, yomi="うちだ はなこ"):
    unit = store.facility().units[0]
    return _roster(store) + [Staff.from_dict({"name": "内田花子", "unit": unit, "can": ["日"],
                                              **({"yomi": yomi} if yomi else {})})]


def test_given_name_alone_is_masked(store):
    """名簿が「内田花子」で呼び名が無くても、下の名前だけ（敬称なしも）・その読みで呼んだ一言を伏せる（Astra の指摘）。"""
    for staff in (_hanako(store), _hanako(store, yomi=None)):
        m = Masker(staff, store.facility())
        code = m.to_code["内田花子"]
        texts = ["花子は12日休み", "花子の夜勤なし", "花子さんは早番だけ", "花子15日休み", "あと花子も早番なし"]
        if getattr(staff[-1], "yomi", None):
            texts += ["はなこは12日休み", "ハナコさんの夜勤は3回まで", "うちだの早番は5回まで"]
        for text in texts:
            masked, unknown = m.mask(text)
            assert not unknown and not m.leftover, (text, masked, unknown)
            assert code in masked, (text, masked)
            assert not [w for w in ("花子", "はなこ", "ハナコ", "内田", "うちだ") if w in masked], (text, masked)


def test_given_name_of_someone_else_is_not_sent(store):
    """名簿にない人の姓につながった下の名前（森田花子）は、名簿の人にせず、送らずに聞き返す。"""
    m = Masker(_hanako(store), store.facility())
    masked, unknown = m.mask("森田花子は15日休み")
    assert unknown and "職員" not in masked, (masked, unknown)


def test_more_words_that_are_not_names_do_not_stop(store):
    m = Masker(_roster(store), store.facility())
    for text in ("リーダーさんの夜勤は4回まで", "ヘルパーさんが少ない日は日勤を3人", "夜勤はさんにんまで",
                 "おかげさまで、早番は5回まで"):
        masked, unknown = m.mask(text)
        assert not unknown and not m.leftover, (text, masked, unknown)


def test_choose_name_after_katakana_left_over(sess):
    """区切りの無いカタカナの名前（キクチ来週休み）は送らずに聞き、選んだ名前に置き換えて読む。聞いた語が一言のままの形で、
    選ぶと「名簿の名前から選んでください」で行き止まりにならない。"""
    r = sess.say("キクチ来週休み")
    assert r.kind == "ask" and r.extra["ask_name"]["wrong"] == "キクチ", r
    assert r.extra["ask_name"]["candidates"] == ["菊池"]
    assert sess.reader.sent == []
    r = sess.choose_name(r.uid, r.extra["ask_name"]["wrong"], "菊池")
    assert r.message != "名簿の名前から選んでください", r
    sent = json.dumps([x.messages for x in sess.reader.sent], ensure_ascii=False)
    assert sent and "キクチ" not in sent and "菊池" not in sent and "職員09" in sent, sent


@pytest.mark.parametrize("text,item", [
    ("おかださん、15と16は休み", {"type": "day_off", "days": [15, 16]}),
    ("あおき、15と16は休み", {"type": "day_off", "days": [15, 16]}),
    ("おかださん、夜勤は月4回まで", {"type": "count_limit", "shift": "夜", "max": 4}),
    ("おかだ君、夜勤は月4回まで", {"type": "count_limit", "shift": "夜", "max": 4}),
    ("おかだ、連勤は4日まで", {"type": "consecutive_limit", "max": 4}),
])
def test_kana_name_without_yomi_never_goes_to_everyone(store, text, item):
    """見本の名簿に読みを足したので、読みの無い名簿でのかなの名前の道（tests/test_safety.py の同じ一言）もここで確かめる。
    返事が人を省いても全員に効かせない。"""
    from shiftvoice.intake import Clarify, to_proposal
    staff = _no_yomi(store.staff())
    assert not any(getattr(s, "yomi", None) for s in staff)
    with pytest.raises(Clarify):
        to_proposal([item], text, store.facility(), staff, store.month(2026, 11))


@pytest.mark.parametrize("text", ["遅番ばっかり続くんはしんどいから3日までにしたって", "夜勤が続くんやったら間に休みを入れて"])
def test_kansai_ku_n_is_not_a_person_when_read_as_everyone(store, text):
    """「続くんは」（続くのは）を、人を呼んだと見ない。読み直しが全員のルールと読めば、そのまま全員に効かせる（c07）。"""
    from shiftvoice.intake import to_proposal
    p = to_proposal([{"type": "consecutive_limit", "shift": "遅", "max": 3, "everyone": True}], text,
                    store.facility(), store.staff(), store.month(2026, 11))
    assert p.rules and not p.rules[0]["who"]


@pytest.mark.parametrize("text", ["健くん、連勤は3日まで", "たくくん、連勤は3日まで"])
def test_kun_after_a_name_is_still_a_person(store, text):
    """名前に付いた「くん」は今どおり人を呼んだと見て、返事が人を省いても全員に効かせない。"""
    from shiftvoice.intake import Clarify, to_proposal
    with pytest.raises(Clarify):
        to_proposal([{"type": "consecutive_limit", "max": 3, "everyone": True}], text,
                    store.facility(), store.staff(), store.month(2026, 11))
