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
from .rules import Instance, cell_value, expand, is_day_off_wish, with_overrides

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
    items = _items(facility, staff, rules, month)
    seen: set[frozenset] = set()
    found: list[dict] = []
    for d in range(1, D + 1):
        a, b = max(1, d - window), min(D, d + window)
        core = _core(facility, staff, month, req, _window(items, a, b), a, b, dtime, S)
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


def _items(facility: Facility, staff: list[Staff], rules: list[Rule], month: Month) -> list:
    """段1で外せないルールの場面と、その場面の月の中の日の最小・最大。[(ルール, 場面, 最小, 最大), ...]"""
    items = []
    for r in with_overrides(rules):
        if r.flex not in HARD:
            continue
        for inst in expand(r, facility, staff, month):
            ds = [t.day for t in inst.terms if t.day >= 1]
            if ds:
                items.append((r, inst, min(ds), max(ds)))
    return items


def _window(items, a: int, b: int) -> list:
    """a〜b 日の小さなモデルに入れる場面。中で閉じる場面と、はみ出す小さな上限を窓の分に切った場面"""
    return [(r, i) for r, i, lo, hi in items if a <= lo and hi <= b] + _caps_in(items, a, b)


def _caps_in(items, a: int, b: int) -> list:
    """a〜b 日からはみ出す上限の場面（「早番と8時の日勤は入れない（月0回まで）」など）を、a〜b 日の分だけにした場面。
    係数がどれも0以上なら、窓の外の分を落とした和も同じ上限を超えないので、言い過ぎにならない。窓の日数より小さい上限だけが
    日の勤務を縛るので、それだけを入れる（月7回までの夜勤は3日の窓では縛らない）"""
    out = []
    for r, inst, lo, hi in items:
        if a <= lo and hi <= b:
            continue
        if inst.hi is None or inst.hi >= b - a + 1 or any(t.coef < 0 for t in inst.terms):
            continue
        terms = [t for t in inst.terms if t.day < 1 or a <= t.day <= b]
        if any(t.day >= 1 for t in terms):
            out.append((r, Instance(rule=inst.rule, key=inst.key, terms=terms, lo=None, hi=inst.hi)))
    return out


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
        if d is not None and d < 1:
            d = 1   # 前の月の末の日の場面（「夜勤の次の日は公休」の0日）は、月の1日の詰まり（0日を指して止まった。Astra）
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
        if r.kind == "coverage" and d is not None and "op" in r.params and (inst.lo, inst.hi) != _orig(r.params):
            # 日付の人数などでその日だけゆるめた数は、ゆるめた後の数で言う（前は元の「毎日2人」と言った。Astra）
            name += f"（{d}日は{_amount(inst.lo, inst.hi)}）"
        if name not in rules:
            rules.append(name)
        if d is not None:
            (cov if r.kind == "coverage" else other)[d] += 1
    pick = cov or other or wish_days
    day = pick.most_common(1)[0][0] if pick else center
    return {"day": day, "wishes": wishes, "rules": rules, "cells": cells}


def _orig(p: dict) -> tuple:
    n = p["n"]
    return {"==": (n, n), ">=": (n, None), "<=": (None, n)}[p["op"]]


def _amount(lo, hi) -> str:
    if lo is not None and lo == hi:
        return f"{lo}人"
    if hi is None:
        return f"{lo}人以上"
    if lo is None:
        return f"{hi}人まで"
    return f"{lo}〜{hi}人"


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


def hole_offers(facility: Facility, staff: list[Staff], rules: list[Rule], month: Month, day: int, *,
                window: int = WINDOW, dtime: float = DTIME, limit: int = 3) -> list[dict]:
    """詰まり所の日 day に、誰か1人を1つの勤務に入れれば、その日の前後が組めるか（作者 2026-11-07「この人に早番を
    頼むなら解決する？」）。候補ごとに、詰まり所と同じ小さなモデルで、その人をその勤務に決めて解く。希望休と動かせない
    ルールは外さない。外してよいのは、その人だけの場面で、その日に当たる運用の都合のルールだけ（いちばん少なく外す）。
    ほかのユニットの人は、その日だけ詰まったユニットへ応援で入る形で試す。
    1件は {"staff", "day", "symbol", "unit"（応援で入るユニット。応援でなければ None）, "hole_unit", "except": [(ルール, 場面)]}。
    並べ方は、外すルールが少ない人、外すなら休みの並びや連勤より回数のルールを先（休みの並びは体を守るため）、
    同じユニットの人、その月の勤務の日が少ない人の順。1人につき1件"""
    from dataclasses import replace

    from . import solver as S

    a, b = max(1, day - window), min(month.days, day + window)
    # 候補を確かめる窓は、day を含む詰まり所の窓（真ん中が day−window〜day+window）を全部覆う幅。真ん中の窓だけで
    # 確かめると、応援で抜けたユニットの次の日の窓が組めなくなるのを見落とした（作り話の施設の試験で、受けた後も同じ日が
    # 詰まり所に残った）
    a2, b2 = max(1, day - 2 * window), min(month.days, day + 2 * window)
    req = S.requested_cells(rules, facility, staff, month)
    base = _items(facility, staff, rules, month)
    core = _core(facility, staff, month, req, _window(base, a, b), a, b, dtime, S)
    if not core:
        return []
    units = sorted(_hole_units(core, facility))
    work = {st.symbol for st in facility.shift_types if st.kind == "work"}
    syms = sorted({x for _r, inst in core if _r.kind in ("coverage", "unit_after")
                   for t in inst.terms if t.day == day and t.coef > 0 for x in t.symbols} & work)
    table = month.table or {}
    best: dict[str, tuple] = {}
    for s in staff:
        row = table.get(s.name)
        workdays = sum(v in work for v in row) if row else month.days   # 表に行の無い人（その月から入る人）は後ろへ
        for sym in syms:
            if sym not in s.can:
                continue
            for u in ([None] if not units or s.unit in units else units):
                m2 = month if u is None else replace(month, support={**month.support, f"{s.name}/{day}": u})
                items = base if u is None else _items(facility, staff, rules, m2)
                exc = _try_pin(facility, staff, m2, req, _window(items, a2, b2), a2, b2, dtime, S, s.name, day, sym)
                if exc is None:
                    continue
                heavy = sum(1 for r, _i in exc if r.kind != "count")
                key = (len(exc), heavy, u is not None, workdays)
                if s.name not in best or key < best[s.name][0]:
                    best[s.name] = (key, {"staff": s.name, "day": day, "symbol": sym, "unit": u,
                                          "hole_unit": u or (s.unit if units else None), "except": exc})
    return [v for _k, v in sorted(best.values(), key=lambda kv: kv[0])][:limit]


def _hole_units(core: list, facility: Facility) -> set[str]:
    """芯の人数のルールの場面が指すユニット（「1丁目/12」「1丁目（明け番なし）/12」）"""
    out = set()
    for r, inst in core:
        if r.kind in ("coverage", "unit_after"):
            u = inst.key.rsplit("/", 1)[0].split("（")[0]
            if u in facility.units:
                out.add(u)
    return out


def _try_pin(facility, staff, month, req, items, a, b, dtime, S, who: str, day: int, sym: str) -> list | None:
    """who を day の sym に決めて a〜b 日を解く。作れなければ None。作れれば、外した場面の一覧（無ければ空）。
    外してよいのは、who だけの場面で day に当たる運用の都合の場面。外す数を最も少なくする"""
    m = cp_model.CpModel()
    x = {}
    for s in staff:
        for d in range(a, b + 1):
            vs = []
            for k in S._allowed(s, facility, req.get((s.name, d), set())):
                v = m.new_bool_var("")
                x[(s.name, d, k)] = v
                vs.append(v)
            m.add_exactly_one(vs)
    if (who, day, sym) not in x:
        return None
    m.add(x[(who, day, sym)] == 1)
    soft = []
    for r, inst in items:
        expr, const = [], 0
        for t in inst.terms:
            if t.day < 1:
                v = cell_value({}, month, t.staff, t.day)
                const += t.coef if v is not None and v in t.symbols else 0
                continue
            for k in t.symbols:
                var = x.get((t.staff, t.day, k))
                if var is not None:
                    expr.append(t.coef * var)
        e = sum(expr) + const
        mine = (r.flex == "operational" and all(t.staff == who for t in inst.terms)
                and any(t.day == day for t in inst.terms))
        lit = m.new_bool_var("") if mine else None
        if inst.lo is not None:
            c = m.add(e >= inst.lo)
            if lit is not None:
                c.only_enforce_if(lit)
        if inst.hi is not None:
            c = m.add(e <= inst.hi)
            if lit is not None:
                c.only_enforce_if(lit)
        if lit is not None:
            soft.append((lit, r, inst))
    if soft:
        m.maximize(sum(lit for lit, _r, _i in soft))
    sv = cp_model.CpSolver()
    sv.parameters.num_workers = 1
    sv.parameters.random_seed = 11
    sv.parameters.max_deterministic_time = dtime
    st = sv.solve(m)
    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None
    return [(r, inst) for lit, r, inst in soft if not sv.value(lit)]
