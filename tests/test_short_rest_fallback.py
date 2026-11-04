"""体の目安（短い間隔の罰）を入れた施設で段1が時間内に見つからない時、ルールを緩める前に目安を外して段1で作り直す
（2026-11-04 職員48人の試算で、目安のせいで段3へ下がり運用の都合を外していたため）。"""
from shiftvoice import build
from shiftvoice.model import Facility
from shiftvoice.solver import SolveResult


def _facility(sr):
    return Facility(id="t", name="t", units=["u"], shift_types=[],
                    settings={"quality": {"short_rest": sr, "short_rest_hours": 14}})


def test_drop_short_rest_before_lowering_stage(monkeypatch):
    calls = []

    def solve(facility, staff, rules, month, *, stage=1, **kw):
        sr = facility.settings.get("quality", {}).get("short_rest", 0)
        calls.append((stage, sr))
        if stage == 1 and sr:
            return SolveResult("timeout", None, seconds=1.0)
        return SolveResult("ok", {"x": []}, seconds=0.5)
    monkeypatch.setattr(build, "solve_month", solve)
    r = build.solve_staged(_facility(100), [], [], None)
    assert r.ok
    assert calls == [(1, 100), (1, 0)]          # 段3へ下げる前に、目安を外して段1
    assert r.note == build.SHORT_REST_NOTE
    assert r.stages[-1].get("without") == "short_rest"


def test_lower_stage_without_short_rest_when_plain_also_times_out(monkeypatch):
    calls = []

    def solve(facility, staff, rules, month, *, stage=1, **kw):
        sr = facility.settings.get("quality", {}).get("short_rest", 0)
        calls.append((stage, sr))
        return SolveResult("timeout" if stage == 1 else "ok", None if stage == 1 else {"x": []}, seconds=0.5)
    monkeypatch.setattr(build, "solve_month", solve)
    r = build.solve_staged(_facility(100), [], [], None)
    assert calls == [(1, 100), (1, 0), (3, 0)]  # 段3は目安を外したまま
    assert r.note == build.STAGE_NOTE[3]


def test_without_short_rest_timeout_still_goes_to_stage3(monkeypatch):
    calls = []

    def solve(facility, staff, rules, month, *, stage=1, **kw):
        calls.append(stage)
        return SolveResult("timeout" if stage == 1 else "ok", None if stage == 1 else {"x": []}, seconds=0.5)
    monkeypatch.setattr(build, "solve_month", solve)
    r = build.solve_staged(_facility(0), [], [], None)
    assert calls == [1, 3]                       # 目安の無い施設は今までどおり
    assert r.note == build.STAGE_NOTE[3]
