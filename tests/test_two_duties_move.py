"""待機当番と勤務の係が同じ日にある月に、その日の係の人が休みの電話をしても、休みを移す候補が消えない
（2026-11-04 敵対レビューの C。rest_moves の当番の一覧に、休む人のその日の係が残っていた）。"""
from shiftvoice.standby import validate_def

from test_act2 import CALL, STANDBY, act2  # noqa: F401


def test_duty_person_calling_in_keeps_move_candidates(act2):
    s = act2
    s.say(STANDBY)
    s.distribute()
    m = s.store.month(2026, 11)
    fac = s.store.facility()
    # 11月1日（日）に、休みの電話をかける青木さんを、送迎の係にしておく（勤務の中で受け持つ係）
    ride = validate_def({"name": "送迎係", "days": {"weekdays": ["sat", "sun"], "holidays": False}, "per_day": 1,
                         "own_shift_on_duty_day": {"in": ["日", "早", "遅"], "prev_not_in": []},
                         "counters": [{"id": "assigned"}]}, fac)
    m.layers[ride["id"]] = {"def": ride, "assign": {"1": ["青木"]}}
    s.store.save_month(m)
    r = s.say(CALL)
    assert r.kind == "standby_offer"
    assert r.extra.get("moves"), "休みを移す候補が消えている"
    assert not r.extra.get("no_move")
