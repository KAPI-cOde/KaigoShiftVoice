"""右のルールの一覧のまとまり（作者 2026-11-07「施設のハードルール、ソフトルールそれぞれの重み付けと、個人ごとにまとめられた
ハードルールとソフトルールが分かりやすく見れるといい」）。分け方は画面の JS（static/index.html の groupCards）を node で動かして確かめる。"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

import shiftvoice

HTML = pathlib.Path(shiftvoice.__file__).parent / "static" / "index.html"
ROSTER = ["青木", "石田", "上野", "江口", "小川", "加藤", "岡田", "落合",
          "菊池", "久保", "小島", "斉藤", "篠原", "杉山", "田村", "千葉"]


def _card(i, flex, who=None, scope="rule", kind="count", **meta):
    return {"id": i, "name": i, "flex": flex, "scope": scope, "kind": kind, "who": who, "meta": meta}


CARDS = [
    _card("早番の人数", "fixed", kind="coverage"),
    _card("夜勤の回数", "operational"),
    _card("早番の2連続を避ける", "negotiable", kind="max_run"),
    _card("斉藤 水曜", "wish", ["斉藤"], kind="weekday"),
    _card("小島 夜勤を続けない", "operational", ["小島"], kind="max_run", said="小島さんの夜勤はできるだけ単発で"),
    _card("小島 早番は月5回くらい", "negotiable", ["小島"], said="小島さん、早番は月5回くらい"),
    _card("青木・石田 同じ日に夜勤にしない", "fixed", ["青木", "石田"], kind="pair"),
    _card("公休の数（取り込み）", "operational", ROSTER[:13]),     # 名簿の半分を超える人の形は施設のルール
    _card("小島 今月は夜勤0", "operational", ["小島"], scope="once"),
    _card("小島 29日まで夜勤3回", "fixed", ["小島"], until="2026-11-29"),
    _card("千葉 連勤は4日まで", "fixed", ["千葉"], kind="max_run"),
    _card("赤坂 早番なし", "fixed", ["赤坂"], kind="weekday"),     # 今の名簿にいない人
    _card("岡田 15日 休み", "wish", ["岡田"], scope="once", kind="cell", until="2026-11-30"),   # マスの頼みは表に出る
]


def _run(cards, roster):
    if not shutil.which("node"):
        pytest.skip("node が無い")
    html = HTML.read_text(encoding="utf-8")
    fns = [re.search(r"function monthOnly\(c\) \{.*?\n\}", html, re.S)[0],
           re.search(r"function groupCards\(cards, roster\) \{.*?\n\}", html, re.S)[0]]
    js = (f"var S={{year:2026,month:11}};{';'.join(fns)};"
          f"var g=groupCards({json.dumps(cards, ensure_ascii=False)},{json.dumps(roster, ensure_ascii=False)});"
          "function ids(a){return a.map(function(c){return c.id;});}"
          "function grp(p){return {name:p.name,gone:p.gone,must:ids(p.must),wish:ids(p.wish),soft:ids(p.soft)};}"
          "console.log(JSON.stringify({only:ids(g.only),total:g.total,facMust:ids(g.facMust),facSoft:ids(g.facSoft),"
          "people:g.people.map(grp),multi:g.multi&&grp(g.multi)}));")
    return json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)


def test_groups_split_facility_by_strength_and_people_by_name():
    g = _run(CARDS, ROSTER)
    assert g["only"] == ["小島 29日まで夜勤3回", "小島 今月は夜勤0"]          # 今月だけは今までどおり新しい順でいちばん上
    assert g["facMust"] == ["早番の人数", "夜勤の回数", "公休の数（取り込み）"]
    assert g["facSoft"] == ["早番の2連続を避ける"]
    assert [p["name"] for p in g["people"]] == ["小島", "斉藤", "千葉", "赤坂"]   # 名簿の順。名簿にいない人は後
    kojima, saito, chiba, gone = g["people"]
    assert kojima == {"name": "小島", "gone": False, "must": ["小島 夜勤を続けない"], "wish": [],
                      "soft": ["小島 早番は月5回くらい"]}
    assert saito["wish"] == ["斉藤 水曜"] and not saito["must"] and not saito["soft"]   # 本人の希望は人の中
    assert chiba["must"] == ["千葉 連勤は4日まで"]
    assert gone["gone"] is True and not kojima["gone"]
    assert g["multi"]["must"] == ["青木・石田 同じ日に夜勤にしない"]               # 複数の人のルールは1か所にまとめる
    assert g["total"] == 10                                                        # 今月だけとマスの頼みは数えない


def test_no_people_rules_leave_people_empty():
    g = _run(CARDS[:3], ROSTER)
    assert g["people"] == [] and g["multi"] is None and g["total"] == 3


def test_list_has_the_headings_and_remembers_folds():
    h = HTML.read_text(encoding="utf-8")
    body = h[h.index("function groupCards("):h.index("function renderSaid")]
    for w in ("施設のルール", "守る（動かせない・運用の都合）", "できれば（相談できる）", "人ごとのルール", "本人の希望", "複数の人"):
        assert f'"{w}' in body, w
    assert "groupCards(S.cards" in body
    assert 'viewPref("sv_fold_' in body and 'setViewPref(' in body     # たたんだ状態はブラウザが覚える（try/catch の中）
    assert "ルールを消す" not in body                                   # 見えなくするだけ。消す機能は作らない
