"""なるべく平等に入れる有給（回数のルールの fair）を、一から表を作る時に人ごとの日数の幅にそろえる。

施設の方針は「誰が何回取れるかは組んでみるまでわからない。希望休などの制約の中で、なるべく平等に有給を入れる」
（作者 2026-11-07）。今までどおり表を作った後（有給は人ごとの罰で、日数が増えるほど1日が重い。solver._fair_leave）、
軽い計算で有給にしないと収まらない日数（余り）を数え、それを数える人に平等に割った幅（17人で15日なら1人0〜1日）を
人ごとの回数のルールにして、もう一度探す。作れなければ1回目の表のまま。
人ごとの罰だけでは、17人の12月で時間内に平等にならず、作るたびに有給15〜41日・1人0〜7日と揺れた。外せないルールを全部
入れて余りを数える形は、計算の量40でも答えが出ないことがあった。動かせない並び以外の並びと連勤のルールを外すと、2秒で
15日だった。1回目の表の合計を割る形は、合計が17〜36日と揺れた。幅を付けた2回目は、計算の量40では時間切れで、量150で
78秒で見つかり、配慮のルールの外れも減った（2026-11-07 に測った）。
"""
from __future__ import annotations

import time
from dataclasses import replace

from .model import Facility, Month, Rule, Staff
from .rules import _syms_of, _who, is_fair_on, thin_on, with_overrides

SPARE_DTIME = 20.0   # 余りを数える計算の量（17人の12月で、5では少ない日数まで届かないことがあった）
DAY_WEIGHT = 20000   # 寄せる曜日を重い配慮にする時の重み（運用の都合の重みと同じ）
EVEN_DTIME = 150.0   # 長く探す施設の2回目の計算の量。3回中2回見つかった。時間切れの時だけ長く探す量まで増やす（量300は毎回3分待つ）


def fair_rules(rules: list[Rule]) -> list[Rule]:
    return [r for r in rules if is_fair_on(r)]


def spare_table(facility: Facility, staff: list[Staff], rules: list[Rule], month: Month) -> dict | None:
    """外せないルールのうち、人数・回数・日付・曜日のルールと動かせない並びを守る時に、有給がいちばん少ない表。
    作れなければ None。動かせない並びを残すのは、夜勤の明けが余りを吸わないように（明けの記号がある施設で余りが0に見えた）。"""
    from .solver import solve_month
    keep = [r for r in rules if r.flex in ("fixed", "wish", "operational")
            and (r.kind in ("coverage", "count", "cell", "weekday") or (r.kind == "sequence" and r.flex == "fixed"))]
    weights = {**facility.settings.get("weights", {}), "leave_each": 1, "leave_step": 0}
    lite = replace(facility, settings={**facility.settings, "quality": {}, "weights": weights})
    res = solve_month(lite, staff, keep, month, stage=1, max_dtime=SPARE_DTIME)
    return res.table if res.status == "ok" else None


def _level(total: int, cap: dict[str, int]) -> tuple[dict[str, int], dict[str, int]]:
    """合計を、少ない人から順に同じ日数まで入れる時の人ごとの幅（下限, 上限）。人ごとの上限は超えない。"""
    def at(k: int) -> dict[str, int]:
        return {n: min(c, k) for n, c in cap.items()}

    k = 0
    while sum(at(k + 1).values()) <= total and at(k + 1) != at(k):
        k += 1
    lo = at(k)
    return lo, (at(k + 1) if sum(lo.values()) < total else lo)


def shares(staff: list[Staff], rules: list[Rule], month: Month, table: dict[str, list[str]],
           room: int | None = None, last: dict[str, int] | None = None) -> tuple[list[Rule], list[int]]:
    """表の有給の合計を、数える人に平等に割った人ごとの回数のルール。返す物は（ルール, ルールごとの合計）。
    有給の日数や日付を頼まれている人（「有給2日」・有給消化の人）はその頼みのままで、割る人に入れない。上限が0の人にも割らない。
    ルールの目安（at_least。全員1日など）があれば、余りが少なくてもその日数は割る。room は目安の上に入る合計（room_total）。
    割り切れない分は、先月の有給（last）が少ない人に入れる（作者 2026-11-07「先月取れていない人は優先」）。"""
    eff = with_overrides(rules)
    out: list[Rule] = []
    totals: list[int] = []
    for r in fair_rules(rules):
        syms = _syms_of(r.params.get("shifts"))
        days = list(r.when) if r.when else list(range(1, month.days + 1))
        people = [s.name for s in _who(r, staff) if s.name in table]
        cap = {n: len(days) for n in people}
        own: set[str] = set()
        for q in eff:
            if q.params.get("fair") or not q.who:
                continue
            if q.kind == "count" and _syms_of(q.params.get("shifts")) == syms:
                for w in set(q.who) & set(people):
                    if q.params.get("max") is not None:
                        cap[w] = min(cap[w], q.params["max"])
                    if q.params.get("min"):
                        own.add(w)
            elif q.kind == "cell" and q.params.get("in") and set(q.params["in"]) <= syms:
                own.update(set(q.who) & set(people))
        cap = {n: c for n, c in cap.items() if n not in own and c > 0}
        total = sum(1 for n in cap for d in days if table[n][d - 1] in syms)
        total = max(total, (r.params.get("at_least") or 0) * len(cap), room or 0)
        totals.append(total)
        lo, hi = _level(total, cap)
        if last is not None and sum(lo.values()) < total:
            first = sorted((n for n in cap if hi[n] > lo[n]), key=lambda n: last.get(n, 0))[:total - sum(lo.values())]
            lo = {**lo, **{n: hi[n] for n in first}}
            hi = lo
        what = "・".join(sorted(syms))
        for n in cap:
            p = {"shifts": sorted(syms), "max": hi[n]}
            if lo[n]:
                p["min"] = lo[n]
            out.append(Rule(id=f"{r.id}-share-{n}", ver=1, name=f"{n}さん {what}は{lo[n]}〜{hi[n]}日（{r.name}）",
                            kind="count", flex=r.flex, params=p, who=[n], when=r.when, scope="once",
                            meta={"derived": "fair_share", "of": r.id}))
    return out, totals


def weekday_people(rules: list[Rule]) -> list[Rule]:
    """配慮の強さの曜日の人数（「水曜は3人に寄せる」）を、外せない強さにした写し。"""
    return [replace(r, id=f"{r.id}-keep", flex="operational") for r in rules
            if r.kind == "coverage" and r.flex == "negotiable" and r.params.get("weekdays")]


def room_total(facility: Facility, staff: list[Staff], rules: list[Rule], month: Month, add: list[Rule]) -> int | None:
    """目安の日数の上に1日ずつ足す時に、外せないルールと「寄せる曜日」の人数（配慮の強さの曜日の人数）を守って入る有給の合計。
    add は目安の日数の人ごとのルール。寄せる曜日を守るのは、3人にしたい水曜などを有給が食わないように（作者 2026-11-07）。"""
    from .solver import solve_month
    names = [r.who[0] for r in add]
    up = [replace(r, id=f"{r.id}-up", params={**r.params, "max": r.params["max"] + 1}) for r in add]
    keep = [r for r in rules if r.flex in ("fixed", "wish", "operational")
            and (r.kind in ("coverage", "count", "cell", "weekday") or (r.kind == "sequence" and r.flex == "fixed"))
            and not (r.kind == "count" and r.params.get("fair"))]
    days = weekday_people(rules)
    fair = [replace(r, who=names) for r in fair_rules(rules)]   # 足せる人の有給だけを増やす
    weights = {**facility.settings.get("weights", {}), "leave_each": -1, "leave_step": 0}
    lite = replace(facility, settings={**facility.settings, "quality": {}, "weights": weights})
    res = solve_month(lite, staff, keep + days + fair + up, month, stage=1, max_dtime=SPARE_DTIME)
    if res.status != "ok":
        return None
    syms = {s for r in fair for s in r.params["shifts"]}
    return sum(1 for n in names for v in res.table[n] if v in syms)


def even_out(facility: Facility, staff: list[Staff], rules: list[Rule], month: Month, res,
             max_dtime: float | None = None, last: dict[str, int] | None = None) -> tuple[object, dict | None]:
    """段1で作れた表（res）の有給が余りの分を平等に割った幅に入っていなければ、その幅でもう一度探す。
    ルールに目安（at_least）があれば、目安の上に入る分も足す（room_total）。last は先月の有給の日数。
    返す物は（表の結果, 段の記録の1行）。作れない時は1回目の表のまま。"""
    thin = any(r.kind == "thin_even" for r in rules)
    if res.status != "ok" or not (fair_rules(rules) or thin):
        return res, None
    t0 = time.perf_counter()
    spare = spare_table(facility, staff, rules, month) if fair_rules(rules) else None
    add, totals = shares(staff, rules, month, spare or res.table, last=last)
    days: list[Rule] = []
    if add and any(r.params.get("at_least") for r in fair_rules(rules)):
        room = room_total(facility, staff, rules, month, add)
        if room:
            add, totals = shares(staff, rules, month, spare or res.table, room=room, last=last)
            days = weekday_people(rules)   # 足した有給は寄せる曜日を守る前提の数なので、2回目でも守る
    if not thin and all((r.params.get("min") or 0) <= sum(1 for v in res.table[r.who[0]] if v in r.params["shifts"]) <= r.params["max"]
                        for r in add):
        return res, None   # もう平等
    from .solver import solve_month
    second = thin_on(rules) + add
    if not max_dtime:
        tries = [(second + days, None)]
    else:
        # 寄せる曜日を外せない形は見つからないことがある（3回中1回）ので、最後は重い配慮の形で必ず作る
        tries = [(second + days, min(max_dtime, EVEN_DTIME)), (second + days, max_dtime)]
        if days:
            tries.append((second + [replace(r, flex="negotiable", weight=DAY_WEIGHT) for r in days], EVEN_DTIME))
    for rs, dtime in tries:
        r2 = solve_month(facility, staff, rs, month, stage=1, max_dtime=dtime)
        if r2.status != "timeout":
            break
    note = {"stage": 1, "status": r2.status, "seconds": round(time.perf_counter() - t0, 3), "fair_leave": totals}
    return (r2 if r2.status == "ok" else res), note
