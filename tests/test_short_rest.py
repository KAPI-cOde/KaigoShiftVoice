"""次の勤務までの間が短い並び（遅番の翌日の早番など）の罰と数え方の試験。API は呼ばない。"""
import json
from pathlib import Path

from shiftvoice.interval import count_short, short_pairs
from shiftvoice.model import Facility, Month, Staff
from shiftvoice.onboard import create_facility, infer, parse_paste
from shiftvoice.solver import solve_month
from shiftvoice.store import SEED

SAMPLE = Path(__file__).resolve().parents[1] / "data" / "samples" / "komorebi-2026-11.tsv"
TYPES = [
    {"symbol": "早", "name": "早番", "kind": "work", "start": "06:45", "hours": 9},
    {"symbol": "遅", "name": "遅番", "kind": "work", "start": "13:15", "hours": 9},
    {"symbol": "公", "name": "公休", "kind": "rest"},
]


def _facility(quality):
    return Facility.from_dict({"id": "t", "name": "小さな施設", "units": ["A"], "shift_types": TYPES,
                               "settings": {"quality": quality, "solve": {"max_deterministic_time": 5.0}}})


def _staff():
    return [Staff(name=n, unit="A", can=["早", "遅"]) for n in ("甲", "乙")]


def _month(prev_tail=None):
    return Month(year=2026, month=12, holidays=[], prev_tail=prev_tail or {})


def _bias(m, x, penalties):
    """毎日どちらかの勤務に入り、奇数の日は遅、偶数の日は早に寄せる（遅の翌日が早になりやすい土台）。"""
    for (n, d, sym), v in x.items():
        if sym == "公":
            m.add(v == 0)
        elif (d % 2 == 1) == (sym == "早"):
            penalties.append(v)


def _solve(quality, prev_tail=None):
    r = solve_month(_facility(quality), _staff(), [], _month(prev_tail), extra=_bias)
    assert r.status == "ok"
    return r.table


def test_weight_zero_is_same_as_before():
    """重みが0でも、キーが無くても、罰を足す前と同じ表になる。"""
    assert _solve({"short_rest": 0}) == _solve({})
    assert _solve({"short_rest": 0, "short_rest_hours": 14}) == _solve({})


def test_weight_avoids_short_runs():
    types = _facility({}).shift_types
    base = _solve({})
    assert count_short(base, types)[0] > 0   # 土台は短い並びだらけ
    t = _solve({"short_rest": 100})
    assert count_short(t, types)[0] == 0


def test_prev_tail_avoids_short_start():
    """前の月の末日が遅番なら、1日目に早番を置かない。"""
    types = _facility({}).shift_types
    tail = {"甲": ["公", "遅"], "乙": ["公", "遅"]}
    t = _solve({"short_rest": 100}, tail)
    assert count_short(t, types, 14, tail)[0] == 0
    assert all(t[n][0] != "早" for n in t)


def test_count_short_numbers():
    types = _facility({}).shift_types
    assert short_pairs(types, 14) == {("遅", "早"): 5.5}   # 遅番は22時15分に終わり、翌日の早番は6時45分から（8時間半）
    table = {"甲": ["遅", "早", "公", "遅", "遅"], "乙": ["早", "遅", "早", "公", "公"]}
    # 遅→早 は 甲の1日目→2日目 と 乙の2日目→3日目、遅→遅（15時間）は線の外で数えない
    total, by = count_short(table, types, 14)
    assert (total, by) == (2, {"遅番→早番": 2})
    # 前の月の末日が遅番で、1日目が早番の人を足すと数が増える
    total2, by2 = count_short({"丙": ["早", "公"]}, types, 14, {"丙": ["公", "遅"]})
    assert (total2, by2) == (1, {"遅番→早番": 1})


def test_imported_default_has_short_rest_but_hidamari_not(tmp_path):
    hid = json.loads((SEED / "facility.json").read_text(encoding="utf-8"))
    assert "short_rest" not in hid["settings"]["quality"]
    prop = infer(parse_paste(SAMPLE.read_text(encoding="utf-8")), 2026, 11)
    c = create_facility(prop, tmp_path / "f", name="こもれびの家（架空）", next_year=2026, next_month=12)
    q = json.loads((tmp_path / "f" / "facility.json").read_text(encoding="utf-8"))["settings"]["quality"]
    assert q["short_rest"] == 100 and q["short_rest_hours"] == 14
    # 見本の施設Bの12月は、貼った11月より短い並びが減る
    sr = c.short_rest
    assert sr["after"] < sr["before"], sr
    assert (sr["month"], sr["next_month"], sr["line"]) == (11, 12, 14)


NIGHT_TYPES = TYPES + [{"symbol": "夜", "name": "夜勤", "kind": "work", "start": "16:30", "hours": 16}]   # 明けを書かない施設


def test_night_to_night_is_not_a_short_run():
    """日をまたぐ勤務どうし（夜→夜）は、明けを書かない施設でも次の勤務までの間の並びに数えない。夜→早 と 遅→早 は残す。"""
    from shiftvoice.standby import _short_gap
    pairs = short_pairs(NIGHT_TYPES, 14)
    assert ("夜", "夜") not in pairs
    assert ("遅", "早") in pairs and ("夜", "早") in pairs
    table = {"甲": ["夜", "夜", "夜", "公"], "乙": ["遅", "早", "公", "公"]}
    total, by = count_short(table, NIGHT_TYPES, 14)
    assert (total, by) == (1, {"遅番→早番": 1})
    fac = Facility.from_dict({"id": "t", "name": "小さな施設", "units": ["A"], "shift_types": NIGHT_TYPES,
                              "settings": {}})
    assert _short_gap(fac, "夜", "夜", 14) is False
    assert _short_gap(fac, "遅", "早", 14) is True
