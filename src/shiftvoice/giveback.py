"""引き受けを次の月に返す（12章。試験 B1〜B4・B7）。

- 入力は前の月の引き受けの記録（了承をもらって表を直した回数。発生日が基準日まで）
- 何を重い当番として返すかは施設の設定 settings.giveback（ひだまりは土日祝の夜勤）
- 返さない案と返す案は、同じ決まり・同じ種・同じ計算の量で作る（A21）。返す案だけに
  「引き受けた人が重い当番に入る1回ごとに weight×引き受けた回数」を目的に足し、合格基準を制約として入れる（制約版）
  ① 重い当番の合計は同じ ② 動かせない決まりの違反0 ③ 人の間の差は返さない案から slack 回まで
  ④ ほかの人の増分は1人 max_increase 回まで ⑤ 本人の希望の外れを増やさない
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import Acceptance, Facility, Month, Rule, Staff
from .solver import SolveResult, solve_month

DEFAULT = {"shift": "夜", "days": "weekend_or_holiday", "weight": 150, "max_increase": 2, "spread_slack": 1}


def settings(facility: Facility) -> dict:
    return {**DEFAULT, **facility.settings.get("giveback", {})}


def heavy_days(month: Month, gs: dict) -> list[int]:
    return [d for d in range(1, month.days + 1) if month.is_weekend_or_holiday(d)]


def accepted_counts(items: list[Acceptance], ym: str, base_date: str) -> dict[str, int]:
    """その月（YYYY-MM）の引き受けの回数。基準日より後の発生日の記録は数えない（デモ設定）。"""
    out: dict[str, int] = {}
    for a in items:
        if a.date.startswith(ym) and a.date <= base_date:
            out[a.staff] = out.get(a.staff, 0) + 1
    return out


def heavy_counts(table: dict[str, list[str]], days: list[int], shift: str) -> dict[str, int]:
    return {n: sum(row[d - 1] == shift for d in days) for n, row in table.items()}


@dataclass
class GiveBack:
    status: str                        # ok / cannot（この条件では返せません）/ base_failed
    base: SolveResult
    give: SolveResult | None
    before: dict[str, int] = field(default_factory=dict)   # 返さない案の重い当番の回数
    after: dict[str, int] = field(default_factory=dict)    # 返す案
    counts: dict[str, int] = field(default_factory=dict)   # 引き受けた回数
    checks: dict[str, bool] = field(default_factory=dict)
    message: str = ""

    @property
    def increased(self) -> dict[str, tuple[int, int]]:
        return {n: (self.before[n], self.after[n]) for n in self.after if self.after[n] > self.before[n]}

    @property
    def decreased(self) -> dict[str, tuple[int, int]]:
        return {n: (self.before[n], self.after[n]) for n in self.after if self.after[n] < self.before[n]}


def _hard_broken(r: SolveResult) -> int:
    return sum(1 for b in r.broken if b["flex"] in ("fixed",))


def _wish_broken(r: SolveResult) -> int:
    return sum(1 for b in r.broken if b["flex"] == "wish")


def build_pair(facility: Facility, staff: list[Staff], rules: list[Rule], month: Month,
               counts: dict[str, int]) -> GiveBack:
    gs = settings(facility)
    days = heavy_days(month, gs)
    sym = gs["shift"]
    base = solve_month(facility, staff, rules, month)
    if base.status != "ok" or _hard_broken(base):
        return GiveBack("base_failed", base, None, message="返さない案が作れませんでした")
    before = heavy_counts(base.table, days, sym)
    capable = [s.name for s in staff if sym in s.can]
    spread0 = max(before[n] for n in capable) - min(before[n] for n in capable)

    def extra(m, x, penalties):
        def heavy(name):
            return sum(x[(name, d, sym)] for d in days if (name, d, sym) in x)
        for name, c in counts.items():
            if c > 0:
                penalties.append(gs["weight"] * c * heavy(name))
        for name in capable:  # ④ 引き受けた人どうしで移す時も、増える人は1人 max_increase 回まで（Astra の指摘6）
            m.add(heavy(name) <= before[name] + gs["max_increase"])
        hi = m.new_int_var(0, len(days), "giveback_hi")
        lo = m.new_int_var(0, len(days), "giveback_lo")
        hs = []
        for name in capable:
            v = m.new_int_var(0, len(days), "")
            m.add(v == heavy(name))
            hs.append(v)
        m.add_max_equality(hi, hs)
        m.add_min_equality(lo, hs)
        m.add(hi - lo <= spread0 + gs["spread_slack"])                    # ③
        m.add(sum(heavy(n) for n in capable) == sum(before[n] for n in capable))  # ①

    give = solve_month(facility, staff, rules, month, extra=extra)
    gb = GiveBack("ok", base, give, before=before, counts=dict(counts))
    if give.status != "ok":
        gb.status, gb.message = "cannot", "この条件では返せません"
        return gb
    after = heavy_counts(give.table, days, sym)
    gb.after = after
    receivers = [n for n in capable if counts.get(n, 0) == 0]
    gb.checks = {
        "1_total_same": sum(after.values()) == sum(before.values()),
        "2_no_hard_broken": _hard_broken(give) == 0,
        "3_spread": (max(after[n] for n in capable) - min(after[n] for n in capable)) <= spread0 + gs["spread_slack"],
        "4_increase": all(after[n] - before[n] <= gs["max_increase"] for n in capable),
        "5_wish_not_more": _wish_broken(give) <= _wish_broken(base),
        "returned": sum(after.get(n, 0) for n in counts) < sum(before.get(n, 0) for n in counts),
    }
    if not all(gb.checks[k] for k in gb.checks if k[0].isdigit()):
        gb.status, gb.message = "cannot", "この条件では返せません"
    return gb


def pattern_counts(table: dict[str, list[str]], facility: Facility, run_len: int = 5) -> dict[str, int]:
    """並びの負担（合格基準には入れない。B5・17章 A6）。
    飛び石＝休み・勤務・休み（勤務が1日だけ挟まる）。長い連勤＝勤務の日が run_len 日以上続いた回数（月の中だけ）。"""
    work = {st.symbol for st in facility.shift_types if st.counts_as_workday}
    tobi = runs = 0
    for row in table.values():
        w = [c in work for c in row]
        tobi += sum(1 for i in range(1, len(w) - 1) if w[i] and not w[i - 1] and not w[i + 1])
        n = 0
        for x in w + [False]:
            if x:
                n += 1
            else:
                runs += n >= run_len
                n = 0
    return {"tobi": tobi, "long_runs": runs}
