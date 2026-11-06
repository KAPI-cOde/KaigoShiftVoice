"""ソルバーの芯（CP-SAT）。

- 変数は 職員×日×記号 の0か1で、1マスにちょうど1つ
- 置ける記号は、その人が入れる勤務・公休・明け（夜勤に入れる人）・決まりが指す休暇だけ
- 決まりは rules.expand の場面ごとにスイッチを持つ。fixed は常に守る
- 段1は 本人の希望（wish）と運用の都合（operational）も外せなくして解く。相談できる決まりは重みつきで外せる
- 再現性のため、種・並列数・計算の量（max_deterministic_time）は施設の設定から取る。秒数では区切らない
- その月の職員が30人を超える時と、段を下げて解く時（段2〜4）は、作業者を8つ決まった順で回す（同じ入力なら同じ表。
  large_workers）
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from .interval import short_pairs
from .model import Facility, Month, Rule, Staff
from .rules import Instance, _syms_of, cell_value, check_table, expand, with_overrides

DEFAULT_WEIGHTS = {"wish": 100000, "operational": 20000, "negotiable": 5000}
HOLD_CELLS = 10   # 外れている回数を悪くしないために動かしてよいマスの目安（hold）


@dataclass
class SolveResult:
    status: str                       # ok / infeasible / timeout
    table: dict[str, list[str]] | None
    broken: list[dict] = field(default_factory=list)
    objective: float | None = None
    seconds: float = 0.0
    deterministic_time: float = 0.0
    stage: int = 1
    solver_broken: list = field(default_factory=list)  # ソルバーが外した場面（スイッチ=0）


def _allowed(s: Staff, facility: Facility, requested: set[str]) -> list[str]:
    syms = []
    for st in facility.shift_types:
        if st.on_request_only:
            if st.symbol in requested:
                syms.append(st.symbol)
        elif st.kind == "ake":
            if any(facility.shift(c).next_is_ake for c in s.can):
                syms.append(st.symbol)
        elif st.kind == "rest":
            syms.append(st.symbol)
        elif st.symbol in s.can:
            syms.append(st.symbol)
    return syms


def _requested_leaves(rules: list[Rule], facility: Facility, days: int) -> dict[tuple[str, int], set[str]]:
    """頼まれた時だけ置く記号（有給・研修）を、決まり（cell の in）が指す人・日に限って開ける。
    日付を任せる頼み（回数の下限がある決まり。日数のない有給）は、その人の全日（when があればその日）に開ける。"""
    req_syms = {st.symbol for st in facility.shift_types if st.on_request_only}
    out: dict[tuple[str, int], set[str]] = {}
    for r in rules:
        if r.kind == "count" and r.params.get("min") and r.who:
            syms = set(r.params.get("shifts") or []) & req_syms
            if syms:
                for w in r.who:
                    for d in (r.when or range(1, days + 1)):
                        out.setdefault((w, d), set()).update(syms)
            continue
        if r.kind == "cell" and "in" in r.params:
            syms = set(r.params["in"]) & req_syms
            if syms:
                for w in (r.who or []):
                    for d in (r.when or []):
                        out.setdefault((w, d), set()).update(syms)
    return out


def requested_cells(rules: list[Rule], facility: Facility, staff: list[Staff], month: Month) -> dict[tuple[str, int], set[str]]:
    """頼まれた時だけ置く記号を開ける（人, 日）。話して足した勤務は、人数・回数・曜日のルールの下限でも開ける
    （「平日は日8を1人」は誰と言っていないので、そのルールが数える人と日の全部）。下限の無いルールでは開けない。"""
    out = _requested_leaves(rules, facility, month.days)
    spoken = {st.symbol for st in facility.shift_types if st.spoken}
    if not spoken:
        return out
    for r in rules:
        if r.kind not in ("coverage", "count", "weekday") or \
                not spoken & (_syms_of(r.params.get("shifts")) | _syms_of(r.params.get("in"))):
            continue
        for inst in expand(r, facility, staff, month):
            if inst.lo:
                for t in inst.terms:
                    if t.day >= 1:
                        out.setdefault((t.staff, t.day), set()).update(spoken & set(t.symbols))
    return out


# その月の職員がこの人数を超える時は、表を作る計算で、決まった順で複数の作業者を回す。1人で探すと、56〜100人の
# 施設で時間内に段1の表が見つからず、運用の都合のルールを外したり作れなかったりした（2026-11-05、docs/measurements.md）
LARGE_STAFF = 30
INTERLEAVE_WORKERS = 8
LARGE_DTIME = 40.0   # 作業者の合計の計算の量。20では短い並びが0〜2回残り、40で0回になった（32〜112人、この Mac で3〜19秒）


def large_workers(facility: Facility, staff: list[Staff], stage: int = 1) -> int:
    """その月の職員が30人を超える時と、段を下げて解く時（段2〜4）に回す作業者の数（施設の設定
    solve.interleave_workers があればその数）。段1で30人以下は0。
    取り込んだ時の人数ではなく、その月の人数で決める（後から増えた施設・前の版で取り込んだ施設にも効き、
    30人以下に減った施設は今までどおり1人で探す）。
    段を下げる時は、運用の都合のルールが外せるスイッチになって探す所が広がり、1人では悪い表から抜け出せなかった。
    3ユニット16人の12月で、外れた運用の都合17件・遅番の次の日の早番23回の表を「できました」と出した。計算の量を
    3倍にしても16件・20回のまま。8つ回すと、同じ計算の量で2件・0回、公休は全員9日になった（2026-11-06 に測った）"""
    if len(staff) <= LARGE_STAFF and stage < 2:
        return 0
    return int(facility.settings.get("solve", {}).get("interleave_workers") or INTERLEAVE_WORKERS)


def pinned_cells(rules: list[Rule], flexes: set[str]) -> dict[tuple[str, int], set[str]]:
    """外せないマスの決まり（人と日が決まった cell の in）が置ける記号。"""
    out: dict[tuple[str, int], set[str]] = {}
    for r in with_overrides(rules):
        if r.kind == "cell" and r.flex in flexes and r.params.get("in"):
            for w in r.who or []:
                for d in r.when or []:
                    out[(w, d)] = out.get((w, d), set(r.params["in"])) & set(r.params["in"])
    return out


def forced_dev(inst: Instance, pins: dict[tuple[str, int], set[str]]) -> int:
    """外せないマスだけで決まる、回数のルールからのずれ（14日の希望休と公休9日なら5）。"""
    least = most = 0
    for t in inst.terms:
        p = pins.get((t.staff, t.day))
        most += t.coef if p is None or p & set(t.symbols) else 0
        least += t.coef if p and p <= set(t.symbols) else 0
    return max(0, least - inst.hi if inst.hi is not None else 0, inst.lo - most if inst.lo is not None else 0)


def solve_month(facility: Facility, staff: list[Staff], rules: list[Rule], month: Month, *,
                hint: dict[str, list[str]] | None = None, stage: int = 1,
                fixed_cells: dict[tuple[str, int], str] | None = None,
                max_dtime: float | None = None,
                skip: set[tuple[str, str]] | None = None,
                dev_cap: int | None = None,
                hold: dict[tuple[str, str], int] | None = None,
                extra=None) -> SolveResult:
    """fixed_cells は 動かさないマス（人, 日）→ 記号。狭い範囲の直しで、範囲の外を止めるのに使う。
    skip は課さない場面（決まりの id, 場面の名前）。相談できる決まりで前の表で既に外れていた場面に使う。
    dev_cap は、運用の都合の回数のルールを外す時の、1人あたりのずれの上限。
    hold は skip の場面のうち、今の値（場面 → 値）からルールと反対へ離れるのを罰する場面。"""
    st = facility.settings.get("solve", {})
    weights = {**DEFAULT_WEIGHTS, **facility.settings.get("weights", {})}
    quality = facility.settings.get("quality", {})
    D = month.days
    days = range(1, D + 1)
    req = requested_cells(rules, facility, staff, month)

    m = cp_model.CpModel()
    x: dict[tuple[str, int, str], cp_model.IntVar] = {}
    allowed: dict[tuple[str, int], list[str]] = {}
    for s in staff:
        for d in days:
            syms = _allowed(s, facility, req.get((s.name, d), set()))
            allowed[(s.name, d)] = syms
            vs = []
            for sym in syms:
                v = m.new_bool_var(f"x[{s.name},{d},{sym}]")
                x[(s.name, d, sym)] = v
                vs.append(v)
            m.add_exactly_one(vs)
            if fixed_cells and (s.name, d) in fixed_cells:
                sym = fixed_cells[(s.name, d)]
                if sym in syms:
                    m.add(x[(s.name, d, sym)] == 1)

    def lin(inst: Instance):
        expr, const = [], 0
        for t in inst.terms:
            if t.day < 1:
                v = cell_value({}, month, t.staff, t.day)
                if v is not None and v in t.symbols:
                    const += t.coef
                continue
            for sym in t.symbols:
                var = x.get((t.staff, t.day, sym))
                if var is not None:
                    expr.append(t.coef * var)
        return sum(expr) + const if expr else const

    hard_flex = {"fixed"} if stage >= 4 else ({"fixed", "wish", "operational"} if stage == 1 else
                                              {"fixed", "operational"} if stage == 2 else {"fixed", "wish"})
    pins = pinned_cells(rules, hard_flex)
    penalties = []
    switches: list[tuple[Instance, cp_model.IntVar | None]] = []
    for r in with_overrides(rules):
        for inst in expand(r, facility, staff, month):
            now = (hold or {}).get((r.id, inst.key))
            if skip and (r.id, inst.key) in skip and now is None:
                continue  # 前の表で既に外れていた場面は、直しても点にしない
            e = lin(inst)
            if isinstance(e, int):  # 定数だけの場面（前の月だけ）は課せない
                continue
            if now is not None:
                # 直す点にはしないが、これ以上離れるなら休みを振り替える方を選ぶ。十数マス動かすほどなら離れて知らせる（resolve）
                far = m.new_int_var(0, D, "")
                m.add(far >= (e - now if inst.hi is not None and now > inst.hi else now - e))
                penalties.append(HOLD_CELLS * facility.settings.get("keep_weight", 500) * far)
                continue
            hard = r.flex in hard_flex
            if hard:
                if inst.lo is not None:
                    m.add(e >= inst.lo)
                if inst.hi is not None:
                    m.add(e <= inst.hi)
                switches.append((inst, None))
            else:
                b = m.new_bool_var(f"keep[{r.id}:{inst.key}]")
                if inst.lo is not None:
                    m.add(e >= inst.lo).only_enforce_if(b)
                if inst.hi is not None:
                    m.add(e <= inst.hi).only_enforce_if(b)
                w = r.weight or (weights[r.flex] if not r.light else facility.settings.get("light_weight", 100))
                penalties.append(w * (1 - b))
                switches.append((inst, b))
                if r.kind == "count" and r.flex == "operational":
                    # 外した後のずれに上限を付ける。外すかどうかだけだと、外す人を少なくするために1人へ全部寄り、公休9日の施設で
                    # 1人だけ19日の表ができた（2026-11-06 通しの試し）。上限が無い時も、ずれの分だけ罰する。ずれの2乗・段階の罰・
                    # いちばん大きいずれの罰は、希望休が入ると時間内に解ききれず、30日全休の人が出たので使わない（同じ日に測った）
                    # 希望休などで避けられない分は、その人にだけ上限へ足す。1人の希望で全員の上限を広げると、ほかの人へ寄った
                    dev = m.new_int_var(0, D if dev_cap is None else max(dev_cap, forced_dev(inst, pins)), "")
                    if inst.lo is not None:
                        m.add(dev >= inst.lo - e)
                    if inst.hi is not None:
                        m.add(dev >= e - inst.hi)
                    penalties.append(w // 4 * dev)

    # 質の物差し（施設の設定。既定は控えめ）
    work = set(facility.workday_symbols())
    rest_syms = [t.symbol for t in facility.shift_types if t.kind in ("rest", "leave")]

    def is_rest(name, d):
        return sum(x[(name, d, sym)] for sym in rest_syms if (name, d, sym) in x)

    w_tobi = quality.get("tobiishi", 0)
    if w_tobi:
        for s in staff:
            for d in range(2, D):
                t = m.new_bool_var("")
                # 休み・勤務・休み の時 t=1
                m.add(is_rest(s.name, d - 1) + (1 - is_rest(s.name, d)) + is_rest(s.name, d + 1) - 2 <= t)
                penalties.append(w_tobi * t)
    w_same3 = quality.get("same_shift_3run", 0)
    if w_same3:
        for s in staff:
            for sym in s.can:
                if facility.shift(sym).next_is_ake:
                    continue
                for d in range(1, D - 1):
                    vs = [x.get((s.name, dd, sym)) for dd in (d, d + 1, d + 2)]
                    if all(v is not None for v in vs):
                        t = m.new_bool_var("")
                        m.add(sum(vs) - 2 <= t)
                        penalties.append(w_same3 * t)
    w_wk = quality.get("weekend_off_spread", 0)
    if w_wk:
        night = [s for s in staff if any(facility.shift(c).next_is_ake for c in s.can)]
        wk_days = [d for d in days if month.is_weekend_or_holiday(d)]
        offs = []
        for s in night:
            v = m.new_int_var(0, len(wk_days), "")
            m.add(v == sum(is_rest(s.name, d) for d in wk_days))
            offs.append(v)
        if offs:
            hi, lo = m.new_int_var(0, len(wk_days), ""), m.new_int_var(0, len(wk_days), "")
            m.add_max_equality(hi, offs)
            m.add_min_equality(lo, offs)
            penalties.append(w_wk * (hi - lo))
    w_gap = quality.get("short_rest", 0)
    if w_gap:
        # 次の勤務までの間が線（時間）より短い並びを、短いほど重く罰する（夜勤・明け・休みは数えない）
        pairs = short_pairs(facility.shift_types, quality.get("short_rest_hours", 14))
        for s in staff:
            last = cell_value({}, month, s.name, 0)   # 前の月の末日（分かれば）
            for (a, b), gap in pairs.items():
                if last == a and (s.name, 1, b) in x:
                    penalties.append(int(w_gap * gap) * x[(s.name, 1, b)])
            for d in range(1, D):
                for (a, b), gap in pairs.items():
                    va, vb = x.get((s.name, d, a)), x.get((s.name, d + 1, b))
                    if va is not None and vb is not None:
                        t = m.new_bool_var("")
                        m.add(va + vb - 1 <= t)
                        penalties.append(int(w_gap * gap) * t)
    if hint:
        w_keep = facility.settings.get("keep_weight", 500)
        for s in staff:
            for d in days:
                old = hint.get(s.name, [None] * D)[d - 1]
                if old in allowed[(s.name, d)]:
                    m.add_hint(x[(s.name, d, old)], 1)
                    penalties.append(w_keep * (1 - x[(s.name, d, old)]))

    if extra is not None:  # 目的と制約を足す口（返す案など）。extra(model, x, penalties)
        extra(m, x, penalties)

    m.minimize(sum(penalties))

    solver = cp_model.CpSolver()
    # 月ごとの種（撮影用に固定した一組。施設の設定 seed_by_month）。無ければ施設の種
    solver.parameters.random_seed = st.get("seed_by_month", {}).get(f"{month.year:04d}-{month.month:02d}",
                                                                    st.get("seed", 11))
    iw = large_workers(facility, staff, stage)
    dtime = st.get("max_deterministic_time", 30.0)
    if iw:
        # その月の職員が30人を超える時と段を下げる時は、決まった順で複数の作業者を回す。区切りの大きさも決めるので、
        # 同じ入力なら同じ表になる。近くを探し直す作業者が入り、1人で探すより短い並びとルールの外れが減る（2026-11-05・
        # 11-06 に測った。docs/measurements.md）。計算の量は作業者の合計で数えるので、30人を超える月は LARGE_DTIME まで
        # 増やす（30人以下で段を下げる時は施設の量のまま。16人の12月は20で足りた）
        solver.parameters.num_workers = iw
        solver.parameters.interleave_search = True
        solver.parameters.interleave_batch_size = iw
        if len(staff) > LARGE_STAFF:
            dtime = max(dtime, LARGE_DTIME)
    else:
        solver.parameters.num_workers = st.get("workers", 1)
    solver.parameters.max_deterministic_time = max_dtime if max_dtime is not None else dtime
    t0 = time.perf_counter()
    code = solver.solve(m)
    secs = time.perf_counter() - t0
    if code in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        table = {s.name: [next(sym for sym in allowed[(s.name, d)] if solver.value(x[(s.name, d, sym)]))
                          for d in days] for s in staff}
        res = SolveResult("ok", table, objective=solver.objective_value, seconds=secs,
                          deterministic_time=solver.deterministic_time, stage=stage)
        res.broken = check_table(rules, facility, staff, month, table)
        res.solver_broken = [{"rule": i.rule.id, "key": i.key} for i, b in switches
                             if b is not None and not solver.value(b)]
        return res
    status = "infeasible" if code == cp_model.INFEASIBLE else "timeout"
    return SolveResult(status, None, seconds=secs, deterministic_time=solver.deterministic_time, stage=stage)
