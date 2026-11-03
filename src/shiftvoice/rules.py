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


def _sides(new: dict, old: dict) -> set[str]:
    """新しい回数のルールが言った側（下限・上限）と、言っていない側でも新しい値とぶつかる古い側。"""
    out = {side for side in ("min", "max") if new.get(side) is not None}
    if "max" in out and "min" not in out and old.get("min") is not None and new["max"] < old["min"]:
        out.add("min")
    if "min" in out and "max" not in out and old.get("max") is not None and new["min"] > old["max"]:
        out.add("max")
    return out


def _replace_same(rules: list[Rule]) -> list[Rule]:
    """同じ記号の組を同じ相手に数える、帳面の回数のルールが2本ある時は、新しい方（後ろ）が古い方に代わる。
    相手が同じとは、全員どうし（全員向けと、取り込みで作った何人かまとめた公休の数を含む）か、同じ人どうし。
    新しい方が言った側だけ古い方から外す。毎月か今月だけかは帳面の期間のまま（今月だけの方は翌月に残らない）。
    （取り込みの知らせが勧める「公休は月10日」が、元の公休の数とぶつかって効かなかった。敵対レビュー 2026-11-03）
    当てるのは運用の都合（operational）どうしで、月の全部に効くものだけ。帳面に無いルール（返す案の g-・前提の p-）は
    当てない（帳面のルールは rules_at が meta に from を入れる）。"""
    import copy

    def ok(r: Rule) -> bool:
        return r.kind == "count" and not r.when and r.flex == "operational" and "from" in r.meta

    out = list(rules)
    for i, new in enumerate(rules):
        if not ok(new):
            continue
        key = tuple(sorted(new.params.get("shifts") or []))
        for j in range(i):
            old = out[j]
            if not ok(old) or tuple(sorted(old.params.get("shifts") or [])) != key:
                continue
            sides = _sides(new.params, old.params)
            if not new.who:
                if old.who and len(old.who) == 1:
                    continue   # 1人だけのルールは全員向けより優先する（with_overrides）。新しい全員向けでも消さない
                old = copy.copy(old)
                old.params = {k: v for k, v in old.params.items() if k not in sides}
            else:
                hit = sorted(set(new.who) & set(old.who or []))
                if not hit:
                    continue   # 古い方が全員向けなら、下の名指しの優先で外れる
                old = copy.copy(old)
                old.meta = {**old.meta, **{f"except_{side}": sorted(set(old.meta.get(f"except_{side}", ())) | set(hit))
                                           for side in sides}}
            out[j] = old
    return out


def with_overrides(rules: list[Rule]) -> list[Rule]:
    """人を名指しした月の回数のルールは、同じ勤務の全員向け（と、より多くの人をまとめた）回数のルールより優先する。
    （「小川さん、今月は夜勤ゼロで」が「夜勤の回数 3〜5回」とぶつかって守られなかった。2026-11-02）
    名指しのルールが言った側（下限か上限）だけ、その人を全員向けの同じ側から外す（片側だけの頼みで、
    もう片側の守りまで外さない）。期間の一部だけのルール（29日まで など）・本人の希望・
    全員向けが動かせない決まり（公休の数 など）の時は外さない。
    その前に、同じ相手に2本ある帳面の回数のルールは新しい方を残す（_replace_same）。"""
    import copy
    rules = _coverage_days(_replace_same(rules))
    named: dict[tuple, list[tuple[Rule, str, dict]]] = {}   # 勤務 → [(元のルール, 人, {"min": 値, "max": 値})]
    for r in rules:
        if r.kind == "count" and r.who and not r.when and r.flex in ("operational", "fixed"):
            for w in r.who:
                v = {side: r.params[side] for side in ("min", "max") if r.params.get(side) is not None}
                if v:
                    named.setdefault(tuple(sorted(r.params.get("shifts") or [])), []).append((r, w, v))
    if not named:
        return rules
    out = []
    for r in rules:
        src = (named.get(tuple(sorted(r.params.get("shifts") or [])))
               if r.kind == "count" and not r.when and r.flex != "fixed" else None)
        if src and r.who:
            # 何人かをまとめたルール（取り込みで作った公休の数など）は、その中の人をより少ない人数で名指ししたルールに譲る
            src = [(s, w, v) for s, w, v in src if s is not r and w in r.who and len(s.who) < len(r.who)]
        if src:
            d: dict[str, dict] = {}
            for _s, w, v in src:
                d.setdefault(w, {}).update(v)
            gmin, gmax = r.params.get("min"), r.params.get("max")
            ex_min, ex_max = set(r.meta.get("except_min", ())), set(r.meta.get("except_max", ()))
            for w, v in d.items():
                # 言った側は上書き。言っていない側も、名指しの値とぶつかる時（上限0 と 下限3 など）は外す
                if "min" in v or ("max" in v and gmin is not None and v["max"] < gmin):
                    ex_min.add(w)
                if "max" in v or ("min" in v and gmax is not None and v["min"] > gmax):
                    ex_max.add(w)
            r = copy.copy(r)
            r.meta = {**r.meta, "except_min": sorted(ex_min), "except_max": sorted(ex_max)}
        out.append(r)
    return out


def on_days(p: dict, month: Month, d: int) -> bool:
    """人数のルールが掛かる日か。曜日（weekdays）を持てばその曜日、祝日も（holidays）ならその月の祝日も。
    曜日を持たなければ毎日。"""
    if not p.get("weekdays"):
        return True
    return month.weekday(d) in p["weekdays"] or bool(p.get("holidays")) and d in month.holidays


def _coverage_days(rules: list[Rule]) -> list[Rule]:
    """曜日つきの人数のルールが、毎日の同じ勤務の人数のルールの上限より多く頼んだ時（「土日は早番を2人」と
    「早番の人数 1人」）、その曜日だけ毎日のルールの上限を外す。下限は外さない（人を減らす頼みは、ぶつかって
    守れないと知らせる方に倒す）。"""
    import copy
    days = [r for r in rules if r.kind == "coverage" and r.params.get("weekdays")]
    if not days:
        return rules
    out = []
    for g in rules:
        p = g.params
        if g.kind == "coverage" and not p.get("weekdays") and p["op"] in ("==", "<="):
            offs = []
            for r in days:
                q = r.params
                if sorted(q["shifts"] if isinstance(q["shifts"], list) else [q["shifts"]]) != \
                        sorted(p["shifts"] if isinstance(p["shifts"], list) else [p["shifts"]]) \
                        or q.get("scope") != p.get("scope"):
                    continue
                if q["n"] > p["n"] and q["op"] in ("==", ">=", "<="):
                    offs.append({"weekdays": q["weekdays"], "holidays": bool(q.get("holidays"))})
            if offs:
                g = copy.copy(g)
                g.meta = {**g.meta, "hi_off": offs}
        out.append(g)
    return out


def _who(rule: Rule, staff: list[Staff]) -> list[Staff]:
    if not rule.who:
        return staff
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
            if not on_days(p, month, d):
                continue   # 曜日つきの人数（「土日は日勤を2人」）はその曜日だけ
            hi_off = any(on_days(o, month, d) for o in rule.meta.get("hi_off", ()))
            groups = ([(u, [s for s in people if unit_on(s, d) == u]) for u in facility.units]
                      if p.get("scope") == "each_unit" else [("全体", people)])
            for gname, members in groups:
                terms = [Term(s.name, d, syms) for s in members]
                n = p["n"]
                lo, hi = {"==": (n, n), ">=": (n, None), "<=": (None, n)}[p["op"]]
                if hi_off:
                    hi = None   # 曜日つきの人数が上限を超えて頼んだ日（with_overrides）
                if lo is None and hi is None:
                    continue
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
            if s.name in rule.meta.get("except_min", ()):   # 名指しのルールが下限を言った人（with_overrides）
                lo = None
            if s.name in rule.meta.get("except_max", ()):
                hi = None
            if lo is None and hi is None:
                continue
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
