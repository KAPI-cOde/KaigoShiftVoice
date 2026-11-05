"""職員が30人を超える施設は、表を作る計算で、決まった順で複数の作業者を回す（2026-11-05）。
1人で探すと、大きな施設では時間内に段1の表が見つからなかったり、短い並びと相談できるルールの外れが残ったりした。
30人以下の施設は設定に何も足さないので、今までどおり1人で探す（表は1マスも変わらない）。API は呼ばない。"""
from pathlib import Path

from ortools.sat.python import cp_model

from shiftvoice import solver as solver_mod
from shiftvoice.build import month_rules
from shiftvoice.model import Facility, Month, Staff
from shiftvoice.onboard import SOLVE_KEYS, _settings, create_facility, infer, parse_paste, plan_symbols
from shiftvoice.solver import INTERLEAVE_WORKERS, LARGE_DTIME, LARGE_STAFF, large_workers
from shiftvoice.rules import check_table
from shiftvoice.store import SEED, Store, read_json

FX = Path(__file__).resolve().parent / "fixtures"
SYMBOLS = (FX / "standard_form_symbols.tsv").read_text(encoding="utf-8")
TYPES = [
    {"symbol": "早", "name": "早番", "kind": "work", "start": "07:00", "hours": 9},
    {"symbol": "遅", "name": "遅番", "kind": "work", "start": "11:00", "hours": 9},
    {"symbol": "公", "name": "公休", "kind": "rest"},
]


def test_取り込みの設定は変えず_その月の人数で作業者の数を決める():
    """取り込んだ時の人数ではなく、その月の人数で決める（前の版で取り込んだ大きな施設・後から30人を超えた施設にも
    効き、30人以下に減った施設は1人で探す。Codex Astra のレビュー 2026-11-05）。"""
    assert LARGE_STAFF == 30 and INTERLEAVE_WORKERS == 8
    seed = read_json(SEED / "facility.json")["settings"]["solve"]
    assert _settings(2027, 2, 2027, 3, [])["solve"] == {k: seed[k] for k in SOLVE_KEYS if k in seed}
    fac = Facility.from_dict({"id": "t", "name": "施設", "units": ["A"], "shift_types": TYPES,
                              "settings": {"solve": {"workers": 1}}})
    staff = [Staff(name=f"職員{i:02d}", unit="A", can=["早", "遅"]) for i in range(1, 32)]
    assert large_workers(fac, staff) == 8              # 31人
    assert large_workers(fac, staff[:30]) == 0         # 30人以下は1人で探す
    fac.settings["solve"]["interleave_workers"] = 4    # 設定で数を変えられる（30人以下には効かない）
    assert large_workers(fac, staff) == 4 and large_workers(fac, staff[:30]) == 0


def _tiny(solve: dict, n: int = 2):
    fac = Facility.from_dict({"id": "t", "name": "小さな施設", "units": ["A"], "shift_types": TYPES,
                              "settings": {"solve": solve}})
    staff = [Staff(name=f"職員{i:02d}", unit="A", can=["早", "遅"]) for i in range(1, n + 1)]
    return fac, staff, Month(year=2026, month=12, holidays=[], prev_tail={})


def test_31人の月は決まった順で作業者を回し_30人以下は1人で探す(monkeypatch):
    seen = []

    class Spy(cp_model.CpSolver):
        def solve(self, model, *a, **k):
            p = self.parameters
            seen.append((p.num_workers, p.interleave_search, p.interleave_batch_size, p.max_deterministic_time))
            return super().solve(model, *a, **k)

    monkeypatch.setattr(solver_mod.cp_model, "CpSolver", Spy)
    fac, staff, m = _tiny({"workers": 1, "max_deterministic_time": 2.0})
    assert solver_mod.solve_month(fac, staff, [], m).status == "ok"
    # 前の版の取り込みで設定に書いた interleave_workers が残っていても、30人以下なら1人で探す
    fac, staff, m = _tiny({"workers": 1, "interleave_workers": 8, "max_deterministic_time": 2.0}, n=30)
    assert solver_mod.solve_month(fac, staff, [], m, max_dtime=2.0).status == "ok"
    # 設定に何も無くても、31人なら作業者を8つ回し、計算の量を LARGE_DTIME まで増やす（引数で渡した時はその量）
    fac, staff, m = _tiny({"workers": 1, "max_deterministic_time": 2.0}, n=31)
    solver_mod.solve_month(fac, staff, [], m, max_dtime=2.0)
    solver_mod.solve_month(fac, staff, [], m)
    assert seen[:2] == [(1, False, 0, 2.0), (1, False, 0, 2.0)]
    assert seen[2:] == [(8, True, 8, 2.0), (8, True, 8, LARGE_DTIME)]   # 区切りの大きさも決める（決めないと版で変わりうる）


def _rotate(row: list[str], k: int) -> list[str]:
    """4週の中で並びを k 日ずらす。継ぎ目で夜勤（i）の次が明け（j）でなくなる所は明けにし、明けだけ残った所は休みにする。"""
    new = list(row[k:] + row[:k])
    for t in range(1, len(new)):
        if new[t - 1] == "i" and new[t] != "j":
            new[t] = "j"
        elif new[t] == "j" and new[t - 1] != "i":
            new[t] = "公"
    return new


def _big_paste(copies: int) -> str:
    """試験用の9人の記載例（作り話）を、ユニットごとに並びを3日ずつずらして写した、大きな施設の貼り付け。"""
    p = parse_paste((FX / "standard_form_4weeks.tsv").read_text(encoding="utf-8"), SYMBOLS)
    lines = ["ユニット\t氏名\t" + "\t".join(str(d) for d in range(1, 29))]
    no = 0
    for c in range(copies):
        for row in p.table.values():
            no += 1
            cells = _rotate(row, 3 * c)
            lines.append(f"ユニット{c + 1}\t職員{no:02d}\t" + "\t".join("" if x == "公" else x for x in cells))
    return "\n".join(lines) + "\n"


def test_36人の施設でも段1のままルールを全部守り_短い並びを無くし_同じ入力なら同じ表(tmp_path):
    p = parse_paste(_big_paste(4), SYMBOLS)
    plan = plan_symbols(p, SYMBOLS)
    prop = infer(p, 2027, 2, plan.answers)
    assert len(prop.staff) == 36 and prop.problems == []
    c = create_facility(prop, tmp_path / "f", name="試験", next_year=2027, next_month=3,
                        answers=plan.answers, times=plan.times)
    assert c.next_table_made and c.stage_note is None   # 段を下げず、間の目安も外していない
    store = Store(tmp_path / "f")
    fac, staff, m = store.facility(), store.staff_on(2027, 3), store.month(2027, 3)
    assert large_workers(fac, staff) == INTERLEAVE_WORKERS
    rules = month_rules(store, 2027, 3, with_premise=False)
    assert check_table(rules, fac, staff, m, m.table) == []   # 相談できるルールも外れていない（1人で探すと2件）
    assert c.short_rest["before"] > 20 and c.short_rest["after"] == 0   # 1人で探すと24回残った
    # 同じ入力なら同じ表（計算の量を小さくして2回）
    a = solver_mod.solve_month(fac, staff, rules, m, max_dtime=5.0)
    b = solver_mod.solve_month(fac, staff, rules, m, max_dtime=5.0)
    assert a.status == b.status == "ok" and a.table == b.table
