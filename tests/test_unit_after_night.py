"""明け番のいるユニットは8時の日勤、いないユニットは早番（作者 2026-11-07。朝は早番1人・8時の日勤2人で、夜勤の明けの人が
朝までいるユニットには遅い時刻の人で足りる）。「朝の人は各ユニット1人」だけを守ると、実際の施設の形の12月で40ユニット日が
この形から外れていた。実際の11月の表は90ユニット日すべてがこの形だった。"""
from dataclasses import replace

from shiftvoice.choke import chokepoints
from shiftvoice.model import Month, Rule, ShiftType, Staff
from shiftvoice.ruledetail import rule_detail
from shiftvoice.rules import check_table, expand
from shiftvoice.solver import solve_month

UNITS = ["東", "西", "南"]
RULE = Rule(id="t-after", ver=1, name="明け番のいるユニットは8時の日勤、いないユニットは早番", kind="unit_after", flex="fixed",
            params={"after": ["夜"], "yes": ["8"], "no": ["早"]}, scope="rule")


def _shape(store):
    """見本の施設の16人を3ユニットに分け、明けの記号を使わず、8時の日勤を足した形（作り話）。"""
    fac = store.facility()
    types = [t for t in fac.shift_types if t.kind != "ake" and t.symbol != "研"]
    types.append(ShiftType(symbol="8", name="8時の日勤", kind="work", start="08:00", hours=9))
    types = [replace(t, next_is_ake=False) for t in types]
    fac = replace(fac, units=UNITS, shift_types=types, settings={**fac.settings, "quality": {}})
    staff = [Staff(name=s.name, unit=UNITS[i % 3], can=["早", "日", "遅", "夜", "8"]) for i, s in enumerate(store.staff())]
    return fac, staff


def _month(staff, last_night=("東", "西"), days=(2026, 11)):
    tail = {s.name: ["公"] * 5 for s in staff}
    for u in last_night:
        tail[next(s.name for s in staff if s.unit == u)][-1] = "夜"
    return Month(year=days[0], month=days[1], holidays=[], prev_tail=tail)


def _base():
    def cov(i, sym, scope, op, n):
        return Rule(id=i, ver=1, name=i, kind="coverage", flex="fixed", params={"shifts": [sym], "scope": scope, "op": op, "n": n},
                    scope="rule")
    return [cov("t-e", "早", "all", "==", 1), cov("t-8", "8", "all", "==", 2), cov("t-l", "遅", "each_unit", "==", 1),
            cov("t-n", "夜", "all", "==", 2), cov("t-nu", "夜", "each_unit", "<=", 1),
            Rule(id="t-r", ver=1, name="公休", kind="count", flex="fixed", params={"shifts": ["公"], "min": 9, "max": 11}, scope="rule"),
            Rule(id="t-s", ver=1, name="夜勤の翌日", kind="sequence", flex="fixed", params={"if": ["夜"], "next_in": ["公"]}, scope="rule")]


def test_each_unit_day_has_two_scenes(store):
    fac, staff = _shape(store)
    m = _month(staff)
    insts = expand(RULE, fac, staff, m)
    assert len(insts) == 3 * 30 * 2
    first = [i for i in insts if i.key.endswith("/1") and i.key.startswith("東")]
    assert {(i.lo, i.hi) for i in first} == {(0, 0), (1, 1)}
    assert all(any(t.day == 0 and t.coef != 0 for t in i.terms) for i in first)   # 前の月の末日の夜勤も見る


def test_no_previous_month_skips_first_day(store):
    """前の月の末が分からない月の1日は、明け番が分からないので課さない。"""
    fac, staff = _shape(store)
    m = Month(year=2026, month=11, holidays=[], prev_tail={})
    assert not [i for i in expand(RULE, fac, staff, m) if i.key.endswith("/1")]


def test_solver_keeps_the_morning_shape(store):
    fac, staff = _shape(store)
    m = _month(staff)
    r = solve_month(fac, staff, _base() + [RULE], m, max_dtime=30.0)
    assert r.status == "ok"
    t, by = r.table, {u: [s.name for s in staff if s.unit == u] for u in UNITS}
    for u, names in by.items():
        for d in range(30):
            prev = sum((t[n][d - 1] if d else m.prev_tail[n][-1]) == "夜" for n in names)
            assert sum(t[n][d] == "8" for n in names) == prev, (u, d + 1)
            assert sum(t[n][d] == "早" for n in names) == 1 - prev, (u, d + 1)


def test_table_out_of_shape_is_reported(store):
    fac, staff = _shape(store)
    m = _month(staff, last_night=("東",))
    t = {s.name: ["公"] * 30 for s in staff}
    higashi = next(s.name for s in staff if s.unit == "東")
    t[higashi][0] = "早"   # 東は前の月の末に夜勤の人がいるのに、1日が早番
    keys = {b["key"] for b in check_table([RULE], fac, staff, m, t)}
    assert "東（明け番あり）/1" in keys and "東（明け番なし）/1" in keys


def test_support_counts_in_the_unit_of_that_day(store):
    """応援で別のユニットに入った日は、その日に入るユニットで数える。朝の人も、前の日の夜勤の人も（Astra 3×8）。"""
    fac, staff = _shape(store)
    m = _month(staff, last_night=())
    t = {s.name: ["公"] * 30 for s in staff}
    by = {u: [s.name for s in staff if s.unit == u] for u in UNITS}
    t[by["東"][0]][1] = "夜"        # 東の人が2日に夜勤
    t[by["東"][1]][2] = "8"         # 3日、東に明け番がいるので8時の日勤
    t[by["西"][0]][2] = "早"
    t[by["南"][0]][2] = "早"
    keys = lambda: {b["key"] for b in check_table([RULE], fac, staff, m, t) if b["key"].endswith("/3")}
    assert keys() == set()
    m.support[f"{by['東'][1]}/3"] = "南"   # 東の8時の人が3日は南へ応援。東に8時がいなくなり、南は早番と2人になる
    assert "東（明け番あり）/3" in keys() and "南（明け番あり）/3" in keys()
    m.support.clear()
    m.support[f"{by['東'][0]}/2"] = "西"   # 前の日の夜勤の人が西へ応援に入っていたら、明け番は西
    assert "西（明け番あり）/3" in keys() and "東（明け番あり）/3" in keys()


def test_detail_says_the_shape_and_where(store):
    fac, staff = _shape(store)
    m = _month(staff, last_night=("東",))
    t = {s.name: ["公"] * 30 for s in staff}
    d = rule_detail("t-after", [RULE], fac, staff, m, t)
    assert "明け番" in d["line"] and "8時の日勤" in d["line"] and "早番" in d["line"], d
    assert "東 1日" in d["now"][0], d


def test_choke_reads_the_day_of_the_scene(store):
    """詰まり所の探し方が、このルールの場面の日を読める（落ちない）。"""
    fac, staff = _shape(store)
    m = _month(staff)
    assert isinstance(chokepoints(fac, staff, _base() + [RULE], m), list)


APART = Rule(id="t-apart", ver=1, name="9時の日勤は2つ以上のユニットに分ける", kind="unit_apart", flex="operational",
             params={"shifts": ["日"]}, scope="rule")


def test_nine_oclock_people_are_not_in_one_unit(store):
    """9時の日勤の最低2人は、どのユニットでもよいが別のユニット（作者 2026-11-07「ユニットは別」）。"""
    fac, staff = _shape(store)
    m = _month(staff)
    two = Rule(id="t-2", ver=1, name="9時は2人以上", kind="coverage", flex="fixed",
               params={"shifts": ["日"], "scope": "all", "op": ">=", "n": 2}, scope="rule")
    t = solve_month(fac, staff, _base() + [two, APART], m, max_dtime=30.0).table
    unit = {s.name: s.unit for s in staff}
    for d in range(30):
        assert len({unit[n] for n in t if t[n][d] == "日"}) >= 2, d + 1


def test_table_with_one_unit_is_reported(store):
    fac, staff = _shape(store)
    m = _month(staff)
    t = {s.name: ["公"] * 30 for s in staff}
    higashi = [s.name for s in staff if s.unit == "東"][:2]
    for n in higashi:
        t[n][2] = "日"
    days = {b["key"].rsplit("/", 1)[1] for b in check_table([APART], fac, staff, m, t)}
    assert "3" in days
    d = rule_detail("t-apart", [APART], fac, staff, m, t)
    assert "2つ以上のユニット" in d["line"] and "固まった日" in d["now"][0], d
    t[next(s.name for s in staff if s.unit == "西")][2] = "日"
    assert "3" not in {b["key"].rsplit("/", 1)[1] for b in check_table([APART], fac, staff, m, t)}
