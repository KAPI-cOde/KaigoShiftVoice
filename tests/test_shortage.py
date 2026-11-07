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
