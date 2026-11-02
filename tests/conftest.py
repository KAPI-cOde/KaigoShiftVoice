from pathlib import Path

import pytest

from shiftvoice.build import month_rules
from shiftvoice.solver import solve_month
from shiftvoice.store import Store

SEED = Path(__file__).resolve().parents[1] / "data" / "hidamari"


@pytest.fixture(scope="session")
def store():
    return Store(SEED)


@pytest.fixture(scope="session")
def nov(store):
    fac, staff, m = store.facility(), store.staff(), store.month(2026, 11)
    rules = month_rules(store, 2026, 11)
    return fac, staff, m, rules


@pytest.fixture(scope="session")
def nov_result(nov):
    fac, staff, m, rules = nov
    return solve_month(fac, staff, rules, m)
