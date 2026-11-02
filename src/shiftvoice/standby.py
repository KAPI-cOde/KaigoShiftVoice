"""待機当番（12章。台本の2幕。試験 D1〜D8）。

- 勤務の記号とは別の層（日×人）。表のマスを1つも変えずに置ける。中身は「当番の定義」と「月ごとの割り当て」
- 定義は言葉から組み立て役（Sonnet）が作る候補。ここで形を確かめ、読み返しの文はコードが定義から作る
- 割り当ては対象の日ごとに per_day 人。担当できるのは、その日が公休で、前の日が夜勤・明けでない人（16章の8）。
  目的は月の最多回数を小さく
- 休みの電話（配った後）は、頼んだ人と、その日の待機の人だけを動かして直す。待機の人はその日だけ頼んだ人の
  ユニットへ応援に入れる。公休は頼んだ人 +1・待機の人 −1（操作ごとに足し引き。月の rest_adjust）
"""
from __future__ import annotations

import copy

from ortools.sat.python import cp_model

from .model import WEEKDAYS, Facility, Month, Rule, Staff
from .resolve import changed_cells
from .rules import check_table
from .solver import solve_month

DEFAULT_DEF = {
    "id": "standby", "name": "待機当番", "layer": "oncall",
    "days": {"weekdays": ["sat", "sun"], "holidays": False}, "per_day": 1,
    "own_shift_on_duty_day": {"in": ["公"], "prev_not_in": ["夜", "明"]},
    "fairness": {"minimize": "max_count"}, "on_absence": {"call": "duty_only"},
    "counters": [{"id": "assigned"}, {"id": "activated", "carry_to_next_month": True}],
}

ASK_WIDER = "待機の人だけでは埋められません。ほかの人に頼む案も見ますか"
ALREADY_OFF = "もう休みです"


class DefError(Exception):
    pass


def validate_def(d: dict, facility: Facility) -> dict:
    """組み立て役の返した定義を確かめる。知らない記号・曜日・人数は聞き返す（AI は候補まで）。"""
    if not isinstance(d, dict):
        raise DefError("当番の定義が読めませんでした")
    out = copy.deepcopy(DEFAULT_DEF)
    out["name"] = str(d.get("name") or out["name"])[:20]
    days = d.get("days") or {}
    wds = days.get("weekdays")
    if not isinstance(wds, list) or not wds or any(w not in WEEKDAYS for w in wds):
        raise DefError("どの曜日の当番か読めませんでした")
    out["days"] = {"weekdays": wds, "holidays": bool(days.get("holidays", False))}
    n = d.get("per_day", 1)
    if not isinstance(n, int) or not 1 <= n <= 3:
        raise DefError("1日に何人か読めませんでした")
    out["per_day"] = n
    own = d.get("own_shift_on_duty_day") or {}
    ins = own.get("in", ["公"])
    prev = own.get("prev_not_in", ["夜", "明"])
    if any(s not in facility.symbols for s in list(ins) + list(prev)):
        raise DefError("当番の日の勤務が読めませんでした")
    out["own_shift_on_duty_day"] = {"in": list(ins), "prev_not_in": list(prev)}
    call = (d.get("on_absence") or {}).get("call", "duty_only")
    out["on_absence"] = {"call": "duty_only" if call != "all" else "all"}
    counters = d.get("counters") or out["counters"]
    out["counters"] = [{"id": str(c.get("id")), "carry_to_next_month": bool(c.get("carry_to_next_month", False))}
                       for c in counters if isinstance(c, dict) and c.get("id")]
    return out


def explain(d: dict) -> list[str]:
    """読み返しの文（コードが定義から作る。AI に書かせない）。"""
    jp = "".join("月火水木金土日"[WEEKDAYS.index(w)] for w in d["days"]["weekdays"])
    days = {"土日": "土日"}.get(jp, f"{jp}曜") + ("と祝日" if d["days"]["holidays"] else "")
    own = "・".join(d["own_shift_on_duty_day"]["in"])
    own_jp = {"公": "公休"}.get(own, own)
    lines = [days, f"{own_jp}の人から{d['per_day']}人", "連絡を受ける当番（出勤は本人の了承の後）"]
    if any(c.get("carry_to_next_month") for c in d["counters"]):
        lines.append("引き受けた回数を翌月へ")
    return lines


def duty_days(d: dict, month: Month) -> list[int]:
    wds = set(d["days"]["weekdays"])
    return [x for x in range(1, month.days + 1)
            if month.weekday(x) in wds or (d["days"]["holidays"] and x in month.holidays)]


def _prev_cell(month: Month, table: dict[str, list[str]], name: str, day: int) -> str | None:
    if day >= 2:
        return table[name][day - 2]
    tail = month.prev_tail.get(name, [])
    return tail[-1] if tail else None


def eligible(d: dict, month: Month, table: dict[str, list[str]], name: str, day: int) -> bool:
    own = d["own_shift_on_duty_day"]
    return table[name][day - 1] in own["in"] and _prev_cell(month, table, name, day) not in own["prev_not_in"]


def assign(d: dict, facility: Facility, staff: list[Staff], month: Month, table: dict[str, list[str]],
           pins: dict[int, str] | None = None) -> dict[str, list[str]]:
    """待機の割り当て（日 → 人の並び）。種と並列数は施設の設定で、同じ入力なら同じ答え。"""
    pins = pins or {}
    days = duty_days(d, month)
    m = cp_model.CpModel()
    y = {}
    for day in days:
        for s in staff:
            if eligible(d, month, table, s.name, day):
                y[(s.name, day)] = m.new_bool_var(f"y[{s.name},{day}]")
        vs = [v for (n, dd), v in y.items() if dd == day]
        m.add(sum(vs) <= d["per_day"])
        if day in pins and (pins[day], day) in y:
            m.add(y[(pins[day], day)] == 1)
    filled = sum(y.values())
    mx = m.new_int_var(0, len(days), "max")
    for s in staff:
        m.add(sum(v for (n, _), v in y.items() if n == s.name) <= mx)
    m.maximize(filled * 1000 - mx * 10)
    sv = cp_model.CpSolver()
    st = facility.settings.get("solve", {})
    sv.parameters.random_seed = st.get("seed", 11)
    sv.parameters.num_workers = st.get("workers", 1)
    sv.parameters.max_deterministic_time = 5.0
    sv.solve(m)
    out: dict[str, list[str]] = {}
    for day in days:
        out[str(day)] = [n for (n, dd), v in y.items() if dd == day and sv.value(v)]
    return out


def rest_count(facility: Facility, table: dict[str, list[str]], name: str) -> int:
    rest = {s.symbol for s in facility.shift_types if s.kind == "rest"}
    return sum(x in rest for x in table[name])


def fix_absence(facility: Facility, staff: list[Staff], rules: list[Rule], month: Month,
                table: dict[str, list[str]], requester: str, day: int, duty: list[str],
                window: int = 3) -> dict:
    """休みの電話の直し。動かすのは頼んだ人と待機の人だけ（前後 window 日）。

    返す {"status": ok/already_off/no_duty/cannot, "table", "changes", "duty", "rest": {名前: (前, 後)}, "support"}
    """
    if table[requester][day - 1] == "公":
        return {"status": "already_off", "message": ALREADY_OFF}
    duty = [n for n in duty if n != requester]
    if not duty:
        return {"status": "no_duty", "message": ASK_WIDER}
    by_name = {s.name: s for s in staff}
    for helper in duty:
        trial = copy.deepcopy(month)
        trial.rest_adjust = dict(month.rest_adjust)
        trial.rest_adjust[requester] = trial.rest_adjust.get(requester, 0) + 1
        trial.rest_adjust[helper] = trial.rest_adjust.get(helper, 0) - 1
        if by_name[helper].unit != by_name[requester].unit:
            trial.support = {**month.support, f"{helper}/{day}": by_name[requester].unit}
        movable = {requester, helper}
        fixed = {}
        for s in staff:
            for dd in range(1, month.days + 1):
                if s.name not in movable or abs(dd - day) > window:
                    fixed[(s.name, dd)] = table[s.name][dd - 1]
        off = Rule(id="t-absence", ver=1, name=f"{requester}さん {day}日 休み", kind="cell", flex="fixed",
                   params={"in": ["公"]}, who=[requester], when=[day], scope="once")
        rs = rules + [off]
        r = solve_month(facility, staff, rs, trial, hint=table, fixed_cells=fixed, max_dtime=2.0)
        if r.status != "ok":
            continue
        before = check_table(rules, facility, staff, month, table)
        after = [b for b in r.broken if b["rule"] != "t-absence"]
        hard_before = {(b["rule"], b["key"]) for b in before if b["flex"] in ("fixed", "wish", "operational")}
        hard_new = [b for b in after if b["flex"] in ("fixed", "wish", "operational")
                    and (b["rule"], b["key"]) not in hard_before]
        if hard_new:
            continue
        return {"status": "ok", "table": r.table, "changes": changed_cells(table, r.table), "duty": helper,
                "rest": {requester: (rest_count(facility, table, requester), rest_count(facility, r.table, requester)),
                         helper: (rest_count(facility, table, helper), rest_count(facility, r.table, helper))},
                "rest_adjust": trial.rest_adjust, "support": trial.support, "seconds": r.seconds}
    return {"status": "cannot", "message": ASK_WIDER}
