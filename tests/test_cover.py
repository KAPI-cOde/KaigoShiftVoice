"""残業・早出での穴埋めを一言で記録する（2026-11-04 同僚の介護員4人以上の答え「急な休みの穴は、すでに勤務している人の
残業で埋めることも多い。残業代は出るが、それでよしとせず報いることができるといい」）。
表のマスは変えず、確かめのカードで［記録する］を押した時だけ引き受けの記録に足す。翌月の返す計算でも1回として数える。"""
import json

import pytest

from shiftvoice import nextflow
from shiftvoice.giveback import GiveBack
from shiftvoice.reader import RecordedReader

from test_t2 import snap, unchanged

OVERTIME = "14日は菊池さんが2時間残って埋めてくれた"
NO_DAY = "田村さんが残業して、遅番の穴を埋めてくれた"
EARLY = "今日の夜、久保さんが早く来てくれて助かった"
YESTERDAY = "昨日、千葉さんが1時間早く来て、早番の穴を埋めてくれた"
SAME_DAY = "8日、田村さんが残業で日勤の穴を埋めてくれたよ"   # 用意した記録（田村さん 11月8日の待機）と同じ人・同じ日
UNKNOWN = "森田さんが残って、夜勤の穴を埋めてくれた"          # 名簿にない名前


@pytest.fixture
def s(sess):
    sess.base_date = "2026-11-20"   # 11月20日に話す（14日・8日は過ぎた日）
    return sess


def _mine(s, uid):
    return [a for a in s.store.acceptances() if a.op_id == uid]


def test_cover_recorded_table_unchanged(s):
    """確かめのカード → ［記録する］で引き受けの記録が1件増える。表のマスと帳面は変わらない。"""
    b = snap(s)
    n = len(s.store.acceptances())
    r = s.say(OVERTIME)
    assert r.kind == "cover_offer", r
    assert r.message == "菊池さん 11月14日 残業で穴埋め → 引き受けとして記録します"
    assert len(s.store.acceptances()) == n            # ［記録する］の前は足さない
    a = s.accept(r.uid)
    assert a.kind == "applied", a
    assert [(x.staff, x.date, x.layer, x.note) for x in _mine(s, r.uid)] == [("菊池", "2026-11-14", "残業", OVERTIME)]
    assert len(s.store.acceptances()) == n + 1
    unchanged(b, snap(s))                              # 表のマスも帳面も変えない
    assert s.store.pending(2026, 11) == []


def test_cover_not_twice(s):
    """同じ人・同じ日の記録があれば「もう記録があります」と言って足さない（待機で引き受けた日も同じ）。"""
    first, again = s.say(OVERTIME), s.say(OVERTIME)    # 案を2つ出してから両方を押しても1件だけ
    assert s.accept(first.uid).kind == "applied"
    n = len(s.store.acceptances())
    x = s.accept(again.uid)
    assert x.kind == "ask" and "もう記録があります" in x.message, x
    r = s.say(OVERTIME)
    assert r.kind == "ask" and "もう記録があります" in r.message, r
    r = s.say(SAME_DAY)
    assert r.kind == "ask" and "もう記録があります" in r.message, r
    assert len(s.store.acceptances()) == n and s.store.pending(2026, 11) == []


def test_cover_undo_removes(s):
    """［ひとつ戻す］で記録が消える。表と帳面はそのまま。"""
    b = snap(s)
    n = len(s.store.acceptances())
    r = s.say(OVERTIME)
    s.accept(r.uid)
    assert _mine(s, r.uid)
    u = s.undo()
    assert u.kind == "applied", u
    assert _mine(s, r.uid) == [] and len(s.store.acceptances()) == n
    unchanged(b, snap(s))


def test_cover_counts_in_giveback(s, monkeypatch):
    """翌月の返す計算に渡す回数に、残業の記録も1回として入る。画面の記録の行にも出る。"""
    r = s.say(NO_DAY)
    s.accept(r.uid)
    seen = {}

    def fake(fac, staff, rules, month, counts):
        seen.update(counts)
        return GiveBack("cannot", None, None)
    monkeypatch.setattr(nextflow, "build_pair", fake)
    nextflow.NextFlow(s.store, 2026, 11).build()
    assert seen == {"田村": 3, "菊池": 1}   # 用意した記録（田村さん2回・菊池さん1回）に、田村さんの残業の1回
    rows = s.state()["accepted_items"]
    assert {"staff": "田村", "date": "2026-11-20", "label": "残業で穴埋め", "cover": True} in rows


def test_cover_no_day_is_today(s):
    """日を言っていなければ今日（基準日）。「昨日」は前の日。確かめのカードに日を出す。"""
    r = s.say(NO_DAY)
    assert r.kind == "cover_offer" and r.message.startswith("田村さん 11月20日 残業で穴埋め"), r
    s.accept(r.uid)
    assert [(x.staff, x.date) for x in _mine(s, r.uid)] == [("田村", "2026-11-20")]
    r = s.say(EARLY)
    assert r.kind == "cover_offer" and r.message.startswith("久保さん 11月20日 早出で穴埋め"), r
    r = s.say(YESTERDAY)
    assert r.kind == "cover_offer" and r.message.startswith("千葉さん 11月19日 早出で穴埋め"), r


def test_cover_outside_month_or_future_asks(sess):
    """今日が開いている月の外の時と、まだ来ていない日は、記録せずに日を聞く。"""
    b = snap(sess)
    n = len(sess.store.acceptances())
    r = sess.say(NO_DAY)                  # 1幕の基準（10月28日）。今日は11月の表の外
    assert r.kind == "ask" and "11月の表の外" in r.message, r
    sess.base_date = "2026-11-10"
    r = sess.say(OVERTIME)                # 14日はまだ先
    assert r.kind == "ask" and "まだ先" in r.message, r
    unchanged(b, snap(sess))
    assert len(sess.store.acceptances()) == n and sess.store.pending(2026, 11) == []


def test_cover_unknown_name_asks_first(s):
    """名簿にない名前は Claude へ送らずに聞き返し、選んだ名前で読む（今ある流れに乗る）。"""
    r = s.say(UNKNOWN)
    assert r.kind == "ask" and r.extra.get("ask_name") and s.reader.sent == [], r
    c = s.choose_name(r.uid, "森田", "石田")
    assert c.kind == "cover_offer" and c.message.startswith("石田さん 11月20日 残業で穴埋め"), c
    sent = json.dumps([x.__dict__ for x in s.reader.sent], ensure_ascii=False)
    assert "石田" not in sent and "森田" not in sent


@pytest.mark.parametrize("items", [
    [{"type": "cover", "staff": None, "day": None, "how": "残業", "reason": None}],          # 人が読み取れない
    [{"type": "cover", "staff": ["職員15", "職員09"], "day": 14, "how": "残業", "reason": None}],   # 2人まとめて
])
def test_cover_bad_reply_changes_nothing(s, items):
    """人が読み取れない・2人まとめての返事は、記録も表も変えずに聞き返す。"""
    text = "残業して埋めてくれて助かった"
    s.reader = RecordedReader({text: json.dumps({"items": items}, ensure_ascii=False)})
    b = snap(s)
    n = len(s.store.acceptances())
    r = s.say(text)
    assert r.kind == "ask", r
    unchanged(b, snap(s))
    assert len(s.store.acceptances()) == n and s.store.pending(2026, 11) == []
