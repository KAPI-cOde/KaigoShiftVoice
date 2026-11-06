"""取り込みで読み替えた記号（休→公、A→早、空欄→公 など）を、表の持ち出しで施設の書き方へ戻す
（2026-11-04 敵対レビューの B。施設の様式は集計の式が入った Excel なので、アプリの記号のまま貼ると集計が合わない）。"""
import csv
import io
import shutil
from pathlib import Path

import pytest

from conftest import FIXED_NOW, REPLIES
from shiftvoice.onboard import create_facility, infer, parse_paste
from shiftvoice.reader import RecordedReader
from shiftvoice.server import table_tsv
from shiftvoice.session import Session
from shiftvoice.store import Store

PASTES = Path(__file__).resolve().parent / "fixtures" / "pastes"
A3 = {"A": {"same_as": "早"}, "B": {"same_as": "日"}, "C": {"same_as": "遅"}, "N": {"same_as": "夜"}}
A5 = {"ア": {"same_as": "明"}}


def paste(n):
    return (PASTES / f"{n:02d}.tsv").read_text(encoding="utf-8")


def made(tmp_path, n, answers=None, tag="f", text=None):
    prop = infer(parse_paste(text or paste(n)), 2026, 11, answers)
    c = create_facility(prop, tmp_path / tag, name="試験", next_year=2026, next_month=12, answers=answers)
    assert c.next_table_made
    return prop, Store(tmp_path / tag)


def export(store, y, m):
    s = Session(store, y, m, RecordedReader.from_file(REPLIES), now=lambda: FIXED_NOW)
    s.open()
    st = s.state()
    back = store.facility().settings.get("paste_symbols") or {}
    return st, [r.split("\t") for r in table_tsv(st, back).splitlines()]


def pasted_cells(n, text=None):
    """貼ったままの、名前 → 日ごとのセル。"""
    names = set(parse_paste(text or paste(n)).table)
    out = {}
    for cells in csv.reader(io.StringIO(text or paste(n)), delimiter="\t"):
        name = cells[0].replace(" ", "").replace("\u3000", "") if cells else ""
        if name in names:
            out[name] = cells[1:31]
    return out


def test_設定に書き方の対応が残る(tmp_path):
    prop, store = made(tmp_path, 3, A3)
    ps = store.facility().settings["paste_symbols"]
    assert ps["公"] == "休" and "早" in ps and ps["早"] == "A" and ps["夜"] == "N"
    assert ps == prop.paste_symbols
    assert "有" not in ps                       # 貼った表でも「有」のまま


def test_書き方が2つあれば多い方(tmp_path):
    # 03 は公休を「休」と「希」で書いている。休の方が多い
    cells = [c for row in pasted_cells(3).values() for c in row]
    assert cells.count("休") > cells.count("希") > 0
    _, store = made(tmp_path, 3, A3)
    assert store.facility().settings["paste_symbols"]["公"] == "休"


@pytest.mark.parametrize("n,answers", [(3, A3), (5, A5)])
def test_貼った月を持ち出すと貼った書き方に戻る(tmp_path, n, answers):
    _, store = made(tmp_path, n, answers)
    st, rows = export(store, 2026, 11)
    raw = pasted_cells(n)
    assert raw and set(raw) == {r[0] for r in rows[2:2 + len(st["staff"])]}
    majority = store.facility().settings["paste_symbols"]
    diff = 0
    for r in rows[2:2 + len(st["staff"])]:
        for got, was in zip(r[1:], raw[r[0]]):
            if got != was:
                diff += 1
                assert was == "希"               # 少ない方の書き方（03 の希）だけが多い方の「休」に寄る
    assert (diff == 0) == (n == 5)
    # アプリの記号（公・早・夜）はもう出てこない
    out = {c for r in rows[2:] for c in r[1:]}
    assert not (out & set(majority) - set(majority.values()))


def test_作った翌月も施設の書き方で出る(tmp_path):
    _, store = made(tmp_path, 3, A3)
    st, rows = export(store, 2026, 12)
    used = {c for r in rows[2:] for c in r[1:]}
    assert {"休", "A", "N"} <= used and not used & {"公", "早", "夜"}
    assert len(rows[0]) == 1 + 31


def test_空欄で書いていた休みは空欄で出る(tmp_path):
    # 見本の施設Bの形（休みは空欄）。7番の表の「休」を空欄にして貼る
    text = paste(7).replace("\t休", "\t")
    prop, store = made(tmp_path, 7, text=text)
    assert prop.paste_symbols == {"公": ""} and parse_paste(text).blank_as_rest > 0
    for ym in ((2026, 11), (2026, 12)):
        st, rows = export(store, *ym)
        used = {c for r in rows[2:2 + len(st["staff"])] for c in r[1:]}
        assert "" in used and "公" not in used
    st, rows = export(store, 2026, 11)
    raw = pasted_cells(7, text)
    assert [r[1:] for r in rows[2:2 + len(st["staff"])]] == [raw[s["name"]] for s in st["staff"]]


def test_空欄と休が両方あれば多い方():
    head = "氏名\t" + "\t".join(str(d) for d in range(1, 29))
    t = head + "\n赤坂\t" + "\t".join(["早", "", "", "日"] * 7) + "\n井上\t" + "\t".join(["休", "遅", "夜", "明"] * 7)
    assert infer(parse_paste(t), 2027, 2).paste_symbols["公"] == ""
    t2 = head + "\n赤坂\t" + "\t".join(["早", "休", "休", "日"] * 7) + "\n井上\t" + "\t".join(["", "遅", "夜", "明"] * 7)
    assert infer(parse_paste(t2), 2027, 2).paste_symbols["公"] == "休"


def test_ひだまりは今と同じ(built_dir, tmp_path):
    root = tmp_path / "hidamari"
    shutil.copytree(built_dir, root)
    store = Store(root)
    assert "paste_symbols" not in store.facility().settings
    st, rows = export(store, 2026, 11)
    people = [r for r in rows[2:] if not r[0].endswith("ユニット")]   # ユニットの見出しの行を除いた職員の行
    assert [r[-st["days"]:] for r in people] == [st["table"][s["name"]][:st["days"]] for s in st["staff"]]


def test_対応の無い記号はそのままで先頭の式の守りは戻した後にも効く():
    st = {"staff": [{"name": "あ", "unit": "A"}], "days": 3, "weekdays": ["sat", "sun", "mon"],
          "table": {"あ": ["公", "有", "早"]}, "layers": {}}
    rows = [r.split("\t") for r in table_tsv(st, {"公": "-休", "早": "=A"}).splitlines()]
    assert rows[2] == ["あ", "'-休", "有", "'=A"]       # 2字以上の式に見える物は、戻した後でも守る


def test_サーバーは戻した記号を応答の頭で知らせる(tmp_path, built_dir):
    """画面のコピーの知らせ（「記号は、貼った表の書き方に戻しています」）が読む頭。ひだまりには付かない。"""
    import json
    import threading
    import urllib.parse
    import urllib.request
    from http.server import ThreadingHTTPServer

    from shiftvoice.server import make_handler

    def head(store, y, m):
        s = Session(store, y, m, RecordedReader.from_file(REPLIES), now=lambda: FIXED_NOW)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(s, threading.Lock(), True, None))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/api/table.tsv") as r:
                return r.headers.get("X-Restored-Symbols"), r.read().decode("utf-8")
        finally:
            httpd.shutdown()
            httpd.server_close()

    _, store = made(tmp_path, 3, A3)
    h, body = head(store, 2026, 12)
    back = json.loads(urllib.parse.unquote(h))
    assert back == store.facility().settings["paste_symbols"]
    assert back.get("早") == "A" and back.get("公") == "休"
    assert "\tA\t" in body or body.count("A") > 0

    root = tmp_path / "hidamari"
    shutil.copytree(built_dir, root)
    h2, _ = head(Store(root), 2026, 11)
    assert h2 is None
