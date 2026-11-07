"""希望休のマスの枠と、マウスを乗せた時の文（作者 2026-11-07「希望休のセルは赤枠など、わかりやすくなっているといい。
マウスオーバーすると希望休や、何らかの事情が表示されると、なお良い」）。
マスごとに集めるのは画面の JS（static/index.html の CELLRULES-BEGIN〜END）。node で動かして確かめる。"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

import shiftvoice

HTML = pathlib.Path(shiftvoice.__file__).parent / "static" / "index.html"
SYMBOLS = [{"symbol": "早", "kind": "work"}, {"symbol": "日", "kind": "work"}, {"symbol": "夜", "kind": "work"},
           {"symbol": "明", "kind": "ake"}, {"symbol": "公", "kind": "rest"}, {"symbol": "有", "kind": "leave"},
           {"symbol": "研", "kind": "work"}]


def _js():
    h = HTML.read_text(encoding="utf-8")
    assert "CELLRULES-BEGIN" in h and "CELLRULES-END" in h, "マスのルールを集める関数が画面にありません"
    return h.split("// CELLRULES-BEGIN")[1].split("// CELLRULES-END")[0]


def _map(cards, symbols, names):
    if not shutil.which("node"):
        pytest.skip("node が無い")
    prog = _js() + """
var a = JSON.parse(process.argv[1]);
console.log(JSON.stringify(cellRuleMap(a.cards, a.symbols, a.names)));
"""
    arg = json.dumps({"cards": cards, "symbols": symbols, "names": names}, ensure_ascii=False)
    return json.loads(subprocess.run(["node", "-e", prog, arg], capture_output=True, text=True, check=True).stdout)


def test_話した希望休のマスに枠と理由(sess):
    before = sess.state()
    names = [s["name"] for s in before["staff"]]
    assert not any(v["wish"] for v in _map(before["cards"], before["symbols"], names).values())
    sess.say("岡田さん、15と16は法事で休み")
    st = sess.state()
    got = _map(st["cards"], st["symbols"], names)
    assert {k for k, v in got.items() if v["wish"]} == {"岡田|15", "岡田|16"}
    assert got["岡田|15"]["lines"] == ["希望休（法事）"]
    assert "岡田|14" not in got and "岡田|17" not in got


def _card(name, flex, params, who, when, kind="cell", reason=None):
    return {"id": name, "name": name, "kind": kind, "flex": flex, "params": params, "who": who, "when": when,
            "meta": {"reason": reason} if reason else {}}


def test_枠は希望休だけで_ほかの日付のマスのルールは文だけ():
    cards = [
        _card("小島さん 10日 研修", "fixed", {"in": ["研"]}, ["小島"], [10], reason="外部の研修"),
        _card("小島さん 11日 日勤", "wish", {"in": ["日"]}, ["小島"], [11]),             # 本人の希望でも勤務の日は希望休でない
        _card("小島さん 12日は夜勤でない", "wish", {"not_in": ["夜"]}, ["小島"], [12]),  # 置かない頼みは希望休でない
        _card("小島さん 13日 休み", "negotiable", {"in": ["公"]}, ["小島"], [13]),       # 相談できる休み（できれば休み）
        _card("岡田さん 20日 休み", "wish", {"in": ["公", "有"]}, ["岡田"], [20]),       # 理由の無い希望休はルールの名前
        _card("3日は全員日勤にしない", "fixed", {"not_in": ["日"]}, None, [3]),          # 人を決めないマスのルールは全員のマスに
        _card("岡田さんの夜勤は月4回まで", "operational", {"shifts": ["夜"], "max": 4}, ["岡田"], [20], kind="count"),
        _card("岡田さんは毎日公休", "wish", {"in": ["公"]}, ["岡田"], None),              # 日付の無いマスのルールは出さない
    ]
    got = _map(cards, SYMBOLS, ["小島", "岡田"])
    assert {k for k, v in got.items() if v["wish"]} == {"岡田|20"}
    assert got["岡田|20"]["lines"] == ["希望休（岡田さん 20日 休み）"]
    assert got["小島|10"] == {"wish": False, "lines": ["小島さん 10日 研修（外部の研修）"]}
    assert got["小島|11"]["lines"] == ["小島さん 11日 日勤"] and not got["小島|11"]["wish"]
    assert not got["小島|12"]["wish"] and not got["小島|13"]["wish"]
    assert got["小島|3"]["lines"] == got["岡田|3"]["lines"] == ["3日は全員日勤にしない"]
    assert set(got) == {"小島|10", "小島|11", "小島|12", "小島|13", "岡田|20", "小島|3", "岡田|3"}


def test_同じマスの2件は両方の文を出す():
    cards = [_card("小島さん 10日 休み", "wish", {"in": ["公"]}, ["小島"], [10], reason="通院"),
             _card("小島さん 10日は早番でない", "fixed", {"not_in": ["早"]}, ["小島"], [10])]
    got = _map(cards, SYMBOLS, ["小島"])
    assert got["小島|10"] == {"wish": True, "lines": ["希望休（通院）", "小島さん 10日は早番でない"]}


def _print_block(h):
    i = h.index("@media print {")
    depth, j = 0, i
    while True:
        c = h[j]
        depth += c == "{"
        depth -= c == "}"
        j += 1
        if c == "}" and depth == 0:
            return h[i:j]


def test_表のマスと凡例と印刷():
    h = HTML.read_text(encoding="utf-8")
    table = h.split("function renderTable()")[1].split("\nfunction ")[0]
    assert 'td.className += " wish"' in table and "td.title = cr.lines.join" in table
    # 枠は、印刷で消える影（box-shadow）と、案内の赤い枠・選んだマスが使う outline を使わない
    rule = re.search(r"\.grid td\.cell\.wish::after \{([^}]*)\}", h).group(1)
    assert "dashed #c00" in rule and "box-shadow" not in rule and "outline" not in rule
    legend = h.split("function renderLegend()")[1].split("\nfunction ")[0]
    assert "赤い点線の枠＝本人の希望休" in legend and "wishsw" in legend
    pr = _print_block(h)
    assert "wish::after" in pr and "print-color-adjust: exact" in pr      # 紙にも枠を出す
    hidden = re.findall(r"([^{}]*)\{\s*display:\s*none", pr)
    assert not any("wishkey" in x or "wishsw" in x or "td.cell.wish" in x for x in hidden)
    assert any(".legend .wishhint" in x for x in hidden)                  # 「マウスを乗せると」は紙に出さない
