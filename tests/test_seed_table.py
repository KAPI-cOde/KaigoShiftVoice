"""見本の施設（ひだまりの郷）の11月の表は、作者の Mac で作った物をデータに入れて配る。

計算の部品（OR-Tools）は OS ごとに探し方の道筋が違い、同じ入力でも違う表になる（ルールはどれも守る）。
起動のたびに作っていた時は、Linux（Codespaces・GitHub の CI）や Windows で、動画や写真と違う11月の表から
始まっていた（2026-11-05、GitHub の CI で Mac と376マス違うと分かった）。
"""
from pathlib import Path

from shiftvoice.build import month_rules
from shiftvoice.rules import check_table
from shiftvoice.store import Store

SEED = Path(__file__).resolve().parents[1] / "data" / "hidamari"


def test_見本の11月の表はデータに入っていて動かせないルールを守る():
    st = Store(SEED)
    m = st.month(2026, 11)
    assert m.table is not None, "見本の11月の表がデータに無い（uv run python tools/build_seed.py で作る）"
    staff = st.staff_on(2026, 11)
    assert sorted(m.table) == sorted(s.name for s in staff)
    assert all(len(row) == m.days for row in m.table.values())
    broken = check_table(month_rules(st, 2026, 11), st.facility(), staff, m, m.table)
    assert [b for b in broken if b["flex"] in ("fixed", "wish", "operational")] == []
