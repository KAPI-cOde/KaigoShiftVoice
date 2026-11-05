"""2人の勤務の入れ替え（「小川さんと加藤さんの24日、入れ替えて」）。
その日の今の表から2人の勤務を取って取り替える。決まりで決められない所（2人でない・同じ勤務・言った勤務が表と違う・
公休の数が変わる・明けや研修・入れない勤務・前の頼みとぶつかる）は、表を変えずに聞き返す。
前は「この形の頼みは、まだ扱えません」と返していた（2026-11-05、別の系統の AI が書いた現場の言い方2件で3回とも）。
Claude には swap の型の名前だけを書かせ、2人と日は一言の文から手元で取る（指示文に足すと、ほかの一言の読みが崩れた）。
API は呼ばない（録った形の返事で回す）。"""
import datetime as dt
import json
import shutil

from shiftvoice.reader import RecordedReader
from shiftvoice.session import JST, Session
from shiftvoice.store import Store

NOW = dt.datetime(2026, 11, 5, 22, 0, tzinfo=JST)


def _session(built_dir, tmp_path, replies):
    root = tmp_path / "h"
    shutil.copytree(built_dir, root)
    s = Session(Store(root), 2026, 11, RecordedReader(replies), now=lambda: NOW)
    s.open()
    return s


def _reply(*items):
    return json.dumps({"items": list(items)}, ensure_ascii=False)


def _swap(staff, days):
    return {"type": "swap", "staff": staff, "days": days, "reason": None}


def _table(s):
    return {n: list(r) for n, r in s.store.month(2026, 11).table.items()}


def test_同じ日の2人の勤務が入れ替わる(built_dir, tmp_path):
    text = "職員05さんと職員06さんの24日、入れ替えて"
    s = _session(built_dir, tmp_path, {text: _reply(_swap(["職員05", "職員06"], [24]))})
    before = _table(s)
    assert (before["小川"][23], before["加藤"][23]) == ("遅", "早")
    r = s.say("小川さんと加藤さんの24日、入れ替えて")
    assert r.kind == "applied", r
    after = _table(s)
    assert (after["小川"][23], after["加藤"][23]) == ("早", "遅")
    assert r.message == "小川さんと加藤さんの24日を入れ替え（小川さん 早番・加藤さん 遅番）"
    # 同じユニットの早番と遅番なので、ほかのマスは動かない
    assert sorted((c["staff"], c["day"]) for c in r.changes) == [("加藤", 24), ("小川", 24)]


def test_言った勤務が表と合えば入れ替える(built_dir, tmp_path):
    text = "職員05さんの24日の遅番、職員06さんの早番と入れ替えて"
    s = _session(built_dir, tmp_path, {text: _reply(_swap(["職員05", "職員06"], [24]))})
    r = s.say("小川さんの24日の遅番、加藤さんの早番と入れ替えて")
    assert r.kind == "applied", r
    after = _table(s)
    assert (after["小川"][23], after["加藤"][23]) == ("早", "遅")


def test_入れ替えた2つのマスは片方だけ外れない(built_dir, tmp_path):
    """斉藤さんには「水曜は早番に入れない」の希望がある。水曜の11日に杉山さんの早番と入れ替えても、片方だけの
    入れ替え（杉山さんだけ日勤になり、ほかの人が早番に回る）にしない。本人の希望の強さでは、片方だけになった"""
    text = "職員12さんと職員14さんの11日、入れ替えて"
    s = _session(built_dir, tmp_path, {text: _reply(_swap(["職員12", "職員14"], [11]))})
    before = _table(s)
    assert (before["斉藤"][10], before["杉山"][10]) == ("日", "早")
    r = s.say("斉藤さんと杉山さんの11日、入れ替えて")
    assert r.kind == "applied", r
    after = _table(s)
    assert (after["斉藤"][10], after["杉山"][10]) == ("早", "日")
    swapped = [x for x in s.store.ledger().rules_at().values() if "入れ替え" in x.name]
    assert len(swapped) == 2 and all(x.flex == "fixed" for x in swapped)


def test_夜勤を入れ替えると翌日の明けとその後の休みも動く(built_dir, tmp_path):
    text = "職員01さんと職員02さんの3日、入れ替えて"
    s = _session(built_dir, tmp_path, {text: _reply(_swap(["職員01", "職員02"], [3]))})
    before = _table(s)
    assert before["青木"][2:5] == ["夜", "明", "公"] and before["石田"][2] == "遅"
    r = s.say("青木さんと石田さんの3日、入れ替えて")
    assert r.kind == "applied", r
    after = _table(s)
    assert after["青木"][2] == "遅" and after["石田"][2:5] == ["夜", "明", "公"]
    assert after["青木"][3] != "明"
    for n in ("青木", "石田"):   # 公休の数は変わらない
        assert after[n].count("公") == before[n].count("公")


def test_同じ勤務なら聞き返す(built_dir, tmp_path):
    text = "職員01さんと職員08さんの1日、入れ替えて"
    s = _session(built_dir, tmp_path, {text: _reply(_swap(["職員01", "職員08"], [1]))})
    before = _table(s)
    assert before["青木"][0] == before["落合"][0] == "日"
    r = s.say("青木さんと落合さんの1日、入れ替えて")
    assert r.kind == "ask" and r.message.startswith("青木さんと落合さんの1日は、2人とも日勤です"), r
    assert _table(s) == before


def test_3人や1人なら聞き返す(built_dir, tmp_path):
    three = "職員05さんと職員06さんと職員07さんの24日、入れ替えて"
    one = "職員05さんの24日、入れ替えて"
    s = _session(built_dir, tmp_path, {three: _reply(_swap(["職員05", "職員06", "職員07"], [24])),
                                       one: _reply(_swap(["職員05"], [24]))})
    before = _table(s)
    for t in ("小川さんと加藤さんと岡田さんの24日、入れ替えて", "小川さんの24日、入れ替えて"):
        r = s.say(t)
        assert r.kind == "ask" and r.message.startswith("入れ替える2人を教えてください"), r
    assert _table(s) == before


def test_日が無ければ聞き返す(built_dir, tmp_path):
    text = "職員05さんと職員06さん、入れ替えて"
    s = _session(built_dir, tmp_path, {text: _reply(_swap(["職員05", "職員06"], []))})
    before = _table(s)
    r = s.say("小川さんと加藤さん、入れ替えて")
    assert r.kind == "ask" and "何日の勤務を入れ替えるか" in r.message, r
    assert _table(s) == before


def test_言った勤務が表と違えば表の勤務を見せて聞き返す(built_dir, tmp_path):
    """表を見ずに言った一言を、表のマスのまま取り替えない。24日の落合さんは日勤、菊池さんは早番（言ったのは日勤と
    遅番）。取り替えると落合さんが早番になり、言った人が思っていない勤務になる。22日の小川さんは早番、加藤さんは公休
    （言ったのは遅番と日勤）"""
    a = "職員08さんと職員09さん、24日の日勤と遅番、入れ替えてくれる？ 職員08さんが遅番になる方ね。"
    b = "職員05さんの22日の遅番と、職員06さんの22日の日勤、本人同士で話ついたから入れ替えて。"
    s = _session(built_dir, tmp_path, {a: _reply(_swap(["職員08", "職員09"], [24])),
                                       b: _reply(_swap(["職員05", "職員06"], [22]))})
    before = _table(s)
    assert (before["落合"][23], before["菊池"][23]) == ("日", "早")
    r = s.say("落合さんと菊池さん、24日の日勤と遅番、入れ替えてくれる？ 落合さんが遅番になる方ね。")
    assert r.kind == "ask" and r.message.startswith("表では、落合さんの24日は日勤、菊池さんは早番です。言われた勤務と違う"), r
    r = s.say("小川さんの22日の遅番と、加藤さんの22日の日勤、本人同士で話ついたから入れ替えて。")
    assert r.kind == "ask" and r.message.startswith("表では、小川さんの22日は早番、加藤さんは公休です。言われた勤務と違う"), r
    assert _table(s) == before


def test_休みと勤務の入れ替えで公休の数が変わる時は聞き返す(built_dir, tmp_path):
    text = "職員05さんと職員06さんの22日、入れ替えて"
    s = _session(built_dir, tmp_path, {text: _reply(_swap(["職員05", "職員06"], [22]))})
    before = _table(s)
    assert (before["小川"][21], before["加藤"][21]) == ("早", "公")
    r = s.say("小川さんと加藤さんの22日、入れ替えて")
    assert r.kind == "ask", r
    assert "小川さんの公休が1日増え、加藤さんの公休が1日減る" in r.message
    assert _table(s) == before


def test_2つの日で公休の数が変わらなければ入れ替える(built_dir, tmp_path):
    """3日は小川さんが公休・加藤さんが早番、22日は小川さんが早番・加藤さんが公休。2つの日を一緒に入れ替えると、
    2人とも公休の数は変わらない"""
    text = "職員05さんと職員06さんの3日と22日、入れ替えて"
    s = _session(built_dir, tmp_path, {text: _reply(_swap(["職員05", "職員06"], [3, 22]))})
    before = _table(s)
    assert (before["小川"][2], before["加藤"][2], before["小川"][21], before["加藤"][21]) == ("公", "早", "早", "公")
    r = s.say("小川さんと加藤さんの3日と22日、入れ替えて")
    assert r.kind == "applied", r
    after = _table(s)
    assert (after["小川"][2], after["加藤"][2], after["小川"][21], after["加藤"][21]) == ("早", "公", "公", "早")
    for n in ("小川", "加藤"):
        assert after[n].count("公") == before[n].count("公")


def test_入れない勤務や明けや研修は聞き返す(built_dir, tmp_path):
    night = "職員09さんと職員14さんの2日、入れ替えて"     # 杉山さんは夜勤に入れない
    ake = "職員09さんと職員14さんの3日、入れ替えて"       # 菊池さんの3日は明け
    train = "職員01さんと職員03さんの12日、入れ替えて"    # 上野さんの12日は研修
    s = _session(built_dir, tmp_path, {night: _reply(_swap(["職員09", "職員14"], [2])),
                                       ake: _reply(_swap(["職員09", "職員14"], [3])),
                                       train: _reply(_swap(["職員01", "職員03"], [12]))})
    before = _table(s)
    assert (before["菊池"][1], before["杉山"][1], before["菊池"][2], before["上野"][11]) == ("夜", "日", "明", "研")
    r = s.say("菊池さんと杉山さんの2日、入れ替えて")
    assert r.kind == "ask" and r.message.startswith("杉山さんは夜勤に入れないので"), r
    r = s.say("菊池さんと杉山さんの3日、入れ替えて")
    assert r.kind == "ask" and r.message.startswith("菊池さんの3日は明けです"), r
    r = s.say("青木さんと上野さんの12日、入れ替えて")
    assert r.kind == "ask" and r.message.startswith("上野さんの12日は研修です"), r
    assert _table(s) == before


def test_前に頼んだ同じ日のマスとぶつかる時は聞き返す(built_dir, tmp_path):
    first = "職員05さん、24日は遅番でお願い"
    swap = "職員05さんと職員06さんの24日、入れ替えて"
    s = _session(built_dir, tmp_path, {
        first: _reply({"type": "set_cell", "staff": "職員05", "days": [24], "shift": "遅", "strength": "wish",
                       "reason": None}),
        swap: _reply(_swap(["職員05", "職員06"], [24]))})
    assert s.say("小川さん、24日は遅番でお願い").kind == "applied"
    before = _table(s)
    r = s.say("小川さんと加藤さんの24日、入れ替えて")
    assert r.kind == "ask" and r.message.startswith("小川さんの24日には、前からの「小川さん 24日 遅番」があります"), r
    assert _table(s) == before


def test_入れ替えとほかの頼みが混ざれば聞き返す(built_dir, tmp_path):
    text = "職員05さんと職員06さんの24日、入れ替えて。職員05さんは25日休み"
    s = _session(built_dir, tmp_path, {text: _reply(
        _swap(["職員05", "職員06"], [24]),
        {"type": "day_off", "staff": "職員05", "days": [25], "reason": None})})
    before = _table(s)
    r = s.say("小川さんと加藤さんの24日、入れ替えて。小川さんは25日休み")
    assert r.kind == "ask" and r.message.startswith("入れ替えは、ほかの頼みと分けて"), r
    assert _table(s) == before


def test_型の名前だけの返事でも_一言の名前と日で入れ替える(built_dir, tmp_path):
    """Claude は swap の型の名前と summary だけを返す（指示文のとおり）。2人と日は一言から取る。"""
    text = "職員05さんと職員06さんの24日、入れ替えて"
    s = _session(built_dir, tmp_path, {text: _reply({"type": "swap", "summary": "2人の24日の入れ替え"})})
    r = s.say("小川さんと加藤さんの24日、入れ替えて")
    assert r.kind == "applied", r
    after = _table(s)
    assert (after["小川"][23], after["加藤"][23]) == ("早", "遅")
