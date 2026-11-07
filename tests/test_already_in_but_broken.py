"""言われたルールがもう帳面にあっても、今の表がそのルールを守れていない時（段を下げて作った表など）は、「もう入っています」で
終わらせない。ルールは足さずに、その外れだけを狭い範囲の直しで直す。直すとほかのルールとぶつかる時は、表を変えずに、守れていない
数とぶつかるルール1つと次の手1つを2文で言う。どちらも、守れていないことを認める文から始め、「もう入っています」を先に言わない。守れている時は今までどおり「もう入っています」。
施設と職員は作り話（ひだまりの郷）。録った返事で回すので鍵は要らない。"""
from __future__ import annotations

from shiftvoice.build import month_rules
from shiftvoice.rules import check_table

KIKUCHI = "菊池さん、早番が多すぎる。月5回くらいまで"
KOJIMA = "小島さん、今月は夜勤0で。あ、毎月じゃなくて、今月だけね"
SAITO = "斉藤さん、毎月、夜勤は2回まで"


def _say(sess, text, scope):
    r = sess.say(text)
    if r.kind == "ask_period":
        r = sess.answer(r.uid, scope)
    return r


def _broken(sess) -> list[tuple[str, str]]:
    st = sess.store
    m = st.month(2026, 11)
    return [(b["rule"], b["key"]) for b in check_table(month_rules(st, 2026, 11, with_premise=False), st.facility(),
                                                       st.staff_on(2026, 11), m, m.table)]


def _break_kikuchi(sess) -> None:
    """同じユニットの同じ日で、菊池さんの日勤とほかの人の早番を入れ替え、菊池さんの早番を6回にする（毎日の人数は変わらない）"""
    m = sess.store.month(2026, 11)
    t = {n: list(v) for n, v in m.table.items()}
    west = [s.name for s in sess.store.staff() if s.unit == "西" and s.name != "菊池"]
    row = t["菊池"]
    for d in range(m.days):
        if row.count("早") >= 6:
            break
        if row[d] != "日" or "早" in (row[d - 1] if d else "", row[d + 1] if d + 1 < m.days else ""):
            continue
        other = next((w for w in west if t[w][d] == "早"), None)
        if other:
            row[d], t[other][d] = "早", "日"
    assert row.count("早") == 6
    m.table = t
    sess.store.save_month(m)


def test_もう入っているルールを今の表が守れていなければ_ルールを足さずに表だけ直す(sess):
    sess.open()
    assert _say(sess, KIKUCHI, "once").kind == "applied"
    rid = next(r.id for r in sess.store.ledger().rules_at().values() if r.who == ["菊池"])
    _break_kikuchi(sess)
    assert _broken(sess) == [(rid, "菊池")]   # 手で壊したのは、このルールだけ
    broken_table = {n: list(v) for n, v in sess.store.month(2026, 11).table.items()}
    rev, n_rules = sess.store.ledger().rev, len(sess.store.ledger().rules_at())

    r = _say(sess, KIKUCHI, "once")
    assert r.kind == "applied", r.message
    assert r.message == "「菊池さん 早番は月5回まで」は今の表で1か所守れていなかったので、直しました"
    assert r.changes and r.rule_ids == [] and "もう入っています" not in r.message
    assert _broken(sess) == []
    assert sess.store.month(2026, 11).table["菊池"].count("早") <= 5
    assert sess.store.ledger().rev == rev and len(sess.store.ledger().rules_at()) == n_rules   # ルールは足さない
    assert sess.store.pending(2026, 11) == []

    # 守れている時は、今までどおり「もう入っています」で表も変えない
    fixed_table = {n: list(v) for n, v in sess.store.month(2026, 11).table.items()}
    r2 = _say(sess, KIKUCHI, "once")
    assert r2.kind == "ask" and r2.message == "11月はもう入っています"
    assert sess.store.month(2026, 11).table == fixed_table

    # ［ひとつ戻す］で直す前の表に戻り、帳面のルールは外れない
    u = sess.undo()
    assert u.kind == "applied" and KIKUCHI in u.message
    assert sess.store.month(2026, 11).table == broken_table
    assert rid in sess.store.ledger().rules_at()


def _conflict_month(sess):
    """小島さんを今月0回、斉藤さんを毎月2回までにすると、夜勤の回数のルールとぶつかり、段を下げて作った表になる"""
    sess.open()
    assert _say(sess, KOJIMA, "once").kind == "applied"
    r = _say(sess, SAITO, "rule")
    assert r.kind == "applied" and r.warning
    rid = next(r.id for r in sess.store.ledger().rules_at().values() if r.who == ["斉藤"] and r.kind == "count")
    assert (rid, "斉藤") in _broken(sess)


def test_直すとほかのルールとぶつかる時は_表を変えずに守れていない数と理由と次の手を言う(sess):
    _conflict_month(sess)
    table = {n: list(v) for n, v in sess.store.month(2026, 11).table.items()}
    rev, hist = sess.store.ledger().rev, len(sess.store.month(2026, 11).history)

    r = _say(sess, SAITO, "rule")
    assert r.kind == "ask"
    assert r.message == ("「斉藤さん 夜勤は月2回まで」は今の表で1か所守れていません（直すと「小島さん 夜勤は月0回まで」と"
                         "ぶつかります）。全部のルールで一から作り直しますか"), r.message
    assert "もう入っています" not in r.message
    assert len([s for s in r.message.split("。") if s]) <= 2 and ":" not in r.message and "：" not in r.message
    assert sess.store.month(2026, 11).table == table
    assert sess.store.ledger().rev == rev and len(sess.store.month(2026, 11).history) == hist
    assert sess.store.pending(2026, 11) == []


def test_公開版では作り直しを勧めず_直したい所を聞く(sess, monkeypatch):
    monkeypatch.setenv("SHIFTVOICE_PUBLIC", "1")
    _conflict_month(sess)
    r = _say(sess, SAITO, "rule")
    assert r.kind == "ask"
    assert r.message.endswith("。直したい所を一言で伝えてください"), r.message
    assert "作り直し" not in r.message


def test_配った後は案にして_これで直すを押すまで表を変えない(sess):
    sess.open()
    assert _say(sess, KIKUCHI, "once").kind == "applied"
    _break_kikuchi(sess)
    sess.distribute()
    table = {n: list(v) for n, v in sess.store.month(2026, 11).table.items()}
    rev, hist = sess.store.ledger().rev, len(sess.store.month(2026, 11).history)

    r = _say(sess, KIKUCHI, "once")
    assert r.kind == "fix_offer", r.message
    assert r.message.startswith("「菊池さん 早番は月5回まで」は今の表で1か所守れていません。直すと"), r.message
    assert r.changes
    assert sess.store.month(2026, 11).table == table and len(sess.store.month(2026, 11).history) == hist

    a = sess.accept(r.uid)
    assert a.kind == "applied", a.message
    assert _broken(sess) == []
    assert sess.store.ledger().rev == rev   # ルールは足さない
    assert sess.store.pending(2026, 11) == []
    assert sess.undo().kind == "applied"
    assert sess.store.month(2026, 11).table == table


def _negotiable_swap(sess, table):
    """表で、同じユニットの同じ日の2人の 日勤↔早番 を入れ替え、相談できる「早番の2連続を避ける」だけを1か所外す"""
    st = sess.store
    m = st.month(2026, 11)
    units = {s.name: s.unit for s in st.staff()}
    rules = month_rules(st, 2026, 11, with_premise=False)
    base = {(b["rule"], b["key"]) for b in check_table(rules, st.facility(), st.staff_on(2026, 11), m, table)}
    for d in range(1, m.days - 1):
        for x in table:
            if x in ("菊池", "斉藤") or table[x][d - 1] != "早" or table[x][d] != "日" or table[x][d + 1] == "早":
                continue
            for y in table:
                if y == x or y == "菊池" or units[y] != units[x] or table[y][d] != "早":
                    continue
                t = {n: list(v) for n, v in table.items()}
                t[x][d], t[y][d] = "早", "日"
                new = [b for b in check_table(rules, st.facility(), st.staff_on(2026, 11), m, t)
                       if (b["rule"], b["key"]) not in base]
                if len(new) == 1 and new[0]["flex"] == "negotiable":
                    return t, new[0]["name"]
    raise AssertionError("相談できるルールだけを外す入れ替えが見つからない")


def test_相談できるルールを外して直した時は_返事にそう添える(sess, monkeypatch):
    from shiftvoice.resolve import changed_cells
    from shiftvoice.session import Session
    sess.open()
    assert _say(sess, KIKUCHI, "once").kind == "applied"
    good = {n: list(v) for n, v in sess.store.month(2026, 11).table.items()}
    _break_kikuchi(sess)
    gave, name = _negotiable_swap(sess, good)
    real = Session._resolve

    def resolve_giving(self, m, olds):   # 狭い範囲の直しが、相談できるルールを1か所譲って直した形にする
        rr = real(self, m, olds)
        rr.staged.result.table = gave
        rr.changes = changed_cells(m.table, gave)
        return rr
    monkeypatch.setattr(Session, "_resolve", resolve_giving)

    r = _say(sess, KIKUCHI, "once")
    assert r.kind == "applied", r.message
    assert r.message == f"「菊池さん 早番は月5回まで」は今の表で1か所守れていなかったので、直しました（相談できる「{name}」が1か所外れました）"
    assert sess.store.month(2026, 11).table == gave
