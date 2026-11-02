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
    st, dec, gb = pair
    assert gb.base.status == "ok" and gb.give is not None
    print("返さない", {n: gb.before[n] for n in ("小島", "田村", "菊池")},
          "返す", {n: gb.after.get(n) for n in ("小島", "田村", "菊池")},
          "増", gb.increased, "減", gb.decreased, f"{gb.give.seconds:.1f}秒")
    assert gb.status == "ok", (gb.message, gb.checks)
    assert all(gb.checks.values()), gb.checks
    three = ("小島", "田村", "菊池")
    assert sum(gb.after[n] for n in three) < sum(gb.before[n] for n in three)
    # 増えた回数と減った回数が合う（B3）
    up = sum(a - b for b, a in gb.increased.values())
    down = sum(b - a for b, a in gb.decreased.values())
    assert up == down
    assert all(a - b <= 2 for b, a in gb.increased.values())  # B4
