"""施設の Excel の形（前の月の末5日つきの日付の行・曜日・予定の行・欠員の印つきのユニットの見出し・合計の行）と、
明けを書かない夜勤（夜勤の翌日を「公」と書き、夜勤が2日続く「夜夜公公」）を読む（2026-11-06 実際の施設の形の試し）。
見本は作り話の2ユニット10人の11月で、10日周期の「早遅夜夜公公日早遅公」を1人ずつずらして回す。API は呼ばない。"""
from pathlib import Path

from shiftvoice.build import month_rules
from shiftvoice.onboard import create_facility, infer, parse_paste
from shiftvoice.rules import check_table
from shiftvoice.ruledetail import rule_detail
from shiftvoice.store import Store

FX = Path(__file__).parent / "fixtures" / "pastes"
PASTE = (FX / "excel_nights_no_ake.tsv").read_text(encoding="utf-8")
NAMES = ["青木", "石田", "上野", "江口", "小川", "加藤", "岡田", "落合", "菊池", "久保"]


def test_予定の行と欠員の印つきの見出しを人にしない():
    """直す前は「予定」の行（祝と行事）を人に、その上の「日付」をユニットに読み、見出しの行に「欠」があると「U1」も人に
    読んだ（知らない記号に 祝・欠・秋祭り が出て、ユニットは「全体」の1つになった）"""
    p = parse_paste(PASTE)
    assert list(p.table) == NAMES
    assert p.unit_list == ["U1", "U2"]
    assert p.unit_of["青木"] == "U1" and p.unit_of["久保"] == "U2"
    assert p.unknown_symbols == []


def test_明けを書かない施設は夜勤の後の並びを貼った表から決める():
    prop = infer(parse_paste(PASTE), 2026, 11, {})
    assert prop.problems == []
    rules = {r["name"]: r for r in prop.rules}
    seq = rules["夜勤の翌日は夜勤か公休"]
    assert seq["flex"] == "fixed" and seq["params"] == {"if": ["夜"], "next_in": ["夜", "公"]}
    assert "明けの記号が表に無い" in seq["meta"]["evidence"]
    two = rules["夜勤の後の休みの翌日も休み"]
    assert two["flex"] == "fixed" and two["params"] == {"if": ["夜"], "then": ["公"], "next2_in": ["公"]}
    run = rules["夜勤は続けて2日まで"]
    assert run["kind"] == "max_run" and run["params"] == {"shifts": ["夜"], "max": 2}


def test_明けを書く施設には足さない():
    prop = infer(parse_paste((FX / "three_units.tsv").read_text(encoding="utf-8")), 2026, 11,
                 {"日8": {"name": "日8の勤務", "kind": "work"}})
    assert not any("next2_in" in r["params"] for r in prop.rules)
    assert not any(r["kind"] == "max_run" and r["params"]["shifts"] == ["夜"] for r in prop.rules)
    assert any(r["name"] == "夜勤の翌日は明け" for r in prop.rules)


def test_作った翌月の表で夜勤の翌日に勤務を置かず_説明も出る(tmp_path):
    """直す前は、明けのルールが無いので、作った表で夜勤の翌日に早番・日勤・遅番が入った"""
    prop = infer(parse_paste(PASTE), 2026, 11, {})
    c = create_facility(prop, tmp_path / "f", name="試験", next_year=2026, next_month=12)
    assert c.next_table_made
    store = Store(tmp_path / "f")
    fac, staff, m = store.facility(), store.staff_on(2026, 12), store.month(2026, 12)
    for name, row in m.table.items():
        seq = m.prev_tail[name][-2:] + row
        for d in range(2, len(seq)):
            if seq[d - 1] == "夜":
                assert seq[d] in ("夜", "公"), (name, d)
            if seq[d - 2] == "夜" and seq[d - 1] == "公":
                assert seq[d] == "公", (name, d)
        assert "夜夜夜" not in "".join(row)
    rules = month_rules(store, 2026, 12, with_premise=False)
    assert [b for b in check_table(rules, fac, staff, m, m.table) if b["flex"] == "fixed"] == []
    two = next(r for r in rules if "next2_in" in r.params)
    d = rule_detail(two.id, rules, fac, staff, m, m.table)
    assert "夜勤の次の日が公休なら、その次の日も公休" in str(d)

