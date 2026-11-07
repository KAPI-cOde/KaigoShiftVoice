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
from .rules import _syms_of, broken, check_table, expand
from .solver import requested_cells, solve_month

WINDOWS = (3, 7, 14, None)


def carry_rules(facility: Facility, base: dict[str, list[str]], rules: list[Rule],
                opened: dict[tuple[str, int], set[str]] | None = None) -> list[Rule]:
    """前の表にある研修・有給を、そのマスの決まりとして残す。決まりがそのマスで外す（not_in）なら残さない。
    話して足した勤務はふつうの勤務なので、そのマスを別の勤務にする頼みがあれば残さない。有給も同じ。ルールがそのマスを開けている時
    （opened）も残さない。ルールが効いている間は前の表に寄せる点で残り、固定すると回数のルールとぶつかるため"""
    req = {st.symbol for st in facility.shift_types if st.on_request_only}
    spoken = {st.symbol for st in facility.shift_types if st.spoken}
    leave = {st.symbol for st in facility.shift_types if st.kind == "leave"}
    out = []
    for name, row in base.items():
        for d, sym in enumerate(row, 1):
            if sym not in req:
                continue
            removed = any(r.kind == "cell" and sym in r.params.get("not_in", []) and
                          (not r.who or name in r.who) and (not r.when or d in r.when) for r in rules)
            if removed:
                continue
            if sym in spoken and (sym in (opened or {}).get((name, d), ()) or any(
                    r.kind == "cell" and "in" in r.params and sym not in r.params["in"] and
                    (not r.who or name in r.who) and (not r.when or d in r.when) for r in rules)):
                continue
            if sym in leave and any(r.kind == "cell" and "in" in r.params and sym not in r.params["in"] and
                                    (not r.who or name in r.who) and (not r.when or d in r.when) for r in rules):
                continue   # 有給のマスを別の勤務にする頼み（有給の日の言い直し・元の勤務に戻す）
            if sym in leave and any(r.kind == "count" and r.who and name in r.who and r.params.get("max") is not None
                                    and sym in _syms_of(r.params.get("shifts")) and r.params["max"] < row.count(sym)
                                    for r in rules):
                continue   # 「〇〇さんは有給なしで」。残すと回数の上限とぶつかり、段を下げても有給が残った
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


def _off(b: dict) -> int:
    """回数がルールからいくつ離れているか。"""
    return max(0, b["value"] - b["hi"] if b["hi"] is not None else 0, b["lo"] - b["value"] if b["lo"] is not None else 0)


def worse_note(was: dict[tuple[str, str], dict], broken: list[dict]) -> str:
    """前の表で外れていた回数のルールが、直した表でさらに離れた人を言う。"""
    out = []
    for b in broken:
        old = was.get((b["rule"], b["key"]))
        if old and _off(b) > _off(old):
            want = b["lo"] if b["lo"] == b["hi"] else f"{b['lo'] if b['lo'] is not None else ''}〜{b['hi'] if b['hi'] is not None else ''}"
            out.append(f"{b['key']}さんの{b['name'].split('（')[0]}が{old['value']}から{b['value']}になり、ルールの{want}からさらに離れました")
    return "。".join(out)


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
    all_rules = rules + carry_rules(facility, base, rules, requested_cells(rules, facility, staff, month))
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
    # （外れを直す点が入ると、一言と関係のないマスまで数十マス動いた。2026-11-02 に測った）。
    # 運用の都合も同じ。公休を守りきれなかった月は、1マスの一言でも狭い範囲の直しが全部失敗し、ひと月を解き直して
    # 54マス動いた（2026-11-06 通しの試し）
    off = [b for b in check_table(rules, facility, staff, month, base)
           if b["flex"] in ("negotiable", "operational") and b["rule"] not in new_ids]
    skip = {(b["rule"], b["key"]) for b in off}
    # 外れている回数を、一言でさらに悪くしない。公休10日の人に休みを足すと11日になり、知らせも出なかった（Astra 2026-11-06 の
    # 指摘1）。別の休みを振り替えて保つ。範囲の中でできなければ広げ、それでも離れたら知らせる
    counts = {r.id for r in rules if r.kind == "count"}
    was = {(b["rule"], b["key"]): b for b in off if b["flex"] == "operational" and b["rule"] in counts}
    hold = {k: b["value"] for k, b in was.items()}
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
                        skip=skip, hold=hold)
        tries.append({"window": w, "status": r.status, "seconds": round(r.seconds, 3)})
        if r.status != "ok":
            continue
        hard = [b for b in r.broken if b["flex"] in ("fixed", "wish", "operational") and (b["rule"], b["key"]) not in skip]
        new_broken = [b for b in r.broken if b["rule"] in new_ids]
        worse = worse_note(was, r.broken)
        if (hard or new_broken or worse) and w is not None:
            continue  # 範囲の中では守れない。広げる
        staged = StagedResult(r, [{"stage": 1, "status": "ok", "seconds": round(r.seconds, 3)}], note=worse or None)
        return ResolveResult(staged, w, changed_cells(base, r.table), tries)
    staged = solve_staged(facility, staff, all_rules, month, hint=base)
    if staged.ok:
        worse = worse_note(was, staged.result.broken)
        staged.note = "。".join(x for x in (staged.note, worse) if x) or None
        if worse and staged.detail is not None:
            staged.detail["notes"].append(worse)
    rr = ResolveResult(staged, None, changed_cells(base, staged.result.table) if staged.ok else [], tries)
    rr.tries.append({"window": "staged", "status": staged.result.status, "seconds": round(staged.seconds, 3)})
    return rr
