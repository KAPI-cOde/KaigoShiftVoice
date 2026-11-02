"""決まりを1次式の不等式（場面）の束に展開する。

ソルバーも表の点検も、同じ場面の一覧を使う。だからソルバーの「守った・外した」と、
表で数えた「破れている」が食い違わない（試験 S3）。

場面 = 項の並び（(人, 日, 記号の組), 係数）と、下限・上限。
日が 0 以下は前の月の末5日（0＝前の月の末日）で、表から定数として読む。
"""
from __future__ import annotations

from dataclasses import dataclass

from .model import Facility, Month, Rule, Staff


@dataclass(frozen=True)
class Term:
    staff: str
    day: int                 # 1..days。0 以下は前の月
    symbols: tuple[str, ...]
    coef: int = 1


@dataclass
class Instance:
    rule: Rule
    key: str                 # 場面の名前（例 "東/12"・"小島"・"小島/3-4"）
    terms: list[Term]
    lo: int | None
    hi: int | None


def with_overrides(rules: list[Rule]) -> list[Rule]:
    """人を名指しした月の回数のルールは、同じ勤務の全員向けの回数のルールより優先する。
    （「小川さん、今月は夜勤ゼロで」が「夜勤の回数 3〜5回」とぶつかって守られなかった。2026-11-02）
    名指しの人を、全員向けのルールの対象から外す。期間の一部だけのルール（29日まで など）・本人の希望・
    全員向けが動かせない決まり（公休の数 など）の時は外さない（希望は段の順で、動かせない決まりはそのまま守る）。"""
    import copy
    named: dict[tuple, set[str]] = {}
    for r in rules:
        if r.kind == "count" and r.who and not r.when and r.flex in ("operational", "fixed"):
            named.setdefault(tuple(sorted(r.params.get("shifts") or [])), set()).update(r.who)
    if not named:
        return rules
    out = []
    for r in rules:
        ex = (named.get(tuple(sorted(r.params.get("shifts") or [])))
              if r.kind == "count" and not r.who and r.flex != "fixed" else None)
        if ex:
            r = copy.copy(r)
            r.meta = {**r.meta, "except": sorted(ex)}
        out.append(r)
    return out


def _who(rule: Rule, staff: list[Staff]) -> list[Staff]:
    if not rule.who:
        ex = set(rule.meta.get("except") or [])
        return [s for s in staff if s.name not in ex] if ex else staff
    names = set(rule.who)
    return [s for s in staff if s.name in names]


def _days(rule: Rule, month: Month) -> list[int]:
    return list(rule.when) if rule.when else list(range(1, month.days + 1))


def _syms(spec, facility: Facility) -> tuple[str, ...]:
    if spec == "work":
        return tuple(facility.workday_symbols())
    if isinstance(spec, str):
        return (spec,)
    return tuple(spec)


def expand(rule: Rule, facility: Facility, staff: list[Staff], month: Month) -> list[Instance]:
    p = rule.params
    k = rule.kind
    out: list[Instance] = []
    people = _who(rule, staff)
    if k == "coverage":
        syms = _syms(p["shifts"], facility)
        def unit_on(s, d):  # 応援の日は、その日だけ入るユニットで数える
            return month.support.get(f"{s.name}/{d}", s.unit)

        for d in _days(rule, month):
            groups = ([(u, [s for s in people if unit_on(s, d) == u]) for u in facility.units]
                      if p.get("scope") == "each_unit" else [("全体", people)])
            for gname, members in groups:
                terms = [Term(s.name, d, syms) for s in members]
                n = p["n"]
                lo, hi = {"==": (n, n), ">=": (n, None), "<=": (None, n)}[p["op"]]
                out.append(Instance(rule, f"{gname}/{d}", terms, lo, hi))
    elif k == "count":
        syms = _syms(p["shifts"], facility)
        if p.get("capable_only"):  # その勤務に入れる人だけ（例 夜勤の回数は夜勤に入れる人だけ）
            people = [s for s in people if any(x in s.can for x in syms)]
        rest = {st.symbol for st in facility.shift_types if st.kind == "rest"}
        adjust = month.rest_adjust if set(syms) <= rest else {}
        for s in people:
            terms = [Term(s.name, d, syms) for d in _days(rule, month)]
            delta = adjust.get(s.name, 0)  # 公休の足し引き（配った後の直しの例外。12章）
            lo = p.get("min") + delta if p.get("min") is not None else None
            hi = p.get("max") + delta if p.get("max") is not None else None
            out.append(Instance(rule, s.name, terms, lo, hi))
    elif k == "sequence":
        # if の記号の翌日（または前日）の中身
        a = _syms(p["if"], facility)
        for s in people:
            for d in _days(rule, month):
                if "next_in" in p:      # x[d,if] <= x[d+1,next]（翌日が月の外なら課さない）
                    if d + 1 > month.days:
                        continue
                    terms = [Term(s.name, d, a, 1), Term(s.name, d + 1, _syms(p["next_in"], facility), -1)]
                    out.append(Instance(rule, f"{s.name}/{d}", terms, None, 0))
                if "next_not_in" in p:  # x[d,if] + x[d+1,next] <= 1
                    if d + 1 > month.days:
                        continue
                    terms = [Term(s.name, d, a), Term(s.name, d + 1, _syms(p["next_not_in"], facility))]
                    out.append(Instance(rule, f"{s.name}/{d}", terms, None, 1))
                if "prev_in" in p:      # x[d,if] <= x[d-1,prev]
                    terms = [Term(s.name, d, a, 1), Term(s.name, d - 1, _syms(p["prev_in"], facility), -1)]
                    out.append(Instance(rule, f"{s.name}/{d}", terms, None, 0))
        # 月またぎ。前の月の末日の if の後ろ（1日）も見る
        if "next_in" in p or "next_not_in" in p:
            for s in people:
                if 1 not in _days(rule, month) and rule.when:
                    continue
                if "next_in" in p:
                    terms = [Term(s.name, 0, a, 1), Term(s.name, 1, _syms(p["next_in"], facility), -1)]
                    out.append(Instance(rule, f"{s.name}/0", terms, None, 0))
                if "next_not_in" in p:
                    terms = [Term(s.name, 0, a), Term(s.name, 1, _syms(p["next_not_in"], facility))]
                    out.append(Instance(rule, f"{s.name}/0", terms, None, 1))
    elif k == "max_run":
        syms = _syms(p["shifts"], facility)
        m = p["max"]
        tail = len(next(iter(month.prev_tail.values()), []))
        for s in people:
            # 窓 [start, start+m]（m+1 日）に m を超えて入れない。前の月の末日も含める
            for start in range(1 - tail, month.days - m + 1):
                days = list(range(start, start + m + 1))
                if days[-1] < 1:
                    continue  # 前の月だけの窓は数えない
                terms = [Term(s.name, d, syms) for d in days]
                out.append(Instance(rule, f"{s.name}/{days[0]}-{days[-1]}", terms, None, m))
        # 月末の夜勤は翌月1日の明けまで続く。月末の m 日がすべて勤務で末日が夜勤だと、翌月1日で上限を超える
        # （月の中だけを見て作ると、翌月の表が作れなくなる。2026-11-02 に12月で起きた）
        ake = [st.symbol for st in facility.shift_types if st.kind == "ake"]
        night = [st.symbol for st in facility.shift_types if st.next_is_ake]
        if ake and night and ake[0] in syms and month.days >= m:
            last = list(range(month.days - m + 1, month.days + 1))
            for s in people:
                terms = [Term(s.name, d, syms) for d in last] + [Term(s.name, month.days, night)]
                out.append(Instance(rule, f"{s.name}/{last[0]}-翌月1", terms, None, m))
    elif k == "weekday":
        wd = set(p["weekdays"])
        for s in people:
            for d in _days(rule, month):
                if month.weekday(d) not in wd:
                    continue
                if "not_in" in p:
                    out.append(Instance(rule, f"{s.name}/{d}", [Term(s.name, d, _syms(p["not_in"], facility))], None, 0))
                if "in" in p:
                    out.append(Instance(rule, f"{s.name}/{d}", [Term(s.name, d, _syms(p["in"], facility))], 1, None))
    elif k == "cell":
        for s in people:
            for d in _days(rule, month):
                if "in" in p:
                    out.append(Instance(rule, f"{s.name}/{d}", [Term(s.name, d, _syms(p["in"], facility))], 1, None))
                if "not_in" in p:
                    out.append(Instance(rule, f"{s.name}/{d}", [Term(s.name, d, _syms(p["not_in"], facility))], None, 0))
    elif k == "layer":
        pass  # 待機などの層。表のマスには式をかけない（割り当ては standby.assign）
    else:
        raise ValueError(f"まだ扱わない種類 {k}")
    return out


def cell_value(table: dict[str, list[str]], month: Month, staff: str, day: int) -> str | None:
    if day >= 1:
        return table[staff][day - 1]
    tail = month.prev_tail.get(staff, [])
    idx = len(tail) - 1 + day  # day=0 → 末日
    return tail[idx] if 0 <= idx < len(tail) else None


def evaluate(inst: Instance, table: dict[str, list[str]], month: Month) -> int:
    total = 0
    for t in inst.terms:
        v = cell_value(table, month, t.staff, t.day)
        if v is not None and v in t.symbols:
            total += t.coef
    return total


def broken(inst: Instance, table: dict[str, list[str]], month: Month) -> bool:
    v = evaluate(inst, table, month)
    return (inst.lo is not None and v < inst.lo) or (inst.hi is not None and v > inst.hi)


def check_table(rules: list[Rule], facility: Facility, staff: list[Staff], month: Month,
                table: dict[str, list[str]]) -> list[dict]:
    """表で破れている場面の一覧（ソルバーと同じ式で数える）。"""
    out = []
    for r in with_overrides(rules):
        for inst in expand(r, facility, staff, month):
            if broken(inst, table, month):
                out.append({"rule": r.id, "name": r.name, "flex": r.flex, "key": inst.key,
                            "value": evaluate(inst, table, month), "lo": inst.lo, "hi": inst.hi})
    return out
