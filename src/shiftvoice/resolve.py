"""狭い範囲の直し（確定の前。一言ごとに表を直す）。

- 撮影用の前提（固定のマス・回数の条件）は元の表を作る時だけ。直す時は使わない（A24 の1）
- 前の表をヒントにし、1マス変えるごとに keep_weight で寄せる（4章。500 で数マス）
- 速さのため、変えてよい日を 直す日の前後3日 → 7日 → 14日 → ひと月 と広げる。範囲の外のマスは止める。
  新しい決まりが範囲の中で守れない時は次の幅へ広げる
- 前の表にある研修・有給（頼まれた時だけ置く記号）は、言葉で外されるまで残す（表にある頼み）
- 1か月まで広げても作れない時は、段の順（build.solve_staged）で解く
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from .build import StagedResult, solve_staged
from .model import Facility, Month, Rule, Staff
from .rules import broken, check_table, expand
from .solver import solve_month

WINDOWS = (3, 7, 14, None)


def carry_rules(facility: Facility, base: dict[str, list[str]], rules: list[Rule]) -> list[Rule]:
    """前の表にある研修・有給を、そのマスの決まりとして残す。決まりがそのマスで外す（not_in）なら残さない。"""
    req = {st.symbol for st in facility.shift_types if st.on_request_only}
    out = []
    for name, row in base.items():
        for d, sym in enumerate(row, 1):
            if sym not in req:
                continue
            removed = any(r.kind == "cell" and sym in r.params.get("not_in", []) and
                          (not r.who or name in r.who) and (not r.when or d in r.when) for r in rules)
            if removed:
                continue
            out.append(Rule(id=f"c-{name}-{d}", ver=1, name=f"表にある{sym}（{name}さん {d}日）",
                            kind="cell", flex="fixed", params={"in": [sym]}, who=[name], when=[d],
                            scope="once", meta={"carry": True}))
    return out


def changed_cells(a: dict[str, list[str]], b: dict[str, list[str]]) -> list[dict]:
    out = []
    for name in a:
        for d, (x, y) in enumerate(zip(a[name], b[name]), 1):
            if x != y:
                out.append({"staff": name, "day": d, "before": x, "after": y})
    return out


def focus_days(new_rules: list[Rule], facility: Facility, staff: list[Staff], month: Month,
               base: dict[str, list[str]]) -> set[int] | None:
    """新しい決まりが触る日。日の決まった決まりはその日、前の表で破れていればその日。分からなければ None（ひと月）。"""
    days: set[int] = set()
    for r in new_rules:
        if r.kind == "cell" and r.when:
            days.update(r.when)
            continue
        if r.kind in ("count", "max_run", "coverage"):
            return None
        for inst in expand(r, facility, staff, month):
            if broken(inst, base, month):
                days.update(t.day for t in inst.terms if t.day >= 1)
    return days


@dataclass
class ResolveResult:
    staged: StagedResult
    window: int | None              # 使った幅（None はひと月）
    changes: list[dict] = field(default_factory=list)
    tries: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.staged.ok

    @property
    def table(self):
        return self.staged.result.table

    @property
    def seconds(self) -> float:
        return sum(t["seconds"] for t in self.tries)


def resolve(facility: Facility, staff: list[Staff], rules: list[Rule], month: Month,
            base: dict[str, list[str]], new_rules: list[Rule] | None = None) -> ResolveResult:
    """rules は帳面のこの月の決まり（前提を含めない）。new_rules は今回の一言で増えた・変わった決まり。"""
    new_rules = new_rules or []
    all_rules = rules + carry_rules(facility, base, rules)
    st = facility.settings.get("solve", {})
    dtime = st.get("resolve_max_deterministic_time", 5.0)
    # 直す時の質の物差しは施設の設定（resolve_quality）。飛び石・同じ勤務の3連続は人×日の変数が増えて
    # 2秒では数十マス動く案しか見つからなかったため、ひだまりは土日の休みの偏りだけにした（2026-11-02 に測った）
    if "resolve_quality" in facility.settings:
        facility = copy.deepcopy(facility)
        facility.settings["quality"] = facility.settings["resolve_quality"]
    focus = focus_days(new_rules, facility, staff, month, base)
    new_ids = {r.id for r in new_rules}
    # 前の表で既に外れていた相談できる決まりの場面は「これ以上悪くしない」。直しても点にしない
    # （外れを直す点が入ると、一言と関係のないマスまで数十マス動いた。2026-11-02 に測った）
    skip = {(b["rule"], b["key"]) for b in check_table(rules, facility, staff, month, base)
            if b["flex"] == "negotiable" and b["rule"] not in new_ids}
    tries = []
    for w in WINDOWS:
        if focus is None and w is not None:
            continue
        if focus is not None and not focus and w is not None:
            # 前の表で新しい決まりが破れていない。表はそのまま
            w = 0
        fixed = {}
        if w is not None:
            near = {d for f in focus for d in range(f - w, f + w + 1)} if focus else set()
            for s in staff:
                for d in range(1, month.days + 1):
                    if d not in near:
                        fixed[(s.name, d)] = base[s.name][d - 1]
        r = solve_month(facility, staff, all_rules, month, hint=base, fixed_cells=fixed, max_dtime=dtime,
                        skip=skip)
        tries.append({"window": w, "status": r.status, "seconds": round(r.seconds, 3)})
        if r.status != "ok":
            continue
        hard = [b for b in r.broken if b["flex"] in ("fixed", "wish", "operational")]
        new_broken = [b for b in r.broken if b["rule"] in new_ids]
        if (hard or new_broken) and w is not None:
            continue  # 範囲の中では守れない。広げる
        staged = StagedResult(r, [{"stage": 1, "status": "ok", "seconds": round(r.seconds, 3)}])
        return ResolveResult(staged, w, changed_cells(base, r.table), tries)
    staged = solve_staged(facility, staff, all_rules, month, hint=base)
    rr = ResolveResult(staged, None, changed_cells(base, staged.result.table) if staged.ok else [], tries)
    rr.tries.append({"window": "staged", "status": staged.result.status, "seconds": round(staged.seconds, 3)})
    return rr
