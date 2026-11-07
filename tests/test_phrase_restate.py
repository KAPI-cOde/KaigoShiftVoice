"""今のルールの言い直し（同じ数・ゆるめる・強さを言わない）と、全員の回数、ルールと日付の頼みが混ざった一言。
本物の Claude で録った返事（replies.json・rereads.json）で回す。"""
from shiftvoice.build import month_rules
from shiftvoice.rules import expand, with_overrides

ALREADY = ("もう入っています", "もう守られています")


def _drive(sess, text, scope="rule", dup="replace"):
    r = sess.say(text)
    for _ in range(4):
        if r.kind == "ask_period":
            r = sess.answer(r.uid, scope)
        elif r.kind == "ask_replace":
            r = sess.answer(r.uid, dup)
        else:
            break
    return r


def _rules(sess):
    return sess.store.ledger().rules_at(between=sess._span())


def _added(sess, before):
    return [r for k, r in _rules(sess).items() if k not in before]


def test_everyone_night_count_leaves_out_staff_who_cannot_work_nights(sess):
    before = set(_rules(sess))
    r = _drive(sess, "夜勤は全員3回から4回")
    assert r.kind == "applied", r.message
    new = _added(sess, before)
    assert len(new) == 1 and new[0].params.get("capable_only") is True
    # 西ユニットは夜勤に入れる人が7人で30夜をこなすので、4回までだと数のうえで足りない（斉藤さん・篠原さんが5回）。
    # 外れとして出てよいのは、その数のうえの足りなさだけで、夜勤に入れない杉山さんの0回ではない
    broken = " ".join(((r.extra or {}).get("broken") or {}).get("items") or [])
    assert "杉山" not in broken
    fac, staff, m = sess.store.facility(), sess.store.staff_on(2026, 11), sess.store.month(2026, 11)
    eff = next(x for x in with_overrides(month_rules(sess.store, 2026, 11)) if x.id == new[0].id)
    people = {i.key.split("/")[0] for i in expand(eff, fac, staff, m)}
    assert "杉山" not in people and "千葉" in people   # 杉山さんは夜勤に入れない


def test_same_night_count_said_again_is_already_in(sess):
    before = _rules(sess)
    r = _drive(sess, "夜勤は3回から5回")
    assert any(w in r.message for w in ALREADY), r.message
    assert set(_rules(sess)) == set(before)


def test_loosening_this_month_lifts_the_monthly_run_limit_only_this_month(sess):
    r = _drive(sess, "今月は連勤6日までOKにする", scope="once")
    assert r.kind == "applied", r.message
    assert _rules(sess)["r010"].params["max"] == 5   # 毎月のルールは帳面に残る
    runs = [x for x in with_overrides(month_rules(sess.store, 2026, 11))
            if x.kind == "max_run" and not x.who and x.params.get("shifts") == "work"]
    assert runs and all(x.params["max"] == 6 for x in runs)
    dec = [x for x in with_overrides(month_rules(sess.store, 2026, 12))
           if x.kind == "max_run" and not x.who and x.params.get("shifts") == "work"]
    assert [x.params["max"] for x in dec] == [5]


def test_rule_mixed_with_a_dated_request_keeps_the_rule_for_everyone(sess):
    before = set(_rules(sess))
    r = sess.say("早番は月8回まで、あと石田さん12日休み")
    assert r.kind == "ask_period", r.message
    r = sess.answer(r.uid, "rule")
    assert r.kind == "applied", r.message
    assert "続けて「石田さん、12日は休み」" in r.summary
    new = _added(sess, before)
    assert [(x.kind, x.who, x.params.get("max")) for x in new] == [("count", None, 8)]
    assert "capable_only" not in new[0].params   # 全員が入れる早番に「入れる人だけ」は付けない


def test_restating_a_fixed_run_limit_without_strength_keeps_it_fixed(sess):
    before = set(_rules(sess))
    r = _drive(sess, "連勤は4日までにしてください")
    assert r.kind == "applied", r.message
    new = _added(sess, before)
    assert [(x.kind, x.params["max"], x.flex) for x in new] == [("max_run", 4, "fixed")]
    assert "r010" not in _rules(sess)


def test_restating_fixed_days_off_or_night_coverage_is_already_in(sess):
    for text in ("公休は月9日で", "夜勤は各ユニット1人"):
        before = _rules(sess)
        r = _drive(sess, text)
        assert any(w in r.message for w in ALREADY), (text, r.message)
        assert set(_rules(sess)) == set(before)
        assert _rules(sess)["r005"].flex == _rules(sess)["r003"].flex == "fixed"


def test_said_strength_is_not_overwritten_by_the_old_rule(sess):
    for text, flex in (("強めのソフトで、連勤は4日まで", "operational"), ("できれば連勤は3日までにしたい", "negotiable")):
        before = set(_rules(sess))
        r = _drive(sess, text)
        assert r.kind == "applied", (text, r.message)
        assert [x.flex for x in _added(sess, before) if x.kind == "max_run"] == [flex]
