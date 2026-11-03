"""本物の Haiku に言わせて止まった・間違って効いた3つ（2026-11-03、作った施設で一言20個を2回ずつ）。

- 言い直し「赤坂さんじゃなくて井上さん、10日が休みで」。Haiku は井上さんの休みだけを返し、前の赤坂さんの10日の
  休みが残っていた。前の頼みが1件に決まる時だけ、外すのと入れるのを一緒に、表を変える前に確かめる
- 人も日も無い取り消し「さっきのやっぱりなし」。この月に話したマスの頼みが1件の時だけ、外すかを確かめる
- 全員と個人が1つの一言に入る「みんな夜勤は月4回までで、井上さんだけは3回までにして」。全員向けと名指しの
  2本にし、名指しを優先する。毎月か今月だけかは1回だけ聞く

どれも、作った施設（見本の表から取り込み）とデモの施設（ひだまり）の両方で確かめる。決まらない形は聞き返す。
"""
import json
import shutil
from pathlib import Path

import pytest

from shiftvoice.build import month_rules
from shiftvoice.names import Masker
from shiftvoice.onboard import create_facility, infer, parse_paste
from shiftvoice.reader import RecordedReader
from shiftvoice.rules import with_overrides
from shiftvoice.session import Session
from shiftvoice.store import Store

from conftest import FIXED_NOW

SAMPLE = Path(__file__).resolve().parents[1] / "data" / "samples" / "komorebi-2026-11.tsv"

PLACES = {
    "komorebi": {"ym": (2026, 12), "a": "赤坂", "b": "井上", "c": "遠藤"},
    "hidamari": {"ym": (2026, 11), "a": "岡田", "b": "石田", "c": "青木"},
}


@pytest.fixture(scope="module")
def komorebi_dir(tmp_path_factory):
    prop = infer(parse_paste(SAMPLE.read_text(encoding="utf-8")), 2026, 11)
    out = tmp_path_factory.mktemp("komorebi2") / "f"
    c = create_facility(prop, out, name="こもれびの家（架空）", next_year=2026, next_month=12)
    assert c.next_table_made
    return out


@pytest.fixture(params=["komorebi", "hidamari"])
def place(request, komorebi_dir, built_dir, tmp_path):
    src = komorebi_dir if request.param == "komorebi" else built_dir
    dst = tmp_path / "f"
    shutil.copytree(src, dst)
    return {**PLACES[request.param], "name": request.param, "store": Store(dst)}


def _session(place, said: dict[str, list[dict]]) -> Session:
    """言う一言ごとに用意した返事で読む（返事の名前は伏せた番号で書く）。"""
    store = place["store"]
    masker = Masker(store.staff(), store.facility())

    def code(v):
        if isinstance(v, str):
            return masker.to_code.get(v, v)
        if isinstance(v, list):
            return [masker.to_code.get(x, x) for x in v]
        return v
    replies = {}
    for text, items in said.items():
        items = [{k: (code(v) if k == "staff" else v) for k, v in it.items()} for it in items]
        replies[masker.mask(text)[0]] = json.dumps({"items": items}, ensure_ascii=False)
    y, m = place["ym"]
    s = Session(store, y, m, RecordedReader(replies), now=lambda: FIXED_NOW)
    s.open()
    return s


def _table(place):
    y, m = place["ym"]
    return {k: list(v) for k, v in place["store"].month(y, m).table.items()}


def _rev(place):
    return place["store"].ledger().rev


def _say(s, text, scope="once"):
    r = s.say(text)
    if r.kind == "ask_period":
        r = s.answer(r.uid, scope)
    return r


def _cells(place, who):
    """帳面にある、その人の話したマスの頼み（日の並び）。"""
    return sorted(tuple(x.when) for x in place["store"].ledger().rules_at().values()
                  if x.kind == "cell" and x.who == [who] and str(x.meta.get("source", "")).startswith("話した"))


# 1 言い直し

def test_言い直しは前の頼みを外して入れる(place):
    a, b = place["a"], place["b"]
    first, again = f"{a}さん、10日は休みで", f"{a}さんじゃなくて{b}さん、10日が休みで"
    s = _session(place, {first: [{"type": "day_off", "staff": a, "days": [10]}],
                         again: [{"type": "day_off", "staff": b, "days": [10], "strength": "wish"}]})
    assert _say(s, first).kind == "applied"
    tb, rev = _table(place), _rev(place)
    r = s.say(again)
    assert r.kind == "fix_offer", r
    assert r.message.startswith(f"{a}さんの10日の休みを外して、{b}さんの10日を休みにします"), r.message
    assert _table(place) == tb and _rev(place) == rev        # 確かめる前は帳面も表も変えない
    # 開き直しても同じ文で出す
    pend = [p for p in place["store"].pending(*place["ym"]) if p["status"] == "offer"]
    assert pend and pend[0]["offer"]["message"] == r.message
    r = s.accept(r.uid)
    assert r.kind == "applied", r
    assert _cells(place, a) == [] and _cells(place, b) == [(10,)]
    assert _table(place)[b][9] == "公"
    # 外したルールの番号を、新しい頼みに使い回さない
    led = place["store"].ledger()
    adds = [t["rule"]["id"] for t in led.txs if t["op"] == "add"]
    assert len(adds) == len(set(adds)), adds
    # ひとつ戻すと、外した頼みも入れた頼みも戻る
    s.undo()
    assert _cells(place, a) == [(10,)] and _cells(place, b) == []
    assert _table(place) == tb


def test_言い直しはやめれば何も変えない(place):
    a, b = place["a"], place["b"]
    first, again = f"{a}さん、10日は休みで", f"{a}さんではなくて{b}さん、10日が休みで"
    s = _session(place, {first: [{"type": "day_off", "staff": a, "days": [10]}],
                         again: [{"type": "day_off", "staff": b, "days": [10]}]})
    _say(s, first)
    tb, rev = _table(place), _rev(place)
    r = s.say(again)
    assert r.kind == "fix_offer"
    s.cancel(r.uid)
    assert _table(place) == tb and _rev(place) == rev and _cells(place, a) == [(10,)]


def test_言い直しで前の頼みが2件あれば聞き返す(place):
    a, b = place["a"], place["b"]
    one, two = f"{a}さん、10日は休みで", f"{a}さん、9日と10日は早番にしないで"
    again = f"{a}さんじゃなくて{b}さん、10日が休みで"
    fac = place["store"].facility()
    sym = next(st.symbol for st in fac.shift_types if st.kind not in ("rest", "leave"))
    s = _session(place, {one: [{"type": "day_off", "staff": a, "days": [10]}],
                         two: [{"type": "avoid_cell", "staff": a, "days": [9, 10], "shift": sym}],
                         again: [{"type": "day_off", "staff": b, "days": [10]}]})
    _say(s, one)
    _say(s, two)
    tb, rev = _table(place), _rev(place)
    r = s.say(again)
    assert r.kind == "ask" and "2件" in r.message, r
    assert _table(place) == tb and _rev(place) == rev


def test_言い直しで前の頼みが無ければ今どおり入れる(place):
    a, b = place["a"], place["b"]
    again = f"{a}さんじゃなくて{b}さん、10日が休みで"
    s = _session(place, {again: [{"type": "day_off", "staff": b, "days": [10]}]})
    r = _say(s, again)
    assert r.kind == "applied", r
    assert _cells(place, b) == [(10,)]


def test_言い直しの言葉が無ければ前の頼みを外さない(place):
    """「赤坂さんも井上さんも」のように2人を言っただけでは、前の頼みを外す案にしない。"""
    a, b = place["a"], place["b"]
    first, two = f"{a}さん、10日は休みで", f"{a}さんに続いて{b}さんも、10日が休みで"
    s = _session(place, {first: [{"type": "day_off", "staff": a, "days": [10]}],
                         two: [{"type": "day_off", "staff": b, "days": [10]}]})
    _say(s, first)
    r = _say(s, two)
    assert r.kind == "applied", r
    assert _cells(place, a) == [(10,)] and _cells(place, b) == [(10,)]


# 2 人も日も無い取り消し

def test_さっきのやっぱりなしは確かめてから外す(place):
    a = place["a"]
    first, cancel = f"{a}さん、8日は休みで", "さっきのやっぱりなし"
    s = _session(place, {first: [{"type": "day_off", "staff": a, "days": [8]}],
                         cancel: [{"type": "cancel", "staff": None, "days": None, "shift": None}]})
    assert _say(s, first).kind == "applied"
    tb, rev = _table(place), _rev(place)
    r = s.say(cancel)
    assert r.kind == "fix_offer", r
    assert r.message == f"{a}さんの8日の休みを取り消しますか。表のマスはそのままです", r.message
    assert _table(place) == tb and _rev(place) == rev
    r = s.accept(r.uid)
    assert r.kind == "applied", r
    assert _cells(place, a) == [] and _table(place) == tb


def test_取り消せる頼みが2件あればやっぱりなしは聞き返す(place):
    a, c = place["a"], place["c"]
    one, two, cancel = f"{a}さん、8日は休みで", f"{c}さん、11日は休みで", "さっきのやっぱりなし"
    s = _session(place, {one: [{"type": "day_off", "staff": a, "days": [8]}],
                         two: [{"type": "day_off", "staff": c, "days": [11]}],
                         cancel: [{"type": "cancel", "staff": None, "days": None, "shift": None}]})
    _say(s, one)
    _say(s, two)
    tb, rev = _table(place), _rev(place)
    r = s.say(cancel)
    assert r.kind == "ask" and "2件" in r.message, r
    assert _table(place) == tb and _rev(place) == rev


def test_取り消せる頼みが無ければやっぱりなしは聞き返す(place):
    cancel = "さっきのやっぱりなし"
    s = _session(place, {cancel: [{"type": "cancel", "staff": None, "days": None, "shift": None}]})
    rev = _rev(place)
    r = s.say(cancel)
    assert r.kind == "ask" and _rev(place) == rev, r


def test_名前を言ったのに人が無い取り消しは別の人の頼みを外さない(place):
    """読み取りが名前を落としても、一言に名簿の人が出ていれば、ほかの人の頼みを外す案にしない。"""
    a, c = place["a"], place["c"]
    first, cancel = f"{c}さん、11日は休みで", f"さっきの{a}さんの、やっぱりなし"
    s = _session(place, {first: [{"type": "day_off", "staff": c, "days": [11]}],
                         cancel: [{"type": "cancel", "staff": None, "days": None, "shift": None}]})
    _say(s, first)
    rev = _rev(place)
    r = s.say(cancel)
    assert r.kind == "ask" and _rev(place) == rev, r


# 3 全員と個人

def _night(place):
    fac = place["store"].facility()
    return next(st.symbol for st in fac.shift_types if st.symbol == "夜" or "夜勤" == st.name)


def test_みんなと名指しの回数が一緒に効く(place):
    b = place["b"]
    sym = _night(place)
    t0 = _table(place)
    hi = max(row.count(sym) for row in t0.values())   # 元の表の一番多い回数（解けなくならない数）
    text = f"みんな夜勤は月{hi}回までで、{b}さんだけは{hi - 1}回までにして"
    s = _session(place, {text: [
        {"type": "count_limit", "staff": None, "shift": sym, "min": None, "max": hi, "until_day": None,
         "strength": "operational"},
        {"type": "count_limit", "staff": [b], "shift": sym, "min": None, "max": hi - 1, "until_day": None,
         "strength": "operational"}]})
    r = s.say(text)
    assert r.kind == "ask_period", r                 # 2本まとめて1回だけ聞く
    r = s.answer(r.uid, "rule")
    assert r.kind == "applied", r
    assert not r.warning, r.warning
    led = place["store"].ledger().rules_at()
    new = [led[i] for i in r.rule_ids]
    assert sorted((x.who or []) for x in new) == [[], [b]]
    assert all(x.scope == "rule" for x in new)
    t = _table(place)
    assert t[b].count(sym) <= hi - 1
    assert all(row.count(sym) <= hi for row in t.values())
    every = next(x for x in new if not x.who)
    for g in with_overrides(month_rules(place["store"], *place["ym"], with_premise=False)):
        if g.id == every.id:
            assert b in g.meta.get("except_max", [])


def test_みんなが無いのに人の無い回数が混ざれば聞き返す(place):
    b = place["b"]
    sym = _night(place)
    text = f"夜勤は月4回までで、{b}さんだけは3回までにして"
    s = _session(place, {text: [
        {"type": "count_limit", "staff": None, "shift": sym, "max": 4},
        {"type": "count_limit", "staff": [b], "shift": sym, "max": 3}]})
    rev = _rev(place)
    r = s.say(text)
    assert r.kind == "ask" and _rev(place) == rev, r


def test_みんなでも名指しの人の分が無ければ聞き返す(place):
    """「みんなの夜勤を井上さんと同じ回数に」が人の無い1本だけで返ったら、井上さんの分が落ちているので当てない。"""
    b = place["b"]
    sym = _night(place)
    text = f"みんなの夜勤は{b}さんと同じ月4回までにして"
    s = _session(place, {text: [{"type": "count_limit", "staff": None, "shift": sym, "max": 4}]})
    rev = _rev(place)
    r = s.say(text)
    assert r.kind == "ask" and _rev(place) == rev, r
