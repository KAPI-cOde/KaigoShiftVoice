"""表の下に出す、日ごとの勤務の人数の行（ユニットごとと施設全体）。

人数のルールに足りない日・多すぎる日を、表を作る計算と同じ式（with_overrides・expand・evaluate）で印を付ける。
だから行の印と、表を作る時の「守った・外した」が食い違わない。
"""
from __future__ import annotations

from .model import Facility, Month, Rule, Staff
from .rules import _syms, evaluate, expand, with_overrides


def day_counts(fac: Facility, staff: list[Staff], month: Month, rules: list[Rule]) -> list[dict]:
    """行の並び。1行は {"unit": ユニット（施設全体は None）, "symbol": 記号, "name": 勤務の名前,
    "counts": [日ごとの人数], "short": [足りない日], "over": [多すぎる日]}。
    数える勤務は、ふつうの勤務と話して足した勤務（頼まれた時だけの有給・研修を除く）。ユニットが2つ以上ある時は、ユニットごとの行を
    ユニットごとの人数のルールがある勤務だけ出し、施設全体の行は全部の勤務で出す（行が増えすぎないように）。
    施設全体の行のいちばん下に公休の人数も出す（作者の Excel の並べ方。早番・日勤・遅番・夜勤・公休）。"""
    table = month.table
    if not table:
        return []
    days = month.days
    syms = [st.symbol for st in fac.shift_types if st.kind == "work" and (not st.on_request_only or st.spoken)]
    rest = [st.symbol for st in fac.shift_types if st.kind == "rest" and not st.on_request_only]
    multi = len(fac.units) > 1
    unit_of = {s.name: s.unit for s in staff}

    def unit_on(name: str, d: int) -> str | None:   # 応援の日は、その日だけ入るユニットで数える（expand と同じ）
        return month.support.get(f"{name}/{d}", unit_of.get(name))

    # ルールの印。場面の名前 "東/12"・"全体/12" と、数える記号で、行（ユニット, 記号）の日に印を付ける
    marks: dict[tuple, dict[str, set[int]]] = {}
    per_unit_syms: set[str] = set()
    for r in with_overrides(rules):
        if r.kind != "coverage":
            continue
        rs = _syms(r.params["shifts"], fac)
        if r.params.get("scope") == "each_unit":
            per_unit_syms.update(rs)
        for inst in expand(r, fac, staff, month):
            g, d = inst.key.split("/")
            v = evaluate(inst, table, month)
            side = "short" if inst.lo is not None and v < inst.lo else \
                "over" if inst.hi is not None and v > inst.hi else None
            if side is None:
                continue
            unit = None if g == "全体" else g
            for x in rs:
                marks.setdefault((unit, x), {"short": set(), "over": set()})[side].add(int(d))

    names = {st.symbol: st.name for st in fac.shift_types}
    out = []
    groups = ([(u, [x for x in syms if x in per_unit_syms]) for u in fac.units] if multi else []) + [(None, syms + rest)]
    for unit, ss in groups:
        for x in ss:
            counts = [sum(1 for n, row in table.items() if len(row) >= d and row[d - 1] == x
                          and (unit is None or unit_on(n, d) == unit)) for d in range(1, days + 1)]
            mk = marks.get((unit, x), {"short": set(), "over": set()})
            out.append({"unit": unit, "symbol": x, "name": names.get(x, x), "counts": counts,
                        "short": sorted(mk["short"]), "over": sorted(mk["over"])})
    return out
