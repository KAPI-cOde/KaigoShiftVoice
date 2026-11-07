"""名簿にいて表に行が無い人（先の月から加わる職員）がいても、月が開けて数えられる。表は書き換えない。"""
import shutil

from shiftvoice.model import Staff
from shiftvoice.reader import RecordedReader
from shiftvoice.session import Session
from shiftvoice.store import Store

from conftest import FIXED_NOW, REPLIES

NEWCOMER = "赤坂"   # 名簿だけにいて、表に行が無い作り話の人


def _with_newcomer(built_dir, tmp_path):
    d = tmp_path / "hidamari"
    shutil.copytree(built_dir, d)
    st = Store(d)
    staff = st.staff()
    st.save_staff(staff + [Staff(name=NEWCOMER, unit=staff[0].unit, can=list(staff[0].can))])
    return Session(st, 2026, 11, RecordedReader.from_file(REPLIES), now=lambda: FIXED_NOW)


def test_表に行が無い人が名簿にいても月が開けて表は変わらない(built_dir, tmp_path):
    s = _with_newcomer(built_dir, tmp_path)
    before = s.store.month(2026, 11).table
    assert NEWCOMER not in before
    st = s.open()
    assert st
    assert s.store.month(2026, 11).table == before
