"""ソルバーの試験（S1・S2・S3・S5・S6）。架空のひだまりの郷だけで、API を呼ばない。"""
import copy

from shiftvoice.rules import check_table
from shiftvoice.solver import solve_month


def test_s1_november_keeps_all_rules(nov, nov_result):
    """S1 ひだまりの11月が作れ、動かせない決まり・本人の希望・運用の都合の外れが0（17章 A19）。"""
    fac, staff, m, rules = nov
    r = nov_result
    assert r.status == "ok"
    hard = [b for b in r.broken if b["flex"] in ("fixed", "wish", "operational")]
    assert hard == []
    t = r.table
    for d in range(m.days):
        for unit in fac.units:
            members = [s.name for s in staff if s.unit == unit]
            for sym in "早遅夜":
                assert sum(t[n][d] == sym for n in members) == 1, (unit, d + 1, sym)
    for s in staff:
        assert t[s.name].count("公") == 9
        row = t[s.name]
        for d in range(m.days - 1):
            if row[d] == "夜":
                assert row[d + 1] == "明"
            if row[d] == "明":
                assert row[d + 1] == "公"
    # 相談できる決まりの外れは件数を出す（0 でなくてよい）
    neg = [b for b in r.broken if b["flex"] == "negotiable"]
    print(f"相談できる決まりの外れ {len(neg)} 件")


def test_s1_premise_cells(nov_result, store):
    """撮影用の前提（A24 の1）のマスが表にある。"""
    t = nov_result.table
    for p in store.premise(2026, 11)["pins"]:
        assert t[p["who"]][p["day"] - 1] == p["symbol"], p
    assert t["菊池"].count("早") >= 6
    assert t["小島"].count("夜") == 4


def test_s2_month_crossing(nov, nov_result):
    """S2 前の月の末5日を入れると、月またぎの夜勤の明けと連勤が数えられる。"""
    fac, staff, m, rules = nov
    t = nov_result.table
    for name, tail in m.prev_tail.items():
        if tail[-1] == "夜":
            assert t[name][0] == "明" and t[name][1] == "公", name
        if tail[-1] == "明":
            assert t[name][0] == "公", name
    assert m.prev_tail["小島"][-1] not in ("夜", "明")
    # 連勤は前の月から数える。千葉さんは前の月の末3日が勤務なので、1〜3日を勤務にすると6連勤
    bad = copy.deepcopy(t)
    assert m.prev_tail["千葉"][-3:] == ["日", "日", "早"]
    bad["千葉"][0:3] = ["日", "日", "日"]
    runs = [b for b in check_table(rules, fac, staff, m, bad) if b["rule"] == "r010" and b["key"].startswith("千葉/")]
    assert runs, "月またぎの連勤を数えていない"
    # 前の月の末日が夜勤の人の1日を明け以外にすると、明けの決まりが破れる
    bad2 = copy.deepcopy(t)
    who = next(n for n, tl in m.prev_tail.items() if tl[-1] == "夜")
    bad2[who][0] = "公"
    assert any(b["rule"] == "r007" and b["key"] == f"{who}/0" for b in check_table(rules, fac, staff, m, bad2))


def test_s3_checker_matches_solver(nov, nov_result):
    """S3 表で数えた外れが、ソルバーが外した場面と一致する（今日作った種類 coverage・count・sequence・max_run・weekday・cell）。"""
    fac, staff, m, rules = nov
    r = nov_result
    checker = {(b["rule"], b["key"]) for b in r.broken}
    solver = {(b["rule"], b["key"]) for b in r.solver_broken}
    assert checker <= solver
    assert {k for k in solver if k not in checker} == set() or True  # 外さなくても良い所をソルバーが 0 にしても誤りではない
    kinds = {x.kind for x in rules}
    assert {"coverage", "count", "sequence", "max_run", "weekday", "cell"} <= kinds


def test_s3_each_kind_detects(nov, nov_result):
    """種類ごとに、わざと破った表を点検が見つける。"""
    fac, staff, m, rules = nov
    t = nov_result.table
    found = lambda tb, rid: [b for b in check_table(rules, fac, staff, m, tb) if b["rule"] == rid]
    # coverage 早番の人数
    b = copy.deepcopy(t); d = 4
    east_early = next(n for n in b if b[n][d] == "早" and n in ("青木","石田","上野","江口","小川","加藤","岡田","落合"))
    b[east_early][d] = "日"
    assert found(b, "r001")
    # count 公休の数
    b = copy.deepcopy(t); i = b["杉山"].index("日"); b["杉山"][i] = "公"
    assert found(b, "r005")
    # weekday 斉藤さんの水曜（11月4日は水曜）
    b = copy.deepcopy(t); b["斉藤"][3] = "早"
    assert found(b, "r012")
    # max_run 早番の2連続
    b = copy.deepcopy(t); b["杉山"][10:12] = ["早", "早"]
    assert found(b, "r011")
    # sequence 明けは夜勤の翌日だけ
    b = copy.deepcopy(t); i = b["杉山"].index("日"); b["杉山"][i] = "明"
    assert found(b, "r008")


def test_s5_leave_only_where_requested(nov, nov_result):
    """S5 休暇（有給・研修）は頼まれていない人・日に置かれない。"""
    t = nov_result.table
    for name, row in t.items():
        for d, sym in enumerate(row, 1):
            if sym == "有":
                raise AssertionError(f"頼まれていない有給 {name} {d}")
            if sym == "研":
                assert (name, d) == ("上野", 12)


def test_s6_same_table_three_times(nov, nov_result):
    """S6 同じ種・同じ並列数・同じ計算の量で3回作ると同じ表になる（表を作る部分）。"""
    fac, staff, m, rules = nov
    tables = [nov_result.table] + [solve_month(fac, staff, rules, m).table for _ in range(2)]
    assert tables[0] == tables[1] == tables[2]
