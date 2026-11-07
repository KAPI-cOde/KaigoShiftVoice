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


def test_s4_wish_conflict_breaks_operational_first(nov):
    """作れないと分かった時は、まず運用の都合を外して希望を守る（段3）。作者の答え 2026-11-06 で順が変わった
    （前はこの一言で段2へ進み、希望の方を外していた）。"""
    fac, staff, m, rules = nov
    wish = Rule(id="t-wish", ver=1, name="小島さん 夜勤7回（試験用の希望）", kind="count",
                flex="wish", params={"shifts": ["夜"], "min": 7}, who=["小島"])
    rs = [r for r in rules if not r.meta.get("premise")] + [wish]
    r = solve_staged(_fac(fac, 5.0), staff, rs, m)
    assert [s["stage"] for s in r.stages][:2] == [1, 3]
    assert r.stages[0]["status"] == "infeasible"
    assert r.ok and r.result.stage == 3
    assert not any(b["rule"] == "t-wish" for b in r.result.broken)
    assert {b["flex"] for b in r.result.broken} <= {"operational", "negotiable"}
    assert r.note


def test_s4_wish_against_fixed_goes_to_stage2(nov):
    """動かせないルールとぶつかる希望は、運用の都合を外しても作れないので、その次に希望を外す（段3 → 段2）。"""
    fac, staff, m, rules = nov
    pin = Rule(id="t-pin", ver=1, name="小島さん 1日は夜勤（試験用）", kind="cell", flex="fixed",
               params={"in": ["夜"]}, who=["小島"], when=[1])
    wish = Rule(id="t-wish", ver=1, name="小島さん 2日は早番（試験用の希望）", kind="cell", flex="wish",
                params={"in": ["早"]}, who=["小島"], when=[2])
    rs = [r for r in rules if not r.meta.get("premise")] + [pin, wish]
    r = solve_staged(_fac(fac, 5.0), staff, rs, m)
    assert [s["stage"] for s in r.stages][:3] == [1, 3, 2]
    assert r.ok and r.result.stage == 2
    assert any(b["rule"] == "t-wish" for b in r.result.broken)
    assert not any(b["flex"] in ("fixed", "operational") for b in r.result.broken)


def test_s4_impossible_fixed_asks_c3(nov):
    fac, staff, m, rules = nov
    bad = Rule(id="t-bad", ver=1, name="公休20日（試験用）", kind="count", flex="fixed",
               params={"shifts": ["公"], "min": 20}, who=["青木"])
    r = solve_staged(_fac(fac, 2.0), staff, rules + [bad], m)
    assert [s["stage"] for s in r.stages] == [1, 3, 2, 4]
    assert not r.ok and r.message == C3_MESSAGE



def _scripted(calls, status):
    """段ごとの状態を決めた解き方の代わり。status(stage) が ok なら表を返す。"""
    from shiftvoice.solver import SolveResult

    def solve(facility, staff, rules, month, *, stage=1, **kw):
        calls.append(stage)
        s = status(stage)
        return SolveResult(s, {"x": []} if s == "ok" else None, seconds=0.1, stage=stage)
    return solve


def _wish_rule():
    return Rule(id="t-wish", ver=1, name="小島さん 12日は休み（試験用の希望休）", kind="cell", flex="wish",
                params={"in": ["公"]}, who=["小島"], when=[12])


def test_段3が時間切れなら希望を外す段へ進まない(monkeypatch):
    """段1で作れないと分かっても、段3（希望を全部守り、運用の都合を外せる形）が時間切れなら、希望を全部守って作れるかは
    分からない。そのまま段2（希望を外す）へ進むと、作れたかもしれない月で希望休を外した表を出してしまう（作者 2026-11-07
    「希望休は原則守る。対処をしつくしてから、どうしても空かない時だけ相談」）。止めて、詰まり所と一緒に聞き返す。"""
    from shiftvoice import build, choke
    calls = []
    monkeypatch.setattr(build, "solve_month", _scripted(calls, lambda st: {1: "infeasible", 3: "timeout"}.get(st, "ok")))
    monkeypatch.setattr(choke, "chokepoints", lambda *a, **k: [{"day": 12, "text": "いちばんの詰まり所は12日です"}])
    r = build._solve_staged(build_fac(), [], [_wish_rule()], None, explain=True)   # 叶わない希望休の下ごしらえは飛ばす
    assert 2 not in calls and 4 not in calls
    assert [s["stage"] for s in r.stages] == [1, 3]
    assert not r.ok and r.message == TIMEOUT_MESSAGE
    assert r.choke and r.choke[0]["day"] == 12      # 表が無くても、詰まり所は画面へ渡す


def test_外す希望が無ければ段3が時間切れでも今までどおり段2と段4へ進む(monkeypatch):
    """貼った直後の最初の表（翌月の希望休がまだ無い）で止めると、取り込みそのものが「作れませんでした」で止まる。"""
    from shiftvoice import build
    calls = []
    monkeypatch.setattr(build, "solve_month", _scripted(calls, lambda st: {1: "infeasible", 3: "timeout", 2: "infeasible"}.get(st, "ok")))
    r = build.solve_staged(build_fac(), [], [], None)
    assert calls[:1] == [1] and 2 in calls and calls[-1] == 4
    assert r.ok and r.result.stage == 4


def build_fac():
    from shiftvoice.model import Facility
    return Facility(id="t", name="t", units=["u"], shift_types=[], settings={})
