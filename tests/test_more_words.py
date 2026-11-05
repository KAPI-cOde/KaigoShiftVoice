"""現場の人が言いそうな一言で止まっていた3つ（2026-11-03、取り込んで作った施設で本物の Haiku に言わせた結果から）。

- 曜日つきの人数「土日は日勤を2人にしてほしい」。人数のルールに曜日を持たせる。毎月か今月だけかは聞く
- 日付の無い取り消し「さっきの赤坂さんの休み、やっぱりなしでお願い」。その人がこの月に話した頼みが1つに決まる時だけ、
  表を変える前に「〜を取り消しますか」と確かめる。2つ以上・0なら聞き返す
- 曜日の固定の休み「遠藤さんは日曜日は休みで固定してほしい」。その曜日を休みにする曜日のルールにする

どれも、作った施設（見本の表から取り込み）とデモの施設（ひだまり）の両方で確かめる。迷う言い方は聞き返す。
"""
import json
import shutil
from pathlib import Path

import pytest

from shiftvoice.build import month_rules
from shiftvoice.names import Masker
from shiftvoice.onboard import create_facility, infer, parse_paste
from shiftvoice.reader import RecordedReader
from shiftvoice.rules import expand, with_overrides
from shiftvoice.session import Session
from shiftvoice.store import Store

from shiftvoice.period import resolve_relative_days

from conftest import FIXED_NOW

SAMPLE = Path(__file__).resolve().parents[1] / "data" / "samples" / "komorebi-2026-11.tsv"

# 施設ごとの見本（作った施設は12月、デモの施設は11月）
PLACES = {
    "komorebi": {"ym": (2026, 12), "a": "赤坂", "b": "遠藤", "sun": [6, 13, 20, 27],
                 "weekend": [5, 6, 12, 13, 19, 20, 26, 27], "day": ["日"], "n": 2},
    "hidamari": {"ym": (2026, 11), "a": "岡田", "b": "青木", "sun": [1, 8, 15, 22, 29],
                 "weekend": [1, 7, 8, 14, 15, 21, 22, 28, 29], "day": ["日", "研"], "n": 3},
}


@pytest.fixture(scope="module")
def komorebi_dir(tmp_path_factory):
    prop = infer(parse_paste(SAMPLE.read_text(encoding="utf-8")), 2026, 11)
    out = tmp_path_factory.mktemp("komorebi") / "f"
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
    replies = {}
    y, m = place["ym"]
    for text, items in said.items():
        items = [{k: (masker.to_code.get(v, v) if k == "staff" and isinstance(v, str) else v) for k, v in it.items()}
                 for it in items]
        # 送る文と同じに、伏せた後で「今度の日曜」なども表の月の日付に置き換える（session._read）
        sent = resolve_relative_days(masker.mask(text)[0], FIXED_NOW.date(), y, m)
        replies[sent] = json.dumps({"items": items}, ensure_ascii=False)
    s = Session(store, y, m, RecordedReader(replies), now=lambda: FIXED_NOW)
    s.open()
    return s


def _table(place):
    y, m = place["ym"]
    return {k: list(v) for k, v in place["store"].month(y, m).table.items()}


def _rev(place):
    return place["store"].ledger().rev


def _say(s, text, scope="rule"):
    r = s.say(text)
    if r.kind == "ask_period":
        r = s.answer(r.uid, scope)
    return r


# 1 曜日つきの人数

def test_土日の日勤の人数が効く(place):
    text = f"土日は日勤を{place['n']}人にしてほしい"
    s = _session(place, {text: [{"type": "coverage", "shift": "日", "min": place["n"], "max": None,
                                 "weekdays": ["sat", "sun"], "holidays": False, "strength": "operational"}]})
    r = s.say(text)
    assert r.kind == "ask_period"          # 毎月か今月だけかは聞く
    r = s.answer(r.uid, "rule")
    assert r.kind == "applied", r
    assert not r.warning, r.warning
    t = _table(place)
    for d in place["weekend"]:
        assert sum(row[d - 1] in place["day"] for row in t.values()) >= place["n"], d
    new = place["store"].ledger().rules_at()[r.rule_ids[0]]
    assert new.kind == "coverage" and new.scope == "rule" and new.params["weekdays"] == ["sat", "sun"]
    assert new.params["shifts"] == place["day"]      # 今の日勤の人数のルールと同じ数え方（ひだまりは研修を含む）
    # 場面は土日だけ（平日には掛からない）
    y, m = place["ym"]
    month = place["store"].month(y, m)
    keys = {i.key.split("/")[1] for i in expand(new, place["store"].facility(), place["store"].staff(), month)}
    assert keys == {str(d) for d in place["weekend"]}


def test_土日の早番を2人にすると全体の早番の上限は土日だけ外れる(place):
    if place["name"] != "komorebi":
        pytest.skip("早番の人数がちょうど1人の施設で確かめる")
    text = "土日は早番を2人にしてほしい"
    s = _session(place, {text: [{"type": "coverage", "shift": "早", "min": 2, "max": None,
                                 "weekdays": ["sat", "sun"], "holidays": False}]})
    r = _say(s, text)
    assert r.kind == "applied" and not r.warning, r
    t = _table(place)
    for d in range(1, 32):
        n = sum(row[d - 1] == "早" for row in t.values())
        assert n == (2 if d in place["weekend"] else 1), (d, n)


@pytest.mark.parametrize("items", [
    [{"type": "coverage", "shift": "日", "min": None, "max": None, "weekdays": ["sat", "sun"]}],   # 人数が無い
    [{"type": "coverage", "shift": "日", "min": 2, "weekdays": ["土日"]}],                          # 曜日が読めない
    [{"type": "coverage", "shift": "日", "min": 3, "max": 2, "weekdays": ["sat"]}],                # 下限が上限より大きい
    [{"type": "coverage", "shift": "待機", "min": 1, "weekdays": ["sat", "sun"]}],                  # 表に無い勤務
])
def test_読めない人数の頼みは聞き返す(place, items):
    text = "土日はもう少し人を増やして"
    s = _session(place, {text: items})
    b, rev = _table(place), _rev(place)
    r = s.say(text)
    assert r.kind == "ask", r
    assert _table(place) == b and _rev(place) == rev


def test_人の名前がある人数の頼みは聞き返す(place):
    """人数のルールは全員に掛かる。名前を言った一言が人数の形で返ってきたら当てない。"""
    text = f"{place['a']}さんがいる土日は日勤を2人にして"
    s = _session(place, {text: [{"type": "coverage", "shift": "日", "min": 2, "weekdays": ["sat", "sun"]}]})
    b, rev = _table(place), _rev(place)
    r = s.say(text)
    assert r.kind == "ask", r
    assert _table(place) == b and _rev(place) == rev


def test_待機の当番は人数のルールにしない(place):
    """表に無い当番は今どおり型に収まらない頼み（custom）。人数の形にしない。"""
    text = "毎月、土日は休みの人から待機1人"
    s = _session(place, {text: [{"type": "custom", "summary": "土日の待機当番"}]})
    rev = _rev(place)
    r = s.say(text)
    assert r.kind == "ask" and _rev(place) == rev


# 2 日付の無い取り消し

def test_日付の無い取り消しは確かめてから外す(place):
    a = place["a"]
    first, cancel = f"{a}さん、24日は休みで", f"さっきの{a}さんの休み、やっぱりなしでお願い"
    s = _session(place, {first: [{"type": "day_off", "staff": a, "days": [24]}],
                         cancel: [{"type": "cancel", "staff": a, "days": None, "shift": "公"}]})
    assert _say(s, first).kind == "applied"
    b, rev = _table(place), _rev(place)
    r = s.say(cancel)
    assert r.kind == "fix_offer", r
    assert r.message == f"{a}さんの24日の休みを取り消しますか。表のマスはそのままです"
    assert _table(place) == b and _rev(place) == rev        # 確かめる前は帳面も表も変えない
    r = s.accept(r.uid)
    assert r.kind == "applied", r
    assert _table(place) == b
    left = [x for x in place["store"].ledger().rules_at().values()
            if x.kind == "cell" and x.who == [a] and x.meta.get("source") == "話した"]
    assert left == []


def test_日付の無い取り消しはやめれば何も変えない(place):
    a = place["a"]
    first, cancel = f"{a}さん、24日は休みで", f"さっきの{a}さんの休み、やっぱりなしでお願い"
    s = _session(place, {first: [{"type": "day_off", "staff": a, "days": [24]}],
                         cancel: [{"type": "cancel", "staff": a, "days": None, "shift": "公"}]})
    _say(s, first)
    b, rev = _table(place), _rev(place)
    r = s.say(cancel)
    s.cancel(r.uid)
    assert _table(place) == b and _rev(place) == rev


def test_取り消す頼みが2つあれば聞き返す(place):
    a = place["a"]
    one, two, cancel = f"{a}さん、24日は休みで", f"{a}さん、10日も休みで", f"さっきの{a}さんの休み、やっぱりなしで"
    s = _session(place, {one: [{"type": "day_off", "staff": a, "days": [24]}],
                         two: [{"type": "day_off", "staff": a, "days": [10]}],
                         cancel: [{"type": "cancel", "staff": a, "days": None, "shift": "公"}]})
    _say(s, one)
    _say(s, two)
    b, rev = _table(place), _rev(place)
    r = s.say(cancel)
    assert r.kind == "ask" and "2件" in r.message and "何日" in r.message, r
    assert _table(place) == b and _rev(place) == rev


def test_取り消す頼みが無ければ日付を聞く(place):
    a = place["a"]
    cancel = f"さっきの{a}さんの休み、やっぱりなしで"
    s = _session(place, {cancel: [{"type": "cancel", "staff": a, "days": None, "shift": "公"}]})
    b, rev = _table(place), _rev(place)
    r = s.say(cancel)
    assert r.kind == "ask" and "何日" in r.message, r
    assert _table(place) == b and _rev(place) == rev


def test_ほかの人の頼みは取り消しの候補にしない(place):
    """赤坂さんの取り消しで、遠藤さんの頼みを拾わない（人が違う頼みは数えない）。"""
    a, o = place["a"], place["b"]
    first, cancel = f"{o}さん、24日は休みで", f"さっきの{a}さんの休み、やっぱりなしで"
    s = _session(place, {first: [{"type": "day_off", "staff": o, "days": [24]}],
                         cancel: [{"type": "cancel", "staff": a, "days": None, "shift": "公"}]})
    _say(s, first)
    rev = _rev(place)
    r = s.say(cancel)
    assert r.kind == "ask" and _rev(place) == rev


def test_確かめている間に帳面が変わったら作り直す(place):
    a, o = place["a"], place["b"]
    first, other, cancel = f"{a}さん、24日は休みで", f"{o}さん、11日は休みで", f"さっきの{a}さんの休み、やっぱりなしで"
    s = _session(place, {first: [{"type": "day_off", "staff": a, "days": [24]}],
                         other: [{"type": "day_off", "staff": o, "days": [11]}],
                         cancel: [{"type": "cancel", "staff": a, "days": None, "shift": "公"}]})
    _say(s, first)
    r = s.say(cancel)
    assert r.kind == "fix_offer"
    _say(s, other)
    r2 = s.accept(r.uid)
    assert r2.kind == "fix_offer" and r2.message.startswith("表が変わったので"), r2
    assert s.accept(r2.uid).kind == "applied"


# 3 曜日の固定の休み

@pytest.mark.parametrize("items", [
    [{"type": "weekday_pref", "staff": "B", "weekdays": ["sun"], "shift": "公", "avoid": False}],
    # 本物の Haiku は日付の無い休みで返した。一言に曜日が1つだけあれば曜日の休みに読み替える
    [{"type": "day_off", "staff": "B", "days": None, "strength": "fixed"}],
])
def test_日曜を休みで固定する(place, items):
    b = place["b"]
    text = f"{b}さんは日曜日は休みで固定してほしい"
    items = [{**it, "staff": b} for it in items]
    s = _session(place, {text: items})
    r = s.say(text)
    assert r.kind == "ask_period", r
    r = s.answer(r.uid, "rule")
    assert r.kind == "applied" and not r.warning, r
    t = _table(place)
    assert all(t[b][d - 1] == "公" for d in place["sun"]), t[b]
    new = place["store"].ledger().rules_at()[r.rule_ids[0]]
    assert new.kind == "weekday" and new.params == {"weekdays": ["sun"], "in": ["公"]} and new.who == [b]
    assert "休み" in new.name and "公休" not in new.name
    y, m = place["ym"]
    rules = month_rules(place["store"], y, m, with_premise=False)
    from shiftvoice.rules import check_table
    broken = check_table(rules, place["store"].facility(), place["store"].staff(), place["store"].month(y, m), t)
    assert [x for x in broken if x["flex"] == "fixed"] == []


@pytest.mark.parametrize("text", [
    "{b}さんは休みで固定してほしい",          # 曜日も日付も無い
    "{b}さんは日曜と月曜の休みを固定してほしい・・・いや、やっぱり祝日も",   # 祝日は曜日のルールで表せない
])
def test_曜日の読めない日付の無い休みは聞き返す(place, text):
    b = place["b"]
    text = text.format(b=b)
    s = _session(place, {text: [{"type": "day_off", "staff": b, "days": None}]})
    tb, rev = _table(place), _rev(place)
    r = s.say(text)
    assert r.kind == "ask", r
    assert _table(place) == tb and _rev(place) == rev


def test_曜日の人数は翌月にもその曜日だけ掛かる(place):
    """曜日は日付でなく曜日で持つので、翌月に写しても曜日のまま効く。"""
    text = f"土日は日勤を{place['n']}人にしてほしい"
    s = _session(place, {text: [{"type": "coverage", "shift": "日", "min": place["n"],
                                 "weekdays": ["sat", "sun"], "holidays": True}]})
    r = _say(s, text)
    assert r.kind == "applied", r
    new = place["store"].ledger().rules_at()[r.rule_ids[0]]
    assert new.params["holidays"] is True and "祝" in new.name
    from shiftvoice.model import Month
    nxt = Month(2027, 1, holidays=[1, 11], prev_tail={})
    days = sorted({int(i.key.split("/")[1]) for i in expand(new, place["store"].facility(),
                                                             place["store"].staff(), nxt)})
    assert days == [1, 2, 3, 9, 10, 11, 16, 17, 23, 24, 30, 31]
    # 全体の日勤の上限（作った施設の3人まで）は、上限を上げない限り土日も残る
    for g in with_overrides([new] + month_rules(place["store"], *place["ym"], with_premise=False)):
        if g.kind == "coverage" and g is not new:
            assert not g.meta.get("hi_off")


def test_今度の日曜の休みは曜日のルールにしない(place):
    """1日だけの言い方（今度の日曜）を毎週の休みに広げない。日付を聞く。"""
    b = place["b"]
    text = f"{b}さん、今度の日曜は休みで固定してほしい"
    s = _session(place, {text: [{"type": "day_off", "staff": b, "days": None}]})
    rev = _rev(place)
    r = s.say(text)
    assert r.kind == "ask" and _rev(place) == rev, r
