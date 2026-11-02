"""狭い範囲の直し（確定の前）。前提は元の表だけに使い、直す時は前の表に寄せる。"""
import pytest

from shiftvoice.build import month_rules
from shiftvoice.model import Rule
from shiftvoice.resolve import resolve
from shiftvoice.rules import check_table

LATE_EARLY = Rule(id="t-le", ver=1, name="遅番の次の日に早番を置かない", kind="sequence",
                  flex="negotiable", params={"if": ["遅"], "next_not_in": ["早"]})


@pytest.fixture(scope="module")
def base_rules(store):
    return month_rules(store, 2026, 11, with_premise=False)


@pytest.fixture(scope="module")
def late_early(nov, nov_result, base_rules):
    fac, staff, m, _ = nov
    return resolve(fac, staff, base_rules + [LATE_EARLY], m, nov_result.table, [LATE_EARLY])


def test_r10_late_then_early_all_fixed(nov, nov_result, late_early):
    """R10 の土台。当てはまっていたマスが全部直る（数は台本の〈実測値〉に使う）。"""
    fac, staff, m, _ = nov
    before = check_table([LATE_EARLY], fac, staff, m, nov_result.table)
    assert any(b["key"] == "千葉/9" for b in before)  # 前提の 千葉9日遅→10日早
    r = late_early
    assert r.ok
    assert check_table([LATE_EARLY], fac, staff, m, r.table) == []
    hard = [b for b in r.staged.result.broken if b["flex"] in ("fixed", "wish", "operational")]
    assert hard == []
    print(f"当てはまり {len(before)} 件 → 動いたマス {len(r.changes)}、{r.seconds:.2f} 秒、幅 {r.window}")
    assert len(r.changes) <= 3 * len(before)


def test_resolve_keeps_act2_cells(late_early):
    """2幕に要る小島さん1日の公休は、前提を外しても寄せる重みで残る。
    青木さん1日の日勤は、この1件だけの直しでは早番へ動くことがある（2026-11-02、元の表が変わった時）。
    撮影の順（1幕の全部の一言の後）で日勤のままかは test_t1_act1 で確かめる。"""
    t = late_early.table
    assert t["小島"][0] == "公"


def test_resolve_carries_training(late_early):
    """前の表にある研修は、言葉で外されるまで残る。"""
    assert late_early.table["上野"][11] == "研"


def test_resolve_moves_training(nov, nov_result, base_rules):
    """「上野さんの研修、13日に変わった」の形。12日の研修を外し、13日に置く。"""
    fac, staff, m, _ = nov
    off = Rule(id="t-off", ver=1, name="上野さん 12日は研修でない", kind="cell", flex="fixed",
               params={"not_in": ["研"]}, who=["上野"], when=[12], scope="once")
    on = Rule(id="t-on", ver=1, name="上野さん 13日 研修", kind="cell", flex="fixed",
              params={"in": ["研"]}, who=["上野"], when=[13], scope="once")
    r = resolve(fac, staff, base_rules + [off, on], m, nov_result.table, [off, on])
    assert r.ok
    assert r.table["上野"][11] != "研" and r.table["上野"][12] == "研"
    assert sum(row.count("研") for row in r.table.values()) == 1
