"""Astra の敵対レビュー（2026-11-06）の指摘1・4・5・6。
1 日勤だけにして表を直した時、ほかの人の希望を外したら黙らずに知らせる
4 別の月の希望の表を貼っても、開いている月に入れない
5 名簿にない長い名前を、先頭が同じ短い名前の別の人に入れない
6 期間や「12/15」で言った別の月の一言も、月違いの聞き返しを通す"""
import pytest

from shiftvoice.nextflow import NextFlow, WishOtherMonth, parse_wish_paste
from shiftvoice.period import other_month
from shiftvoice.session import Reply


def _paste(header, rows, title=None):
    lines = [title] if title else []
    lines.append("\t".join(header))
    lines += ["\t".join([n] + ["希" if i + 1 in ds else "" for i in range(len(header) - 1)]) for n, ds in rows]
    return "\n".join(lines)


NOV = ["名前"] + [f"11/{d}" for d in range(1, 31)]
BARE = ["名前"] + [str(d) for d in range(1, 32)]
ROSTER = ["岡田", "千葉", "青木"]


# 1 日勤だけ ---------------------------------------------------------------------------

def _pin_unit_on_day(sess, who):
    """who が早番か遅番の日を1日選び、同じユニットのほかの人全員に、その日の今の勤務を希望として入れる。
    人数は動かせないので、who の代わりに誰かの希望を外すしか表が作れない"""
    table = sess.store.month(2026, 11).table
    staff = sess.store.staff()
    unit = next(s.unit for s in staff if s.name == who)
    day = next(d for d, sym in enumerate(table[who], 1) if sym in ("早", "遅"))
    led = sess.store.ledger()
    names = []
    for i, s in enumerate(x for x in staff if x.unit == unit and x.name != who):
        name = f"{s.name}さん {day}日の希望"
        led.append("add", rule={"id": f"r9{i:02d}", "name": name, "kind": "cell", "flex": "wish", "scope": "once",
                                "params": {"in": [table[s.name][day - 1]]}, "who": [s.name], "when": [day],
                                "meta": {}},
                   op_id=f"pin-{i}", frm="2026-11-01", until="2026-11-30", meta={})
        names.append(name)
    sess.store.save_ledger(led)
    return names


def test_day_only_tells_when_someone_elses_wish_is_dropped(sess):
    names = _pin_unit_on_day(sess, "杉山")
    r = sess.say("杉山さんは日勤だけにして")
    assert r.kind == "applied", r.message
    assert "守れなくなりました" in r.warning and any(n in r.warning for n in names), r.warning
    assert "：" not in r.warning and ":" not in r.warning


def test_day_only_says_nothing_when_nothing_is_dropped(sess):
    r = sess.say("杉山さんは日勤だけにして")
    assert r.kind == "applied" and "守れなくなりました" not in r.warning


# 4 別の月の希望の表 --------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    _paste(NOV, [("岡田", {1, 2})]),                                        # 見出しに月
    _paste(["名前"] + [f"11月{d}日" for d in range(1, 31)], [("岡田", {1})]),
    _paste(["名前"] + [f"2026-11-{d:02d}" for d in range(1, 31)], [("岡田", {1})]),
    _paste(["名前"] + [str(46326 + d) for d in range(1, 31)], [("岡田", {1})]),  # Excel の通し番号（11月）
    _paste(BARE, [("岡田", {1})], title="11月の希望休"),                      # 題に月
    _paste(["11月"] + BARE[1:], [("岡田", {1})]),                            # 左上の隅に月
])
def test_other_month_paste_is_refused(text):
    with pytest.raises(WishOtherMonth) as e:
        parse_wish_paste(text, ROSTER, 31, 12)
    assert e.value.said == 11


@pytest.mark.parametrize("text", [
    _paste(["名前"] + [f"12/{d}" for d in range(1, 32)], [("岡田", {1, 2})]),
    _paste(BARE, [("岡田", {1, 2})], title="12月の希望休"),
    _paste(BARE, [("岡田", {1, 2})]),                                        # 月が書いていない表
    _paste(BARE, [("岡田", {1, 2})], title="12月の希望休（11月30日締切）"),
    _paste(BARE, [("岡田", {1, 2})], title="11月30日締切"),
])
def test_paste_of_the_month_goes_in(text):
    assert parse_wish_paste(text, ROSTER, 31, 12)[0] == {"岡田": [1, 2]}


def test_mark_on_the_other_month_part_is_refused():
    """11/29 から始まる見出し。12月のマスだけなら入り、11月のマスに「希」があれば入れない"""
    head = ["名前", "11/29", "11/30"] + [f"12/{d}" for d in range(1, 32)]
    assert parse_wish_paste(_paste(head, [("岡田", {3})]), ROSTER, 31, 12)[0] == {"岡田": [1]}
    with pytest.raises(WishOtherMonth):
        parse_wish_paste(_paste(head, [("岡田", {1, 3})]), ROSTER, 31, 12)


def test_other_month_paste_adds_nothing(sess):
    fl = NextFlow(sess.store, 2026, 11)
    fl.review()
    rev = sess.store.ledger().rev
    w = fl.wish_paste(_paste(NOV, [("岡田", {1, 2})]))
    assert w["wish_error"] == "11月の希望の表です。いま入れているのは12月です。12月の表を貼ってください。何も入れていません"
    assert w["wishes"] == [] and sess.store.ledger().rev == rev


# 5 別の人の希望 ------------------------------------------------------------------------

def test_longer_name_is_not_the_short_name():
    got, unknown = parse_wish_paste(_paste(BARE, [("青木石田", {3}), ("岡田", {4})]), ROSTER, 31, 12)
    assert got == {"岡田": [4]} and unknown == ["青木石田"]


@pytest.mark.parametrize("cell", ["青木 石田", "青木　石田", "青木さん"])
def test_name_with_a_space_is_still_the_person(cell):
    assert parse_wish_paste(_paste(BARE, [(cell, {3})]), ROSTER, 31, 12) == ({"青木": [3]}, [])


def test_unknown_longer_name_is_told(sess):
    fl = NextFlow(sess.store, 2026, 11)
    fl.review()
    w = fl.wish_paste(_paste(BARE, [("青木石田", {3}), ("岡田", {4})]))
    assert [x["who"] for x in w["wishes"]] == [["岡田"]]
    assert "名簿にない名前は入れていません（青木石田）" in w["wish_note"]


# 6 期間と「12/15」 ---------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "岡田さん、12月1日から3日まで希望休",
    "岡田さん、12月の1日から3日まで休み",
    "岡田さん、12月1日から3日は休み",
    "岡田さん、12月1日〜3日は休み",
    "岡田さん、１２月１日から１２月３日まで休み",
    "岡田さん、12/1から12/3まで休み",
    "岡田さん、12/1〜3は休み",
    "岡田さん、12/15は休み",
    "岡田さん、１２／１５は休み",
    "岡田さん、2026/12/15は休み",
])
def test_other_month_is_found(text):
    assert other_month(text, 11) == 12


@pytest.mark.parametrize("text", [
    "杉山さん、12月1日から夜勤もできる",        # ほかの月からの変更
    "杉山さん、12月から夜勤もできるって",
    "杉山さん、1月31日まで夜勤なし",            # 期限
    "12月1日から3人にして",                     # 期間ではない
    "杉山さん、12月1日から3か月は日勤だけ",
    "岡田さん、11月1日から3日まで希望休",        # 開いている月
    "岡田さん、11/15は休み",
    "岡田さん、11月20日から12月5日まで休み",     # 開いている月にかかる
    "岡田さん、10月25日から11月5日まで休み",
])
def test_not_other_month(text):
    assert other_month(text, 11) is None


def test_period_of_other_month_is_asked_not_sent(sess):
    class NoCall:
        calls_claude = True

        def read(self, masked, history=None):
            raise AssertionError(f"Claude へ送った {masked}")

    nov = sess.store.month(2026, 11).table
    rev = sess.store.ledger().rev
    sess.reader = NoCall()
    for said in ("岡田さん、12月1日から3日まで希望休", "岡田さん、12/15は休み"):
        r = sess.say(said)
        assert isinstance(r, Reply) and r.kind == "ask" and "12月の頼みです" in r.message and r.sent is None
    assert sess.store.month(2026, 11).table == nov and sess.store.ledger().rev == rev


def test_period_in_wish_box_is_refused(sess):
    fl = NextFlow(sess.store, 2026, 11)
    fl.review()
    e = fl.wish("岡田さん、11月1日から3日まで希望休", None)
    assert "11月の頼みです" in e["wish_error"] and e["wishes"] == []
