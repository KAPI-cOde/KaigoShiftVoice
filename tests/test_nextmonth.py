"""翌月の写し。L4 毎月の札は残り今月だけの札は消える・杉山さんの12月からの夜勤が効く。L3 の翌月への適用。"""
from shiftvoice.build import build_original, month_rules
from shiftvoice.nextmonth import copy_next_month


def test_l4_december_copy(sess):
    r1 = sess.say("岡田さん、15と16は法事で休み")                      # 今月だけ（日付つき）
    a = sess.say("遅番の次の日に早番はきついから、やめてあげて")
    r2 = sess.answer(a.uid, "毎月")                                       # 毎月
    b = sess.say("菊池さん、早番が多すぎる。月5回くらいまで")
    r3 = sess.answer(b.uid, "今月だけ")                                   # 今月だけ（聞いて答えた）
    r4 = sess.say("小島さん、29日に試験だから、それまで夜勤3回まで")      # 29日まで
    sess.say("杉山さん、12月から夜勤もできるって")
    store = sess.store
    dec = copy_next_month(store, 2026, 11)
    nov = store.month(2026, 11)
    assert dec.prev_tail["小島"] == nov.table["小島"][-5:]
    assert dec.holidays == []
    ids = {r.id for r in month_rules(store, 2026, 12, with_premise=False)}
    assert r2.rule_ids[0] in ids                  # 毎月の札は残る
    assert r1.rule_ids[0] not in ids              # 今月だけは消える
    assert r3.rule_ids[0] not in ids              # 聞いて今月だけと答えた札も消える
    assert r4.rule_ids[0] not in ids              # 29日で終わった札は12月に効かない
    assert {f"r{i:03d}" for i in range(1, 13)} <= ids
    res = build_original(store, 2026, 12)
    assert res.ok
    t = res.result.table
    assert 3 <= t["杉山"].count("夜") <= 5         # 12月から夜勤に入る
    hard = [x for x in res.result.broken if x["flex"] in ("fixed", "wish", "operational")]
    assert hard == []
    # 月またぎ。11月30日が夜勤の人は12月1日が明け
    for name, tail in dec.prev_tail.items():
        if tail[-1] == "夜":
            assert t[name][0] == "明"
