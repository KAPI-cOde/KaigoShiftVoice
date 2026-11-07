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


def test_two_tier_run_limit_with_an_excepted_person(sess):
    """「遅番はなるべく連続2回まで。どうしようもない時は3回まで。ただし〇〇さんは当たらない」は、全員（その人を除く）の
    遅番の続く日の上限を2本（2日は配慮、3日は運用の都合）にする。前は除く人を受ける所が無く「この形の頼みは、まだ扱えません」
    で止まった（作者が実際の施設の12月で 2026-11-07 に言った一言を、作り話の名前にした）。"""
    from shiftvoice.rules import expand
    sess.open()
    r = sess.say("遅番はなるべく連続2回まで。どうしようもない時は3回まで認めます。ただし杉山さんのような日勤と遅番中心の勤務の場合はそれには当たりません")
    if r.kind == "ask_period":
        r = sess.answer(r.uid, "rule")
    assert r.kind == "applied", r.message
    rules = sorted((x for x in sess.store.ledger().rules_at().values()
                    if x.kind == "max_run" and x.params.get("shifts") == ["遅"] and not x.who), key=lambda x: x.params["max"])
    assert [(x.params["max"], x.flex) for x in rules] == [(2, "negotiable"), (3, "operational")]
    assert all(x.meta.get("except_who") == ["杉山"] for x in rules)
    assert all("杉山さんを除く" in x.name for x in rules)
    fac, staff, m = sess.store.facility(), sess.store.staff(), sess.store.month(2026, 11)
    assert not any(i.key.startswith("杉山/") for i in expand(rules[1], fac, staff, m))


def test_cards_show_broken_rules(sess):
    """今の表で外れているルールは、右の一覧のカードに外れの数が出る（作者 2026-11-07「ルールがあるのになぜ4回入っているのか」）。"""
    sess.open()
    m = sess.store.month(2026, 11)
    t = {n: list(r) for n, r in m.table.items()}
    t["小島"] = ["夜", "明", "公"] * 10
    m.table = t
    sess.store.save_month(m)
    cards = {c["id"]: c for c in sess.state()["cards"]}
    assert any(c["broken"] for c in cards.values())
    assert all(c["broken"] == 0 for c in cards.values() if c["kind"] == "weekday")
