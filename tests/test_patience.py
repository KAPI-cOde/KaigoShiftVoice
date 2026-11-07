"""取り込んだ施設で一から表を作る時、段1（本人の希望と運用の都合を外せなくして解く）が時間切れなら、段を下げる前に
計算の量を増やしてもう一度段1を探す（作者 2026-11-07「希望休は原則守る。外す前にほかの調整を探す」）。
実際の施設の10月の写し（作り話の名前）で、量40では一度も見つからず段3へ下がって夜勤が2人でない日が8日残ったが、
量300では7回とも、全部のルールと希望休51日を守る表が見つかった（docs/measurements.md）。
一言の直し（resolve）・静かなルールの確かめ（quiet）など速さが要る所は、patience を渡さないので今までどおり。"""
from __future__ import annotations

from shiftvoice import build
from shiftvoice.model import Facility
from shiftvoice.solver import SolveResult


def _facility(pasted: bool = True, sr: int = 100, **solve):
    settings = {"quality": {"short_rest": sr, "short_rest_hours": 14}, "solve": solve}
    if pasted:
        settings["pasted_month"] = "2026-11"
    return Facility(id="t", name="t", units=["u"], shift_types=[], settings=settings)


def _solver(calls, ok_when):
    def solve(facility, staff, rules, month, *, stage=1, max_dtime=None, **kw):
        sr = facility.settings.get("quality", {}).get("short_rest", 0)
        calls.append((stage, sr, max_dtime))
        if ok_when(stage, sr, max_dtime):
            return SolveResult("ok", {"x": []}, seconds=0.5, stage=stage)
        return SolveResult("timeout", None, seconds=1.0, stage=stage)
    return solve


def test_取り込んだ施設は段を下げる前に長く探す(monkeypatch):
    calls = []
    monkeypatch.setattr(build, "solve_month", _solver(calls, lambda st, sr, mx: st == 1 and mx == 300))
    fac = _facility()
    r = build.solve_staged(fac, [], [], None, patience=build.patience_of(fac))
    assert r.ok and r.result.stage == 1
    assert calls == [(1, 100, None), (1, 0, None), (1, 100, 300)]   # 目安を外して1回、目安を戻して長く1回
    assert r.note is None                                           # 全部のルールを守ったので、段を下げた知らせは無い
    assert r.stages[-1].get("patience") == 300


def test_長く探しても見つからなければ今までどおり段3(monkeypatch):
    calls = []
    monkeypatch.setattr(build, "solve_month", _solver(calls, lambda st, sr, mx: st == 3))
    fac = _facility()
    r = build.solve_staged(fac, [], [], None, patience=build.patience_of(fac))
    assert [c[0] for c in calls] == [1, 1, 1, 3]
    assert r.note == build.STAGE_NOTE[3]


def test_作れないと分かった時と_一言の直しと_見本の施設では長く探さない(monkeypatch):
    calls = []

    def solve(facility, staff, rules, month, *, stage=1, max_dtime=None, **kw):
        calls.append((stage, max_dtime))
        return SolveResult("infeasible" if stage == 1 else "ok", None if stage == 1 else {"x": []}, seconds=0.1)
    monkeypatch.setattr(build, "solve_month", solve)
    fac = _facility(sr=0)
    build.solve_staged(fac, [], [], None, patience=build.patience_of(fac))
    assert all(mx is None for _, mx in calls)                       # 作れないと分かった時は長く探しても無駄
    calls.clear()
    monkeypatch.setattr(build, "solve_month", _solver(calls, lambda st, sr, mx: st == 3))
    build.solve_staged(fac, [], [], None)                           # patience を渡さない呼び出し（一言の直しなど）
    assert all(mx is None for *_, mx in calls)
    assert build.patience_of(_facility(pasted=False)) is None       # 見本の施設（取り込んでいない）
    assert build.patience_of(_facility(patience_dtime=0)) is None   # 施設の設定で止められる
    assert build.patience_of(_facility(patience_dtime=120)) == 120
    monkeypatch.setenv("SHIFTVOICE_PUBLIC", "1")
    assert build.patience_of(_facility()) is None                    # 公開版は重い処理の枠を占めないように長く探さない


def test_月の表を一から作る所は長く探す(monkeypatch):
    seen = {}

    def staged(fac, staff, rules, m, **kw):
        seen.update(kw)
        return build.StagedResult(SolveResult("ok", {}, seconds=0.0))
    monkeypatch.setattr(build, "solve_staged", staged)

    class S:
        def facility(self):
            return _facility()

        def staff_on(self, y, mo):
            return []

        def month(self, y, mo):
            return None
    monkeypatch.setattr(build, "month_rules", lambda *a, **k: [])
    build.build_original(S(), 2026, 12)
    assert seen.get("patience") == build.PATIENCE_DTIME
