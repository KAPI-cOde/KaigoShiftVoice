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


# 台本を本物の Claude で通す（鍵が要る。SHIFTVOICE_LIVE=1 の時だけ）。
# 録った返事がある一言だけ本物を呼ぶ（無い一言は今までどおり ReadError）。台本の幕の試験だけで使う:
#   SHIFTVOICE_LIVE=1 uv run pytest tests/test_t1_act1.py tests/test_act2.py tests/test_act3.py
import os  # noqa: E402

if os.environ.get("SHIFTVOICE_LIVE") == "1":
    from shiftvoice.reader import HaikuReader, RecordedBuilder, SonnetBuilder
    from shiftvoice.server import load_dotenv

    load_dotenv(SEED.parents[1] / ".env")
    _haiku, _sonnet = HaikuReader(), SonnetBuilder()
    LIVE_CALLS: list[str] = []

    def _live_read(self, masked_text, history=None):
        if masked_text not in self.replies:
            return _orig_read(self, masked_text, history)
        LIVE_CALLS.append(masked_text)
        return _haiku.read(masked_text, history)

    def _live_build(self, masked_text):
        if masked_text not in self.replies:
            return _orig_build(self, masked_text)
        LIVE_CALLS.append(masked_text)
        return _sonnet.build(masked_text)

    _orig_read, _orig_build = RecordedReader.read, RecordedBuilder.build
    RecordedReader.read, RecordedBuilder.build = _live_read, _live_build

    def pytest_terminal_summary(terminalreporter):
        terminalreporter.write_line(f"本物の Claude を呼んだ回数 {len(LIVE_CALLS)}")
