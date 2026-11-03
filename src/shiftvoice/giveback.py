"""引き受けた人に、次の月を少し楽にする（12章。2026-11-03 に作者が返し方を変えた）。

土日祝の夜勤のような、人によって好みの分かれる勤務を減らすのはやめた。誰にとってもほぼ助かる物を少しだけ返す。
- 有給をその月の平均＋1日（平均は返さない案の全員の有給の日数。小数は切り捨て）。もともとそれ以上なら変えない
- いちばん長い連勤を1日短く（4日以上ある時だけ）
- 締め切りの前なので表は一から作り直す。ただし引き受けていない人について、5日続く勤務と飛び石（休み・勤務・休み）を
  返さない案より増やさない。引き受けていない人の有給は頼まれた分のまま
- 作れない時は段を下げる。有給と連勤 → 有給だけ → 連勤だけ → この条件では返せません
- 入力は引き受けの記録（了承をもらって表を直した回数と、配った後の直しで勤務が入った回数）
返さない案と返す案は、同じ決まり・同じ種・同じ計算の量で作る（A21）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import Acceptance, Facility, Month, Rule, Staff
from .build import solve_staged
from .solver import SolveResult, solve_month

DEFAULT = {"leave": "有", "extra_leave": 1, "shorten_run": 1, "min_run_to_shorten": 4, "window_months": 3}
STAGES = (("leave", "run"), ("leave",), ("run",))


def settings(facility: Facility) -> dict:
    return {**DEFAULT, **facility.settings.get("giveback", {})}


def _months_back(ym: str, n: int) -> list[str]:
    y, m = int(ym[:4]), int(ym[5:7])
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out


def accepted_counts(items: list[Acceptance], ym: str, base_date: str, window: int = 1,
                    include_returned: bool = False) -> dict[str, int]:
    """返していない引き受けの回数。ym（YYYY-MM）から window か月さかのぼる。基準日より後の発生日は数えない。
    返した記録（returned_in がある物）は数えない（返しきれなかった分は次の月へ持ち越す）。
    画面の「〇月の引き受け」は返した物も数える（include_returned）。"""
    months = set(_months_back(ym, window))
    out: dict[str, int] = {}
    for a in items:
        if a.date[:7] in months and a.date <= base_date and (include_returned or not a.returned_in):
            out[a.staff] = out.get(a.staff, 0) + 1
    return out


def longest_run(row: list[str], work: set[str]) -> int:
    best = n = 0
    for c in row:
        n = n + 1 if c in work else 0
        best = max(best, n)
    return best


@dataclass
class GiveBack:
    status: str                        # ok / cannot（この条件では返せません）/ base_failed
    base: SolveResult
    give: SolveResult | None
    counts: dict[str, int] = field(default_factory=dict)   # 引き受けた回数
    people: list[dict] = field(default_factory=list)       # 引き受けた人ごとの前後
    others: dict = field(default_factory=dict)             # 引き受けていない人の並びの前後
    stage: tuple = ()
    leave_avg: float = 0.0
    checks: dict[str, bool] = field(default_factory=dict)
    message: str = ""
    base_note: str = ""                # 返さない案で段を下げた時の知らせ
    base_seconds: float = 0.0          # 返さない案に使った秒（全段の合計）


def _hard_broken(r: SolveResult) -> int:
    return sum(1 for b in r.broken if b["flex"] in ("fixed",))


def _wish_broken(r: SolveResult) -> int:
    return sum(1 for b in r.broken if b["flex"] == "wish")


def build_pair(facility: Facility, staff: list[Staff], rules: list[Rule], month: Month,
               counts: dict[str, int]) -> GiveBack:
    gs = settings(facility)
    work = set(facility.workday_symbols())
    yu = gs["leave"]
    # 返さない案は段の順で作る（作る時に段を下げた施設や、一言でぶつかるルールを足した施設でも翌々月へ進める。
    # 敵対レビュー 2026-11-03）。段1で作れる時は、これまでと同じ表になる
    staged = solve_staged(facility, staff, rules, month)
    base = staged.result
    if base.status != "ok" or _hard_broken(base):
        return GiveBack("base_failed", base, None, message=staged.message or "返さない案が作れませんでした",
                        base_seconds=staged.seconds)
    note, secs = staged.note or "", staged.seconds
    names = [s.name for s in staff]
    takers = [n for n in names if counts.get(n, 0) > 0]
    others = [n for n in names if n not in takers]
    leave0 = {n: base.table[n].count(yu) for n in names}
    avg = sum(leave0.values()) / len(names)
    target = {n: max(int(avg) + gs["extra_leave"], leave0[n]) for n in takers}
    run0 = {n: longest_run(base.table[n], work) for n in takers}
    pat0 = pattern_counts({n: base.table[n] for n in others}, facility)

    def stage_rules(stage):
        out = []
        for n in takers:
            if "leave" in stage and target[n] > leave0[n] and yu in facility.symbols:   # 有給の記号が無い施設では返さない
                out.append(Rule(id=f"g-leave-{n}", ver=1, name=f"{n}さん 有給を{target[n]}日", kind="count",
                                flex="fixed", params={"shifts": [yu], "min": target[n], "max": target[n]},
                                who=[n], when=None, scope="once"))
            if "run" in stage and run0[n] >= gs["min_run_to_shorten"]:
                out.append(Rule(id=f"g-run-{n}", ver=1, name=f"{n}さん 連勤を{run0[n] - gs['shorten_run']}日まで",
                                kind="max_run", flex="fixed",
                                params={"shifts": "work", "max": run0[n] - gs["shorten_run"]},
                                who=[n], when=None, scope="once"))
        return out

    def extra(m, x, penalties):
        # 引き受けていない人の5日続く勤務（5日の窓の数）と飛び石を、返さない案より増やさない
        def w(name, d):
            return sum(x[(name, d, y)] for y in work if (name, d, y) in x)
        runs, tobi = [], []
        for n in others:
            for i in range(1, month.days - 3):
                b = m.new_bool_var("")
                m.add(sum(w(n, d) for d in range(i, i + 5)) - 4 <= b)
                runs.append(b)
            for d in range(2, month.days):
                t = m.new_bool_var("")
                m.add((1 - w(n, d - 1)) + w(n, d) + (1 - w(n, d + 1)) - 2 <= t)
                tobi.append(t)
        m.add(sum(runs) <= _run_windows({n: base.table[n] for n in others}, work))
        m.add(sum(tobi) <= pat0["tobi"])

    gb = GiveBack("cannot", base, None, counts=dict(counts), leave_avg=avg, message="この条件では返せません",
                  base_note=note, base_seconds=secs)
    if not takers:
        gb.message = "引き受けた人がいません"
        return gb
    for stage in STAGES:
        add = stage_rules(stage)
        if not add:
            continue
        give = solve_month(facility, staff, rules + add, month, hint=base.table, extra=extra)
        if give.status != "ok":
            continue
        pat1 = pattern_counts({n: give.table[n] for n in others}, facility)
        gb.give, gb.stage = give, stage
        gb.people = [{"name": n, "accepted": counts[n],
                      "leave_before": leave0[n], "leave_after": give.table[n].count(yu),
                      "run_before": run0[n], "run_after": longest_run(give.table[n], work)} for n in takers]
        gb.others = {"run5_before": _run_windows({n: base.table[n] for n in others}, work),
                     "run5_after": _run_windows({n: give.table[n] for n in others}, work),
                     "tobi_before": pat0["tobi"], "tobi_after": pat1["tobi"]}
        gb.checks = {
            "1_no_hard_broken": _hard_broken(give) == 0,
            "2_wish_not_more": _wish_broken(give) <= _wish_broken(base),
            "3_others_leave_same": all(give.table[n].count(yu) == leave0[n] for n in others),
            "4_others_runs_not_more": gb.others["run5_after"] <= gb.others["run5_before"],
            "5_others_tobi_not_more": gb.others["tobi_after"] <= gb.others["tobi_before"],
            "returned": any(p["leave_after"] > p["leave_before"] or p["run_after"] < p["run_before"]
                            for p in gb.people),
        }
        if all(gb.checks.values()):
            gb.status, gb.message = "ok", ""
            return gb
    return gb


def _run_windows(table: dict[str, list[str]], work: set[str], n: int = 5) -> int:
    """n 日続けて勤務の窓の数（6連勤なら2つ）。"""
    return sum(1 for row in table.values() for i in range(len(row) - n + 1)
               if all(c in work for c in row[i:i + n]))


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
