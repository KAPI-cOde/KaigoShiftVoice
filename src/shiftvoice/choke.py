"""全部の希望休を入れると組めない時の、いちばんの詰まり所（作者 2026-11-07）。

施設は希望休を守れなかったことが一度もなく、組めない時は、日勤専属の人に早出を頼む・その日の人数を減らす・応援を頼む、
などの人の手の対処をしつくしてから、どうしても空かない時だけ希望休を本人と相談する。そのために、最初に表を作った段階で
「この希望休が重なって、この日が組めない」を手をつける前に伝える。

作り方。段1（本人の希望と運用の都合も外せない）のルールの場面のうち、ある日の前後 WINDOW 日の中だけで閉じる場面で小さな
モデルを作り、場面ごとに仮定（assumption）の印を付けて解く。作れなければ、CP-SAT が作れない理由になった仮定の組（芯）を
返す。月の場面の一部だけで作れないなら、月全体も必ず作れない（言い過ぎにならない）。月全体が時間切れで作れるかどうか分から
ない時も、日ごとの小さなモデルは一瞬で答えが出る（2026-11-07 の試作。作り話の施設と実際の施設の写しで、1日0.02〜0.08秒、
芯は6〜11件）。月全体にまたがるぶつかり（公休の数の合計など）は、この作りでは見えない。
"""
from __future__ import annotations

from collections import Counter

from ortools.sat.python import cp_model

from .model import WEEKDAYS, Facility, Month, Rule, Staff
from .rules import cell_value, expand, is_day_off_wish, with_overrides

WINDOW = 1          # 前後何日まで入れるか（夜勤の明けと次の日の休みが入る幅）
DTIME = 0.5         # 1日の小さなモデルの計算の量
LIMIT = 3           # 出す詰まり所の数
HARD = ("fixed", "wish", "operational")   # 段1で外せない強さ


def _day_of(key: str) -> int | None:
    tail = key.rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def chokepoints(facility: Facility, staff: list[Staff], rules: list[Rule], month: Month, *,
                window: int = WINDOW, dtime: float = DTIME, limit: int = LIMIT) -> list[dict]:
    """詰まり所の一覧（いちばんの詰まり所が先頭。多くて limit 件）。1件は
    {"day": 日, "wishes": [[名前, 希望休の日], ...], "rules": [ルールの名前, ...], "cells": [[名前, 日], ...]}。
    rules は build.impossible_wishes を通した後のルール（希望休の前の日の夜勤を止める頼みを含む）。"""
    from . import solver as S

    D = month.days
    req = S.requested_cells(rules, facility, staff, month)
    items = []   # (ルール, 場面, 月の中の日の最小, 最大)
    for r in with_overrides(rules):
        if r.flex not in HARD:
            continue
        for inst in expand(r, facility, staff, month):
            ds = [t.day for t in inst.terms if t.day >= 1]
            if ds:
                items.append((r, inst, min(ds), max(ds)))
    seen: set[frozenset] = set()
    found: list[dict] = []
    for d in range(1, D + 1):
        a, b = max(1, d - window), min(D, d + window)
        core = _core(facility, staff, month, req, [(r, i) for r, i, lo, hi in items if a <= lo and hi <= b], a, b,
                     dtime, S)
        if not core:
            continue
        key = frozenset((r.id, i.key) for r, i in core)
        if key in seen:
            continue
        seen.add(key)
        found.append(_describe(core, facility, d))
    # 同じ日の詰まりは1つにまとめ、希望休が多く重なる所を先に出す
    by_day: dict[int, dict] = {}
    for c in found:
        if c["day"] not in by_day:
            by_day[c["day"]] = c
    out = sorted(by_day.values(), key=lambda c: (-len(c["wishes"]), c["day"]))
    return out[:limit]


def _core(facility, staff, month, req, items, a, b, dtime, S) -> list:
    """a〜b 日だけの小さなモデルを、場面ごとの仮定つきで解く。作れなければ、作れない理由になった（ルール, 場面）の組。"""
    if not items:
        return []
    m = cp_model.CpModel()
    x = {}
    for s in staff:
        for d in range(a, b + 1):
            vs = []
            for sym in S._allowed(s, facility, req.get((s.name, d), set())):
                v = m.new_bool_var("")
                x[(s.name, d, sym)] = v
                vs.append(v)
            m.add_exactly_one(vs)
    lits, back = [], {}
    for r, inst in items:
        expr, const = [], 0
        for t in inst.terms:
            if t.day < 1:
                v = cell_value({}, month, t.staff, t.day)
                const += t.coef if v is not None and v in t.symbols else 0
                continue
            for sym in t.symbols:
                var = x.get((t.staff, t.day, sym))
                if var is not None:
                    expr.append(t.coef * var)
        e = sum(expr) + const
        lit = m.new_bool_var("")
        if inst.lo is not None:
            m.add(e >= inst.lo).only_enforce_if(lit)
        if inst.hi is not None:
            m.add(e <= inst.hi).only_enforce_if(lit)
        lits.append(lit)
        back[lit.index] = (r, inst)
    m.add_assumptions(lits)
    sv = cp_model.CpSolver()
    sv.parameters.num_workers = 1   # 作れない理由の組は、作業者1つの時に小さく取れる（作業者8つでは仮定の全部が返った）
    sv.parameters.random_seed = 11
    sv.parameters.max_deterministic_time = dtime
    if sv.solve(m) != cp_model.INFEASIBLE:
        return []
    return [back[i] for i in sv.sufficient_assumptions_for_infeasibility() if i in back]


def _describe(core: list, facility: Facility, center: int) -> dict:
    """芯を、詰まり所の日・重なった希望休・ぶつかったルールにする。日は、人数のルールの日があればそれ（足りないのはその日の
    勤務。希望休の前の日の夜勤が足りない時に、希望休の日を指していた。Astra）、無ければほかのルールの場面の日、それも無ければ
    希望休の日。どれも無ければ小さなモデルの真ん中の日。"""
    wishes, rules, cells = [], [], []
    cov, other, wish_days = Counter(), Counter(), Counter()
    for r, inst in core:
        d = _day_of(inst.key)
        if is_day_off_wish(r, facility):
            who = inst.key.rsplit("/", 1)[0]
            derived = r.meta.get("derived") == "rest_not_ake"
            day = d + 1 if derived and d is not None else d   # 前の日の夜勤を止める頼みは、次の日が希望休
            if day is not None and [who, day] not in wishes:
                wishes.append([who, day])
                cells.append([who, day])
            if d is not None:
                (other if derived else wish_days)[d] += 1    # 前の日の夜勤を止める頼みは、その前の日の勤務の詰まり
            continue
        name = r.name
        unit = inst.key.rsplit("/", 1)[0] if r.kind == "coverage" else None
        if unit and unit != "全体" and unit not in name:
            name += f"（{unit}）"
        if name not in rules:
            rules.append(name)
        if d is not None:
            (cov if r.kind == "coverage" else other)[d] += 1
    pick = cov or other or wish_days
    day = pick.most_common(1)[0][0] if pick else center
    return {"day": day, "wishes": wishes, "rules": rules, "cells": cells}


def _wd(month: Month, d: int) -> str:
    return "月火水木金土日"[WEEKDAYS.index(month.weekday(d))]


def _names(wishes: list, day: int, limit: int = 3) -> str:
    parts = [f"{w}さん" + ("" if d == day else f"（{d}日）") for w, d in wishes[:limit]]
    return "・".join(parts) + (f"ほか{len(wishes) - limit}人" if len(wishes) > limit else "")


def explain(chokes: list[dict], month: Month) -> list[str]:
    """作成者に見せる文（1件目は詳しく、2件目からは1行）。"""
    out = []
    for i, c in enumerate(chokes):
        d = c["day"]
        when = f"{d}日（{_wd(month, d)}）"
        rules = "".join(f"「{n}」" for n in c["rules"][:3]) + ("など" if len(c["rules"]) > 3 else "")
        if i == 0 and c["wishes"]:
            head = f"全部の希望休を入れると組めない日があります。いちばんの詰まり所は{when}です。"
            body = f"{_names(c['wishes'], d)}の希望休が重なり、" + (f"{rules}を同時には守れません。" if rules else "")
            out.append(head + body + "日勤専属の人に早出を頼む、その日の人数を一言で減らす、応援を頼む、などで空けられないか"
                       "試してください。どうしても空かない時は、希望休を本人と相談します")
        elif i == 0:   # 希望休の無い、ルールどうしのぶつかり（Astra）。希望休の相談はすすめない
            out.append(f"ルールどうしがぶつかって組めない日があります。いちばんの詰まり所は{when}です。"
                       f"{rules}を同時には守れません。どちらかを一言で直せないか試してください")
        else:
            out.append(f"ほかに{when}も詰まっています" + (f"（{_names(c['wishes'], d)}の希望休）" if c["wishes"] else ""))
    return out
