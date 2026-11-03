"""静かなルール（quiet.py・docs/rule-cleanup.md）。Q1〜Q5。"""
import shutil

import pytest

from shiftvoice.nextflow import NextFlow
from shiftvoice.quiet import _path, check_month, streaks
from shiftvoice.review import answer, review_cards
from shiftvoice.store import Store, atomic_write_json

from conftest import SEED


@pytest.fixture
def st(tmp_path):
    shutil.copytree(SEED, tmp_path / "f")
    return Store(tmp_path / "f")


def _add(store, rid, name, kind, params, who=None, frm="2026-06-01", flex="negotiable"):
    led = store.ledger()
    led.append("add", rule={"id": rid, "name": name, "kind": kind, "flex": flex, "params": params,
                            "who": who, "when": None, "scope": "rule", "meta": {"said": name}},
               op_id=f"t-{rid}", frm=frm)
    store.save_ledger(led)


def _fake(store, ym, states):
    y, m = int(ym[:4]), int(ym[5:])
    atomic_write_json(_path(store, y, m), {"month": ym, "rules": {k: {"state": v} for k, v in states.items()}})


def test_q1_no_scene_is_quiet_without_solving(st):
    """名簿にいない人のルールは、作り直さずに静かと数える（当てはまる場面が無い）。"""
    _add(st, "g1", "森田さん 夜勤は月3回まで", "count", {"shifts": ["夜"], "max": 3}, ["森田"])
    q = check_month(st, 2026, 11, only=["g1"], workers=1)
    assert q["rules"]["g1"] == {"state": "quiet", "no_scene": True, "seconds": 0.0}


def test_q2_needed_rule_is_not_quiet(st):
    """外すと表が破る構造のルール（夜勤の翌日は明け）は、静かと数えない。"""
    q = check_month(st, 2026, 11, only=["r007"], workers=1)
    assert q["rules"]["r007"]["state"] == "needed"


def test_q3_streak_stops_at_unmeasured_month(st):
    _fake(st, "2026-11", {"a": "quiet", "b": "quiet", "c": "needed"})
    _fake(st, "2026-10", {"a": "quiet", "b": "needed", "c": "quiet"})
    _fake(st, "2026-09", {"a": "quiet"})
    # 2026-08 は測っていない → そこで止める
    assert streaks(st, 2026, 12) == {"a": 3, "b": 1}


def test_q4_quiet_card_in_review_after_three_months(st):
    _add(st, "g1", "千葉さん 夜勤は月8回まで", "count", {"shifts": ["夜"], "max": 8}, ["千葉"])
    for ym in ("2026-09", "2026-10", "2026-11"):
        _fake(st, ym, {"g1": "quiet", "r012": "quiet"})
    v = review_cards(st.ledger(), 2026, 12, streaks(st, 2026, 12), 3)
    quiet = [(c.rule_id, c.months) for c in v["cards"] if c.reason == "quiet"]
    assert quiet == [("g1", 3), ("r012", 3)]
    assert review_cards(st.ledger(), 2026, 12, streaks(st, 2026, 12), 4)["cards"] == []   # 施設の設定で月数を変えられる


def test_q5_keep_suppresses_until_streak_again(st):
    """［今月も使う］と答えたら、その後また3か月静かが続くまで聞かない。"""
    _add(st, "g1", "千葉さん 夜勤は月8回まで", "count", {"shifts": ["夜"], "max": 8}, ["千葉"])
    for ym in ("2026-09", "2026-10", "2026-11"):
        _fake(st, ym, {"g1": "quiet"})
    answer(st, 2026, 12, "g1", "keep", quiet=streaks(st, 2026, 12), quiet_months=3)
    for ym in ("2026-12", "2027-01"):
        _fake(st, ym, {"g1": "quiet"})
    assert [c for c in review_cards(st.ledger(), 2027, 2, streaks(st, 2027, 2), 3)["cards"]
            if c.reason == "quiet"] == []          # 答えた後は2か月だけ
    _fake(st, "2027-02", {"g1": "quiet"})
    assert [c.months for c in review_cards(st.ledger(), 2027, 3, streaks(st, 2027, 3), 3)["cards"]
            if c.reason == "quiet"] == [3]


def test_q6_card_lines_and_archive(st):
    """画面の札は3行で、片付けると12月のルールから外れる。11月の表は変わらない。"""
    from shiftvoice.build import build_original, month_rules
    r = build_original(st, 2026, 11, with_premise=False)
    m = st.month(2026, 11)
    m.table = r.result.table
    st.save_month(m)
    for ym in ("2026-09", "2026-10", "2026-11"):
        _fake(st, ym, {"r012": "quiet"})
    flow = NextFlow(st, 2026, 11)
    v = flow.review()
    card = next(c for c in v["cards"] if c["reason"] == "quiet")
    assert card["label"] == "無くても同じだった" and card["lines"][0].startswith("3か月続けて")
    assert [b[0] for b in card["buttons"]] == ["archive", "keep"]
    flow.answer("r012", "archive")
    assert "r012" not in {x.id for x in month_rules(st, 2026, 12)}
    assert st.month(2026, 11).table == m.table


def test_q7_duplicates_are_overlap_not_quiet(st):
    """同じことを決めた2件は、1件ずつなら互いに守り合って静かに見える。まとめて外すと破れるので重なりに移す。"""
    _add(st, "g1", "早番は各ユニット1人（もう1件）", "coverage",
         {"shifts": ["早"], "scope": "each_unit", "op": "==", "n": 1}, flex="fixed")
    q = check_month(st, 2026, 11, only=["r001", "g1"], workers=1)
    assert {k: v["state"] for k, v in q["rules"].items()} == {"r001": "overlap", "g1": "overlap"}


def test_q7b_weaker_duplicate_is_quiet(st):
    """外せる重なり（動かせない決まりの下にある、相談できる同じルール）は、ゆるい方だけ静かと数える。"""
    _add(st, "g1", "早番は各ユニット1人（相談できる）", "coverage",
         {"shifts": ["早"], "scope": "each_unit", "op": "==", "n": 1})
    q = check_month(st, 2026, 11, only=["r001", "g1"], workers=1)
    assert {k: v["state"] for k, v in q["rules"].items()} == {"r001": "needed", "g1": "quiet"}


def test_q8_quiet_cards_capped_per_month(st):
    ids = [f"g{i}" for i in range(1, 8)]
    for i, rid in enumerate(ids):
        _add(st, rid, f"千葉さん {i}", "count", {"shifts": ["夜"], "max": 20 + i}, ["千葉"])
    for ym in ("2026-09", "2026-10", "2026-11"):
        _fake(st, ym, {rid: "quiet" for rid in ids})
    v = review_cards(st.ledger(), 2026, 12, streaks(st, 2026, 12), 3, 5)
    assert len([c for c in v["cards"] if c.reason == "quiet"]) == 5 and v["quiet_more"] == 2


def test_q9_quiet_dropped_when_cover_expires(st):
    """静かなルールを守っていたかもしれない同じ中身のルールが期限切れになる月は、静かの候補に出さない。"""
    _add(st, "g1", "早番は各ユニット1人（相談できる）", "coverage",
         {"shifts": ["早"], "scope": "each_unit", "op": "==", "n": 1})
    led = st.ledger()
    led.append("change", rule_id="r001", rule={"name": "早番の人数"}, op_id="t-close", frm=None, until="2026-11-30")
    st.save_ledger(led)
    for ym in ("2026-09", "2026-10", "2026-11"):
        _fake(st, ym, {"g1": "quiet"})
    v = review_cards(st.ledger(), 2026, 12, streaks(st, 2026, 12), 3)
    assert [c.rule_id for c in v["cards"] if c.reason == "quiet"] == []
    assert [c.rule_id for c in v["cards"] if c.reason == "expired"] == ["r001"]
