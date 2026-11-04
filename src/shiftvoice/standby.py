"""待機当番（12章。台本の2幕。試験 D1〜D8）。

- 勤務の記号とは別の層（日×人）。表のマスを1つも変えずに置ける。中身は「当番の定義」と「月ごとの割り当て」
- 定義は言葉から組み立て役（Sonnet）が作る候補。ここで形を確かめ、読み返しの文はコードが定義から作る
- 割り当ては対象の日ごとに per_day 人。担当できるのは、その日が公休で、前の日が夜勤・明けでない人（16章の8）。
  目的は月の最多回数を小さく
- 休みの電話（配った後）は、頼んだ人と、その日の待機の人だけを動かして直す。待機の人はその日だけ頼んだ人の
  ユニットへ応援に入れる
- 待機の人の公休は減らさない（公休移動。2026-11-04 作者と同僚の答え）。電話の日より後の、待機の人の勤務の日を
  1日公休に移す。移す日の候補は負担の少ない順に3つまで（rest_moves）。今月の中に移せる日が無い時だけ、今までの
  形（頼んだ人 +1・待機の人 −1。操作ごとに足し引き。月の rest_adjust）にする
"""
from __future__ import annotations

import copy
import dataclasses
import time

from ortools.sat.python import cp_model

from .model import WEEKDAYS, Facility, Month, Rule, Staff
from .resolve import carry_rules, changed_cells
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
NO_MOVE = "今月の中で休みを移せる日がありませんでした。公休は{n}日になります"
HARD = ("fixed", "wish", "operational")
MOVE_LIMIT = 3          # 休みを移す日の候補の数
SHORT_GAP_HOURS = 14    # 次の勤務までの間がこれより短い並びを「短い並び」と数える（施設の設定 move_rest.short_gap_hours で変えられる）


class DefError(Exception):
    pass


def validate_def(d: dict, facility: Facility) -> dict:
    """組み立て役の返した定義を確かめる。知らない記号・曜日・人数は聞き返す（AI は候補まで）。"""
    if not isinstance(d, dict):
        raise DefError("当番の定義が読めませんでした")
    out = copy.deepcopy(DEFAULT_DEF)
    name = str(d.get("name") or out["name"])
    out["name"] = (name if name.endswith(("当番", "係")) else name + "当番")[:20]   # 「待機」→「待機当番」（画面と台本の名前にそろえる）
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
    off = {st.symbol for st in facility.shift_types if st.kind in ("rest", "leave")}   # 公休・有給はもう休み
    if table[requester][day - 1] in off:
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
        # 頼まれた人は、その日に休む人と同じ勤務に入る（別の日に入ってその日が足りないままの案を出さない）
        fill = Rule(id="t-fill", ver=1, name=f"{helper}さん {day}日 穴埋め", kind="cell", flex="fixed",
                    params={"in": [table[requester][day - 1]]}, who=[helper], when=[day], scope="once")
        rs = rules + carry_rules(facility, table, rules) + [off, fill]  # 表にある研修・有給は残す
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
        res = {"status": "ok", "table": r.table, "changes": changed_cells(table, r.table), "duty": helper,
               "rest": {requester: (rest_count(facility, table, requester), rest_count(facility, r.table, requester)),
                        helper: (rest_count(facility, table, helper), rest_count(facility, r.table, helper))},
               "rest_adjust": trial.rest_adjust, "support": trial.support, "seconds": r.seconds,
               "moves": [], "moved_to": None}
        # 待機の人の公休は減らさず、電話の日より後へ移す（公休移動）。移せる日が無ければ上の形（公休が1日減る）のまま
        t0 = time.perf_counter()
        moves = rest_moves(facility, staff, rules, month, table, r.table, requester, helper, day, trial.support)
        res["seconds"] = r.seconds + time.perf_counter() - t0
        if moves:
            res.update(use_move(moves[0]), moves=moves)
        return res
    return {"status": "cannot", "message": ASK_WIDER}


def use_move(mv: dict) -> dict:
    """候補の1つを、案の中身（表・変わるマス・公休・足し引き・応援・移す日）にする。"""
    return {"table": mv["table"], "changes": mv["changes"], "rest": mv["rest"], "rest_adjust": mv["rest_adjust"],
            "support": mv["support"], "moved_to": mv["day"]}


def _hour(hhmm: str) -> float:
    h, m = str(hhmm).split(":")
    return int(h) + int(m) / 60


def _short_gap(facility: Facility, a: str, b: str, limit: float) -> bool:
    """a の勤務の終わりから、翌日の b の勤務の始まりまでが limit 時間より短いか（勤務の種類の始まりと長さから）。
    ひだまりでは 遅番→早番（11時間）・日勤→早番（13時間）・遅番→日勤（13時間）。明け・公休・有給は数えない。"""
    try:
        sa, sb = facility.shift(a), facility.shift(b)
    except KeyError:
        return False
    if sa.start is None or sa.hours is None or sb.start is None:
        return False
    return 24 + _hour(sb.start) - (_hour(sa.start) + sa.hours) < limit


def burden_of(facility: Facility, month: Month, table: dict[str, list[str]], name: str,
              limit: float = SHORT_GAP_HOURS) -> tuple[int, int]:
    """（次の勤務までの間が短い並びの数, この月にかかる、いちばん長い連勤）。前の月の末から続けて数える。"""
    tail = month.prev_tail.get(name, [])
    row = tail[-1:] + table[name]
    short = sum(_short_gap(facility, a, b, limit) for a, b in zip(row, row[1:]))
    work = set(facility.workday_symbols())
    best = run = 0
    for i, c in enumerate(tail + table[name]):
        run = run + 1 if c in work else 0
        if i >= len(tail):
            best = max(best, run)
    return short, best


def rest_moves(facility: Facility, staff: list[Staff], rules: list[Rule], month: Month,
               base: dict[str, list[str]], filled: dict[str, list[str]], requester: str, helper: str, day: int,
               support: dict[str, str], limit: int = MOVE_LIMIT) -> list[dict]:
    """公休移動の候補（2026-11-04 作者と同僚の答え。休日に入った人の公休は減らさず、別の日へ移す）。

    filled は電話の日を埋めた表（待機の人が入り、公休が1日減った形）。待機の人の、電話の日より後の勤務の日を1日
    公休にする。動かすのは休む人と待機の人の2人だけ（関わる人を増やさない）。移す日ごとに、次の形から1つ選ぶ。
    - 入れ替え。移す日に休む人が公休で、その勤務ができる時、休む人が入る（4マス。2人とも公休の日数は元のまま）
    - 休む人も1日入る。移す日の勤務が、1人減っても人数を守れる勤務（日勤など）の時、休む人は電話の日より後の
      自分の公休の日に1日入る（4マス。2人とも元のまま）
    - 移すだけ。上の2つができない日は、待機の人の休みだけを移す（3マス。休む人の公休は今までと同じく1日多い）
    動かせないルール・本人の希望・運用の都合を新しく破る物と、待機当番の人が当番に入れなくなる物は出さない。
    順は 相談できるルールの外れが増えない → 休む人の公休も元のまま → 変わるマスと関わる人が少ない →
    関わる人の短い並び（次の勤務までの間が14時間未満）を増やさない → 関わる人のいちばん長い連勤を延ばさない →
    電話の日に近い。limit 件まで返し、1つ目に「おすすめ」と付ける。
    """
    by = {s.name: s for s in staff}
    rest_sym = next((st.symbol for st in facility.shift_types if st.kind == "rest"), None)
    if rest_sym is None or requester not in by or helper not in by:
        return []
    limit_h = float((facility.settings.get("move_rest") or {}).get("short_gap_hours", SHORT_GAP_HOURS))
    order = {s.name: i for i, s in enumerate(staff)}
    rs = rules + carry_rules(facility, base, rules)   # 表にある研修・有給は残す

    def broken_sets(mon: Month, t: dict) -> tuple[set, dict]:
        br = check_table(rs, facility, staff, mon, t)
        return ({(b["rule"], b["key"]) for b in br if b["flex"] in HARD},
                {(b["rule"], b["key"]): b["name"] for b in br if b["flex"] == "negotiable"})

    hard0, neg0 = broken_sets(month, base)
    # 当番に入れる人は入れるままにする（前の電話で勤務に入った当番の日は、もう入れないので見ない）
    duty = [(L["def"], n, int(k)) for L in month.layers.values() if isinstance(L, dict) and "def" in L and "assign" in L
            for k, names in L["assign"].items() for n in names
            if (n, int(k)) != (helper, day) and n in base and eligible(L["def"], month, base, n, int(k))]
    ra0 = dict(month.rest_adjust)
    ra_more = {**ra0, requester: ra0.get(requester, 0) + 1}    # 休む人の公休が1日多い（移すだけ）
    ra_less = {**ra0, helper: ra0.get(helper, 0) - 1}          # 待機の人の休みをまだ移していない（休む人の埋め合わせだけ）

    def check(t: dict, sup: dict, ra: dict) -> list[str] | None:
        """新しく破るルールが無く、待機当番の人がみな当番に入れるままなら、増える相談できるルールの外れの名前。
        だめなら None。"""
        mon = dataclasses.replace(month, support=sup, rest_adjust=ra)
        hard, neg = broken_sets(mon, t)
        if hard - hard0 or any(not eligible(d, mon, t, n, k) for d, n, k in duty):
            return None
        return [neg[k] for k in neg if k not in neg0]

    def edit(cells: dict) -> dict:
        t = {n: list(row) for n, row in filled.items()}
        for (n, d), sym in cells.items():
            t[n][d - 1] = sym
        return t

    def movable(sym: str) -> bool:   # 1マスで動かせる勤務（夜勤は明けと公休が続くので動かさない。研修・有給も）
        st = facility.shift(sym)
        return st.kind == "work" and not st.next_is_ake and not st.on_request_only

    def cand(kind: str, d2: int, t: dict, sup: dict, ra: dict, neg: list[str], make: tuple | None = None) -> dict:
        ch = sorted(changed_cells(base, t), key=lambda c: (c["day"], order.get(c["staff"], 99)))
        names = sorted({c["staff"] for c in ch}, key=lambda n: order.get(n, 99))
        b0 = {n: burden_of(facility, month, base, n, limit_h) for n in names}
        b1 = {n: burden_of(facility, month, t, n, limit_h) for n in names}
        up = [n for n in names if b1[n][0] > b0[n][0]]
        longer = [n for n in names if b1[n][1] > b0[n][1]]
        rest = {n: (rest_count(facility, base, n), rest_count(facility, t, n)) for n in (requester, helper)}
        more = rest[requester][1] != rest[requester][0]
        if kind == "swap":
            head = f"{d2}日に{helper}さんが休み、{requester}さんが入る"
            other = f"{requester}さんは代わりに{d2}日に入る"
        elif kind == "makeup":
            head = f"{d2}日に{helper}さんが休み、{requester}さんは{make[0]}日に{facility.shift(make[1]).name}"
            other = f"{requester}さんは代わりに{make[0]}日に{facility.shift(make[1]).name}"
        else:
            head = f"{d2}日に{helper}さんが休み"
            other = None
        # 説明の区切り。負担が増える区切りだけ warn（画面で赤くする）
        notes = []
        if more:
            notes.append((f"{requester}さんの公休は{rest[requester][1]}日になる", False))
        if neg:
            notes.append(("".join(f"「{x}」" for x in sorted(set(neg))) + f"が{len(neg)}件守れなくなる", True))
        notes.append(("短い並びは増えない", False) if not up else
                     ("・".join(f"{n}さん" for n in up) + f"の短い並びが{sum(b1[n][0] - b0[n][0] for n in up)}つ増える",
                      True))
        notes += [(f"{n}さんの連勤が{b1[n][1]}日になる", True) for n in longer]
        keys = (len(neg), int(more), len(ch), len(names), sum(b1[n][0] - b0[n][0] for n in up),
                sum(b1[n][1] - b0[n][1] for n in longer), d2 - day, (make[0] - day) if make else 0)
        # line は1行の説明（移す日から言う）。parts は画面の案の下に出す区切り（移す日は「休みは〇日へ移す」で言うので、
        # 休む人がどうなるかから言う）
        return {"day": d2, "kind": kind, "makeup": make[0] if make else None, "table": t, "changes": ch,
                "support": sup, "rest_adjust": ra, "rest": rest, "keys": keys,
                "line": "。".join([f"{head}（{len(ch)}マス）"] + [x for x, _ in notes]),
                "parts": ([{"text": other, "warn": False}] if other else []) + [{"text": x, "warn": w} for x, w in notes],
                "warn": bool(neg or up or longer)}

    after = range(day + 1, month.days + 1)
    options: dict[int, list[dict]] = {}
    alone: dict[int, list[str]] = {}
    for d2 in after:
        hs = filled[helper][d2 - 1]
        if not movable(hs):
            continue
        if filled[requester][d2 - 1] == rest_sym and hs in by[requester].can:      # 入れ替え
            t = edit({(helper, d2): rest_sym, (requester, d2): hs})
            sup = dict(support)
            if by[requester].unit != by[helper].unit:
                sup[f"{requester}/{d2}"] = by[helper].unit   # 休む人はその日だけ待機の人のユニットへ応援
            neg = check(t, sup, ra0)
            if neg is not None:
                options.setdefault(d2, []).append(cand("swap", d2, t, sup, ra0, neg))
        neg = check(edit({(helper, d2): rest_sym}), support, ra_more)               # 移すだけ
        if neg is not None:
            alone[d2] = neg
            options.setdefault(d2, []).append(cand("alone", d2, edit({(helper, d2): rest_sym}), dict(support),
                                                   ra_more, neg))
    makeups = [(d3, x) for d3 in after if filled[requester][d3 - 1] == rest_sym
               for x in by[requester].can if movable(x)
               and check(edit({(requester, d3): x}), support, ra_less) is not None]
    for d2 in alone:                                                                # 休む人も1日入る
        for d3, x in makeups:
            if d3 == d2:
                continue
            t = edit({(helper, d2): rest_sym, (requester, d3): x})
            neg = check(t, support, ra0)
            if neg is not None:
                options[d2].append(cand("makeup", d2, t, dict(support), ra0, neg, (d3, x)))
    best = [min(v, key=lambda c: c["keys"]) for v in options.values()]
    out = sorted(best, key=lambda c: c["keys"])[:limit]
    for i, c in enumerate(out):
        c["label"] = "おすすめ" if i == 0 else ""
    return out
