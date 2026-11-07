"""有給は、組んで人が余った日に、なるべく平等に入れる（作者 2026-11-07「誰が何回取れるかは組んでみるまでわからない。
希望休等の制約の中で、なるべく平等に有給を入れるのが基本の方針。『今月は何回入れる』ではない」）。
前は有給を頼まれた人・日にしか開けず、人が余る月（31日の12月など）は、日勤が上限を超えるか公休が増えた。"""
import copy

from shiftvoice.build import solve_staged
from shiftvoice.fairleave import _level, shares, spare_table
from shiftvoice.model import Rule
from shiftvoice.ruledetail import rule_detail
from shiftvoice.rules import fair_on
from shiftvoice.solver import requested_cells, solve_month

FAIR = Rule(id="t-fair", ver=1, name="有給は、人が余った日になるべく平等に入れる", kind="count", flex="operational",
            params={"shifts": ["有"], "fair": True}, scope="rule")
# 日勤を毎日2人までにすると、公休9日の16人では人が余る（見本の施設の11月は日勤が1日3人ほど）
DAY_CAP = Rule(id="t-daycap", ver=1, name="日勤は2人まで", kind="coverage", flex="operational",
               params={"shifts": ["日", "研"], "scope": "all", "op": "<=", "n": 2}, scope="rule")


def test_fair_rule_opens_leave_for_everyone(nov):
    fac, staff, m, rules = nov
    # ふだんは何もしないルール。一から作る時（fair_on）だけ開く
    before = requested_cells(rules + [FAIR], fac, staff, m)
    assert not any("有" in v for v in before.values())
    opened = requested_cells(fair_on(rules + [FAIR]), fac, staff, m)
    assert all("有" in opened.get((s.name, d), ()) for s in staff for d in range(1, m.days + 1))


def test_spare_people_get_leave_evenly(nov):
    fac, staff, m, rules = nov
    r = solve_staged(fac, staff, copy.deepcopy(rules) + [DAY_CAP, FAIR, CAP0], m, even_leave=True).result
    assert r.status == "ok"
    t = r.table
    assert all(sum(t[n][d] in ("日", "研") for n in t) <= 2 for d in range(m.days))
    counts = {s.name: t[s.name].count("有") for s in staff}
    assert counts["斉藤"] == 0                      # 上限0の人には入れない
    others = [v for n, v in counts.items() if n != "斉藤"]
    assert sum(others) > 0
    assert max(others) - min(others) <= 1, counts   # 1人あたりの差は1日まで


def test_only_named_people_get_leave(nov):
    fac, staff, m, rules = nov
    who = [s.name for s in staff][:8]
    fair = copy.deepcopy(FAIR)
    fair.who = who
    # 余りを8人の有給だけで受けられる程度にする（日勤3人まで）
    cap = copy.deepcopy(DAY_CAP)
    cap.params["n"] = 3
    t = solve_month(fac, staff, fair_on(copy.deepcopy(rules) + [cap, fair]), m).table
    assert all(t[s.name].count("有") == 0 for s in staff if s.name not in who)


def test_no_spare_no_leave(nov):
    """人が余らない月は、有給を勝手に入れない（1日ごとに小さな罰）。"""
    fac, staff, m, rules = nov
    t = solve_month(fac, staff, fair_on(copy.deepcopy(rules) + [FAIR]), m).table
    assert sum(row.count("有") for row in t.values()) == 0


def test_detail_says_even(nov, nov_result):
    fac, staff, m, rules = nov
    d = rule_detail("t-fair", rules + [FAIR], fac, staff, m, nov_result.table)
    assert "平等" in d["line"] or "同じ" in d["line"], d


CAP0 = Rule(id="t-cap0", ver=1, name="斉藤さん 有給は月0日まで", kind="count", flex="operational",
            params={"shifts": ["有"], "max": 0}, who=["斉藤"], scope="rule")


def test_fresh_build_splits_spare_evenly(nov):
    """一から作る時（even_leave）は、余りの日数を平等に割る。上限0の人には入れない。余りより多く入れない。"""
    fac, staff, m, rules = nov
    rs = copy.deepcopy(rules) + [DAY_CAP, FAIR, CAP0]
    spare = spare_table(fac, staff, fair_on(rs), m)
    total = sum(row.count("有") for row in spare.values())
    assert total > 0
    out = solve_staged(fac, staff, rs, m, even_leave=True)
    assert out.ok and out.result.stage == 1
    t = out.result.table
    assert t["斉藤"].count("有") == 0
    counts = [t[s.name].count("有") for s in staff if s.name != "斉藤"]
    assert max(counts) - min(counts) <= 1, counts
    assert max(counts) <= -(-total // len(counts)), (counts, total)   # 余りを割った日数より多く入れない


def test_keeps_first_table_when_even_search_fails(nov, monkeypatch):
    """平等にそろえる計算が時間切れでも、1回目の表は捨てない。"""
    import shiftvoice.solver as S
    from shiftvoice.fairleave import even_out
    fac, staff, m, rules = nov
    rs = fair_on(copy.deepcopy(rules) + [DAY_CAP, FAIR])
    first = solve_month(fac, staff, rs, m)
    first.table = {n: list(row) for n, row in first.table.items()}
    name = staff[0].name
    first.table[name][:3] = ["有", "有", "有"]   # 平等でない表にする
    monkeypatch.setattr(S, "solve_month", lambda *a, **k: S.SolveResult("timeout", None))
    res, line = even_out(fac, staff, rs, m, first)
    assert res is first and line["status"] == "timeout"


def test_asked_leave_is_kept_when_evened(nov):
    """「有給2日」と頼まれた人の日数を、平等にそろえる時に下げない。平等に割るのはその上で。"""
    fac, staff, m, rules = nov
    asked = Rule(id="t-ask", ver=1, name="岡田さん 有給2日（日付は任せる）", kind="count", flex="wish",
                 params={"shifts": ["有"], "min": 2, "max": 2}, who=["岡田"], scope="once")
    out = solve_staged(fac, staff, copy.deepcopy(rules) + [DAY_CAP, FAIR, asked], m, even_leave=True)
    assert out.ok and out.result.stage == 1
    t = out.result.table
    assert t["岡田"].count("有") == 2
    others = [t[s.name].count("有") for s in staff if s.name != "岡田"]
    assert max(others) - min(others) <= 1, others


def test_level_split():
    """合計を少ない人から同じ日数まで入れる。上限を守る。"""
    z = {n: 0 for n in "abcd"}
    big = {n: 31 for n in "abcd"}
    assert _level(0, big) == (z, z)
    assert _level(4, big) == ({n: 1 for n in "abcd"}, {n: 1 for n in "abcd"})
    assert _level(3, big) == (z, {n: 1 for n in "abcd"})
    lo, hi = _level(7, {**big, "a": 1})   # a は上限1。残り6日を3人で2日ずつ
    assert lo == {"a": 1, "b": 2, "c": 2, "d": 2} == hi


def test_at_least_gives_everyone_the_day_when_no_spare(nov):
    """余りが無い月でも、目安（at_least）の日数は全員に割る。日数を頼まれた人はそのままで、割る人に入れない。"""
    fac, staff, m, rules = nov
    fair = copy.deepcopy(FAIR)
    fair.params["at_least"] = 1
    table = {s.name: ["日"] * m.days for s in staff}
    asked = Rule(id="t-own", ver=1, name="岡田さん 有給消化", kind="count", flex="wish",
                 params={"shifts": ["有"], "min": 18, "max": 20}, who=["岡田"], scope="once")
    dated = [Rule(id=f"t-d{d}", ver=1, name=f"小川さん {d}日 有給", kind="cell", flex="wish", params={"in": ["有"]},
                  who=["小川"], when=[d], scope="once") for d in (12, 20)]   # 2本に分けて入れた日付の有給
    add, totals = shares(staff, fair_on([fair, asked, CAP0] + dated), m, table)
    assert totals == [len(staff) - 3]
    assert {r.who[0] for r in add} == {s.name for s in staff} - {"岡田", "斉藤", "小川"}
    assert all(r.params["min"] == r.params["max"] == 1 for r in add)



def test_longer_search_only_after_timeout(nov, monkeypatch):
    """長く探す施設は、時間切れの時だけ量を増やす。見つかればそこで止める。"""
    import shiftvoice.solver as S
    from shiftvoice.fairleave import EVEN_DTIME, even_out
    fac, staff, m, rules = nov
    rs = fair_on(copy.deepcopy(rules) + [DAY_CAP, FAIR])
    first = solve_month(fac, staff, rs, m)
    first.table = {n: list(row) for n, row in first.table.items()}
    first.table[staff[0].name][:3] = ["有", "有", "有"]
    asked = []

    def fake(*a, max_dtime=None, **k):
        asked.append(max_dtime)
        return S.SolveResult("timeout" if len(asked) < 3 else "ok", first.table)

    monkeypatch.setattr(S, "solve_month", fake)
    res, line = even_out(fac, staff, rs, m, first, max_dtime=300.0)
    assert asked[1:] == [EVEN_DTIME, 300.0]   # 最初の1回は余りを数える計算
    assert res.status == "ok" and line["status"] == "ok"


def test_weekday_people_become_heavy_when_hard_search_fails(nov, monkeypatch):
    """寄せる曜日を外せない形で見つからない時は、重い配慮の形で作る（作れないまま1回目の表へ戻らない）。"""
    import shiftvoice.solver as S
    from shiftvoice.fairleave import DAY_WEIGHT, even_out
    fac, staff, m, rules = nov
    fair = copy.deepcopy(FAIR)
    fair.params["at_least"] = 1
    wed = Rule(id="t-wed", ver=1, name="水曜は日勤を3人に寄せる", kind="coverage", flex="negotiable",
               params={"shifts": ["日", "研"], "scope": "all", "op": ">=", "n": 3, "weekdays": ["wed"]}, scope="rule")
    rs = fair_on(copy.deepcopy(rules) + [fair, wed])
    first = solve_month(fac, staff, rs, m)
    real, seen = S.solve_month, []

    def fake(fc, st, rl, mo, **k):
        keep = [r for r in rl if r.id == "t-wed-keep"]
        if not keep or k.get("max_dtime") == 20.0:   # 余りの計算はそのまま
            return real(fc, st, rl, mo, **k)
        seen.append((keep[0].flex, keep[0].weight))
        return S.SolveResult("ok", first.table) if keep[0].flex == "negotiable" else S.SolveResult("timeout", None)

    monkeypatch.setattr(S, "solve_month", fake)
    res, line = even_out(fac, staff, rs, m, first, max_dtime=300.0)
    assert seen[-1] == ("negotiable", DAY_WEIGHT) and seen[0][0] == "operational"
    assert line["status"] == "ok"


def test_only_fresh_builds_use_the_rule(nov, nov_result):
    """表を開いた後の直しでは働かない。公休が有給に変わらず、表にある有給は残る。"""
    from shiftvoice.resolve import resolve
    fac, staff, m, rules = nov
    base = {n: list(row) for n, row in nov_result.table.items()}
    free = [s.name for s in staff if not any(r.kind == "cell" and s.name in (r.who or []) for r in rules)]   # 撮影用の前提の無い人
    name, other = free[:2]
    d = next(i for i, v in enumerate(base[name]) if v == "日")
    base[name][d] = "有"
    pin = Rule(id="t-pin", ver=1, name="有給", kind="cell", flex="wish", params={"in": ["有"]}, who=[name], when=[d + 1], scope="once")
    day = next(i for i, v in enumerate(base[other]) if v == "日") + 1
    off = Rule(id="t-off", ver=1, name="休み", kind="cell", flex="wish", params={"in": ["公"]}, who=[other], when=[day], scope="once")
    r = resolve(fac, staff, copy.deepcopy(rules) + [FAIR, pin, off], m, base, [off])
    assert r.table[name][d] == "有"
    assert sum(row.count("有") for row in r.table.values()) == 1


def test_no_leave_word_after_opening(nov, nov_result):
    """表を開いた後の「〇〇さんは有給なしで」は、表の有給を残さない（carry_rules）。"""
    from shiftvoice.resolve import carry_rules
    fac, staff, m, rules = nov
    base = {n: list(row) for n, row in nov_result.table.items()}
    name = staff[0].name
    base[name][3] = "有"
    none = Rule(id="t-none", ver=1, name="有給なし", kind="count", flex="operational",
                params={"shifts": ["有"], "max": 0}, who=[name], scope="once")
    assert any(c.who == [name] for c in carry_rules(fac, base, rules))
    assert not any(c.who == [name] for c in carry_rules(fac, base, rules + [none]))


def test_quiet_does_not_count_the_rule(tmp_path):
    """なるべく平等に入れる有給は、静かなルールの候補に入らない（「今の名簿にいない人のルール」と言わない）。"""
    import shutil
    from pathlib import Path
    from shiftvoice.ledger import Ledger
    from shiftvoice.quiet import candidates
    from shiftvoice.store import Store
    d = tmp_path / "f"
    shutil.copytree(Path(__file__).resolve().parents[1] / "data" / "hidamari", d)
    st = Store(d)
    led = st.ledger()
    led.append("add", rule=FAIR.to_dict() if hasattr(FAIR, "to_dict") else {
        "id": "t-fair", "ver": 1, "name": FAIR.name, "kind": "count", "flex": "operational",
        "params": dict(FAIR.params), "who": None, "scope": "rule"}, op_id="t")
    st.save_ledger(led)
    assert "t-fair" not in candidates(st, 2026, 11)


def test_giveback_with_fair_rule_is_not_stuck(nov):
    """有給を平等に入れた月でも返す案が作れ、ほかの人の有給は動かない（前は「この条件では返せません」で翌月の表を採れなかった）。"""
    from shiftvoice.giveback import build_pair
    fac, staff, m, rules = nov
    gb = build_pair(fac, staff, copy.deepcopy(rules) + [DAY_CAP, FAIR], m, {"田村": 2})
    assert gb.status == "ok", (gb.message, gb.checks)
    assert gb.checks["3_others_leave_same"]
    p = gb.people[0]
    assert p["leave_after"] > p["leave_before"] or p["run_after"] < p["run_before"]


def test_last_month_short_people_come_first(nov):
    """割り切れない分は、先月の有給が少ない人に入れる（作者 2026-11-07）。"""
    fac, staff, m, rules = nov
    table = {s.name: ["日"] * m.days for s in staff}
    names = [s.name for s in staff]
    for n in names[:3]:
        table[n][0] = "有"
    last = {n: 1 for n in names}
    last[names[10]] = last[names[11]] = last[names[12]] = 0
    add, _ = shares(staff, fair_on([FAIR]), m, table, last=last)
    got = {r.who[0] for r in add if r.params.get("min") == 1}
    assert got == {names[10], names[11], names[12]}
    assert all(r.params["max"] == (1 if r.who[0] in got else 0) for r in add)


def test_room_keeps_the_weekday_people(nov):
    """目安の上に足す有給は、寄せる曜日の人数を食わない。寄せる曜日が増えると、足せる日数は減る。"""
    from shiftvoice.fairleave import room_total
    fac, staff, m, rules = nov
    fair = copy.deepcopy(FAIR)
    fair.params["at_least"] = 1
    floor = Rule(id="t-floor", ver=1, name="日勤は2人以上", kind="coverage", flex="operational",
                 params={"shifts": ["日", "研"], "scope": "all", "op": ">=", "n": 2}, scope="rule")
    base = fair_on(copy.deepcopy(rules) + [fair, floor])
    add, _ = shares(staff, base, m, {s.name: ["日"] * m.days for s in staff})
    free = room_total(fac, staff, base, m, add)
    wed = Rule(id="t-wed", ver=1, name="水曜は日勤を4人に寄せる", kind="coverage", flex="negotiable",
               params={"shifts": ["日", "研"], "scope": "all", "op": ">=", "n": 4, "weekdays": ["wed"]}, scope="rule")
    kept = room_total(fac, staff, base + [wed], m, add)
    assert free is not None and kept is not None and kept < free
