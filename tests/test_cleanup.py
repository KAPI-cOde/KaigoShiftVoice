"""ルールの整理（cleanup.py・docs/rule-cleanup.md の6）。片付けの候補を種類ごとにまとめ、その時に全部決める。"""
import datetime as dt
import shutil

import pytest

from shiftvoice.aging import RecordedAging, classify_rules
from shiftvoice.cleanup import apply, view
from shiftvoice.quiet import _path
from shiftvoice.review import history, restore
from shiftvoice.store import Store, atomic_write_json

from conftest import SEED

NOW = dt.datetime(2026, 11, 20, 9, 0)


@pytest.fixture
def st(tmp_path):
    shutil.copytree(SEED, tmp_path / "f")
    s = Store(tmp_path / "f")
    led = s.ledger()
    rows = [("g1", "千葉さん 夜勤は月6回まで", "count", {"shifts": ["夜"], "max": 6}, ["千葉"], "fixed", None),
            ("g2", "千葉さん 夜勤は月7回まで", "count", {"shifts": ["夜"], "max": 7}, ["千葉"], "fixed", None),
            ("g3", "森田さん 夜勤は月3回まで", "count", {"shifts": ["夜"], "max": 3}, ["森田"], "operational", None),
            ("g4", "森田さん 日曜は早番に入れない", "weekday", {"weekdays": ["sun"], "not_in": ["早"]}, ["森田"], "wish", None),
            ("g5", "田村さん 夜勤なし", "count", {"shifts": ["夜"], "max": 0}, ["田村"], "operational", "腰を痛めている")]
    for rid, name, k, p, who, fl, reason in rows:
        led.append("add", rule={"id": rid, "name": name, "kind": k, "flex": fl, "params": p, "who": who, "when": None,
                                "scope": "rule", "meta": {"said": name, **({"reason": reason} if reason else {})}},
                   op_id=f"t-{rid}", frm="2026-06-01")
    s.save_ledger(led)
    for ym in ("2026-09", "2026-10", "2026-11"):
        atomic_write_json(_path(s, int(ym[:4]), int(ym[5:])), {"month": ym, "rules": {
            "g1": {"state": "overlap"}, "g2": {"state": "overlap"}, "r011": {"state": "quiet"},
            "g3": {"state": "quiet", "no_scene": True}, "g4": {"state": "quiet", "no_scene": True}}})
    classify_rules(s, RecordedAging({"腰を痛めている": True}))
    return s


def test_c1_groups_and_head(st):
    v = view(st, 2026, 11)
    g = v["groups"]
    assert [x["ids"] for x in g["absent"]] == [["g3", "g4"]]
    assert [(x["loose"], x["strict"], x["known"]) for x in g["overlap"]] == [("g2", "g1", True)]
    assert [x["id"] for x in g["quiet"]] == ["r011"]
    assert [(x["id"], x["reason"]) for x in g["aged"]] == [("g5", "腰を痛めている")]
    assert v["count"] == 5                     # 名簿にいない人2・重なり1組（片付けるのは1件）・静か1・事情1
    assert v["head"].startswith(f"ルールは{v['total']}件。このうち5件は")


def test_c2_apply_all_at_once_and_restore(st):
    """種類ごとにまとめて片付け、残すと決めた物は次の測りまで出さない。片付けた物は一覧から戻せる。"""
    v = apply(st, 2026, 11, ["g3", "g4", "g2"], ["r011", "g5"], NOW)
    assert v["count"] == 0
    now = st.ledger().rules_at()
    assert not {"g3", "g4", "g2"} & set(now) and {"g1", "r011", "g5"} <= set(now)
    assert {h["rule_id"] for h in history(st.ledger())} == {"g3", "g4", "g2"}
    restore(st, "g2", NOW.isoformat())
    assert "g2" in st.ledger().rules_at()


def test_c3_kept_comes_back_after_three_more_months(st):
    apply(st, 2026, 11, [], ["r011"], NOW)
    for ym in ("2026-12", "2027-01"):
        atomic_write_json(_path(st, int(ym[:4]), int(ym[5:])), {"month": ym, "rules": {"r011": {"state": "quiet"}}})
    assert [x["id"] for x in view(st, 2027, 1)["groups"]["quiet"]] == []
    atomic_write_json(_path(st, 2027, 2), {"month": "2027-02", "rules": {"r011": {"state": "quiet"}}})
    assert [x["id"] for x in view(st, 2027, 2)["groups"]["quiet"]] == ["r011"]


def test_c4_stale_ids_refused(st):
    apply(st, 2026, 11, ["g3"], [], NOW)
    out = None
    from shiftvoice.ledger import LedgerError
    with pytest.raises(LedgerError):
        apply(st, 2026, 11, ["g3"], [], NOW)
