"""ユニットの続き。人数のルールの優先（Astra の指摘2・3）、ユニット間の行き来（応援）、作者の Excel に合わせた並べ方。
施設と職員はすべて作り話（ひだまりの郷、3ユニットは 職員001 の形）。"""
from pathlib import Path

from shiftvoice.build import month_rules
from shiftvoice.daycount import day_counts
from shiftvoice.model import Facility, Month, Rule, Staff
from shiftvoice.onboard import create_facility, infer, parse_paste
from shiftvoice.reader import RecordedReader
from shiftvoice.rules import expand, with_overrides
from shiftvoice.server import table_tsv
from shiftvoice.session import Session, support_items
from shiftvoice.store import Store

from conftest import FIXED_NOW

PASTE = Path(__file__).parent / "fixtures" / "pastes" / "units3.tsv"
U = {"東": [f"職員{i:03d}" for i in range(1, 7)],
     "西": [f"職員{i:03d}" for i in range(7, 12)],
     "南": [f"職員{i:03d}" for i in range(12, 17)]}


def _fac3() -> Facility:
    return Facility.from_dict({"id": "t", "name": "試験", "units": ["東", "西", "南"], "settings": {}, "shift_types": [
        {"symbol": "早", "name": "早番", "kind": "work"}, {"symbol": "日", "name": "日勤", "kind": "work"},
        {"symbol": "遅", "name": "遅番", "kind": "work"}, {"symbol": "夜", "name": "夜勤", "kind": "work", "next_is_ake": True},
        {"symbol": "明", "name": "明け", "kind": "ake"}, {"symbol": "公", "name": "公休", "kind": "rest"}]})


def _cov(rid, op, n, units=None, shift="日", flex="fixed"):
    p = {"shifts": [shift], "scope": "each_unit", "op": op, "n": n}
    if units:
        p["units"] = units
    return Rule(rid, 1, rid, "coverage", flex, p)


def _spans(rules, unit, day=1):
    """そのユニットのその日の場面の（下限, 上限）を、効いているルールの全部から集める。"""
    fac = _fac3()
    staff = [Staff(n, u, ["早", "日", "遅", "夜"]) for u, ns in U.items() for n in ns]
    m = Month(2026, 12, [], {})
    return sorted(((i.lo, i.hi) for r in with_overrides(rules) for i in expand(r, fac, staff, m)
                   if i.key == f"{unit}/{day}"), key=str)


# ── A 指摘2 ユニットの上限を足しても、共通の下限は残る
def test_ユニットの上限を足しても共通の下限が消えない():
    rules = [_cov("r1", ">=", 2), _cov("r2", "<=", 3, ["東"])]
    assert (2, None) in _spans(rules, "東") and (None, 3) in _spans(rules, "東")
    assert _spans(rules, "西") == [(2, None)]


def test_ユニットの下限を足しても共通の上限が残る():
    rules = [_cov("r1", "<=", 3), _cov("r2", ">=", 2, ["東"])]
    assert (None, 3) in _spans(rules, "東") and (2, None) in _spans(rules, "東")


def test_言っていない側でも値がぶつかる時は共通の側を外す():
    # 共通は2人以上。東は1人まで＝共通の下限の2人とぶつかるので、東だけ下限を外す（名指しが優先）
    assert _spans([_cov("r1", ">=", 2), _cov("r2", "<=", 1, ["東"])], "東") == [(None, 1)]
    # ちょうどの名指しは、共通の上限1人とぶつかるので外す。下限の1人は2人と両立するので残す
    assert _spans([_cov("r1", "==", 1), _cov("r2", "==", 2, ["東"])], "東") == [(1, None), (2, 2)]
    # 人を減らす名指しは、共通の下限を外す
    assert _spans([_cov("r1", "==", 2), _cov("r2", "==", 1, ["東"])], "東") == [(1, 1), (None, 2)]


# ── A 指摘3 まとめて取り込んだユニットの一部だけを変えられる
def test_まとめたユニットの一部だけを新しいルールで変えられる():
    rules = [_cov("r1", "==", 1, ["西", "南"]), _cov("r2", "==", 2, ["西"])]
    assert _spans(rules, "西") == [(1, None), (2, 2)]      # 上限の1人は残らない
    assert _spans(rules, "南") == [(1, 1)]


def test_後から広いユニットを言ったら新しい方が効く():
    rules = [_cov("r1", "==", 2, ["西"]), _cov("r2", "==", 3, ["西", "南"])]
    assert _spans(rules, "西") == [(2, None), (3, 3)]
    assert _spans(rules, "南") == [(3, 3)]


def test_名指しの片側だけの変更はまとめたルールのもう片側を残す():
    rules = [_cov("r1", ">=", 1, ["西", "南"]), _cov("r2", "<=", 2, ["西"])]
    assert (1, None) in _spans(rules, "西") and (None, 2) in _spans(rules, "西")


def _made(tmp_path):
    pr = infer(parse_paste(PASTE.read_text(encoding="utf-8")), 2026, 11)
    create_facility(pr, tmp_path / "f", name="試験", next_year=2026, next_month=12)
    return Store(tmp_path / "f")


def test_取り込んだ西と南の早番のうち西だけを一言で2人にできる(tmp_path):
    st = _made(tmp_path)
    said = "西ユニットの早番は毎日2人にして。毎月"
    reply = '{"items":[{"type":"coverage","shift":"早","min":2,"max":2,"weekdays":null,"strength":"operational"}]}'
    s = Session(st, 2026, 12, RecordedReader({"ユニット2の早番は毎日2人にして。毎月": reply}), now=lambda: FIXED_NOW)
    s.open()
    r = s.say(said)
    assert r.kind == "applied", r.message
    tab = st.month(2026, 12).table
    west = [sum(1 for n in U["西"] if tab[n][d] == "早") for d in range(31)]
    assert all(sum(1 for n in U["南"] if tab[n][d] == "早") == 1 for d in range(31))   # 南は1人のまま
    # 西は5人なので毎日2人は無理な日があり、運用の都合として外れた日はそう知らせる。外れた日も元の1人は下回らない
    assert min(west) >= 1 and west.count(2) >= 20
    assert "守れませんでした" in r.warning
    rows = day_counts(st.facility(), st.staff_on(2026, 12), st.month(2026, 12), month_rules(st, 2026, 12, with_premise=False))
    row = next(x for x in rows if x["unit"] == "西" and x["symbol"] == "早")
    assert row["over"] == [] and row["short"] == [d + 1 for d, n in enumerate(west) if n < 2]


# ── C 応援の読み取り（手元で読む。Claude へ送らない）
def fac2():
    return Store(Path(__file__).resolve().parents[1] / "data" / "hidamari").facility()


def test_応援の言い方を手元で読む():
    f = fac2()
    def one(t):
        got = support_items(t, f)
        return got and (got[0]["staff"], got[0]["days"], got[0]["shift"], got[0]["unit"])
    assert one("12日、職員01さんはユニット2の遅番に入って") == ("職員01", [12], "遅", "ユニット2")
    assert one("12日、ユニット1の職員01さんはユニット2の遅番に入って") == ("職員01", [12], "遅", "ユニット2")
    assert one("職員01さん、12日と13日はユニット2の早番に入ってください") == ("職員01", [12, 13], "早", "ユニット2")
    assert one("12日から14日、職員01さんはユニット2の遅番へ応援") == ("職員01", [12, 13, 14], "遅", "ユニット2")
    assert one("12日、職員01さんはユニット2へ応援") == ("職員01", [12], None, "ユニット2")
    assert one("12日は遅番で職員01さんがユニット2にヘルプ") == ("職員01", [12], "遅", "ユニット2")
    assert one("12日のユニット2の遅番は職員01さんに入ってもらう") == ("職員01", [12], "遅", "ユニット2")


def test_応援でない一言は手元で読まない():
    f = fac2()
    for t in ("ユニット2の遅番は毎日2人にして", "12日、職員01さんは遅番にして", "職員01さんはユニット2の遅番に入らないで",
              "12日、職員01さんはユニット2の遅番に入らないで", "12日にユニット2の遅番に職員01さんと職員02さんが入って",
              "毎週木曜は職員01さんがユニット2の遅番に入って", "12日、職員01さんがユニット2の遅番に入ってくれた",
              "12日の応援はやっぱり取り消して"):
        assert support_items(t, f) is None, t


# ── C 応援を入れる・数える・戻す
SAID = "12日、青木さんは西の遅番に入って"


def _late(s, unit, d, staff=None):
    st, m = s.store, s.store.month(s.year, s.month)
    rows = day_counts(st.facility(), st.staff_on(s.year, s.month), m, month_rules(st, s.year, s.month, with_premise=False))
    return next(r for r in rows if r["unit"] == unit and r["symbol"] == "遅")["counts"][d - 1]


def test_応援を言うとその日は応援先の人数に数え元のユニットから外す(sess):
    sess.open()
    r = sess.say(SAID)
    assert r.kind == "applied", r.message
    assert r.sent is None                                   # Claude へは送らない
    m = sess.store.month(2026, 11)
    assert m.table["青木"][11] == "遅" and m.support == {"青木/12": "西"}
    st = sess.state()
    assert st["support"] == [{"name": "青木", "day": 12, "from": "東", "to": "西", "arrow": "↓"}]
    # 人数のルール（遅番は各ユニット1人）の側で、青木さんは西に数える。東も西も1人のまま
    tab = m.table
    west = [n for n in tab if m.support.get(f"{n}/12", next(x.unit for x in sess.store.staff() if x.name == n)) == "西"]
    assert "青木" in west and sum(1 for n in west if tab[n][11] == "遅") == _late(sess, "西", 12)
    assert _late(sess, "西", 12) == 1 and _late(sess, "東", 12) == 1


def test_ひとつ戻すで応援も表も戻る(sess):
    sess.open()
    before = {k: list(v) for k, v in sess.store.month(2026, 11).table.items()}
    assert sess.say(SAID).kind == "applied"
    sess.undo()
    m = sess.store.month(2026, 11)
    assert m.support == {} and m.table == before and sess.state()["support"] == []


def test_同じ人の同じ日に別の勤務を言うと応援は外れる(sess):
    sess.open()
    sess.say(SAID)
    reply = '{"items":[{"type":"day_off","staff":"職員01","days":[12],"reason":null}]}'
    sess.reader.replies["職員01さん、12日は休みで"] = reply
    assert sess.say("青木さん、12日は休みで").kind == "applied"
    assert sess.store.month(2026, 11).support == {}
    sess.undo()
    assert sess.store.month(2026, 11).support == {"青木/12": "西"}


def test_取り消すと応援も外れる(sess):
    sess.open()
    sess.say(SAID)
    reply = '{"items":[{"type":"cancel","staff":"職員01","days":[12],"shift":"遅"}]}'
    sess.reader.replies["職員01さんの12日の遅番、やっぱりいらない"] = reply
    assert sess.say("青木さんの12日の遅番、やっぱりいらない").kind == "applied"
    assert sess.store.month(2026, 11).support == {}
    sess.undo()
    assert sess.store.month(2026, 11).support == {"青木/12": "西"}


def test_自分のユニットを言った時は応援にしない(sess):
    sess.open()
    r = sess.say("12日、青木さんは東の遅番に入って")
    assert r.kind == "applied", r.message
    m = sess.store.month(2026, 11)
    assert m.table["青木"][11] == "遅" and m.support == {} and sess.state()["support"] == []


def test_勤務を言わない応援はその日の勤務のまま_休みの日は聞き返す(sess):
    sess.open()
    m = sess.store.month(2026, 11)
    work = next(d for d in range(1, 31) if m.table["青木"][d - 1] in ("早", "日", "遅"))
    sym = m.table["青木"][work - 1]
    r = sess.say(f"{work}日、青木さんは西へ応援")
    assert r.kind == "applied", r.message
    m = sess.store.month(2026, 11)
    assert m.table["青木"][work - 1] == sym and m.support == {f"青木/{work}": "西"}
    off = next(d for d in range(1, 31) if m.table["青木"][d - 1] == "公")
    r = sess.say(f"{off}日、青木さんは西へ応援")
    assert r.kind == "ask" and "何の勤務" in r.message
    assert sess.store.month(2026, 11).support == {f"青木/{work}": "西"}


def test_配った後の応援は案を選んでから入る(sess):
    sess.open()
    sess.distribute()
    r = sess.say(SAID)
    assert r.kind == "fix_offer", r.message
    assert sess.store.month(2026, 11).support == {}
    sess.accept(r.uid)
    assert sess.store.month(2026, 11).support == {"青木/12": "西"}
    sess.undo()
    assert sess.store.month(2026, 11).support == {}


def test_ユニットが1つの施設では応援を読まない():
    f = Facility.from_dict({**{"id": "t", "name": "試験", "settings": {}}, "units": ["全体"],
                            "shift_types": [{"symbol": "遅", "name": "遅番", "kind": "work"}]})
    assert support_items("12日、職員01さんはユニット1の遅番に入って", f) is None


# ── B 公休の行とコピーの並べ方
def test_施設全体の行に公休の人数がある(sess):
    sess.open()
    st = sess.state()
    rest = [r for r in st["day_counts"] if r["symbol"] == "公"]
    assert [r["unit"] for r in rest] == [None]
    tab = st["table"]
    assert rest[0]["counts"][0] == sum(1 for row in tab.values() if row[0] == "公")
    assert st["day_counts"][-1]["unit"] is None


def test_コピーは曜日と日付の下にユニットの見出しの行を挟む(sess):
    sess.open()
    sess.say(SAID)
    st = sess.state()
    rows = [r.split("\t") for r in table_tsv(st).splitlines()]
    assert rows[0][1:3] == ["日", "月"] and rows[1][1:3] == ["1", "2"]
    east, west = rows[2], rows[3 + 8]
    assert east[0] == "東ユニット" and west[0] == "西ユニット"
    assert east[12] == "↓" and all(c == "" for i, c in enumerate(east[1:], 1) if i != 12)
    assert rows[3][0] == "青木" and rows[3][12] == "遅"       # マスは記号だけ（Excel に貼れる形のまま）
    assert all(len(r) == 1 + st["days"] for r in rows)


def test_コピーした表をそのまま取り込むと同じユニットに読める(sess):
    sess.open()
    sess.say(SAID)
    st = sess.state()
    p = parse_paste(table_tsv(st))
    assert p.unit_list == ["東", "西"] and p.days == st["days"]
    assert {s["name"]: s["unit"] for s in st["staff"]} == p.unit_of
    assert p.table["青木"] == st["table"]["青木"]


def test_録った一言と言い換えの一覧は応援に読まない():
    """手元の読み取りが、今まで Claude へ送っていた一言を横取りしない。"""
    import json
    root = Path(__file__).resolve().parents[1]
    said = list(json.loads((root / "tests" / "fixtures" / "replies.json").read_text(encoding="utf-8")))
    said += list(json.loads((root / "tools" / "heldout.json").read_text(encoding="utf-8")))
    f = fac2()
    assert len(said) > 40 and [t for t in said if support_items(t, f)] == []
