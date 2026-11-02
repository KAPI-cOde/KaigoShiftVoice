"""待機当番の土台。D2 土日に1人ずつ・偏りすぎない。D3 休みの電話で待機の人だけが動く。D7 もう休み。"""
import pytest

from shiftvoice.build import month_rules
from shiftvoice.standby import ALREADY_OFF, DEFAULT_DEF, assign, duty_days, explain, fix_absence, validate_def

from test_t1_act1 import ACT1


@pytest.fixture(scope="module")
def act1_store(built_dir, tmp_path_factory):
    import shutil
    from conftest import FIXED_NOW, REPLIES
    from shiftvoice.reader import RecordedReader
    from shiftvoice.session import Session
    from shiftvoice.store import Store
    d = tmp_path_factory.mktemp("sb") / "hidamari"
    shutil.copytree(built_dir, d)
    s = Session(Store(d), 2026, 11, RecordedReader.from_file(REPLIES), now=lambda: FIXED_NOW)
    for text, ans in ACT1:
        r = s.say(text)
        if r.kind == "ask_period":
            s.answer(r.uid, ans)
    return s.store


def test_d1_explain_from_def(store):
    d = validate_def(DEFAULT_DEF, store.facility())
    assert explain(d) == ["土日", "公休の人から1人", "連絡を受ける当番（出勤は本人の了承の後）", "引き受けた回数を翌月へ"]


def test_d2_assign_weekends(act1_store):
    st = act1_store
    m = st.month(2026, 11)
    d = validate_def(DEFAULT_DEF, st.facility())
    a = assign(d, st.facility(), st.staff_on(2026, 11), m, m.table, pins={1: "小島"})
    days = duty_days(d, m)
    assert [int(k) for k in a] == days and all(len(v) == 1 for v in a.values())
    assert a["1"] == ["小島"]
    for k, v in a.items():
        n = v[0]
        assert m.table[n][int(k) - 1] == "公"
    counts = {}
    for v in a.values():
        counts[v[0]] = counts.get(v[0], 0) + 1
    assert max(counts.values()) <= 1      # 土日9日を9人に1回ずつ
    assert a == assign(d, st.facility(), st.staff_on(2026, 11), m, m.table, pins={1: "小島"})  # 同じ入力で同じ


def test_d3_only_duty_moves(act1_store):
    st = act1_store
    m = st.month(2026, 11)
    rules = month_rules(st, 2026, 11, with_premise=False)
    r = fix_absence(st.facility(), st.staff_on(2026, 11), rules, m, m.table, "青木", 1, ["小島"])
    assert r["status"] == "ok", r
    assert {c["staff"] for c in r["changes"]} == {"青木", "小島"}
    print("2幕の直し", r["changes"], r["rest"], f"{r['seconds']:.2f}秒")
    assert r["table"]["青木"][0] == "公" and r["table"]["小島"][0] == "日"
    assert r["rest"]["青木"] == (9, 10) and r["rest"]["小島"] == (9, 8)
    assert r["table"]["小島"][:29].count("夜") <= 3   # 試験までの夜勤3回は守ったまま


def test_d7_already_off(act1_store):
    st = act1_store
    m = st.month(2026, 11)
    rules = month_rules(st, 2026, 11, with_premise=False)
    r = fix_absence(st.facility(), st.staff_on(2026, 11), rules, m, m.table, "小島", 1, ["青木"])
    assert r["status"] == "already_off" and r["message"] == ALREADY_OFF
