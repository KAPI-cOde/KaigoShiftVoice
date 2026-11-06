"""ユニットを標準にする試験。ユニットごとに違う人数（東の早番は毎日2人・西と南は1人）、施設全体の合計、
表の下の日ごとの人数の行。施設と職員はすべて作り話（3ユニット 東6人・西5人・南5人、名前は職員001 の形）。
直す前は、早番の人数が「各ユニットで1人以上・2人まで」の1本にまとまって東の2人が消え、12月の東の早番は31日中11日が
1人になり、運用の都合を外す段まで下がっていた（2026-11-06 の確かめ、場面6）。"""
from collections import Counter
from pathlib import Path

from shiftvoice.build import build_original, solve_staged
from shiftvoice.daycount import day_counts
from shiftvoice.intake import to_proposal
from shiftvoice.model import Facility, Month, Rule
from shiftvoice.onboard import create_facility, infer, parse_paste
from shiftvoice.rules import check_table
from shiftvoice.store import Store

U = {"東": [f"職員{i:03d}" for i in range(1, 7)],
     "西": [f"職員{i:03d}" for i in range(7, 12)],
     "南": [f"職員{i:03d}" for i in range(12, 17)]}
# 作り話の11月の表（30日）。早番は東だけ毎日2人・西と南は1人、遅番は各ユニット毎日1人、夜勤は施設全体で毎晩2人、
# 公休は全員8日、遅番の次の日に早番を置かない。このアプリの計算で作った物を貼り付けの形にした
PASTE = Path(__file__).parent / "fixtures" / "pastes" / "units3.tsv"


def paste(units=("東", "西", "南")) -> str:
    out, keep = [], True
    for line in PASTE.read_text(encoding="utf-8").splitlines():
        if line.endswith("ユニット"):
            keep = line[:-4] in units
        if keep:
            out.append(line)
    return "\n".join(out)


def made(tmp_path):
    p = parse_paste(paste())
    assert p.unit_list == ["東", "西", "南"]
    pr = infer(p, 2026, 11)
    c = create_facility(pr, tmp_path / "f", name="試験", next_year=2026, next_month=12)
    return pr, c, Store(tmp_path / "f")


def test_取り込みでユニットごとに違う人数をユニットごとのルールで持つ(tmp_path):
    pr = infer(parse_paste(paste()), 2026, 11)
    early = [r for r in pr.rules if r["kind"] == "coverage" and r["params"]["shifts"] == ["早"]]
    got = sorted((tuple(r["params"].get("units") or []), r["params"]["op"], r["params"]["n"]) for r in early)
    assert got == [(("東",), "==", 2), (("西", "南"), "==", 1)]
    # どのユニットも同じ人数の勤務（遅番・夜勤）は、今までどおり全ユニット向けの1本
    late = [r for r in pr.rules if r["kind"] == "coverage" and r["params"]["shifts"] == ["遅"]]
    assert len(late) == 1 and "units" not in late[0]["params"] and late[0]["params"]["scope"] == "each_unit"


def test_3ユニットの人数の違う施設で翌月を作るとルールの外れが0で段も下がらない(tmp_path):
    pr, c, st = made(tmp_path)
    assert c.next_table_made and c.stage_note is None
    tab = st.month(2026, 12).table
    for u, names in U.items():
        per_day = Counter(sum(1 for n in names if tab[n][d] == "早") for d in range(31))
        assert per_day == Counter({2 if u == "東" else 1: 31}), (u, per_day)
    res = build_original(st, 2026, 12)
    assert res.ok and [s["stage"] for s in res.stages] == [1]
    from shiftvoice.build import month_rules
    assert check_table(month_rules(st, 2026, 12), st.facility(), st.staff_on(2026, 12), st.month(2026, 12), tab) == []


def _fac3() -> Facility:
    return Facility.from_dict({"id": "t", "name": "試験", "units": ["東", "西", "南"], "settings": {}, "shift_types": [
        {"symbol": "早", "name": "早番", "kind": "work"}, {"symbol": "日", "name": "日勤", "kind": "work"},
        {"symbol": "遅", "name": "遅番", "kind": "work"}, {"symbol": "夜", "name": "夜勤", "kind": "work", "next_is_ake": True},
        {"symbol": "明", "name": "明け", "kind": "ake"}, {"symbol": "公", "name": "公休", "kind": "rest"}]})


def _one(text, items, rules=None):
    from shiftvoice.model import Staff
    staff = [Staff(n, u, ["早", "日", "遅", "夜"]) for u, ns in U.items() for n in ns]
    m = Month(2026, 12, [], {})
    return to_proposal(items, text, _fac3(), staff, m, rules=rules or []).rules


def test_一言でユニットを言うとそのユニットだけの人数になる():
    general = Rule("r1", 1, "早番の人数", "coverage", "fixed", {"shifts": ["早"], "scope": "each_unit", "op": "==", "n": 1})
    it = [{"type": "coverage", "shift": "早", "min": 2, "max": 2, "weekdays": None, "strength": "operational"}]
    [r] = _one("東ユニットは早番を毎日2人にして", it, [general])
    assert r["params"] == {"shifts": ["早"], "scope": "each_unit", "op": "==", "n": 2, "units": ["東"]}
    assert "（東）" in r["name"]
    # 施設全体と言ったら施設全体の人数（ユニットごとのルールとは別に持つ）
    it3 = [{"type": "coverage", "shift": "早", "min": 4, "max": None, "weekdays": None, "strength": "operational"}]
    [w] = _one("施設全体で早番は毎日4人以上", it3, [general])
    assert w["params"]["scope"] == "all" and "units" not in w["params"]
    # ユニットのある施設で、今のルールが無い勤務は、聞き返さずにユニットごと（標準はユニット）
    [e] = _one("日勤は毎日1人", [{"type": "coverage", "shift": "日", "min": 1, "max": 1, "weekdays": None}])
    assert e["params"]["scope"] == "each_unit"


def test_ユニットごとに違う人数と施設全体の合計を一緒に守る():
    """全ユニット向けの「早番は各ユニットで1人」に、東だけの「早番2人」が優先し、施設全体の「早番4人以上」と一緒に守る。"""
    from shiftvoice.model import Staff
    fac = _fac3()
    staff = [Staff(n, u, ["早", "日", "遅", "夜"]) for u, ns in U.items() for n in ns]
    m = Month(2026, 12, [], {})
    rules = [
        Rule("r1", 1, "早番の人数", "coverage", "fixed", {"shifts": ["早"], "scope": "each_unit", "op": "==", "n": 1}),
        Rule("r2", 1, "早番の人数（東）", "coverage", "fixed",
             {"shifts": ["早"], "scope": "each_unit", "op": "==", "n": 2, "units": ["東"]}),
        Rule("r3", 1, "早番の人数（施設全体）", "coverage", "fixed", {"shifts": ["早"], "scope": "all", "op": ">=", "n": 4}),
        Rule("r4", 1, "遅番の人数", "coverage", "fixed", {"shifts": ["遅"], "scope": "each_unit", "op": "==", "n": 1}),
        Rule("r5", 1, "夜勤の人数", "coverage", "fixed", {"shifts": ["夜"], "scope": "each_unit", "op": "==", "n": 1}),
    ]
    res = solve_staged(fac, staff, rules, m)
    assert res.ok and [s["stage"] for s in res.stages] == [1]
    tab = res.result.table
    for d in range(31):
        east = sum(1 for n in U["東"] if tab[n][d] == "早")
        assert east == 2
        assert all(sum(1 for n in U[u] if tab[n][d] == "早") == 1 for u in ("西", "南"))
    assert check_table(rules, fac, staff, m, tab) == []
    # 施設全体の合計が、ユニットごとの合計と合わない時（早番5人以上）は、表を作れないか段を下げる＝黙って片方を捨てない
    rules[2] = Rule("r3", 1, "早番の人数（施設全体）", "coverage", "fixed", {"shifts": ["早"], "scope": "all", "op": ">=", "n": 5})
    res2 = solve_staged(fac, staff, rules, m)
    assert not res2.ok or [s["stage"] for s in res2.stages] != [1]


def test_表の下の日ごとの人数の行はユニットごとと施設全体で足りない日に印が付く(tmp_path):
    _pr, _c, st = made(tmp_path)
    m = st.month(2026, 12)
    from shiftvoice.build import month_rules
    rules = month_rules(st, 2026, 12, with_premise=False)
    rows = day_counts(st.facility(), st.staff_on(2026, 12), m, rules)
    east = next(r for r in rows if r["unit"] == "東" and r["symbol"] == "早")
    assert east["counts"] == [2] * 31 and east["short"] == []
    whole = next(r for r in rows if r["unit"] is None and r["symbol"] == "早")
    assert whole["counts"] == [4] * 31
    # 東の早番を1日だけ公休にすると、東の早番の行のその日に「足りない」の印
    n = next(x for x in U["東"] if m.table[x][4] == "早")
    m.table[n][4] = "公"
    rows = day_counts(st.facility(), st.staff_on(2026, 12), m, rules)
    east = next(r for r in rows if r["unit"] == "東" and r["symbol"] == "早")
    assert east["counts"][4] == 1 and east["short"] == [5]
    assert next(r for r in rows if r["unit"] == "西" and r["symbol"] == "早")["short"] == []


def test_ユニットが1つの施設は施設全体の行だけ(tmp_path):
    """見出しの無い表（ユニットなし）は1ユニットの扱い。ユニットを名指ししたルールは作らず、人数の行は施設全体だけ。"""
    p = parse_paste("\n".join(x for x in paste(("東",)).splitlines() if not x.endswith("ユニット")))
    assert p.unit_list in ([], ["全体"])
    pr = infer(p, 2026, 11)
    assert all("units" not in x["params"] and x["params"]["scope"] == "all" for x in pr.rules if x["kind"] == "coverage")
    c = create_facility(pr, tmp_path / "f", name="試験", next_year=2026, next_month=12)
    assert c.next_table_made
    st = Store(tmp_path / "f")
    from shiftvoice.build import month_rules
    rows = day_counts(st.facility(), st.staff_on(2026, 12), st.month(2026, 12), month_rules(st, 2026, 12))
    assert rows and all(x["unit"] is None for x in rows)
