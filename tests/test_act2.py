"""2幕の通し。待機当番を言葉で足す（D1）→ 配る → 休みの電話で待機の人だけ（D3）→ 了承で記録（D5）・断られた（D8）。"""
import json

import pytest

from shiftvoice.reader import RecordedBuilder
from shiftvoice.standby import ASK_WIDER

from conftest import REPLIES
from test_t1_act1 import ACT1

BUILDS = REPLIES.parent / "builds.json"
STANDBY = "毎月、土日は休みの人から待機1人。急な休みの電話はその人にだけ。引き受けた回数は、翌月も覚えておいて"
CALL = "青木さんから電話で、11月1日の日曜、お休みしたいって"


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
