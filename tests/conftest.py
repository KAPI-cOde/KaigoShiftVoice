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


import datetime as dt
import shutil

from shiftvoice.reader import RecordedReader
from shiftvoice.session import JST, Session

REPLIES = Path(__file__).resolve().parent / "fixtures" / "replies.json"
FIXED_NOW = dt.datetime(2026, 10, 28, 18, 0, tzinfo=JST)  # デモの基準日（1幕は10月28日に話す）


@pytest.fixture(scope="session")
def built_dir(tmp_path_factory):
    """ひだまりの11月の元の表を一度だけ作った置き場。"""
    d = tmp_path_factory.mktemp("built") / "hidamari"
    shutil.copytree(SEED, d)
    Session(Store(d), 2026, 11, RecordedReader.from_file(REPLIES), now=lambda: FIXED_NOW).open()
    return d


@pytest.fixture
def sess(built_dir, tmp_path):
    d = tmp_path / "hidamari"
    shutil.copytree(built_dir, d)
    return Session(Store(d), 2026, 11, RecordedReader.from_file(REPLIES), now=lambda: FIXED_NOW)
