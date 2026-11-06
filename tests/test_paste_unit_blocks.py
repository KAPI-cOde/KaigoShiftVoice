"""ユニットごとに見出しや日付の行がある Excel の表を貼る（2026-11-06 通しの試しで見つけた）。

直す前は、最初のユニットの見出しが日付の行より上だと最初のユニットの人が「全体」になり、ユニットごとに日付の行が
繰り返す表は「名前が重なっています」で止まるか、日付の行が人の行になっていた。"""
import pytest

from shiftvoice.onboard import infer, parse_paste
from shiftvoice.server import blank_rows, table_tsv

UNITS = {"東": ["青木", "石田", "上野"], "西": ["加藤", "岡田"], "南": ["小島", "斉藤"]}
WK = "日月火水木金土"
DAYS = 30
PATTERN = "早遅夜明公日早"


def row(i: int) -> list[str]:
    return [PATTERN[(d + i) % 7] for d in range(DAYS)]


NAMES = [n for ns in UNITS.values() for n in ns]
TABLE = {n: row(i) for i, n in enumerate(NAMES)}
WEEK = [WK[d % 7] for d in range(DAYS)]
DATES = [str(d + 1) for d in range(DAYS)]
RIGHT = ["早", "遅", "夜", "公"]


def staff_row(n: str) -> list[str]:
    return [n] + TABLE[n] + [str(TABLE[n].count(c)) for c in RIGHT]


def tsv(rows: list[list[str]]) -> str:
    return "\n".join("\t".join(r) for r in rows) + "\n"


def blocks(where: str) -> list[list[str]]:
    """ユニットごとに曜日と日付の行が繰り返す表。where はユニットの名前を書く場所"""
    out = []
    for u, ns in UNITS.items():
        name = u + "ユニット"
        if where == "row":
            out.append([name])
        out.append([name if where == "weekday" else ""] + WEEK)
        out.append([name if where == "date" else "氏名"] + DATES + RIGHT)
        out += [staff_row(n) for n in ns]
    return out + [["早番"] + ["2"] * DAYS]


def top_heading(title: bool = False) -> list[list[str]]:
    """いちばん上にユニットの見出し、曜日、日付。2つ目のユニットからは人の行の間に見出し"""
    out = [["2026年11月 勤務表"]] if title else []
    out += [["東ユニット"], [""] + WEEK, ["氏名"] + DATES + RIGHT]
    for i, (u, ns) in enumerate(UNITS.items()):
        if i:
            out.append([u + "ユニット"])
        out += [staff_row(n) for n in ns]
    return out + [["夜勤"] + ["1"] * DAYS]


def assert_units(p) -> None:
    assert list(p.table) == NAMES
    assert p.table == TABLE
    assert p.unit_list == ["東", "西", "南"]
    assert {n: p.unit_of[n] for n in NAMES} == {n: u for u, ns in UNITS.items() for n in ns}
    assert p.unknown_symbols == []


@pytest.mark.parametrize("where", ["row", "weekday", "date"])
def test_unit_blocks_are_read(where):
    assert_units(parse_paste(tsv(blocks(where))))


@pytest.mark.parametrize("title", [False, True])
def test_first_unit_heading_above_the_dates(title):
    p = parse_paste(tsv(top_heading(title)))
    assert_units(p)
    # ルールも3ユニットで見る（「全体」のユニットができない）
    assert infer(p, 2026, 11).units == ["東", "西", "南"]


def test_title_above_is_not_a_unit():
    rows = [["みどりの丘"], [""] + WEEK, ["氏名"] + DATES] + [[n] + TABLE[n] for n in NAMES]
    p = parse_paste(tsv(rows))
    assert p.unit_list == ["全体"]
    assert p.layout.get("plan") is None


def state(p) -> dict:
    """12月（31日）の表の state。今の貼り付けの人の並びで、全部のマスを「公」にする"""
    return {"staff": [{"name": n, "unit": p.unit_of[n]} for n in p.table], "days": 31,
            "weekdays": ["tue"] * 31, "table": {n: ["公"] * 31 for n in p.table}}


@pytest.mark.parametrize("rows", [blocks("row"), top_heading(), blocks("date"), blocks("weekday"), top_heading(True)],
                         ids=["row", "top", "date", "weekday", "title"])
def test_copy_keeps_the_pasted_row_order(rows):
    """人の行が、貼った時と同じ行に戻る（Excel の左上に貼って、人と行がずれない）"""
    p = parse_paste(tsv(rows))
    out = [r.split("\t") for r in table_tsv(state(p), None, p.layout).splitlines()]
    body = [r for r in rows if r[0] != "早番" and r[0] != "夜勤"]
    assert len(out) == len(body)
    for pasted, copied in zip(body, out):
        assert copied[0] == pasted[0]
        if pasted[0] in NAMES:
            assert copied[1:32] == ["公"] * 31
    assert parse_paste(table_tsv(state(p), None, p.layout)).unit_of == p.unit_of


def test_title_rows_come_back_above_a_plain_table():
    """タイトルと空の行の上に置いた、ユニットの見出しの無い表も、同じ左上へ貼り戻して人の行がそろう（Astra 2026-11-06 の指摘3）"""
    rows = [["みどりの丘"], [], ["", "", "2026年11月"], [""] + WEEK, ["氏名"] + DATES] + [[n] + TABLE[n] for n in NAMES]
    p = parse_paste(tsv(rows))
    assert p.layout.get("plan") is None and blank_rows(p.layout) == 0
    out = [r.split("\t") for r in table_tsv(state(p), None, p.layout).splitlines()]
    assert out[:3] == [["みどりの丘"], [""], ["", "", "2026年11月"]]
    assert [r[0] for r in out[3:]] == [r[0] for r in rows[3:]]
    assert list(parse_paste(table_tsv(state(p), None, p.layout)).table) == NAMES


def test_summary_rows_between_units_stay_as_blank_rows():
    rows = [[""] + WEEK, ["氏名"] + DATES]
    for u, ns in UNITS.items():
        rows.append([u + "ユニット"])
        rows += [[n] + TABLE[n] for n in ns]
        rows.append(["早番"] + ["1"] * DAYS)
    p = parse_paste(tsv(rows))
    assert_units(p)
    assert blank_rows(p.layout) == 2   # いちばん下の人数の行は、コピーの外
    out = [r.split("\t") for r in table_tsv(state(p), None, p.layout).splitlines()]
    assert [r[0] for r in out] == [r[0] for r in rows[:-1]]
    assert out[6][1:] == [""] * 31   # 東の3人の後の集計の行は、空で1行とる


def test_blank_line_between_units_keeps_its_row():
    rows = []
    for u, ns in UNITS.items():
        rows += [[u + "ユニット"], ["氏名"] + DATES] + [[n] + TABLE[n] for n in ns] + [[]]
    p = parse_paste(tsv(rows))
    assert_units(p)
    out = table_tsv(state(p), None, p.layout).splitlines()
    assert [r.split("\t")[0] for r in out] == [(r[0] if r else "") for r in rows[:-1]]


def test_plain_table_keeps_the_old_copy():
    """ふつうの並び（見出しが1回、ユニットの行、人の行）は、前と同じコピーのまま"""
    rows = [[""] + WEEK, ["氏名"] + DATES]
    for u, ns in UNITS.items():
        rows.append([u + "ユニット"])
        rows += [[n] + TABLE[n] for n in ns]
    assert parse_paste(tsv(rows + [["早番"] + ["1"] * DAYS])).layout.get("plan") is None
