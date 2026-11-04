"""画面の知らせが、実際に起きたことと食い違わないようにする（2026-11-04 夜の Astra の指摘と、審査員役2人の読み）。
- 取り込みの「11月と違う所」の例は、出した違いを戻す一言にする（赤い行が失敗の印に見える、と2人が読んだ）
- 貼った表で書き方が2つ以上あった記号（休と希）は、持ち出しで1つにまとまることを知らせる
- 採った月の表の題の下の1行は、実際に返した物（有給・短い連勤）と引き受けの月から作る"""
import json
import threading
import types
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from conftest import FIXED_NOW, REPLIES
from shiftvoice.onboard import create_facility, infer, parse_paste
from shiftvoice.reader import RecordedReader
from shiftvoice.server import make_handler
from shiftvoice.session import Session
from shiftvoice.store import Store

ROOT = Path(__file__).resolve().parents[1]
PASTES = Path(__file__).resolve().parent / "fixtures" / "pastes"
A3 = {"A": {"same_as": "早"}, "B": {"same_as": "日"}, "C": {"same_as": "遅"}, "N": {"same_as": "夜"}}


def test_違う所の例は違いを戻す一言(tmp_path):
    text = (ROOT / "data" / "samples" / "komorebi-2026-11.tsv").read_text(encoding="utf-8")
    prop = infer(parse_paste(text), 2026, 11)
    c = create_facility(prop, tmp_path / "k", name="こもれびの家（架空）", next_year=2026, next_month=12)
    shape = [d for d in c.diffs if d["kind"] == "weekday_shape"]
    assert shape, c.diffs
    assert shape[0]["say"] == "土日の日勤は1人"   # 貼った11月は土日の日勤が毎回1人。12月は2.0人に増えた
    assert shape[0]["why"] == "平日と土日の日勤の分け方は、ルールにしていないので、11月と変わることがあります"
    html = (ROOT / "src" / "shiftvoice" / "static" / "index.html").read_text(encoding="utf-8")
    assert "ルールにしていない形は、" in html and "d.say" in html


def test_書き方が2つあった記号は持ち出しで1つにまとまると知らせる(tmp_path):
    prop = infer(parse_paste((PASTES / "03.tsv").read_text(encoding="utf-8")), 2026, 11, A3)
    assert prop.paste_merged["公"]["to"] == prop.paste_symbols["公"] == "休"
    assert "希" in prop.paste_merged["公"]["from"]
    c = create_facility(prop, tmp_path / "f", name="試験", next_year=2026, next_month=12, answers=A3)
    assert c.next_table_made
    store = Store(tmp_path / "f")
    assert store.facility().settings["paste_merged"]["公"]["to"] == "休"

    s = Session(store, 2026, 12, RecordedReader.from_file(REPLIES), now=lambda: FIXED_NOW)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(s, threading.Lock(), True, None))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/api/table.tsv") as r:
            merged = json.loads(urllib.parse.unquote(r.headers.get("X-Merged-Symbols")))
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert merged["公"]["to"] == "休" and "希" in merged["公"]["from"]


def note(stage, months, adopted="give", ids=("a1",)):
    m = types.SimpleNamespace(layers={"_adopted": adopted,
                                      "_plans": {"acc_ids": list(ids), "stage": stage, "acc_months": months}})
    return Session._returned_note(types.SimpleNamespace(month=12), m)


def test_採った月の1行は返した物と月から作る():
    assert note(["leave", "run"], ["2026-11"]) == "11月に引き受けた人に、有給と短い連勤を返した12月の表です"
    assert note(["leave"], ["2026-11"]) == "11月に引き受けた人に、有給を返した12月の表です"
    assert note(["run"], ["2026-10", "2026-11"]) == "10月・11月に引き受けた人に、短い連勤を返した12月の表です"
    assert note(None, None) == "引き受けた人に報いた12月の表です"        # 記録の無い古い案
    assert note(["leave"], ["2026-11"], adopted="base") == ""             # 返さない案
    assert note(["leave"], ["2026-11"], ids=()) == ""                     # 返す引き受けが無い
