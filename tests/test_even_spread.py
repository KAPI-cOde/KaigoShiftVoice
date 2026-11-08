"""回数をならすルール（even）を入れた時の返事に、1人あたりの回数の幅を「早番は1人2〜6回 → 3〜4回」の形で添える。
数えるのはその勤務に入れる人だけ（名指しなら名指しの人だけ）。ほかのルールの返事には添えない。"""
import re
import sys

import pytest

from shiftvoice.model import Rule
from shiftvoice.rules import even_spread

SCENE = [("遅番の次の日に早番はきついから、やめてあげて", "毎月"),
         ("みんな、遅番が3日も4日も続くのはやめて", "毎月")]
EVEN = "早番も遅番も、可能な範囲で平均化して"


def _say(sess, text, ans):
    r = sess.say(text)
    if r.kind == "ask_period":
        r = sess.answer(r.uid, ans)
    return r


def _range(table, staff, sym):
    cs = [table[s.name].count(sym) for s in staff if sym in s.can]
    return min(cs), max(cs)


def _fmt(lo, hi):
    return f"{lo}回" if lo == hi else f"{lo}〜{hi}回"


def test_even_reply_says_spread_before_and_after(sess):
    sess.open()
    for text, ans in SCENE:
        r = _say(sess, text, ans)
        assert r.kind == "applied" and "spread" not in r.extra, (text, r.extra)   # ほかのルールの返事には添えない
    m = sess.store.month(2026, 11)
    before = {k: list(v) for k, v in m.table.items()}
    r = _say(sess, EVEN, "毎月")
    assert r.kind == "applied", (r.kind, r.message)
    staff = sess.store.staff_on(2026, 11)
    after = sess.store.month(2026, 11).table
    lines = r.extra["spread"]
    assert [x[:2] for x in lines] == ["早番", "遅番"], lines
    for sym, line in zip(["早", "遅"], lines):
        b, a = _range(before, staff, sym), _range(after, staff, sym)
        want = f"1人{_fmt(*b)} → {_fmt(*a)}" if b != a else f"1人{_fmt(*a)}のまま"
        assert want in line, (line, b, a)


@pytest.mark.skipif(sys.platform != "darwin", reason="OR-Tools の道筋が OS で違い、回数の幅が変わる")
def test_見本の3つの一言の平均化は_READMEと動画に書いた回数の幅になる(sess):
    sess.open()
    for text, ans in SCENE:
        _say(sess, text, ans)
    r = _say(sess, EVEN, "毎月")
    assert r.extra["spread"] == ["早番は1人2〜7回 → 3〜4回", "遅番は1人1〜6回 → 3〜4回"]


def test_named_rule_counts_only_the_named_people(sess):
    """名指しのならし（菊池さんと久保さん）は、その2人の回数だけで幅を言う。ほかの人の回数を混ぜない。"""
    sess.open()
    fac, staff = sess.store.facility(), sess.store.staff_on(2026, 11)
    m = sess.store.month(2026, 11)
    table = {k: list(v) for k, v in m.table.items()}
    two = ["菊池", "久保"]
    rule = Rule(id="t", ver=1, name="菊池さんと久保さん 早番の回数をできるだけそろえる", kind="even", flex="negotiable",
                params={"shifts": ["早"]}, who=two)
    after = {k: list(v) for k, v in table.items()}
    after["菊池"] = ["公"] * len(after["菊池"])   # 菊池さんの早番を0回にした表（幅が変わったことを言えるか）
    line = even_spread(rule, fac, staff, m, table, after)[0]
    n = {x: table[x].count("早") for x in two}
    lo, hi = min(n.values()), max(n.values())
    assert line.startswith(f"早番は1人{_fmt(lo, hi)} → "), (line, n)
    assert re.search(rf"→ {_fmt(0, n['久保'])}$", line), (line, n)


def test_平均化を入れた月の翌月の表も_早番と遅番の回数の幅が1以内(sess):
    """翌月へそのまま引き継いだ［毎月］の平均化が、12月の表で効いている。11月の表だけで確かめても、翌月に落ちていると気づけない。"""
    from shiftvoice.nextflow import NextFlow
    sess.open()
    for text, ans in SCENE:
        _say(sess, text, ans)
    _say(sess, EVEN, "毎月")
    fl = NextFlow(sess.store, 2026, 11)
    for c in fl.review()["cards"]:
        fl.answer(c["rule_id"], "keep")
    b = fl.build(base_date="2026-11-30")
    assert b["status"] == "ok", b.get("message")
    assert all(b["checks"].values()), b["checks"]
    staff = sess.store.staff_on(2026, 12)
    give = b["tables"]["give"]
    for sym in ("早", "遅"):
        lo, hi = _range(give, staff, sym)
        assert hi - lo <= 1, (sym, lo, hi)
