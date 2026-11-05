"""取り込みで、ユニットごとには0人の日がある勤務（夜勤を2ユニットで順に回す表）は、施設全体の人数も守る試験。
直す前は、ユニットごとの夜勤に下限が付かず、作った翌月に施設全体で夜勤0人の夜ができていた。名前はすべて作り話。"""
import calendar

import pytest

from shiftvoice.build import build_original
from shiftvoice.nextmonth import copy_next_month, next_ym
from shiftvoice.onboard import create_facility, infer, parse_paste
from shiftvoice.store import Store

EAST = ["赤坂", "井上", "内田", "遠藤"]
WEST = ["太田", "川口", "木下", "工藤"]
DAYS = 30


def make_paste(per_night: int) -> str:
    """毎晩、施設全体で夜勤が per_night 人。夜勤は東と西が1日おきに回すので、ユニットごとには0人の日がある。"""
    # 夜勤と明けの人以外は、土日(曜日の行の土・日)は公休、平日は日勤
    grid = {nm: ["公" if d % 7 in (5, 6) else "日" for d in range(DAYS)] for nm in EAST + WEST}
    for d in range(DAYS):
        unit = EAST if d % 2 == 0 else WEST
        for k in range(per_night):
            nm = unit[((d // 2) * per_night + k) % len(unit)]
            grid[nm][d] = "夜"
            if d + 1 < DAYS:
                grid[nm][d + 1] = "明"
    lines = ["氏名\t" + "\t".join(str(d) for d in range(1, DAYS + 1)),
             "\t" + "\t".join("月火水木金土日"[i % 7] for i in range(DAYS)), "東ユニット"]
    lines += [nm + "\t" + "\t".join(grid[nm]) for nm in EAST]
    lines.append("西ユニット")
    lines += [nm + "\t" + "\t".join(grid[nm]) for nm in WEST]
    return "\n".join(lines)


def night_totals(table: dict) -> list[int]:
    days = len(next(iter(table.values())))
    return [sum(1 for row in table.values() if row[d] == "夜") for d in range(days)]


@pytest.mark.parametrize("per_night", [1, 2])
def test_ユニットごとに夜勤0人の日がある表は施設全体の夜勤の人数も守る(tmp_path, per_night):
    p = parse_paste(make_paste(per_night))
    assert p.unit_list == ["東", "西"]
    pasted_totals = night_totals(p.table)
    assert min(pasted_totals) == max(pasted_totals) == per_night       # 施設全体では毎晩同じ人数
    pr = infer(p, 2026, 11)
    whole = [r for r in pr.rules if r["name"].startswith("夜勤の人数（施設全体")]
    assert whole and all(r["params"]["scope"] == "all" for r in whole)
    c = create_facility(pr, tmp_path / "f", name="試験", next_year=2026, next_month=12)
    assert c.next_table_made
    table = Store(tmp_path / "f").month(2026, 12).table
    assert min(night_totals(table)) >= per_night                       # 夜勤0人の夜が無い


def test_翌月の日数が多い時は夜勤の回数の幅も広げて毎晩の人数と両立する(tmp_path):
    """28日の2月に、夜勤の4人が7回ずつ（2ユニットで1日おきに回し、施設全体では毎晩1人）。31日の3月は31回の夜勤が要るので、
    回数の上限が7回のままだと毎晩の人数と合わせきれず、運用の都合を外す段に下がって1人に夜勤が寄っていた（Codex の指摘 2026-11-05）。"""
    days = 28
    east_n, west_n = ["赤坂", "井上"], ["太田", "川口"]
    east_d, west_d = ["内田", "遠藤"], ["木下", "工藤"]
    grid = {nm: ["公" if d % 7 in (5, 6) else "日" for d in range(days)] for nm in east_n + west_n + east_d + west_d}
    for nm in east_n + west_n:
        grid[nm] = ["公"] * days
    for d in range(days):
        unit = east_n if d % 2 == 0 else west_n
        nm = unit[(d // 2) % 2]
        grid[nm][d] = "夜"
        if d + 1 < days:
            grid[nm][d + 1] = "明"
    lines = ["氏名\t" + "\t".join(str(d) for d in range(1, days + 1)), "東ユニット"]
    lines += [nm + "\t" + "\t".join(grid[nm]) for nm in east_n + east_d]
    lines.append("西ユニット")
    lines += [nm + "\t" + "\t".join(grid[nm]) for nm in west_n + west_d]
    p = parse_paste("\n".join(lines))
    assert [sum(1 for x in grid[nm] if x == "夜") for nm in east_n + west_n] == [7, 7, 7, 7]
    pr = infer(p, 2027, 2)
    rule = [r for r in pr.rules if r["name"] == "夜勤の回数（夜勤に入れる人）"][0]
    assert (rule["params"]["min"], rule["params"]["max"]) == (7, 8)
    c = create_facility(pr, tmp_path / "f", name="試験", next_year=2027, next_month=3)
    assert c.next_table_made and c.stage_note is None, c.stage_note          # 運用の都合を外す段に下がらない
    table = Store(tmp_path / "f").month(2027, 3).table
    assert min(night_totals(table)) >= 1
    assert max(sum(1 for x in table[nm] if x == "夜") for nm in east_n + west_n) <= 8


NIGHT = ["青木", "石田", "上野", "江口", "小川", "加藤", "岡田"]
DAYTIME = ["落合", "菊池"]


def rotation_paste(days: int, k: int) -> str:
    """夜勤の k 人が順に毎晩1人ずつ入る表（夜勤→明け→公休、ほかの日は日勤と公休）。日勤だけの2人は土日が公休。"""
    cycle = ["夜", "明", "公"] + ["日"] * (k - 4) + ["公"]
    grid = {nm: [cycle[(d - i) % k] for d in range(days)] for i, nm in enumerate(NIGHT[:k])}
    for nm in DAYTIME:
        grid[nm] = ["公" if d % 7 in (5, 6) else "日" for d in range(days)]
    lines = ["氏名\t" + "\t".join(str(d) for d in range(1, days + 1))]
    lines += [nm + "\t" + "\t".join(grid[nm]) for nm in NIGHT[:k] + DAYTIME]
    return "\n".join(lines)


@pytest.mark.parametrize("year, month, k, nights, want", [
    (2026, 11, 6, [5] * 6, (5, 6)),             # 30日の11月に6人が5回ずつ。四捨五入（5×31/30）では上限5回で、6人で30回 < 12月の31晩
    (2027, 2, 7, [4] * 7, (4, 5)),              # 28日の2月に7人が4回ずつ。四捨五入（4×31/28）では上限4回で、7人で28回 < 3月の31晩
    (2027, 1, 6, [6, 5, 5, 5, 5, 5], (4, 6)),   # 31日の1月に6人が5〜6回。下限5回のままだと6人で30回 > 2月の28晩
])
def test_翌月の夜勤の回数の幅は夜勤に入れる人の合計で毎晩の人数に合う(tmp_path, year, month, k, nights, want):
    """毎晩1人の夜勤を、夜勤に入れる人で回す表。翌月の日数が違うと、回数の幅の合計が翌月の晩の数に届かない（または超える）ことがあり、
    運用の都合を外す段に下がっていた（Codex の指摘 2026-11-05。上限は 4×7、下限は同じ形の逆向き）。"""
    days = calendar.monthrange(year, month)[1]
    p = parse_paste(rotation_paste(days, k))
    assert [sum(1 for x in p.table[nm] if x == "夜") for nm in NIGHT[:k]] == nights
    pr = infer(p, year, month)
    rule = [r for r in pr.rules if r["name"] == "夜勤の回数（夜勤に入れる人）"][0]
    assert (rule["params"]["min"], rule["params"]["max"]) == want
    ny, nm = next_ym(year, month)
    c = create_facility(pr, tmp_path / "f", name="試験", next_year=ny, next_month=nm)
    assert c.next_table_made and c.stage_note is None, c.stage_note          # 運用の都合を外す段に下がらない
    table = Store(tmp_path / "f").month(ny, nm).table
    assert min(night_totals(table)) >= 1
    assert all(want[0] <= sum(1 for x in table[n] if x == "夜") <= want[1] for n in NIGHT[:k])


UNIT_N = {"東": ["青木", "石田", "上野", "江口", "小川", "加藤"], "西": ["岡田", "落合", "菊池", "久保", "小島"]}
UNIT_D = {"東": "斉藤", "西": "篠原"}


def units_rotation_paste(days: int) -> str:
    """2ユニットのグループホームの形。夜勤は各ユニットで毎晩1人で、東は6人・西は5人が順に入る（夜勤→明け→公休、ほかの日は
    日勤と公休）。各ユニットに日勤だけの人が1人いて、土日が公休。"""
    grid = {}
    for names in UNIT_N.values():
        k = len(names)
        cycle = ["夜", "明", "公"] + ["日"] * (k - 4) + ["公"]
        grid |= {nm: [cycle[(d - i) % k] for d in range(days)] for i, nm in enumerate(names)}
    grid |= {nm: ["公" if d % 7 in (5, 6) else "日" for d in range(days)] for nm in UNIT_D.values()}
    lines = ["氏名\t" + "\t".join(str(d) for d in range(1, days + 1))]
    for u, names in UNIT_N.items():
        lines += [f"{u}ユニット"] + [nm + "\t" + "\t".join(grid[nm]) for nm in names + [UNIT_D[u]]]
    return "\n".join(lines)


@pytest.mark.parametrize("year, month, nights, want", [
    # 30日の11月に、東6人が5回ずつ・西5人が6回ずつ。施設全体の11人で見ると上限6回で足りるが、西は5人×6回=30回 < 12月の31晩
    (2026, 11, {"東": [5] * 6, "西": [6] * 5}, (5, 7)),
    # 31日の1月に、東は5〜6回・西は6〜7回。施設全体で見ると下限5回のままだが、東は6人×5回=30回 > 2月の28晩
    (2027, 1, {"東": [6, 5, 5, 5, 5, 5], "西": [7, 6, 6, 6, 6]}, (4, 7)),
])
def test_ユニットごとに毎晩夜勤がいる表は夜勤の回数の幅をユニットごとの人数で広げる(tmp_path, year, month, nights, want):
    """夜勤の人数がユニットごとのルール（各ユニットに毎晩1人）で、ユニットごとに夜勤に入れる人の数が違う表。回数の幅を施設全体の
    人数だけで広げると、人の少ないユニットの上限の合計が晩の数に届かず（人の多いユニットの下限の合計が晩の数を超え）、運用の都合を
    外す段に下がって1人に夜勤が寄っていた（Astra の指摘 2026-11-05 4×6。2ユニットのグループホームでふつうにある形）。"""
    days = calendar.monthrange(year, month)[1]
    p = parse_paste(units_rotation_paste(days))
    assert p.unit_list == ["東", "西"]
    assert {u: [sum(1 for x in p.table[nm] if x == "夜") for nm in names] for u, names in UNIT_N.items()} == nights
    pr = infer(p, year, month)
    rule = [r for r in pr.rules if r["name"] == "夜勤の回数（夜勤に入れる人）"][0]
    assert (rule["params"]["min"], rule["params"]["max"]) == want
    ny, nm = next_ym(year, month)
    c = create_facility(pr, tmp_path / "f", name="試験", next_year=ny, next_month=nm)
    assert c.next_table_made and c.stage_note is None, c.stage_note          # 運用の都合を外す段に下がらない
    table = Store(tmp_path / "f").month(ny, nm).table
    for names in UNIT_N.values():
        assert all(sum(1 for n in names if table[n][d] == "夜") == 1 for d in range(len(table[names[0]])))
        assert all(want[0] <= table[n].count("夜") <= want[1] for n in names)


def build_after(store: Store, year: int, month: int):
    """year・month の次の月の表を作って置く（取り込んだ施設で月を進めた時と同じ帳面のルールで作る）。"""
    copy_next_month(store, year, month)
    ny, nm = next_ym(year, month)
    r = build_original(store, ny, nm, with_premise=False)
    m = store.month(ny, nm)
    m.table = r.result.table
    store.save_month(m)
    return r


def test_取り込んだ後の28日の2月も夜勤の回数の下限が毎晩の人数と合う(tmp_path):
    """30日の11月に6人が5回ずつ（毎晩1人）を取り込む。取り込みのルールは翌月より先の月にもそのまま使うので、28日の2月に
    下限5回のままだと6人で30回になり28晩を超え、運用の都合を外す段に下がっていた。"""
    c = create_facility(infer(parse_paste(rotation_paste(30, 6)), 2026, 11), tmp_path / "f", name="試験",
                        next_year=2026, next_month=12)
    assert c.next_table_made
    store = Store(tmp_path / "f")
    assert build_after(store, 2026, 12).ok                                  # 1月
    feb = build_after(store, 2027, 1)
    assert feb.ok and feb.note is None, feb.note                            # 2月も運用の都合を外す段に下がらない
    table = store.month(2027, 2).table
    assert min(night_totals(table)) >= 1 and sum(night_totals(table)) == 28
