"""人ごとの勤務の回数の列（作者「その人ごとの勤務の数が表示されてもいいかも」2026-11-06）。
数えるのは画面の JS（static/index.html の COUNTS-BEGIN〜END）。node で動かして、マスの数と合うことを確かめる。"""
import json
import pathlib
import shutil
import subprocess

import pytest

import shiftvoice
from shiftvoice.server import table_tsv

HTML = pathlib.Path(shiftvoice.__file__).parent / "static" / "index.html"


def _js():
    h = HTML.read_text(encoding="utf-8")
    assert "COUNTS-BEGIN" in h and "COUNTS-END" in h, "回数を数える関数が画面にありません"
    return h.split("// COUNTS-BEGIN")[1].split("// COUNTS-END")[0]


def _cols_and_counts(st):
    """画面と同じ関数を node で動かし、列と人ごとの回数を返す。"""
    if not shutil.which("node"):
        pytest.skip("node が無い")
    names = [s["name"] for s in st["staff"]]
    prog = _js() + """
var st = JSON.parse(process.argv[1]);
var rows = st.names.map(function (n) { return st.table[n] || []; });
var cols = shiftCountCols(st.symbols, rows);
console.log(JSON.stringify({cols: cols.map(function (c) { return c.sym; }),
  counts: rows.map(function (r) { return shiftCountsOf(r, cols); })}));
"""
    arg = json.dumps({"names": names, "table": st["table"], "symbols": st["symbols"]}, ensure_ascii=False)
    out = subprocess.run(["node", "-e", prog, arg], capture_output=True, text=True, check=True).stdout
    return json.loads(out), names


def test_counts_match_the_cells(sess):
    st = sess.state()
    got, names = _cols_and_counts(st)
    assert got["cols"][:4] == ["早", "日", "遅", "夜"] and got["cols"][-2:] == ["公", "有"]
    assert "明" not in got["cols"]                          # 明けは夜勤と同じ数なので出さない
    for n, counts in zip(names, got["counts"]):
        row = st["table"][n]
        for sym, c in zip(got["cols"], counts):
            assert c == row.count(sym), (n, sym)
    assert any(c for cs in got["counts"] for c in cs)


def test_unused_training_column_hidden_and_added_shift_shown(sess):
    st = sess.state()
    got, _ = _cols_and_counts(st)
    used = {x for r in st["table"].values() for x in r}
    assert ("研" in got["cols"]) == ("研" in used)
    assert sess.say("8時から17時の日8を足して").kind == "applied"
    sess.say("10日の小島さん、日8に")
    st = sess.state()
    got, _ = _cols_and_counts(st)
    in_table = any("日8" in r for r in st["table"].values())
    assert ("日8" in got["cols"]) == in_table                # その月に入っていれば出る


def test_copy_text_has_no_count_columns(sess):
    st = sess.state()
    lines = table_tsv(st).splitlines()
    person = next(x for x in lines if x.startswith(st["staff"][0]["name"]))
    assert len(person.split("\t")) == 1 + st["days"]         # 名前＋日の数だけ。回数は貼り戻す形に入れない


def test_screen_draws_the_counts_in_every_table():
    h = HTML.read_text(encoding="utf-8")
    assert h.count("cntCells(") >= 3                          # 定義と、11月・12月の表と返す案の表
    assert "cntBlank(" in h and "countRows(cols)" in h        # 見出しの行・人数の行は罫線だけそろえる
    assert "tr.unithead" in h and "th.cnt" in h
