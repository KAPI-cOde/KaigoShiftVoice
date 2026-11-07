"""「〇〇さんの夜勤はできるだけ単発で」は、その人の毎月の「夜勤を2日続けない」ルール。前は読み取りが表に無い当番の話と読み、
当番の組み立てへ回して「この形では作れません」と返した（作者が実際の施設の12月で 2026-11-07 に言った一言）。
強さは、人を名指しした続く日の上限なので、「できるだけ」でも配慮ではなく運用の都合（作者「個人のハードルール。強めのソフトかハードに」）。"""


def test_single_night_is_a_personal_monthly_rule(sess):
    sess.open()
    r = sess.say("小島さんの夜勤はできるだけ単発で")
    if r.kind == "ask_period":
        r = sess.answer(r.uid, "rule")
    assert r.kind == "applied", r.message
    rules = [x for x in sess.store.ledger().rules_at().values() if x.kind == "max_run" and x.who == ["小島"]]
    assert len(rules) == 1
    rule = rules[0]
    assert rule.params == {"shifts": ["夜"], "max": 1}
    assert rule.flex == "operational"
    assert rule.scope == "rule"
    t = sess.store.month(2026, 11).table["小島"]
    assert not any(a == b == "夜" for a, b in zip(t, t[1:]))
