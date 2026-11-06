"""施設の Excel の形（前の月の末5日つきの日付の行・曜日・予定の行・欠員の印つきのユニットの見出し・合計の行）と、
明けを書かない夜勤（夜勤の翌日を「公」と書き、夜勤が2日続く「夜夜公公」）を読む（2026-11-06 実際の施設の形の試し）。
見本は作り話の2ユニット10人の11月で、10日周期の「早遅夜夜公公日早遅公」を1人ずつずらして回す。API は呼ばない。"""
from pathlib import Path

from shiftvoice.build import month_rules
from shiftvoice.onboard import create_facility, infer, parse_paste
from shiftvoice.rules import check_table
from shiftvoice.ruledetail import rule_detail
from shiftvoice.solver import large_workers
from shiftvoice.store import SEED, Store

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
    assert two["flex"] == "fixed" and two["params"] == {"if": ["夜"], "then": ["公"], "next2_in": ["公", "有"]}
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


def test_取り込んだ施設は30人以下でも最初の段から作業者を回す(tmp_path):
    """1人で探すと、次の勤務までの間の罰の入った取り込みの施設で良い表に届かなかった（実際の施設の形の18人の10月で
    短い並び52回。8つ回すと0回）。見本の施設は取り込んでいないので1人のまま"""
    prop = infer(parse_paste(PASTE), 2026, 11, {})
    create_facility(prop, tmp_path / "f", name="試験", next_year=2026, next_month=12)
    store = Store(tmp_path / "f")
    assert large_workers(store.facility(), store.staff_on(2026, 12)) == 8
    seed = Store(SEED)
    assert large_workers(seed.facility(), seed.staff_on(2026, 11)) == 0


def test_1文字の名前や施設独自の記号だけの人の行は見出しにしない():
    """職員を A・B と書き、勤務を施設独自の記号（N・D）で書き、休みを空欄にする表。勤務の記号に当たらなくても人の行"""
    head = "氏名\t" + "\t".join(str(d) for d in range(1, 31))
    cycle = ["N", "D", "", "D", ""]
    rows = [f"{nm}\t" + "\t".join(cycle[(d + k) % 5] for d in range(30)) for k, nm in enumerate(["A", "B", "C", "U1"])]
    p = parse_paste("\n".join([head] + rows) + "\n")
    assert list(p.table) == ["A", "B", "C", "U1"]   # U1 も印が半分の日を超えるので人の行


def test_名前の注記や施設の記号の人を見出しや予定の行にしない():
    """「青木（2階）」で勤務を施設独自の記号・休みを空欄で書く人と、「石田（入職予定）」の人は、人の行（Astra 2026-11-07）"""
    head = "氏名\t" + "\t".join(str(d) for d in range(1, 31))
    rows = ["青木（2階）\t" + "\t".join(["D", "", ""][d % 3] for d in range(30)),
            "石田（入職予定）\t" + "\t".join(["日", "公", "日"][d % 3] for d in range(30))]
    rows += [f"{n}\t" + "\t".join(["日", "公", "日"][d % 3] for d in range(30)) for n in ("上野", "江口", "小川")]
    p = parse_paste("\n".join([head] + rows) + "\n")
    assert len(p.table) == 5 and p.unit_list == ["全体"]


def test_夜勤の後の休みの次の日の有給も休みに数える():
    """夜勤・公・有 の並びを外れにすると、2日先のルールが作られなかったり、有給の日を公休へ動かしたりした（Astra）"""
    rows = {n: list("夜公公日公") * 6 for n in ("青木", "石田", "上野", "江口", "小川")}
    for n in list(rows)[:4]:
        rows[n][2] = "有"
    prop = infer(parse_paste("\n".join(n + "\t" + "\t".join(r) for n, r in rows.items()) + "\n"), 2026, 11, {})
    two = [r["params"] for r in prop.rules if "next2_in" in r["params"]]
    assert two == [{"if": ["夜"], "then": ["公"], "next2_in": ["公", "有"]}]


def test_月間予定の行と括弧つきの見出しと所属を添えた人():
    """「月間予定」は人にしない。「2階（介護）」は欠の印があっても見出し。「青木 2階」は欠の印だけの月でも人（Astra 2026-11-07）"""
    head = "氏名\t" + "\t".join(str(d) for d in range(1, 31))
    plain = lambda: "\t".join(["日", "公", "日"][d % 3] for d in range(30))
    marks = "\t".join("欠" if d in (3, 9) else "" for d in range(30))
    rows = ["月間予定\t" + "\t".join("祝" if d == 2 else "" for d in range(30)),
            "2階（介護）\t" + marks, "石田\t" + plain(), "上野\t" + plain(),
            "青木 2階\t" + marks, "江口\t" + plain()]
    p = parse_paste("\n".join([head] + rows) + "\n")
    assert list(p.table) == ["石田", "上野", "青木2階", "江口"]   # 名前の空白は詰め、括弧は半角にそろえて読む（前から）
    assert p.unit_list == ["2階(介護)"] and p.unit_of["青木2階"] == "2階(介護)"


def test_持ち出す表の前の月の日の列は_その月の前の月の末(tmp_path):
    """貼った Excel の日付の行の左にある前の月の末の5日（10/27〜31）を、12月の表を持ち出す時にもそのまま出していた。
    12月は 11/26〜30 の日付・曜日・値にする。貼った11月は貼った時のまま（2026-11-07 の通しの試し）"""
    from shiftvoice.reader import RecordedReader
    from shiftvoice.server import table_tsv
    from shiftvoice.session import Session
    create_facility(infer(parse_paste(PASTE), 2026, 11, {}), tmp_path / "f", name="試験", next_year=2026, next_month=12)
    st = Store(tmp_path / "f")
    fac = st.facility()
    out = {}
    for mo in (11, 12):
        s = Session(st, 2026, mo, RecordedReader({}))
        s.open()
        out[mo] = [ln.split("\t") for ln in
                   table_tsv(s.state(), fac.settings.get("paste_symbols"), fac.settings.get("paste_layout")).splitlines()]
    nov, dec = out[11], out[12]
    assert nov[0][:6] == ["日付", "10/27", "28", "29", "30", "31"] and nov[1][:6] == ["曜日", "火", "水", "木", "金", "土"]
    assert dec[0][:7] == ["日付", "11/26", "27", "28", "29", "30", "1"] and dec[1][:6] == ["曜日", "木", "金", "土", "日", "月"]
    row = next(r for r in dec if r[0] == "青木")
    assert row[1:6] == st.month(2026, 12).prev_tail["青木"][-5:] and row[1:6] == st.month(2026, 11).table["青木"][-5:]
    assert next(r for r in nov if r[0] == "青木")[1:6] == parse_paste(PASTE).layout["lefts"]["青木"][1:6]
