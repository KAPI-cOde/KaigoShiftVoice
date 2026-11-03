"""取り込み（Excel の勤務表を貼る → 施設のデータ）の試験。見本は作り話の施設B。"""
from pathlib import Path

import pytest

from shiftvoice.onboard import (OnboardError, ParseError, create_facility, infer, parse_paste)
from shiftvoice.rules import check_table
from shiftvoice.store import Store

SAMPLE = Path(__file__).resolve().parents[1] / "data" / "samples" / "komorebi-2026-11.tsv"


@pytest.fixture(scope="module")
def text():
    return SAMPLE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def pasted(text):
    return parse_paste(text)


@pytest.fixture(scope="module")
def proposal(pasted):
    return infer(pasted, 2026, 11)


def row(*cells, n=30):
    cells = list(cells) + [""] * (n - len(cells))
    return "\t".join(cells)


def test_parse_見出し_曜日_集計を飛ばし空欄が公になる(text, pasted):
    assert pasted.days == 30 and pasted.had_header
    assert list(pasted.table) == ["赤坂", "井上", "内田", "遠藤", "太田", "川口", "木下", "工藤"]
    assert pasted.skipped == 1                     # 「早番 計」の行
    assert pasted.blank_as_rest > 0
    blanks = sum(1 for ln in text.split("\n")[2:10] for c in ln.split("\t")[1:] if c == "")
    assert pasted.blank_as_rest == blanks
    assert pasted.unknown_symbols == []
    assert all(len(r) == 30 for r in pasted.table.values())
    assert "" not in {s for r in pasted.table.values() for s in r}


def test_parse_別名と知らない記号と名前のそろえ():
    head = "氏名\t" + "\t".join(str(d) for d in range(1, 29))
    cells = ["休", "公休", "明け", "夜勤", "早番", "日勤", "遅番", "有給", "有休", "年休", "研修", "半", ""]
    cells += ["日"] * (28 - len(cells))
    t = head + "\n\u3000赤坂さん\t" + "\t".join(cells) + "\n井上\t" + "\t".join(["公"] * 28)
    p = parse_paste(t)
    assert "赤坂" in p.table                       # 「さん」と全角空白を除く
    assert p.table["赤坂"][:13] == ["公", "公", "明", "夜", "早", "日", "遅", "有", "有", "有", "研", "半", "公"]
    assert p.unknown_symbols == ["半"]              # 知らない記号はそのまま残して一覧で返す
    assert p.blank_as_rest == 1


def test_parse_カンマ区切りと見出し無し():
    n = 28
    t = "\n".join([
        "赤坂," + ",".join(["早", "公"] * 14),
        "井上," + ",".join(["公", "早"] * 14),
        "合計," + ",".join(["1"] * n),
    ])
    p = parse_paste(t)
    assert p.days == 28 and not p.had_header
    assert list(p.table) == ["赤坂", "井上"] and p.skipped == 1


def test_parse_日付の見出しと曜日の行():
    t = "\n".join([
        "名前\t" + "\t".join(f"11/{d}" for d in range(1, 31)),
        "\t" + "\t".join("月火水木金土日"[i % 7] for i in range(30)),
        "赤坂" + "\t" + "\t".join(["日"] * 30),
    ])
    p = parse_paste(t)
    assert p.days == 30 and list(p.table) == ["赤坂"]  # 全部「日」の人の行は曜日の行にしない


def test_parse_こわれた貼り付けは一文で言う():
    head = "氏名\t" + "\t".join(str(d) for d in range(1, 31))
    ok = "赤坂\t" + "\t".join(["公"] * 30)
    with pytest.raises(ParseError, match="3行目の「井上」.*29日分しかなく.*30日"):
        parse_paste(head + "\n" + ok + "\n井上\t" + "\t".join(["公"] * 29))
    with pytest.raises(ParseError, match="2行目と3行目で名前「赤坂」が重なって"):
        parse_paste(head + "\n" + ok + "\n" + ok)
    with pytest.raises(ParseError, match="職員の行が1つも"):
        parse_paste(head + "\n早番 計\t" + "\t".join(["1"] * 30))
    with pytest.raises(ParseError, match="日の数が20日分しかありません"):
        parse_paste("赤坂\t" + "\t".join(["公"] * 20))
    with pytest.raises(ParseError, match="11月は30日まで"):
        infer(parse_paste(head.replace("\t30", "\t30\t31") + "\n赤坂\t" + "\t".join(["公"] * 31)), 2026, 11)


def test_infer_勤務の種類と職員(proposal):
    assert [s["symbol"] for s in proposal.shift_types] == ["早", "日", "遅", "夜", "明", "公"]
    night = next(s for s in proposal.shift_types if s["symbol"] == "夜")
    assert night["next_is_ake"] is True
    assert proposal.units == ["全体"] and proposal.unknown_symbols == []
    assert len(proposal.staff) == 8 and all(s["can"] == ["早", "日", "遅", "夜"] for s in proposal.staff)


def test_infer_ルールの候補(proposal):
    by = {r["name"]: r for r in proposal.rules}
    assert by["早番の人数"]["flex"] == "fixed" and by["早番の人数"]["params"] == {
        "shifts": ["早"], "scope": "all", "op": "==", "n": 1}
    assert by["遅番の人数"]["params"]["n"] == 1 and by["夜勤の人数"]["flex"] == "fixed"
    day = by["日勤の人数"]
    assert day["flex"] == "negotiable" and day["params"]["op"] == ">=" and day["params"]["n"] == 1
    assert by["公休の数"]["params"] == {"shifts": ["公"], "min": 9, "max": 9} and by["公休の数"]["flex"] == "fixed"
    assert by["夜勤の翌日は明け"]["params"] == {"if": ["夜"], "next_in": ["明"]}
    assert by["明けは夜勤の翌日だけ"]["params"] == {"if": ["明"], "prev_in": ["夜"]}
    assert by["明けの翌日は公休"]["params"] == {"if": ["明"], "next_in": ["公"]}
    run = by["連勤の上限"]
    assert run["kind"] == "max_run" and run["params"]["shifts"] == "work" and run["params"]["max"] == 5
    for r in proposal.rules:
        assert r["meta"]["how"] == "貼った表から見つけた" and r["meta"]["evidence"]
    assert "毎日、早番が1人" in by["早番の人数"]["meta"]["evidence"]
    assert "公休は8人中8人が9日" in by["公休の数"]["meta"]["evidence"]


def test_infer_夜明が9割に満たなければ作らない():
    # 夜の翌日が明でない表。夜→明の候補は出ず、夜の next_is_ake は false
    n = 28
    t = "\n".join(f"{nm}\t" + "\t".join(r * 1 for r in (["夜", "公"] * 14)) for nm in ["赤坂", "井上"])
    pr = infer(parse_paste(t), 2027, 2)
    names = {r["name"] for r in pr.rules}
    assert "夜勤の翌日は明け" not in names
    assert next(s for s in pr.shift_types if s["symbol"] == "夜")["next_is_ake"] is False


def test_create_施設を作って12月の表ができる(tmp_path, proposal):
    root = tmp_path / "komorebi"
    c = create_facility(proposal, root, name="こもれびの家", next_year=2026, next_month=12)
    assert c.root == root and c.staff_count == 8 and c.rule_count == 9
    assert c.next_table_made and c.seconds >= 0
    for f in ("facility.json", "staff.json", "ledger.json", "months/2026-11.json", "months/2026-12.json"):
        assert (root / f).exists()
    store = Store(root)
    fac = store.facility()
    assert fac.name == "こもれびの家" and fac.id.isalnum()
    s = fac.settings
    assert set(s["solve"]) <= {"workers", "seed", "max_deterministic_time", "resolve_max_deterministic_time"}
    assert "seed_by_month" not in s["solve"] and "note" not in s.get("giveback", {})
    from shiftvoice.onboard import HOLIDAYS
    assert s["holidays"] == HOLIDAYS and s["holidays"]["2027-01"] == [1, 11]   # 翌々月以降も入る
    led = store.ledger()
    assert led.rev == 9 and led.txs[0]["meta"]["by"] == "作成者"
    nov, dec = store.month(2026, 11), store.month(2026, 12)
    assert nov.table == proposal.table and nov.prev_tail == {}
    assert dec.prev_tail == {k: v[-5:] for k, v in proposal.table.items()}
    assert dec.built_rev == led.rev and dec.table is not None and len(dec.table) == 8
    assert all(len(r) == 31 for r in dec.table.values())
    # 動かせないルールの外れが0
    from shiftvoice.build import month_rules
    rules = [r for r in month_rules(store, 2026, 12, with_premise=False) if r.flex == "fixed"]
    assert rules
    assert check_table(rules, fac, store.staff_on(2026, 12), dec, dec.table) == []


def test_create_翌月が貼った月の次でなければ作らない(tmp_path, proposal):
    with pytest.raises(OnboardError, match="翌月"):
        create_facility(proposal, tmp_path / "x", name="試験", next_year=2027, next_month=1)
    assert not (tmp_path / "x").exists()


def test_create_祝日の表に無い月は知らせる(tmp_path):
    n = 30
    t = "\n".join(f"{nm}\t" + "\t".join((["早", "日", "遅", "公", "公"] * 6)[:n])
                  for nm in ["赤坂", "井上", "内田", "遠藤", "太田", "川口"])
    pr = infer(parse_paste(t), 2027, 6)
    c = create_facility(pr, tmp_path / "h", name="試験", next_year=2027, next_month=7)
    assert any("祝日は入っていません" in x for x in c.notes)
    hol = Store(tmp_path / "h").facility().settings["holidays"]
    assert hol["2027-06"] == [] and hol["2027-07"] == [] and hol["2027-05"] == [3, 4, 5]


def test_create_知らない記号が残る時は作らない(tmp_path):
    head = "氏名\t" + "\t".join(str(d) for d in range(1, 29))
    t = head + "\n赤坂\t" + "\t".join(["早", "公", "半", "日"] * 7) + "\n井上\t" + "\t".join(["公", "遅", "日", "夜"] * 7)
    pr = infer(parse_paste(t), 2027, 2)
    assert pr.unknown_symbols == ["半"]
    root = tmp_path / "u"
    with pytest.raises(OnboardError, match="半"):
        create_facility(pr, root, name="試験", next_year=2027, next_month=3)
    assert not root.exists()
    with pytest.raises(OnboardError, match="答え"):
        create_facility(pr, root, name="試験", next_year=2027, next_month=3,
                        answers={"半": {"name": "半休", "kind": "ふつう"}})
    assert not root.exists()


# ---------------------------------------------------------------- 現場の書き方10通り（tests/fixtures/pastes/）

PASTES = Path(__file__).resolve().parent / "fixtures" / "pastes"
NAMES8 = ["赤坂", "井上", "内田", "遠藤", "太田", "川口", "木下", "工藤"]
NAMES_W = ["青木", "石田", "上野", "江口", "小川", "加藤", "岡田", "落合"]


def paste(n):
    return (PASTES / f"{n:02d}.tsv").read_text(encoding="utf-8")


def rule_map(pr):
    return {r["name"]: r for r in pr.rules}


def assert_komorebi_rules(pr):
    """見本と同じ表なので、見つかるルールも見本と同じ9件。"""
    by = rule_map(pr)
    assert len(pr.rules) == 9
    assert by["早番の人数"]["params"] == {"shifts": ["早"], "scope": "all", "op": "==", "n": 1}
    assert by["夜勤の人数"]["params"]["n"] == 1 and by["遅番の人数"]["params"]["n"] == 1
    assert by["公休の数"]["params"] == {"shifts": ["公"], "min": 9, "max": 9}
    assert by["夜勤の翌日は明け"]["params"] == {"if": ["夜"], "next_in": ["明"]}
    assert by["連勤の上限"]["params"]["max"] == 5


def made_next(tmp_path, pr, answers=None):
    c = create_facility(pr, tmp_path / "f", name="試験", next_year=2026, next_month=12, answers=answers)
    return c, Store(tmp_path / "f").month(2026, 12)


def test_貼り付け_全部の書き方で表が同じに読める(text, tmp_path):
    base = parse_paste(text).table
    for n in (1, 2, 6, 7, 10):
        p = parse_paste(paste(n))
        assert list(p.table) == NAMES8 and p.days == 30, n
        assert p.table == base, n
        assert p.unknown_symbols == [], n
    # 3番は記号がA/B/C/N。希→公、有はそのまま
    p3 = parse_paste(paste(3))
    assert list(p3.table) == NAMES8 and p3.days == 30 and p3.unknown_symbols == ["A", "B", "N", "C"]
    assert p3.aliases.get("希") == "公" and "希" not in {s for r in p3.table.values() for s in r}
    assert "有" in {s for r in p3.table.values() for s in r}


def test_貼り付け1_表題と曜日行と名前の空白(tmp_path):
    p = parse_paste(paste(1))
    assert p.skipped == 3 and list(p.table) == NAMES8   # 「赤 坂」の空白が取れる。集計3行は飛ばす
    pr = infer(p, 2026, 11)
    assert_komorebi_rules(pr)
    c, dec = made_next(tmp_path, pr)
    assert c.next_table_made and len(dec.table) == 8


def test_貼り付け2_番号と職種の列(tmp_path):
    pr = infer(parse_paste(paste(2)), 2026, 11)
    assert list(pr.table) == NAMES8 and pr.staff[0]["name"] == "赤坂"
    assert_komorebi_rules(pr)
    c, _ = made_next(tmp_path, pr)
    assert c.next_table_made


def test_貼り付け3_ABCNは同じにする答えで夜も入る(tmp_path):
    pasted = parse_paste(paste(3))
    answers = {"A": {"same_as": "早"}, "B": {"same_as": "日"}, "C": {"same_as": "遅"}, "N": {"same_as": "夜"}}
    pr = infer(pasted, 2026, 11, answers)
    assert pr.unknown_symbols == [] and pr.same_as == {"A": "早", "B": "日", "C": "遅", "N": "夜"}
    by = rule_map(pr)
    assert {"早番の人数", "遅番の人数", "夜勤の人数", "夜勤の翌日は明け"} <= set(by)
    assert by["夜勤の人数"]["params"]["n"] == 1
    c, dec = made_next(tmp_path, pr, answers)
    assert c.next_table_made and any("夜" in r for r in dec.table.values())
    assert not any("1回も入っていません" in x for x in c.notes)


def test_貼り付け4_丸数字は同じにする答えで夜も入る(tmp_path):
    pasted = parse_paste(paste(4))
    # 丸数字は数字に変えずに残す（本人の Excel と見比べられるように）
    assert pasted.unknown_symbols == ["①", "②", "④", "③"]
    answers = {"①": {"same_as": "早"}, "②": {"same_as": "日"}, "③": {"same_as": "遅"}, "④": {"same_as": "夜"}}
    pr = infer(pasted, 2026, 11, answers)
    by = rule_map(pr)
    assert by["早番の人数"]["params"]["n"] == 1 and by["遅番の人数"]["params"]["n"] == 1
    assert by["夜勤の人数"]["params"]["n"] == 1 and "夜勤の翌日は明け" in by
    c, dec = made_next(tmp_path, pr, answers)
    assert c.next_table_made and any("夜" in r for r in dec.table.values())


def test_貼り付け5_入は夜でアは同じにする答え(tmp_path):
    pasted = parse_paste(paste(5))
    assert pasted.aliases.get("入") == "夜" and pasted.unknown_symbols == ["ア"]
    answers = {"ア": {"same_as": "明"}}
    pr = infer(pasted, 2026, 11, answers)
    assert_komorebi_rules(pr)
    c, dec = made_next(tmp_path, pr, answers)
    assert c.next_table_made and any("夜" in r for r in dec.table.values())


def test_同じにする先は雛形の勤務だけ():
    with pytest.raises(OnboardError, match="知らない勤務"):
        infer(parse_paste(paste(5)), 2026, 11, {"ア": {"same_as": "ぬ"}})


def test_夜勤の記号を新しい勤務と答えると翌月に入らないと知らせる(tmp_path):
    pasted = parse_paste(paste(3))
    answers = {"A": {"same_as": "早"}, "B": {"same_as": "日"}, "C": {"same_as": "遅"},
               "N": {"name": "夜勤B", "kind": "work"}}
    pr = infer(pasted, 2026, 11, answers)
    c, dec = made_next(tmp_path, pr, answers)
    assert any("夜勤B（N）が12月の表に1回も入っていません。何の勤務かの答えを確かめてください" == x for x in c.notes)


def test_貼り付け6_明けは明(tmp_path):
    p = parse_paste(paste(6))
    assert p.aliases.get("明け") == "明" and p.table == parse_paste(SAMPLE.read_text(encoding="utf-8")).table
    assert_komorebi_rules(infer(p, 2026, 11))


def test_貼り付け7_右端の集計列は読まない(tmp_path):
    p = parse_paste(paste(7))
    assert p.days == 30 and p.skipped == 5 and list(p.table) == NAMES8
    assert_komorebi_rules(infer(p, 2026, 11))


def test_貼り付け8_締め日が月末でない表は作らない():
    with pytest.raises(ParseError) as e:
        parse_paste(paste(8))
    assert str(e.value) == ("締め日が月末でない表（16日から翌月15日など）は、まだ読めません。"
                            "1日から月末までの表を貼ってください")


def test_貼り付け9_ユニットごとの人数(tmp_path):
    p = parse_paste(paste(9))
    assert list(p.table) == NAMES8 + NAMES_W and p.unit_list == ["東", "西"]
    assert all(p.unit_of[n] == "東" for n in NAMES8) and all(p.unit_of[n] == "西" for n in NAMES_W)
    pr = infer(p, 2026, 11)
    assert pr.units == ["東", "西"] and {s["unit"] for s in pr.staff} == {"東", "西"}
    by = rule_map(pr)
    for nm in ("早番の人数", "遅番の人数", "夜勤の人数"):
        assert by[nm]["params"]["scope"] == "each_unit" and by[nm]["params"]["n"] == 1
    assert by["公休の数"]["params"] == {"shifts": ["公"], "min": 9, "max": 9}
    assert len(pr.rules) == 9
    c, dec = made_next(tmp_path, pr)
    assert c.next_table_made and len(dec.table) == 16
    assert Store(tmp_path / "f").facility().units == ["東", "西"]


def test_貼り付け10_途中の空の行と列と全角数字の見出し():
    p = parse_paste(paste(10))
    assert p.days == 30 and list(p.table) == NAMES8
    assert p.table == parse_paste(SAMPLE.read_text(encoding="utf-8")).table
    assert_komorebi_rules(infer(p, 2026, 11))


def test_見出しが無い貼り付けでも全部空の列は取り除く():
    rows = ["赤坂\t" + "\t".join(["早", "公"] * 14) + "\t",
            "井上\t" + "\t".join(["公", "早"] * 14) + "\t"]
    p = parse_paste("\n".join(rows))
    assert p.days == 28 and not p.had_header


def test_名前の列は見出しか中身で決まる():
    days = [str(d) for d in range(1, 29)]
    body = lambda nm, role: [str(1), role, nm] + ["早", "公"] * 14   # noqa: E731
    t = "\n".join(["番号\t職種\t名前\t" + "\t".join(days), "\t".join(body("赤坂", "介護福祉士")),
                   "\t".join(["2", "パート", "井上"] + ["公", "早"] * 14)])
    assert list(parse_paste(t).table) == ["赤坂", "井上"]
    t2 = t.replace("名前", "")      # 見出しが無くても、職種の言葉と数字だけの列は名前にしない
    assert list(parse_paste(t2).table) == ["赤坂", "井上"]


def test_パートの人の公休は人ごとに1本_翌月も同じ数(tmp_path):
    # 8人は公休9日、1人（パート）は空欄が多く公休20日。翌月もその人は20日
    t = parse_paste(SAMPLE.read_text(encoding="utf-8"))
    lines = SAMPLE.read_text(encoding="utf-8").split("\n")
    part = "青木\t" + "\t".join(["早", "公"] * 10 + [""] * 10)
    pr = infer(parse_paste("\n".join(lines[:10] + [part] + lines[10:])), 2026, 11)
    by = rule_map(pr)
    assert by["公休の数"]["who"] == NAMES8 and by["公休の数"]["params"]["min"] == 9
    one = by["公休の数（青木さん）"]
    assert one["who"] == ["青木"] and one["params"]["min"] == one["params"]["max"] == 20
    assert "12月は31日あります。公休の数を確かめてください" in one["meta"]["evidence"]
    assert len(t.table) == 8
    c, dec = made_next(tmp_path, pr)
    assert c.next_table_made and dec.table["青木"].count("公") == 20


def test_言葉の一覧に無い集計の行も飛ばす():
    lines = SAMPLE.read_text(encoding="utf-8").split("\n")
    extra = ["出勤者数\t" + "\t".join(["3"] * 30), "公休数\t" + "\t".join(["2"] * 30 ),
             "夜勤者数\t" + "\t".join(["1"] * 30) + "\t5"]
    p = parse_paste("\n".join(lines[:10] + extra))
    assert list(p.table) == NAMES8 and p.skipped >= 3
