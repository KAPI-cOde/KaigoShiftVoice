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


# 同じ一言から入ったルールは、同じまとまりの中で1枚に束ねる（作者 2026-11-07「人間の認知負荷が飽和」「こちらで指示しなくても、
# 少ないルールにまとめたり統合したりする動きを自動でやってほしい」）。帳面のルールは変えず、画面の見た目だけ
SAID1 = "小島さんは有給消化。夜勤は単発で1〜3回なら可"
SAID2 = "遅番はなるべく連続2回まで。どうしようもない時は3回まで認めます"


def _op(card, op_id):
    return {**card, "op_id": op_id}


BUNDLE_CARDS = [
    _op(_card("早番の人数", "fixed", kind="coverage"), "seed-r001"),
    _op(_card("遅番の人数", "fixed", kind="coverage"), "seed-r002"),
    _op(_card("全員 遅番は続けて2日まで", "negotiable", kind="max_run", said=SAID2), "u3"),
    _op(_card("全員 遅番は続けて3日まで", "operational", kind="max_run", said=SAID2), "u3"),
    _op(_card("小島 有給を入れる", "wish", ["小島"], said=SAID1), "u1"),
    _op(_card("小島 早番は月5回くらい", "negotiable", ["小島"], said="小島さん、早番は月5回くらい"), "u2"),
    _op(_card("小島 夜勤は3回まで", "fixed", ["小島"], said=SAID1), "u1"),
    _op(_card("小島 夜勤を続けない", "operational", ["小島"], kind="max_run", said=SAID1), "u1"),
    _op(_card("千葉 夜勤は2回まで", "fixed", ["千葉"], said="千葉さん、夜勤は2回まで"), "u4"),
    _op(_card("千葉 夜勤は2回まで（言い直し）", "fixed", ["千葉"], said="千葉さん、夜勤は2回まで"), "u5"),   # 同じ文でも別の一言
    _op(_card("千葉 連勤は4日まで", "fixed", ["千葉"], kind="max_run"), "rv-1"),        # 言った言葉の無いルールは束ねない
    _op(_card("千葉 早番は5回まで", "fixed", ["千葉"]), "rv-1"),
    _card("小島 今月は夜勤0", "operational", ["小島"], scope="once", said="小島さん、今月は夜勤0で"),
    _card("小島 今月は早番2回まで", "operational", ["小島"], scope="once", said="小島さん、今月は夜勤0で"),   # op_id が無い時は言った言葉で
]


def _bundles(cards, roster):
    if not shutil.which("node"):
        pytest.skip("node が無い")
    html = HTML.read_text(encoding="utf-8")
    fns = [re.search(rf"function {n}\(.*?\) \{{.*?\n\}}", html, re.S)[0] for n in ("monthOnly", "groupCards", "bundleCards", "facBoxes")]
    js = (f"var S={{year:2026,month:11}};{';'.join(fns)};"
          f"var g=groupCards({json.dumps(cards, ensure_ascii=False)},{json.dumps(roster, ensure_ascii=False)});"
          "var f=facBoxes(g.facMust,g.facSoft);"
          "function ids(a){return a.map(function(c){return c.id;});}"
          "function bids(bs){return bs.map(function(b){return ids(b.cards);});}"
          "console.log(JSON.stringify({only:bids(bundleCards(g.only)),must:bids(f.must),soft:bids(f.soft),"
          "mustCards:ids(f.mustCards),softCards:ids(f.softCards),"
          "said:bundleCards(g.people[0].must.concat(g.people[0].wish,g.people[0].soft)).map(function(b){return b.said;}),"
          "people:g.people.map(function(p){return [p.name,bids(bundleCards(p.must.concat(p.wish,p.soft)))];})}));")
    return json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)


def test_one_utterance_is_one_card_and_other_utterances_stay_apart():
    b = _bundles(BUNDLE_CARDS, ROSTER)
    people = dict((n, x) for n, x in b["people"])
    # 同じ op_id の3件（守る・本人の希望をまたぐ）が1枚。別の一言は別の枚。束はいちばん前のルールの場所に置く
    assert people["小島"] == [["小島 夜勤を続けない", "小島 夜勤は3回まで", "小島 有給を入れる"], ["小島 早番は月5回くらい"]]
    assert b["said"] == [SAID1, "小島さん、早番は月5回くらい"]
    # 同じ文でも op_id が違えば別の一言。言った言葉の無いルールは op_id が同じでも束ねない
    assert people["千葉"] == [["千葉 夜勤は2回まで（言い直し）"], ["千葉 夜勤は2回まで"], ["千葉 連勤は4日まで"], ["千葉 早番は5回まで"]]
    # op_id が無い時は、同じ言った言葉で束ねる
    assert b["only"] == [["小島 今月は早番2回まで", "小島 今月は夜勤0"]]


def test_facility_bundle_crosses_strength_and_sits_in_must():
    b = _bundles(BUNDLE_CARDS, ROSTER)
    # 「なるべく2回・どうしようもない時は3回」の2件は、守る・できればをまたいで1枚にし、守るの箱に置く
    assert b["must"] == [["全員 遅番は続けて3日まで", "全員 遅番は続けて2日まで"], ["早番の人数"], ["遅番の人数"]]
    assert b["soft"] == []
    # 箱の件数と外れの数は、その箱に描くルールで数える
    assert b["mustCards"] == ["全員 遅番は続けて3日まで", "全員 遅番は続けて2日まで", "早番の人数", "遅番の人数"]
    assert b["softCards"] == []


def test_list_draws_bundles():
    h = HTML.read_text(encoding="utf-8")
    body = h[h.index("function renderCards("):h.index("function showClean")]
    assert body.count("bundleCards(") >= 2 and "facBoxes(" in body and "putBundles(" in body
    mk = h[h.index("function makeBundle("):h.index("function renderSaid")]
    assert "makeCard(c)" in mk                                          # 押すと中のルールが今までどおりのカードで出る
    assert "c.broken" in mk and "negotiable" in mk                      # 外れの赤い数は中のルールの合計


def test_state_cards_carry_the_op_id_of_the_utterance(sess):
    """右の一覧で束ねるため、state() のカードに、そのルールを入れた一言の op_id（帳面の add）を付ける。"""
    sess.open()
    r = sess.say("遅番はなるべく連続2回まで。どうしようもない時は3回まで認めます。ただし杉山さんのような日勤と遅番中心の勤務の場合はそれには当たりません")
    if r.kind == "ask_period":
        r = sess.answer(r.uid, "rule")
    assert r.kind == "applied", r.message
    cards = sess.state()["cards"]
    two = [c for c in cards if c["kind"] == "max_run" and c["params"].get("shifts") == ["遅"] and not c["who"]]
    assert len(two) == 2
    assert two[0]["op_id"] and two[0]["op_id"] == two[1]["op_id"]
    others = [c["op_id"] for c in cards if c not in two]
    assert two[0]["op_id"] not in others
    assert all(o for o in others)                                       # 最初からのルールにも、1件ずつ違う op_id がある
