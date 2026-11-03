"""なぜこのマスか（docs/rule-cleanup.md の4）。マスを押すと、そのマスを縛っているルールを3件まで出す。

縛っている＝そのマスを含む場面が、ちょうど上限か下限にある（または破れている）こと。
- そのマスの勤務を数えていて上限に届いている（このマスで上限）・下限ちょうど（このマスが無いと足りない）
- そのマスの勤務は数えていないが上限に届いている（このマスをその勤務にはできない）
下限だけの場面でそのマスを数えていない時は、変えても困らないので出さない。
並べる順は、その日の頼み → その人だけのルール → 話して足したルール → そのほか。数えている物を先に。
"""
from __future__ import annotations

from .model import Facility, Month, Rule, Staff
from .rules import evaluate, expand, with_overrides


def _names(fac: Facility, syms) -> str:
    nm = {st.symbol: st.name for st in fac.shift_types}
    return "・".join(nm.get(s, s) for s in syms)


def _unit(fac: Facility, syms) -> str:
    rest = {st.symbol for st in fac.shift_types if st.kind == "rest"}
    return "日" if set(syms) <= rest else "回"


def why_cell(rules: list[Rule], fac: Facility, staff: list[Staff], month: Month,
             table: dict[str, list[str]], name: str, day: int, limit: int = 3) -> dict:
    """because＝このマスの勤務を数えていて、ちょうど上限か下限にあるルール（この勤務になっている理由）。
    cannot＝このマスの勤務は数えていないが上限に届いているルール（ほかの勤務にできない理由。勤務ごとに1件）。"""
    sym = table[name][day - 1]
    because: dict[str, tuple] = {}
    cannot: dict[str, tuple] = {}
    for r in with_overrides(rules):
        for inst in expand(r, fac, staff, month):
            mine = [t for t in inst.terms if t.staff == name and t.day == day]
            if not mine:
                continue
            v = evaluate(inst, table, month)
            syms = mine[0].symbols
            coef = mine[0].coef
            counted = sym in syms
            at_hi = inst.hi is not None and v >= inst.hi
            at_lo = inst.lo is not None and v <= inst.lo
            # 数えているマスを外すと値は coef だけ減り、数えていないマスをその勤務にすると coef だけ増える。
            # 縛っているのは、その動きで上限か下限を越える時だけ（並びの式は係数が負の項を持つ）
            need = counted and ((coef > 0 and at_lo) or (coef < 0 and at_hi))
            block = (not counted) and ((coef > 0 and at_hi) or (coef < 0 and at_lo))
            if r.kind == "count":
                who = f"{inst.key}さんの" if inst.key != "全体" else ""
                # 「9日で上限」は、公休にできない理由として逆に読まれた（読み手2人とも。2026-11-04）。
                # 下限＝上限（公休は9日ちょうど）の時、このマスがその勤務なのは下限のため、ほかにできないのは上限のため。
                # 役割で言い分ける（上限を先に見て、公休のマスの理由まで「上限」と書いていた。敵対レビュー 2026-11-04）
                if block or (at_hi and not need):
                    fact = f"{who}{_names(fac, syms)}はもう{v}{_unit(fac, syms)}（上限）"
                else:
                    fact = f"{who}{_names(fac, syms)}はちょうど{v}{_unit(fac, syms)}（下限）"
            elif r.kind == "coverage":
                fact = f"{day}日の{_names(fac, syms)} {v}人"
            elif r.kind == "cell":
                fact = "この日の頼み"
            else:
                fact = ""
            rank = 0 if r.kind == "cell" else 1 if r.who and len(r.who) == 1 else 2 if r.meta.get("said") else \
                4 if r.kind == "coverage" else 3
            item = {"rule_id": r.id, "name": r.name, "fact": fact}
            if need or (counted and coef > 0 and at_hi and r.kind in ("coverage", "cell")):
                if r.id not in because or rank < because[r.id][0]:
                    because[r.id] = (rank, item)
            elif block:
                shift = _names(fac, [x for x in syms if x != sym])
                if shift and (shift not in cannot or rank < cannot[shift][0]):
                    cannot[shift] = (rank, {**item, "shift": shift})
    b = [x[1] for x in sorted(because.values(), key=lambda x: (x[0], x[1]["name"]))]
    c = [x[1] for x in sorted(cannot.values(), key=lambda x: (x[0], x[1]["shift"]))]
    return {"staff": name, "day": day, "symbol": sym, "symbol_name": _names(fac, [sym]),
            "because": b[:limit], "cannot": c[:limit], "more": max(0, len(b) - limit) + max(0, len(c) - limit)}
