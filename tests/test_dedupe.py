"""入口で重複を止める（docs/rule-cleanup.md の2）。同じ所を決める毎月のルールを言ったら、置き換えるか別に足すかを聞く。"""
from shiftvoice.model import Rule
from shiftvoice.rules import same_slot

LATE = "遅番の次の日に早番はきついから、やめてあげて"
LATE2 = "遅番の次の日は、早番も日勤もやめて"


def _rule(**kw):
    d = {"id": "x", "ver": 1, "name": "x", "flex": "wish", "scope": "rule", "who": ["斉藤"], "when": None}
    d.update(kw)
    return Rule.from_dict(d)


def test_d1_same_slot():
    old = _rule(kind="weekday", params={"weekdays": ["wed"], "not_in": ["早"]})
    assert same_slot({"kind": "weekday", "who": ["斉藤"], "params": {"weekdays": ["wed"], "not_in": ["早", "遅"]}}, old)
    assert not same_slot({"kind": "weekday", "who": ["斉藤"], "params": {"weekdays": ["thu"], "not_in": ["早"]}}, old)
    assert not same_slot({"kind": "weekday", "who": ["小島"], "params": {"weekdays": ["wed"], "not_in": ["早"]}}, old)
    assert not same_slot({"kind": "weekday", "who": ["斉藤"], "params": {"weekdays": ["wed"], "in": ["早"]}}, old)
    once = _rule(kind="weekday", params={"weekdays": ["wed"], "not_in": ["早"]}, scope="once")
    assert not same_slot({"kind": "weekday", "who": ["斉藤"], "params": {"weekdays": ["wed"], "not_in": ["遅"]}}, once)
    # 運用の都合の回数どうしは、前から新しい方が代わる作り（_replace_same）なので聞かない
    cnt = _rule(kind="count", flex="operational", who=None, params={"shifts": ["夜"], "min": 3, "max": 5})
    assert not same_slot({"kind": "count", "flex": "operational", "who": None, "params": {"shifts": ["夜"], "max": 4}}, cnt)
    assert same_slot({"kind": "count", "flex": "wish", "who": None, "params": {"shifts": ["夜"], "max": 4}}, cnt)


def _second(sess):
    a = sess.say(LATE)
    first = sess.answer(a.uid, "毎月").rule_ids[0]
    r = sess.say(LATE2)
    if r.kind == "ask_period":
        r = sess.answer(r.uid, "毎月")
    return first, r


def test_d2_replace(sess):
    first, r = _second(sess)
    assert r.kind == "ask_replace" and "遅番の次の日に早番を置かない" in r.message
    assert first in sess.store.ledger().rules_at()          # 聞いている間は何も変えない
    done = sess.answer(r.uid, "replace")
    assert done.kind == "applied" and "置き換えました" in done.summary
    now = sess.store.ledger().rules_at()
    assert first not in now and done.rule_ids[0] in now
    sess.undo()                                              # ［ひとつ戻す］で前のルールが戻る
    now = sess.store.ledger().rules_at()
    assert first in now and done.rule_ids[0] not in now


def test_d3_add_keeps_both(sess):
    first, r = _second(sess)
    done = sess.answer(r.uid, "add")
    now = sess.store.ledger().rules_at()
    assert done.kind == "applied" and first in now and done.rule_ids[0] in now


def test_d4_reopen_keeps_question(sess):
    """聞いている途中で閉じても、開き直すと同じ問いが残る（未反映の発言の状態）。"""
    _, r = _second(sess)
    pend = [u for u in sess.store.pending(2026, 11) if u["id"] == r.uid]
    assert pend and pend[0]["status"] == "ask_replace"
    sess.open()
    assert sess.answer(r.uid, "replace").kind == "applied"


def test_d5_strength_only_restatement_asks():
    old = _rule(kind="count", flex="fixed", params={"shifts": ["夜"], "max": 4})
    assert same_slot({"kind": "count", "flex": "negotiable", "who": ["斉藤"], "params": {"shifts": ["夜"], "max": 4}}, old)
    assert not same_slot({"kind": "count", "flex": "fixed", "who": ["斉藤"], "params": {"shifts": ["夜"], "max": 4}}, old)


def test_d6_list_says_stricter_wins(sess):
    """同じ人の上限が2件ある時、ゆるい方に「厳しい方が効きます」と添える。"""
    led = sess.store.ledger()
    for rid, mx in (("g1", 6), ("g2", 7)):
        led.append("add", rule={"id": rid, "name": f"千葉さん 夜勤は月{mx}回まで", "kind": "count", "flex": "fixed",
                                "params": {"shifts": ["夜"], "max": mx}, "who": ["千葉"], "when": None,
                                "scope": "rule", "meta": {}}, op_id=f"t-{rid}", frm="2026-06-01")
    sess.store.save_ledger(led)
    cards = {c["id"]: c for c in sess.state()["cards"]}
    assert cards["g2"]["shadowed_by"] == "千葉さん 夜勤は月6回まで" and cards["g1"]["shadowed_by"] is None
