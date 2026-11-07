"""ソルバーの芯（CP-SAT）。

- 変数は 職員×日×記号 の0か1で、1マスにちょうど1つ
- 置ける記号は、その人が入れる勤務・公休・明け（夜勤に入れる人）・決まりが指す休暇だけ
- 決まりは rules.expand の場面ごとにスイッチを持つ。fixed は常に守る
- 段1は 本人の希望（wish）と運用の都合（operational）も外せなくして解く。相談できる決まりは重みつきで外せる
- 再現性のため、種・並列数・計算の量（max_deterministic_time）は施設の設定から取る。秒数では区切らない
- その月の職員が30人を超える時と、段を下げて解く時（段2〜4）と、取り込んだ施設では、作業者を8つ決まった順で回す
  （同じ入力なら同じ表。large_workers）
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from .interval import short_pairs
from .model import Facility, Month, Rule, Staff
from .rules import Instance, _syms_of, cell_value, check_table, expand, is_day_off_wish, is_fair_on, with_overrides

DEFAULT_WEIGHTS = {"wish": 100000, "operational": 20000, "negotiable": 5000}
# 希望休（rules.is_day_off_wish）の重み。本人の希望を外す段（段2・段4）でも、ほかの本人の希望（曜日の勤務の希望など）と
# 運用の都合より後に外す（作者 2026-11-07「希望休は原則ハードルール。どうしても組めない時のみ相談する」）。前は本人の希望と
# 同じ重みで、曜日の希望を残して希望休を外すことがあった
DAY_OFF_WISH_WEIGHT = 1000000
# なるべく平等に入れる有給（count の fair）の罰。1人の有給の k 日目に LEAVE_EACH + (k−1)×LEAVE_STEP（1日目1000・
# 2日目3000・3日目5000…）。1人に寄せるほど重いので、全員に1日ずつ配る方を選ぶ。1日目の罰で、人が余らない日には入れない。
# 最多と最少の差を罰する形では、計算が時間内に良い表を見つけられず、12月（17人）で有給41日・1人0〜7日になった（2026-11-07）
LEAVE_EACH_WEIGHT = 1000
LEAVE_STEP_WEIGHT = 2000
# 手薄なユニットで働く回数の平均化（rule の thin_even）の罰。相談できるルール（5000）より軽く、可能な範囲で
THIN_EVEN_WEIGHT = 1000   # 300では回数が2〜8回のまま、3000では配慮の外れが増えた（17人の12月）
THIN_EVEN_STEPS = 12
HOLD_CELLS = 10   # 外れている回数を悪くしないために動かしてよいマスの目安（hold）
# 言われた勤務の人ごとの回数をならす（rule の even。「早番も遅番も、可能な範囲で平均化して」）罰。数える人の平均の切り捨てを
# t として、t〜t+1 回の帯から外れた分に、k 回を超えた分ごとに EVEN_WEIGHT（外れが大きいほど1回が重いので、多い人から少ない
# 人へ移す方を選ぶ）。帯の中は罰さないので、総数を減らしても得が無い（回数そのものを罰すると、人数に下限しかない施設で早番の
# 総数が60から30に減った）。1回目の外れは相談できるルールの1件（5000）より軽い。一言の直しでは1マス動かすごとに
# keep_weight（500）がかかる。見本の11月（遅番の次の日の早番・遅番の3連続をやめた後）で、1000 では「遅番が偏らないように」が
# 3〜5回で止まり、1500 で3〜4回。3000 まで上げても遅番の次の日の早番・ルールの外れは0のままだった（2026-11-08 に測った）
EVEN_WEIGHT = 1500
EVEN_STEPS = 12


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


def _requested_leaves(rules: list[Rule], facility: Facility, days: int,
                      staff: list[Staff] | None = None) -> dict[tuple[str, int], set[str]]:
    """頼まれた時だけ置く記号（有給・研修）を、決まり（cell の in）が指す人・日に限って開ける。
    日付を任せる頼み（回数の下限がある決まり。日数のない有給）は、その人の全日（when があればその日）に開ける。
    働かせた、なるべく平等に入れる有給（rules.fair_on）は、ルールが数える人（誰と言わなければ全員）の全日に開ける。"""
    req_syms = {st.symbol for st in facility.shift_types if st.on_request_only}
    out: dict[tuple[str, int], set[str]] = {}
    for r in rules:
        if is_fair_on(r):
            syms = set(r.params.get("shifts") or []) & req_syms
            for w in (r.who or [s.name for s in staff or []]):
                for d in (r.when or range(1, days + 1)):
                    out.setdefault((w, d), set()).update(syms)
            continue
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
    out = _requested_leaves(rules, facility, month.days, staff)
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
    3倍にしても16件・20回のまま。8つ回すと、同じ計算の量で2件・0回、公休は全員9日になった（2026-11-06 に測った）。
    取り込んだ施設（設定に pasted_month がある）は、30人以下でも段1から回す。次の勤務までの間の罰が入るので探す所が広く、
    実際の施設の形の18人の10月で、1人では短い並び52回・24秒、8つ（計算の量40）では0回・17秒だった（2026-11-06）。見本の施設は
    取り込んでいないので、今までどおり1人で探す（表は変わらない）"""
    if len(staff) <= LARGE_STAFF and stage < 2 and "pasted_month" not in facility.settings:
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


def _fair_leave(m, x, penalties, rules: list[Rule], facility: Facility, staff: list[Staff], days, weights: dict) -> None:
    """なるべく平等に入れる有給（count の fair）の目的。数える人ごとに、有給の k 日目を LEAVE_EACH + (k−1)×LEAVE_STEP で
    罰する（日数が増えるほど1日あたりが重い。施設の設定 weights の leave_each・leave_step で変えられる）。"""
    fair = [r for r in rules if is_fair_on(r)]
    if not fair:
        return
    w_each = weights.get("leave_each", LEAVE_EACH_WEIGHT)
    w_step = weights.get("leave_step", LEAVE_STEP_WEIGHT)
    names = {s.name for s in staff}
    for r in fair:
        syms = set(_syms_of(r.params.get("shifts")))
        ds = list(r.when) if r.when else list(days)
        for name in (r.who or [s.name for s in staff]):
            if name not in names:
                continue
            vs = [x[(name, d, sym)] for d in ds for sym in syms if (name, d, sym) in x]
            if not vs:
                continue
            c = sum(vs)
            penalties.append(w_each * c)
            for k in range(1, min(len(vs), 8)):   # k 日を超えた分（2日目からの重さ。9日目からは同じ重さ）
                over = m.new_int_var(0, len(vs), "")
                m.add(over >= c - k)
                penalties.append(w_step * over)


def _thin_even(m, x, penalties, rules: list[Rule], facility: Facility, staff: list[Staff], days, weights: dict) -> None:
    """手薄なユニット（その日に none の勤務の人がいないユニット）で work の勤務に入る回数を、人ごとに平均化する目的（rule の
    thin_even）。回数が増えるほど1回が重くなる罰（k 回を超えた分ごとに weights の thin_even）。"""
    w = weights.get("thin_even", THIN_EVEN_WEIGHT)
    for r in rules:
        if r.kind != "thin_even" or not r.meta.get("thin_on"):
            continue
        none, work = _syms_of(r.params["none"]), _syms_of(r.params["work"])
        units: dict[str, list[str]] = {}
        for s in staff:
            units.setdefault(s.unit, []).append(s.name)
        for names in units.values():
            ys: dict[str, list] = {n: [] for n in names}
            for d in days:
                thin = m.new_bool_var("")
                m.add(thin + sum(x[(n, d, sym)] for n in names for sym in none if (n, d, sym) in x) >= 1)
                for n in names:
                    vs = [x[(n, d, sym)] for sym in work if (n, d, sym) in x]
                    if vs:
                        y = m.new_bool_var("")
                        m.add(y >= thin + sum(vs) - 1)
                        ys[n].append(y)
            for n, vs in ys.items():
                for k in range(1, min(len(vs), THIN_EVEN_STEPS)):
                    over = m.new_int_var(0, len(vs), "")
                    m.add(over >= sum(vs) - k)
                    penalties.append(w * over)


def _own_bounds(rules: list[Rule], r: Rule, name: str, sym: str) -> tuple[int | None, int | None]:
    """その人の名指しの回数のルール（sym を数える count）の下限と上限。ならすルールがその人を名指ししていれば数えない。"""
    if r.who and name in r.who:
        return None, None
    lo = hi = None
    for q in rules:
        if q.kind != "count" or not q.who or name not in q.who or sym not in _syms_of(q.params.get("shifts")):
            continue
        if q.params.get("min") is not None:
            lo = max(lo or 0, q.params["min"])
        if q.params.get("max") is not None:
            hi = q.params["max"] if hi is None else min(hi, q.params["max"])
    return lo, hi


def _even(m, x, penalties, rules: list[Rule], facility: Facility, staff: list[Staff], days, weights: dict,
          hint: dict[str, list[str]] | None = None) -> None:
    """言われた勤務の人ごとの回数をならす目的（rule の even）。勤務ごとに、数える人の平均の切り捨て t を帯の下にして、
    t〜t+1 回の人は罰さず、帯から外れた分を罰する。数えるのは、その勤務に入れる人だけ（入れない人を0回として数えない）。
    名指しの回数のルールがある人は、帯をそのルールの回数まで広げる（「月7回以上」なら t〜7 回は罰さない）。帯の外は数える
    ので、ほかの人をならすためにその人へ寄せもしない。本人の希望は外せない強さで先に効く。
    前の表（hint）があれば、数える人のその勤務の合計が前の表から離れた分も罰する。ならすのは人の間で移すことで、
    帯から外れた人を、勤務を消したり足したりして帯に入れると、移すより動かすマスが少なくて済むため。"""
    w = weights.get("even", EVEN_WEIGHT)
    for r in rules:
        if r.kind != "even":
            continue
        ds = list(r.when) if r.when else list(days)
        for sym in sorted(_syms_of(r.params.get("shifts"))):
            people, names = [], []
            for s in staff:
                if (r.who and s.name not in r.who) or sym not in s.can:
                    continue
                vs = [x[(s.name, d, sym)] for d in ds if (s.name, d, sym) in x]
                if vs:
                    people.append((sum(vs), len(vs), *_own_bounds(rules, r, s.name, sym)))
                    names.append(s.name)
            if len(people) < 2:
                continue
            top = max(n for _, n, _, _ in people)
            t = m.new_int_var(0, top, "")
            total = sum(c for c, _, _, _ in people)
            m.add(len(people) * t <= total)
            m.add(total <= len(people) * t + len(people) - 1)
            if hint:
                old = sum(1 for nm in names for d in ds if (hint.get(nm) or [])[d - 1:d] == [sym])
                gap = m.new_int_var(0, len(people) * top, "")
                m.add(gap >= total - old)
                m.add(gap >= old - total)
                penalties.append(w * gap)
            for c, n, lo, hi in people:
                upper, lower = t + 1, t
                if lo is not None:
                    upper = m.new_int_var(0, top + 1, "")
                    m.add_max_equality(upper, [t + 1, min(lo, n)])
                if hi is not None:
                    lower = m.new_int_var(0, top, "")
                    m.add_min_equality(lower, [t, hi])
                for k in range(min(n, EVEN_STEPS)):
                    up, down = m.new_int_var(0, n, ""), m.new_int_var(0, top, "")
                    m.add(up >= c - upper - k)
                    m.add(down >= lower - c - k)
                    penalties.append(w * up)
                    penalties.append(w * down)


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
                if is_day_off_wish(r, facility):
                    w = max(w, weights.get("day_off_wish", DAY_OFF_WISH_WEIGHT))
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

    _fair_leave(m, x, penalties, rules, facility, staff, days, weights)
    _thin_even(m, x, penalties, rules, facility, staff, days, weights)
    _even(m, x, penalties, rules, facility, staff, days, weights, hint)

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
    iw = large_workers(facility, staff, stage)
    if hint:
        w_keep = facility.settings.get("keep_weight", 500)
        for s in staff:
            for d in days:
                old = hint.get(s.name, [None] * D)[d - 1]
                if old in allowed[(s.name, d)]:
                    if not iw:
                        # 作業者を回す時は、前の表を計算の手がかりに渡さない（近づける罰は下で同じに課す）。OR-Tools 9.15 は、
                        # 手がかりのあるモデルが読み込みの時点で作れないと分かると、探し方の1つ（max_lp）が手がかりを
                        # 使おうとして止まり、アプリごと落ちた（Check failed: heuristics.fixed_search）。36人の施設の
                        # 一言と、段を下げた一言の直しで起きた。渡さなくても、12通りの一言で動いたマスは同じだった（2026-11-06）
                        m.add_hint(x[(s.name, d, old)], 1)
                    penalties.append(w_keep * (1 - x[(s.name, d, old)]))

    if extra is not None:  # 目的と制約を足す口（返す案など）。extra(model, x, penalties)
        extra(m, x, penalties)

    m.minimize(sum(penalties))

    solver = cp_model.CpSolver()
    # 月ごとの種（撮影用に固定した一組。施設の設定 seed_by_month）。無ければ施設の種
    solver.parameters.random_seed = st.get("seed_by_month", {}).get(f"{month.year:04d}-{month.month:02d}",
                                                                    st.get("seed", 11))
    dtime = st.get("max_deterministic_time", 30.0)
    if iw:
        # その月の職員が30人を超える時と段を下げる時は、決まった順で複数の作業者を回す。区切りの大きさも決めるので、
        # 同じ入力なら同じ表になる。近くを探し直す作業者が入り、1人で探すより短い並びとルールの外れが減る（2026-11-05・
        # 11-06 に測った。docs/measurements.md）。計算の量は作業者の合計で数えるので、30人を超える月と取り込んだ施設は
        # LARGE_DTIME まで増やす（実際の施設の形の18人の10月で、20では日勤の次の日の早番が4回残り、40で0回。13.8→17.0秒）。
        # 見本の施設が段を下げる時は施設の量のまま
        solver.parameters.num_workers = iw
        solver.parameters.interleave_search = True
        solver.parameters.interleave_batch_size = iw
        if len(staff) > LARGE_STAFF or "pasted_month" in facility.settings:
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
