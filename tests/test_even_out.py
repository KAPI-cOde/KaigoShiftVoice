"""「早番も遅番も、可能な範囲で平均化して」。何回から何回とは決めず、人ごとの回数をできるだけそろえる（作者 2026-11-08
「月3〜4回にして、とは通常言わない。それで組めるか分からないから。『可能な範囲で平均化して』が通る方が良い」）。

- 外れを持たない（守れなかった件に数えない）。前に足したルール（遅番の次の日の早番・遅番の3連続）を崩してまでならさない
- 数えるのはその勤務に入れる人だけ。名指しの回数のルールや本人の希望が先に効く
"""
from shiftvoice.model import Facility, Month, Rule, ShiftType, Staff
from shiftvoice.resolve import resolve
from shiftvoice.rules import check_table
from shiftvoice.solver import solve_month


def _spread(table, staff, sym):
    cs = [table[s.name].count(sym) for s in staff if sym in s.can]
    return min(cs), max(cs)


def _late_then_early(table):
    return sum(1 for row in table.values() for a, b in zip(row, row[1:]) if a == "遅" and b == "早")


def _late_runs3(table, month):
    """遅番が3日以上続く所の数（前の月の末から続く並びも数える）。"""
    n = 0
    for name, row in table.items():
        tail = list(month.prev_tail.get(name) or [])
        run = 0
        for i, v in enumerate(tail + list(row)):
            run = run + 1 if v == "遅" else 0
            n += run == 3 and i >= len(tail)
    return n


# ---- 小さな施設で、偏った表からならす（一言の直しと同じ resolve）

NAMES = ["赤坂", "井上", "内田", "遠藤", "太田"]


def _small(can_early=None):
    fac = Facility("small", "小さな施設（架空）", ["東"], [
        ShiftType("早", "早番", "work", "07:00", 8), ShiftType("日", "日勤", "work", "09:00", 8),
        ShiftType("遅", "遅番", "work", "13:00", 8), ShiftType("公", "公休", "rest")],
        {"solve": {"seed": 11, "workers": 1, "max_deterministic_time": 10.0, "resolve_max_deterministic_time": 5.0},
         "keep_weight": 500})
    can_early = can_early or NAMES
    staff = [Staff(n, "東", (["早"] if n in can_early else []) + ["日", "遅"]) for n in NAMES]
    month = Month(2026, 11, [], {n: ["日"] * 5 for n in NAMES})
    rules = [
        Rule("c1", 1, "早番の人数", "coverage", "fixed", {"shifts": ["早"], "scope": "all", "op": "==", "n": 1}),
        Rule("c2", 1, "遅番の人数", "coverage", "fixed", {"shifts": ["遅"], "scope": "all", "op": "==", "n": 1}),
        Rule("o1", 1, "公休の数", "count", "fixed", {"shifts": ["公"], "min": 8, "max": 8}),
        Rule("s1", 1, "遅番の次の日に早番を置かない", "sequence", "negotiable", {"if": ["遅"], "next_not_in": ["早"]}),
        Rule("m1", 1, "遅番は続けて2日まで", "max_run", "operational", {"shifts": ["遅"], "max": 2}),
    ]
    return fac, staff, month, rules


def _skewed(fac, staff, month, rules, who="赤坂", n=12):
    """1人に早番を寄せた表（前の表）。"""
    lean = Rule("x", 1, "寄せる", "count", "fixed", {"shifts": ["早"], "min": n}, who=[who])
    res = solve_month(fac, staff, rules + [lean], month)
    assert res.status == "ok"
    return res.table


def _even_rule(shifts=("早",), who=None):
    return Rule("e1", 1, "全員 早番の回数を、できるだけそろえる", "even", "negotiable", {"shifts": list(shifts)}, who=who)


def test_small_facility_spread_narrows_and_keeps_rules():
    fac, staff, month, rules = _small()
    base = _skewed(fac, staff, month, rules)
    lo, hi = _spread(base, staff, "早")
    assert hi - lo >= 5
    rr = resolve(fac, staff, rules + [_even_rule()], month, base, [_even_rule()])
    assert rr.ok
    t = rr.table
    lo, hi = _spread(t, staff, "早")
    assert hi - lo <= 1, (lo, hi)   # 5人で月30回なので6回ずつ
    assert _late_then_early(t) == 0 and _late_runs3(t, month) == 0
    assert check_table(rules, fac, staff, month, t) == []
    assert check_table([_even_rule()], fac, staff, month, t) == []   # 外れを持たない


def test_small_facility_counts_only_people_who_can_work_it():
    """太田さんは早番に入れない。入れる4人でならす（30回を4人で7〜8回）。"""
    fac, staff, month, rules = _small(can_early=NAMES[:4])
    base = _skewed(fac, staff, month, rules)
    rr = resolve(fac, staff, rules + [_even_rule()], month, base, [_even_rule()])
    assert rr.ok
    assert rr.table["太田"].count("早") == 0
    lo, hi = _spread(rr.table, staff, "早")
    assert (lo, hi) == (7, 8)


def test_named_count_rule_and_wish_come_first():
    """名指しの回数のルール（井上さんは早番を月10回以上）と本人の希望（遠藤さんは1〜3日は遅番）が先に効く。
    ほかの人は残りでならす。名指しの人に寄せて、ほかの人を減らしもしない（井上さんはちょうど10回）。"""
    fac, staff, month, rules = _small()
    base = _skewed(fac, staff, month, rules)
    named = Rule("n1", 1, "井上さん 早番は月10回以上", "count", "operational", {"shifts": ["早"], "min": 10}, who=["井上"])
    wish = Rule("w1", 1, "遠藤さん 1・2日 遅番", "cell", "wish", {"in": ["遅"]}, who=["遠藤"], when=[1, 2])
    rs = rules + [named, wish, _even_rule()]
    rr = resolve(fac, staff, rs, month, base, [named, wish, _even_rule()])
    assert rr.ok
    t = rr.table
    assert t["井上"].count("早") == 10
    assert t["遠藤"][:2] == ["遅", "遅"]
    others = [t[n].count("早") for n in NAMES if n != "井上"]
    assert max(others) - min(others) <= 1, others   # 残り20回を4人で5回ずつ
    assert check_table(rs, fac, staff, month, t) == []
