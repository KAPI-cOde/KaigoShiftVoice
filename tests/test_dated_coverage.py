"""日付を決めた人数（「28日は行事なので早番を3人」「1日はU2の早番はいなくていい」）を、その日だけのルールにする。
実際の施設の10月を画面の口で作った通しの試し（2026-11-07）で、施設が設けていた日付の例外（ユニットの早番を0人・2人に
する日）を言えず、全部のルールで作ると段4まで下がって希望休が51日中41日しか守れなかった。前の作り（f0f5141）は Astra の
5回目のレビューで穴が6つ見つかって取り消した（5de8031）。その6つを先に試験にしてから作り直した（どれも直す前は落ちる）。

その日は、同じ勤務の毎日・曜日の人数のルールが、日付のルールと両立しない側だけ、日付のルールの数までゆるむ（外しきらない）。
名前は作り話。API は呼ばない。"""
from __future__ import annotations

from dataclasses import replace

import pytest

from shiftvoice.model import Facility, Month, Rule, ShiftType, Staff
from shiftvoice.rules import expand, with_overrides
from test_spoken_rules_misread import _say, made  # noqa: F401  （作り話の2ユニット10人の施設の12月）

FAC = Facility("x", "x", ["U1", "U2"], [ShiftType("早", "早番", "work"), ShiftType("公", "公休", "rest")], {})
STAFF = [Staff(f"職員{i:02d}", "U1" if i <= 3 else "U2", ["早"]) for i in range(1, 7)]
NOV = Month(2026, 11, [], {})   # 2026年11月1日は日曜


def cov(rid: str, flex: str, op: str, n: int, *, scope: str = "each_unit", units=None, when=None, weekdays=None) -> Rule:
    p = {"shifts": ["早"], "scope": scope, "op": op, "n": n}
    if units:
        p["units"] = units
    if weekdays:
        p.update(weekdays=weekdays, holidays=False)
    return Rule(rid, 1, rid, "coverage", flex, p, when=when, scope="once" if when else "rule")


def bounds(rules: list[Rule], rid: str) -> dict[str, tuple]:
    """with_overrides の後のルール rid の、場面（"U1/1"・"全体/28"）ごとの (下限, 上限)。"""
    eff = next(r for r in with_overrides(rules) if r.id == rid)
    return {i.key: (i.lo, i.hi) for i in expand(eff, FAC, STAFF, NOV)}


def early_by_day(rules: list[Rule], month: Month = NOV) -> dict[int, dict[str, int]]:
    """全部のルールを動かせない強さで解いた表の、日ごと・ユニットごとの早番の人数。"""
    import shiftvoice.solver as S
    res = S.solve_month(FAC, STAFF, [replace(r, flex="fixed") for r in rules], month, stage=4)
    assert res.status == "ok", res.status
    unit = {s.name: s.unit for s in STAFF}
    out: dict[int, dict[str, int]] = {}
    for name, row in res.table.items():
        for d, c in enumerate(row, 1):
            if c == "早":
                out.setdefault(d, {"U1": 0, "U2": 0})[unit[name]] += 1
    return {d: out.get(d, {"U1": 0, "U2": 0}) for d in range(1, month.days + 1)}


# ---- Astra の穴1 別のユニットの日付の人数で、そのユニットでない人数まで外れた ----

def test_穴1_別のユニットの日付の人数では_ほかのユニットの名指しの人数はゆるまない():
    """毎日「U1の早番1人」に「1日はU2の早番0人」を足すと、U1の1日の下限まで消えて0人を許した（3×9）"""
    u1 = cov("u1", "fixed", "==", 1, units=["U1"])
    none = cov("d1", "operational", "==", 0, units=["U2"], when=[1])
    assert bounds([u1, none], "u1")["U1/1"] == (1, 1)


def test_穴1_全ユニットの毎日の人数は_名指しのユニットのその日だけゆるむ():
    daily = cov("r1", "fixed", "==", 1)
    none = cov("d1", "operational", "<=", 0, units=["U2"], when=[1])
    two = cov("d28", "operational", "==", 2, units=["U1"], when=[28])
    b = bounds([daily, none, two], "r1")
    assert b["U1/1"] == (1, 1) and b["U2/2"] == (1, 1) and b["U1/27"] == (1, 1)
    assert b["U2/1"][0] in (None, 0) and b["U2/1"][1] == 1      # U2 の1日は下限だけゆるむ
    assert b["U1/28"] == (1, 2) and b["U2/28"] == (1, 1)        # U1 の28日は上限が2人までゆるむ（外しきらない）


# ---- 穴2 上限だけの日付の人数で、両立する施設全体の下限まで消えた ----

def test_穴2_両立する施設全体の下限は残る():
    """毎日「施設全体の早番2人以上（動かせない）」に「28日は施設全体で3人まで」を足すと、28日の2人以上が消えた（3×9）"""
    lo = cov("lo", "fixed", ">=", 2, scope="all")
    cap = cov("d28", "operational", "<=", 3, scope="all", when=[28])
    assert bounds([lo, cap], "lo")["全体/28"] == (2, None)


def test_穴2_両立しない時は日付の数までゆるむ():
    lo = cov("lo", "fixed", ">=", 2, scope="all")
    cap = cov("d28", "operational", "<=", 1, scope="all", when=[28])
    assert bounds([lo, cap], "lo")["全体/28"] == (1, None)   # 「1人まで」の日は、下限を1人まで下げる（外しきらない）
    assert bounds([lo, cap], "lo")["全体/27"] == (2, None)


# ---- 穴3 施設全体をちょうど減らす頼みが作れなかった ----

def test_穴3_施設全体をちょうど1人にする日は作れる():
    """2ユニットで毎日各1人（動かせない）、「1日は施設全体の早番をちょうど1人」。各ユニット1人以上とぶつかり作れなかった（4×7）"""
    daily = cov("r1", "fixed", "==", 1)
    one = cov("d1", "operational", "==", 1, scope="all", when=[1])
    got = early_by_day([daily, one])
    assert sum(got[1].values()) == 1
    assert all(got[d] == {"U1": 1, "U2": 1} for d in range(2, 31))


def test_穴3_ゆるめても施設全体の下限は毎日のルールの強さで残る():
    """日付のルールが外れても（運用の都合を外す段）、その日の施設全体が0人にならない。毎日のルールの強さのまま、
    日付の数を施設全体の下限に持つ"""
    daily = cov("r1", "fixed", "==", 1)
    one = cov("d1", "operational", "==", 1, scope="all", when=[1])
    eff = next(r for r in with_overrides([daily, one]) if r.id == "r1")
    day1 = [i for i in expand(eff, FAC, STAFF, NOV) if i.key.endswith("/1")]
    assert any(i.lo == 1 and {t.staff for t in i.terms} == {s.name for s in STAFF} for i in day1)
    assert all(i.lo in (None, 0) for i in day1 if i.key in ("U1/1", "U2/1"))


# ---- 穴4 曜日の人数が、その日の人数を逆にゆるめた ----

def test_穴4_曜日の人数は日付の人数をゆるめない():
    """毎週日曜は各ユニット早番2人、11月1日（日）だけ各1人。日付の上限1人が消え、各2人の表で通った（3×8）"""
    daily = cov("r1", "fixed", "==", 1)
    sun = cov("sun", "operational", "==", 2, weekdays=["sun"])
    one = cov("d1", "operational", "==", 1, when=[1])
    assert bounds([daily, sun, one], "d1")["U1/1"] == (1, 1)
    got = early_by_day([daily, sun, one])
    assert got[1] == {"U1": 1, "U2": 1}
    assert got[8] == {"U1": 2, "U2": 2}       # ほかの日曜は2人のまま


# ---- 穴5・6 一言の読み方（作り話の2ユニット10人の施設の12月） ----

def test_日付の人数はその日だけ_今月だけ(made, tmp_path):
    from shiftvoice.build import month_rules
    from shiftvoice.store import Store
    import shiftvoice.solver as S
    r, new = _say(made, tmp_path, "28日は行事なので、早番を3人にして", [{"type": "coverage", "shift": "早", "min": 3, "max": None}])
    assert r.kind == "applied", r.message
    assert [(x.when, x.params["scope"], x.params["op"], x.params["n"], x.scope) for x in new] == [([28], "all", ">=", 3, "once")]
    assert "28日" in new[0].name and "毎日" not in new[0].name
    st = Store(tmp_path / "f")
    fac, staff, m = st.facility(), st.staff_on(2026, 12), st.month(2026, 12)
    fixed = [x for x in month_rules(st, 2026, 12, with_premise=False) if x.flex == "fixed" and x.id != new[0].id]
    res = S.solve_month(fac, staff, fixed + [replace(new[0], flex="fixed")], m, stage=4)
    assert res.status == "ok"
    assert sum(1 for row in res.table.values() if row[27] == "早") >= 3
    assert all(sum(1 for row in res.table.values() if row[d] == "早") == 2 for d in range(31) if d != 27)


def test_ユニットを言った日付の人数(made, tmp_path):
    r, new = _say(made, tmp_path, "1日はU2の早番はいなくていい", [{"type": "coverage", "shift": "早", "min": None, "max": 0}])
    assert r.kind == "applied", r.message
    assert [(x.when, x.params.get("units"), x.params["op"], x.params["n"]) for x in new] == [([1], ["U2"], "<=", 0)]


def test_穴5_何日までの人数は期間にせず聞き返す(made, tmp_path):
    """「今月は28日まで早番3人」が28日だけの人数になった（4×7）"""
    r, new = _say(made, tmp_path, "今月は28日まで早番3人", [{"type": "coverage", "shift": "早", "min": 3, "max": 3}])
    assert r.kind != "applied" and new == []


def test_穴5_何日から何日までは_その間の毎日(made, tmp_path):
    """「1日から5日まで早番3人」が1日と5日だけになった（4×7）"""
    r, new = _say(made, tmp_path, "1日から5日まで早番を3人", [{"type": "coverage", "shift": "早", "min": 3, "max": None}])
    assert r.kind == "applied", r.message
    assert [x.when for x in new] == [[1, 2, 3, 4, 5]]


def test_穴5_月に何日の人数は日付にしない(made, tmp_path):
    """「早番は月に10日だけ3人」が10日の人数になった"""
    r, new = _say(made, tmp_path, "早番は月に10日だけ3人", [{"type": "coverage", "shift": "早", "min": 3, "max": 3}])
    assert r.kind != "applied" and new == []


def test_穴6_人数の頼みが2つある一言は日付を混ぜずに聞き返す(made, tmp_path):
    """「28日は早番3人、29日は夜勤1人」で早番も夜勤も両日になった（4×8）"""
    r, new = _say(made, tmp_path, "28日は早番3人、29日は夜勤1人", [
        {"type": "coverage", "shift": "早", "min": 3, "max": None},
        {"type": "coverage", "shift": "夜", "min": 1, "max": 1}])
    assert r.kind != "applied" and new == []
    (tmp_path / "b").mkdir()
    r, new = _say(made, tmp_path / "b", "早番は毎日2人、夜勤は28日だけ1人", [
        {"type": "coverage", "shift": "早", "min": 2, "max": 2},
        {"type": "coverage", "shift": "夜", "min": 1, "max": 1}])
    assert r.kind != "applied" and new == []


def test_穴6_言い直した日付は混ぜない(made, tmp_path):
    """「14日、あ、違う、18日は早番3人」が14日と18日の両方になった"""
    r, new = _say(made, tmp_path, "14日、あ、違う、18日は早番3人", [{"type": "coverage", "shift": "早", "min": 3, "max": None}])
    assert all(14 not in (x.when or []) for x in new)
    assert r.kind != "applied" or [x.when for x in new] == [[18]]


def test_2つの日付を並べた人数は両方の日(made, tmp_path):
    r, new = _say(made, tmp_path, "24日と25日は早番を3人", [{"type": "coverage", "shift": "早", "min": 3, "max": None}])
    assert r.kind == "applied", r.message
    assert [x.when for x in new] == [[24, 25]]


def test_日付と合う曜日は日付だけにする(made, tmp_path):
    """2026年12月27日は日曜。「27日の日曜は早番3人」は27日だけ（毎週日曜にしない）。曜日が合わない時は聞き返す"""
    r, new = _say(made, tmp_path, "27日の日曜は早番3人", [{"type": "coverage", "shift": "早", "min": 3, "max": None,
                                                         "weekdays": ["sun"]}])
    assert r.kind == "applied", r.message
    assert [(x.when, x.params.get("weekdays")) for x in new] == [([27], None)]
    (tmp_path / "b").mkdir()
    r, new = _say(made, tmp_path / "b", "28日の日曜は早番3人", [{"type": "coverage", "shift": "早", "min": 3, "max": None,
                                                               "weekdays": ["sun"]}])
    assert r.kind != "applied" and new == []


def test_日付の人数どうしは新しい方が勝つ():
    old = cov("a", "operational", "==", 2, units=["U1"], when=[28])
    new = cov("b", "operational", "==", 3, units=["U1"], when=[28])
    assert bounds([old, new], "a")["U1/28"] == (2, 3)


# ---- Astra の2回目（2026-11-07 朝）の指摘 ----

def test_上限だけの日付の人数でも_下限は日付の数までで0人を通さない():
    """毎日「施設全体で早番2人以上」に「1日は早番を1人まで」。1日の下限が消え、0人の表が違反なしになった（4×9）"""
    from shiftvoice.rules import check_table
    lo = cov("lo", "fixed", ">=", 2, scope="all")
    cap = cov("d1", "operational", "<=", 1, scope="all", when=[1])
    assert bounds([lo, cap], "lo")["全体/1"] == (1, None)
    nobody = {s.name: ["公"] + ["早"] * 29 for s in STAFF}
    assert any(b["rule"] == "lo" and b["key"] == "全体/1" for b in check_table([lo, cap], FAC, STAFF, NOV, nobody))


def test_施設全体の上限だけの日付の人数で_ユニットの下限を外す時も施設全体は日付の数まで():
    daily = cov("r1", "fixed", "==", 1)
    cap = cov("d1", "operational", "<=", 1, scope="all", when=[1])
    eff = next(r for r in with_overrides([daily, cap]) if r.id == "r1")
    tot = [(i.lo, i.hi) for i in expand(eff, FAC, STAFF, NOV) if i.key == "全体/1"]
    assert tot == [(1, None)]


def test_同じ日の施設全体の人数を言い直すと新しい方だけが効く():
    """2ユニットで毎日各2人（動かせない）、同じ1日に「施設全体3人」、続けて「施設全体1人」。古い3人が毎日のルールの強さで残り、
    段1で作れず、段3で3人の表になった（4×7）"""
    daily = cov("r1", "fixed", "==", 2)
    three = cov("d3", "operational", "==", 3, scope="all", when=[1])
    one = cov("d1", "operational", "==", 1, scope="all", when=[1])
    eff = next(r for r in with_overrides([daily, three, one]) if r.id == "r1")
    assert [(i.lo, i.hi) for i in expand(eff, FAC, STAFF, NOV) if i.key == "全体/1"] == [(1, None)]
    assert sum(early_by_day([daily, three, one])[1].values()) == 1


def test_範囲とほかの日付を一緒に言った人数は聞き返す(made, tmp_path):
    """「1日から5日までと10日は早番3人」が1〜5日だけになった（4×8）"""
    r, new = _say(made, tmp_path, "1日から5日までと10日は早番を3人", [{"type": "coverage", "shift": "早", "min": 3, "max": None}])
    assert r.kind != "applied" and new == []


def test_範囲を言い直した人数は聞き返す(made, tmp_path):
    """「1日から5日まで、いや、3日から5日まで早番3人」が言い直す前の1〜5日になった（4×8）"""
    r, new = _say(made, tmp_path, "1日から5日まで、いや、3日から5日まで早番を3人",
                  [{"type": "coverage", "shift": "早", "min": 3, "max": None}])
    assert r.kind != "applied" and new == []


def test_何日以外の人数は聞き返す(made, tmp_path):
    """「28日以外は早番3人」が28日だけの人数になった（3×8）"""
    r, new = _say(made, tmp_path, "28日以外は早番を3人", [{"type": "coverage", "shift": "早", "min": 3, "max": None}])
    assert r.kind != "applied" and new == []


def test_幅のある日付の人数は言い直しと取り違えない():
    """「28日は施設全体で早番を3人から4人に」は下限と上限の2本になる。新しい方だけを効かせる時に上限だけが残り、毎日の各1人の
    上限がゆるまず、段1で作れず段3で2人になった（Astra 2026-11-07 朝の2回目。3×8）"""
    daily = cov("r1", "fixed", "==", 1)
    lo = replace(cov("d28lo", "operational", ">=", 3, scope="all", when=[28]), meta={"uid": "u1"})   # 1つの一言から2本
    hi = replace(cov("d28hi", "operational", "<=", 4, scope="all", when=[28]), meta={"uid": "u1"})
    got = early_by_day([daily, lo, hi])
    assert 3 <= sum(got[28].values()) <= 4


def test_何日だけ除いての人数は聞き返す(made, tmp_path):
    """「28日だけ除いて早番を3人にして」が28日だけの人数になった（3×8）"""
    r, new = _say(made, tmp_path, "28日だけ除いて早番を3人にして", [{"type": "coverage", "shift": "早", "min": 3, "max": None}])
    assert r.kind != "applied" and new == []


def test_日付の無い以外の人数は今までどおり(made, tmp_path):
    """日付を言っていない一言の「以外」は、日付の人数の聞き返しに入れない"""
    r, new = _say(made, tmp_path, "日曜以外は早番を1人", [{"type": "coverage", "shift": "早", "min": 1, "max": 1,
                                                       "weekdays": ["mon", "tue", "wed", "thu", "fri", "sat"]}])
    assert "日付" not in (r.message or "")
    assert all(not x.when for x in new)


def test_0人の後に3人以上と言い直すと古い0人は効かない():
    """毎日各ユニット1人、同じ1日に「施設全体0人」、続けて「施設全体3人以上」。古い0人の上限が残って毎日の下限がゆるみ、
    希望休と重なると0人の表が通った（Astra 2026-11-07 朝の3回目。2×9）"""
    daily = cov("r1", "fixed", "==", 1)
    zero = replace(cov("z", "operational", "==", 0, scope="all", when=[1]), meta={"uid": "u1"})
    three = replace(cov("t", "operational", ">=", 3, scope="all", when=[1]), meta={"uid": "u2"})
    eff = next(r for r in with_overrides([daily, zero, three]) if r.id == "r1")
    day1 = {i.key: (i.lo, i.hi) for i in expand(eff, FAC, STAFF, NOV) if i.key.endswith("/1")}
    assert day1["U1/1"][0] == 1 and day1["U2/1"][0] == 1    # 各ユニットの下限は残る（0人にゆるめない）
    assert sum(early_by_day([daily, zero, three])[1].values()) >= 3


def test_何日を除きの人数は聞き返す(made, tmp_path):
    r, new = _say(made, tmp_path, "28日を除き早番を3人", [{"type": "coverage", "shift": "早", "min": 3, "max": None}])
    assert r.kind != "applied" and new == []
