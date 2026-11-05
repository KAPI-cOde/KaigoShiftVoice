"""取り込みで、ユニットごとには0人の日がある勤務（夜勤を2ユニットで順に回す表）は、施設全体の人数も守る試験。
直す前は、ユニットごとの夜勤に下限が付かず、作った翌月に施設全体で夜勤0人の夜ができていた。名前はすべて作り話。"""
import pytest

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
