"""貼って作った施設で、翌月の希望休を入れて作り直す道と、取り込んだ勤務の一言（2026-11-06 通しの試し）。

直す前は、取り込むと12月の表ができて12月を開くので、［1月へ］しか無く、12月の希望休の欄へ進めなかった。
取り込みで答えた「日8」は Claude へそのまま送り、「10日の小島さん、日8に」が日勤と読まれていた。"""
import shutil
import threading
from http.server import ThreadingHTTPServer

import pytest

from conftest import FIXED_NOW, REPLIES
from shiftvoice.addshift import hide_spoken
from shiftvoice.model import ShiftType
from shiftvoice.nextflow import NextFlow
from shiftvoice.reader import RecordedReader
from shiftvoice.server import make_handler
from shiftvoice.session import Session
from shiftvoice.store import Store
from test_setup_server import call, paste_body


@pytest.fixture()
def srv(built_dir, tmp_path):
    root = tmp_path / "hidamari"
    shutil.copytree(built_dir, root)
    session = Session(Store(root), 2026, 11, RecordedReader.from_file(REPLIES), now=lambda: FIXED_NOW)
    handler = make_handler(session, threading.Lock(), True, None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    box = handler.box
    httpd.flow = lambda: NextFlow(box.session.store, box.session.year, box.session.month)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_貼って作った後に希望休を入れて作り直せる(srv):
    code, made = call(srv, "/api/setup/create", paste_body(name="みどりの丘"))
    assert code == 200 and made["state"]["month"] == 12 and made["state"]["redo_wishes"]
    code, back = call(srv, "/api/setup/wishes", {})
    assert code == 200 and back["state"]["month"] == 11
    assert back["state"]["redo_wishes"] is False
    name = back["state"]["staff"][0]["name"]
    days = "\t".join(str(d) for d in range(1, 32))
    row = "\t".join("希" if d == 15 else "" for d in range(1, 32))
    code, w = call(srv, "/api/next/wish_paste", {"text": f"氏名\t{days}\n{name}\t{row}\n"})
    assert code == 200 and w["wish_month"] == 12 and len(w["wishes"]) == 1
    code, b = call(srv, "/api/next/build", {})
    assert code == 200 and b["status"] == "ok"
    assert b["tables"]["base"][name][14] == "公"
    code, a = call(srv, "/api/next/adopt", {"plan": "base"})
    assert code == 200 and a["ok"]
    code, fac = call(srv, "/api/facilities")
    cur = next(f for f in fac["facilities"] if f["current"])
    code, opened = call(srv, "/api/facilities/open", {"id": cur["id"]})
    assert opened["state"]["month"] == 12 and opened["state"]["table"][name][14] == "公"


def test_配った後とひだまりでは作り直しを出さない(srv):
    code, st = call(srv, "/api/state")
    assert st["redo_wishes"] is False
    assert call(srv, "/api/setup/wishes", {})[0] == 400
    call(srv, "/api/setup/create", paste_body(name="みどりの丘"))
    assert call(srv, "/api/distribute", {})[0] == 200
    code, st = call(srv, "/api/state")
    assert st["distributed"] and st["redo_wishes"] is False
    assert call(srv, "/api/setup/wishes", {})[0] == 400


class Fac:
    def __init__(self, *extra):
        base = [ShiftType(symbol=s, name=n, kind="work") for s, n in (("早", "早番"), ("日", "日勤"), ("遅", "遅番"), ("夜", "夜勤"))]
        self.shift_types = base + [ShiftType(symbol="研", name="研修", kind="work", on_request_only=True)] + list(extra)


def test_取り込んだ勤務の記号も伏せて送る():
    fac = Fac(ShiftType(symbol="日8", name="日8の勤務", kind="work", on_request_only=True))
    t, tokens = hide_spoken("10日の職員12さん、日8に", fac)
    assert "日8" not in t and tokens[next(iter(tokens))] == "日8" and "10日" in t


def test_ふつうの勤務だけの一言は送る文を変えない():
    fac = Fac(ShiftType(symbol="日8", name="日8の勤務", kind="work", on_request_only=True))
    assert hide_spoken("18日の職員01さん、日勤に", fac) == ("18日の職員01さん、日勤に", {})
    assert hide_spoken("18日の職員01さん、研修に", fac) == ("18日の職員01さん、研修に", {})   # 研修は読み手が知っている


def test_AとBの記号で伏せた字を当て直さない():
    fac = Fac(ShiftType(symbol="A", name="Aの勤務", kind="work"), ShiftType(symbol="B", name="Bの勤務", kind="work"))
    t, tokens = hide_spoken("職員01さん、3日はBで4日はA", fac)
    assert sorted(tokens.values()) == ["A", "B"]
    assert t.count("勤務") == 2 and "勤務勤務" not in t
