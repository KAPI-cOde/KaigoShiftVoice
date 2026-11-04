"""README「審査員の方へ」の3と4を、録った返事で通す。マスの位置や人を決め打ちせず、どの OS でも成り立つことだけを確かめる。
表を作る計算の部品（OR-Tools）は OS ごとに違う表を作る（ルールはどれも守る）ので、Linux（GitHub Codespaces）でも手順が最後まで動くことの確認に使う。"""
import json

from shiftvoice.build import month_rules
from shiftvoice.reader import RecordedBuilder
from shiftvoice.rules import check_table

from test_act2 import BUILDS, CALL, STANDBY

LATE_THEN_EARLY = "遅番の次の日に早番はきついから、やめてあげて"


def hard_broken(st):
    rules = month_rules(st, 2026, 11, with_premise=False)
    m = st.month(2026, 11)
    return {(b["rule"], b["key"]) for b in check_table(rules, st.facility(), st.staff_on(2026, 11), m, m.table)
            if b["flex"] in ("fixed", "wish", "operational")}


def rest(t, n):
    return t[n].count("公")


def test_judge_steps_3_and_4_any_os(sess):
    sess.builder = RecordedBuilder.from_file(BUILDS)
    sess.base_date = "2026-11-01"
    st = sess.store

    # 3. 一言で直す
    r = sess.say(LATE_THEN_EARLY)
    assert r.kind == "ask_period", r.message
    r = sess.answer(r.uid, "毎月")
    assert r.kind == "applied" and r.warning == "", (r.message, r.warning)
    cards = [c for c in sess.state()["cards"] if c["scope"] == "rule" and c["meta"].get("said")]
    assert len(cards) == 1
    t = st.month(2026, 11).table
    pairs = [(n, i) for n, row in t.items() for i in range(len(row) - 1) if row[i] == "遅" and row[i + 1] == "早"]
    assert pairs == []
    assert hard_broken(st) == set()

    # 4. 待機当番 → 配る → 休みの電話 → 了承
    r = sess.say(STANDBY)
    assert r.kind == "applied"
    assign = sess.state()["layers"]["standby"]["assign"]
    days = [int(k) for k in assign]
    assert days and all(len(v) == 1 for v in assign.values())
    import datetime as dt
    assert all(dt.date(2026, 11, d).weekday() >= 5 for d in days)   # 土日だけ
    assert "1" in assign
    sess.distribute()
    t0 = json.loads(json.dumps(st.month(2026, 11).table))
    hard0 = hard_broken(st)

    r = sess.say(CALL)
    assert r.kind == "standby_offer", r.message
    duty = assign["1"][0]
    assert r.extra["duty"] == duty
    assert t0["青木"][0] != "公" and t0[duty][0] == "公"   # 青木さんは1日に働き、待機の人は休み
    a = sess.accept(r.uid)
    assert a.kind == "applied", a.message
    t = st.month(2026, 11).table
    assert t["青木"][0] == "公" and t[duty][0] != "公"
    assert t[duty][0] == t0["青木"][0]   # 頼んだ人が青木さんの勤務に入る
    assert rest(t, "青木") == rest(t0, "青木") and rest(t, duty) == rest(t0, duty)
    assert hard_broken(st) <= hard0
    assert [x.staff for x in st.acceptances() if x.date == "2026-11-01"] == [duty]
