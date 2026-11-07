"""「早番も遅番も、可能な範囲で平均化して」。何回から何回とは決めず、人ごとの回数をできるだけそろえる（作者 2026-11-08
「月3〜4回にして、とは通常言わない。それで組めるか分からないから。『可能な範囲で平均化して』が通る方が良い」）。

- 読み取り（本物の Haiku で録った8つの言い回し）から、全員の「回数をそろえる」ルールになる。毎月か今月だけかは聞く
- 外れを持たない（守れなかった件に数えない）。前に足したルール（遅番の次の日の早番・遅番の3連続）を崩してまでならさない
- 数えるのはその勤務に入れる人だけ。名指しの回数のルールや本人の希望が先に効く
"""
import shutil

import pytest

from shiftvoice.build import month_rules
from shiftvoice.model import Facility, Month, Rule, ShiftType, Staff
from shiftvoice.quiet import candidates
from shiftvoice.resolve import resolve
from shiftvoice.rules import check_table
from shiftvoice.solver import solve_month

SCENE = [("遅番の次の日に早番はきついから、やめてあげて", "毎月"),
         ("みんな、遅番が3日も4日も続くのはやめて", "毎月")]
EVEN = "早番も遅番も、可能な範囲で平均化して"

PHRASES = {   # 本物の Haiku で3回ずつ読ませ、3回とも同じ読みだった（2026-11-08）
    "早番も遅番も、可能な範囲で平均化して": ["早", "遅"],
    "早番と遅番の回数を、できるだけみんな同じくらいにして": ["早", "遅"],
    "夜勤の回数をなるべく平等にして": ["夜"],
    "遅番が偏らないようにして": ["遅"],
    "早番をみんなで均等に分けて": ["早"],
    "可能な範囲で平均化して、早番と遅番": ["早", "遅"],
    "みんなの早番の回数、ばらつきすぎ。できるだけそろえて": ["早"],
    "遅番の回数に差がありすぎるから、ならしてほしい": ["遅"],
}


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


@pytest.mark.parametrize("text", list(PHRASES))
def test_phrase_becomes_even_rule_for_everyone(sess, text):
    """名前を言わない言い回しは全員のルール。「なるべく」「可能な範囲で」でも止まらず、聞き返しにも読み直しにも落ちない。
    直す前は読み取りの型に無く、読めない返事として止まった。"""
    r = sess.say(text)
    assert r.kind == "ask_period", r.message
    r = sess.answer(r.uid, "毎月")
    assert r.kind == "applied", r.message
    assert r.warning == ""
    assert sess.reader.reread_sent == []
    card = next(c for c in sess.state()["cards"] if c["id"] == r.rule_ids[0])
    assert card["kind"] == "even" and card["who"] is None and card["scope"] == "rule"
    assert card["params"]["shifts"] == PHRASES[text]
    assert card["broken"] == 0
    assert "できるだけそろえる" in card["name"] and card["name"].startswith("全員")


def test_scene_narrows_without_breaking_earlier_rules(sess):
    """動画の3つ目の場面。遅番の次の日の早番と、遅番の3連続をやめた後に平均化を頼む。早番・遅番の幅は狭まり、
    前の2つのルールは0か所のまま、ルールの外れは増えない。直す前は「まだ扱えません」から読み直しで回数を聞き返した。"""
    for text, ans in SCENE:
        r = sess.say(text)
        r = sess.answer(r.uid, ans)
        assert r.kind == "applied", (text, r.message)
    st = sess.store
    staff = st.staff_on(2026, 11)
    m = st.month(2026, 11)
    before = {sym: _spread(m.table, staff, sym) for sym in ("早", "遅")}
    rules = month_rules(st, 2026, 11, with_premise=False)
    broken_before = check_table(rules, st.facility(), staff, m, m.table)
    assert _late_then_early(m.table) == 0 and _late_runs3(m.table, m) == 0
    assert before["早"][1] - before["早"][0] >= 4 and before["遅"][1] - before["遅"][0] >= 4   # 2〜7回・1〜6回

    r = sess.say(EVEN)
    r = sess.answer(r.uid, "毎月")
    assert r.kind == "applied", r.message
    m = st.month(2026, 11)
    for sym in ("早", "遅"):
        lo, hi = _spread(m.table, staff, sym)
        assert hi - lo <= 1, (sym, lo, hi)   # 16人で月60回なので、3〜4回
    assert _late_then_early(m.table) == 0
    assert _late_runs3(m.table, m) == 0
    rules = month_rules(st, 2026, 11, with_premise=False)
    assert len(check_table(rules, st.facility(), staff, m, m.table)) <= len(broken_before)
    for name, row in m.table.items():   # 公休9日・夜勤3〜5回（入れる人）は動かせない・運用の都合のまま
        assert row.count("公") == 9
    assert all(3 <= m.table[s.name].count("夜") <= 5 for s in staff if "夜" in s.can)


def test_even_rule_is_not_a_cleanup_or_quiet_candidate(sess):
    """場面を持たないので、当てはまる場面が無いことを理由に片付け・静かなルールの札にしない。"""
    r = sess.answer(sess.say(EVEN).uid, "毎月")
    rid = r.rule_ids[0]
    assert rid not in candidates(sess.store, 2026, 11)
    view = sess.cleanup()
    assert all(rid not in str(g) for g in view["groups"].values())


def test_rule_detail_counts_only_people_who_can_work_it(sess):
    """夜勤に入れない人（杉山さん。12月から入れる）を0回として数えない。"""
    r = sess.answer(sess.say("夜勤の回数をなるべく平等にして").uid, "毎月")
    d = sess.rule_detail(r.rule_ids[0])
    staff = sess.store.staff_on(2026, 11)
    n = sum(1 for s in staff if "夜" in s.can)
    assert n == len(staff) - 1
    assert any(f"入れる{n}人" in x for x in d["now"]), d
    assert not any("0〜" in x for x in d["now"]), d


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


def test_named_people_only(sess):
    """人を言えば、その人たちだけをならす（ルールの人は名指しの2人）。"""
    from shiftvoice.reader import RecordedReader
    sess.reader = RecordedReader({"職員09さんと職員10さんの早番を同じくらいにして":
                                  '{"items":[{"type":"even_out","staff":["職員09","職員10"],"shift":["早"],"reason":null}]}'})
    r = sess.say("菊池さんと久保さんの早番を同じくらいにして")
    r = sess.answer(r.uid, "毎月")
    assert r.kind == "applied", r.message
    card = next(c for c in sess.state()["cards"] if c["id"] == r.rule_ids[0])
    assert card["who"] == ["菊池", "久保"] and card["kind"] == "even"
    t = sess.store.month(2026, 11).table
    assert abs(t["菊池"].count("早") - t["久保"].count("早")) <= 1


def test_rest_days_are_not_evened(sess):
    """休みの日数は数で決まる（公休9日など）ので、そろえる頼みにしない。止まって表もルールも変えない。"""
    from shiftvoice.reader import RecordedReader
    sess.reader = RecordedReader({"公休をみんな同じくらいにして":
                                  '{"items":[{"type":"even_out","staff":null,"shift":["公"],"reason":null}]}'})
    before = sess.store.month(2026, 11).table
    r = sess.say("公休をみんな同じくらいにして")
    assert r.kind in ("ask", "error")
    assert sess.store.month(2026, 11).table == before
    assert not any(c["kind"] == "even" for c in sess.state()["cards"])


def test_named_people_are_not_pulled_down(sess):
    """名指しの2人（菊池さん6回・久保さん4回）をならしても、2人の早番は合わせて減らず、少ない方の回数より減らない。
    回数そのものを罰すると、2人とも1回まで下がる。"""
    from shiftvoice.reader import RecordedReader
    sess.reader = RecordedReader({"職員09さんと職員10さんの早番を同じくらいにして":
                                  '{"items":[{"type":"even_out","staff":["職員09","職員10"],"shift":["早"],"reason":null}]}'})
    pair = ("菊池", "久保")
    t = sess.store.month(2026, 11).table
    before = {n: t[n].count("早") for n in pair}
    assert before == {"菊池": 6, "久保": 4}
    r = sess.answer(sess.say("菊池さんと久保さんの早番を同じくらいにして").uid, "毎月")
    assert r.kind == "applied", r.message
    t = sess.store.month(2026, 11).table
    after = {n: t[n].count("早") for n in pair}
    assert sum(after.values()) >= sum(before.values()), (before, after)
    assert min(after.values()) >= min(before.values()), (before, after)
    assert abs(after["菊池"] - after["久保"]) <= 1, after


@pytest.fixture(scope="module")
def imported():
    """取り込んだ施設の形。人数のルールが「下限は動かせない・上限は相談できる」の幅なので、日ごとの人数が決まらない。
    前の表は、早番が2人の日が多い（取り込んだ勤務表のように）。"""
    names = NAMES + ["川口", "木下"]
    fac, _, _, _ = _small()
    staff = [Staff(n, "東", ["早", "日", "遅"]) for n in names]
    month = Month(2026, 11, [], {n: ["日"] * 5 for n in names})
    rules = [Rule("o1", 1, "公休の数", "count", "fixed", {"shifts": ["公"], "min": 9, "max": 9})]
    for i, (sym, hi) in enumerate((("早", 2), ("日", 3), ("遅", 2))):
        rules += [Rule(f"c{i}", 1, "人数（下限）", "coverage", "fixed", {"shifts": [sym], "scope": "all", "op": ">=", "n": 1}),
                  Rule(f"c{i}b", 1, "人数（上限）", "coverage", "negotiable",
                       {"shifts": [sym], "scope": "all", "op": "<=", "n": hi})]
    lean = Rule("x", 1, "早番を2人に寄せる", "coverage", "fixed", {"shifts": ["早"], "scope": "all", "op": "==", "n": 2})
    res = solve_month(fac, staff, rules + [lean], month)
    assert res.status == "ok"
    return fac, staff, month, rules, res.table


@pytest.mark.parametrize("shifts", [["早"], ["早", "遅"], ["日"]])
def test_imported_facility_keeps_totals(imported, shifts):
    """人数に幅がある施設でならしても、ならした勤務の総数は前の表と同じで、人の間で移すだけ。"""
    fac, staff, month, rules, base = imported
    ev = _even_rule(shifts)
    rr = resolve(fac, staff, rules + [ev], month, base, [ev])
    assert rr.ok
    for sym in shifts:
        assert _spread(base, staff, sym)[1] - _spread(base, staff, sym)[0] >= 3, sym
        total = [sum(row.count(sym) for row in t.values()) for t in (base, rr.table)]
        assert total[1] == total[0], (sym, total)
        lo, hi = _spread(rr.table, staff, sym)
        assert hi - lo <= 1, (sym, lo, hi)
    assert check_table(rules, fac, staff, month, rr.table) == []


# ---- ならした後に、総数を減らす回数の頼みを足す

def _two_with_fixed_partner():
    """早番に入れるのは2人だけで、人数のルールが無いので早番の総数は減らせる。井上さんは早番ちょうど10回（動かせない）。
    前の表は2人とも早番10回。"""
    fac, _, _, _ = _small()
    staff = [Staff(n, "東", ["早", "日", "遅"]) for n in ("赤坂", "井上")]
    month = Month(2026, 11, [], {n: ["日"] * 5 for n in ("赤坂", "井上")})
    rules = [Rule("o1", 1, "公休の数", "count", "fixed", {"shifts": ["公"], "min": 8, "max": 8}),
             Rule("b10", 1, "井上さん 早番は月10回", "count", "fixed", {"shifts": ["早"], "min": 10, "max": 10}, who=["井上"])]
    row = ["早"] * 10 + ["公"] * 8 + ["日"] * 12
    base = {"赤坂": list(row), "井上": list(row)}
    ask = Rule("a2", 1, "赤坂さん 早番はできれば月2回まで", "count", "negotiable", {"shifts": ["早"], "max": 2}, who=["赤坂"])
    return fac, staff, month, rules, base, ask


@pytest.mark.parametrize("with_even", [False, True])
def test_later_count_request_that_lowers_total_is_kept(with_even):
    """ならすルールがある月に「赤坂さんはできれば早番2回まで」と言うと、赤坂さんは2回になる。井上さんへは移せないので
    早番の総数は20回から12回に減るが、それは頼みで減る分なので罰さない。直す前は、総数を前の表に近づける罰（8回で12000）が
    相談できるルール1件（5000）より重く、ならすルールがある時だけ赤坂さんが10回のまま頼みが外れた。"""
    fac, staff, month, rules, base, ask = _two_with_fixed_partner()
    rs = rules + ([_even_rule()] if with_even else []) + [ask]
    rr = resolve(fac, staff, rs, month, base, [ask])
    assert rr.ok
    t = rr.table
    assert t["赤坂"].count("早") == 2, t["赤坂"]
    assert t["井上"].count("早") == 10
    assert check_table(rs, fac, staff, month, t) == []


def test_count_rule_everyone_keeps_does_not_drop_the_total_guard(imported):
    """全員の回数のルール（早番・遅番は月20回まで。前の表で全員が守っている）があっても、ならした勤務の総数は前の表のまま。
    回数のルールがあるだけの人まで総数から外すと、帯の上の人の勤務を移さずに消して、総数が減る。"""
    fac, staff, month, rules, base = imported
    shifts = ["早", "遅"]
    caps = [Rule(f"cap{sym}", 1, f"全員 {sym}は月20回まで", "count", "negotiable", {"shifts": [sym], "max": 20}) for sym in shifts]
    assert check_table(caps, fac, staff, month, base) == []
    ev = _even_rule(shifts)
    rr = resolve(fac, staff, rules + caps + [ev], month, base, [ev])
    assert rr.ok
    for sym in shifts:
        total = [sum(row.count(sym) for row in t.values()) for t in (base, rr.table)]
        assert total[1] == total[0], (sym, total)
        lo, hi = _spread(rr.table, staff, sym)
        assert hi - lo <= 1, (sym, lo, hi)


def test_later_count_request_moves_to_others_when_it_can():
    """「赤坂さんはできれば早番5回まで」で減らす分は、井上さんへ移せるなら移す（早番の総数12回のまま）。
    頼みを破っている人を総数の罰から丸ごと外すと、移した先の増えた分だけが罰され、移すより消す方が安くなって
    総数が5回まで減った（Codex Astra の敵対レビュー。起きやすさ4×影響7）。"""
    fac, _, _, _ = _small()
    staff = [Staff(n, "東", ["早", "日", "遅"]) for n in ("赤坂", "井上")]
    month = Month(2026, 11, [], {n: ["日"] * 5 for n in ("赤坂", "井上")})
    rules = [Rule("o1", 1, "公休の数", "count", "fixed", {"shifts": ["公"], "min": 8, "max": 8})]
    base = {"赤坂": ["早"] * 10 + ["公"] * 8 + ["日"] * 12, "井上": ["早"] * 2 + ["公"] * 8 + ["日"] * 20}
    ask = Rule("a5", 1, "赤坂さん 早番はできれば月5回まで", "count", "negotiable", {"shifts": ["早"], "max": 5}, who=["赤坂"])
    rs = rules + [_even_rule()] + [ask]
    rr = resolve(fac, staff, rs, month, base, [ask])
    assert rr.ok
    t = rr.table
    assert t["赤坂"].count("早") <= 5, t["赤坂"]
    assert t["赤坂"].count("早") + t["井上"].count("早") == 12, t
    assert check_table(rs, fac, staff, month, t) == []


def test_later_count_request_raises_someone_from_zero():
    """ならすルールがある月に、早番0回の赤坂さんへ「できれば早番6回以上」と言うと、赤坂さんは6回になり、ほかの人の3回は
    減らない。直す前は、軽くする幅を前の回数で数えていたので0回の人は幅が0で、ほかの人から2回ずつしか移せず
    [2,3,3,2,2] で頼みが外れた（Astra 2026-11-08）。"""
    fac, _, _, _ = _small()
    staff = [Staff(n, "東", ["早", "日", "遅"]) for n in NAMES]
    month = Month(2026, 11, [], {n: ["日"] * 5 for n in NAMES})
    base = {n: (["日"] * 30 if n == "赤坂" else ["早"] * 3 + ["日"] * 27) for n in NAMES}
    ask = Rule("a6", 1, "赤坂さん 早番はできれば月6回以上", "count", "negotiable", {"shifts": ["早"], "min": 6}, who=["赤坂"])
    rs = [_even_rule(), ask]
    rr = resolve(fac, staff, rs, month, base, [ask])
    assert rr.ok
    t = rr.table
    assert t["赤坂"].count("早") >= 6, [t[n].count("早") for n in NAMES]
    assert [t[n].count("早") for n in NAMES[1:]] == [3] * 4, [t[n].count("早") for n in NAMES]
    assert check_table(rs, fac, staff, month, t) == []
