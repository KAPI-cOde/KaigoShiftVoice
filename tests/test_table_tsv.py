"""表の持ち出し（GET /api/table.tsv）。行数・列数・記号が画面の表と同じで、待機当番の行が最後に付く。Claude は呼ばない。"""
import shutil
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from conftest import FIXED_NOW, REPLIES
from shiftvoice.reader import RecordedBuilder, RecordedReader
from shiftvoice.server import make_handler, table_tsv
from shiftvoice.session import Session
from shiftvoice.store import Store
from test_act2 import BUILDS, STANDBY
from test_t1_act1 import ACT1

WD = {"sun": "日", "mon": "月", "tue": "火", "wed": "水", "thu": "木", "fri": "金", "sat": "土"}


def fetch(base, headers=None):
    req = urllib.request.Request(base + "/api/table.tsv", headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.headers.get("Content-Type"), r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type"), e.read().decode("utf-8")


@pytest.fixture
def served(built_dir, tmp_path):
    root = tmp_path / "hidamari"
    shutil.copytree(built_dir, root)
    session = Session(Store(root), 2026, 11, RecordedReader.from_file(REPLIES), now=lambda: FIXED_NOW)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(session, threading.Lock(), True, None))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", session
    httpd.shutdown()
    httpd.server_close()


def grid(text):
    assert text.endswith("\n")
    return [line.split("\t") for line in text[:-1].split("\n")]


def test_形は人数と日数どおりで記号は表と同じ(served):
    base, session = served
    code, ctype, text = fetch(base)
    assert code == 200 and ctype.startswith("text/tab-separated-values")
    st = session.state()
    rows = grid(text)
    units = list(dict.fromkeys(s["unit"] for s in st["staff"]))
    # 見出し2行＋ユニットの見出しの行（ユニットが2つ以上ある時だけ）＋人数（この日は待機の欄なし）
    assert len(rows) == 2 + (len(units) if len(units) > 1 else 0) + len(st["staff"])
    assert all(len(r) == 1 + st["days"] for r in rows)
    assert rows[0][1:] == [WD[w] for w in st["weekdays"]]
    assert rows[1][1:] == [str(d) for d in range(1, st["days"] + 1)]
    by_name = {r[0]: r for r in rows[2:]}
    for s in st["staff"]:
        assert by_name[s["name"]][1:] == [st["table"][s["name"]][d] for d in range(st["days"])]
    if len(units) > 1:
        heads = [i for i, r in enumerate(rows) if r[0].endswith("ユニット")]
        assert [rows[i][0] for i in heads] == [u + "ユニット" for u in units]
        for i, u in zip(heads, units):            # 見出しの下に、そのユニットの職員が続く
            n = sum(1 for s in st["staff"] if s["unit"] == u)
            assert {r[0] for r in rows[i + 1:i + 1 + n]} == {s["name"] for s in st["staff"] if s["unit"] == u}


def test_待機当番の行が最後に付く(served):
    base, session = served
    session.builder = RecordedBuilder.from_file(BUILDS)
    session.base_date = "2026-11-01"
    for t, ans in ACT1:
        r = session.say(t)
        if r.kind == "ask_period":
            session.answer(r.uid, ans)
    session.say(STANDBY)
    st = session.state()
    assert st["layers"]
    rows = grid(fetch(base)[2])
    lead = 1
    heads = len({s["unit"] for s in st["staff"]})
    assert len(rows) == len(st["staff"]) + 2 + (heads if heads > 1 else 0) + len(st["layers"])
    assert all(len(r) == lead + st["days"] for r in rows)
    layer = next(iter(st["layers"].values()))
    last = rows[-1]
    assert last[lead - 1] == layer["name"]
    for d in range(1, st["days"] + 1):
        who = [n.removesuffix("さん") for n in layer["assign"].get(str(d), [])]
        assert last[lead - 1 + d] == "・".join(who)
    assert any(c for c in last[lead:])           # 土日には担当の名前が入る


def test_タブと改行を含む名前でも列がずれない():
    st = {"staff": [{"name": "あ\tい\n", "unit": "A"}], "days": 2, "weekdays": ["sat", "sun"],
          "table": {"あ\tい\n": ["早", "休"]}, "layers": {}}
    rows = grid(table_tsv(st))
    assert len(rows) == 3 and all(len(r) == 3 for r in rows)


def test_Hostが違うと断る(served):
    base, _ = served
    assert fetch(base, {"Host": "evil.example"})[0] == 403


def test_式に見える名前は先頭にアポストロフィを付ける():
    # Excel に貼った時に式として動かないように（自動のセキュリティレビューの指摘）。記号1字の「-」はそのまま
    st = {"staff": [{"name": "=1+1", "unit": "A"}, {"name": "＝HYPERLINK(1)", "unit": "A"}], "days": 2,
          "weekdays": ["sat", "sun"], "table": {"=1+1": ["-", "@x"], "＝HYPERLINK(1)": ["日", "公"]}, "layers": {}}
    rows = grid(table_tsv(st))
    assert rows[2][0] == "'=1+1" and rows[3][0] == "'＝HYPERLINK(1)"
    assert rows[2][1:] == ["-", "'@x"]
