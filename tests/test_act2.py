"""2幕の通し。待機当番を言葉で足す（D1）→ 配る → 休みの電話で待機の人だけ（D3）→ 了承で記録（D5）・断られた（D8）。"""
import copy
import json

import pytest

from shiftvoice.reader import RecordedBuilder
from shiftvoice.standby import ASK_WIDER

from conftest import REPLIES
from test_t1_act1 import ACT1

BUILDS = REPLIES.parent / "builds.json"
STANDBY = "毎月、土日は休みの人から待機1人。急な休みの電話はその人にだけ。引き受けた回数は、翌月も覚えておいて"
CALL = "青木さんから電話で、11月1日の日曜、お休みしたいって"
# 小島さんの残り2回の穴埋め（2幕を撮った後に、2幕と同じ［了承をもらって直す］で作る。12章のデモ設定）
LATER_CALLS = ["久保さんから電話で、11月14日の土曜、お休みしたいって",
               "篠原さんから電話で、11月22日の日曜、お休みしたいって"]


def kojima_rest(s):
    """2幕の後、小島さんの残り2回を実際の操作で作る（記録を足して代えない）。"""
    out = []
    for text in LATER_CALLS:
        r = s.say(text)
        assert r.kind == "standby_offer" and r.extra.get("duty", "小島") == "小島", r
        out.append(s.accept(r.uid))
    return out


@pytest.fixture
def act2(sess):
    sess.builder = RecordedBuilder.from_file(BUILDS)
    sess.base_date = "2026-11-01"
    for text, ans in ACT1:
        r = sess.say(text)
        if r.kind == "ask_period":
            sess.answer(r.uid, ans)
    return sess


def test_d1_standby_by_words(act2):
    before = json.dumps(act2.store.month(2026, 11).table)
    r = act2.say(STANDBY)
    assert r.kind == "applied"                       # 「毎月」を言っているので聞かない
    assert json.dumps(act2.store.month(2026, 11).table) == before   # 表のマスは1つも変えない
    st = act2.state()
    card = next(c for c in st["cards"] if c["kind"] == "layer")
    assert card["scope"] == "rule"
    assert "連絡を受ける当番（出勤は本人の了承の後）" in card["meta"]["explain"]
    assert "引き受けた回数を翌月へ" in card["meta"]["explain"]
    assign = st["layers"]["standby"]["assign"]
    assert assign["1"] == ["小島"] and all(len(v) == 1 for v in assign.values())
    sent = json.dumps([s.__dict__ for s in act2.builder.sent], ensure_ascii=False)
    assert "小島" not in sent and "青木" not in sent


def test_d3_d5_accept(act2):
    act2.say(STANDBY)
    act2.distribute()
    t0 = json.dumps(act2.store.month(2026, 11).table)
    r = act2.say(CALL)
    assert r.kind == "standby_offer", r.message
    assert r.extra["duty"] == "小島" and len(r.changes) == 2
    assert "小島さん 9日 → 8日" in r.summary and "青木さん 9日 → 10日" in r.summary
    assert json.dumps(act2.store.month(2026, 11).table) == t0          # 了承の前は変えない
    assert act2.store.acceptances() == [a for a in act2.store.acceptances() if a.staff != "小島"]
    a = act2.accept(r.uid)
    assert a.kind == "applied" and a.summary == "11月の引き受け 0回 → 1回"
    t = act2.store.month(2026, 11).table
    assert t["青木"][0] == "公" and t["小島"][0] == "日"
    rec = [x for x in act2.store.acceptances() if x.staff == "小島"]
    assert len(rec) == 1 and rec[0].date == "2026-11-01" and rec[0].op_id == r.uid
    # 基準日を月末にすると、用意した記録も数える（3幕の前）
    act2.base_date = "2026-11-30"
    assert act2.state()["accepted"] == {"小島": 1, "田村": 2, "菊池": 1}
    # ひとつ戻すと、表・公休の足し引き・記録もまとめて戻る（16章の11）
    act2.undo()
    assert json.dumps(act2.store.month(2026, 11).table) == t0
    assert not [x for x in act2.store.acceptances() if x.staff == "小島"]
    assert act2.store.month(2026, 11).rest_adjust == {}


def test_d8_refused_changes_nothing(act2):
    act2.say(STANDBY)
    act2.distribute()
    t0 = json.dumps(act2.store.month(2026, 11).table)
    n = len(act2.store.acceptances())
    r = act2.say(CALL)
    x = act2.refuse(r.uid)
    assert x.kind == "ask" and x.message == ASK_WIDER
    assert json.dumps(act2.store.month(2026, 11).table) == t0
    assert len(act2.store.acceptances()) == n
    assert act2.accept(r.uid).kind == "error"    # 断られた案は了承できない


def test_d7_already_off_via_words(act2):
    act2.say(STANDBY)
    act2.distribute()
    r = act2.say("小島さん、1日休みで")
    assert r.kind == "ask" and r.message == "もう休みです"


def test_astra1_stale_offer_is_rebuilt(act2):
    """古い待機の案を了承しても、その後の直しを上書きしない（作り直す）。"""
    act2.say(STANDBY)
    act2.distribute()
    r = act2.say(CALL)
    x = act2.say("石田さん、木曜は早番にしないで。毎月")   # 案の後に別の直しが入った
    assert x.kind == "applied"
    after_other = act2.store.month(2026, 11).table
    a = act2.accept(r.uid)
    assert a.kind == "standby_offer" and "作り直しました" in a.message, a.message
    assert act2.accept(a.uid).kind == "applied"
    t = act2.store.month(2026, 11).table
    assert all(t["石田"][d - 1] != "早" for d in (5, 12, 19, 26))   # 後の直しが消えていない（木曜）
    assert t["青木"][0] == "公"
    assert {n for n in t if t[n] != after_other[n]} <= {"青木", "小島"}


def test_astra2_multi_day_after_distribution_needs_ok(act2):
    """配った後の複数日の休みは、［これで直す］まで表を変えない。"""
    act2.say(STANDBY)
    act2.distribute()
    t0 = json.dumps(act2.store.month(2026, 11).table)
    r = act2.say("篠原さん、20日は遅番で")   # 20日は金曜（待機の日ではない）
    assert r.kind == "fix_offer"
    assert json.dumps(act2.store.month(2026, 11).table) == t0     # ［これで直す］の前は変えない
    assert act2.accept(r.uid).kind == "applied"
    assert act2.store.month(2026, 11).table["篠原"][19] == "遅"


def test_astra3_crash_before_record_recovers(act2, monkeypatch):
    """了承の途中（帳面の後）で止まっても、開き直すと記録と表がそろう。"""
    act2.say(STANDBY)
    act2.distribute()
    r = act2.say(CALL)
    st = act2.store
    orig = st.save_acceptances
    monkeypatch.setattr(st, "save_acceptances", lambda items: (_ for _ in ()).throw(OSError("止まった")))
    with pytest.raises(OSError):
        act2.accept(r.uid)
    monkeypatch.setattr(st, "save_acceptances", orig)
    from shiftvoice.reader import RecordedReader
    from shiftvoice.session import Session
    from shiftvoice.store import Store
    s2 = Session(Store(st.root), 2026, 11, RecordedReader({}), now=act2.now)
    s2.open()
    assert [a.staff for a in s2.store.acceptances() if a.op_id == r.uid] == ["小島"]
    assert s2.store.month(2026, 11).table["小島"][0] == "日"
    assert s2.store.pending(2026, 11) == []


def test_d4_wider_after_refused(act2):
    """D4 案E。待機の人に断られたら「ほかの人に頼む案も見ますか」→ その日に休みの人へ1人ずつ。
    動くのは頼んだ人と頼まれた人だけで、了承をもらって直すとその人の引き受けとして記録する。"""
    s = act2
    s.say(STANDBY)
    s.distribute()
    t0 = copy.deepcopy(s.store.month(2026, 11).table)
    r = s.say(CALL)
    x = s.refuse(r.uid)
    assert x.extra.get("wider") and x.message == ASK_WIDER
    o = s.wider(r.uid)
    assert o.kind == "standby_offer", o
    helper = o.extra["duty"]
    assert helper not in ("小島", "青木") and t0[helper][0] == "公"
    assert o.message.startswith(f"待機ではない、その日休みの{helper}さんに頼む案です")
    a = s.accept(r.uid)
    assert a.kind == "applied"
    t1 = s.store.month(2026, 11).table
    assert {n for n in t0 if t0[n] != t1[n]} <= {"青木", helper}
    assert t1["青木"][0] == "公" and t1[helper][0] != "公"
    assert [x.staff for x in s.store.acceptances() if x.date == "2026-11-01"] == [helper]
    # 断った小島さんの記録は増えない
    assert not any(x.staff == "小島" and x.date == "2026-11-01" for x in s.store.acceptances())


def test_d4_wider_refused_again_tries_next(act2):
    s = act2
    s.say(STANDBY)
    s.distribute()
    r = s.say(CALL)
    s.refuse(r.uid)
    first = s.wider(r.uid).extra["duty"]
    s.refuse(r.uid)
    second = s.wider(r.uid)
    assert second.kind == "standby_offer" and second.extra["duty"] not in (first, "小島")
