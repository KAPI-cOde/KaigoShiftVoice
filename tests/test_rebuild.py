"""［全部のルールで一から作り直す］。根本のルールを足した後は、一言の狭い直しではなく、全部のルールで白紙から作り直す
（作者 2026-11-07「まずゼロから根本的に回す。だいぶいい感じになったら、少しの変更を加え始める」）。［ひとつ戻す］で戻せる。"""
from shiftvoice.rules import check_table


def _hard(sess):
    m = sess.store.month(2026, 11)
    rules = __import__("shiftvoice.build", fromlist=["month_rules"]).month_rules(sess.store, 2026, 11)
    return [b for b in check_table(rules, sess.store.facility(), sess.store.staff(), m, m.table)
            if b["flex"] in ("fixed", "wish", "operational")]


def test_rebuild_makes_a_table_that_keeps_all_rules_and_can_be_undone(sess):
    sess.open()
    m = sess.store.month(2026, 11)
    broken = {n: list(r) for n, r in m.table.items()}
    name = next(iter(broken))
    broken[name] = ["夜"] * len(broken[name])   # ルールを大きく外した表にする
    m.table = broken
    sess.store.save_month(m)
    assert _hard(sess)
    r = sess.rebuild(tries=1)
    assert r.kind == "applied", r.message
    assert "一から作り直しました" in r.message and r.changes
    assert not _hard(sess)
    u = sess.undo()
    assert sess.store.month(2026, 11).table == broken, u.message


def test_rebuild_is_refused_after_distribution(sess):
    sess.open()
    sess.distribute()
    before = sess.store.month(2026, 11).table
    r = sess.rebuild(tries=1)
    assert r.kind == "ask" and "配った後" in r.message
    assert sess.store.month(2026, 11).table == before


def test_public_server_does_not_take_rebuild():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src" / "shiftvoice" / "server.py").read_text(encoding="utf-8")
    assert '"/api/rebuild" and public is None' in src
    pub = (Path(__file__).resolve().parents[1] / "src" / "shiftvoice" / "public.py").read_text(encoding="utf-8")
    assert "/api/rebuild" not in pub
