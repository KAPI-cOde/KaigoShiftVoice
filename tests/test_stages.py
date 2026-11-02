"""S4 段の順。時間切れの時は段4へ自動で進まず、希望を外さない（運用の都合を先に外す）。"""
import copy

from shiftvoice.build import C3_MESSAGE, TIMEOUT_MESSAGE, solve_staged
from shiftvoice.model import Rule


def _fac(fac, dtime):
    f = copy.deepcopy(fac)
    f.settings["solve"] = {**f.settings.get("solve", {}), "max_deterministic_time": dtime}
    return f


def test_s4_timeout_goes_to_stage3_only(nov):
    fac, staff, m, rules = nov
    r = solve_staged(_fac(fac, 0.0001), staff, rules, m)
    assert [s["stage"] for s in r.stages] == [1, 3]
    assert all(s["status"] == "timeout" for s in r.stages)
    assert not r.ok
    assert r.message == TIMEOUT_MESSAGE
    assert "この時間では見つかりませんでした" in r.message


def test_s4_wish_conflict_keeps_operational(nov):
    """作れないと分かった時は、まず運用を守って希望を外す（段2）。"""
    fac, staff, m, rules = nov
    wish = Rule(id="t-wish", ver=1, name="小島さん 夜勤7回（試験用の希望）", kind="count",
                flex="wish", params={"shifts": ["夜"], "min": 7}, who=["小島"])
    rs = [r for r in rules if not r.meta.get("premise")] + [wish]
    r = solve_staged(_fac(fac, 5.0), staff, rs, m)
    assert [s["stage"] for s in r.stages][:2] == [1, 2]
    assert r.stages[0]["status"] == "infeasible"
    assert r.ok and r.result.stage == 2
    broken = {b["rule"] for b in r.result.broken if b["flex"] in ("fixed", "operational")}
    assert broken == set()
    assert any(b["rule"] == "t-wish" for b in r.result.broken)
    assert r.note


def test_s4_impossible_fixed_asks_c3(nov):
    fac, staff, m, rules = nov
    bad = Rule(id="t-bad", ver=1, name="公休20日（試験用）", kind="count", flex="fixed",
               params={"shifts": ["公"], "min": 20}, who=["青木"])
    r = solve_staged(_fac(fac, 2.0), staff, rules + [bad], m)
    assert [s["stage"] for s in r.stages] == [1, 2, 3, 4]
    assert not r.ok and r.message == C3_MESSAGE
