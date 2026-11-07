"""事務の人の公休は「その月の土日祝の数」で、年末年始（12/29〜1/3）も休みに数える。数は月ごとに変わる
（作者 2026-11-07「毎月11日という考え方が間違っています。土日祝の数が事務員の公休数。年末年始だけ別のルール」）。
取り込みでは11月の数（11日）が毎月の決まった数として入り、12月はたまたま同じでも、1月（12日）・2月（10日）で合わなくなった。"""
import copy

from shiftvoice.model import Month, Rule
from shiftvoice.ruledetail import rule_detail
from shiftvoice.rules import expand, with_overrides
from shiftvoice.solver import solve_month


def _office(who="岡田", year_end=True):
    p = {"shifts": ["公"], "per_month": "weekend_holidays"}
    if year_end:
        p["year_end"] = True
    return Rule(id="t-office", ver=1, name=f"{who}さんの公休は、その月の土日祝の数", kind="count",
                flex="operational", params=p, who=[who], scope="rule")


def _month(y, m, holidays):
    return Month(year=y, month=m, holidays=holidays, prev_tail={})


def _n(rule, fac, staff, month):
    insts = expand(rule, fac, staff, month)
    assert len(insts) == 1, insts
    i = insts[0]
    assert i.lo == i.hi
    return i.lo


def test_count_changes_by_month(store):
    fac, staff = store.facility(), store.staff()
    r = _office()
    assert _n(r, fac, staff, _month(2026, 11, [3, 23])) == 11   # 土日9＋祝2
    assert _n(r, fac, staff, _month(2026, 12, [])) == 11        # 土日8＋29〜31日
    assert _n(r, fac, staff, _month(2027, 1, [1, 11])) == 12    # 土日10＋祝2（1/1〜3 は祝と土日に重なる）
    assert _n(r, fac, staff, _month(2027, 2, [11, 23])) == 10
    assert _n(_office(year_end=False), fac, staff, _month(2026, 12, [])) == 8


def test_named_month_count_takes_over_everyone_rule():
    """全員の公休の数より、その人の「土日祝の数」が先に当たる（両方の側）。"""
    general = Rule(id="t-all", ver=1, name="公休の数", kind="count", flex="operational",
                   params={"shifts": ["公"], "min": 9, "max": 9}, scope="rule")
    out = {r.id: r for r in with_overrides([general, _office()])}
    assert "岡田" in out["t-all"].meta.get("except_min", ())
    assert "岡田" in out["t-all"].meta.get("except_max", ())


def test_solver_gives_month_count(nov):
    fac, staff, m, rules = nov
    rules = copy.deepcopy(rules)
    for r in rules:
        if r.kind == "count" and r.params.get("shifts") == ["公"] and not r.who:
            r.flex = "operational"   # 見本の施設は動かせない強さ。名指しで上書きできる強さにして試す
    t = solve_month(fac, staff, rules + [_office()], m).table
    assert t["岡田"].count("公") == 11
    assert all(t[s.name].count("公") == 9 for s in staff if s.name != "岡田")


def test_detail_says_weekend_holidays(store):
    fac, staff = store.facility(), store.staff()
    m = _month(2026, 12, [])
    table = {s.name: ["日"] * 31 for s in staff}
    d = rule_detail("t-office", [_office()], fac, staff, m, table)
    assert "土日祝" in d["line"] and "11日" in d["line"], d
    assert not any("広げて" in x for x in d["now"]), d


def test_month_count_replaces_older_named_count():
    """帳面に同じ人の「公休11日」が残っていても、後から入れた「土日祝の数」が両方の側で代わる（2本並んで組めなくならない）。"""
    from shiftvoice.rules import _replace_same
    old = Rule(id="t-old", ver=1, name="岡田さん 公休11日", kind="count", flex="operational",
               params={"shifts": ["公"], "min": 11, "max": 11}, who=["岡田"], scope="rule", meta={"from": None})
    new = _office()
    new.meta = {"from": "2026-12-01"}
    out = {r.id: r for r in _replace_same([old, new])}
    assert "岡田" in out["t-old"].meta.get("except_min", ()) and "岡田" in out["t-old"].meta.get("except_max", ())


def test_detail_warns_when_month_has_no_holiday_table(store):
    """祝日の表に無い月は土日だけで数えるので、ルールの中身で知らせる。"""
    from dataclasses import replace
    fac, staff = store.facility(), store.staff()
    fac = replace(fac, settings={**fac.settings, "holidays": {"2026-12": []}})
    table = {s.name: ["日"] * 31 for s in staff}
    d = rule_detail("t-office", [_office()], fac, staff, _month(2028, 1, []), table)
    assert any("祝日の表がありません" in x for x in d["now"]), d
    d = rule_detail("t-office", [_office()], fac, staff, _month(2026, 12, []), table)
    assert not any("祝日の表" in x for x in d["now"]), d
