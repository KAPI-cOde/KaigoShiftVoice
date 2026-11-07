"""一から作る前に、勤務ごとの人手の足りなさを数えて名前つきで言う（作者 2026-11-07。有給消化が2人になり夜勤が足りないのに、
計算はルールを外して作り、何が足りないかが分からなかった）。足りない時は表を変えずに止める。"""
import copy

from shiftvoice.model import Rule
from shiftvoice.shortage import shortage_text, shortages


def _caps(staff, n):
    return [Rule(id=f"t-cap-{s.name}", ver=1, name=f"{s.name}さん 夜勤は月{n}回まで", kind="count", flex="operational",
                 params={"shifts": ["夜"], "max": n}, who=[s.name], scope="rule") for s in staff if "夜" in s.can]


def test_no_shortage_in_the_sample(nov):
    fac, staff, m, rules = nov
    assert shortages(fac, staff, rules, m) == []


def test_night_shortage_is_named(nov):
    fac, staff, m, rules = nov
    x = shortages(fac, staff, copy.deepcopy(rules) + _caps(staff, 2), m)
    assert {i["unit"] for i in x} == {"東", "西"} and all(i["symbol"] == "夜" for i in x)   # 見本は夜勤がユニットごとに1人
    assert all(i["need"] == m.days and i["cap"] < i["need"] for i in x)
    t = shortage_text(x)
    assert "東の夜勤が" in t and "回足りません" in t and "表は変えていません" in t
    assert any(n + "さん（2回まで）" in t for n, _ in x[0]["people"])


def test_rebuild_stops_before_solving_when_short(sess):
    sess.open()
    led = sess.store.ledger()
    for r in _caps(sess.store.staff(), 2):
        d = r.to_dict() if hasattr(r, "to_dict") else {"id": r.id, "ver": 1, "name": r.name, "kind": r.kind, "flex": r.flex,
                                                       "params": r.params, "who": r.who, "scope": r.scope}
        led.append("add", rule=d, op_id="t")
    sess.store.save_ledger(led)
    before = sess.store.month(2026, 11).table
    r = sess.rebuild(tries=1)
    assert r.kind == "ask" and "足りません" in r.message
    assert sess.store.month(2026, 11).table == before


# 足りなさの知らせを見て、一言で名指しの上限を上げ、作り直すまで（作者 2026-11-07）。斉藤さんは西で夜勤に入れる
SAITO = "斉藤さん、毎月、夜勤は2回まで"


def _say(sess, text, scope):
    r = sess.say(text)
    if r.kind == "ask_period":
        r = sess.answer(r.uid, scope)
    return r


def _saito_cap(sess):
    """斉藤さんの11月の夜勤の上限（作り直しの前に足りなさを数える時と同じ数え方）"""
    from shiftvoice.build import month_rules
    from shiftvoice.rules import with_overrides
    from shiftvoice.shortage import _cap
    st = sess.store
    s = next(x for x in st.staff_on(2026, 11) if x.name == "斉藤")
    return _cap(st.facility(), with_overrides(month_rules(st, 2026, 11)), s, "夜", 30)


def _saito_rules(sess, between):
    return [r.name for r in sess.store.ledger().rules_at(between=between).values()
            if r.kind == "count" and r.who == ["斉藤"]]


def test_今月だけ上げると_その月は新しい上限が効き_翌月は前の上限に戻る(sess):
    """前は毎月2回までがあると「今の表で、もう守られています」と止まり、上限が上がらなかった"""
    sess.open()
    assert _say(sess, SAITO, "rule").kind == "applied"
    assert _saito_cap(sess) == 2
    r = _say(sess, "斉藤さんの夜勤は4回まで", "once")
    assert r.kind == "applied", r.message
    assert _saito_cap(sess) == 4
    assert _saito_rules(sess, ("2026-12-01", "2026-12-31")) == ["斉藤さん 夜勤は月2回まで"]


def test_毎月で上げると前の上限と置き換わる(sess):
    sess.open()
    _say(sess, SAITO, "rule")
    r = _say(sess, "斉藤さんの夜勤は4回まで", "rule")
    assert r.kind == "applied" and "置き換えました" in r.message, r.message
    assert _saito_cap(sess) == 4
    assert _saito_rules(sess, ("2026-11-01", "2026-11-30")) == ["斉藤さん 夜勤は月4回まで"]


def test_回数を増やしての一言は今の上限に足し_読み取りへ送らない(sess):
    sess.open()
    _say(sess, SAITO, "rule")
    n = len(sess.reader.sent)
    r = _say(sess, "斉藤さんの夜勤を1回増やして", "once")
    assert r.kind == "applied", r.message
    assert len(sess.reader.sent) == n
    assert _saito_cap(sess) == 3
    r = _say(sess, "斉藤さんの夜勤の上限を毎月2回上げてください", "rule")
    assert r.kind == "applied", r.message
    assert _saito_cap(sess) == 5
    assert _saito_rules(sess, ("2026-12-01", "2026-12-31")) == ["斉藤さん 夜勤は月5回まで"]


def test_上限の無い人の回数を増やしては今までどおり読み取りへ送る(sess):
    """上限が無い人の「夜勤を1回増やして」は、表に夜勤を足す頼みかもしれない。上限のルールを作らない"""
    from shiftvoice.session import raise_items
    sess.open()
    assert raise_items("職員12さんの夜勤を1回増やして", sess.store.facility(), lambda c, s: None) is None


def test_足りない時に上限を上げると_作り直しが止まらずに進む(sess):
    """西の夜勤に入れる7人で、毎日1人の夜勤に30回要る。小島さんは元の表の前提で4回。ほかを5・5・5・4・4回、斉藤さんを
    2回までにすると29回で1回足りない。知らせの文に出る言い方で斉藤さんを今月だけ4回までにすると、足りて作り直す"""
    sess.open()
    led = sess.store.ledger()
    for name, n in (("菊池", 5), ("久保", 5), ("篠原", 5), ("田村", 4), ("千葉", 4)):
        led.append("add", rule={"id": f"t-cap-{name}", "ver": 1, "name": f"{name}さん 夜勤は月{n}回まで", "kind": "count",
                                "flex": "operational", "params": {"shifts": ["夜"], "max": n}, "who": [name],
                                "when": None, "scope": "rule"}, op_id="t")
    sess.store.save_ledger(led)
    _say(sess, SAITO, "rule")
    r = sess.rebuild(tries=1)
    assert r.kind == "ask" and "西の夜勤が1回足りません" in r.message, r.message
    assert "「斉藤さんの夜勤は3回まで」のように言い" in r.message, r.message
    assert _say(sess, "斉藤さんの夜勤は4回まで", "once").kind == "applied"
    fac, staff = sess.store.facility(), sess.store.staff_on(2026, 11)
    from shiftvoice.build import month_rules
    assert shortages(fac, staff, month_rules(sess.store, 2026, 11), sess.store.month(2026, 11)) == []
    r = sess.rebuild(tries=1)
    assert r.kind == "applied", r.message
