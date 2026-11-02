"""B0 引き受けの記録・基準日。B1 12月を返さない・返すの2通りで同じ条件で作る（合格基準は制約）。"""
import pytest

from shiftvoice.build import month_rules
from shiftvoice.giveback import accepted_counts, build_pair
from shiftvoice.model import Acceptance
from shiftvoice.nextmonth import copy_next_month


def test_b0_base_date(store):
    items = store.acceptances() + [
        Acceptance(id="k1", staff="小島", date="2026-11-01", layer="standby", op_id="op-1")]
    assert accepted_counts(items, "2026-11", "2026-11-01") == {"小島": 1}   # 2幕の時点
    assert accepted_counts(items, "2026-11", "2026-11-30") == {"小島": 1, "田村": 2, "菊池": 1}


@pytest.fixture(scope="module")
def pair(built_dir, tmp_path_factory):
    import shutil
    from shiftvoice.store import Store
    d = tmp_path_factory.mktemp("gb") / "hidamari"
    shutil.copytree(built_dir, d)
    st = Store(d)
    # 杉山さんの12月からの夜勤可（1幕の一言で入る物を、ここでは直接置く）
    staff = st.staff()
    next(s for s in staff if s.name == "杉山").changes.append({"from": "2026-12", "can_add": ["夜"]})
    st.save_staff(staff)
    dec = copy_next_month(st, 2026, 11)
    counts = {"小島": 3, "田村": 2, "菊池": 1}
    return st, dec, build_pair(st.facility(), st.staff_on(2026, 12), month_rules(st, 2026, 12), dec, counts)


def test_b1_both_plans_same_conditions(pair):
    """返す案は、引き受けた人に有給（平均＋1日）と短い連勤。ほかの人の有給・5日続く勤務・飛び石は増えない。"""
    st, dec, gb = pair
    assert gb.status == "ok", (gb.message, gb.checks)
    assert all(gb.checks.values()), gb.checks
    for p in gb.people:
        print(p)
        assert p["leave_after"] == max(int(gb.leave_avg) + 1, p["leave_before"])
        if p["run_before"] >= 4:
            assert p["run_after"] <= p["run_before"] - 1
    o = gb.others
    assert o["run5_after"] <= o["run5_before"] and o["tobi_after"] <= o["tobi_before"]


def test_b0_carry_over():
    """返した引き受けは数えない。返していない物は window か月さかのぼって数える（持ち越し）。"""
    items = [Acceptance(id="a", staff="小島", date="2026-10-04", layer="standby", op_id="o1"),
             Acceptance(id="b", staff="小島", date="2026-11-01", layer="standby", op_id="o2", returned_in="2026-12"),
             Acceptance(id="c", staff="田村", date="2026-09-12", layer="fix", op_id="o3")]
    assert accepted_counts(items, "2026-11", "2026-11-30", 3) == {"小島": 1, "田村": 1}
    assert accepted_counts(items, "2026-11", "2026-11-30", 2) == {"小島": 1}
    assert accepted_counts(items, "2026-11", "2026-11-30") == {}
