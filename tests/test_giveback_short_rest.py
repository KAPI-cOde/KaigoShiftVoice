"""体の目安を入れた施設（取り込んだ施設）の返す案。目安の罰で解き直さず、短い並びを返さない案より増やさない
（2026-11-04 敵対レビュー。罰のまま解くと計算の量の上限で止まり、返さない案から60〜130マス離れ、短い並びが増えた）。"""
from pathlib import Path

from shiftvoice import onboard
from shiftvoice.interval import count_short
from shiftvoice.model import Acceptance
from shiftvoice.nextflow import NextFlow
from shiftvoice.store import Store

SAMPLE = Path(__file__).resolve().parents[1] / "data" / "samples" / "komorebi-2026-11.tsv"


def test_giveback_does_not_add_short_rests(tmp_path):
    prop = onboard.infer(onboard.parse_paste(SAMPLE.read_text(encoding="utf-8")), 2026, 11)
    onboard.create_facility(prop, tmp_path / "f", name="見本の施設B", next_year=2026, next_month=12)
    st = Store(tmp_path / "f")
    assert st.facility().settings["quality"].get("short_rest")          # 取り込んだ施設は目安あり
    names = [s.name for s in st.staff()]
    takers = names[3:6]          # 前は63マス・短い並び4→8になった組
    st.save_acceptances([Acceptance(id=f"a-x{i}", staff=n, date=f"2026-12-{6 + i:02d}", layer="standby", op_id=f"x{i}")
                         for i, n in enumerate(takers)])
    r = NextFlow(st, 2026, 12).build()
    assert r["status"] == "ok", r.get("message")
    assert all(r["checks"].values()) and r["checks"].get("6_short_rest_not_more") is True
    fac, jan = st.facility(), st.month(2027, 1)
    base, give = r["tables"]["base"], r["tables"]["give"]
    assert count_short(give, fac.shift_types, 14, jan.prev_tail)[0] <= count_short(base, fac.shift_types, 14, jan.prev_tail)[0]
    for n in takers:   # 引き受けた人の短い並びも増やさない
        assert (count_short({n: give[n]}, fac.shift_types, 14, jan.prev_tail)[0]
                <= count_short({n: base[n]}, fac.shift_types, 14, jan.prev_tail)[0])
    assert r["changed"] <= 20                                            # 返さない案から大きく離れない
