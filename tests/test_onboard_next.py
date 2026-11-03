"""取り込んで作った施設で、翌々月へ進めるか（作った直後は引き受けた人がいない）。"""
from pathlib import Path

from shiftvoice.nextflow import NextFlow
from shiftvoice.onboard import create_facility, infer, parse_paste
from shiftvoice.store import Store

SAMPLE = Path(__file__).resolve().parents[1] / "data" / "samples" / "komorebi-2026-11.tsv"


def test_作った施設で翌々月の表を作って採れる(tmp_path):
    prop = infer(parse_paste(SAMPLE.read_text(encoding="utf-8")), 2026, 11)
    c = create_facility(prop, tmp_path / "f", name="こもれびの家（架空）", next_year=2026, next_month=12)
    assert c.next_table_made
    store = Store(tmp_path / "f")
    flow = NextFlow(store, 2026, 12)
    assert "error" not in flow.review()
    out = flow.build()
    assert out["status"] == "ok" and out.get("no_takers") and out["changed"] == 0
    assert "引き受けた人はいません" in out["main"]
    assert flow.adopt("base") == {"ok": True, "plan": "base"}
    jan = store.month(2027, 1)
    assert jan.table and set(jan.table) == set(prop.table)
    assert all(len(row) == 31 for row in jan.table.values())
