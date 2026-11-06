"""Codex Astra の4回目の敵対レビューの線超え5件。応援の取り消し・コピーの行き先・貼り戻しの位置・100人目・配った後の応援。
施設と職員はすべて作り話（ひだまりの郷、3ユニットは 職員001 の形）。"""
import csv
import io
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from shiftvoice.model import Facility, Staff
from shiftvoice.names import Masker
from shiftvoice.onboard import create_facility, infer, parse_paste
from shiftvoice.public import kana_name_like, name_like
from shiftvoice.reader import RecordedReader
from shiftvoice.server import make_handler, table_tsv
from shiftvoice.session import Session, only_shift_items, support_items
from shiftvoice.store import Store

from conftest import FIXED_NOW, REPLIES

PASTES = Path(__file__).parent / "fixtures" / "pastes"
SAID = "12日、青木さんは西の遅番に入って"
CANCEL = '{"items":[{"type":"cancel","staff":"職員01","days":[12],"shift":"遅"}]}'


# ── 1 応援を取り消すと足りない所を返事で知らせる
def test_応援を取り消すと足りないユニットを返事で知らせる(sess):
    sess.open()
    sess.say(SAID)
    sess.reader.replies["職員01さんの12日の遅番、やっぱりいらない"] = CANCEL
    r = sess.say("青木さんの12日の遅番、やっぱりいらない")
    assert r.kind == "applied", r.message
    assert "12日の西は遅番の人数が足りません（0人）" in r.warning
    assert "12日の東は遅番の人数が多くなりました（2人）" in r.warning
    assert sess.store.month(2026, 11).support == {}


def test_日付を言わない取り消しを選んだ時も足りないと知らせる(sess):
    sess.open()
    sess.say(SAID)
    reply = '{"items":[{"type":"cancel","staff":"職員01","days":null,"shift":"遅"}]}'
    sess.reader.replies["職員01さんの遅番、やっぱりいらない"] = reply
    r = sess.say("青木さんの遅番、やっぱりいらない")
    assert r.kind == "fix_offer", r.message
    done = sess.accept(r.uid)
    assert done.kind == "applied" and "12日の西は遅番の人数が足りません" in done.warning


def test_応援でない頼みの取り消しは今までどおり何も足さない(sess):
    sess.open()
    reply = '{"items":[{"type":"day_off","staff":"職員01","days":[12],"reason":null}]}'
    sess.reader.replies["職員01さん、12日は休みで"] = reply
    assert sess.say("青木さん、12日は休みで").kind == "applied"
    sess.reader.replies["職員01さんの12日の休み、やっぱりいらない"] = \
        '{"items":[{"type":"cancel","staff":"職員01","days":[12],"shift":null}]}'
    r = sess.say("青木さんの12日の休み、やっぱりいらない")
    assert r.kind == "applied", r.message
    assert not r.warning


# ── 2 コピーの見出しの行に行き先を入れる
def _st3(support):
    names = {"東": ["青木", "石田"], "西": ["上野"], "南": ["江口"]}
    return {"staff": [{"name": n, "unit": u} for u, ns in names.items() for n in ns], "days": 28,
            "weekdays": ["sun", "mon", "tue", "wed", "thu", "fri", "sat"] * 4,
            "table": {n: ["日"] * 28 for ns in names.values() for n in ns}, "layers": {}, "support": support}


def test_コピーの見出しの行に行き先が入る_同じ日に別のユニットへ出ても分かる():
    st = _st3([{"name": "青木", "day": 12, "from": "東", "to": "西", "arrow": "↓"},
               {"name": "石田", "day": 12, "from": "東", "to": "南", "arrow": "↓"},
               {"name": "上野", "day": 13, "from": "西", "to": "東", "arrow": "↑"}])
    rows = [r.split("\t") for r in table_tsv(st).splitlines()]
    east = next(r for r in rows if r[0] == "東ユニット")
    west = next(r for r in rows if r[0] == "西ユニット")
    assert east[12] == "↓西↓南" and west[13] == "↑東"


def test_同じ日に同じ行き先へ2人出ても行き先は1回だけ書く():
    st = _st3([{"name": "青木", "day": 12, "from": "東", "to": "西", "arrow": "↓"},
               {"name": "石田", "day": 12, "from": "東", "to": "西", "arrow": "↓"}])
    rows = [r.split("\t") for r in table_tsv(st).splitlines()]
    assert next(r for r in rows if r[0] == "東ユニット")[12] == "↓西"


def test_行き先の入ったコピーを貼り直しても見出しのまま読める():
    st = _st3([{"name": "青木", "day": 12, "from": "東", "to": "西", "arrow": "↓"},
               {"name": "石田", "day": 12, "from": "東", "to": "南", "arrow": "↓"},
               {"name": "上野", "day": 13, "from": "西", "to": "東", "arrow": "↑"}])
    tsv = table_tsv(st)
    p = parse_paste(tsv)
    assert p.unit_list == ["東", "西", "南"]
    assert p.unit_of == {"青木": "東", "石田": "東", "上野": "西", "江口": "南"}
    # 日の見出しの行が無い貼り方でも、見出しの行を人に読まない
    p2 = parse_paste("\n".join(tsv.splitlines()[2:]) + "\n")
    assert sorted(p2.unit_list) == ["南", "東", "西"] and set(p2.table) == {"青木", "石田", "上野", "江口"}


# ── 3 取り込んだ施設は貼った時の並べ方でコピーを出す
def text(n):
    return (PASTES / (f"{n:02d}.tsv" if isinstance(n, int) else f"{n}.tsv")).read_text(encoding="utf-8")


def imported(tmp_path, n):
    prop = infer(parse_paste(text(n)), 2026, 11)
    create_facility(prop, tmp_path / "f", name="試験", next_year=2026, next_month=12)
    store = Store(tmp_path / "f")
    s = Session(store, 2026, 12, RecordedReader.from_file(REPLIES), now=lambda: FIXED_NOW)
    s.open()
    return store, s


def export(store, s):
    f = store.facility().settings
    return [r.split("\t") for r in table_tsv(s.state(), f.get("paste_symbols"), f.get("paste_layout")).splitlines()]


def pasted_grid(n):
    """貼った表を、日の見出しの行（曜日が上ならその行）から下の行だけにした物。"""
    delim = "," if "\t" not in text(n) else "\t"
    rows = [r for r in csv.reader(io.StringIO(text(n)), delimiter=delim) if any(c.strip() for c in r)]
    top = next(i for i, r in enumerate(rows) if any(c.strip() in ("氏名", "職員名") for c in r))
    return rows[top:]


@pytest.mark.parametrize("n", ["units3", 2, 7, 9, 10, 19])
def test_貼った表と同じ行と列に人と日が並ぶ(tmp_path, n):
    store, s = imported(tmp_path, n)
    got, want = export(store, s), pasted_grid(n)
    col = next(j for j, c in enumerate(want[0]) if c.strip() in ("氏名", "職員名"))
    names = list(s.store.month(2026, 12).table)
    for name in names:
        row = next(i for i, r in enumerate(want) if r[col].replace(" ", "") == name)
        assert got[row][col].replace(" ", "") == name, (name, row)
    # 1日と15日の列も同じ場所（貼った表の見出しの字は違っても、日の番号の位置がそろう）
    first = next(j for j, c in enumerate(want[0]) if j > col and c.strip())
    assert got[0][first] == "1" and got[0][:first] == want[0][:first]


def test_貼った表の左の列の中身はそのまま出す(tmp_path):
    store, s = imported(tmp_path, 2)
    got, want = export(store, s), pasted_grid(2)
    assert got[1][:3] == want[1][:3] and got[2][:3] == want[2][:3]    # No・職種・氏名


def test_ユニットの列の表は見出しの行を足さず_日の見出しと曜日の順も同じ(tmp_path):
    store, s = imported(tmp_path, 19)
    got = export(store, s)
    assert got[0][:3] == ["ユニット", "氏名", "1"]
    assert got[1][:2] == ["", ""] and got[1][2] == "火"                  # 12月1日は火曜
    assert got[2][:2] == ["さくら", "青木"]
    assert not any(r[0].endswith("ユニット") and r[0] != "ユニット" for r in got)


def test_曜日の行が無い表は曜日の行を足さない(tmp_path):
    store, s = imported(tmp_path, "units3")
    got = export(store, s)
    assert got[0][:2] == ["氏名", "1"] and got[1][0] == "東ユニット"


def test_貼った形のコピーをもう一度取り込んでも同じ人と勤務に読める(tmp_path):
    for i, n in enumerate(["units3", 19, 9]):
        store, s = imported(tmp_path / str(i), n)
        st = s.state()
        f = store.facility().settings
        p = parse_paste(table_tsv(st, f.get("paste_symbols"), f.get("paste_layout")))
        assert p.unit_of == {x["name"]: x["unit"] for x in st["staff"]}
        assert p.table == st["table"]


def test_見本の施設と標準様式は今の形のまま(sess, tmp_path):
    sess.open()
    assert "paste_layout" not in sess.store.facility().settings
    src = Path(__file__).parent / "fixtures" / "standard_form_gh30.tsv"
    assert parse_paste(src.read_text(encoding="utf-8")).layout is None


def _served(s):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(s, threading.Lock(), True, None))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def test_サーバーのコピーも貼った形で_応援の見出しが無い形はそう知らせる(tmp_path):
    store, s = imported(tmp_path, 19)
    units = store.facility().units
    other = next(u for u in units if u != s.state()["staff"][0]["unit"])
    m = store.month(2026, 12)
    name = s.state()["staff"][0]["name"]
    day = next(d for d in range(1, 32) if m.table[name][d - 1] in ("早", "日", "遅"))
    m.support = {f"{name}/{day}": other}
    store.save_month(m)
    httpd = _served(s)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/api/table.tsv") as r:
            body, left = r.read().decode("utf-8"), r.headers.get("X-Support-Left-Out")
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert body.splitlines()[0].split("\t")[:2] == ["ユニット", "氏名"]
    assert left == "1"


def test_見出しの行がある形は応援が入るので知らせない(sess):
    sess.open()
    sess.say(SAID)
    httpd = _served(sess)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/api/table.tsv") as r:
            body, left = r.read().decode("utf-8"), r.headers.get("X-Support-Left-Out")
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert "↓西" in body and left is None


# ── 4 100人目を10人目にしない
def _big(n):
    fac = Facility.from_dict({"id": "t", "name": "試験", "units": ["東", "西"], "settings": {}, "shift_types": [
        {"symbol": "早", "name": "早番", "kind": "work"}, {"symbol": "日", "name": "日勤", "kind": "work"},
        {"symbol": "遅", "name": "遅番", "kind": "work"}, {"symbol": "公", "name": "公休", "kind": "rest"}]})
    staff = [Staff(f"担当{i:03d}", "東" if i <= n // 2 else "西", ["早", "日", "遅"]) for i in range(1, n + 1)]
    return fac, Masker(staff, fac)


def test_100人の名簿で100人目への応援は100人目に当たる():
    fac, mk = _big(100)
    masked, _ = mk.mask("12日、担当100さんは東の遅番に入って")
    got = support_items(masked, fac)
    assert got and mk.unmask(got[0]["staff"]) == "担当100" and got[0]["days"] == [12]
    masked, _ = mk.mask("担当100さん15日は東へ応援")
    got = support_items(masked, fac)
    assert got and mk.unmask(got[0]["staff"]) == "担当100" and got[0]["days"] == [15]


def test_100人の名簿でも1人目と10人目はそのまま():
    fac, mk = _big(100)
    for name in ("担当001", "担当010"):
        masked, _ = mk.mask(f"12日、{name}さんは西の遅番に入って")
        got = support_items(masked, fac)
        assert got and mk.unmask(got[0]["staff"]) == name


def test_100人の名簿で番号と日付がつながらず名前の見張りに数字が残らない():
    fac, mk = _big(100)
    masked, _ = mk.mask("担当10015日休み")
    assert " 15" in masked and mk.unmask(masked.split(" ")[0]) == "担当100"
    assert name_like(masked) is None and kana_name_like(masked) is None


def test_100人の名簿でも日勤だけの一言は本人に当たる():
    fac, mk = _big(100)
    masked, _ = mk.mask("担当100 12月から日勤だけ")
    got = only_shift_items(masked, fac)
    assert got and mk.unmask(got[0]["staff"]) == "担当100" and got[0]["from_month"] == 12


def test_99人までの番号は2桁のまま_番号の後の日付も読める():
    fac, mk = _big(99)
    assert mk.to_code["担当001"] == "職員01" and mk.to_code["担当099"] == "職員99"
    masked, _ = mk.mask("担当01715日は西へ応援")
    got = support_items(masked, fac)
    assert got and mk.unmask(got[0]["staff"]) == "担当017" and got[0]["days"] == [15]
    masked, _ = mk.mask("担当001 12月から日勤だけ")
    got = only_shift_items(masked, fac)
    assert got and mk.unmask(got[0]["staff"]) == "担当001" and got[0]["from_month"] == 12


def test_名前の後の読点と日付がつながっても番号が1桁ずれない():
    fac, mk = _big(99)
    got = support_items(mk.mask("担当001、15日は西の遅番に入って")[0], fac)
    assert got and mk.unmask(got[0]["staff"]) == "担当001" and got[0]["days"] == [15]
    got = only_shift_items(mk.mask("担当001、12月から日勤だけ")[0], fac)
    assert got and mk.unmask(got[0]["staff"]) == "担当001" and got[0]["from_month"] == 12
    fac, mk = _big(100)
    got = support_items(mk.mask("担当100、15日は東の遅番に入って")[0], fac)
    assert got and mk.unmask(got[0]["staff"]) == "担当100" and got[0]["days"] == [15]


# ── 5 配った後の応援の変更を数える
def _work_day(sess):
    m = sess.store.month(2026, 11)
    return next(d for d in range(1, 31) if m.table["青木"][d - 1] == "日")


def test_配った後に応援先だけ変えると案が0マスと言わず_配った表からの差に数える(sess):
    sess.open()
    d = _work_day(sess)
    sess.distribute()
    assert sess.state()["dist_changed"] == 0
    r = sess.say(f"{d}日、青木さんは西へ応援")
    assert r.kind == "fix_offer", r.message
    assert "0マス" not in r.message and "応援先" in r.message and "青木さん" in r.message
    assert sess.accept(r.uid).kind == "applied"
    assert sess.state()["dist_changed"] == 1
    sess.undo()
    assert sess.state()["dist_changed"] == 0


def test_案を開き直しても同じ文で出す(sess):
    sess.open()
    d = _work_day(sess)
    sess.distribute()
    r = sess.say(f"{d}日、青木さんは西へ応援")
    offer = next(u for u in sess.store.pending(2026, 11) if u["id"] == r.uid)["offer"]
    assert offer["message"] == r.message


def test_勤務も応援も変わったマスは1マスに数える(sess):
    sess.open()
    sess.distribute()
    r = sess.say(SAID)
    assert r.kind == "fix_offer", r.message
    n = len(r.changes)
    assert f"{n}マス動きます" in r.message
    sess.accept(r.uid)
    assert sess.state()["dist_changed"] == n


def test_前の版で配った月は今ある応援を差に数えない(sess):
    sess.open()
    sess.distribute()
    m = sess.store.month(2026, 11)
    m.support = {"青木/12": "西"}
    m.dist_support = None
    sess.store.save_month(m)
    assert sess.state()["dist_changed"] == 0
