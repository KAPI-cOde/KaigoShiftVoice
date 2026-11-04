"""待機当番のほかの係（入浴係など）を言葉で足した時（2026-11-04、本物の Claude で入浴係を足して見つけた）。
- 欄の ID が待機当番と重ならない（前は全部 standby で、2つ目が1つ目を上書きした）
- 読み返しの文が係の中身に合う（前は入浴係にも「連絡を受ける当番（出勤は本人の了承の後）」と出た）
- 勤務の中で受け持つ係は、休みの電話の当番に使わない"""
from pathlib import Path

from shiftvoice.model import Month
from shiftvoice.standby import explain, is_oncall, oncall_layer, validate_def
from shiftvoice.store import Store

SEED = Path(__file__).resolve().parents[1] / "data" / "hidamari"
BATH = {"name": "入浴係", "days": {"weekdays": ["mon", "thu"], "holidays": False}, "per_day": 2,
        "own_shift_on_duty_day": {"in": ["日"], "prev_not_in": []}, "on_absence": {"call": "duty_only"},
        "counters": [{"id": "assigned"}]}
WAIT = {"name": "待機", "days": {"weekdays": ["sat", "sun"], "holidays": False}, "per_day": 1,
        "own_shift_on_duty_day": {"in": ["公"], "prev_not_in": ["夜", "明"]},
        "counters": [{"id": "activated", "carry_to_next_month": True}]}


def _fac():
    return Store(SEED).facility()


def test_ids_do_not_collide_and_standby_keeps_its_id():
    fac = _fac()
    w, b = validate_def(WAIT, fac), validate_def(BATH, fac)
    assert w["id"] == "standby"                    # 撮影の状態と前提が使う ID はそのまま
    assert b["id"] != "standby" and b["id"].startswith("duty-")
    assert validate_def(BATH, fac)["id"] == b["id"]   # 同じ名前なら同じ ID


def test_explain_fits_the_kind_of_duty():
    fac = _fac()
    assert "連絡を受ける当番（出勤は本人の了承の後）" in explain(validate_def(WAIT, fac))
    lines = explain(validate_def(BATH, fac))
    assert "連絡を受ける当番（出勤は本人の了承の後）" not in lines
    assert "その日の勤務の中で受け持つ係" in lines and "日の人から2人" in lines
    assert is_oncall(validate_def(WAIT, fac)) and not is_oncall(validate_def(BATH, fac))


def test_working_duty_is_not_used_for_absence_calls():
    fac = _fac()
    m = Month(year=2026, month=11, holidays=[3, 23], prev_tail={}, table={}, layers={})
    w, b = validate_def(WAIT, fac), validate_def(BATH, fac)
    m.layers = {b["id"]: {"def": b, "assign": {}}, w["id"]: {"def": w, "assign": {}}}
    assert oncall_layer(m, 5) is None                     # 11月5日（木）は入浴係の日だが、休みの電話の当番ではない
    assert oncall_layer(m, 1)["def"]["id"] == "standby"   # 11月1日（日）は待機当番
